import os
from dotenv import load_dotenv

load_dotenv()

# Telegram Bot Token
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

# Admin Telegram IDs for notifications
_admin_env = os.getenv("ADMIN_IDS", os.getenv("ADMIN_ID", ""))
ADMIN_IDS = [x.strip() for x in _admin_env.split(",")] if _admin_env else []

# 1C Integration URL
ONEC_API_URL = os.getenv("ONEC_API_URL", "http://your-1c-server.local/api/doctor_finance")

# Database URL
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///doctors.db")

# Scheduler Config (For example: run on the 1st day of every month at 10:00)
# Use '*' to disable constraints (e.g., test every minute)
SCHEDULER_DAY = os.getenv("SCHEDULER_DAY", "1")
SCHEDULER_HOUR = os.getenv("SCHEDULER_HOUR", "10")
SCHEDULER_MINUTE = os.getenv("SCHEDULER_MINUTE", "0")

# Flask Server Port
PORT = int(os.getenv("PORT", 5001))
