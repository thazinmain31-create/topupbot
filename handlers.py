import io
import re
import datetime
import time
import random
import asyncio
import html
import json

from bs4 import BeautifulSoup
from aiogram import F, types
from aiogram.filters import Command, or_f
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from curl_cffi.requests import AsyncSession

import database as db
import config
from config import dp, bot, OWNER_ID, MMT, user_locks, api_semaphore
from packages import DOUBLE_DIAMOND_PACKAGES, BR_PACKAGES, PH_PACKAGES, MCC_PACKAGES, PH_MCC_PACKAGES
from helpers import is_authorized, notify_owner, generate_list
import easy_bby

async def execute_buy_process(message, lines, regex_pattern, currency, packages_dict, process_func, title_prefix, is_mcc=False):
    tg_id = str(message.from_user.id)
    telegram_user = message.from_user.username
    
    if telegram_user:
        user_link = f'<a href="https://t.me/{telegram_user}">@{telegram_user}</a>'
    else:
        user_link = f'<a href="tg://user?id={tg_id}">{tg_id}</a>'
        
    v_bal_key = 'br_balance' if currency == 'BR' else 'ph_balance'
    
    async with user_locks[tg_id]: 
        parsed_orders = []
        
        for line in lines:
            line = line.strip()
            if not line: continue 
            
            match = re.search(regex_pattern, line)
            if not match:
                await message.reply(f"Invalid format: `{line}`\nCheck /help for correct format.")
                continue
                
            game_id = match.group(1)
            zone_id = match.group(2)
            raw_items_str = match.group(3).lower()
            
            requested_packages = raw_items_str.split()
            packages_to_buy = [] 
            not_found_pkgs = []
            
            for pkg in requested_packages:
                active_packages = None
                if isinstance(packages_dict, list):
                    for p_dict in packages_dict:
                        if pkg in p_dict: 
                            active_packages = p_dict
                            break
                else:
                    if pkg in packages_dict: 
                        active_packages = packages_dict
                        
                if active_packages: 
                    pkg_items = []
                    for item_dict in active_packages[pkg]:
                        new_item = item_dict.copy()
                        new_item['pkg_name'] = pkg.upper() 
                        pkg_items.append(new_item)
                    packages_to_buy.append({
                        'pkg_name': pkg.upper(),
                        'items': pkg_items
                    })
                else: 
                    not_found_pkgs.append(pkg)
                    
            if not_found_pkgs:
                await message.reply(f"❌ Package(s) not found for ID {game_id}: {', '.join(not_found_pkgs)}")
                continue
            if not packages_to_buy: 
                continue
                
            line_price = sum(item['price'] for p in packages_to_buy for item in p['items'])
            parsed_orders.append({
                'game_id': game_id, 
                'zone_id': zone_id, 
                'raw_items_str': raw_items_str, 
                'packages_to_buy': packages_to_buy, 
                'line_price': line_price
            })
            
        if not parsed_orders: 
            return

        user_wallet = await db.get_reseller(tg_id)
        user_v_bal = user_wallet.get(v_bal_key, 0.0) if user_wallet else 0.0
            
        start_time = time.time()
        loading_msg = await message.reply(f"in processing [ {len(parsed_orders)} | 0 ] ● ᥫ᭡")

        current_v_bal = [user_v_bal] 

        async def process_order_line(order):
            game_id = order['game_id']
            zone_id = order.get('order_zone', order['zone_id'])
            raw_items_str = order['raw_items_str']
            packages_to_buy = order['packages_to_buy']
            
            overall_success_count = 0
            overall_fail_count = 0
            total_spent = 0.0
            
            ig_name = "Unknown"
            package_results = [] 

            async with api_semaphore:
                prev_context = None
                last_success_order = ""
                
                for pkg_data in packages_to_buy:
                    pkg_name = pkg_data['pkg_name']
                    items = pkg_data['items']
                    
                    pkg_success_count = 0
                    pkg_fail_count = 0
                    pkg_spent = 0.0
                    pkg_order_ids = ""
                    pkg_error = ""
                    
                    pkg_total_price = sum(item['price'] for item in items)
                    
                    if current_v_bal[0] < pkg_total_price:
                        pkg_fail_count = len(items)
                        pkg_error = "Insufficient balance for the full package"
                        overall_fail_count += 1
                        package_results.append({
                            'pkg_name': pkg_name,
                            'status': 'fail',
                            'spent': 0.0,
                            'order_ids': "",
                            'error_msg': pkg_error,
                            'ig_name': ig_name
                        })
                        continue
                    
                    for item in items:
                        if current_v_bal[0] < item['price']:
                            pkg_fail_count += 1
                            pkg_error = "Insufficient balance"
                            break

                        current_v_bal[0] -= item['price']

                        skip_check = False 
                        res = {}
                        
                        max_retries = 2
                        for attempt in range(max_retries):
                            res = await process_func(
                                game_id, zone_id, item['pid'], currency, 
                                prev_context=prev_context, skip_role_check=skip_check, 
                                known_ig_name=ig_name, last_success_order_id=last_success_order
                            )
                            
                            error_text_check = str(res.get('message', '')).lower()
                            
                            if res.get('status') == 'success' or "insufficient" in error_text_check or "invalid" in error_text_check or "not found" in error_text_check:
                                break
                                
                            if attempt < max_retries - 1:
                                await asyncio.sleep(1.5)
                                
                        fetched_name = res.get('ig_name') or res.get('username') or res.get('role_name') or res.get('nickname')
                        if fetched_name and str(fetched_name).strip() not in ["", "Unknown", "None"]:
                            ig_name = str(fetched_name).strip()

                        if res.get('status') == 'success':
                            pkg_success_count += 1
                            pkg_spent += item['price']
                            pkg_order_ids += f"{res.get('order_id', '')}\n"
                            prev_context = {'csrf_token': res.get('csrf_token')}
                            last_success_order = res.get('order_id', '')
                        else:
                            current_v_bal[0] += item['price']
                            pkg_fail_count += 1
                            pkg_error = res.get('message', 'Unknown Error')
                            break 
                            
                    if pkg_success_count > 0:
                        overall_success_count += 1
                        total_spent += pkg_spent
                        
                        display_name = pkg_name
                        if len(items) > 1 and pkg_success_count < len(items):
                            if pkg_name.upper().startswith("WP"):
                                display_name = f"WP{pkg_success_count}"
                            else:
                                display_name = f"{pkg_name} ({pkg_success_count}/{len(items)} Success)"
                                
                        package_results.append({
                            'pkg_name': display_name,
                            'status': 'success',
                            'spent': pkg_spent,
                            'order_ids': pkg_order_ids.strip(),
                            'error_msg': "",
                            'ig_name': ig_name
                        })
                        
                    if pkg_fail_count > 0:
                        overall_fail_count += 1
                        
                        display_name = pkg_name
                        if len(items) > 1 and pkg_fail_count < len(items):
                            if pkg_name.upper().startswith("WP"):
                                display_name = f"WP{len(items) - pkg_success_count}"
                            else:
                                display_name = f"{pkg_name} ({len(items) - pkg_success_count} Failed)"
                                
                        package_results.append({
                            'pkg_name': display_name,
                            'status': 'fail',
                            'spent': 0.0,
                            'order_ids': "",
                            'error_msg': pkg_error,
                            'ig_name': ig_name
                        })
                        
            return {
                'game_id': game_id, 
                'zone_id': zone_id, 
                'raw_items_str': raw_items_str, 
                'success_count': overall_success_count, 
                'fail_count': overall_fail_count, 
                'total_spent': total_spent, 
                'ig_name': ig_name,
                'package_results': package_results 
            }

        line_tasks = [process_order_line(order) for order in parsed_orders]
        line_results = await asyncio.gather(*line_tasks)
        time_taken_seconds = int(time.time() - start_time)
        await loading_msg.delete() 

        if not line_results: return

        now = datetime.datetime.now(MMT) 
        date_str = now.strftime("%m/%d/%Y, %I:%M:%S %p")

        for res in line_results:
            current_wallet = await db.get_reseller(tg_id)
            initial_bal_for_receipt = current_wallet.get(v_bal_key, 0.0) if current_wallet else 0.0
            
            if res['total_spent'] > 0:
                if currency == 'BR': await db.update_balance(tg_id, br_amount=-res['total_spent'])
                else: await db.update_balance(tg_id, ph_amount=-res['total_spent'])
                
            new_wallet = await db.get_reseller(tg_id)
            new_v_bal = new_wallet.get(v_bal_key, 0.0) if new_wallet else 0.0
            
            header_title = f"{title_prefix} {res['game_id']} ({res['zone_id']}) {res['raw_items_str'].upper()} ({currency})"
            
            report = f"<blockquote><pre>{header_title}\n"
            report += f"===== TRANSACTION REPORT =====\n\n"

            for pr in res['package_results']:
                safe_ig_name = html.escape(str(pr['ig_name']))
                
                if pr['status'] == 'success':
                    report += f"ORDER STATUS : ✅ Success\n"
                    report += f"GAME ID      : {res['game_id']} {res['zone_id']}\n"
                    report += f"IG NAME      : {safe_ig_name}\n"
                    report += f"SERIAL       :\n{pr['order_ids']}\n"
                    report += f"ITEM         : {pr['pkg_name']} 💎\n"
                    report += f"SPENT        : {pr['spent']:.2f} 🪙\n\n"
                    
                    final_order_ids = pr['order_ids'].replace('\n', ', ')
                    await db.save_order(
                        tg_id=tg_id, game_id=res['game_id'], zone_id=res['zone_id'], item_name=pr['pkg_name'], 
                        price=pr['spent'], order_id=final_order_ids, status="success"
                    )
                else:
                    error_text = str(pr['error_msg']).lower()
                    if "insufficient" in error_text or "saldo" in error_text: 
                        display_err = "Insufficient balance"
                    elif "invalid" in error_text or "not found" in error_text:
                        display_err = "Invalid Account"
                    elif "erro no servidor" in error_text or "server error" in error_text:
                        display_err = "Game Server Error (Please try again later)"
                    elif "query failed" in error_text:
                        display_err = "Smileone website api error try again."
                    elif "limit" in error_text or "exceed" in error_text or "máximo" in error_text or "limite" in error_text:
                        display_err = "Weekly Pass Limit Exceeded"
                    elif "zone" in error_text or "region" in error_text or "country" in error_text or "indonesia" in error_text or "support recharge" in error_text or "Singapore" in error_text or "Russia" in error_text or "the Philippines" in error_text:
                        display_err = "Ban Server"
                    else: 
                        display_err = pr['error_msg'].replace('❌', '').strip()
                        if not display_err: display_err = "Purchase Failed"
                        
                        if "wp" in pr['pkg_name'].lower():
                            if "unable" in error_text or "fail" in error_text or "error" in error_text:
                                display_err = "Weekly Pass Limit Exceeded"
                                
                    report += f"ORDER STATUS : ❌ FAILED\n"
                    report += f"GAME ID      : {res['game_id']} {res['zone_id']}\n"
                    report += f"IG NAME      : {safe_ig_name}\n"
                    report += f"ITEM         : {pr['pkg_name']} 💎\n"
                    report += f"ERROR        : {display_err}\n\n"

            report += f"DATE         : {date_str}\n"
            report += f"===== {user_link} =====\n"
            report += f"INITIAL      : ${initial_bal_for_receipt:,.2f}\n"
            report += f"FINAL        : ${new_v_bal:,.2f}\n\n"
            report += f"SUCCESS {res['success_count']} / FAIL {res['fail_count']}\n"
            report += f"TIME TAKEN   : {time_taken_seconds} SECONDS</pre></blockquote>"

            await message.reply(report, parse_mode=ParseMode.HTML)

@dp.message(or_f(Command("add"), F.text.regexp(r"(?i)^\.add(?:$|\s+)")))
async def add_reseller(message: types.Message):
    if message.from_user.id != OWNER_ID: return await message.reply("You are not the Owner.")
    parts = message.text.split()
    if len(parts) < 2: return await message.reply("`/add <user_id>`")
    target_id = parts[1].strip()
    if not target_id.isdigit(): return await message.reply("Please enter the User ID in numbers only.")
    if await db.add_reseller(target_id, f"User_{target_id}"):
        await message.reply(f"✅ Reseller ID `{target_id}` has been approved.")
    else:
        await message.reply(f"Reseller ID `{target_id}` is already in the list.")

@dp.message(or_f(Command("remove"), F.text.regexp(r"(?i)^\.remove(?:$|\s+)")))
async def remove_reseller(message: types.Message):
    if message.from_user.id != OWNER_ID: return await message.reply("You are not the Owner.")
    parts = message.text.split()
    if len(parts) < 2: return await message.reply("Usage format - `/remove <user_id>`")
    target_id = parts[1].strip()
    if target_id == str(OWNER_ID): return await message.reply("The Owner cannot be removed.")
    if await db.remove_reseller(target_id):
        await message.reply(f"✅ Reseller ID `{target_id}` has been removed.")
    else:
        await message.reply("That ID is not in the list.")

@dp.message(or_f(Command("users"), F.text.regexp(r"(?i)^\.users$")))
async def list_resellers(message: types.Message):
    if message.from_user.id != OWNER_ID: return await message.reply("You are not the Owner.")
    resellers_list = await db.get_all_resellers()
    user_list = []
    for r in resellers_list:
        role = "owner" if r["tg_id"] == str(OWNER_ID) else "users"
        user_list.append(f"🟢 ID: `{r['tg_id']}` ({role})\n   BR: ${r.get('br_balance', 0.0)} | PH: ${r.get('ph_balance', 0.0)}")
    final_text = "\n\n".join(user_list) if user_list else "No users found."
    await message.reply(f"🟢 **Approved users List (V-Wallet):**\n\n{final_text}")

@dp.message(Command("setcookie"))
async def set_cookie_command(message: types.Message):
    if message.from_user.id != OWNER_ID: return await message.reply("❌ Only the Owner can set the Cookie.")
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2: return await message.reply("⚠️ **Usage format:**\n`/setcookie <Long_Main_Cookie>`")
    await db.update_main_cookie(parts[1].strip())
    
    easy_bby.GLOBAL_SCRAPER = None
    easy_bby.GLOBAL_CSRF = {'mlbb_br': None, 'mlbb_ph': None, 'mcc_br': None, 'mcc_ph': None}
    await message.reply("✅ **Main Cookie has been successfully updated securely.**")

@dp.message(F.text.contains("PHPSESSID") & F.text.contains("cf_clearance"))
async def handle_smart_cookie_update(message: types.Message):
    if message.from_user.id != OWNER_ID: return await message.reply("❌ You are not authorized.")
    text = message.text
    target_keys = ["PHPSESSID", "cf_clearance", "__cf_bm", "_did", "_csrf"]
    extracted_cookies = {}
    try:
        for key in target_keys:
            pattern = rf"['\"]?{key}['\"]?\s*[:=]\s*['\"]?([^'\",;\s}}]+)['\"]?"
            match = re.search(pattern, text)
            if match:
                extracted_cookies[key] = match.group(1)
        if "PHPSESSID" not in extracted_cookies or "cf_clearance" not in extracted_cookies:
            return await message.reply("❌ <b>Error:</b> `PHPSESSID` နှင့် `cf_clearance` ကို ရှာမတွေ့ပါ။ Format မှန်ကန်ကြောင်း စစ်ဆေးပါ။", parse_mode=ParseMode.HTML)
        formatted_cookie_str = "; ".join([f"{k}={v}" for k, v in extracted_cookies.items()])
        await db.update_main_cookie(formatted_cookie_str)
        
        easy_bby.GLOBAL_SCRAPER = None
        easy_bby.GLOBAL_CSRF = {'mlbb_br': None, 'mlbb_ph': None, 'mcc_br': None, 'mcc_ph': None}
        
        success_msg = "✅ <b>Cookies Successfully Extracted & Saved!</b>\n\n📦 <b>Extracted Data:</b>\n"
        for k, v in extracted_cookies.items():
            display_v = f"{v[:15]}...{v[-15:]}" if len(v) > 35 else v
            success_msg += f"🔸 <code>{k}</code> : {display_v}\n"
        success_msg += f"\n🍪 <b>Formatted Final String:</b>\n<code>{formatted_cookie_str}</code>"
        await message.reply(success_msg, parse_mode=ParseMode.HTML)
    except Exception as e:
        await message.reply(f"❌ <b>Parsing Error:</b> {str(e)}", parse_mode=ParseMode.HTML)

@dp.message(or_f(Command("addbal"), F.text.regexp(r"(?i)^\.addbal(?:$|\s+)")))
async def add_balance_command(message: types.Message):
    if message.from_user.id != OWNER_ID: return await message.reply("❌ You are not authorized.")
    parts = message.text.strip().split()
    if len(parts) < 3: return await message.reply("⚠️ **Usage format:**\n`.addbal <User_ID> <Amount> [BR/PH]`")
    target_id = parts[1]
    try: amount = float(parts[2])
    except ValueError: return await message.reply("❌ Invalid amount.")
    currency = "BR"
    if len(parts) > 3:
        currency = parts[3].upper()
        if currency not in ['BR', 'PH']: return await message.reply("❌ Invalid currency.")
    target_wallet = await db.get_reseller(target_id)
    if not target_wallet: return await message.reply(f"❌ User ID `{target_id}` not found.")
    if currency == 'BR': await db.update_balance(target_id, br_amount=amount)
    else: await db.update_balance(target_id, ph_amount=amount)
    updated_wallet = await db.get_reseller(target_id)
    new_br = updated_wallet.get('br_balance', 0.0)
    new_ph = updated_wallet.get('ph_balance', 0.0)
    await message.reply(f"✅ **Balance Added Successfully!**\n\n👤 **User ID:** `{target_id}`\n💰 **Added:** `+{amount:,.2f} {currency}`\n\n📊 **Current Balance:**\n🇧🇷 BR: `${new_br:,.2f}`\n🇵🇭 PH: `${new_ph:,.2f}`")

@dp.message(or_f(Command("deduct"), F.text.regexp(r"(?i)^\.deduct(?:$|\s+)")))
async def deduct_balance_command(message: types.Message):
    if message.from_user.id != OWNER_ID: return await message.reply("❌ You are not authorized.")
    parts = message.text.strip().split()
    if len(parts) < 3: return await message.reply("⚠️ **Usage format:**\n`.deduct <User_ID> <Amount> [BR/PH]`")
    target_id = parts[1]
    try: amount = abs(float(parts[2]))
    except ValueError: return await message.reply("❌ Invalid amount.")
    currency = "BR"
    if len(parts) > 3:
        currency = parts[3].upper()
        if currency not in ['BR', 'PH']: return await message.reply("❌ Invalid currency.")
    target_wallet = await db.get_reseller(target_id)
    if not target_wallet: return await message.reply(f"❌ User ID `{target_id}` not found.")
    if currency == 'BR': await db.update_balance(target_id, br_amount=-amount)
    else: await db.update_balance(target_id, ph_amount=-amount)
    updated_wallet = await db.get_reseller(target_id)
    new_br = updated_wallet.get('br_balance', 0.0)
    new_ph = updated_wallet.get('ph_balance', 0.0)
    await message.reply(f"✅ **Balance Deducted Successfully!**\n\n👤 **User ID:** `{target_id}`\n💸 **Deducted:** `-{amount:,.2f} {currency}`\n\n📊 **Current Balance:**\n🇧🇷 BR: `${new_br:,.2f}`\n🇵🇭 PH: `${new_ph:,.2f}`")

@dp.message(F.text.regexp(r"(?i)^\.topup\s+([a-zA-Z0-9]+)(?:\s+(BR|PH))?"))
async def handle_topup(message: types.Message):
    if not await is_authorized(message.from_user.id): return await message.reply("ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ ᴜsᴇʀ.")
    
    match = re.search(r"(?i)^\.topup\s+([a-zA-Z0-9]+)(?:\s+(BR|PH))?", message.text.strip())
    if not match: return await message.reply("Usage format - `.topup <Code> [BR/PH]`")
    
    activation_code = match.group(1).strip()
    target_region = match.group(2).upper() if match.group(2) else None
    
    tg_id = str(message.from_user.id)
    user_id_int = message.from_user.id 
    loading_msg = await message.reply(f"Checking Code `{activation_code}`...")
    
    async with user_locks[tg_id]:
        scraper = await easy_bby.get_main_scraper()
        from bs4 import BeautifulSoup
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36', 'Accept': 'text/html'}
        
        async def try_redeem(api_type):
            if api_type == 'PH':
                page_url = 'https://www.smile.one/ph/customer/activationcode'
                check_url = 'https://www.smile.one/ph/smilecard/pay/checkcard'
                pay_url = 'https://www.smile.one/ph/smilecard/pay/payajax'
                base_origin = 'https://www.smile.one'
                base_referer = 'https://www.smile.one/ph/'
                balance_check_url = 'https://www.smile.one/ph/customer/order'
            else:
                page_url = 'https://www.smile.one/customer/activationcode'
                check_url = 'https://www.smile.one/smilecard/pay/checkcard'
                pay_url = 'https://www.smile.one/smilecard/pay/payajax'
                base_origin = 'https://www.smile.one'
                base_referer = 'https://www.smile.one/'
                balance_check_url = 'https://www.smile.one/customer/order'

            req_headers = headers.copy()
            req_headers['Referer'] = base_referer

            try:
                res = await scraper.get(page_url, headers=req_headers)
                if "login" in str(res.url).lower() or res.status_code in [403, 503]: return "expired", None

                soup = BeautifulSoup(res.text, 'html.parser')
                csrf_token = soup.find('meta', {'name': 'csrf-token'})
                csrf_token = csrf_token.get('content') if csrf_token else (soup.find('input', {'name': '_csrf'}).get('value') if soup.find('input', {'name': '_csrf'}) else None)
                if not csrf_token: return "expired", None 

                ajax_headers = req_headers.copy()
                ajax_headers.update({'X-Requested-With': 'XMLHttpRequest', 'Origin': base_origin, 'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'})

                check_res_raw = await scraper.post(check_url, data={'_csrf': csrf_token, 'pin': activation_code}, headers=ajax_headers)
                check_res = check_res_raw.json()
                code_status = str(check_res.get('code', check_res.get('status', '')))
                
                card_amount = 0.0
                try:
                    if 'data' in check_res and isinstance(check_res['data'], dict):
                        val = check_res['data'].get('amount', check_res['data'].get('money', 0))
                        if val: card_amount = float(val)
                except: pass

                if code_status in ['200', '201', '0', '1'] or 'success' in str(check_res.get('msg', '')).lower():
                    old_bal = await easy_bby.get_smile_balance(scraper, headers, balance_check_url)
                    pay_res_raw = await scraper.post(pay_url, data={'_csrf': csrf_token, 'sec': activation_code}, headers=ajax_headers)
                    pay_res = pay_res_raw.json()
                    pay_status = str(pay_res.get('code', pay_res.get('status', '')))
                    
                    if pay_status in ['200', '0', '1'] or 'success' in str(pay_res.get('msg', '')).lower():
                        await asyncio.sleep(5) 
                        anti_cache_url = f"{balance_check_url}?_t={int(time.time())}"
                        new_bal = await easy_bby.get_smile_balance(scraper, headers, anti_cache_url)
                        bal_key = 'br_balance' if api_type == 'BR' else 'ph_balance'
                        added = round(new_bal[bal_key] - old_bal[bal_key], 2)
                        if added <= 0 and card_amount > 0: added = card_amount
                        return "success", added
                    else: return "fail", "Payment failed."
                else: return "invalid", "Invalid Code"
            except Exception as e: return "error", str(e)

        if target_region == 'BR':
            status, result = await try_redeem('BR')
            active_region = 'BR'
        elif target_region == 'PH':
            status, result = await try_redeem('PH')
            active_region = 'PH'
        else:
            status, result = await try_redeem('BR')
            active_region = 'BR'
            if status in ['invalid', 'fail']: 
                status, result = await try_redeem('PH')
                active_region = 'PH'

        if status == "expired":
            await loading_msg.edit_text("⚠️ <b>Cookies Expired!</b>\n\nAuto-login စတင်နေပါသည်... ခဏစောင့်ပြီး ပြန်လည်ကြိုးစားပါ။", parse_mode=ParseMode.HTML)
            await notify_owner("⚠️ <b>Top-up Alert:</b> Code ဖြည့်သွင်းနေစဉ် Cookie သက်တမ်းကုန်သွားပါသည်။ Auto-login စတင်နေပါသည်...")
            success = await easy_bby.auto_login_and_get_cookie()
            if not success: await notify_owner("❌ <b>Critical:</b> Auto-Login မအောင်မြင်ပါ။ `/setcookie` ဖြင့် အသစ်ထည့်ပေးပါ။")
        elif status == "error": await loading_msg.edit_text(f"❌ Error: {result}")
        elif status in ['invalid', 'fail']: await loading_msg.edit_text("Cʜᴇᴄᴋ Fᴀɪʟᴇᴅ❌\n(Code is invalid or might have been used)")
        elif status == "success":
            added_amount = result
            if added_amount <= 0:
                await loading_msg.edit_text(f"sᴍɪʟᴇ ᴏɴᴇ ʀᴇᴅᴇᴇᴍ ᴄᴏᴅᴇ sᴜᴄᴄᴇss ✅\n(Cannot retrieve exact amount due to System Delay.)")
            else:
                if user_id_int == OWNER_ID: 
                    fee_percent = 0.0
                else:
                    if added_amount >= 10000: fee_percent = 0.10
                    elif added_amount >= 5000: fee_percent = 0.15
                    elif added_amount == 1120: fee_percent = 0.2 
                    elif added_amount >= 1000: fee_percent = 0.2
                    else: fee_percent = 0.3

                fee_amount = round(added_amount * (fee_percent / 100), 2)
                net_added = round(added_amount - fee_amount, 2)
        
                user_wallet = await db.get_reseller(tg_id)
                if active_region == 'BR':
                    assets = user_wallet.get('br_balance', 0.0) if user_wallet else 0.0
                    await db.update_balance(tg_id, br_amount=net_added)
                else:
                    assets = user_wallet.get('ph_balance', 0.0) if user_wallet else 0.0
                    await db.update_balance(tg_id, ph_amount=net_added)

                total_assets = assets + net_added
                fmt_amount = int(added_amount) if added_amount % 1 == 0 else added_amount

                msg = (f"✅ <b>Code Top-Up Successful</b>\n\n<code>Code   : {activation_code} ({active_region})\nAmount : {fmt_amount:,}\nFee    : -{fee_amount:.1f} ({fee_percent}%)\nAdded  : +{net_added:,.1f} 🪙\nAssets : {assets:,.1f} 🪙\nTotal  : {total_assets:,.1f} 🪙</code>")
                await loading_msg.edit_text(msg, parse_mode=ParseMode.HTML)

@dp.message(or_f(Command("balance"), F.text.regexp(r"(?i)^\.bal(?:$|\s+)")))
async def check_balance_command(message: types.Message):
    if not await is_authorized(message.from_user.id): return await message.reply("ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ ᴜsᴇʀ.")
    tg_id = str(message.from_user.id)
    user_wallet = await db.get_reseller(tg_id)
    if not user_wallet: return await message.reply("Yᴏᴜʀ ᴀᴄᴄᴏᴜɴᴛ ɪɴғᴏʀᴍᴀᴛɪᴏɴ ᴄᴀɴɴᴏᴛ ʙᴇ ғᴏᴜɴᴅ.")
    
    ICON_EMOJI = "5956330306167376831" 
    BR_EMOJI = "5228878788867142213"   
    PH_EMOJI = "5231361434583049965"   

    report = (f"<blockquote><tg-emoji emoji-id='{ICON_EMOJI}'>💳</tg-emoji> <b>𝗬𝗢𝗨𝗥 𝗪𝗔𝗟𝗟𝗘𝗧 𝗕𝗔𝗟𝗔𝗡𝗖𝗘</b>\n\n<tg-emoji emoji-id='{BR_EMOJI}'>🇧🇷</tg-emoji> 𝗕𝗥 𝗕𝗔𝗟𝗔𝗡𝗖𝗘 : ${user_wallet.get('br_balance', 0.0):,.2f}\n<tg-emoji emoji-id='{PH_EMOJI}'>🇵🇭</tg-emoji> 𝗣𝗛 𝗕𝗔𝗟𝗔𝗡𝗖𝗘 : ${user_wallet.get('ph_balance', 0.0):,.2f}</blockquote>")
    
    if message.from_user.id == OWNER_ID:
        loading_msg = await message.reply("Fetching real balance from the official account...")
        scraper = await easy_bby.get_main_scraper()
        headers = {'X-Requested-With': 'XMLHttpRequest', 'Origin': 'https://www.smile.one'}
        try:
            balances = await easy_bby.get_smile_balance(scraper, headers, 'https://www.smile.one/customer/order')
            report += (f"\n\n<blockquote><tg-emoji emoji-id='{ICON_EMOJI}'>💳</tg-emoji> <b>𝗢𝗙𝗙𝗜𝗖𝗜𝗔𝗟 𝗔𝗖𝗖𝗢𝗨𝗡𝗧 𝗕𝗔𝗟𝗔𝗡𝗖𝗘</b>\n\n<tg-emoji emoji-id='{BR_EMOJI}'>🇧🇷</tg-emoji> 𝗕𝗥 𝗕𝗔𝗟𝗔𝗡𝗖𝗘 : ${balances.get('br_balance', 0.00):,.2f}\n<tg-emoji emoji-id='{PH_EMOJI}'>🇵🇭</tg-emoji> 𝗣𝗛 𝗕𝗔𝗟𝗔𝗡𝗖𝗘 : ${balances.get('ph_balance', 0.00):,.2f}</blockquote>")
            await loading_msg.edit_text(report, parse_mode=ParseMode.HTML)
        except Exception as e:
            try: await loading_msg.edit_text(report, parse_mode=ParseMode.HTML)
            except: pass
    else:
        try: await message.reply(report, parse_mode=ParseMode.HTML)
        except: pass

@dp.message(or_f(Command("history"), F.text.regexp(r"(?i)^\.his$")))
async def send_order_history(message: types.Message):
    if not await is_authorized(message.from_user.id): return await message.reply("ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ ᴜsᴇʀ.")
    tg_id = str(message.from_user.id)
    user_name = message.from_user.username or message.from_user.first_name
    history_data = await db.get_user_history(tg_id, limit=200)
    if not history_data: return await message.reply("📜 **No Order History Found.**")
    response_text = f"==== Order History for @{user_name} ====\n\n"
    for order in history_data:
        response_text += (f"🆔 Game ID: {order['game_id']}\n🌏 Zone ID: {order['zone_id']}\n💎 Pack: {order['item_name']}\n🆔 Order ID: {order['order_id']}\n📅 Date: {order['date_str']}\n💲 Rate: ${order['price']:,.2f}\n📊 Status: {order['status']}\n────────────────\n")
    file_bytes = response_text.encode('utf-8')
    document = BufferedInputFile(file_bytes, filename=f"History_{tg_id}.txt")
    await message.answer_document(document=document, caption=f"📜 **Order History**\n👤 User: @{user_name}\n📊 Records: {len(history_data)}")

@dp.message(or_f(Command("clean"), F.text.regexp(r"(?i)^\.clean$")))
async def clean_order_history(message: types.Message):
    if not await is_authorized(message.from_user.id): return await message.reply("ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ ᴜsᴇʀ.")
    tg_id = str(message.from_user.id)
    deleted_count = await db.clear_user_history(tg_id)
    if deleted_count > 0: await message.reply(f"🗑️ **History Cleaned Successfully.**\nDeleted {deleted_count} order records from your history.")
    else: await message.reply("📜 **No Order History Found to Clean.**")

@dp.message(F.text.regexp(r"(?i)^(?:msc|mlb|br|b)\s*\d+"))
async def handle_br_mlbb(message: types.Message):
    if not await is_authorized(message.from_user.id): 
        return await message.reply(f"ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ ᴜsᴇʀ.❌")
    try:
        lines = [line.strip() for line in message.text.strip().split('\n') if line.strip()]
        regex = r"(?i)^(?:(?:b|br|mlb|msc)\s*)?(\d+)\s*\(?\s*(\d+)\s*\)?\s*(.+)$"
        
        total_pkgs = 0
        for line in lines:
            match = re.search(regex, line)
            if match: total_pkgs += len(match.group(3).split())
            
        if total_pkgs > 10: 
            return await message.reply("❌ 10 Limit Exceeded: တစ်ကြိမ်လျှင် အများဆုံး ၁၀ ခုသာ ဝယ်ယူနိုင်ပါသည်။")
            
        await execute_buy_process(message, lines, regex, 'BR', [DOUBLE_DIAMOND_PACKAGES, BR_PACKAGES], easy_bby.process_smile_one_order, "MLBB")
    except Exception as e: 
        await message.reply(f"System Error: {str(e)}")

@dp.message(F.text.regexp(r"(?i)^(?:mlp|ph|p)\s*\d+"))
async def handle_ph_mlbb(message: types.Message):
    if not await is_authorized(message.from_user.id): 
        return await message.reply(f"ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ ᴜsᴇʀ.❌")
    try:
        lines = [line.strip() for line in message.text.strip().split('\n') if line.strip()]
        regex = r"(?i)^(?:(?:p|ph|mlp|mcp)\s*)?(\d+)\s*\(?\s*(\d+)\s*\)?\s*(.+)$"
        
        total_pkgs = 0
        for line in lines:
            match = re.search(regex, line)
            if match: total_pkgs += len(match.group(3).split())
            
        if total_pkgs > 10: 
            return await message.reply("❌ 10 Limit Exceeded: တစ်ကြိမ်လျှင် အများဆုံး ၁၀ ခုသာ ဝယ်ယူနိုင်ပါသည်။")
            
        await execute_buy_process(message, lines, regex, 'PH', PH_PACKAGES, easy_bby.process_smile_one_order, "MLBB")
    except Exception as e: 
        await message.reply(f"System Error: {str(e)}")
        
@dp.message(F.text.regexp(r"(?i)^(?:mcc|mcb)\s*\d+"))
async def handle_br_mcc(message: types.Message):
    if not await is_authorized(message.from_user.id): 
        return await message.reply(f"ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ ᴜsᴇʀ.❌")
    try:
        lines = [line.strip() for line in message.text.strip().split('\n') if line.strip()]
        regex = r"(?i)^(?:(?:mcc|mcb|mcp|mcgg)\s*)?(\d+)\s*\(?\s*(\d+)\s*\)?\s*(.+)$"
        
        total_pkgs = 0
        for line in lines:
            match = re.search(regex, line)
            if match: total_pkgs += len(match.group(3).split())
            
        if total_pkgs > 5: 
            return await message.reply("❌ 5 Limit Exceeded: တစ်ကြိမ်လျှင် အများဆုံး ၅ ခုသာ ဝယ်ယူနိုင်ပါသည်။")
            
        await execute_buy_process(message, lines, regex, 'BR', MCC_PACKAGES, easy_bby.process_mcc_order, "MCC", is_mcc=True)
    except Exception as e: 
        await message.reply(f"System Error: {str(e)}")
        
@dp.message(F.text.regexp(r"(?i)^mcp\s*\d+"))
async def handle_ph_mcc(message: types.Message):
    if not await is_authorized(message.from_user.id): 
        return await message.reply(f"ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ ᴜsᴇʀ.❌")
    try:
        lines = [line.strip() for line in message.text.strip().split('\n') if line.strip()]
        regex = r"(?i)^(?:mcp\s*)?(\d+)\s*\(?\s*(\d+)\s*\)?\s*(.+)$"
        
        total_pkgs = 0
        for line in lines:
            match = re.search(regex, line)
            if match: total_pkgs += len(match.group(3).split())
            
        if total_pkgs > 5: 
            return await message.reply("❌ 5 Limit Exceeded: တစ်ကြိမ်လျှင် အများဆုံး ၅ ခုသာ ဝယ်ယူနိုင်ပါသည်။")
            
        await execute_buy_process(message, lines, regex, 'PH', PH_MCC_PACKAGES, easy_bby.process_mcc_order, "MCC", is_mcc=True)
    except Exception as e: 
        await message.reply(f"System Error: {str(e)}")

@dp.message(or_f(Command("listb"), F.text.regexp(r"(?i)^\.listb$")))
async def show_price_list_br(message: types.Message):
    if not await is_authorized(message.from_user.id): return await message.reply("ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ ᴜsᴇʀ.")
    response_text = f"🇧🇷 <b>𝘿𝙤𝙪𝙗𝙡𝙚 𝙋𝙖𝙘𝙠𝙖𝙜𝙚𝙨</b>\n<code>{generate_list(DOUBLE_DIAMOND_PACKAGES)}</code>\n\n🇧🇷 <b>𝘽𝙧 𝙋𝙖𝙘𝙠𝙖𝙜𝙚𝙨</b>\n<code>{generate_list(BR_PACKAGES)}</code>"
    await message.reply(response_text, parse_mode=ParseMode.HTML)

@dp.message(or_f(Command("listp"), F.text.regexp(r"(?i)^\.listp$")))
async def show_price_list_ph(message: types.Message):
    if not await is_authorized(message.from_user.id): return await message.reply("ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ ᴜsᴇʀ.")
    response_text = f"🇵🇭 <b>𝙋𝙝 𝙋𝙖𝙘𝙠𝙖𝙜𝙚𝙨</b>\n<code>{generate_list(PH_PACKAGES)}</code>"
    await message.reply(response_text, parse_mode=ParseMode.HTML)

@dp.message(or_f(Command("listmb"), F.text.regexp(r"(?i)^\.listmb$")))
async def show_price_list_mcc(message: types.Message):
    if not await is_authorized(message.from_user.id): return await message.reply("ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ ᴜsᴇʀ.")
    response_text = f"🇧🇷 <b>𝙈𝘾𝘾 𝙋𝘼𝘾𝙆𝘼𝙂𝙀𝙎</b>\n<code>{generate_list(MCC_PACKAGES)}</code>\n\n🇵🇭 <b>𝙋𝙝 𝙈𝘾𝘾 𝙋𝙖𝙘𝙠𝙖𝙜𝙚𝙨</b>\n<code>{generate_list(PH_MCC_PACKAGES)}</code>"
    await message.reply(response_text, parse_mode=ParseMode.HTML)

@dp.message(F.text.regexp(r"^[\d\s\.\(\)]+[\+\-\*\/][\d\s\+\-\*\/\(\)\.]+$"))
async def auto_calculator(message: types.Message):
    try:
        expr = message.text.strip()
        if re.match(r"^09[-\s]?\d+", expr): return
        clean_expr = expr.replace(" ", "")
        result = eval(clean_expr, {"__builtins__": None})
        if isinstance(result, float): formatted_result = f"{result:.4f}".rstrip('0').rstrip('.')
        else: formatted_result = str(result)
        await message.reply(f"{expr} = {formatted_result}")
    except Exception: pass

@dp.message(or_f(Command("cookies"), F.text.regexp(r"(?i)^\.cookies$")))
async def check_cookie_status(message: types.Message):
    if message.from_user.id != OWNER_ID: return await message.reply("❌ You are not authorized.")
    loading_msg = await message.reply("Checking Cookie status...")
    try:
        scraper = await easy_bby.get_main_scraper()
        headers = {'User-Agent': 'Mozilla/5.0', 'X-Requested-With': 'XMLHttpRequest', 'Origin': 'https://www.smile.one'}
        response = await scraper.get('https://www.smile.one/customer/order', headers=headers, timeout=15)
        if "login" not in str(response.url).lower() and response.status_code == 200: await loading_msg.edit_text("🟢 Aᴄᴛɪᴠᴇ", parse_mode=ParseMode.HTML)
        else: await loading_msg.edit_text("🔴 Exᴘɪʀᴇᴅ", parse_mode=ParseMode.HTML)
    except Exception as e: await loading_msg.edit_text(f"❌ Error checking cookie: {str(e)}")

@dp.message(or_f(Command("role"), F.text.regexp(r"(?i)^\.role(?:$|\s+)")))
async def handle_check_role(message: types.Message):

    if not await is_authorized(message.from_user.id): return await message.reply("ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ ᴜsᴇʀ.")
    match = re.search(r"(?i)^[./]?role\s+(\d+)\s*[\(]?\s*(\d+)\s*[\)]?", message.text.strip())
    if not match: return await message.reply("❌ Invalid format. Use: `.role 12345678 1234`")
    
    game_id, zone_id = match.group(1).strip(), match.group(2).strip()
    loading_msg = await message.reply("Checking region", parse_mode=ParseMode.HTML)

    url = 'https://coldofficialstore.com/api/name-checker/mlbb'
    params = {
        'user_id': game_id,
        'server_id': zone_id,
    }
    
    headers = {
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Cache-Control': 'no-cache',
        'Connection': 'keep-alive',
        'Pragma': 'no-cache',
        'Referer': 'https://coldofficialstore.com/name-checker',
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'same-origin',
        'User-Agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36',
    }

    try:
        async with AsyncSession(impersonate="chrome120") as local_scraper:
            res = await local_scraper.get(url, params=params, headers=headers, timeout=15)
        
        try:
            data = res.json()
        except Exception:
            return await loading_msg.edit_text(f"❌ API Error: Invalid Response.\n\n<code>{res.text[:100]}...</code>", parse_mode=ParseMode.HTML)

        user_data = data.get('data', {})
        ig_name = user_data.get('username', 'Unknown')
        
        if not ig_name or str(ig_name).strip() == "" or ig_name == 'Unknown':
            return await loading_msg.edit_text("❌ **Invalid Account:** Game ID or Zone ID is incorrect or not found.", parse_mode=ParseMode.HTML)
            
        country_code = user_data.get('country', 'Unknown')
        country_map = {"MM": "Myanmar", "FR": "France", "MY": "Malaysia", "PH": "Philippines", "ID": "Indonesia", "BR": "Brazil", "SG": "Singapore", "KH": "Cambodia", "TH": "Thailand"}
        final_region = country_map.get(str(country_code).upper(), country_code)

        limit_50 = limit_150 = limit_250 = limit_500 = True 
        
        bonus_limits = data.get('data2', {}).get('bonus_limit', [])
        for item in bonus_limits:
            title = str(item.get('title', ''))
            reached_limit = item.get('reached_limit', True) 
            
            if "50+50" in title: limit_50 = reached_limit
            elif "150+150" in title: limit_150 = reached_limit
            elif "250+250" in title: limit_250 = reached_limit
            elif "500+500" in title: limit_500 = reached_limit

        style_50 = "danger" if limit_50 else "success"
        style_150 = "danger" if limit_150 else "success"
        style_250 = "danger" if limit_250 else "success"
        style_500 = "danger" if limit_500 else "success"

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="Bᴏɴᴜs 50+50", callback_data="ignore", style=style_50),
                InlineKeyboardButton(text="Bᴏɴᴜs 150+150", callback_data="ignore", style=style_150)
            ],
            [
                InlineKeyboardButton(text="Bᴏɴᴜs 250+250", callback_data="ignore", style=style_250),
                InlineKeyboardButton(text="Bᴏɴᴜs 500+500", callback_data="ignore", style=style_500)
            ]
        ])

        final_report = (
            f"<u><b>Mᴏʙɪʟᴇ Lᴇɢᴇɴᴅs Bᴀɴɢ Bᴀɴɢ</b></u>\n\n"
            f"🆔 <code>{'User ID' :<9}:</code> <code>{game_id}</code> (<code>{zone_id}</code>)\n"
            f"👤 <code>{'Nickname':<9}:</code> {ig_name}\n"
            f"🌍 <code>{'Region'  :<9}:</code> {final_region}\n"
            f"────────────────\n\n"
            f"🎁 <b>Fɪʀsᴛ Rᴇᴄʜᴀʀɢᴇ Bᴏɴᴜs Sᴛᴀᴛᴜs</b>"
        )

        await loading_msg.edit_text(final_report, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    except Exception as e: 
        await loading_msg.edit_text(f"❌ System Error: {str(e)}", parse_mode=ParseMode.HTML)

@dp.message(or_f(Command("checkcus"), Command("cus"), F.text.regexp(r"(?i)^\.(?:checkcus|cus)(?:$|\s+)")))
async def check_official_customer(message: types.Message):
    tg_id = str(message.from_user.id)
    is_owner = (message.from_user.id == OWNER_ID)
    user_data = await db.get_reseller(tg_id) 
    
    if not is_owner and not user_data:
        return await message.reply("❌ You are not authorized.")
        
    parts = message.text.strip().split()
    if len(parts) < 2:
        return await message.reply("⚠️ <b>Usage:</b> <code>.cus <Game_ID></code>", parse_mode=ParseMode.HTML)
        
    search_query = parts[1]
    loading_msg = await message.reply(f"Deep Searching Official Records for: <code>{search_query}</code>...", parse_mode=ParseMode.HTML)
    
    scraper = await easy_bby.get_main_scraper()
    headers = {'X-Requested-With': 'XMLHttpRequest', 'Origin': 'https://www.smile.one'}
    
    urls_to_check = [
        'https://www.smile.one/customer/activationcode/codelist', 
        'https://www.smile.one/ph/customer/activationcode/codelist'
    ]
    
    found_orders = []
    seen_ids = set()
    
    try:
        for api_url in urls_to_check:
            for page_num in range(1, 11): 
                res = await scraper.get(
                    api_url, 
                    params={'type': 'orderlist', 'p': str(page_num), 'pageSize': '50'}, 
                    headers=headers, timeout=15
                )
                try:
                    data = res.json()
                    if 'list' in data and len(data['list']) > 0:
                        for order in data['list']:
                            current_user_id = str(order.get('user_id') or order.get('role_id') or '')
                            order_id = str(order.get('increment_id') or order.get('id') or '')
                            status_val = str(order.get('order_status', '') or order.get('status', '')).lower()
                            
                            if (current_user_id == search_query or order_id == search_query) and status_val in ['success', '1']:
                                if order_id not in seen_ids:
                                    seen_ids.add(order_id)
                                    found_orders.append(order)
                    else: 
                        break 
                except: 
                    break
                
        if not found_orders: 
            return await loading_msg.edit_text(f"❌ No successful records found for: <code>{search_query}</code>", parse_mode=ParseMode.HTML)
            
        found_orders = found_orders[:1] 
        report = f"🎉<b>Oғғɪᴄɪᴀʟ Rᴇᴄᴏʀᴅs ғᴏʀ {search_query}</b>\n\n"
        
        for order in found_orders:
            serial_id = str(order.get('increment_id') or order.get('id') or 'Unknown Serial')
            date_str = str(order.get('created_at') or order.get('updated_at') or order.get('create_time') or '')
            currency_sym = str(order.get('total_fee_currency') or '$')
            
            date_display = date_str
            if date_str:
                try:
                    dt_obj = datetime.datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
                    mmt_dt = dt_obj + datetime.timedelta(hours=9, minutes=30)
                    mm_time_str = mmt_dt.strftime("%I:%M:%S %p") 
                    date_display = f"{date_str} ( MM - {mm_time_str} )"
                except Exception:
                    date_display = date_str

            raw_item_name = str(order.get('product_name') or order.get('goods_name') or order.get('title') or 'Unknown Item')
            raw_item_name = raw_item_name.replace("Mobile Legends BR - ", "").replace("Mobile Legends - ", "").strip()
            
            translations = {
                "Passe Semanal de Diamante": "Weekly Diamond Pass",
                "Passagem do crepúsculo": "Twilight Pass",
                "Passe Crepúsculo": "Twilight Pass",
                "Pacote Semanal Elite": "Elite Weekly Bundle",
                "Pacote Mensal Épico": "Epic Monthly Bundle",
                "Membro Estrela Plus": "Starlight Member Plus",
                "Membro Estrela": "Starlight Member",
                "Diamantes": "Diamonds",
                "Diamante": "Diamond",
                "Bônus": "Bonus",
                "Pacote": "Bundle"
            }
            
            for pt, en in translations.items():
                if pt in raw_item_name:
                    raw_item_name = raw_item_name.replace(pt, en)
                    
            if raw_item_name.endswith(" c") or raw_item_name.endswith(" ("):
                raw_item_name = raw_item_name[:-2]
                
            raw_item_name = raw_item_name.strip()
            final_item_name = f"{raw_item_name}"
            
            price = str(order.get('price') or order.get('grand_total') or order.get('real_money') or '0.00')
            if currency_sym != '$':
                price_display = f"{price} {currency_sym}"
            else:
                price_display = f"${price}"
                
            report += f"🏷 <code>{serial_id}</code>\n📅 <code>{date_display}</code>\n💎 {final_item_name} ({price_display})\n📊 Status: ✅ Success\n\n"
            
        await loading_msg.edit_text(report, parse_mode=ParseMode.HTML)
    except Exception as e: 
        await loading_msg.edit_text(f"❌ Search Error: {str(e)}", parse_mode=ParseMode.HTML)

@dp.message(or_f(Command("topcus"), F.text.regexp(r"(?i)^\.topcus$")))
async def show_top_customers(message: types.Message):
    if message.from_user.id != OWNER_ID: return await message.reply("❌ Only Owner.")
    top_spenders = await db.get_top_customers(limit=10)
    if not top_spenders: return await message.reply("📜 No orders found in database.")
    
    report = "🏆 **Top 10 Customers (By Total Spent)** 🏆\n\n"
    for i, user in enumerate(top_spenders, 1):
        tg_id = user['_id']
        spent = user['total_spent']
        count = user['order_count']
        user_info = await db.get_reseller(tg_id)
        vip_tag = "🌟 [VIP]" if user_info and user_info.get('is_vip') else ""
        report += f"**{i}.** `ID: {tg_id}` {vip_tag}\n💰 Spent: ${spent:,.2f} ({count} Orders)\n\n"
        
    report += "💡 *Use `.setvip <ID>` to grant VIP status.*"
    await message.reply(report)

@dp.message(or_f(Command("setvip"), F.text.regexp(r"(?i)^\.setvip(?:$|\s+)")))
async def grant_vip_status(message: types.Message):
    if message.from_user.id != OWNER_ID: return await message.reply("❌ Only Owner.")
    parts = message.text.strip().split()
    if len(parts) < 2: return await message.reply("⚠️ **Usage:** `.setvip <User_ID>`")
    target_id = parts[1]
    user = await db.get_reseller(target_id)
    if not user: return await message.reply("❌ User not found.")
    
    current_status = user.get('is_vip', False)
    new_status = not current_status 
    await db.set_vip_status(target_id, new_status)
    status_msg = "Granted 🌟" if new_status else "Revoked ❌"
    await message.reply(f"✅ VIP Status for `{target_id}` has been **{status_msg}**.")

@dp.message(or_f(Command("sysbal"), F.text.regexp(r"(?i)^\.sysbal$")))
async def check_system_balance(message: types.Message):
    if message.from_user.id != OWNER_ID: return await message.reply("❌ You are not authorized.")
    loading_msg = await message.reply("📊 စနစ်တစ်ခုလုံး၏ မှတ်တမ်းကို တွက်ချက်နေပါသည်...")
    try:
        sys_balances = await db.get_total_system_balances()
        report = (
            "🏦 <b>System V-Wallet Total Balances</b> 🏦\n━━━━━━━━━━━━━━━━━━━━\n"
            f"👥 <b>User အားလုံးဆီရှိ စုစုပေါင်း ငွေကြေး:</b>\n\n"
            f"🇧🇷 BR Balance : <code>${sys_balances['total_br']:,.2f}</code>\n"
            f"🇵🇭 PH Balance : <code>${sys_balances['total_ph']:,.2f}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━\n<i>(မှတ်ချက်: ဤပမာဏသည် User အားလုံးထံသို့ Admin မှ ထည့်ပေးထားသော လက်ကျန်ငွေများ၏ စုစုပေါင်းဖြစ်ပါသည်။)</i>"
        )
        await loading_msg.edit_text(report, parse_mode=ParseMode.HTML)
    except Exception as e: await loading_msg.edit_text(f"❌ Error calculating system balance: {e}")

@dp.message(or_f(F.text.regexp(r"^\d{7,}(?:\s+\(?\d+\)?)?\s*.*$"), F.caption.regexp(r"^\d{7,}(?:\s+\(?\d+\)?)?\s*.*$")))
async def format_and_copy_text(message: types.Message):
    raw_text = (message.text or message.caption).strip()
    if re.match(r"^\d{7,}$", raw_text): formatted_raw = raw_text
    elif re.match(r"^\d{7,}\s+\d+", raw_text):
        match = re.match(r"^(\d{7,})\s+(\d+)\s*(.*)$", raw_text)
        if match:
            player_id, zone_id, suffix = match.group(1), match.group(2), match.group(3).strip()
            if suffix:
                clean_suffix = suffix.lower().replace(" ", "")
                wp_match = re.match(r"^(\d*)wp(\d*)$", clean_suffix)
                if wp_match:
                    num_str = wp_match.group(1) + wp_match.group(2)
                    processed_suffix = "wp" if num_str in ["", "1"] else f"wp{num_str}"
                else: processed_suffix = suffix
                formatted_raw = f"{player_id} ({zone_id}) {processed_suffix}"
            else: formatted_raw = f"{player_id} ({zone_id})"
        else: formatted_raw = raw_text
    elif re.match(r"^\d{7,}\s*\(\d+\)", raw_text):
        match = re.match(r"^(\d{7,})\s*\((\d+)\)\s*(.*)$", raw_text)
        if match:
            player_id, zone_id, suffix = match.group(1), match.group(2), match.group(3).strip()
            if suffix:
                clean_suffix = suffix.lower().replace(" ", "")
                wp_match = re.match(r"^(\d*)wp(\d*)$", clean_suffix)
                if wp_match:
                    num_str = wp_match.group(1) + wp_match.group(2)
                    processed_suffix = "wp" if num_str in ["", "1"] else f"wp{num_str}"
                else: processed_suffix = suffix
                formatted_raw = f"{player_id} ({zone_id}) {processed_suffix}"
            else: formatted_raw = f"{player_id} ({zone_id})"
        else: formatted_raw = raw_text
    else: formatted_raw = raw_text

    formatted_text = f"<code>{formatted_raw}</code>"
    try:
        from aiogram.types import CopyTextButton
        copy_btn = InlineKeyboardButton(text="ᴄᴏᴘʏ", copy_text=CopyTextButton(text=formatted_raw), style="primary")
    except ImportError:
        copy_btn = InlineKeyboardButton(text="ᴄᴏᴘʏ", switch_inline_query=formatted_raw, style="primary")

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[copy_btn]])
    await message.reply(formatted_text, parse_mode=ParseMode.HTML, reply_markup=keyboard)

@dp.message(or_f(Command("maintenance"), F.text.regexp(r"(?i)^\.maintenance(?:$|\s+)")))
async def toggle_maintenance(message: types.Message):
    if message.from_user.id != OWNER_ID:
        return await message.reply("ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ ᴜsᴇʀ.")
        
    parts = message.text.strip().lower().split()
    if len(parts) < 2 or parts[1] not in ["enable", "disable"]:
        return await message.reply("⚠️ **Usage:** `.maintenance enable` သို့မဟုတ် `.maintenance disable`")
        
    action = parts[1]
    
    if action == "enable":
        config.IS_MAINTENANCE = True
        await message.reply("✅ **Maintenance Mode ENABLED.**\nယခုအချိန်မှစ၍ Admin မှလွဲ၍ အခြား User များ Bot ကို အသုံးပြု၍ မရတော့ပါ။")
    elif action == "disable":
        config.IS_MAINTENANCE = False
        await message.reply("✅ **Maintenance Mode DISABLED.**\nBot ကို ပုံမှန်အတိုင်း ပြန်လည်အသုံးပြုနိုင်ပါပြီ။")

@dp.message(or_f(Command("scam"), F.text.regexp(r"(?i)^\.scam(?:$|\s+)")))
async def add_scam_id(message: types.Message):
    if not await is_authorized(message.from_user.id): 
        return await message.reply("ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ ᴜsᴇʀ.")
        
    parts = message.text.strip().split()
    if len(parts) < 2:
        return await message.reply("⚠️ **Usage:** `.scam <Game_ID>`\nဥပမာ: `.scam 123456789`")
        
    scam_id = parts[1].strip()
    if not scam_id.isdigit():
        return await message.reply("❌ Invalid Game ID. ဂဏန်းများသာ ရိုက်ထည့်ပါ။")
        
    await db.add_scammer(scam_id)
    config.GLOBAL_SCAMMERS.add(scam_id)
    
    await message.reply(f"🚨 **Scammer ID Added:** <code>{scam_id}</code>\n✅ ဤ ID ကို Blacklist သို့ ထည့်သွင်းပြီးပါပြီ။ တွေ့တာနဲ့ Bot မှ အလိုအလျောက် သတိပေးပါတော့မည်။", parse_mode=ParseMode.HTML)

@dp.message(or_f(Command("unscam"), F.text.regexp(r"(?i)^\.unscam(?:$|\s+)")))
async def remove_scam_id(message: types.Message):
    if not await is_authorized(message.from_user.id): 
        return await message.reply("ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ ᴜsᴇʀ.")
        
    parts = message.text.strip().split()
    if len(parts) < 2:
        return await message.reply("⚠️ **Usage:** `.unscam <Game_ID>`")
        
    scam_id = parts[1].strip()
    
    removed = await db.remove_scammer(scam_id)
    config.GLOBAL_SCAMMERS.discard(scam_id)
    
    if removed:
        await message.reply(f"✅ **Scammer ID Removed:** <code>{scam_id}</code>\nBlacklist ထဲမှ အောင်မြင်စွာ ဖယ်ရှားလိုက်ပါပြီ။", parse_mode=ParseMode.HTML)
    else:
        await message.reply(f"⚠️ ထို ID သည် Scammer စာရင်းထဲတွင် မရှိပါ။")

@dp.message(or_f(Command("scamlist"), F.text.regexp(r"(?i)^\.scamlist$")))
async def show_scam_list(message: types.Message):
    if not await is_authorized(message.from_user.id): 
        return await message.reply("ɴᴏᴛ ᴀᴜᴛʜᴏʀɪᴢᴇᴅ ᴜsᴇʀ.")
        
    if not config.GLOBAL_SCAMMERS:
        return await message.reply("✅ ယခုလောလောဆယ် Blacklist သွင်းထားသော Scammer မရှိပါ။")
        
    scam_text = "\n".join([f"🔸 <code>{sid}</code>" for sid in config.GLOBAL_SCAMMERS])
    await message.reply(f"🚨 **Scammer Blacklist (Total: {len(config.GLOBAL_SCAMMERS)}):**\n\n{scam_text}", parse_mode=ParseMode.HTML)

@dp.message(or_f(Command("help"), F.text.regexp(r"(?i)^\.help$")))
async def send_help_message(message: types.Message):
    is_owner = (message.from_user.id == OWNER_ID)
    
    help_text = (
        f"<blockquote><b>🤖 𝐁𝐎𝐓 𝐂𝐎𝐌𝐌𝐀𝐍𝐃𝐒 𝐌𝐄𝐍𝐔</b>\n"
        f"━━━━━━━━━━━━━━━━━\n\n"
        f"<b>💎 𝐌𝐋𝐁Ｂ 𝐃𝐢𝐚𝐦𝐨𝐧𝐝𝐬 (ဝယ်ယူရန်)</b>\n"
        f"🇧🇷 BR MLBB: <code>msc/mlb/br/b ID (Zone) Pack</code>\n"
        f"🇵🇭 PH MLBB: <code>mlp/ph/p ID (Zone) Pack</code>\n\n"
        f"<b>♟️ 𝐌𝐚𝐠𝐢𝐜 𝐂𝐡𝐞𝐬𝐬 (ဝယ်ယူရန်)</b>\n"
        f"🇧🇷 BR MCC: <code>mcc/mcb ID (Zone) Pack</code>\n"
        f"🇵🇭 PH MCC: <code>mcp ID (Zone) Pack</code>\n"
        f"━━━━━━━━━━━━━━━━━\n\n"
        f"<b>👤 𝐔𝐬𝐞𝐫 𝐓𝐨𝐨𝐥𝐬 (အသုံးပြုသူများအတွက်)</b>\n"
        f"🔸 <code>.topup Code</code>        : Smile Code ဖြည့်သွင်းရန်\n"
        f"🔹 <code>.bal</code>      : မိမိ Wallet Balance စစ်ရန်\n"
        f"🔹 <code>.role</code>     : Game ID နှင့် Region စစ်ရန်\n"
        f"🔹 <code>.his</code>      : မိမိဝယ်ယူခဲ့သော မှတ်တမ်းကြည့်ရန်\n"
        f"🔹 <code>.clean</code>    : မှတ်တမ်းများ ဖျက်ရန်\n"
        f"🔹 <code>.listb</code>     : BR ဈေးနှုန်းစာရင်း ကြည့်ရန်\n"
        f"🔹 <code>.listp</code>     : PH ဈေးနှုန်းစာရင်း ကြည့်ရန်\n"
        f"🔹 <code>.listmb</code>    : MCC ဈေးနှုန်းစာရင်း ကြည့်ရန်\n"
        f"💡 <i>Tip: 50+50 ဟုရိုက်ထည့်၍ ဂဏန်းပေါင်းစက်အဖြစ် သုံးနိုင်ပါသည်။</i>\n"
    )
    
    if is_owner:
        help_text += (
            f"\n━━━━━━━━━━━━━━━━━\n"
            f"<b>👑 𝐎𝐰𝐧𝐞𝐫 𝐓𝐨𝐨𝐥𝐬 (Admin သီးသန့်)</b>\n\n"
            f"<b>👥 ယူဆာစီမံခန့်ခွဲမှု</b>\n"
            f"🔸 <code>.maintenance [ᴇɴᴀʙʟᴇ/ᴅɪsᴀʙʟᴇ]</code> : ᴇɴᴀʙʟᴇ ᴏʀ ᴅɪsᴀʙʟᴇ ᴛʜᴇ ᴍᴀɪɴᴛᴇɴᴀɴᴄᴇ ᴍᴏᴅᴇ ᴏғ ʏᴏᴜʀ ʙᴏᴛ.\n"
            f"🔸 <code>.add ID</code>    : User အသစ်ထည့်ရန်\n"
            f"🔸 <code>.remove ID</code> : User အား ဖယ်ရှားရန်\n"
            f"🔸 <code>.users</code>     : User စာရင်းအားလုံး ကြည့်ရန်\n\n"
            f"🔸 <code>.addbal ID 50 BR</code>  : Balance ပေါင်းထည့်ရန်\n"
            f"🔸 <code>.deduct ID 50 BR</code>  : Balance နှုတ်ယူရန်\n"
            f"<b>💼 VIP နှင့် စာရင်းစစ်</b>\n"
            f"🔸 <code>.checkcus ID</code> : Official မှတ်တမ်း လှမ်းစစ်ရန်\n"
            f"🔸 <code>.topcus</code>      : ငွေအများဆုံးသုံးထားသူများ ကြည့်ရန်\n"
            f"🔸 <code>.setvip ID</code>   : VIP အဖြစ် သတ်မှတ်ရန်/ဖြုတ်ရန်\n\n"
            f"<b>🚨 Scammer စီမံခန့်ခွဲမှု</b>\n"
            f"🔸 <code>.scam ID</code>     : Scammer စာရင်းသွင်းရန်\n"
            f"🔸 <code>.unscam ID</code>   : Scammer စာရင်းမှပယ်ဖျက်ရန်\n"
            f"🔸 <code>.scamlist</code>    : Scammer အားလုံးကြည့်ရန်\n\n"
            f"<b>⚙️ System Setup</b>\n"
            f"🔸 <code>.sysbal</code>      : စနစ်တစ်ခုလုံး၏ Balance စစ်ရန်\n"
            f"🔸 <code>.cookies</code>     : Cookie အခြေအနေ စစ်ဆေးရန်\n"
            f"🔸 <code>/setcookie</code>   : Main Cookie အသစ်ပြောင်းရန်\n"
        )
        
    help_text += f"</blockquote>"
    
    await message.reply(help_text, parse_mode=ParseMode.HTML)

@dp.message(Command("start"))
async def send_welcome(message: types.Message):
    try:
        tg_id = str(message.from_user.id)
        first_name = message.from_user.first_name or ""
        last_name = message.from_user.last_name or ""
        full_name = f"{first_name} {last_name}".strip() or "User"
        safe_full_name = full_name.replace('<', '').replace('>', '')
        username_display = f'<a href="tg://user?id={tg_id}">{safe_full_name}</a>'
        
        EMOJI_1, EMOJI_2, EMOJI_3, EMOJI_4, EMOJI_5 = "5956355397366320202", "5954097490109140119", "5958289678837746828", "5956330306167376831", "5954078884310814346"

        status = "🟢 Aᴄᴛɪᴠᴇ" if await is_authorized(message.from_user.id) else "🔴 Nᴏᴛ Aᴄᴛɪᴠᴇ"
        
        welcome_text = (
            f"ʜᴇʏ ʙᴀʙʏ <tg-emoji emoji-id='{EMOJI_1}'>🥺</tg-emoji>\n\n"
            f"<tg-emoji emoji-id='{EMOJI_2}'>👤</tg-emoji> {'Usᴇʀɴᴀᴍᴇ' :<11}: {username_display}\n"
            f"<tg-emoji emoji-id='{EMOJI_3}'>🆔</tg-emoji> {'𝐈𝐃' :<11}: <code>{tg_id}</code>\n"
            f"<tg-emoji emoji-id='{EMOJI_4}'>📊</tg-emoji> {'Sᴛᴀᴛᴜs' :<11}: {status}\n\n"
            f"<tg-emoji emoji-id='{EMOJI_5}'>📞</tg-emoji> {'Cᴏɴᴛᴀᴄᴛ ᴜs' :<11}: @Julierbo2_151102"
        )
        await message.reply(welcome_text, parse_mode=ParseMode.HTML)
    except Exception:
        fallback_text = (
            f"ʜᴇʏ ʙᴀʙʏ 🥺\n\n"
            f"👤 {'Usᴇʀɴᴀᴍᴇ' :<11}: {full_name}\n"
            f"🆔 {'𝐈𝐃' :<11}: <code>{tg_id}</code>\n"
            f"📊 {'Sᴛᴀᴛᴜs' :<11}: 🔴 Nᴏᴛ Aᴄᴛɪᴠᴇ\n\n"
            f"📞 {'Cᴏɴᴛᴀᴄᴛ ᴜs' :<11}: @Julierbo2_151102"
        )
        await message.reply(fallback_text, parse_mode=ParseMode.HTML)
