"""SQLite persistence layer for settings and notification tracking."""
import sqlite3
import threading
import json

DB_PATH = "worldcup_bot.db"
_lock = threading.Lock()

def _get_connection():
    """Returns a SQLite connection with dict factory."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

_DEFAULTS = {
    "reminders_enabled": "true",
    "my_scores_enabled": "true",
    "all_scores_enabled": "true",
    "live_goals_enabled": "true",
    "reminder_minutes_before": "60",
    "timezone": "UTC",
    "favourite_teams": "[]"
}

def _init_db():
    """Creates the necessary tables if they do not exist."""
    with _lock:
        with _get_connection() as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS user_settings (
                    chat_id INTEGER,
                    key   TEXT,
                    value TEXT,
                    PRIMARY KEY (chat_id, key)
                )
            ''')
            
            # Migrate old settings if present
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='settings'")
            if cursor.fetchone():
                cursor = conn.execute("SELECT value FROM settings WHERE key = 'telegram_chat_id'")
                row = cursor.fetchone()
                if row:
                    old_chat_id = int(row["value"])
                    cursor = conn.execute("SELECT key, value FROM settings WHERE key != 'telegram_chat_id'")
                    for setting in cursor.fetchall():
                        conn.execute(
                            "INSERT OR IGNORE INTO user_settings (chat_id, key, value) VALUES (?, ?, ?)",
                            (old_chat_id, setting["key"], setting["value"])
                        )
                conn.execute("DROP TABLE settings")

            conn.execute('''
                CREATE TABLE IF NOT EXISTS notified_matches (
                    match_id    INTEGER  NOT NULL,
                    notif_type  TEXT     NOT NULL,
                    notified_at TEXT     NOT NULL,
                    PRIMARY KEY (match_id, notif_type)
                )
            ''')
            conn.commit()

def get_setting(chat_id: int, key: str) -> str | None:
    """Returns the stored value or the default if not set."""
    with _lock:
        with _get_connection() as conn:
            cursor = conn.execute("SELECT value FROM user_settings WHERE chat_id = ? AND key = ?", (chat_id, key))
            row = cursor.fetchone()
            if row:
                return row["value"]
            return _DEFAULTS.get(key)

def set_setting(chat_id: int, key: str, value: str) -> None:
    """Inserts or replaces the key-value pair for a user."""
    with _lock:
        with _get_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO user_settings (chat_id, key, value) VALUES (?, ?, ?)",
                (chat_id, key, str(value))
            )
            conn.commit()

def get_all_users() -> list[int]:
    """Returns a list of all unique chat_ids in the system."""
    with _lock:
        with _get_connection() as conn:
            cursor = conn.execute("SELECT DISTINCT chat_id FROM user_settings")
            return [row["chat_id"] for row in cursor.fetchall()]

def is_notified(match_id: int, notif_type: str) -> bool:
    """Returns True if a row exists for (match_id, notif_type)."""
    with _lock:
        with _get_connection() as conn:
            cursor = conn.execute(
                "SELECT 1 FROM notified_matches WHERE match_id = ? AND notif_type = ?",
                (match_id, notif_type)
            )
            return cursor.fetchone() is not None

def mark_notified(match_id: int, notif_type: str) -> None:
    """Inserts the row. Ignores if already present."""
    import datetime
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with _lock:
        with _get_connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO notified_matches (match_id, notif_type, notified_at) VALUES (?, ?, ?)",
                (match_id, notif_type, now_iso)
            )
            conn.commit()

def get_favourite_teams(chat_id: int) -> list[dict]:
    """Returns a list of favorite teams, e.g., [{"id": 12, "name": "Brazil", "shortName": "BRA"}]."""
    val = get_setting(chat_id, "favourite_teams")
    if val:
        try:
            return json.loads(val)
        except json.JSONDecodeError:
            pass
    return []

def add_favourite_team(chat_id: int, team: dict) -> bool:
    """Adds a team to favorites if not already present. Returns True if added."""
    teams = get_favourite_teams(chat_id)
    for t in teams:
        if t["id"] == team["id"]:
            return False
    teams.append({"id": team["id"], "name": team["name"], "shortName": team.get("shortName")})
    set_setting(chat_id, "favourite_teams", json.dumps(teams))
    return True

def remove_favourite_team(chat_id: int, team_id: int) -> bool:
    """Removes a team from favorites by ID. Returns True if removed."""
    teams = get_favourite_teams(chat_id)
    new_teams = [t for t in teams if t["id"] != team_id]
    if len(new_teams) == len(teams):
        return False
    set_setting(chat_id, "favourite_teams", json.dumps(new_teams))
    return True

def reset_settings(chat_id: int) -> None:
    """Resets all user settings by deleting their entries, falling back to defaults."""
    with _lock:
        with _get_connection() as conn:
            conn.execute("DELETE FROM user_settings WHERE chat_id = ?", (chat_id,))
            conn.commit()
