import io
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

import requests

try:
    import qrcode
    HAS_QR = True
except ImportError:
    HAS_QR = False

BASE_URL = os.getenv("BASE_URL", "https://api.gw-ecommerce.esimulatorservices.com")
EMAILS_FILE = os.getenv("EMAILS_FILE", "emails.txt")
VOUCHER_CODE = os.getenv("VOUCHER_CODE", "EXPERTAFRICA")
SKU = os.getenv("SKU", "ESFX-1681-SL1")
# The provider may reject bursts with 403. Start conservatively and raise only after testing.
MAX_WORKERS = max(1, int(os.getenv("MAX_WORKERS", "2")))
DELAY_BETWEEN_SUBMISSIONS = max(0, float(os.getenv("DELAY_BETWEEN_SUBMISSIONS", "8")))
CREATE_ORDER_RETRIES = max(0, int(os.getenv("CREATE_ORDER_RETRIES", "2")))
CREATE_ORDER_BACKOFF = max(5, int(os.getenv("CREATE_ORDER_BACKOFF", "30")))
POLL_MAX_WAIT = max(10, int(os.getenv("POLL_MAX_WAIT", "120")))
POLL_INTERVAL = max(1, int(os.getenv("POLL_INTERVAL", "3")))

print_lock = Lock()

def log(message):
    with print_lock:
        print(message, flush=True)

def send_telegram_message(message, token, chat_id):
    if not token or not chat_id:
        return False
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=15,
        )
        return response.status_code == 200
    except requests.RequestException as exc:
        log(f"Telegram message error: {exc}")
        return False

def send_telegram_photo(photo_bytes, caption, token, chat_id):
    if not token or not chat_id:
        return False
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendPhoto",
            files={"photo": ("qrcode.png", photo_bytes, "image/png")},
            data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
            timeout=20,
        )
        return response.status_code == 200
    except requests.RequestException as exc:
        log(f"Telegram photo error: {exc}")
        return False

def generate_qr_image(qr_string):
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=2)
    qr.add_data(qr_string)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()

def get_headers():
    return {
        "x-app-origin": "ANDROID",
        "x-app-version": "26.08.17",
        "x-dapx-session-id": str(uuid.uuid4()),
        "x-dapx-request-id": str(uuid.uuid4()),
        "x-dapx-correlation-id": str(uuid.uuid4()),
        "x-locale": "en",
        "content-type": "application/json",
        "accept": "application/json",
        "origin": "https://shop.simlocal.com",
        "user-agent": "Mozilla/5.0 (Linux; Android 16; SM-S938B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Mobile Safari/537.36",
        "x-requested-with": "com.simlocal.esim.travel",
    }

def create_session():
    session = requests.Session()
    session.headers.update(get_headers())
    return session

def get_order_status(session, order_id):
    try:
        response = session.get(f"{BASE_URL}/ecommerce/orders/{order_id}/status", timeout=15)
        if response.status_code == 200:
            return response.text.strip().strip('"')
    except requests.RequestException:
        pass
    return None

def get_full_order(session, order_id):
    try:
        response = session.get(f"{BASE_URL}/ecommerce/orders/{order_id}", timeout=15)
        if response.status_code == 200:
            return response.json()
    except (requests.RequestException, ValueError):
        pass
    return None

def wait_for_esim(session, order_id):
    log(f"   [{order_id}] انتظار تجهيز الشريحة...")
    started = time.time()
    last_status = None
    while time.time() - started < POLL_MAX_WAIT:
        status = get_order_status(session, order_id)
        if status and status != last_status:
            log(f"   [{order_id}] الحالة: {status}")
            last_status = status
        if status == "COMPLETED":
            order = get_full_order(session, order_id)
            if order and order.get("items"):
                item = order["items"][0]
                if item.get("qr_code_string") or item.get("activation_key"):
                    return order
        elif status in {"FAILED", "CANCELLED", "REFUNDED"}:
            return None
        time.sleep(POLL_INTERVAL)
    return get_full_order(session, order_id)

def buy_one(email, voucher_code, bot_token, chat_id):
    session = create_session()
    try:
        create_payload = {
            "locale": "en",
            "currency": {"id": "USD", "alias": "USD", "description": "currency.usd", "display_order": None},
            "items": [{"sku": SKU}],
            "campaign_parameters": None,
        }
        # إنشاء الأوردر فقط هو الجزء الذي يمكن إعادة محاولته بأمان هنا:
        # إذا رجع 403/429 فلم يتم إنشاء أوردر حسب استجابة الخادم.
        order_response = None
        for attempt in range(CREATE_ORDER_RETRIES + 1):
            try:
                order_response = session.post(f"{BASE_URL}/ecommerce/orders", json=create_payload, timeout=20)
            except requests.RequestException as exc:
                if attempt >= CREATE_ORDER_RETRIES:
                    log(f"[{email}] تعذر إنشاء الأوردر بعد إعادة المحاولة: {exc}")
                    return None
                wait_seconds = CREATE_ORDER_BACKOFF * (2 ** attempt)
                log(f"[{email}] خطأ اتصال؛ إعادة المحاولة بعد {wait_seconds} ثانية")
                time.sleep(wait_seconds)
                continue

            if order_response.status_code == 200:
                break
            if order_response.status_code in {403, 429, 500, 502, 503, 504} and attempt < CREATE_ORDER_RETRIES:
                wait_seconds = CREATE_ORDER_BACKOFF * (2 ** attempt)
                log(f"[{email}] الخادم رفض إنشاء الأوردر ({order_response.status_code})؛ إعادة المحاولة بعد {wait_seconds} ثانية")
                time.sleep(wait_seconds)
                continue
            detail = order_response.text[:200].replace("\\n", " ")
            log(f"[{email}] فشل إنشاء الأوردر: {order_response.status_code} | {detail}")
            return None

        order = order_response.json()
        order_id = order["id"]
        log(f"[{email}] أوردر: {order_id}")

        card_payload = {
            "id": order_id,
            "updated_date": order["updated_date"],
            "external_user_email": None,
            "payments": [{"payment_type": {"id": "CARD"}, "price": 14.75}],
        }
        response = session.put(f"{BASE_URL}/ecommerce/orders", json=card_payload, timeout=20)
        if response.status_code != 200:
            log(f"[{email}] فشل PaymentIntent")
            return None
        order = response.json()

        voucher_payload = {
            "id": order_id,
            "updated_date": order["updated_date"],
            "external_user_email": None,
            "payments": [{"payment_type": {"id": "VOUCHER"}, "payment_reference": voucher_code, "price": 14.75}],
        }
        response = session.put(f"{BASE_URL}/ecommerce/orders", json=voucher_payload, timeout=20)
        if response.status_code != 200:
            log(f"[{email}] فشل تطبيق voucher")
            return None
        order = response.json()
        log(f"[{email}] voucher مطبق | discounted: {order.get('discounted_price')}")

        response = session.put(
            f"{BASE_URL}/ecommerce/orders/{order_id}/complete",
            json={"external_user_email": email},
            timeout=20,
        )
        if response.status_code != 200:
            log(f"[{email}] فشل Complete")
            return None
        order = response.json()
        log(f"[{email}] Complete → {order['status']['id']}")

        final_order = wait_for_esim(session, order_id)
        if not final_order or not final_order.get("items"):
            log(f"[{email}] الشريحة لم تجهز")
            return None

        item = final_order["items"][0]
        status = final_order["status"]["id"]
        qr_code = item.get("qr_code_string")
        activation = item.get("activation_key")
        smdp = item.get("smdp_plus_url")
        ssn = item.get("ssn")
        apn = item.get("apn")
        act_date = item.get("activation_date")
        exp_date = item.get("expiry_date")

        caption = f"""🎉 <b>تم الشراء بنجاح - Sim Local</b>

👤 <b>Email:</b> <code>{email}</code>
📦 <b>Order ID:</b> <code>{order_id}</code>
📊 <b>Status:</b> {status}
🔑 <b>Activation Key:</b> <code>{activation or 'None'}</code>
🌐 <b>SMDP+:</b> <code>{smdp or 'None'}</code>
📱 <b>SSN:</b> <code>{ssn or 'None'}</code>
📡 <b>APN:</b> <code>{apn or 'None'}</code>
⏰ Activation: {act_date or 'None'}
⏳ Expiry: {exp_date or 'None'}
🎟 Voucher: {voucher_code}"""

        if qr_code and HAS_QR:
            sent = send_telegram_photo(generate_qr_image(qr_code), caption, bot_token, chat_id)
            if not sent:
                send_telegram_message(f"{caption}\n\n🔗 QR:\n<code>{qr_code}</code>", bot_token, chat_id)
        else:
            send_telegram_message(f"{caption}{f'\\n\\n🔗 QR:\\n<code>{qr_code}</code>' if qr_code else ''}", bot_token, chat_id)

        log(f"[{email}] نجحت العملية")
        return {"email": email, "order_id": order_id, "status": status, "qr": qr_code, "activation": activation}
    except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
        log(f"[{email}] خطأ: {exc}")
        return None
    finally:
        session.close()

def load_emails(filename):
    if not os.path.exists(filename):
        raise FileNotFoundError(f"الملف {filename} غير موجود")
    with open(filename, "r", encoding="utf-8") as file:
        return [line.strip() for line in file if line.strip() and "@" in line]

def main():
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        raise RuntimeError("اضبط TELEGRAM_BOT_TOKEN و TELEGRAM_CHAT_ID في Railway Variables")

    emails = load_emails(EMAILS_FILE)
    if not emails:
        raise RuntimeError(f"لا توجد إيميلات صالحة في {EMAILS_FILE}")

    workers = min(MAX_WORKERS, len(emails))
    log(f"بدء {len(emails)} عملية باستخدام {workers} جلسات متوازية")
    results = []
    failed_emails = []

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="purchase") as executor:
        futures = {}
        for index, email in enumerate(emails):
            if index and DELAY_BETWEEN_SUBMISSIONS:
                time.sleep(DELAY_BETWEEN_SUBMISSIONS)
            futures[executor.submit(buy_one, email, VOUCHER_CODE, bot_token, chat_id)] = email

        for future in as_completed(futures):
            email = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                log(f"[{email}] فشل غير متوقع: {exc}")
                result = None
            if result:
                results.append(result)
            else:
                failed_emails.append(email)
                send_telegram_message(f"❌ <b>فشل الشراء</b>\n\nEmail: <code>{email}</code>", bot_token, chat_id)

    summary = f"📊 <b>ملخص الشراء الجماعي</b>\n\n✅ نجح: {len(results)}\n❌ فشل: {len(failed_emails)}\n📧 الإجمالي: {len(emails)}\n⚙️ الجلسات المتوازية: {workers}\n\n"
    summary += "".join(f"• <code>{item['email']}</code> → {item['order_id'][:8]}...\n" for item in results)
    send_telegram_message(summary, bot_token, chat_id)
    log(f"انتهى التشغيل: نجح {len(results)} | فشل {len(failed_emails)}")

if __name__ == "__main__":
    main()
