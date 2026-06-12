"""Formats and sends Telegram messages."""
import logging
from datetime import datetime
import pytz
from telegram.constants import ParseMode
from telegram.helpers import escape_markdown
from telegram.error import TelegramError

import state
from state import _DEFAULTS as _STATE_DEFAULTS

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
        "ROUND_OF_32": "Round of 32",
        "LAST_32": "Round of 32",
        "ROUND_OF_16": "Round of 16",
        "LAST_16": "Round of 16",
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

async def send_text(application, chat_id: int, text: str, reply_markup=None) -> bool:
    """Sends a plain MarkdownV2 message to the specified chat_id with optional reply_markup."""
    try:
        await application.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=reply_markup
        )
        return True
    except TelegramError as e:
        logger.error(f"Failed to send text message to {chat_id}: {e}")
        return False

async def send_reminder(application, chat_id: int, match: dict, offset: int = None):
    if state.get_setting(chat_id, "reminders_enabled") == "false":
        return
        
    match_id = match.get("id")
    tz_str = state.get_setting(chat_id, "timezone") or "UTC"
    try:
        user_tz = pytz.timezone(tz_str)
    except pytz.exceptions.UnknownTimeZoneError:
        user_tz = pytz.UTC
 
    match_dt = datetime.fromisoformat(match["utcDate"].replace("Z", "+00:00"))
    local_dt = match_dt.astimezone(user_tz)
    
    if offset is None:
        try:
            offsets = state.get_reminder_offsets(chat_id)
            offset = offsets[0] if offsets else 60
        except Exception:
            offset = 60
    minutes_before = offset
    
    home_id = match.get("homeTeam.id")
    away_id = match.get("awayTeam.id")
    home = match.get("homeTeam.name", "TBD")
    away = match.get("awayTeam.name", "TBD")
    stage = _format_stage(match.get("stage", ""))
    group = _format_group(match.get("group"))
    
    fav_teams = state.get_favourite_teams(chat_id)
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

        # Arm result poller directly (late import avoids circular dependency)
        try:
            from scheduler import _arm_result_poller  # noqa: PLC0415
            _arm_result_poller(application, match_id, home, away)
        except ImportError:
            pass
    except TelegramError as e:
        logger.error(f"Failed to send reminder for match {match_id} to {chat_id}: {e}")

async def send_result(application, match: dict):
    match_id = match.get("id")
    if not match_id or state.is_notified(match_id, "result"):
        return
        
    home_id = match.get("homeTeam.id")
    away_id = match.get("awayTeam.id")
    home = match.get("homeTeam.name", "TBD")
    away = match.get("awayTeam.name", "TBD")
    
    stage = _format_stage(match.get("stage", ""))
    group = _format_group(match.get("group"))
    stage_group = _escape(f"{stage} · {group}" if group else stage)
    home_esc = _escape(home)
    away_esc = _escape(away)
    status = match.get("status")

    # One DB query for all users' settings instead of N×3 individual queries
    all_settings = state.get_all_user_settings()
    users = list(all_settings.keys())

    for chat_id in users:
        user_cfg = all_settings.get(chat_id, {})
        fav_teams_raw = user_cfg.get("favourite_teams", _STATE_DEFAULTS["favourite_teams"])
        try:
            import json as _json
            fav_teams = _json.loads(fav_teams_raw)
        except Exception:
            fav_teams = []
        fav_ids = [t["id"] for t in fav_teams]

        match_involves_fav = (home_id in fav_ids or away_id in fav_ids)

        my_scores = user_cfg.get("my_scores_enabled", _STATE_DEFAULTS["my_scores_enabled"])
        all_scores = user_cfg.get("all_scores_enabled", _STATE_DEFAULTS["all_scores_enabled"])

        if match_involves_fav and my_scores == "false":
            continue
        if not match_involves_fav and all_scores == "false":
            continue
            
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
        except TelegramError as e:
            logger.error(f"Failed to send result for match {match_id} to {chat_id}: {e}")
            
    # Mark global notification as complete
    state.mark_notified(match_id, "result")

async def send_goal_alert(application, match: dict, prev_home: int, prev_away: int):
    """Broadcasts a ⚽ GOAL alert to all registered users when the score changes.

    Notifies everyone regardless of favourite teams, but respects each user's
    my_scores_enabled / all_scores_enabled toggles.
    """
    match_id = match.get("id")
    home_id = match.get("homeTeam.id")
    away_id = match.get("awayTeam.id")
    home = match.get("homeTeam.name", "TBD")
    away = match.get("awayTeam.name", "TBD")

    new_home = match.get("score.fullTime.home") or 0
    new_away = match.get("score.fullTime.away") or 0

    stage = _format_stage(match.get("stage", ""))
    group = _format_group(match.get("group"))
    stage_group = _escape(f"{stage} · {group}" if group else stage)
    home_esc = _escape(home)
    away_esc = _escape(away)
    h_score_esc = _escape(str(new_home))
    a_score_esc = _escape(str(new_away))

    # Determine who scored
    scorer_line = ""
    if new_home > prev_home:
        goals = new_home - prev_home
        scorer_line = f"\n⚽ *{home_esc}* scored\\!"
        if goals > 1:
            scorer_line += f" \\(\\+{_escape(str(goals))}\\)"
    elif new_away > prev_away:
        goals = new_away - prev_away
        scorer_line = f"\n⚽ *{away_esc}* scored\\!"
        if goals > 1:
            scorer_line += f" \\(\\+{_escape(str(goals))}\\)"

    msg = (
        "🚨 *GOAL\\!*\n\n"
        f"🏆 FIFA World Cup · {stage_group}\n"
        f"🆚 *{home_esc}* {h_score_esc} – {a_score_esc} *{away_esc}*"
        f"{scorer_line}"
    )

    # One DB query for all users' settings instead of N×4 individual queries
    all_settings = state.get_all_user_settings()
    users = list(all_settings.keys())

    for chat_id in users:
        user_cfg = all_settings.get(chat_id, {})

        # Respect the live goal alert toggle first
        live_goals = user_cfg.get("live_goals_enabled", _STATE_DEFAULTS["live_goals_enabled"])
        if live_goals == "false":
            continue

        fav_teams_raw = user_cfg.get("favourite_teams", _STATE_DEFAULTS["favourite_teams"])
        try:
            import json as _json
            fav_teams = _json.loads(fav_teams_raw)
        except Exception:
            fav_teams = []
        fav_ids = [t["id"] for t in fav_teams]
        match_involves_fav = (home_id in fav_ids or away_id in fav_ids)

        my_scores = user_cfg.get("my_scores_enabled", _STATE_DEFAULTS["my_scores_enabled"])
        all_scores = user_cfg.get("all_scores_enabled", _STATE_DEFAULTS["all_scores_enabled"])

        # Respect notification toggles
        if match_involves_fav and my_scores == "false":
            continue
        if not match_involves_fav and all_scores == "false":
            continue

        try:
            await application.bot.send_message(
                chat_id=chat_id,
                text=msg,
                parse_mode="MarkdownV2"
            )
        except TelegramError as e:
            logger.error(f"Failed to send goal alert for match {match_id} to {chat_id}: {e}")

