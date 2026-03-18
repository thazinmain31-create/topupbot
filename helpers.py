import database as db
from aiogram.enums import ParseMode
from config import OWNER_ID, bot

async def is_authorized(user_id: int):
    if user_id == OWNER_ID:
        return True
    user = await db.get_reseller(str(user_id))
    return user is not None

async def notify_owner(text: str):
    try: 
        await bot.send_message(chat_id=OWNER_ID, text=text, parse_mode=ParseMode.HTML)
    except Exception as e: 
        print(f" Owner ထံသို့ Message ပို့၍မရပါ: {e}")

def generate_list(package_dict):
    lines = []
    for key, items in package_dict.items():
        total_price = sum(item['price'] for item in items)
        lines.append(f"{key:<5} : ${total_price:,.2f}")
    return "\n".join(lines)