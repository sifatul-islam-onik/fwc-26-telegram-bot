# ⚽ FIFA World Cup 2026 Telegram Bot

A fully-featured Telegram bot that keeps you up-to-date with every match of the **FIFA World Cup 2026** — from pre-match reminders and live goal alerts, to full-time results and the complete tournament fixture.

## 🟢 Live Demo

> **The bot is live and ready to use — no setup required!**
>
> 👉 **[t.me/SIFFWC26BOT](https://t.me/SIFFWC26BOT)**
>
> Open Telegram, start a chat with **@SIFFWC26BOT**, send `/start`, and you're in.

---


## ✨ Features

| Feature | Details |
|---|---|
| 📱 Interactive Control Panel | Access a dashboard `/menu` with buttons to manage settings, teams, and schedules |
| 🔔 Pre-match reminders | Get notified *N* minutes before your favourite team kicks off |
| ⚽ Live goal alerts | Real-time goal notifications (polls every **10 seconds** during active matches) |
| 🏁 Full-time results | Automatic result push for every World Cup match |
| 📟 Monospace ASCII Cards | Matches, schedules, and live alerts rendered inside sleek, mobile-optimized ASCII cards |
| 📋 Full fixture | Browse the complete tournament schedule, grouped by stage & group |
| 📅 Today / Tomorrow | Quick view of the day's matches with live/upcoming/finished status |
| 🔴 Live scores | See all currently in-progress matches with scores |
| 📊 Results by date | Look up finished matches for today, yesterday, or any date |
| 👕 Multiple favourites | Track as many teams as you want (with checkboxes/checkmark lists) |
| 🌍 Timezone-aware | All times shown in your configured local timezone |
| ⚙️ Granular toggles | Independently enable/disable reminders, goal alerts, and result notifications |

---

## 🛠 Prerequisites

- **Python 3.10+**
- A free API key from [football-data.org](https://www.football-data.org/) (free tier: 10 req/min)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

---

## 🚀 Setup

### 1 — Clone & install dependencies

```bash
git clone https://github.com/sifatul-islam-onik/fwc-26-telegram-bot.git
cd fwc-26-telegram-bot/worldcup_bot
pip install -r requirements.txt
```

### 2 — Configure environment variables

```bash
cp .env.example .env
```

Open `.env` and fill in your credentials:

```env
# API key from https://www.football-data.org/
FOOTBALL_API_KEY=your_football_data_org_key

# Telegram bot token from @BotFather
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
```

### 3 — Run the bot

```bash
python bot.py
```

You should see:

```
[INFO] — scheduler — Live-goal poller armed (10-second interval).
[INFO] — __main__  — Bot started.
[INFO] — scheduler — Sync complete. Reminders: X. Future matches queued: Y.
```

> **Note:** Only one instance of the bot should run at a time. Starting a second instance while one is already running will cause a `Conflict` error.

---

## 🤖 First-Time Usage

1. Open Telegram and start a chat with your bot.
2. Send `/start` — the bot will register your session.
3. Type `/menu` to open your interactive dashboard.
4. Go to **My Teams** -> **Add Team** to search and choose your favorite teams.
5. Go to **Settings & Preferences** -> **Set Timezone** to search and select your city timezone.
6. That's it — you'll receive automatic reminders and notifications!

---

## 📖 Command Reference

> 💡 **Recommended:** Just type `/menu` to access all configurations, matches, and status indicators using clean, interactive buttons rather than typing slash commands manually.

### ⚙️ Configuration

| Command | Description |
|---|---|
| `/addteam <name>` | Add a team to your favourites (e.g. `/addteam Brazil`) |
| `/removeteam <name>` | Remove a team from your favourites |
| `/teams` | List your current favourite teams |
| `/settimezone <tz>` | Set your timezone using IANA format (e.g. `Asia/Dhaka`, `Europe/London`) |
| `/settings` | View all your current settings at a glance |
| `/reset` | Reset all settings back to their defaults |

### 🔔 Notifications

| Command | Description |
|---|---|
| `/reminders [on\|off]` | Toggle pre-match kickoff reminders |
| `/setreminder <minutes>` | Set how many minutes before kickoff to be reminded (1–1440) |
| `/myscores [on\|off]` | Toggle full-time result notifications for your favourite teams |
| `/allscores [on\|off]` | Toggle full-time result notifications for **all** World Cup matches |
| `/livegoals [on\|off]` | Toggle live in-match goal alerts |

> Running any toggle command without `on` or `off` shows its current status.

### 📅 Match Info

| Command | Description |
|---|---|
| `/today` | Today's matches split into Live, Upcoming, and Finished sections |
| `/tomorrow` | Tomorrow's scheduled matches |
| `/live` | All matches currently in progress |
| `/fixture` | Full tournament fixture (all stages, grouped by stage & group) |
| `/nextmatch` | Your teams' next upcoming match |
| `/matches` | Your teams' full remaining schedule |
| `/results [date]` | Finished matches for a date — `today`, `yesterday`, or `YYYY-MM-DD` |

### 🔧 System

| Command | Description |
|---|---|
| `/syncnow` | Force an immediate schedule sync (useful after adding a team) |
| `/status` | Bot diagnostics — uptime, API quota, active jobs, live poller state |
| `/stats` | Number of unique users tracked by the bot |
| `/help` | In-chat command reference |

---

## 🏗 Project Structure

```
fwc-26-telegram-bot/
└── worldcup_bot/
    ├── bot.py           # Command handlers & entry point
    ├── scheduler.py     # APScheduler jobs (reminders, result pollers, live goal poller)
    ├── notifier.py      # Message formatting & Telegram delivery
    ├── football_api.py  # football-data.org API client
    ├── state.py         # SQLite persistence (settings, notified matches)
    ├── config.py        # Loads .env variables
    ├── migrate.py       # DB migration helper
    ├── requirements.txt
    ├── .env.example
    └── README.md
```

---

## ⚙️ How It Works

### Scheduler Overview

The bot uses **APScheduler** (background thread) alongside the async Telegram polling loop.

| Job | Interval | Purpose |
|---|---|---|
| `sync_schedule` | Every 2 hours | Re-reads all fixtures; arms reminders & result pollers |
| `remind_{match_id}` | One-shot (at kickoff − N min) | Sends pre-match reminder to user |
| `startpoll_{match_id}` | One-shot (at kickoff) | Arms the result poller when match time arrives |
| `poll_{match_id}` | Every 3 minutes | Polls match status until FINISHED; sends result |
| `live_poller` | Every **10 seconds** | Detects score changes and transitions to finished status; sends goal alerts and final results |

### Live Goal & Match End Detection

The `live_poller` job runs only while matches are active:

1. Calls `get_all_wc_matches(bypass_cache=True)` — a single API call that fetches all fixtures and processes them locally to save API quota.
2. Compares each active match's score against an in-memory cache (`_live_score_cache`) to detect and broadcast goals in real-time.
3. Identifies when matches transition from live to ended (when a cached match is no longer in the active matches list).
4. For any transitioned match, if its status is finished (`FINISHED`, `AWARDED`, `CANCELLED`), it immediately dispatches the final results notification to users (respecting settings) and cleans up its associated `poll_{match_id}` and `startpoll_{match_id}` scheduler jobs.
5. When no live matches remain, the job **removes itself** to save API quota.
6. It is automatically re-armed when the next match goes live.

### API Rate Limiting

The free tier allows **10 requests per minute**. The bot manages this by:

- Adding a 0.7 s delay between every API request.
- Checking the `X-RequestsAvailable` response header.
- Catching 429 responses and gracefully skipping that poll cycle.
- **In-Memory Caching**: Caches tournament match data in memory for 5 minutes (`get_all_wc_matches`) and resolves individual team schedules locally, avoiding redundant network queries and automatically falling back to cached schedules if the API is rate-limited.
- The `live_poller` makes **1 call per 10 seconds** (6/min), bypassing the cache (via `bypass_cache=True`) to ensure real-time score delivery while leaving headroom for other pollers.

---

## 🗄 Data Storage

Settings and notification state are persisted in a local **SQLite** database (`worldcup_bot.db`).

| Table | Purpose |
|---|---|
| `user_settings` | Per-user key/value settings (timezone, toggles, favourite teams) |
| `notified_matches` | Tracks which matches have had their result notification sent |

Default settings for new users:

| Setting | Default |
|---|---|
| `reminders_enabled` | `true` |
| `reminder_minutes_before` | `60` |
| `my_scores_enabled` | `true` |
| `all_scores_enabled` | `true` |
| `live_goals_enabled` | `true` |
| `timezone` | `UTC` |

---

## 📦 Dependencies

| Package | Version | Purpose |
|---|---|---|
| `python-telegram-bot` | 21.x | Telegram Bot API wrapper |
| `APScheduler` | 3.x | Background job scheduling |
| `requests` | ≥ 2.31 | HTTP client for football API |
| `python-dotenv` | ≥ 1.0 | `.env` file loading |
| `pytz` | ≥ 2024.1 | Timezone handling |

---

## 📄 License

This project is licensed under the [MIT License](LICENSE).
