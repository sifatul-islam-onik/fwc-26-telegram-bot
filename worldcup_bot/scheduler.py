"""APScheduler configuration and background jobs for reminders and polling."""
import logging
import asyncio
from datetime import datetime, timedelta
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

import state
import football_api
import notifier
from notifier import _format_stage as _pretty_stage, _format_group as _pretty_group  # re-export aliases

logger = logging.getLogger(__name__)

# Module-level variable
_scheduler: BackgroundScheduler = None

# In-memory score cache: {match_id: (home_score, away_score)}
# Used by the live-goal poller to detect score changes between polls.
_live_score_cache: dict[int, tuple[int, int]] = {}

def _run_async(application, coro):
    """Helper to run async functions from APScheduler's synchronous workers
    on the application's main event loop.
    """
    loop = application.bot_data.get("main_loop")
    if not loop:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

    if loop and loop.is_running():
        try:
            curr_loop = asyncio.get_running_loop()
        except RuntimeError:
            curr_loop = None

        if curr_loop == loop:
            loop.create_task(coro)
        else:
            asyncio.run_coroutine_threadsafe(coro, loop)
    else:
        asyncio.run(coro)

# _pretty_stage and _pretty_group are imported from notifier above.
# They are kept as aliases here so any existing call-sites continue to work.

def _pretty_group(group: str | None) -> str:
    """'GROUP_A' -> 'Group A'. Returns '' if group is None."""
    if not group:
        return ""
    return group.replace("_", " ").title()

def sync_schedule(application):
    """The master re-sync function. Called on startup and every 24 hours."""
    logger.info("Starting sync_schedule...")
    
    reminders_count = 0
    pollers_count = 0
    futures_count = 0
    
    users = state.get_all_users()
    now_utc = datetime.now(pytz.utc)  # computed once for the entire sync
    
    # Clear all existing reminder jobs first to prevent stale jobs (e.g. from removed teams or changed offsets)
    for job in list(_scheduler.get_jobs()):
        if job.id.startswith("remind_"):
            _scheduler.remove_job(job.id)

    # PART A — Pre-match reminders (favourite teams only)
    # Fetch all matches once; filter per-team locally to save API calls.
    all_wc_matches = football_api.get_all_wc_matches()

    for chat_id in users:
        fav_teams = state.get_favourite_teams(chat_id)
        if not fav_teams:
            continue

        offsets = state.get_reminder_offsets(chat_id)

        all_fav_matches = {}
        for team in fav_teams:
            for match in football_api.filter_team_matches(all_wc_matches, team["id"]):
                match_id = match.get("id")
                if match_id:
                    all_fav_matches[match_id] = match
                    
        for match_id, match in all_fav_matches.items():
            status = match.get("status")
            
            if status in ("SCHEDULED", "TIMED"):
                match_dt = datetime.fromisoformat(match["utcDate"].replace("Z", "+00:00"))
                
                for offset in offsets:
                    job_id = f"remind_{chat_id}_{match_id}_{offset}"
                    reminder_dt = match_dt - timedelta(minutes=offset)
                    
                    if reminder_dt <= now_utc:
                        continue
                        
                    def make_reminder_func(app, c_id, m):
                        def wrapper():
                            _run_async(app, notifier.send_reminder(app, c_id, m))
                        return wrapper
                    
                    _scheduler.add_job(
                        func=make_reminder_func(application, chat_id, match),
                        trigger=DateTrigger(run_date=reminder_dt),
                        id=job_id,
                        replace_existing=True
                    )
                    reminders_count += 1

    # PART B — Result pollers (ALL WC matches) — reuse the list already fetched above
    all_matches = all_wc_matches
    for match in all_matches:
        match_id = match.get("id")
        if not match_id or state.is_notified(match_id, "result"):
            continue
            
        status = match.get("status")
        utc_date_str = match.get("utcDate")
        if not utc_date_str:
            continue
        match_dt = datetime.fromisoformat(utc_date_str.replace("Z", "+00:00"))
        # now_utc is already computed at the top of sync_schedule
        
        home_name = match.get("homeTeam.name", "TBD")
        away_name = match.get("awayTeam.name", "TBD")
        
        if status in ("FINISHED", "AWARDED", "CANCELLED"):
            _run_async(application, notifier.send_result(application, match))
            state.mark_notified(match_id, 'result')
            continue
            
        if match_dt <= now_utc or status in ("IN_PLAY", "PAUSED", "EXTRA_TIME", "PENALTY_SHOOTOUT"):
            poll_job_id = f"poll_{match_id}"
            _scheduler.add_job(
                func=_poll_result,
                trigger=IntervalTrigger(minutes=3),
                args=[application, match_id, home_name, away_name],
                id=poll_job_id,
                replace_existing=True
            )
            pollers_count += 1
            # Ensure live-goal poller is active
            _arm_live_poller(application)
            
        elif match_dt > now_utc:
            startpoll_job_id = f"startpoll_{match_id}"
            _scheduler.add_job(
                func=_arm_result_poller,
                trigger=DateTrigger(run_date=match_dt),
                args=[application, match_id, home_name, away_name],
                id=startpoll_job_id,
                replace_existing=True
            )
            futures_count += 1
            
    logger.info(f"Sync complete. Reminders: {reminders_count}. Result pollers running: {pollers_count}. Future matches queued: {futures_count}.")

def _arm_result_poller(application, match_id: int, home_name: str, away_name: str):
    """Adds the IntervalTrigger polling job for a match at the moment it starts."""
    job_id = f"poll_{match_id}"
    if not _scheduler.get_job(job_id):
        _scheduler.add_job(
            func=_poll_result,
            trigger=IntervalTrigger(minutes=3),
            args=[application, match_id, home_name, away_name],
            id=job_id,
            replace_existing=True
        )
    # Also make sure the live-goal poller is running
    _arm_live_poller(application)


def _arm_live_poller(application):
    """Ensures the 10-second live-goal poller job is running."""
    if _scheduler and not _scheduler.get_job("live_poller"):
        _scheduler.add_job(
            func=_poll_live_goals,
            trigger=IntervalTrigger(seconds=10),
            args=[application],
            id="live_poller",
            replace_existing=True
        )
        logger.info("Live-goal poller armed (10-second interval).")


def _stop_live_poller():
    """Removes the live-goal poller when no matches are active."""
    if _scheduler and _scheduler.get_job("live_poller"):
        _scheduler.remove_job("live_poller")
        logger.info("Live-goal poller stopped (no live matches).")

def _poll_result(application, match_id: int, home: str, away: str):
    try:
        match = football_api.get_match(match_id)
    except football_api.RateLimitException as e:
        logger.warning(f"Rate limited polling match {match_id}. Will automatically retry next minute. (Wait {e.retry_after}s)")
        return
        
    if not match:
        logger.error(f"Failed to fetch match {match_id} for result polling.")
        return
        
    status = match.get("status")
    
    if status in ("FINISHED", "AWARDED", "CANCELLED"):
        _run_async(application, notifier.send_result(application, match))
        state.mark_notified(match_id, 'result')
        
        poll_job_id = f"poll_{match_id}"
        startpoll_job_id = f"startpoll_{match_id}"
        if _scheduler.get_job(poll_job_id): _scheduler.remove_job(poll_job_id)
        if _scheduler.get_job(startpoll_job_id): _scheduler.remove_job(startpoll_job_id)

        # Clean up score cache entry for this match
        _live_score_cache.pop(match_id, None)
        return
        
    # Safety cutoff (180 mins)
    match_dt_str = match.get("utcDate")
    if match_dt_str:
        match_dt = datetime.fromisoformat(match_dt_str.replace("Z", "+00:00"))
        now_utc = datetime.now(pytz.utc)
        if (now_utc - match_dt).total_seconds() > 180 * 60:
            logger.warning(f"Safety cutoff reached for match {match_id}. Removing poller.")
            poll_job_id = f"poll_{match_id}"
            if _scheduler.get_job(poll_job_id): _scheduler.remove_job(poll_job_id)
            _live_score_cache.pop(match_id, None)


def _poll_live_goals(application):
    """Polls all currently live WC matches every 10 seconds and sends a goal
    alert to all users whenever the score changes for any match.

    Also detects when live matches are finished and broadcasts the final results.

    Uses a single API call (get_all_wc_matches) to minimise API usage.
    Stops itself automatically when no matches are live.
    """
    try:
        all_matches = football_api.get_all_wc_matches(bypass_cache=True)
    except football_api.RateLimitException as e:
        logger.warning(f"Live-goal poller rate-limited. Skipping this tick. (Wait {e.retry_after}s)")
        return

    if not all_matches:
        return

    LIVE_STATUSES = ("IN_PLAY", "PAUSED", "EXTRA_TIME", "PENALTY_SHOOTOUT")
    FINISHED_STATUSES = ("FINISHED", "AWARDED", "CANCELLED", "POSTPONED")
    now_utc = datetime.now(pytz.utc)

    live_matches = []
    finished_matches_lookup = {}
    for m in all_matches:
        status = m.get("status")
        if status in LIVE_STATUSES:
            live_matches.append(m)
        elif status not in FINISHED_STATUSES:
            utc_date_str = m.get("utcDate")
            if utc_date_str:
                match_dt = datetime.fromisoformat(utc_date_str.replace("Z", "+00:00"))
                elapsed = (now_utc - match_dt).total_seconds()
                if 0 <= elapsed <= 130 * 60:
                    live_matches.append(m)
        else:
            finished_matches_lookup[m.get("id")] = m

    live_match_ids = set()
    for match in live_matches:
        match_id = match.get("id")
        if not match_id:
            continue
        live_match_ids.add(match_id)

        new_home = match.get("score.fullTime.home")
        new_away = match.get("score.fullTime.away")

        # Skip if score is not available (free-tier may omit it)
        if new_home is None or new_away is None:
            continue

        new_home = int(new_home)
        new_away = int(new_away)

        prev = _live_score_cache.get(match_id)
        if prev is None:
            # First time we see this match — seed the cache, don't alert
            _live_score_cache[match_id] = (new_home, new_away)
            continue

        prev_home, prev_away = prev
        if new_home != prev_home or new_away != prev_away:
            # Score changed — fire goal alert and update cache
            logger.info(
                f"GOAL detected in match {match_id}: "
                f"{prev_home}-{prev_away} -> {new_home}-{new_away}"
            )
            _live_score_cache[match_id] = (new_home, new_away)
            _run_async(application, notifier.send_goal_alert(application, match, prev_home, prev_away))

    # Detect finished matches (in _live_score_cache but not in live_matches)
    for cached_id in list(_live_score_cache.keys()):
        if cached_id not in live_match_ids:
            finished_match = finished_matches_lookup.get(cached_id)
            if finished_match:
                status = finished_match.get("status")
                if status in ("FINISHED", "AWARDED", "CANCELLED"):
                    if not state.is_notified(cached_id, "result"):
                        logger.info(f"Match {cached_id} finished. Sending result notification.")
                        _run_async(application, notifier.send_result(application, finished_match))
                        state.mark_notified(cached_id, "result")
                        
                        # Clean up jobs for this match
                        poll_job_id = f"poll_{cached_id}"
                        startpoll_job_id = f"startpoll_{cached_id}"
                        if _scheduler.get_job(poll_job_id):
                            _scheduler.remove_job(poll_job_id)
                        if _scheduler.get_job(startpoll_job_id):
                            _scheduler.remove_job(startpoll_job_id)
            
            # Remove from cache as it's no longer live
            _live_score_cache.pop(cached_id, None)

    if not live_matches:
        # No live matches — shut down the poller to save API quota
        _stop_live_poller()

def _sync_schedule_background(application):
    try:
        sync_schedule(application)
    except football_api.RateLimitException as e:
        logger.warning(f"Background sync rate limited. Auto-retrying later. ({e.retry_after}s)")

def start_scheduler(application):
    global _scheduler
    _scheduler = BackgroundScheduler(timezone=pytz.utc)
    
    if not _scheduler.running:
        _scheduler.start()
        
    _scheduler.add_job(
        func=_sync_schedule_background,
        trigger=IntervalTrigger(minutes=30),
        args=[application],
        id="sync_schedule",
        replace_existing=True
    )
    
    _scheduler.add_job(
        func=_sync_schedule_background,
        trigger=DateTrigger(run_date=datetime.now(pytz.utc) + timedelta(seconds=5)),
        args=[application],
        id="sync_schedule_startup",
        replace_existing=True
    )

    # Arm the live-goal poller immediately — it will stop itself
    # if there are no live matches and re-arm when a match goes live.
    _arm_live_poller(application)
    
    return _scheduler
