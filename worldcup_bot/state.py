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

def _init_db():
    """Creates the necessary tables if they do not exist."""
    with _lock:
        with _get_connection() as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS settings (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS notified_matches (
                    match_id    INTEGER  NOT NULL,
                    notif_type  TEXT     NOT NULL,
                    notified_at TEXT     NOT NULL,
                    PRIMARY KEY (match_id, notif_type)
                )
            ''')
            
            # Set defaults if not present
            defaults = {
                "reminders_enabled": "true",
                "my_scores_enabled": "true",
                "all_scores_enabled": "true",
                "reminder_minutes_before": "60",
                "timezone": "UTC",
                "favourite_teams": "[]"
            }
            for k, v in defaults.items():
                conn.execute(
                    "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                    (k, v)
                )
            conn.commit()

def get_setting(key: str) -> str | None:
    """Returns the stored value or None if the key has never been set."""
    with _lock:
        with _get_connection() as conn:
            cursor = conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = cursor.fetchone()
            return row["value"] if row else None

def set_setting(key: str, value: str) -> None:
    """Inserts or replaces the key-value pair."""
    with _lock:
        with _get_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, str(value))
            )
            conn.commit()

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

def get_favourite_teams() -> list[dict]:
    """Returns a list of favorite teams, e.g., [{"id": 12, "name": "Brazil", "shortName": "BRA"}]."""
    val = get_setting("favourite_teams")
    if val:
        try:
            return json.loads(val)
        except json.JSONDecodeError:
            pass
    return []

def add_favourite_team(team: dict) -> bool:
    """Adds a team to favorites if not already present. Returns True if added."""
    teams = get_favourite_teams()
    for t in teams:
        if t["id"] == team["id"]:
            return False
    teams.append({"id": team["id"], "name": team["name"], "shortName": team.get("shortName")})
    set_setting("favourite_teams", json.dumps(teams))
    return True

def remove_favourite_team(team_id: int) -> bool:
    """Removes a team from favorites by ID. Returns True if removed."""
    teams = get_favourite_teams()
    new_teams = [t for t in teams if t["id"] != team_id]
    if len(new_teams) == len(teams):
        return False
    set_setting("favourite_teams", json.dumps(new_teams))
    return True

def reset_settings() -> None:
    """Resets all user settings to their default values, keeping internal keys like telegram_chat_id."""
    defaults = {
        "reminders_enabled": "true",
        "my_scores_enabled": "true",
        "all_scores_enabled": "true",
        "reminder_minutes_before": "60",
        "timezone": "UTC",
        "favourite_teams": "[]"
    }
    with _lock:
        with _get_connection() as conn:
            for k, v in defaults.items():
                conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                    (k, v)
                )
            conn.commit()
