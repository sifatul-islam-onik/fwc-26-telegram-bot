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

logger = logging.getLogger(__name__)

# Module-level variable
_scheduler: BackgroundScheduler = None

def _run_async(coro):
    """Helper to run async functions from APScheduler's synchronous workers."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        asyncio.run_coroutine_threadsafe(coro, loop)
    else:
        asyncio.run(coro)

def _pretty_stage(stage: str) -> str:
    """Converts API enum strings to readable labels."""
    if not stage:
        return ""
    mapping = {
        "GROUP_STAGE": "Group Stage",
        "ROUND_OF_16": "Round of 16",
        "QUARTER_FINALS": "Quarter Finals",
        "SEMI_FINALS": "Semi Finals",
        "THIRD_PLACE": "Third Place",
        "FINAL": "Final"
    }
    return mapping.get(stage, stage.replace("_", " ").title())

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
    
    # PART A — Pre-match reminders (favourite teams only)
    fav_teams = state.get_favourite_teams()
    if not fav_teams:
        logger.warning("No favourite teams found in state. Skipping reminders sync.")
    else:
        minutes_before = int(state.get_setting("reminder_minutes_before") or 60)
        
        all_fav_matches = {}
        for team in fav_teams:
            matches = football_api.get_team_matches(team["id"])
            for match in matches:
                match_id = match.get("id")
                if match_id:
                    all_fav_matches[match_id] = match
                    
        for match_id, match in all_fav_matches.items():
            status = match.get("status")
            job_id = f"remind_{match_id}"
            
            if status == "POSTPONED":
                if _scheduler.get_job(job_id) and not state.is_notified(match_id, "reminder"):
                    _scheduler.remove_job(job_id)
                continue
                
            if status in ("SCHEDULED", "TIMED"):
                if state.is_notified(match_id, "reminder"):
                    continue
                    
                match_dt = datetime.fromisoformat(match["utcDate"].replace("Z", "+00:00"))
                reminder_dt = match_dt - timedelta(minutes=minutes_before)
                now_utc = datetime.now(pytz.utc)
                
                if reminder_dt <= now_utc:
                    continue
                    
                def make_reminder_func(app, m):
                    def wrapper():
                        _run_async(notifier.send_reminder(app, m))
                    return wrapper
                
                _scheduler.add_job(
                    func=make_reminder_func(application, match),
                    trigger=DateTrigger(run_date=reminder_dt),
                    id=job_id,
                    replace_existing=True
                )
                reminders_count += 1

    # PART B — Result pollers (ALL WC matches)
    all_matches = football_api.get_all_wc_matches()
    for match in all_matches:
        match_id = match.get("id")
        if not match_id or state.is_notified(match_id, "result"):
            continue
            
        status = match.get("status")
        utc_date_str = match.get("utcDate")
        if not utc_date_str: continue
        match_dt = datetime.fromisoformat(utc_date_str.replace("Z", "+00:00"))
        now_utc = datetime.now(pytz.utc)
        
        home_name = match.get("homeTeam.name", "TBD")
        away_name = match.get("awayTeam.name", "TBD")
        
        if status in ("FINISHED", "AWARDED", "CANCELLED"):
            _run_async(notifier.send_result(application, match))
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
        _run_async(notifier.send_result(application, match))
        state.mark_notified(match_id, 'result')
        
        poll_job_id = f"poll_{match_id}"
        startpoll_job_id = f"startpoll_{match_id}"
        if _scheduler.get_job(poll_job_id): _scheduler.remove_job(poll_job_id)
        if _scheduler.get_job(startpoll_job_id): _scheduler.remove_job(startpoll_job_id)
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
        trigger=IntervalTrigger(hours=2),
        args=[application],
        id="sync_schedule",
        replace_existing=True
    )
    
    _scheduler.add_job(
        func=_sync_schedule_background,
        trigger=DateTrigger(run_date=datetime.now() + timedelta(seconds=5)),
        args=[application],
        id="sync_schedule_startup",
        replace_existing=True
    )
    
    return _scheduler
