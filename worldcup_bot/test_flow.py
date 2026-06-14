"""Unit and integration tests for the World Cup Telegram Bot.

This test file simulates the complete user notification lifecycle:
1. Setting favorite teams.
2. Receiving pre-match reminders.
3. Receiving live goal score alerts for any team.
4. Receiving full-time results.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch
import asyncio

# Ensure environment variables are populated before importing configuration
os.environ.setdefault("FOOTBALL_API_KEY", "mock_key")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "mock_token")

# Add the parent directory of this test file to python path to ensure imports work correctly
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import state
import football_api
import notifier
import scheduler

# Mock match data that can be altered during the tests to simulate match progression
MOCK_MATCHES = []
MOCK_TEAMS = [
    {"id": 12, "name": "Brazil", "shortName": "BRA", "crest": "http://crest.url/bra.png"},
    {"id": 13, "name": "Argentina", "shortName": "ARG", "crest": "http://crest.url/arg.png"},
    {"id": 14, "name": "Germany", "shortName": "GER", "crest": "http://crest.url/ger.png"}
]

def mock_get_all_wc_matches(bypass_cache=False):
    return MOCK_MATCHES

def mock_get_match(match_id):
    for m in MOCK_MATCHES:
        if m["id"] == match_id:
            return m
    return None

def mock_get_all_teams():
    return MOCK_TEAMS

# Mock Scheduler structure
class MockJob:
    def __init__(self, func, trigger, args=None, id=None):
        self.func = func
        self.trigger = trigger
        self.args = args or []
        self.id = id
        # DateTrigger sets run_date
        self.next_run_time = getattr(trigger, "run_date", None)

class MockScheduler:
    def __init__(self):
        self.jobs = {}
        self.running = False

    def start(self):
        self.running = True

    def shutdown(self, wait=True):
        self.running = False

    def add_job(self, func, trigger=None, args=None, id=None, replace_existing=False):
        job = MockJob(func, trigger, args, id)
        self.jobs[id] = job
        return job

    def get_jobs(self):
        return list(self.jobs.values())

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def remove_job(self, job_id):
        if job_id in self.jobs:
            del self.jobs[job_id]


# Mock Telegram Bot / Application
class MockBot:
    def __init__(self):
        self.sent_messages = []

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent_messages.append({
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "reply_markup": reply_markup
        })


class MockApplication:
    def __init__(self):
        self.bot = MockBot()
        self.bot_data = {"main_loop": None}


class TestBotNotificationFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Point to a temporary test DB file
        cls.test_db_path = "test_worldcup_bot.db"
        state.DB_PATH = cls.test_db_path
        state._init_db()

    @classmethod
    def tearDownClass(cls):
        # Cleanup temporary database file
        if os.path.exists(cls.test_db_path):
            try:
                os.remove(cls.test_db_path)
            except Exception as e:
                print(f"Error removing test database file: {e}")

    def setUp(self):
        global MOCK_MATCHES
        MOCK_MATCHES.clear()
        
        # Clear database tables between test cases
        with state._get_connection() as conn:
            conn.execute("DELETE FROM user_settings")
            conn.execute("DELETE FROM notified_matches")
            conn.commit()

        # Initialize mock components
        self.app = MockApplication()
        self.mock_scheduler = MockScheduler()
        scheduler._scheduler = self.mock_scheduler
        
        # Force scheduler._run_async to call loop synchronously
        scheduler._run_async = lambda app, coro: asyncio.run(coro)

        # Clear active score cache
        scheduler._live_score_cache.clear()

        # Setup patches
        self.patchers = [
            patch("football_api.get_all_wc_matches", side_effect=mock_get_all_wc_matches),
            patch("football_api.get_match", side_effect=mock_get_match),
            patch("football_api.get_all_teams", side_effect=mock_get_all_teams)
        ]
        for p in self.patchers:
            p.start()

    def tearDown(self):
        for p in self.patchers:
            p.stop()

    def test_complete_notification_flow(self):
        chat_id = 99999
        
        # --- 1. USER SETUP & CONFIGURATION ---
        # Add Brazil (ID 12) as the user's favorite team
        added = state.add_favourite_team(chat_id, {"id": 12, "name": "Brazil", "shortName": "BRA"})
        self.assertTrue(added)
        
        # Verify settings defaults are active
        self.assertEqual(state.get_setting(chat_id, "reminders_enabled"), "true")
        self.assertEqual(state.get_setting(chat_id, "my_scores_enabled"), "true")
        self.assertEqual(state.get_setting(chat_id, "all_scores_enabled"), "true")
        self.assertEqual(state.get_setting(chat_id, "live_goals_enabled"), "true")
        
        # --- 2. PRE-MATCH REMINDER ---
        # Mock an upcoming match for Brazil kickoff in 1 hour
        # Date is formatted to match isoformat
        match_id = 1001
        upcoming_match = {
            "id": match_id,
            "utcDate": "2026-06-15T18:00:00Z",
            "status": "SCHEDULED",
            "stage": "GROUP_STAGE",
            "group": "GROUP_A",
            "homeTeam.id": 12,
            "homeTeam.name": "Brazil",
            "awayTeam.id": 13,
            "awayTeam.name": "Argentina",
            "score.fullTime.home": None,
            "score.fullTime.away": None,
            "score.winner": None
        }
        MOCK_MATCHES.append(upcoming_match)

        # Sync the scheduler jobs based on database state and current upcoming matches
        # scheduler.sync_schedule will find user tracking Brazil, mock match for Brazil, and add remind job
        scheduler.sync_schedule(self.app)

        # Check that a reminder job was registered
        reminder_job_id = f"remind_{chat_id}_{match_id}_60"
        job = self.mock_scheduler.get_job(reminder_job_id)
        self.assertIsNotNone(job, f"Reminder job {reminder_job_id} should be scheduled")

        # Manually run the reminder function to simulate reminder triggering
        # job.func triggers notifier.send_reminder (wrapped)
        job.func()

        # Assert that the reminder message was sent
        sent = self.app.bot.sent_messages
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["chat_id"], chat_id)
        self.assertIn("MATCH REMINDER", sent[0]["text"])
        self.assertIn("Brazil", sent[0]["text"])
        self.assertIn("Argentina", sent[0]["text"])
        self.assertIn("60 minutes", sent[0]["text"])

        # --- 3. LIVE MATCH GOAL ALERTS ---
        # Update match status to IN_PLAY
        upcoming_match["status"] = "IN_PLAY"
        upcoming_match["score.fullTime.home"] = 0
        upcoming_match["score.fullTime.away"] = 0

        # Simulate first poller run to seed score cache (should not alert yet)
        scheduler._poll_live_goals(self.app)
        self.assertEqual(len(sent), 1)  # No new message

        # Now, home team (Brazil) scores a goal (0-0 -> 1-0)
        upcoming_match["score.fullTime.home"] = 1
        scheduler._poll_live_goals(self.app)
        
        # Verify goal alert was generated
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[1]["chat_id"], chat_id)
        self.assertIn("GOAL", sent[1]["text"])
        self.assertIn("Brazil", sent[1]["text"])
        self.assertIn("Argentina", sent[1]["text"])
        self.assertIn("1 – 0", sent[1]["text"])
        self.assertIn("scored", sent[1]["text"])

        # Now, away team (Argentina) scores a goal (1-0 -> 1-1)
        upcoming_match["score.fullTime.away"] = 1
        scheduler._poll_live_goals(self.app)

        # Verify goal alert was generated for the opponent too
        self.assertEqual(len(sent), 3)
        self.assertEqual(sent[2]["chat_id"], chat_id)
        self.assertIn("GOAL", sent[2]["text"])
        self.assertIn("Brazil", sent[2]["text"])
        self.assertIn("Argentina", sent[2]["text"])
        self.assertIn("1 – 1", sent[2]["text"])
        self.assertIn("scored", sent[2]["text"])

        # --- 4. FULL-TIME RESULTS ---
        # Match ends. Brazil wins 2-1 (home scores again)
        upcoming_match["score.fullTime.home"] = 2
        # Verify goal alert for the winner goal
        scheduler._poll_live_goals(self.app)
        self.assertEqual(len(sent), 4)
        self.assertIn("scored", sent[3]["text"])

        # Transition match to FINISHED
        upcoming_match["status"] = "FINISHED"
        upcoming_match["score.winner"] = "HOME_TEAM"

        # Poll again. Since it's in the score cache but no longer live (live_matches list doesn't include it),
        # the poller should detect it finished, post result, and remove from cache.
        scheduler._poll_live_goals(self.app)

        # Check final score result notification was sent
        self.assertEqual(len(sent), 5)
        self.assertEqual(sent[4]["chat_id"], chat_id)
        self.assertIn("FULL TIME", sent[4]["text"])
        self.assertIn("Brazil", sent[4]["text"])
        self.assertIn("Argentina", sent[4]["text"])
        self.assertIn("2 – 1", sent[4]["text"])
        self.assertIn("Brazil: WIN", sent[4]["text"])

        # Verify that the match is marked as notified in DB
        self.assertTrue(state.is_notified(match_id, "result"))

        # Verify scheduler cleaned up the score cache entry
        self.assertNotIn(match_id, scheduler._live_score_cache)


if __name__ == "__main__":
    unittest.main()
