"""Entry point: registers commands and starts the bot."""
import logging
import asyncio
from datetime import datetime, timedelta
import pytz
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import TelegramError

import config
import state
import football_api
from football_api import RateLimitException
import scheduler
from notifier import send_text, _escape, _format_stage, _format_group

RATE_LIMIT_MSG = "⏳ The football data service is temporarily rate\\-limited\\. Please try again in a minute or two\\."

# Set up logging
logging.basicConfig(
    format='[%(asctime)s] %(levelname)s — %(name)s — %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Reduce httpx/telegram logging
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

# Record start time
BOT_START_TIME = datetime.now()

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error."""
    if type(context.error).__name__ == "PTBUserDataError":
        return
    logger.error("Exception while handling an update:", exc_info=context.error)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    fav_teams = state.get_favourite_teams(chat_id)
    fav_names = ", ".join([t["name"] for t in fav_teams])
    
    welcome_msg = (
        "👋 Welcome to the *FIFA World Cup Bot*\\!\n\n"
        "I'll keep you updated with pre\\-match reminders, live scores, and full\\-time results\\.\n\n"
        "Your favourite teams: " + (f"*{_escape(fav_names)}*" if fav_names else "none set\\. Please use /addteam to choose one\\.") + "\n\n"
        "Use /help to see all available commands\\."
    )
    await send_text(context.application, chat_id, welcome_msg)

async def addteam_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    query = " ".join(context.args)
    if not query:
        await send_text(context.application, chat_id, "Usage: /addteam <team name\\>\nExample: /addteam Brazil")
        return
        
    try:
        team = football_api.search_team(query)
        if not team:
            msg = f"❌ No team found matching '{_escape(query)}' in the World Cup roster\\. Try a different spelling, e\\.g\\. /addteam Brazil"
            await send_text(context.application, chat_id, msg)
            return
            
        added = state.add_favourite_team(chat_id, team)
        if not added:
            await send_text(context.application, chat_id, f"ℹ️ *{_escape(team['name'])}* is already in your favourites\\.")
            return
            
        await send_text(context.application, chat_id, f"✅ Added *{_escape(team['name'])}* to your favourite teams\\. Syncing schedule…")
        
        # Run sync_schedule in executor
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, scheduler.sync_schedule, context.application)
        
        # Follow-up
        matches = football_api.get_team_matches(team["id"])
        upcoming = [m for m in matches if m.get("status") in ("SCHEDULED", "TIMED")]
        
        await send_text(context.application, chat_id, f"📅 Schedule updated\\. Found {_escape(str(len(upcoming)))} upcoming matches for {_escape(team['name'])}\\.")
        
    except RateLimitException:
        await send_text(context.application, chat_id, RATE_LIMIT_MSG)
    except ValueError:
        await send_text(context.application, chat_id, "❌ Multiple teams matched\\. Please be more specific\\.")
    except Exception as e:
        logger.error(f"Error in addteam: {e}")
        await send_text(context.application, chat_id, "❌ An error occurred while adding the team\\.")

async def removeteam_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    query = " ".join(context.args).lower()
    if not query:
        await send_text(context.application, chat_id, "Usage: /removeteam <team name\\>\nExample: /removeteam Brazil")
        return
        
    fav_teams = state.get_favourite_teams(chat_id)
    matched_team = None
    
    # Try exact match first
    for t in fav_teams:
        if t["name"].lower() == query or (t.get("shortName") and t["shortName"].lower() == query):
            matched_team = t
            break
            
    # Try partial match if no exact match
    if not matched_team:
        for t in fav_teams:
            if query in t["name"].lower():
                matched_team = t
                break
                
    if not matched_team:
        await send_text(context.application, chat_id, f"❌ '{_escape(query)}' is not in your favourite teams\\.")
        return
        
    state.remove_favourite_team(chat_id, matched_team["id"])
    await send_text(context.application, chat_id, f"🗑 Removed *{_escape(matched_team['name'])}* from your favourite teams\\.")
    
    # Sync schedule to remove reminders
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, scheduler.sync_schedule, context.application)

async def teams_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    fav_teams = state.get_favourite_teams(chat_id)
    if fav_teams:
        names = "\n• ".join([_escape(t["name"]) for t in fav_teams])
        await send_text(context.application, chat_id, f"👕 *Favourite teams:*\n• {names}")
    else:
        await send_text(context.application, chat_id, "No favourite teams set\\. Use /addteam to choose one\\.")

async def _toggle_setting(application, chat_id, args, setting_key, label_on, label_off, current_label):
    if not args:
        current = state.get_setting(chat_id, setting_key)
        status = "On" if current != "false" else "Off"
        await send_text(application, chat_id, f"{current_label}: *{status}*")
        return
        
    arg = args[0].lower()
    if arg == "on":
        state.set_setting(chat_id, setting_key, "true")
        await send_text(application, chat_id, f"{current_label}: *On*")
    elif arg == "off":
        state.set_setting(chat_id, setting_key, "false")
        await send_text(application, chat_id, f"{current_label}: *Off*")
    else:
        await send_text(application, chat_id, f"Usage: /{label_on} \\[on\\|off\\]")

async def reminders_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    await _toggle_setting(context.application, chat_id, context.args, "reminders_enabled", "reminders", "reminders", "🔔 Pre\\-match reminders")

async def myscores_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    await _toggle_setting(context.application, chat_id, context.args, "my_scores_enabled", "myscores", "myscores", "⭐ My team result notifications")

async def allscores_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    await _toggle_setting(context.application, chat_id, context.args, "all_scores_enabled", "allscores", "allscores", "🌍 All match result notifications")

async def livegoals_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    await _toggle_setting(context.application, chat_id, context.args, "live_goals_enabled", "livegoals", "livegoals", "⚽ Live goal notifications")

async def setreminder_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if not context.args:
        await send_text(context.application, chat_id, "Usage: /setreminder <minutes\\>\nExample: /setreminder 60")
        return
        
    try:
        minutes = int(context.args[0])
        if not (1 <= minutes <= 1440):
            raise ValueError()
            
        state.set_setting(chat_id, "reminder_minutes_before", str(minutes))
        
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, scheduler.sync_schedule, context.application)
        
        await send_text(context.application, chat_id, f"⏰ Reminder set to *{_escape(str(minutes))} minutes* before kickoff\\.")
    except ValueError:
        await send_text(context.application, chat_id, "❌ Please provide a valid integer between 1 and 1440\\.")

async def settimezone_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if not context.args:
        await send_text(context.application, chat_id, "Usage: /settimezone <tz\\>\nExample: /settimezone Asia/Dhaka")
        return
        
    tz_str = " ".join(context.args)
    try:
        pytz.timezone(tz_str)
        state.set_setting(chat_id, "timezone", tz_str)
        await send_text(context.application, chat_id, f"🌍 Timezone set to *{_escape(tz_str)}*\\.")
    except pytz.exceptions.UnknownTimeZoneError:
        await send_text(context.application, chat_id, "❌ Unknown timezone\\. Use IANA format, e\\.g\\. Asia/Dhaka")

def _get_user_tz(chat_id):
    tz_str = state.get_setting(chat_id, "timezone") or "UTC"
    try:
        return pytz.timezone(tz_str), tz_str
    except pytz.exceptions.UnknownTimeZoneError:
        return pytz.UTC, "UTC"

async def nextmatch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    fav_teams = state.get_favourite_teams(chat_id)
    if not fav_teams:
        await send_text(context.application, chat_id, "No favourite teams set\\. Use /addteam to choose one\\.")
        return
        
    try:
        now_utc = datetime.now(pytz.utc)
        all_upcoming = []
        for t in fav_teams:
            matches = football_api.get_team_matches(t["id"])
            for m in matches:
                if m.get("status") not in ("SCHEDULED", "TIMED"):
                    continue
                match_dt = datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00"))
                if match_dt > now_utc:
                    all_upcoming.append(m)
    except RateLimitException:
        await send_text(context.application, chat_id, RATE_LIMIT_MSG)
        return
        
    if not all_upcoming:
        await send_text(context.application, chat_id, "No upcoming matches found for your favourite teams\\.")
        return
        
    # Deduplicate by match ID
    unique_upcoming = {m["id"]: m for m in all_upcoming}.values()
    sorted_upcoming = sorted(unique_upcoming, key=lambda x: x.get("utcDate", ""))
    
    match = sorted_upcoming[0]
    user_tz, tz_str = _get_user_tz(chat_id)
    match_dt = datetime.fromisoformat(match["utcDate"].replace("Z", "+00:00")).astimezone(user_tz)
    
    stage = _format_stage(match.get("stage", ""))
    group = _format_group(match.get("group"))
    stage_group = _escape(f"{stage} · {group}" if group else stage)
    
    home_esc = _escape(match.get("homeTeam.name", "TBD"))
    away_esc = _escape(match.get("awayTeam.name", "TBD"))
    time_esc = _escape(match_dt.strftime('%I:%M %p'))
    tz_esc = _escape(tz_str)
    date_esc = _escape(match_dt.strftime('%A, %d %B %Y'))
    
    msg = (
        "🔜 *Next Match*\n\n"
        f"🏆 FIFA World Cup · {stage_group}\n"
        f"🆚 *{home_esc}* vs *{away_esc}*\n"
        f"🕐 *{time_esc}* \\({tz_esc}\\)\n"
        f"📅 *{date_esc}*"
    )
    await send_text(context.application, chat_id, msg)

async def matches_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    fav_teams = state.get_favourite_teams(chat_id)
    if not fav_teams:
        await send_text(context.application, chat_id, "No favourite teams set\\. Use /addteam to choose one\\.")
        return
        
    try:
        now_utc = datetime.now(pytz.utc)
        all_upcoming = []
        for t in fav_teams:
            matches = football_api.get_team_matches(t["id"])
            for m in matches:
                if m.get("status") not in ("SCHEDULED", "TIMED"):
                    continue
                match_dt = datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00"))
                if match_dt > now_utc:
                    all_upcoming.append(m)
    except RateLimitException:
        await send_text(context.application, chat_id, RATE_LIMIT_MSG)
        return
        
    if not all_upcoming:
        await send_text(context.application, chat_id, "No upcoming matches found for your favourite teams\\.")
        return
        
    # Deduplicate by match ID
    unique_upcoming = {m["id"]: m for m in all_upcoming}.values()
    sorted_upcoming = sorted(unique_upcoming, key=lambda x: x.get("utcDate", ""))
    
    user_tz, tz_str = _get_user_tz(chat_id)
    tz_esc = _escape(tz_str)
    
    fav_names = ", ".join([t["name"] for t in fav_teams])
    msg = f"📋 *Upcoming matches — {_escape(fav_names)}*\n\n"
    
    for i, match in enumerate(sorted_upcoming, 1):
        match_dt = datetime.fromisoformat(match["utcDate"].replace("Z", "+00:00")).astimezone(user_tz)
        home_esc = _escape(match.get("homeTeam.name", "TBD"))
        away_esc = _escape(match.get("awayTeam.name", "TBD"))
        date_esc = _escape(match_dt.strftime('%a %d %b'))
        time_esc = _escape(match_dt.strftime('%I:%M %p'))
        stage = _format_stage(match.get("stage", ""))
        group = _format_group(match.get("group"))
        stage_group = _escape(f"{stage} · {group}" if group else stage)
        
        msg += f"*{i}\\.* *{home_esc}* 🆚 *{away_esc}*\n"
        msg += f"   📅 {date_esc} · ⏰ {time_esc} \\({tz_esc}\\)\n"
        msg += f"   🏆 {stage_group}\n\n"
        
    await send_text(context.application, chat_id, msg)

async def results_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    user_tz, tz_str = _get_user_tz(chat_id)
    now_local = datetime.now(user_tz)

    if context.args:
        arg = context.args[0].lower()
        if arg == "today":
            target_date = now_local.date()
        elif arg == "yesterday":
            target_date = (now_local - timedelta(days=1)).date()
        else:
            try:
                target_date = datetime.strptime(arg, "%Y-%m-%d").date()
            except ValueError:
                await send_text(context.application, chat_id, "Usage: /results \\[today \\| yesterday \\| yyyy\\-MM\\-dd\\]")
                return
    else:
        target_date = now_local.date()
        
    try:
        all_matches = football_api.get_all_wc_matches()
    except RateLimitException:
        await send_text(context.application, chat_id, RATE_LIMIT_MSG)
        return

    target_date_str = target_date.strftime("%Y-%m-%d")
    
    finished_matches = []
    day_had_matches = False
    
    for m in all_matches:
        utc_date_str = m.get("utcDate", "")
        if not utc_date_str:
            continue
        match_dt = datetime.fromisoformat(utc_date_str.replace("Z", "+00:00")).astimezone(user_tz)
        if match_dt.date() == target_date:
            day_had_matches = True
            if m.get("status") in ("FINISHED", "AWARDED"):
                finished_matches.append(m)
                
    if not day_had_matches:
        await send_text(context.application, chat_id, f"No World Cup matches on {_escape(target_date_str)}\\.")
        return
        
    if not finished_matches:
        await send_text(context.application, chat_id, f"No finished matches on {_escape(target_date_str)}\\.")
        return
        
    finished_matches.sort(key=lambda x: x.get("utcDate", ""))
    
    lines = []
    for m in finished_matches:
        home_esc = _escape(m.get("homeTeam.name", "TBD"))
        away_esc = _escape(m.get("awayTeam.name", "TBD"))
        h_score = _escape(str(m.get("score.fullTime.home") or 0))
        a_score = _escape(str(m.get("score.fullTime.away") or 0))
        stage = _format_stage(m.get("stage", ""))
        group = _format_group(m.get("group"))
        stage_group = _escape(f"{stage} · {group}" if group else stage)
        
        lines.append(f"✅ *{home_esc}* {h_score} – {a_score} *{away_esc}* _{stage_group}_")
        
    await send_text(context.application, chat_id, "\n".join(lines))

async def live_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    try:
        all_matches = football_api.get_all_wc_matches()
    except RateLimitException:
        await send_text(context.application, chat_id, RATE_LIMIT_MSG)
        return

    now_utc = datetime.now(pytz.utc)

    # Matches the API explicitly marks as live
    api_live_statuses = ("IN_PLAY", "PAUSED", "EXTRA_TIME", "PENALTY_SHOOTOUT")
    # Statuses that mean the match is definitely over
    finished_statuses = ("FINISHED", "AWARDED", "CANCELLED", "POSTPONED")

    confirmed_live = []
    probable_live = []

    for m in all_matches:
        status = m.get("status")

        if status in api_live_statuses:
            confirmed_live.append(m)
        elif status not in finished_statuses:
            # The free API tier often keeps status as TIMED/SCHEDULED even
            # after kickoff.  Treat any match whose kickoff was 0-130 mins
            # ago (and hasn't been marked finished) as probably live.
            utc_date_str = m.get("utcDate")
            if utc_date_str:
                match_dt = datetime.fromisoformat(utc_date_str.replace("Z", "+00:00"))
                elapsed = (now_utc - match_dt).total_seconds()
                if 0 <= elapsed <= 130 * 60:
                    probable_live.append(m)

    if not confirmed_live and not probable_live:
        await send_text(context.application, chat_id, "No matches currently in progress\\.")
        return

    lines = []
    for m in confirmed_live:
        home_esc = _escape(m.get("homeTeam.name", "TBD"))
        away_esc = _escape(m.get("awayTeam.name", "TBD"))
        h_score = _escape(str(m.get("score.fullTime.home") or 0))
        a_score = _escape(str(m.get("score.fullTime.away") or 0))
        stage = _format_stage(m.get("stage", ""))
        group = _format_group(m.get("group"))
        stage_group = _escape(f"{stage} · {group}" if group else stage)
        lines.append(f"🔴 LIVE — *{home_esc}* {h_score} – {a_score} *{away_esc}* · _{stage_group}_")

    for m in probable_live:
        home_esc = _escape(m.get("homeTeam.name", "TBD"))
        away_esc = _escape(m.get("awayTeam.name", "TBD"))
        h_score = m.get("score.fullTime.home")
        a_score = m.get("score.fullTime.away")
        stage = _format_stage(m.get("stage", ""))
        group = _format_group(m.get("group"))
        stage_group = _escape(f"{stage} · {group}" if group else stage)

        match_dt = datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00"))
        elapsed_mins = int((now_utc - match_dt).total_seconds() // 60)
        elapsed_esc = _escape(f"~{elapsed_mins}'")

        if h_score is not None and a_score is not None:
            score_str = f"{_escape(str(h_score))} – {_escape(str(a_score))}"
        else:
            score_str = "vs"

        lines.append(f"🟡 IN PROGRESS \\({elapsed_esc}\\) — *{home_esc}* {score_str} *{away_esc}* · _{stage_group}_")

    await send_text(context.application, chat_id, "\n\n".join(lines))

async def fixture_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends the full tournament fixture grouped by stage and group."""
    chat_id = update.effective_chat.id
    user_tz, tz_str = _get_user_tz(chat_id)
    now_utc = datetime.now(pytz.utc)

    try:
        all_matches = football_api.get_all_wc_matches()
    except RateLimitException:
        await send_text(context.application, chat_id, RATE_LIMIT_MSG)
        return

    if not all_matches:
        await send_text(context.application, chat_id, "No fixture data available yet\\.")
        return

    # Sort matches chronologically
    all_matches.sort(key=lambda m: m.get("utcDate", ""))

    # Stage display order
    STAGE_ORDER = [
        "GROUP_STAGE", "ROUND_OF_16", "QUARTER_FINALS",
        "SEMI_FINALS", "THIRD_PLACE", "FINAL"
    ]
    LIVE_STATUSES = ("IN_PLAY", "PAUSED", "EXTRA_TIME", "PENALTY_SHOOTOUT")
    FINISHED_STATUSES = ("FINISHED", "AWARDED", "CANCELLED")

    # Group matches: {stage -> {group_label -> [matches]}}
    from collections import defaultdict
    grouped: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    stages_seen = []

    for m in all_matches:
        stage = m.get("stage") or "UNKNOWN"
        group = m.get("group")  # None for knockout stages
        group_label = _format_group(group) if group else ""

        if stage not in stages_seen:
            stages_seen.append(stage)
        grouped[stage][group_label].append(m)

    # Build message chunks (split at ~4000 chars to stay under Telegram 4096 limit)
    MAX_CHUNK = 4000
    chunks: list[str] = []
    current = "\U0001f4cb *FIFA World Cup 2026 — Full Fixture*\n"
    current += f"_Times shown in {_escape(tz_str)}_\n"

    # Iterate stages in defined order, then any extras
    ordered_stages = [s for s in STAGE_ORDER if s in grouped]
    ordered_stages += [s for s in stages_seen if s not in ordered_stages]

    for stage in ordered_stages:
        stage_label = _escape(_format_stage(stage))
        groups_in_stage = grouped[stage]

        # Sort groups alphabetically (empty string = knockout, comes first in that context)
        sorted_groups = sorted(groups_in_stage.keys())

        for group_label in sorted_groups:
            matches_in_group = groups_in_stage[group_label]

            # Section header
            if group_label:
                header = f"\n\n\u2501\u2501 *{stage_label} \u00b7 {_escape(group_label)}* \u2501\u2501"
            else:
                header = f"\n\n\u2501\u2501 *{stage_label}* \u2501\u2501"

            if len(current) + len(header) > MAX_CHUNK:
                chunks.append(current)
                current = header + "\n"
            else:
                current += header + "\n"

            for m in matches_in_group:
                status = m.get("status", "")
                home = m.get("homeTeam.name") or "TBD"
                away = m.get("awayTeam.name") or "TBD"
                home_esc = _escape(home)
                away_esc = _escape(away)

                utc_date_str = m.get("utcDate", "")
                if utc_date_str:
                    match_dt = datetime.fromisoformat(utc_date_str.replace("Z", "+00:00")).astimezone(user_tz)
                    date_str = _escape(match_dt.strftime("%a %d %b"))
                    time_str = _escape(match_dt.strftime("%H:%M"))
                else:
                    date_str = _escape("TBD")
                    time_str = ""

                h_score = m.get("score.fullTime.home")
                a_score = m.get("score.fullTime.away")

                if status in FINISHED_STATUSES:
                    icon = "\u2705"  # ✅
                    if h_score is not None and a_score is not None:
                        score_part = f"*{home_esc}* {_escape(str(h_score))} \u2013 {_escape(str(a_score))} *{away_esc}*"
                    else:
                        score_part = f"*{home_esc}* vs *{away_esc}*"
                elif status in LIVE_STATUSES:
                    icon = "\U0001f534"  # 🔴
                    if h_score is not None and a_score is not None:
                        score_part = f"*{home_esc}* {_escape(str(h_score))} \u2013 {_escape(str(a_score))} *{away_esc}*"
                    else:
                        score_part = f"*{home_esc}* vs *{away_esc}*"
                elif utc_date_str and status in ("TIMED", "SCHEDULED"):
                    m_dt_utc = datetime.fromisoformat(utc_date_str.replace("Z", "+00:00"))
                    elapsed = (now_utc - m_dt_utc).total_seconds()
                    if 0 < elapsed <= 130 * 60:  # Probably live (free-tier lag)
                        icon = "\U0001f7e1"  # 🟡
                        elapsed_mins = int(elapsed // 60)
                        if h_score is not None and a_score is not None:
                            score_part = (
                                f"*{home_esc}* {_escape(str(h_score))} \u2013 {_escape(str(a_score))} *{away_esc}*"
                                f" _\\(\u007e{_escape(str(elapsed_mins))}'\\)_"
                            )
                        else:
                            score_part = f"*{home_esc}* vs *{away_esc}*"
                    else:
                        icon = "\U0001f551"  # 🕑
                        score_part = f"*{home_esc}* vs *{away_esc}*"
                else:
                    icon = "\U0001f551"  # 🕑
                    score_part = f"*{home_esc}* vs *{away_esc}*"

                if time_str:
                    line = f"{icon} `{date_str}` {time_str} \u2014 {score_part}\n"
                else:
                    line = f"{icon} {score_part}\n"

                if len(current) + len(line) > MAX_CHUNK:
                    chunks.append(current)
                    current = line
                else:
                    current += line

    if current.strip():
        chunks.append(current)

    for chunk in chunks:
        await send_text(context.application, chat_id, chunk)

async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    user_tz, tz_str = _get_user_tz(chat_id)

    try:
        all_matches = football_api.get_all_wc_matches()
    except RateLimitException:
        await send_text(context.application, chat_id, RATE_LIMIT_MSG)
        return

    now_utc = datetime.now(pytz.utc)
    target_date = datetime.now(user_tz).date()
    target_date_str = target_date.strftime("%Y-%m-%d")
    
    today_matches = []
    for m in all_matches:
        utc_date_str = m.get("utcDate", "")
        if not utc_date_str:
            continue
        match_dt = datetime.fromisoformat(utc_date_str.replace("Z", "+00:00")).astimezone(user_tz)
        if match_dt.date() == target_date:
            today_matches.append(m)
    
    if not today_matches:
        await send_text(context.application, chat_id, "No World Cup matches today\\.")
        return
        
    today_matches.sort(key=lambda x: x.get("utcDate", ""))
    
    upcoming = []
    live = []
    finished = []
    
    for m in today_matches:
        status = m.get("status")
        home_esc = _escape(m.get("homeTeam.name", "TBD"))
        away_esc = _escape(m.get("awayTeam.name", "TBD"))
        stage = _format_stage(m.get("stage", ""))
        group = _format_group(m.get("group"))
        stage_group = _escape(f"{stage} · {group}" if group else stage)
        match_dt_utc = datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00"))
        
        if status in ("IN_PLAY", "PAUSED", "EXTRA_TIME", "PENALTY_SHOOTOUT"):
            h_score = _escape(str(m.get("score.fullTime.home") or 0))
            a_score = _escape(str(m.get("score.fullTime.away") or 0))
            live.append(f"• {home_esc} {h_score} – {a_score} {away_esc} · {stage_group}")
        elif status in ("FINISHED", "AWARDED"):
            h_score = _escape(str(m.get("score.fullTime.home") or 0))
            a_score = _escape(str(m.get("score.fullTime.away") or 0))
            finished.append(f"• {home_esc} {h_score} – {a_score} {away_esc} · {stage_group}")
        elif status in ("SCHEDULED", "TIMED") and match_dt_utc > now_utc:
            # Only show as upcoming if kickoff hasn't passed yet
            match_dt_local = match_dt_utc.astimezone(user_tz)
            time_esc = _escape(match_dt_local.strftime('%I:%M %p'))
            tz_esc = _escape(tz_str)
            upcoming.append(f"• {home_esc} vs {away_esc} — {time_esc} \\({tz_esc}\\) · {stage_group}")
        elif status in ("SCHEDULED", "TIMED") and match_dt_utc <= now_utc:
            # Kickoff time has passed but API hasn't updated status yet
            elapsed_mins = int((now_utc - match_dt_utc).total_seconds() // 60)
            if elapsed_mins <= 130:
                elapsed_esc = _escape(f"~{elapsed_mins}'")
                h_score = m.get("score.fullTime.home")
                a_score = m.get("score.fullTime.away")
                if h_score is not None and a_score is not None:
                    score_str = f"{_escape(str(h_score))} – {_escape(str(a_score))}"
                    live.append(f"• {home_esc} {score_str} {away_esc} \\({elapsed_esc}\\) · {stage_group}")
                else:
                    live.append(f"• {home_esc} vs {away_esc} \\({elapsed_esc}\\) · {stage_group}")
            
    parts = [f"📅 *World Cup Matches Today — {_escape(target_date.strftime('%d %B %Y'))}*"]
    if live:
        parts.append("🔴 LIVE NOW\n" + "\n".join(live))
    if upcoming:
        parts.append("🟢 UPCOMING\n" + "\n".join(upcoming))
    if finished:
        parts.append("✅ FINISHED\n" + "\n".join(finished))
        
    await send_text(context.application, chat_id, "\n\n".join(parts))

async def tomorrow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    user_tz, tz_str = _get_user_tz(chat_id)

    try:
        all_matches = football_api.get_all_wc_matches()
    except RateLimitException:
        await send_text(context.application, chat_id, RATE_LIMIT_MSG)
        return

    target_date = (datetime.now(user_tz) + timedelta(days=1)).date()

    tomorrow_matches = []
    for m in all_matches:
        utc_date_str = m.get("utcDate", "")
        if not utc_date_str:
            continue
        match_dt = datetime.fromisoformat(utc_date_str.replace("Z", "+00:00")).astimezone(user_tz)
        if match_dt.date() == target_date:
            tomorrow_matches.append(m)

    if not tomorrow_matches:
        await send_text(context.application, chat_id, "No World Cup matches tomorrow\\.")
        return

    tomorrow_matches.sort(key=lambda x: x.get("utcDate", ""))

    upcoming = []
    finished = []  # Edge case: late-night match that tips into "tomorrow" for some timezones

    tz_esc = _escape(tz_str)
    for m in tomorrow_matches:
        status = m.get("status")
        home_esc = _escape(m.get("homeTeam.name", "TBD"))
        away_esc = _escape(m.get("awayTeam.name", "TBD"))
        stage = _format_stage(m.get("stage", ""))
        group = _format_group(m.get("group"))
        stage_group = _escape(f"{stage} \u00b7 {group}" if group else stage)
        match_dt_local = datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00")).astimezone(user_tz)
        time_esc = _escape(match_dt_local.strftime('%I:%M %p'))

        if status in ("FINISHED", "AWARDED"):
            h_score = _escape(str(m.get("score.fullTime.home") or 0))
            a_score = _escape(str(m.get("score.fullTime.away") or 0))
            finished.append(f"\u2022 {home_esc} {h_score} \u2013 {a_score} {away_esc} \u00b7 {stage_group}")
        else:
            upcoming.append(f"\u2022 {home_esc} vs {away_esc} \u2014 {time_esc} \\({tz_esc}\\) \u00b7 {stage_group}")

    parts = [f"\U0001f4c5 *World Cup Matches Tomorrow \u2014 {_escape(target_date.strftime('%d %B %Y'))}*"]
    if upcoming:
        parts.append("\U0001f7e2 UPCOMING\n" + "\n".join(upcoming))
    if finished:
        parts.append("\u2705 FINISHED\n" + "\n".join(finished))

    await send_text(context.application, chat_id, "\n\n".join(parts))

async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    fav_teams = state.get_favourite_teams(chat_id)
    fav_names = ", ".join([t["name"] for t in fav_teams]) or "None"
    
    reminder_mins = state.get_setting(chat_id, "reminder_minutes_before") or "60"
    reminders_on = "On" if state.get_setting(chat_id, "reminders_enabled") != "false" else "Off"
    myscores_on = "On" if state.get_setting(chat_id, "my_scores_enabled") != "false" else "Off"
    allscores_on = "On" if state.get_setting(chat_id, "all_scores_enabled") != "false" else "Off"
    livegoals_on = "On" if state.get_setting(chat_id, "live_goals_enabled") != "false" else "Off"
    tz_str = state.get_setting(chat_id, "timezone") or "UTC"
    
    msg = (
        "⚙️ *Settings*\n\n"
        f"👕 Favourite teams: *{_escape(fav_names)}*\n"
        f"⏰ Reminder: *{_escape(reminder_mins)} minutes before kickoff*\n"
        f"🔔 Pre\\-match reminders: *{reminders_on}*\n"
        f"⭐ My team results: *{myscores_on}*\n"
        f"🌍 All match results: *{allscores_on}*\n"
        f"⚽ Live goal alerts: *{livegoals_on}*\n"
        f"🕐 Timezone: *{_escape(tz_str)}*"
    )
    await send_text(context.application, chat_id, msg)

async def syncnow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    await send_text(context.application, chat_id, "🔄 Syncing…")
    
    try:
        # Run sync in background so we don't block
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, scheduler.sync_schedule, context.application)
    except RateLimitException:
        await send_text(context.application, chat_id, RATE_LIMIT_MSG)
        return
    
    # Get stats
    jobs = scheduler._scheduler.get_jobs()
    reminders = sum(1 for j in jobs if j.id.startswith("remind_"))
    pollers = sum(1 for j in jobs if j.id.startswith("poll_"))
    
    await send_text(context.application, chat_id, f"✅ Done\\. Reminders: {_escape(str(reminders))}\\. Result pollers: {_escape(str(pollers))}\\.")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    now = datetime.now()
    uptime = now - BOT_START_TIME
    hours, remainder = divmod(int(uptime.total_seconds()), 3600)
    minutes, _ = divmod(remainder, 60)
    uptime_str = f"{hours}h {minutes}m"
    
    rem_quota = football_api._requests_remaining
    
    jobs = scheduler._scheduler.get_jobs()
    active_jobs = len(jobs)
    live_poller_active = scheduler._scheduler.get_job("live_poller") is not None
    
    next_job = None
    next_time = None
    if jobs:
        jobs.sort(key=lambda j: j.next_run_time if j.next_run_time else pytz.utc.localize(datetime.max))
        valid_jobs = [j for j in jobs if j.next_run_time]
        if valid_jobs:
            first = valid_jobs[0]
            next_job = first.id
            user_tz, tz_str = _get_user_tz(chat_id)
            next_time = first.next_run_time.astimezone(user_tz).strftime('%I:%M:%S %p')
            
    live_status_str = "Running \\(10s\\)" if live_poller_active else "Idle"
    live_icon = "🟢" if live_poller_active else "⚫"
    msg = (
        "🤖 *Bot Status*\n\n"
        f"⏱ Uptime: *{_escape(uptime_str)}*\n"
        f"📡 API quota remaining: *{_escape(str(rem_quota))} / 10*\n"
        f"📅 Active scheduler jobs: *{_escape(str(active_jobs))}*\n"
        f"⚽ Live goal poller: {live_icon} *{live_status_str}*\n"
    )
    if next_job and next_time:
        msg += f"🔜 Next job: *{_escape(next_job)}* at *{_escape(next_time)}*"
        
    await send_text(context.application, chat_id, msg)


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    state.reset_settings(chat_id)
    
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, scheduler.sync_schedule, context.application)
    except football_api.RateLimitException:
        pass
        
    await send_text(context.application, chat_id, "✅ All settings have been reset to their defaults\\.")

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    users = state.get_all_users()
    total_users = len(users)
    msg = f"📊 *Bot Statistics*\n\nTotal unique users tracking teams: *{total_users}*"
    await send_text(context.application, chat_id, msg)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    msg = (
        "📖 *Command Reference*\n\n"
        "*Configuration*\n"
        "• /addteam <team\\> — Add a team to your favourites\n"
        "• /removeteam <team\\> — Remove a team from your favourites\n"
        "• /teams — View your favourite teams\n"
        "• /settimezone <tz\\> — Set your local timezone\n"
        "• /settings — View all settings\n"
        "• /reset — Reset all settings to defaults\n\n"
        "*Notifications*\n"
        "• /reminders \\[on\\|off\\] — Toggle pre\\-match reminders\n"
        "• /setreminder <minutes\\> — Set minutes before kickoff to remind\n"
        "• /myscores \\[on\\|off\\] — Toggle results for your teams\n"
        "• /allscores \\[on\\|off\\] — Toggle results for all matches\n"
        "• /livegoals \\[on\\|off\\] — Toggle live in\\-match goal alerts\n\n"
        "*Match Info*\n"
        "• /today — Matches happening today\n"
        "• /tomorrow — Matches scheduled for tomorrow\n"
        "• /live — Matches currently in progress\n"
        "• /fixture — Full tournament fixture\n"
        "• /nextmatch — Your teams' next match\n"
        "• /matches — Your teams' full schedule\n"
        "• /results \\[date\\] — Finished matches \\(today, yesterday, or YYYY\\-MM\\-DD\\)\n\n"
        "*System*\n"
        "• /syncnow — Manually force a schedule sync\n"
        "• /status — View bot diagnostics\n"
        "• /stats — Track bot usage and total users\n"
        "• /help — Show this message"
    )
    await send_text(context.application, chat_id, msg)

async def fallback_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Replies to any unrecognised text or unknown command with a helpful nudge."""
    chat_id = update.effective_chat.id
    user_text = (update.message.text or "").strip()

    # Show the first 30 chars of what they typed so the reply feels personal
    preview = user_text[:30] + ("…" if len(user_text) > 30 else "")
    preview_esc = _escape(preview)

    msg = (
        f"🤔 I don't understand *{preview_esc}*\\.\n\n"
        "I only respond to commands\\. Use /help to see everything I can do\\."
    )
    await send_text(context.application, chat_id, msg)

def main():
    state._init_db()
    
    application = ApplicationBuilder().token(config.TELEGRAM_BOT_TOKEN).build()
    
    application.add_error_handler(error_handler)
    
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("addteam", addteam_cmd))
    application.add_handler(CommandHandler("removeteam", removeteam_cmd))
    application.add_handler(CommandHandler("teams", teams_cmd))
    application.add_handler(CommandHandler("reminders", reminders_cmd))
    application.add_handler(CommandHandler("myscores", myscores_cmd))
    application.add_handler(CommandHandler("allscores", allscores_cmd))
    application.add_handler(CommandHandler("livegoals", livegoals_cmd))
    application.add_handler(CommandHandler("setreminder", setreminder_cmd))
    application.add_handler(CommandHandler("settimezone", settimezone_cmd))
    application.add_handler(CommandHandler("nextmatch", nextmatch_cmd))
    application.add_handler(CommandHandler("matches", matches_cmd))
    application.add_handler(CommandHandler("results", results_cmd))
    application.add_handler(CommandHandler("live", live_cmd))
    application.add_handler(CommandHandler("fixture", fixture_cmd))
    application.add_handler(CommandHandler("today", today_cmd))
    application.add_handler(CommandHandler("tomorrow", tomorrow_cmd))
    application.add_handler(CommandHandler("settings", settings_cmd))
    application.add_handler(CommandHandler("reset", reset_cmd))
    application.add_handler(CommandHandler("syncnow", syncnow_cmd))
    application.add_handler(CommandHandler("status", status_cmd))
    application.add_handler(CommandHandler("stats", stats_cmd))
    application.add_handler(CommandHandler("help", help_cmd))

    # Catch-all: must be registered LAST so all command handlers take priority
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_cmd))
    # Also catch unknown /commands not handled above
    application.add_handler(MessageHandler(filters.COMMAND, fallback_cmd))
    
    sched = scheduler.start_scheduler(application)
    application.bot_data["scheduler"] = sched
    
    logger.info("Bot started.")
    
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    finally:
        sched.shutdown(wait=False)
        logger.info("Bot stopped.")

if __name__ == "__main__":
    main()
