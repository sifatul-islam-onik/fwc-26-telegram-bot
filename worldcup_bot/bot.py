"""Entry point: registers commands and starts the bot."""
import logging
import asyncio
from datetime import datetime, timedelta
import pytz
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.error import TelegramError

import config
import state
import football_api
import scheduler
from notifier import send_text, _escape, _format_stage, _format_group

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
        
    all_upcoming = []
    for t in fav_teams:
        matches = football_api.get_team_matches(t["id"])
        upcoming = [m for m in matches if m.get("status") in ("SCHEDULED", "TIMED")]
        all_upcoming.extend(upcoming)
        
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
        
    all_upcoming = []
    for t in fav_teams:
        matches = football_api.get_team_matches(t["id"])
        upcoming = [m for m in matches if m.get("status") in ("SCHEDULED", "TIMED")]
        all_upcoming.extend(upcoming)
        
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

    if context.args:
        arg = context.args[0].lower()
        if arg == "today":
            target_date = datetime.now(pytz.utc).date()
        elif arg == "yesterday":
            target_date = (datetime.now(pytz.utc) - timedelta(days=1)).date()
        else:
            try:
                target_date = datetime.strptime(arg, "%Y-%m-%d").date()
            except ValueError:
                await send_text(context.application, chat_id, "Usage: /results \\[today \\| yesterday \\| yyyy\\-MM\\-dd\\]")
                return
    else:
        target_date = datetime.now(pytz.utc).date()
        
    all_matches = football_api.get_all_wc_matches()
    target_date_str = target_date.strftime("%Y-%m-%d")
    
    finished_matches = []
    day_had_matches = False
    
    for m in all_matches:
        utc_date_str = m.get("utcDate", "")
        if utc_date_str.startswith(target_date_str):
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

    all_matches = football_api.get_all_wc_matches()
    live_matches = [m for m in all_matches if m.get("status") in ("IN_PLAY", "PAUSED", "EXTRA_TIME", "PENALTY_SHOOTOUT")]
    
    if not live_matches:
        await send_text(context.application, chat_id, "No matches currently in progress\\.")
        return
        
    lines = []
    for m in live_matches:
        home_esc = _escape(m.get("homeTeam.name", "TBD"))
        away_esc = _escape(m.get("awayTeam.name", "TBD"))
        h_score = _escape(str(m.get("score.fullTime.home") or 0))
        a_score = _escape(str(m.get("score.fullTime.away") or 0))
        stage = _format_stage(m.get("stage", ""))
        group = _format_group(m.get("group"))
        stage_group = _escape(f"{stage} · {group}" if group else stage)
        
        lines.append(f"🔴 LIVE — *{home_esc}* {h_score} – {a_score} *{away_esc}* · _{stage_group}_")
        
    await send_text(context.application, chat_id, "\n\n".join(lines))

async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    all_matches = football_api.get_all_wc_matches()
    target_date = datetime.now(pytz.utc).date()
    target_date_str = target_date.strftime("%Y-%m-%d")
    
    today_matches = [m for m in all_matches if m.get("utcDate", "").startswith(target_date_str)]
    
    if not today_matches:
        await send_text(context.application, chat_id, "No World Cup matches today\\.")
        return
        
    today_matches.sort(key=lambda x: x.get("utcDate", ""))
    
    upcoming = []
    live = []
    finished = []
    
    user_tz, tz_str = _get_user_tz(chat_id)
    
    for m in today_matches:
        status = m.get("status")
        home_esc = _escape(m.get("homeTeam.name", "TBD"))
        away_esc = _escape(m.get("awayTeam.name", "TBD"))
        stage = _format_stage(m.get("stage", ""))
        group = _format_group(m.get("group"))
        stage_group = _escape(f"{stage} · {group}" if group else stage)
        
        if status in ("SCHEDULED", "TIMED"):
            match_dt = datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00")).astimezone(user_tz)
            time_esc = _escape(match_dt.strftime('%I:%M %p'))
            tz_esc = _escape(tz_str)
            upcoming.append(f"• {home_esc} vs {away_esc} — {time_esc} \\({tz_esc}\\) · {stage_group}")
        elif status in ("IN_PLAY", "PAUSED", "EXTRA_TIME", "PENALTY_SHOOTOUT"):
            h_score = _escape(str(m.get("score.fullTime.home") or 0))
            a_score = _escape(str(m.get("score.fullTime.away") or 0))
            live.append(f"• {home_esc} {h_score} – {a_score} {away_esc} · {stage_group}")
        elif status in ("FINISHED", "AWARDED"):
            h_score = _escape(str(m.get("score.fullTime.home") or 0))
            a_score = _escape(str(m.get("score.fullTime.away") or 0))
            finished.append(f"• {home_esc} {h_score} – {a_score} {away_esc} · {stage_group}")
            
    parts = [f"📅 *World Cup Matches Today — {_escape(target_date.strftime('%d %B %Y'))}*"]
    if upcoming:
        parts.append("🟢 UPCOMING\n" + "\n".join(upcoming))
    if live:
        parts.append("🔴 LIVE NOW\n" + "\n".join(live))
    if finished:
        parts.append("✅ FINISHED\n" + "\n".join(finished))
        
    await send_text(context.application, chat_id, "\n\n".join(parts))

async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    fav_teams = state.get_favourite_teams(chat_id)
    fav_names = ", ".join([t["name"] for t in fav_teams]) or "None"
    
    reminder_mins = state.get_setting(chat_id, "reminder_minutes_before") or "60"
    reminders_on = "On" if state.get_setting(chat_id, "reminders_enabled") != "false" else "Off"
    myscores_on = "On" if state.get_setting(chat_id, "my_scores_enabled") != "false" else "Off"
    allscores_on = "On" if state.get_setting(chat_id, "all_scores_enabled") != "false" else "Off"
    tz_str = state.get_setting(chat_id, "timezone") or "UTC"
    
    msg = (
        "⚙️ *Settings*\n\n"
        f"👕 Favourite teams: *{_escape(fav_names)}*\n"
        f"⏰ Reminder: *{_escape(reminder_mins)} minutes before kickoff*\n"
        f"🔔 Pre\\-match reminders: *{reminders_on}*\n"
        f"⭐ My team results: *{myscores_on}*\n"
        f"🌍 All match results: *{allscores_on}*\n"
        f"🕐 Timezone: *{_escape(tz_str)}*"
    )
    await send_text(context.application, chat_id, msg)

async def syncnow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    await send_text(context.application, chat_id, "🔄 Syncing…")
    
    # Run sync in background so we don't block
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, scheduler.sync_schedule, context.application)
    
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
            
    msg = (
        "🤖 *Bot Status*\n\n"
        f"⏱ Uptime: *{_escape(uptime_str)}*\n"
        f"📡 API quota remaining: *{_escape(str(rem_quota))} / 10*\n"
        f"📅 Active scheduler jobs: *{_escape(str(active_jobs))}*\n"
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
        "• /allscores \\[on\\|off\\] — Toggle results for all matches\n\n"
        "*Match Info*\n"
        "• /today — Matches happening today\n"
        "• /live — Matches currently in progress\n"
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
    application.add_handler(CommandHandler("setreminder", setreminder_cmd))
    application.add_handler(CommandHandler("settimezone", settimezone_cmd))
    application.add_handler(CommandHandler("nextmatch", nextmatch_cmd))
    application.add_handler(CommandHandler("matches", matches_cmd))
    application.add_handler(CommandHandler("results", results_cmd))
    application.add_handler(CommandHandler("live", live_cmd))
    application.add_handler(CommandHandler("today", today_cmd))
    application.add_handler(CommandHandler("settings", settings_cmd))
    application.add_handler(CommandHandler("reset", reset_cmd))
    application.add_handler(CommandHandler("syncnow", syncnow_cmd))
    application.add_handler(CommandHandler("status", status_cmd))
    application.add_handler(CommandHandler("stats", stats_cmd))
    application.add_handler(CommandHandler("help", help_cmd))
    
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
