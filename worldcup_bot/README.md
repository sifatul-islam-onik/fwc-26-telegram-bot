# World Cup Telegram Bot

A Telegram bot that provides comprehensive coverage for the FIFA World Cup, offering personalized reminders, automated results tracking, and live match updates.

## Prerequisites
- Python 3.11 or higher
- An API key from [football-data.org](https://www.football-data.org/)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

## Setup

1. **Clone the repository and install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure your environment:**
   Copy `.env.example` to `.env` and fill in your keys:
   ```bash
   cp .env.example .env
   ```
   Edit the `.env` file to include:
   - `FOOTBALL_API_KEY`: Your key from football-data.org.
   - `TELEGRAM_BOT_TOKEN`: Your token from @BotFather.

3. **Start the bot:**
   ```bash
   python bot.py
   ```

## First-Run Instructions

1. Go to Telegram and start a chat with your bot.
2. Send `/start` to register your chat session.
3. Send `/setteam <team name>` (e.g., `/setteam Brazil`) to select your favourite team.
   - *Note*: The bot uses the "WC" competition code under the hood. Only teams qualified for the current/most recent World Cup will match.
4. Enjoy automated match reminders and result notifications!

## Command Reference

### Configuration
| Command | Description |
|---------|-------------|
| `/addteam <team>` | Search and add a team to your favourites (e.g., `/addteam Brazil`) |
| `/removeteam <team>` | Remove a team from your favourites |
| `/teams` | View your list of favourite teams |
| `/settimezone <tz>` | Set your local timezone (e.g., `Asia/Dhaka`) |
| `/settings` | View your current configuration |
| `/reset` | Reset all settings to defaults |

### Notifications
| Command | Description |
|---------|-------------|
| `/reminders [on\|off]` | Toggle pre-match reminders for your team |
| `/setreminder <min>` | Set how many minutes before kickoff to remind |
| `/myscores [on\|off]` | Toggle post-match results for your team |
| `/allscores [on\|off]` | Toggle post-match results for ALL WC matches |

### Match Info
| Command | Description |
|---------|-------------|
| `/today` | Show matches happening today (upcoming, live, finished) |
| `/live` | Show matches currently in progress |
| `/nextmatch` | Show your team's next upcoming match |
| `/matches` | Show your team's full match schedule |
| `/results [date]` | Show finished matches (defaults to today; can use `yesterday` or `YYYY-MM-DD`) |

### System
| Command | Description |
|---------|-------------|
| `/syncnow` | Manually force a schedule synchronization |
| `/status` | View bot runtime diagnostics and API quota |
| `/help` | Show the in-chat command reference |

## Note on API Limits

This bot uses the free tier of the football-data.org API, which permits up to 10 requests per minute.
The bot handles rate limiting automatically by sleeping between requests. When doing daily background schedule syncs, it may pause occasionally to ensure it remains well within the limit. For typical usage, you will not hit this limit.
