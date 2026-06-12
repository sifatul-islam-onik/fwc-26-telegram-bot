"""Entry point: registers commands and starts the bot."""
import logging
import asyncio
import json
from collections import defaultdict
from datetime import datetime, timedelta
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
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
        "Your favourite teams: " + (f"*{_escape(fav_names)}*" if fav_names else "none set\\. Use /menu to choose one\\.") + "\n\n"
        "Use /help to see how the bot works\\."
    )
    await send_text(context.application, chat_id, welcome_msg)

async def addteam_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    query = " ".join(context.args)
    if not query:
        await show_addteam_menu(update, context, page=0)
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
        await show_removeteam_menu(update, context)
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

async def _toggle_setting(application, chat_id, args, setting_key, label_on, label_off, current_label, update=None, context=None):
    if not args:
        if update and context:
            await show_toggle_menu(update, context, setting_key, current_label)
        else:
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
    await _toggle_setting(context.application, chat_id, context.args, "reminders_enabled", "reminders", "reminders", "🔔 Pre\\-match reminders", update=update, context=context)

async def myscores_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await _toggle_setting(context.application, chat_id, context.args, "my_scores_enabled", "myscores", "myscores", "⭐ My team result notifications", update=update, context=context)

async def allscores_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await _toggle_setting(context.application, chat_id, context.args, "all_scores_enabled", "allscores", "allscores", "🌍 All match result notifications", update=update, context=context)

async def livegoals_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await _toggle_setting(context.application, chat_id, context.args, "live_goals_enabled", "livegoals", "livegoals", "⚽ Live goal notifications", update=update, context=context)

async def setreminder_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if not context.args:
        await show_setreminder_menu(update, context)
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
        await show_timezone_regions_menu(update, context)
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

def _make_monospace_card(stage_group: str, home: str, away: str, time_str: str = None, date_str: str = None, status_str: str = None, score_str: str = None) -> str:
    # Truncate stage_group to 24 chars
    stage_lbl = stage_group[:24].center(26)
    
    if score_str:
        # e.g., "Brazil   2 - 1   Argentina"
        # Let's allocate 9 chars for home, 6 chars for score, 9 chars for away
        home_lbl = home[:9].rjust(9)
        away_lbl = away[:9].ljust(9)
        match_line = f"{home_lbl} {score_str.center(6)} {away_lbl}"
    else:
        # e.g., "Brazil vs Germany"
        match_line = f"{home} vs {away}"
        if len(match_line) > 26:
            match_line = f"{home[:11]} vs {away[:11]}"
        match_line = match_line.center(26)
        
    lines = [
        "+" + "-" * 26 + "+",
        f"|{stage_lbl}|",
        "+" + "-" * 26 + "+",
        f"|{match_line}|"
    ]
    
    if status_str:
        status_lbl = status_str[:26].center(26)
        lines.append(f"|{status_lbl}|")
        
    if time_str or date_str:
        dt_line = ""
        if date_str and time_str:
            dt_line = f"{date_str} · {time_str}"
        elif date_str:
            dt_line = date_str
        else:
            dt_line = time_str
        dt_lbl = dt_line[:26].center(26)
        lines.append(f"|{dt_lbl}|")
        
    lines.append("+" + "-" * 26 + "+")
    return "\n".join(lines)

async def _get_nextmatch_text(chat_id: int) -> str:
    fav_teams = state.get_favourite_teams(chat_id)
    if not fav_teams:
        return "No favourite teams set\\. Use Add Team to choose one\\."
        
    try:
        now_utc = datetime.now(pytz.utc)
        all_upcoming = []
        all_wc_matches = football_api.get_all_wc_matches()
        for t in fav_teams:
            matches = football_api.filter_team_matches(all_wc_matches, t["id"])
            for m in matches:
                if m.get("status") not in ("SCHEDULED", "TIMED"):
                    continue
                match_dt = datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00"))
                if match_dt > now_utc:
                    all_upcoming.append(m)
    except RateLimitException:
        return RATE_LIMIT_MSG
        
    if not all_upcoming:
        return "No upcoming matches found for your favourite teams\\."
        
    # Deduplicate by match ID
    unique_upcoming = {m["id"]: m for m in all_upcoming}.values()
    sorted_upcoming = sorted(unique_upcoming, key=lambda x: x.get("utcDate", ""))
    
    match = sorted_upcoming[0]
    user_tz, tz_str = _get_user_tz(chat_id)
    match_dt = datetime.fromisoformat(match["utcDate"].replace("Z", "+00:00")).astimezone(user_tz)
    
    stage = _format_stage(match.get("stage", ""))
    group = _format_group(match.get("group"))
    stage_group = f"{stage} · {group}" if group else stage
    
    home = match.get("homeTeam.name", "TBD")
    away = match.get("awayTeam.name", "TBD")
    time_str = match_dt.strftime('%I:%M %p')
    date_str = match_dt.strftime('%a %d %b')
    
    card = _make_monospace_card(
        stage_group=stage_group,
        home=home,
        away=away,
        time_str=time_str,
        date_str=date_str
    )
    
    return f"🔜 *Next Match \\({_escape(tz_str)}\\)*\n\n```\n{card}\n```"

async def _get_matches_text(chat_id: int) -> str:
    fav_teams = state.get_favourite_teams(chat_id)
    if not fav_teams:
        return "No favourite teams set\\. Use Add Team to choose one\\."
        
    try:
        now_utc = datetime.now(pytz.utc)
        all_upcoming = []
        all_wc_matches = football_api.get_all_wc_matches()
        for t in fav_teams:
            matches = football_api.filter_team_matches(all_wc_matches, t["id"])
            for m in matches:
                if m.get("status") not in ("SCHEDULED", "TIMED"):
                    continue
                match_dt = datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00"))
                if match_dt > now_utc:
                    all_upcoming.append(m)
    except RateLimitException:
        return RATE_LIMIT_MSG
        
    if not all_upcoming:
        return "No upcoming matches found for your favourite teams\\."
        
    # Deduplicate by match ID
    unique_upcoming = {m["id"]: m for m in all_upcoming}.values()
    sorted_upcoming = sorted(unique_upcoming, key=lambda x: x.get("utcDate", ""))
    
    user_tz, tz_str = _get_user_tz(chat_id)
    fav_names = ", ".join([t["name"] for t in fav_teams])
    msg = f"📋 *Upcoming matches \\({_escape(tz_str)}\\) — {_escape(fav_names)}*\n\n"
    
    for match in sorted_upcoming[:8]:
        match_dt = datetime.fromisoformat(match["utcDate"].replace("Z", "+00:00")).astimezone(user_tz)
        home = match.get("homeTeam.name", "TBD")
        away = match.get("awayTeam.name", "TBD")
        date_str = match_dt.strftime('%a %d %b')
        time_str = match_dt.strftime('%I:%M %p')
        stage = _format_stage(match.get("stage", ""))
        group = _format_group(match.get("group"))
        stage_group = f"{stage} · {group}" if group else stage
        
        card = _make_monospace_card(
            stage_group=stage_group,
            home=home,
            away=away,
            time_str=time_str,
            date_str=date_str
        )
        msg += f"```\n{card}\n```\n"
        
    return msg

async def nextmatch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    msg = await _get_nextmatch_text(chat_id)
    await send_text(context.application, chat_id, msg)

async def matches_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    msg = await _get_matches_text(chat_id)
    await send_text(context.application, chat_id, msg)

async def _get_results_text(chat_id: int, target_date_arg: str) -> str:
    user_tz, tz_str = _get_user_tz(chat_id)
    now_local = datetime.now(user_tz)

    if target_date_arg == "today":
        target_date = now_local.date()
    elif target_date_arg == "yesterday":
        target_date = (now_local - timedelta(days=1)).date()
    else:
        try:
            target_date = datetime.strptime(target_date_arg, "%Y-%m-%d").date()
        except ValueError:
            return "Usage: /results \\[today \\| yesterday \\| yyyy\\-MM\\-dd\\]"
            
    try:
        all_matches = football_api.get_all_wc_matches()
    except RateLimitException:
        return RATE_LIMIT_MSG

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
        return f"No World Cup matches on {_escape(target_date_str)}\\."
        
    if not finished_matches:
        return f"No finished matches on {_escape(target_date_str)}\\."
        
    finished_matches.sort(key=lambda x: x.get("utcDate", ""))
    
    msg = f"🏆 *Match Results — {_escape(target_date_str)}*\n\n"
    for m in finished_matches:
        home = m.get("homeTeam.name", "TBD")
        away = m.get("awayTeam.name", "TBD")
        h_score = str(m.get("score.fullTime.home") or 0)
        a_score = str(m.get("score.fullTime.away") or 0)
        stage = _format_stage(m.get("stage", ""))
        group = _format_group(m.get("group"))
        stage_group = f"{stage} · {group}" if group else stage
        
        card = _make_monospace_card(
            stage_group=stage_group,
            home=home,
            away=away,
            status_str="✅ FINISHED",
            score_str=f"{h_score} - {a_score}"
        )
        msg += f"```\n{card}\n```\n"
        
    return msg

async def _get_live_text() -> str:
    try:
        all_matches = football_api.get_all_wc_matches()
    except RateLimitException:
        return RATE_LIMIT_MSG

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
        return "No matches currently in progress\\."

    msg = ""
    for m in confirmed_live:
        home = m.get("homeTeam.name", "TBD")
        away = m.get("awayTeam.name", "TBD")
        h_score = str(m.get("score.fullTime.home") or 0)
        a_score = str(m.get("score.fullTime.away") or 0)
        stage = _format_stage(m.get("stage", ""))
        group = _format_group(m.get("group"))
        stage_group = f"{stage} · {group}" if group else stage
        
        card = _make_monospace_card(
            stage_group=stage_group,
            home=home,
            away=away,
            status_str="🔴 LIVE",
            score_str=f"{h_score} - {a_score}"
        )
        msg += f"```\n{card}\n```\n"

    for m in probable_live:
        home = m.get("homeTeam.name", "TBD")
        away = m.get("awayTeam.name", "TBD")
        h_score = m.get("score.fullTime.home")
        a_score = m.get("score.fullTime.away")
        stage = _format_stage(m.get("stage", ""))
        group = _format_group(m.get("group"))
        stage_group = f"{stage} · {group}" if group else stage

        match_dt = datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00"))
        elapsed_mins = int((now_utc - match_dt).total_seconds() // 60)

        score_val = f"{h_score} - {a_score}" if h_score is not None and a_score is not None else "vs"
        
        card = _make_monospace_card(
            stage_group=stage_group,
            home=home,
            away=away,
            status_str=f"🟡 IN PROGRESS (~{elapsed_mins}')",
            score_str=score_val
        )
        msg += f"```\n{card}\n```\n"

    return msg

async def results_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    target_date_arg = "today"
    if context.args:
        target_date_arg = context.args[0].lower()
    msg = await _get_results_text(chat_id, target_date_arg)
    await send_text(context.application, chat_id, msg)

async def live_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    msg = await _get_live_text()
    await send_text(context.application, chat_id, msg)

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

    # Stage display order — covers both old ROUND_OF_* and new LAST_* names used
    # by the football-data.org API for the expanded 48-team WC 2026 format.
    STAGE_ORDER = [
        "GROUP_STAGE",
        "ROUND_OF_32", "LAST_32",
        "ROUND_OF_16", "LAST_16",
        "QUARTER_FINALS",
        "SEMI_FINALS",
        "THIRD_PLACE",
        "FINAL"
    ]
    LIVE_STATUSES = ("IN_PLAY", "PAUSED", "EXTRA_TIME", "PENALTY_SHOOTOUT")
    FINISHED_STATUSES = ("FINISHED", "AWARDED", "CANCELLED")

    # Group matches: {stage -> {group_label -> [matches]}}
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

async def _get_today_text(chat_id: int) -> str:
    user_tz, tz_str = _get_user_tz(chat_id)

    try:
        all_matches = football_api.get_all_wc_matches()
    except RateLimitException:
        return RATE_LIMIT_MSG

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
        return "No World Cup matches today\\."
        
    today_matches.sort(key=lambda x: x.get("utcDate", ""))
    
    upcoming = []
    live = []
    finished = []
    
    for m in today_matches:
        status = m.get("status")
        home = m.get("homeTeam.name", "TBD")
        away = m.get("awayTeam.name", "TBD")
        stage = _format_stage(m.get("stage", ""))
        group = _format_group(m.get("group"))
        stage_group = f"{stage} · {group}" if group else stage
        match_dt_utc = datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00"))
        
        if status in ("IN_PLAY", "PAUSED", "EXTRA_TIME", "PENALTY_SHOOTOUT"):
            h_score = str(m.get("score.fullTime.home") or 0)
            a_score = str(m.get("score.fullTime.away") or 0)
            card = _make_monospace_card(
                stage_group=stage_group,
                home=home,
                away=away,
                status_str="🔴 LIVE",
                score_str=f"{h_score} - {a_score}"
            )
            live.append(card)
        elif status in ("FINISHED", "AWARDED"):
            h_score = str(m.get("score.fullTime.home") or 0)
            a_score = str(m.get("score.fullTime.away") or 0)
            card = _make_monospace_card(
                stage_group=stage_group,
                home=home,
                away=away,
                status_str="✅ FINISHED",
                score_str=f"{h_score} - {a_score}"
            )
            finished.append(card)
        elif status in ("SCHEDULED", "TIMED") and match_dt_utc > now_utc:
            match_dt_local = match_dt_utc.astimezone(user_tz)
            time_str = match_dt_local.strftime('%I:%M %p')
            card = _make_monospace_card(
                stage_group=stage_group,
                home=home,
                away=away,
                time_str=time_str
            )
            upcoming.append(card)
        elif status in ("SCHEDULED", "TIMED") and match_dt_utc <= now_utc:
            elapsed_mins = int((now_utc - match_dt_utc).total_seconds() // 60)
            if elapsed_mins <= 130:
                h_score = m.get("score.fullTime.home")
                a_score = m.get("score.fullTime.away")
                score_val = f"{h_score} - {a_score}" if h_score is not None and a_score is not None else "vs"
                card = _make_monospace_card(
                    stage_group=stage_group,
                    home=home,
                    away=away,
                    status_str=f"🟡 IN PROGRESS (~{elapsed_mins}')",
                    score_str=score_val
                )
                live.append(card)
            
    parts = [f"📅 *World Cup Matches Today \\({_escape(tz_str)}\\) — {_escape(target_date.strftime('%d %B %Y'))}*"]
    if live:
        parts.append("🔴 *LIVE NOW*")
        for card in live:
            parts.append(f"```\n{card}\n```")
    if upcoming:
        parts.append("🟢 *UPCOMING*")
        for card in upcoming:
            parts.append(f"```\n{card}\n```")
    if finished:
        parts.append("✅ *FINISHED*")
        for card in finished:
            parts.append(f"```\n{card}\n```")
        
    return "\n\n".join(parts)

async def _get_tomorrow_text(chat_id: int) -> str:
    user_tz, tz_str = _get_user_tz(chat_id)

    try:
        all_matches = football_api.get_all_wc_matches()
    except RateLimitException:
        return RATE_LIMIT_MSG

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
        return "No World Cup matches tomorrow\\."

    tomorrow_matches.sort(key=lambda x: x.get("utcDate", ""))

    upcoming = []
    finished = []

    for m in tomorrow_matches:
        status = m.get("status")
        home = m.get("homeTeam.name", "TBD")
        away = m.get("awayTeam.name", "TBD")
        stage = _format_stage(m.get("stage", ""))
        group = _format_group(m.get("group"))
        stage_group = f"{stage} · {group}" if group else stage
        match_dt_local = datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00")).astimezone(user_tz)
        time_str = match_dt_local.strftime('%I:%M %p')

        if status in ("FINISHED", "AWARDED"):
            h_score = str(m.get("score.fullTime.home") or 0)
            a_score = str(m.get("score.fullTime.away") or 0)
            card = _make_monospace_card(
                stage_group=stage_group,
                home=home,
                away=away,
                status_str="✅ FINISHED",
                score_str=f"{h_score} - {a_score}"
            )
            finished.append(card)
        else:
            card = _make_monospace_card(
                stage_group=stage_group,
                home=home,
                away=away,
                time_str=time_str
            )
            upcoming.append(card)

    parts = [f"📅 *World Cup Matches Tomorrow \\({_escape(tz_str)}\\) — {_escape(target_date.strftime('%d %B %Y'))}*"]
    if upcoming:
        parts.append("🟢 *UPCOMING*")
        for card in upcoming:
            parts.append(f"```\n{card}\n```")
    if finished:
        parts.append("✅ *FINISHED*")
        for card in finished:
            parts.append(f"```\n{card}\n```")

    return "\n\n".join(parts)

async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    msg = await _get_today_text(chat_id)
    await send_text(context.application, chat_id, msg)

async def tomorrow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    msg = await _get_tomorrow_text(chat_id)
    await send_text(context.application, chat_id, msg)

async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    fav_teams = state.get_favourite_teams(chat_id)
    fav_names = ", ".join([t["name"] for t in fav_teams]) or "None"
    
    offsets = state.get_reminder_offsets(chat_id)
    active_labels = []
    for off in sorted(offsets):
        if off < 60:
            active_labels.append(f"{off}m")
        elif off % 60 == 0:
            active_labels.append(f"{off//60}h")
        else:
            active_labels.append(f"{off/60}h")
    reminder_str = ", ".join(active_labels) if active_labels else "None"
    
    reminders_on = "On" if state.get_setting(chat_id, "reminders_enabled") != "false" else "Off"
    myscores_on = "On" if state.get_setting(chat_id, "my_scores_enabled") != "false" else "Off"
    allscores_on = "On" if state.get_setting(chat_id, "all_scores_enabled") != "false" else "Off"
    livegoals_on = "On" if state.get_setting(chat_id, "live_goals_enabled") != "false" else "Off"
    tz_str = state.get_setting(chat_id, "timezone") or "UTC"
    
    msg = (
        "⚙️ *Settings*\n\n"
        f"👕 Favourite teams: *{_escape(fav_names)}*\n"
        f"⏰ Reminder: *{_escape(reminder_str)} before kickoff*\n"
        f"🔔 Pre\\-match reminders: *{reminders_on}*\n"
        f"⭐ My team results: *{myscores_on}*\n"
        f"🌍 All match results: *{allscores_on}*\n"
        f"⚽ Live goal alerts: *{livegoals_on}*\n"
        f"🕐 Timezone: *{_escape(tz_str)}*"
    )
    await send_text(context.application, chat_id, msg)

async def _bg_sync_schedule(application):
    """Runs scheduler.sync_schedule in a background thread without blocking the main loop."""
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, scheduler.sync_schedule, application)
    except Exception as e:
        logger.error(f"Error in background sync_schedule: {e}")

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

def _get_status_text(chat_id: int) -> str:
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
        
    return msg

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    msg = _get_status_text(chat_id)
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

def _get_stats_text() -> str:
    users = state.get_all_users()
    total_users = len(users)
    return f"📊 *Bot Statistics*\n\nTotal unique users tracking teams: *{total_users}*"

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    msg = _get_stats_text()
    await send_text(context.application, chat_id, msg)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    
    help_text = (
        "⚽ Hey there\\! Welcome to your FIFA World Cup Bot\\.\n\n"
        "Just type /menu to open your dashboard and manage everything with a few taps\\. Here’s what you can do:\n\n"
        "👕 *My Teams*\n"
        "• *Add Team*: Pick your favorite squads to track \\(e\\.g\\., `✅ Brazil`\\)\\. You can select multiple before hitting Done\\.\n"
        "• *Remove Team*: Tap any country on your list to stop tracking them\\.\n"
        "• *View Favorites*: See a quick lineup of all the teams you’re currently following\\.\n\n"
        "⚙️ *Settings & Preferences*\n"
        "• *Timezone*: Set your city so match kickoff times match your local clock\\.\n"
        "• *Reminder Offset*: Choose how early you want a heads\\-up \\(e\\.g\\., 15m or 1h before kickoff\\)\\.\n"
        "• *Notification Toggles*: Customize alerts for pre\\-match reminders, live goals, or final scores\\.\n"
        "• *View Settings Summary*: Take a quick look at your current alert preferences\\.\n\n"
        "📅 *Schedule & Matches*\n"
        "• *Next Match*: Check out the very next game for your favorite teams\\.\n"
        "• *My Schedule*: See a personalized list of all upcoming matches for your squads\\.\n"
        "• *Matches Today / Tomorrow*: View the full daily lineup for today or tomorrow\\.\n"
        "• *Live Matches*: Get real\\-time, minute\\-by\\-minute cards for games happening right now\\.\n"
        "• *Match Results*: Catch up on scores from today or yesterday\\.\n"
        "• *Full Fixture*: Download the complete tournament schedule straight to your device\\.\n\n"
        "🤖 *Bot Info & Stats*\n"
        "• *Bot Status & Stats*: Check the bot's health, uptime, and community usage stats\\.\n"
        "• *Sync Now*: Force a quick manual refresh to update all schedules and alerts\\."
    )
    await send_text(context.application, chat_id, help_text)

async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_master_menu(update, context)

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
TIMEZONES_BY_REGION = {
    "Africa": ["Africa/Cairo", "Africa/Lagos", "Africa/Johannesburg", "Africa/Nairobi", "Africa/Casablanca", "Africa/Algiers"],
    "America": ["America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles", "America/Mexico_City", "America/Sao_Paulo", "America/Argentina/Buenos_Aires", "America/Bogota"],
    "Asia": ["Asia/Dhaka", "Asia/Kolkata", "Asia/Riyadh", "Asia/Tokyo", "Asia/Singapore", "Asia/Dubai", "Asia/Jakarta", "Asia/Tehran"],
    "Europe": ["Europe/London", "Europe/Paris", "Europe/Berlin", "Europe/Rome", "Europe/Madrid", "Europe/Istanbul", "Europe/Moscow", "Europe/Kyiv"],
    "Pacific": ["Pacific/Auckland", "Pacific/Sydney", "Pacific/Honolulu", "Pacific/Fiji"]
}

async def show_master_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, query=None):
    chat_id = update.effective_chat.id if (update and update.effective_chat) else query.message.chat_id
    
    keyboard = [
        [
            InlineKeyboardButton("👕 My Teams", callback_data="menu:teams"),
            InlineKeyboardButton("⚙️ Settings & Preferences", callback_data="menu:settings")
        ],
        [
            InlineKeyboardButton("📅 Schedule & Matches", callback_data="menu:schedule"),
            InlineKeyboardButton("🤖 Bot Info & Stats", callback_data="menu:system")
        ],
        [
            InlineKeyboardButton("❌ Close Menu", callback_data="menu:close")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = (
        "👋 *FIFA World Cup Bot Master Menu*\n\n"
        "Select a section below to manage your teams, adjust settings/timezone, track live scores, view matches, and check bot status\\."
    )
    
    if query:
        try:
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="MarkdownV2")
        except TelegramError as e:
            if "Message is not modified" not in str(e):
                logger.error(f"Error updating master menu: {e}")
    else:
        await send_text(context.application, chat_id, text, reply_markup=reply_markup)

async def show_menu_teams(query, context):
    keyboard = [
        [
            InlineKeyboardButton("➕ Add Team", callback_data="addteam:menu_add"),
            InlineKeyboardButton("➖ Remove Team", callback_data="removeteam:menu_remove")
        ],
        [
            InlineKeyboardButton("📋 View Favourites", callback_data="menu:viewteams")
        ],
        [
            InlineKeyboardButton("◀️ Back to Main Menu", callback_data="menu:main")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = "👕 *My Teams Menu*\n\nManage your favourite teams for match reminders and results\\."
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="MarkdownV2")

async def show_menu_settings(query, context):
    keyboard = [
        [
            InlineKeyboardButton("📋 View Settings Summary", callback_data="menu:viewsettings")
        ],
        [
            InlineKeyboardButton("🌍 Set Timezone", callback_data="settimezone:regions"),
            InlineKeyboardButton("⏰ Set Reminder Offset", callback_data="setreminder:menu_rem")
        ],
        [
            InlineKeyboardButton("🔔 Reminders Toggle", callback_data="menu:toggle_rem"),
            InlineKeyboardButton("⭐ My Results Toggle", callback_data="menu:toggle_my")
        ],
        [
            InlineKeyboardButton("🌍 All Results Toggle", callback_data="menu:toggle_all"),
            InlineKeyboardButton("⚽ Live Goals Toggle", callback_data="menu:toggle_live")
        ],
        [
            InlineKeyboardButton("◀️ Back to Main Menu", callback_data="menu:main")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = "⚙️ *Settings & Preferences*\n\nConfigure your timezone, reminders, and notification preferences\\."
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="MarkdownV2")

async def show_menu_schedule(query, context):
    keyboard = [
        [
            InlineKeyboardButton("🔜 Next Match", callback_data="menu:info_nextmatch"),
            InlineKeyboardButton("📋 My Schedule", callback_data="menu:info_matches")
        ],
        [
            InlineKeyboardButton("📅 Matches Today", callback_data="menu:info_today"),
            InlineKeyboardButton("📅 Matches Tomorrow", callback_data="menu:info_tomorrow")
        ],
        [
            InlineKeyboardButton("🔴 Live Matches", callback_data="menu:info_live"),
            InlineKeyboardButton("🏆 Full Fixture", callback_data="menu:info_fixture")
        ],
        [
            InlineKeyboardButton("✅ Match Results", callback_data="menu:results_menu")
        ],
        [
            InlineKeyboardButton("◀️ Back to Main Menu", callback_data="menu:main")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = "📅 *Schedule & Matches*\n\nTrack matches, schedules, and results for the tournament\\."
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="MarkdownV2")

async def show_results_date_menu(query, context):
    keyboard = [
        [
            InlineKeyboardButton("Today's Results", callback_data="menu:info_results:today"),
            InlineKeyboardButton("Yesterday's Results", callback_data="menu:info_results:yesterday")
        ],
        [
            InlineKeyboardButton("◀️ Back to Schedule", callback_data="menu:schedule")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = "✅ *Match Results*\n\nSelect which results you want to view:"
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="MarkdownV2")

async def show_menu_system(query, context):
    keyboard = [
        [
            InlineKeyboardButton("🤖 Bot Status", callback_data="menu:info_status"),
            InlineKeyboardButton("📊 Bot Statistics", callback_data="menu:info_stats")
        ],
        [
            InlineKeyboardButton("🔄 Sync Now", callback_data="menu:info_syncnow")
        ],
        [
            InlineKeyboardButton("◀️ Back to Main Menu", callback_data="menu:main")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = "🤖 *Bot Info & Stats*\n\nCheck system status, diagnostics, and force data synchronization\\."
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="MarkdownV2")


async def show_addteam_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int, query=None):
    chat_id = update.effective_chat.id if (update and update.effective_chat) else query.message.chat_id
    
    try:
        teams = football_api.get_all_teams()
    except RateLimitException:
        if query:
            await query.edit_message_text(RATE_LIMIT_MSG, parse_mode="MarkdownV2")
        else:
            await send_text(context.application, chat_id, RATE_LIMIT_MSG)
        return
        
    if not teams:
        msg = "❌ No teams available\\."
        if query:
            await query.edit_message_text(msg, parse_mode="MarkdownV2")
        else:
            await send_text(context.application, chat_id, msg)
        return

    # Get user's favorites
    fav_teams = state.get_favourite_teams(chat_id)
    fav_ids = {t["id"] for t in fav_teams}
    
    # Show all teams, sorted alphabetically
    available_teams = list(teams)
    available_teams.sort(key=lambda t: t["name"])

    PAGE_SIZE = 10
    total_pages = (len(available_teams) + PAGE_SIZE - 1) // PAGE_SIZE
    if page < 0:
        page = 0
    elif page >= total_pages:
        page = total_pages - 1
        
    page_teams = available_teams[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    
    # Build keyboard (2 columns)
    keyboard = []
    for i in range(0, len(page_teams), 2):
        row = []
        t1 = page_teams[i]
        label1 = f"✅ {t1['name']}" if t1["id"] in fav_ids else t1["name"]
        row.append(InlineKeyboardButton(label1, callback_data=f"addteam:select:{t1['id']}:{page}"))
        if i + 1 < len(page_teams):
            t2 = page_teams[i + 1]
            label2 = f"✅ {t2['name']}" if t2["id"] in fav_ids else t2["name"]
            row.append(InlineKeyboardButton(label2, callback_data=f"addteam:select:{t2['id']}:{page}"))
        keyboard.append(row)
        
    # Navigation row
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️ Prev", callback_data=f"addteam:page:{page-1}"))
    
    nav_row.append(InlineKeyboardButton(f"{page+1} / {total_pages}", callback_data="addteam:noop"))
    
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"addteam:page:{page+1}"))
        
    keyboard.append(nav_row)
    keyboard.append([InlineKeyboardButton("◀️ Back to My Teams", callback_data="addteam:cancel")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = "👕 *Select teams to add or remove from your favourites:*"
    
    if query:
        try:
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="MarkdownV2")
        except TelegramError as e:
            if "Message is not modified" not in str(e):
                logger.error(f"Error updating addteam menu: {e}")
    else:
        await send_text(context.application, chat_id, text, reply_markup=reply_markup)

async def show_removeteam_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, query=None):
    chat_id = update.effective_chat.id if (update and update.effective_chat) else query.message.chat_id
    
    fav_teams = state.get_favourite_teams(chat_id)
    if not fav_teams:
        msg = "No favourite teams set\\. Use Add Team to choose one\\."
        if query:
            keyboard = [[InlineKeyboardButton("◀️ Back to My Teams", callback_data="menu:teams")]]
            await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="MarkdownV2")
        else:
            await send_text(context.application, chat_id, msg)
        return
        
    keyboard = []
    for team in fav_teams:
        keyboard.append([InlineKeyboardButton(f"✅ {team['name']}", callback_data=f"removeteam:select:{team['id']}")])
        
    keyboard.append([InlineKeyboardButton("◀️ Back to My Teams", callback_data="removeteam:cancel")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    text = "🗑 *Select teams to remove from your favourites:*"
    if query:
        try:
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="MarkdownV2")
        except TelegramError as e:
            if "Message is not modified" not in str(e):
                logger.error(f"Error updating removeteam menu: {e}")
    else:
        await send_text(context.application, chat_id, text, reply_markup=reply_markup)

async def show_setreminder_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, query=None):
    chat_id = update.effective_chat.id if (update and update.effective_chat) else query.message.chat_id
    
    active_offsets = state.get_reminder_offsets(chat_id)
    
    def get_label(label: str, val: int):
        return f"✅ {label}" if val in active_offsets else label
        
    keyboard = [
        [
            InlineKeyboardButton(get_label("15 mins", 15), callback_data="setreminder:select:15"),
            InlineKeyboardButton(get_label("30 mins", 30), callback_data="setreminder:select:30")
        ],
        [
            InlineKeyboardButton(get_label("45 mins", 45), callback_data="setreminder:select:45"),
            InlineKeyboardButton(get_label("1 hour", 60), callback_data="setreminder:select:60")
        ],
        [
            InlineKeyboardButton(get_label("2 hours", 120), callback_data="setreminder:select:120"),
            InlineKeyboardButton(get_label("3 hours", 180), callback_data="setreminder:select:180")
        ],
        [
            InlineKeyboardButton(get_label("6 hours", 360), callback_data="setreminder:select:360"),
            InlineKeyboardButton(get_label("12 hours", 720), callback_data="setreminder:select:720")
        ],
        [
            InlineKeyboardButton(get_label("24 hours", 1440), callback_data="setreminder:select:1440")
        ],
        [
            InlineKeyboardButton("◀️ Back to Settings", callback_data="setreminder:cancel")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    active_labels = []
    for off in sorted(active_offsets):
        if off < 60:
            active_labels.append(f"{off}m")
        elif off % 60 == 0:
            active_labels.append(f"{off//60}h")
        else:
            active_labels.append(f"{off/60}h")
            
    active_str = ", ".join(active_labels) if active_labels else "None"
    text = f"⏰ *Select when you want to receive pre\\-match reminders:*\n\nActive reminders: *{_escape(active_str)}*"
    
    if query:
        try:
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="MarkdownV2")
        except TelegramError as e:
            if "Message is not modified" not in str(e):
                logger.error(f"Error updating setreminder menu: {e}")
    else:
        await send_text(context.application, chat_id, text, reply_markup=reply_markup)

async def show_timezone_regions_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, query=None):
    chat_id = update.effective_chat.id if (update and update.effective_chat) else query.message.chat_id
    
    keyboard = [
        [
            InlineKeyboardButton("Africa", callback_data="settimezone:region:Africa"),
            InlineKeyboardButton("America", callback_data="settimezone:region:America")
        ],
        [
            InlineKeyboardButton("Asia", callback_data="settimezone:region:Asia"),
            InlineKeyboardButton("Europe", callback_data="settimezone:region:Europe")
        ],
        [
            InlineKeyboardButton("Pacific", callback_data="settimezone:region:Pacific"),
            InlineKeyboardButton("UTC", callback_data="settimezone:select:UTC")
        ],
        [
            InlineKeyboardButton("◀️ Back to Settings", callback_data="settimezone:cancel")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = "🌍 *Select your timezone region:*"
    
    if query:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="MarkdownV2")
    else:
        await send_text(context.application, chat_id, text, reply_markup=reply_markup)

async def show_timezone_list_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, region: str, query):
    keyboard = []
    tz_list = TIMEZONES_BY_REGION.get(region, [])
    
    for i in range(0, len(tz_list), 2):
        row = []
        tz1 = tz_list[i]
        label1 = tz1.split("/")[-1].replace("_", " ")
        row.append(InlineKeyboardButton(label1, callback_data=f"settimezone:select:{tz1}"))
        if i + 1 < len(tz_list):
            tz2 = tz_list[i+1]
            label2 = tz2.split("/")[-1].replace("_", " ")
            row.append(InlineKeyboardButton(label2, callback_data=f"settimezone:select:{tz2}"))
        keyboard.append(row)
        
    keyboard.append([InlineKeyboardButton("◀️ Back to Regions", callback_data="settimezone:regions")])
    keyboard.append([InlineKeyboardButton("◀️ Back to Settings", callback_data="settimezone:cancel")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = f"🌍 *Select your timezone in {region}:*"
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="MarkdownV2")

async def show_toggle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, setting_key: str, current_label: str, query=None):
    chat_id = update.effective_chat.id if (update and update.effective_chat) else query.message.chat_id
    
    current = state.get_setting(chat_id, setting_key)
    status_str = "On" if current != "false" else "Off"
    
    text = f"{current_label}: *{status_str}*\n\nSelect a status below to toggle:"
    
    keyboard = [
        [
            InlineKeyboardButton("🟢 On", callback_data=f"toggle:{setting_key}:on"),
            InlineKeyboardButton("🔴 Off", callback_data=f"toggle:{setting_key}:off")
        ],
        [
            InlineKeyboardButton("◀️ Back to Settings", callback_data=f"toggle:{setting_key}:cancel")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if query:
        try:
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="MarkdownV2")
        except TelegramError as e:
            if "Message is not modified" not in str(e):
                logger.error(f"Error updating toggle menu: {e}")
    else:
        await send_text(context.application, chat_id, text, reply_markup=reply_markup)

async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    chat_id = query.message.chat_id
    data = query.data
    
    # 1. Master Menu navigation
    if data == "menu:main":
        await show_master_menu(update, context, query=query)
        return
        
    if data == "menu:teams":
        await show_menu_teams(query, context)
        return
        
    if data == "menu:settings":
        await show_menu_settings(query, context)
        return
        
    if data == "menu:schedule":
        await show_menu_schedule(query, context)
        return
        
    if data == "menu:system":
        await show_menu_system(query, context)
        return
        
    if data == "menu:results_menu":
        await show_results_date_menu(query, context)
        return
        
    if data == "menu:close":
        await query.delete_message()
        return

    # 2. Master Menu Actions (Views)
    if data == "menu:viewteams":
        fav_teams = state.get_favourite_teams(chat_id)
        if fav_teams:
            names = "\n• ".join([_escape(t["name"]) for t in fav_teams])
            text = f"👕 *Favourite teams:*\n• {names}"
        else:
            text = "No favourite teams set\\. Use Add Team to choose one\\."
        keyboard = [[InlineKeyboardButton("◀️ Back to My Teams", callback_data="menu:teams")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="MarkdownV2")
        return
        
    if data == "menu:viewsettings":
        fav_teams = state.get_favourite_teams(chat_id)
        fav_names = ", ".join([t["name"] for t in fav_teams]) or "None"
        
        offsets = state.get_reminder_offsets(chat_id)
        active_labels = []
        for off in sorted(offsets):
            if off < 60:
                active_labels.append(f"{off}m")
            elif off % 60 == 0:
                active_labels.append(f"{off//60}h")
            else:
                active_labels.append(f"{off/60}h")
        reminder_str = ", ".join(active_labels) if active_labels else "None"
        
        reminders_on = "On" if state.get_setting(chat_id, "reminders_enabled") != "false" else "Off"
        myscores_on = "On" if state.get_setting(chat_id, "my_scores_enabled") != "false" else "Off"
        allscores_on = "On" if state.get_setting(chat_id, "all_scores_enabled") != "false" else "Off"
        livegoals_on = "On" if state.get_setting(chat_id, "live_goals_enabled") != "false" else "Off"
        tz_str = state.get_setting(chat_id, "timezone") or "UTC"
        text = (
            "⚙️ *Settings Summary*\n\n"
            f"👕 Favourite teams: *{_escape(fav_names)}*\n"
            f"⏰ Reminder: *{_escape(reminder_str)} before kickoff*\n"
            f"🔔 Pre\\-match reminders: *{reminders_on}*\n"
            f"⭐ My team results: *{myscores_on}*\n"
            f"🌍 All match results: *{allscores_on}*\n"
            f"⚽ Live goal alerts: *{livegoals_on}*\n"
            f"🕐 Timezone: *{_escape(tz_str)}*"
        )
        keyboard = [[InlineKeyboardButton("◀️ Back to Settings", callback_data="menu:settings")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="MarkdownV2")
        return

    if data == "menu:info_nextmatch":
        await query.edit_message_text("⏳ Fetching next match details…", parse_mode="MarkdownV2")
        text = await _get_nextmatch_text(chat_id)
        keyboard = [[InlineKeyboardButton("◀️ Back to Schedule", callback_data="menu:schedule")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="MarkdownV2")
        return
        
    if data == "menu:info_matches":
        await query.edit_message_text("⏳ Fetching your matches…", parse_mode="MarkdownV2")
        text = await _get_matches_text(chat_id)
        keyboard = [[InlineKeyboardButton("◀️ Back to Schedule", callback_data="menu:schedule")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="MarkdownV2")
        return
        
    if data == "menu:info_live":
        await query.edit_message_text("⏳ Fetching live match scores…", parse_mode="MarkdownV2")
        text = await _get_live_text()
        keyboard = [[InlineKeyboardButton("◀️ Back to Schedule", callback_data="menu:schedule")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="MarkdownV2")
        return
        
    if data == "menu:info_today":
        await query.edit_message_text("⏳ Fetching today's matches…", parse_mode="MarkdownV2")
        text = await _get_today_text(chat_id)
        keyboard = [[InlineKeyboardButton("◀️ Back to Schedule", callback_data="menu:schedule")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="MarkdownV2")
        return
        
    if data == "menu:info_tomorrow":
        await query.edit_message_text("⏳ Fetching tomorrow's matches…", parse_mode="MarkdownV2")
        text = await _get_tomorrow_text(chat_id)
        keyboard = [[InlineKeyboardButton("◀️ Back to Schedule", callback_data="menu:schedule")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="MarkdownV2")
        return
        
    if data == "menu:info_fixture":
        await query.edit_message_text("📋 Sending the full fixture list to the chat…", parse_mode="MarkdownV2")
        await fixture_cmd(update, context)
        return
        
    if data.startswith("menu:info_results:"):
        await query.edit_message_text("⏳ Fetching match results…", parse_mode="MarkdownV2")
        target_date_arg = data.split(":")[2]
        text = await _get_results_text(chat_id, target_date_arg)
        keyboard = [[InlineKeyboardButton("◀️ Back to Results", callback_data="menu:results_menu")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="MarkdownV2")
        return
        
    if data == "menu:info_status":
        text = _get_status_text(chat_id)
        keyboard = [[InlineKeyboardButton("◀️ Back to Bot Info", callback_data="menu:system")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="MarkdownV2")
        return
        
    if data == "menu:info_stats":
        text = _get_stats_text()
        keyboard = [[InlineKeyboardButton("◀️ Back to Bot Info", callback_data="menu:system")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="MarkdownV2")
        return
        
    if data == "menu:info_syncnow":
        await query.edit_message_text("🔄 Syncing…", parse_mode="MarkdownV2")
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, scheduler.sync_schedule, context.application)
        except RateLimitException:
            await query.edit_message_text(RATE_LIMIT_MSG, parse_mode="MarkdownV2")
            return
        jobs = scheduler._scheduler.get_jobs()
        reminders = sum(1 for j in jobs if j.id.startswith("remind_"))
        pollers = sum(1 for j in jobs if j.id.startswith("poll_"))
        text = f"✅ Done\\. Reminders: {_escape(str(reminders))}\\. Result pollers: {_escape(str(pollers))}\\."
        keyboard = [[InlineKeyboardButton("◀️ Back to Bot Info", callback_data="menu:system")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="MarkdownV2")
        return

    # 3. Form Commands menu integration triggers
    if data == "addteam:menu_add":
        await show_addteam_menu(update, context, page=0, query=query)
        return
        
    if data == "removeteam:menu_remove":
        await show_removeteam_menu(update, context, query=query)
        return
        
    if data == "setreminder:menu_rem":
        await show_setreminder_menu(update, context, query=query)
        return
        
    if data == "menu:toggle_rem":
        await show_toggle_menu(update, context, "reminders_enabled", "🔔 Pre\\-match reminders", query=query)
        return
        
    if data == "menu:toggle_my":
        await show_toggle_menu(update, context, "my_scores_enabled", "⭐ My team result notifications", query=query)
        return
        
    if data == "menu:toggle_all":
        await show_toggle_menu(update, context, "all_scores_enabled", "🌍 All match result notifications", query=query)
        return
        
    if data == "menu:toggle_live":
        await show_toggle_menu(update, context, "live_goals_enabled", "⚽ Live goal notifications", query=query)
        return

    # 4. Command cancel/back buttons
    if data == "addteam:noop":
        return
        
    if data == "addteam:cancel":
        await show_menu_teams(query, context)
        return
        
    if data.startswith("addteam:page:"):
        page = int(data.split(":")[2])
        await show_addteam_menu(update, context, page=page, query=query)
        return
        
    if data.startswith("addteam:select:"):
        parts = data.split(":")
        team_id = int(parts[2])
        page = int(parts[3]) if len(parts) > 3 else 0
        try:
            teams = football_api.get_all_teams()
            team = next((t for t in teams if t["id"] == team_id), None)
            if not team:
                await query.edit_message_text("❌ Selected team not found\\.", parse_mode="MarkdownV2")
                return
                
            fav_teams = state.get_favourite_teams(chat_id)
            if any(t["id"] == team_id for t in fav_teams):
                state.remove_favourite_team(chat_id, team_id)
            else:
                state.add_favourite_team(chat_id, team)
            
            # Instantly update UI page to toggle checkmark
            await show_addteam_menu(update, context, page=page, query=query)
            
            # Sync schedule in background to prevent lag
            asyncio.create_task(_bg_sync_schedule(context.application))
            
        except RateLimitException:
            await query.edit_message_text(RATE_LIMIT_MSG, parse_mode="MarkdownV2")
        except Exception as e:
            logger.error(f"Error in callback addteam select: {e}")
            await query.edit_message_text("❌ An error occurred while managing the team\\.", parse_mode="MarkdownV2")
        return
        
    if data == "removeteam:cancel":
        await show_menu_teams(query, context)
        return
        
    if data.startswith("removeteam:select:"):
        team_id = int(data.split(":")[2])
        fav_teams = state.get_favourite_teams(chat_id)
        matched_team = next((t for t in fav_teams if t["id"] == team_id), None)
        
        if matched_team:
            state.remove_favourite_team(chat_id, matched_team["id"])
            
            # Instantly update UI checklist
            await show_removeteam_menu(update, context, query=query)
            
            # Sync schedule in background
            asyncio.create_task(_bg_sync_schedule(context.application))
        return
        
    if data == "setreminder:cancel":
        await show_menu_settings(query, context)
        return
        
    if data.startswith("setreminder:select:"):
        minutes = int(data.split(":")[2])
        active_offsets = state.get_reminder_offsets(chat_id)
        if minutes in active_offsets:
            active_offsets.remove(minutes)
        else:
            active_offsets.append(minutes)
        state.set_reminder_offsets(chat_id, active_offsets)
        
        # Instantly update UI checklist
        await show_setreminder_menu(update, context, query=query)
        
        # Sync schedule in background
        asyncio.create_task(_bg_sync_schedule(context.application))
        return
        
    if data == "settimezone:cancel":
        await show_menu_settings(query, context)
        return
        
    if data == "settimezone:regions":
        await show_timezone_regions_menu(update, context, query=query)
        return
        
    if data.startswith("settimezone:region:"):
        region = data.split(":")[2]
        await show_timezone_list_menu(update, context, region, query)
        return
        
    if data.startswith("settimezone:select:"):
        tz_str = data.split(":")[2]
        try:
            pytz.timezone(tz_str)
            state.set_setting(chat_id, "timezone", tz_str)
            text = f"🌍 Timezone set to *{_escape(tz_str)}*\\."
            keyboard = [[InlineKeyboardButton("◀️ Back to Settings", callback_data="menu:settings")]]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="MarkdownV2")
        except pytz.exceptions.UnknownTimeZoneError:
            await query.edit_message_text("❌ Unknown timezone\\.", parse_mode="MarkdownV2")
        return

    if data.startswith("toggle:"):
        _, setting_key, value = data.split(":")
        labels = {
            "reminders_enabled": "🔔 Pre\\-match reminders",
            "my_scores_enabled": "⭐ My team result notifications",
            "all_scores_enabled": "🌍 All match result notifications",
            "live_goals_enabled": "⚽ Live goal notifications"
        }
        label = labels.get(setting_key, setting_key)
        
        if value == "cancel":
            await show_menu_settings(query, context)
            return
            
        new_val = "true" if value == "on" else "false"
        state.set_setting(chat_id, setting_key, new_val)
        
        # Update the toggle menu layout directly
        await show_toggle_menu(None, context, setting_key, label, query=query)
        return

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
    application.add_handler(CommandHandler("menu", menu_cmd))

    application.add_handler(CallbackQueryHandler(button_callback_handler))

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
