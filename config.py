import os
import datetime
import asyncio
from collections import defaultdict
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

load_dotenv()

BOT_TOKEN = os.getenv('BOT_TOKEN')
OWNER_ID = int(os.getenv('OWNER_ID', 1318826936))
GOOGLE_EMAIL = os.getenv('GOOGLE_EMAIL')
GOOGLE_PASS = os.getenv('GOOGLE_PASS')

if not BOT_TOKEN:
    print("❌ Error: BOT_TOKEN is missing in the .env file.")
    exit()

MMT = datetime.timezone(datetime.timedelta(hours=6, minutes=30))

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Global States
IS_MAINTENANCE = False
GLOBAL_SCAMMERS = set()
user_locks = defaultdict(asyncio.Lock)
api_semaphore = asyncio.Semaphore(10)
auth_lock = asyncio.Lock()