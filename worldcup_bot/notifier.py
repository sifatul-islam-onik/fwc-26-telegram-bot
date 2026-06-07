"""Formats and sends Telegram messages."""
import logging
from datetime import datetime
import pytz
from telegram.constants import ParseMode
from telegram.helpers import escape_markdown
from telegram.error import TelegramError

import state

logger = logging.getLogger(__name__)

def _escape(text: str) -> str:
    if text is None:
        return ""
    return escape_markdown(str(text), version=2)

def _format_stage(stage: str) -> str:
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

def _format_group(group: str | None) -> str:
    """'GROUP_A' -> 'Group A'. Returns '' if group is None."""
    if not group:
        return ""
    return group.replace("_", " ").title()

async def send_text(application, text: str) -> bool:
    """Sends a plain MarkdownV2 message to the stored chat_id."""
    chat_id = state.get_setting("telegram_chat_id")
    if not chat_id:
        logger.warning("No chat_id found. User has not run /start.")
        return False
        
    try:
        await application.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN_V2
        )
        return True
    except TelegramError as e:
        logger.error(f"Failed to send text message: {e}")
        return False

async def send_reminder(application, match: dict):
    chat_id = state.get_setting("telegram_chat_id")
    if not chat_id:
        logger.warning("No chat_id found. Cannot send reminder.")
        return

    if state.get_setting("reminders_enabled") == "false":
        return
        
    match_id = match.get("id")
    if not match_id or state.is_notified(match_id, "reminder"):
        return

    # Process times
    tz_str = state.get_setting("timezone") or "UTC"
    try:
        user_tz = pytz.timezone(tz_str)
    except pytz.exceptions.UnknownTimeZoneError:
        user_tz = pytz.UTC

    match_dt = datetime.fromisoformat(match["utcDate"].replace("Z", "+00:00"))
    local_dt = match_dt.astimezone(user_tz)
    
    minutes_before = int(state.get_setting("reminder_minutes_before") or 60)
    
    home_id = match.get("homeTeam.id")
    away_id = match.get("awayTeam.id")
    home = match.get("homeTeam.name", "TBD")
    away = match.get("awayTeam.name", "TBD")
    stage = _format_stage(match.get("stage", ""))
    group = _format_group(match.get("group"))
    
    fav_teams = state.get_favourite_teams()
    fav_names = []
    for t in fav_teams:
        if t["id"] == home_id or t["id"] == away_id:
            fav_names.append(t.get("shortName") or t.get("name") or "your team")
            
    if not fav_names:
        fav_team_short = "your team"
    else:
        fav_team_short = " and ".join(fav_names)
    
    stage_group = _escape(f"{stage} · {group}" if group else stage)
    home_esc = _escape(home)
    away_esc = _escape(away)
    mins_esc = _escape(str(minutes_before))
    time_esc = _escape(local_dt.strftime('%I:%M %p'))
    tz_esc = _escape(tz_str)
    date_esc = _escape(local_dt.strftime('%A, %d %B %Y'))
    fav_esc = _escape(fav_team_short)

    msg = (
        "⚽ *MATCH REMINDER*\n\n"
        f"🏆 FIFA World Cup · {stage_group}\n"
        f"🆚 *{home_esc}* vs *{away_esc}*\n"
        f"⏰ Kicks off in *{mins_esc} minutes\\!*\n"
        f"🕐 Local time: *{time_esc}* \\({tz_esc}\\)\n"
        f"📅 *{date_esc}*\n\n"
        f"Good luck, {fav_esc}\\! 🤞"
    )
    
    try:
        await application.bot.send_message(
            chat_id=chat_id,
            text=msg,
            parse_mode=ParseMode.MARKDOWN_V2
        )
        state.mark_notified(match_id, "reminder")
        
        # Arm result poller directly
        from scheduler import _arm_result_poller
        _arm_result_poller(application, match_id, home, away)
    except TelegramError as e:
        logger.error(f"Failed to send reminder for match {match_id}: {e}")

async def send_result(application, match: dict):
    chat_id = state.get_setting("telegram_chat_id")
    if not chat_id:
        logger.warning("No chat_id found. Cannot send result.")
        return
        
    fav_teams = state.get_favourite_teams()
    fav_ids = [t["id"] for t in fav_teams]
    
    home_id = match.get("homeTeam.id")
    away_id = match.get("awayTeam.id")
    home = match.get("homeTeam.name", "TBD")
    away = match.get("awayTeam.name", "TBD")
    
    match_involves_fav = (home_id in fav_ids or away_id in fav_ids)
    
    if match_involves_fav and state.get_setting("my_scores_enabled") == "false":
        return
    if not match_involves_fav and state.get_setting("all_scores_enabled") == "false":
        return
        
    match_id = match.get("id")
    if not match_id or state.is_notified(match_id, "result"):
        return
        
    stage = _format_stage(match.get("stage", ""))
    group = _format_group(match.get("group"))
    stage_group = _escape(f"{stage} · {group}" if group else stage)
    
    home_esc = _escape(home)
    away_esc = _escape(away)
    
    status = match.get("status")
    if status == "CANCELLED":
        msg = (
            "🏁 *FULL TIME*\n\n"
            f"🏆 FIFA World Cup · {stage_group}\n"
            f"🆚 *{home_esc}* vs *{away_esc}*\n"
            "Match cancelled ❌"
        )
    else:
        h_score = match.get("score.fullTime.home")
        a_score = match.get("score.fullTime.away")
        h_score = h_score if h_score is not None else 0
        a_score = a_score if a_score is not None else 0
        h_score_esc = _escape(str(h_score))
        a_score_esc = _escape(str(a_score))
        
        msg = (
            "🏁 *FULL TIME*\n\n"
            f"🏆 FIFA World Cup · {stage_group}\n"
            f"🆚 *{home_esc}* {h_score_esc} – {a_score_esc} *{away_esc}*"
        )
        
        if match_involves_fav:
            winner = match.get("score.winner")
            outcomes = []
            for t in fav_teams:
                t_id = t["id"]
                if t_id not in (home_id, away_id):
                    continue
                    
                t_name = _escape(t["name"])
                if winner == "HOME_TEAM" and home_id == t_id:
                    outcomes.append(f"⭐ *{t_name}: WIN 🎉*")
                elif winner == "AWAY_TEAM" and away_id == t_id:
                    outcomes.append(f"⭐ *{t_name}: WIN 🎉*")
                elif winner == "DRAW":
                    outcomes.append(f"⭐ *{t_name}: DRAW 🤝*")
                else:
                    outcomes.append(f"⭐ *{t_name}: LOSS 😔*")
                    
            if outcomes:
                msg += "\n\n" + "\n".join(outcomes)
            
    try:
        await application.bot.send_message(
            chat_id=chat_id,
            text=msg,
            parse_mode=ParseMode.MARKDOWN_V2
        )
        state.mark_notified(match_id, "result")
    except TelegramError as e:
        logger.error(f"Failed to send result for match {match_id}: {e}")
