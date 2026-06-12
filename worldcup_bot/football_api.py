"""Client for football-data.org API v4."""
import time
import logging
import requests
from requests.exceptions import RequestException

from config import FOOTBALL_API_KEY

logger = logging.getLogger(__name__)

BASE_URL = "https://api.football-data.org/v4"
COMPETITION_CODE = "WC"

class RateLimitException(Exception):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__(f"Rate limited. Retry after {retry_after}s.")

session = requests.Session()
session.headers.update({"X-Auth-Token": FOOTBALL_API_KEY})

_requests_remaining = 10

def _safe_get(url: str, params: dict = None) -> dict | None:
    """Helper to safely make API requests with rate limit handling."""
    global _requests_remaining
    
    # Pre-request delay to help avoid hitting the 10 req/min limit
    time.sleep(0.7)
    
    try:
        response = session.get(url, params=params)
        
        # Update remaining requests if header exists
        rem = response.headers.get("X-RequestsAvailable")
        if rem is not None:
            _requests_remaining = int(rem)
            
        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", 60))
            logger.warning(f"Rate limited (429). Retry-After: {retry_after}s.")
            raise RateLimitException(retry_after)
            
        elif response.status_code != 200:
            logger.error(f"HTTP {response.status_code} on {url}: {response.text}")
            return None
            
        return response.json()
        
    except RequestException as e:
        logger.error(f"Network error on {url}: {e}")
        return None

def _normalise(raw: dict) -> dict:
    """Extracts and maps fields safely with None fallbacks."""
    home_team = raw.get("homeTeam") or {}
    away_team = raw.get("awayTeam") or {}
    score_dict = raw.get("score") or {}
    full_time = score_dict.get("fullTime") or {}
    
    return {
        "id": raw.get("id"),
        "utcDate": raw.get("utcDate"),
        "status": raw.get("status"),
        "stage": raw.get("stage"),
        "group": raw.get("group"),
        "homeTeam.id": home_team.get("id"),
        "homeTeam.name": home_team.get("name"),
        "awayTeam.id": away_team.get("id"),
        "awayTeam.name": away_team.get("name"),
        "score.fullTime.home": full_time.get("home"),
        "score.fullTime.away": full_time.get("away"),
        "score.winner": score_dict.get("winner")
    }

def search_team(query: str) -> dict | None:
    """Searches for a team by name or shortName in the WC competition."""
    url = f"{BASE_URL}/competitions/{COMPETITION_CODE}/teams"
    data = _safe_get(url)
    if not data or "teams" not in data:
        return None
        
    query_lower = query.lower()
    matches = []
    
    for team in data["teams"]:
        name = (team.get("name") or "").lower()
        short_name = (team.get("shortName") or "").lower()
        
        if query_lower in name or query_lower in short_name:
            matches.append({
                "id": team.get("id"),
                "name": team.get("name"),
                "shortName": team.get("shortName"),
                "crest": team.get("crest")
            })
            
    if len(matches) > 1:
        raise ValueError("Multiple teams matched")
    elif len(matches) == 1:
        return matches[0]
        
    return None

def get_team_matches(team_id: int) -> list[dict]:
    """Fetches all matches for a specific team in the WC competition."""
    url = f"{BASE_URL}/competitions/{COMPETITION_CODE}/matches"
    data = _safe_get(url)
    if not data or "matches" not in data:
        return []
        
    matches = []
    for match in data["matches"]:
        home_team = match.get("homeTeam") or {}
        away_team = match.get("awayTeam") or {}
        home_id = home_team.get("id")
        away_id = away_team.get("id")
        
        if home_id == team_id or away_id == team_id:
            matches.append(_normalise(match))
            
    return matches

def get_all_wc_matches() -> list[dict]:
    """Returns ALL matches in the tournament."""
    url = f"{BASE_URL}/competitions/{COMPETITION_CODE}/matches"
    data = _safe_get(url)
    if not data or "matches" not in data:
        return []
        
    return [_normalise(m) for m in data["matches"]]

def get_live_matches() -> list[dict]:
    """Returns only matches that are currently live or probably live.

    Uses one API call (all WC matches) and filters locally.
    'Probably live' covers the free-tier lag where status stays TIMED/SCHEDULED
    even after kickoff — any match whose kickoff was 0-130 minutes ago and
    hasn't been marked FINISHED/AWARDED/CANCELLED/POSTPONED.
    """
    import time as _time
    from datetime import datetime as _dt, timezone as _tz

    LIVE_STATUSES = ("IN_PLAY", "PAUSED", "EXTRA_TIME", "PENALTY_SHOOTOUT")
    FINISHED_STATUSES = ("FINISHED", "AWARDED", "CANCELLED", "POSTPONED")

    all_matches = get_all_wc_matches()   # single network call
    now_utc = _dt.now(_tz.utc)
    live = []

    for m in all_matches:
        status = m.get("status")
        if status in LIVE_STATUSES:
            live.append(m)
        elif status not in FINISHED_STATUSES:
            utc_date_str = m.get("utcDate")
            if utc_date_str:
                match_dt = _dt.fromisoformat(utc_date_str.replace("Z", "+00:00"))
                elapsed = (now_utc - match_dt).total_seconds()
                if 0 <= elapsed <= 130 * 60:
                    live.append(m)

    return live

def get_match(match_id: int) -> dict | None:
    """Returns a single match object."""
    url = f"{BASE_URL}/matches/{match_id}"
    data = _safe_get(url)
    if not data:
        return None
        
    return _normalise(data)
