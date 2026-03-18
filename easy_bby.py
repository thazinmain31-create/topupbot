import time
import random
import re
import asyncio
from bs4 import BeautifulSoup
from DrissionPage import ChromiumPage, ChromiumOptions
#from playwright.async_api import async_playwright
from curl_cffi.requests import AsyncSession

import database as db
from config import GOOGLE_EMAIL, GOOGLE_PASS, auth_lock
from helpers import notify_owner

# Scraper Globals (Managed here to avoid circular imports)
last_login_time = 0
GLOBAL_SCRAPER = None
GLOBAL_COOKIE_STR = ""
GLOBAL_CSRF = {'mlbb_br': None, 'mlbb_ph': None, 'mcc_br': None, 'mcc_ph': None}

async def get_main_scraper():
    global GLOBAL_SCRAPER, GLOBAL_COOKIE_STR, GLOBAL_CSRF
    
    raw_cookie = await db.get_main_cookie() or ""
    
    if GLOBAL_SCRAPER is None or raw_cookie != GLOBAL_COOKIE_STR:
        cookie_dict = {}
        if raw_cookie:
            for item in raw_cookie.split(';'):
                if '=' in item:
                    k, v = item.strip().split('=', 1)
                    cookie_dict[k.strip()] = v.strip()
                    
        GLOBAL_SCRAPER = AsyncSession(impersonate="chrome120", cookies=cookie_dict)
        GLOBAL_COOKIE_STR = raw_cookie
        GLOBAL_CSRF = {'mlbb_br': None, 'mlbb_ph': None, 'mcc_br': None, 'mcc_ph': None}
        
    return GLOBAL_SCRAPER

def _sync_drission_login(email, password):
    try:
        co = ChromiumOptions()
        co.set_argument('--no-sandbox')
        co.set_argument('--disable-setuid-sandbox')
        co.set_argument('--disable-blink-features=AutomationControlled')
        co.set_user_agent("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        
        co.headless(True) 

        page = ChromiumPage(co)
        page.get("https://www.smile.one/customer/login")
        page.wait(5)
        
        sign_in_btn = page.ele('text=Sign in with Google')
        if sign_in_btn:
            sign_in_btn.click()
        
        page.wait.new_tab()
        google_tab = page.get_tab(page.latest_tab)
        
        google_tab.wait(2)
        google_tab.ele('input[type="email"]').input(email)
        google_tab.wait(1)
        google_tab.ele('input[type="email"]').type('\n') 
        
        google_tab.wait(4)
        google_tab.ele('input[type="password"]').input(password)
        google_tab.wait(1)
        google_tab.ele('input[type="password"]').type('\n') 
        
        page.wait.url_change("customer/order", timeout=30)
        
        cookies_dict = page.cookies(as_dict=True)
        raw_cookie_str = "; ".join([f"{k}={v}" for k, v in cookies_dict.items()])
        
        page.quit()
        return raw_cookie_str
        
    except Exception as e:
        print(f"DrissionPage Login Error: {e}")
        try:
            page.quit()
        except:
            pass
        return None

async def auto_login_and_get_cookie():
    global last_login_time, GLOBAL_SCRAPER, GLOBAL_CSRF
    
    if not GOOGLE_EMAIL or not GOOGLE_PASS:
        print("❌ GOOGLE_EMAIL and GOOGLE_PASS are missing in .env.")
        return False
        
    async with auth_lock:
        if time.time() - last_login_time < 120:
            return True

        print("Logging in with Google to fetch new Cookie using DrissionPage...")
        
        loop = asyncio.get_running_loop()
        new_cookie_str = await loop.run_in_executor(None, _sync_drission_login, GOOGLE_EMAIL, GOOGLE_PASS)
        
        if new_cookie_str:
            print("✅ Auto-Login (Google) successful. Saving Cookie...")
            await db.update_main_cookie(new_cookie_str)
            last_login_time = time.time()
            GLOBAL_SCRAPER = None
            GLOBAL_CSRF = {'mlbb_br': None, 'mlbb_ph': None, 'mcc_br': None, 'mcc_ph': None}
            return True
        else:
            print("❌ Did not reach the Order page. (Google blocked or Checkpoint)")
            return False

async def get_smile_balance(scraper, headers, balance_url='https://www.smile.one/customer/order'):
    balances = {'br_balance': 0.00, 'ph_balance': 0.00}
    try:
        response = await scraper.get(balance_url, headers=headers, timeout=15)
        
        br_match = re.search(r'(?i)(?:Balance|Saldo)[\s:]*?<\/p>\s*<p>\s*([\d\.,]+)', response.text)
        if br_match: balances['br_balance'] = float(br_match.group(1).replace(',', ''))
        else:
            soup = BeautifulSoup(response.text, 'html.parser')
            main_balance_div = soup.find('div', class_='balance-coins')
            if main_balance_div:
                p_tags = main_balance_div.find_all('p')
                if len(p_tags) >= 2: balances['br_balance'] = float(p_tags[1].text.strip().replace(',', ''))
                    
        ph_match = re.search(r'(?i)Saldo PH[\s:]*?<\/span>\s*<span>\s*([\d\.,]+)', response.text)
        if ph_match: balances['ph_balance'] = float(ph_match.group(1).replace(',', ''))
        else:
            soup = BeautifulSoup(response.text, 'html.parser')
            ph_balance_container = soup.find('div', id='all-balance')
            if ph_balance_container:
                span_tags = ph_balance_container.find_all('span')
                if len(span_tags) >= 2: balances['ph_balance'] = float(span_tags[1].text.strip().replace(',', ''))
    except Exception as e: 
        print(f"Error fetching balance from site: {e}")
    return balances

async def process_smile_one_order(game_id, zone_id, product_id, currency_name, prev_context=None, skip_role_check=False, known_ig_name="Unknown", last_success_order_id=""):
    scraper = await get_main_scraper()
    global GLOBAL_CSRF
    cache_key = f"mlbb_{currency_name.lower()}"

    if currency_name == 'PH':
        main_url = 'https://www.smile.one/ph/merchant/mobilelegends'
        checkrole_url = 'https://www.smile.one/ph/merchant/mobilelegends/checkrole'
        query_url = 'https://www.smile.one/ph/merchant/mobilelegends/query'
        pay_url = 'https://www.smile.one/ph/merchant/mobilelegends/pay'
        order_api_url = 'https://www.smile.one/ph/customer/activationcode/codelist'
    else:
        main_url = 'https://www.smile.one/merchant/mobilelegends'
        checkrole_url = 'https://www.smile.one/merchant/mobilelegends/checkrole'
        query_url = 'https://www.smile.one/merchant/mobilelegends/query'
        pay_url = 'https://www.smile.one/merchant/mobilelegends/pay'
        order_api_url = 'https://www.smile.one/customer/activationcode/codelist'
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'X-Requested-With': 'XMLHttpRequest', 
        'Referer': main_url, 
        'Origin': 'https://www.smile.one'
    }

    try:
        csrf_token = prev_context.get('csrf_token') if prev_context else GLOBAL_CSRF.get(cache_key)
        ig_name = known_ig_name

        if not csrf_token:
            response = await scraper.get(main_url, headers=headers)
            soup = BeautifulSoup(response.text, 'html.parser')
            meta_tag = soup.find('meta', {'name': 'csrf-token'})
            csrf_token = meta_tag.get('content') if meta_tag else (soup.find('input', {'name': '_csrf'}).get('value') if soup.find('input', {'name': '_csrf'}) else None)
            if not csrf_token: return {"status": "error", "message": "CSRF Token not found. Re-add Cookie.", "ig_name": ig_name}
            GLOBAL_CSRF[cache_key] = csrf_token

        async def get_flow_id():
            query_data = {'user_id': game_id, 'zone_id': zone_id, 'pid': product_id, 'checkrole': '', 'pay_methond': 'smilecoin', 'channel_method': 'smilecoin', '_csrf': csrf_token}
            return await scraper.post(query_url, data=query_data, headers=headers)

        async def check_role():
            check_data = {'user_id': game_id, 'zone_id': zone_id, '_csrf': csrf_token}
            return await scraper.post(checkrole_url, data=check_data, headers=headers)

        if skip_role_check:
            query_response_raw = await get_flow_id()
        else:
            query_response_raw, role_response_raw = await asyncio.gather(get_flow_id(), check_role())
            try:
                role_result = role_response_raw.json()
                fetched_name = role_result.get('username') or role_result.get('data', {}).get('username')
                if fetched_name and str(fetched_name).strip() != "":
                    ig_name = str(fetched_name).strip()
                else:
                    return {"status": "error", "message": "❌ Invalid Account: Account not found.", "ig_name": "Unknown"}
            except Exception: 
                return {"status": "error", "message": "Check Role API Error.", "ig_name": ig_name}

        try: query_result = query_response_raw.json()
        except Exception: return {"status": "error", "message": "Query API Error", "ig_name": ig_name}
            
        flowid = query_result.get('flowid') or query_result.get('data', {}).get('flowid')
        
        if not flowid:
            real_error = query_result.get('msg') or query_result.get('message') or query_result.get('info') or ""
            
            if "login" in str(real_error).lower() or "unauthorized" in str(real_error).lower():
                GLOBAL_CSRF[cache_key] = None
                await notify_owner("⚠️ <b>Order Alert:</b> Cookie expired. Auto-login started...")
                success = await auto_login_and_get_cookie()
                if success: return {"status": "error", "message": "Session renewed. Please try again.", "ig_name": ig_name}
                else: return {"status": "error", "message": "❌ Auto-Login failed. Please /setcookie.", "ig_name": ig_name}
                
            return {"status": "error", "message": str(real_error), "ig_name": ig_name}

        pay_data = {'_csrf': csrf_token, 'user_id': game_id, 'zone_id': zone_id, 'pay_methond': 'smilecoin', 'product_id': product_id, 'channel_method': 'smilecoin', 'flowid': flowid, 'email': '', 'coupon_id': ''}
        pay_response_raw = await scraper.post(pay_url, data=pay_data, headers=headers)
        pay_text = pay_response_raw.text.lower()
        
        if "saldo insuficiente" in pay_text or "insufficient" in pay_text:
            return {"status": "error", "message": "Insufficient balance in the Main account.", "ig_name": ig_name}
        
        real_order_id, is_success = "Not found", False
        actual_product_name = ""

        try:
            pay_json = pay_response_raw.json()
            status_val = str(pay_json.get('status', ''))
            code = str(pay_json.get('code', status_val))
            
            msg = str(pay_json.get('msg') or pay_json.get('message') or pay_json.get('info') or "").lower()
            
            if code in ['200', '0', '1'] or 'success' in msg: 
                is_success = True
                _id = str(pay_json.get('data', {}).get('order_id') or pay_json.get('order_id') or pay_json.get('increment_id') or "")
                if not _id or _id == "None":
                    _id = f"FAST_{int(time.time())}_{random.randint(100,999)}"
                real_order_id = _id
        except:
            if 'success' in pay_text or 'sucesso' in pay_text: 
                is_success = True
                real_order_id = f"FAST_{int(time.time())}_{random.randint(100,999)}"

        if not is_success:
            try:
                hist_res_raw = await scraper.get(order_api_url, params={'type': 'orderlist', 'p': '1', 'pageSize': '5'}, headers=headers)
                hist_json = hist_res_raw.json()
                if 'list' in hist_json and len(hist_json['list']) > 0:
                    for order in hist_json['list']:
                        if str(order.get('user_id')) == str(game_id) and str(order.get('server_id')) == str(zone_id):
                            current_order_id = str(order.get('increment_id', ""))
                            if current_order_id != last_success_order_id:
                                if str(order.get('order_status', '')).lower() in ['success', '1'] or str(order.get('status')) == '1':
                                    real_order_id = current_order_id
                                    actual_product_name = str(order.get('product_name', ''))
                                    is_success = True
                                    break
            except: pass

        if is_success:
            return {"status": "success", "ig_name": ig_name, "order_id": real_order_id, "csrf_token": csrf_token, "product_name": actual_product_name}
        else:
            error_detail = pay_json.get('msg') or pay_json.get('message') or pay_json.get('info') if 'pay_json' in locals() else "Payment Verification Failed."
            return {"status": "error", "message": str(error_detail), "ig_name": ig_name}

    except Exception as e: 
        return {"status": "error", "message": f"System Error: {str(e)}", "ig_name": known_ig_name}

async def process_mcc_order(game_id, zone_id, product_id, currency_name, prev_context=None, skip_role_check=False, known_ig_name="Unknown", last_success_order_id=""):
    scraper = await get_main_scraper()
    global GLOBAL_CSRF
    cache_key = f"mcc_{currency_name.lower()}"

    if currency_name == 'PH':
        main_url = 'https://www.smile.one/ph/merchant/game/magicchessgogo'
        checkrole_url = 'https://www.smile.one/ph/merchant/game/checkrole'
        query_url = 'https://www.smile.one/ph/merchant/game/query'
        pay_url = 'https://www.smile.one/ph/merchant/game/pay'
        order_api_url = 'https://www.smile.one/ph/customer/activationcode/codelist'
    else:
        main_url = 'https://www.smile.one/br/merchant/game/magicchessgogo'
        checkrole_url = 'https://www.smile.one/br/merchant/game/checkrole'
        query_url = 'https://www.smile.one/br/merchant/game/query'
        pay_url = 'https://www.smile.one/br/merchant/game/pay'
        order_api_url = 'https://www.smile.one/br/customer/activationcode/codelist'
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'X-Requested-With': 'XMLHttpRequest', 
        'Referer': main_url, 
        'Origin': 'https://www.smile.one'
    }

    try:
        csrf_token = prev_context.get('csrf_token') if prev_context else GLOBAL_CSRF.get(cache_key)
        ig_name = known_ig_name
        
        if not csrf_token:
            response = await scraper.get(main_url, headers=headers)
            if response.status_code in [403, 503] or "cloudflare" in response.text.lower():
                 return {"status": "error", "message": "Blocked by Cloudflare.", "ig_name": ig_name}

            soup = BeautifulSoup(response.text, 'html.parser')
            meta_tag = soup.find('meta', {'name': 'csrf-token'})
            csrf_token = meta_tag.get('content') if meta_tag else (soup.find('input', {'name': '_csrf'}).get('value') if soup.find('input', {'name': '_csrf'}) else None)
            if not csrf_token: return {"status": "error", "message": "CSRF Token not found. Add a new Cookie using /setcookie.", "ig_name": ig_name}
            GLOBAL_CSRF[cache_key] = csrf_token

        async def get_flow_id():
            query_data = {'user_id': game_id, 'zone_id': zone_id, 'pid': product_id, 'checkrole': '', 'pay_methond': 'smilecoin', 'channel_method': 'smilecoin', '_csrf': csrf_token}
            return await scraper.post(query_url, data=query_data, headers=headers)

        async def check_role():
            check_data = {'user_id': game_id, 'zone_id': zone_id, '_csrf': csrf_token}
            return await scraper.post(checkrole_url, data=check_data, headers=headers)

        if skip_role_check:
            query_response_raw = await get_flow_id()
        else:
            query_response_raw, role_response_raw = await asyncio.gather(get_flow_id(), check_role())
            try:
                role_result = role_response_raw.json()
                fetched_name = role_result.get('username') or role_result.get('data', {}).get('username')
                if fetched_name and str(fetched_name).strip() != "":
                    ig_name = str(fetched_name).strip()
                else:
                    return {"status": "error", "message": "Account not found.", "ig_name": "Unknown"}
            except Exception: 
                return {"status": "error", "message": "⚠️ Check Role API Error.", "ig_name": ig_name}

        try: query_result = query_response_raw.json()
        except Exception: return {"status": "error", "message": "Query API Error", "ig_name": ig_name}
            
        flowid = query_result.get('flowid') or query_result.get('data', {}).get('flowid')
        
        if not flowid:
            real_error = query_result.get('msg') or query_result.get('message') or query_result.get('info') or ""
            if "login" in str(real_error).lower() or "unauthorized" in str(real_error).lower():
                GLOBAL_CSRF[cache_key] = None
                await notify_owner("⚠️ <b>Order Alert:</b> Cookie expired. Auto-login started...")
                success = await auto_login_and_get_cookie()
                if success:
                    return {"status": "error", "message": "Session renewed. Please enter the command again.", "ig_name": ig_name}
                else: 
                    return {"status": "error", "message": "❌ Auto-Login failed. Please provide /setcookie.", "ig_name": ig_name}
            
            error_display = str(real_error) if real_error else "Invalid account or unable to purchase."
            return {"status": "error", "message": error_display, "ig_name": ig_name}

        pay_data = {'_csrf': csrf_token, 'user_id': game_id, 'zone_id': zone_id, 'pay_methond': 'smilecoin', 'product_id': product_id, 'channel_method': 'smilecoin', 'flowid': flowid, 'email': '', 'coupon_id': ''}
        pay_response_raw = await scraper.post(pay_url, data=pay_data, headers=headers)
        pay_text = pay_response_raw.text.lower()
        
        if "saldo insuficiente" in pay_text or "insufficient" in pay_text:
            return {"status": "error", "message": "Insufficient balance in the Main account.", "ig_name": ig_name}
        
        real_order_id, is_success = "Not found", False
        actual_product_name = ""

        try:
            pay_json = pay_response_raw.json()
            status_val = str(pay_json.get('status', ''))
            code = str(pay_json.get('code', status_val))
            msg = str(pay_json.get('msg') or pay_json.get('message') or pay_json.get('info') or "").lower()
            
            if code in ['200', '0', '1'] or 'success' in msg: 
                is_success = True
                _id = str(pay_json.get('data', {}).get('order_id') or pay_json.get('order_id') or pay_json.get('increment_id') or "")
                if not _id or _id == "None":
                    _id = f"FAST_{int(time.time())}_{random.randint(100,999)}"
                real_order_id = _id
        except:
            if 'success' in pay_text or 'sucesso' in pay_text: 
                is_success = True
                real_order_id = f"FAST_{int(time.time())}_{random.randint(100,999)}"

        if not is_success:
            try:
                hist_res_raw = await scraper.get(order_api_url, params={'type': 'orderlist', 'p': '1', 'pageSize': '5'}, headers=headers)
                hist_json = hist_res_raw.json()
                if 'list' in hist_json and len(hist_json['list']) > 0:
                    for order in hist_json['list']:
                        if str(order.get('user_id')) == str(game_id) and str(order.get('server_id')) == str(zone_id):
                            current_order_id = str(order.get('increment_id', ""))
                            if current_order_id != last_success_order_id:
                                if str(order.get('order_status', '')).lower() in ['success', '1'] or str(order.get('status')) == '1':
                                    real_order_id = current_order_id
                                    actual_product_name = str(order.get('product_name', ''))
                                    is_success = True
                                    break
            except: pass

        if is_success:
            return {"status": "success", "ig_name": ig_name, "order_id": real_order_id, "csrf_token": csrf_token, "product_name": actual_product_name}
        else:
            error_detail = pay_json.get('msg') or pay_json.get('message') or pay_json.get('info') if 'pay_json' in locals() else "Payment Verification Failed."
            return {"status": "error", "message": str(error_detail), "ig_name": ig_name}

    except Exception as e: 
        return {"status": "error", "message": f"System Error: {str(e)}", "ig_name": known_ig_name}