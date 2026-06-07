"""Configuration module for loading environment variables."""
import os
import sys
from dotenv import load_dotenv

load_dotenv()

FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

if not FOOTBALL_API_KEY:
    print("ERROR: FOOTBALL_API_KEY is not set in .env or environment variables.", file=sys.stderr)
    sys.exit(1)

if not TELEGRAM_BOT_TOKEN:
    print("ERROR: TELEGRAM_BOT_TOKEN is not set in .env or environment variables.", file=sys.stderr)
    sys.exit(1)
