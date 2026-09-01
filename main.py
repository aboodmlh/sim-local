import requests
import uuid
import time
import io
import os

try:
    import qrcode
    from PIL import Image
    HAS_QR = True
except ImportError:
    HAS_QR = False
    print("⚠️  مكتبة qrcode مش موجودة → هيبعت نص بس")
    print("   نزّلها: pip install qrcode[pil]")

BASE_URL = "https://api.gw-ecommerce.esimulatorservices.com"

# ================== إعدادات ==================
EMAILS_FILE = "emails.txt"          # ملف الإيميلات
DELAY_BETWEEN_ORDERS = int(os.getenv("DELAY_BETWEEN_ORDERS", "3"))  # ثواني
VOUCHER_CODE = "EXPERTAFRICA"
SKU = "ESFX-1681-SL1"

def send_telegram_message(message, token, chat_id):
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        return r.status_code == 200
    except:
        return False

def send_telegram_photo(photo_bytes, caption, token, chat_id):
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    files = {"photo": ("qrcode.png", photo_bytes, "image/png")}
    data = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
    try:
        r = requests.post(url, data=data, files=files, timeout=20)
        return r.status_code == 200
    except Exception as e:
        print(f"❌ خطأ إرسال صورة: {e}")
        return False

def generate_qr_image(qr_string):
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=2)
    qr.add_data(qr_string)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
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
    s = requests.Session()
    s.headers.update(get_headers())
    return s


def respect_rate_limit(response, fallback=5):
    """Respect Retry-After when the server returns HTTP 429."""
    if response.status_code != 429:
        return False

    value = response.headers.get("Retry-After")
    try:
        delay = max(1, int(value)) if value else fallback
    except (TypeError, ValueError):
        delay = fallback

    print(f"   ⏳ Rate limit (429) — انتظار {delay} ثانية")
    time.sleep(delay)
    return True

def get_order_status(session, order_id):
    try:
        r = session.get(f"{BASE_URL}/ecommerce/orders/{order_id}/status", timeout=15)
        if r.status_code == 200:
            return r.text.strip().strip('"')
    except:
        pass
    return None

def get_full_order(session, order_id):
    r = session.get(f"{BASE_URL}/ecommerce/orders/{order_id}", timeout=15)
    if r.status_code == 200:
        return r.json()
    return None

def wait_for_esim(session, order_id, max_wait=120, interval=3):
    print(f"   [انتظار] جاري تجهيز الشريحة...")
    start = time.time()
    last_status = None

    while time.time() - start < max_wait:
        status = get_order_status(session, order_id)
        if status and status != last_status:
            print(f"      → {status}")
            last_status = status

        if status == "COMPLETED":
            order = get_full_order(session, order_id)
            if order and order.get("items"):
                item = order["items"][0]
                if item.get("qr_code_string") or item.get("activation_key"):
                    return order
        elif status in ("FAILED", "CANCELLED", "REFUNDED"):
            return None

        time.sleep(interval)

    return get_full_order(session, order_id)

def buy_one(email, voucher_code, bot_token, chat_id, sku=SKU):
    session = create_session()

    # 1. إنشاء أوردر
    create_payload = {
        "locale": "en",
        "currency": {"id": "USD", "alias": "USD", "description": "currency.usd", "display_order": None},
        "items": [{"sku": sku}],
        "campaign_parameters": None
    }
    r = session.post(f"{BASE_URL}/ecommerce/orders", json=create_payload, timeout=20)
    if r.status_code != 200:
        respect_rate_limit(r)
        print(f"   ❌ فشل إنشاء الأوردر: {r.status_code}")
        return None

    order = r.json()
    order_id = order["id"]
    print(f"   ✅ أوردر: {order_id}")

    # 2. CARD (PaymentIntent)
    card_payload = {
        "id": order_id,
        "updated_date": order["updated_date"],
        "external_user_email": None,
        "payments": [{"payment_type": {"id": "CARD"}, "price": 14.75}]
    }
    r = session.put(f"{BASE_URL}/ecommerce/orders", json=card_payload, timeout=20)
    if r.status_code != 200:
        respect_rate_limit(r)
        print(f"   ❌ فشل PaymentIntent: HTTP {r.status_code}")
        return None
    order = r.json()

    # 3. VOUCHER
    voucher_payload = {
        "id": order_id,
        "updated_date": order["updated_date"],
        "external_user_email": None,
        "payments": [{
            "payment_type": {"id": "VOUCHER"},
            "payment_reference": voucher_code,
            "price": 14.75
        }]
    }
    r = session.put(f"{BASE_URL}/ecommerce/orders", json=voucher_payload, timeout=20)
    if r.status_code != 200:
        respect_rate_limit(r)
        print(f"   ❌ فشل الفoucher: HTTP {r.status_code}")
        return None
    order = r.json()
    print(f"   ✅ فoucher مطبق | discounted: {order.get('discounted_price')}")

    # 4. Complete
    r = session.put(f"{BASE_URL}/ecommerce/orders/{order_id}/complete",
                    json={"external_user_email": email}, timeout=20)
    if r.status_code != 200:
        respect_rate_limit(r)
        print(f"   ❌ فشل Complete: HTTP {r.status_code}")
        return None
    order = r.json()
    print(f"   ✅ Complete → {order['status']['id']}")

    # 5. انتظار الشريحة
    final_order = wait_for_esim(session, order_id)
    if not final_order:
        print(f"   ❌ الشريحة متجهزتش")
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

    print(f"   🎉 نجحت | Status: {status}")
    print(f"      QR: {qr_code[:50] + '...' if qr_code and len(qr_code) > 50 else qr_code}")

    # ========== إرسال تيليجرام ==========
    caption = f"""🎉 <b>تم الشراء بنجاح - Sim Local</b>

👤 <b>Email:</b> <code>{email}</code>
📦 <b>Order ID:</b> <code>{order_id}</code>
📊 <b>Status:</b> {status}

🔑 <b>Activation Key:</b>
<code>{activation or 'None'}</code>

🌐 <b>SMDP+:</b> <code>{smdp or 'None'}</code>
📱 <b>SSN:</b> <code>{ssn or 'None'}</code>
📡 <b>APN:</b> <code>{apn or 'None'}</code>

⏰ Activation: {act_date or 'None'}
⏳ Expiry: {exp_date or 'None'}
🎟 Voucher: {voucher_code}
"""

    if qr_code and HAS_QR:
        try:
            qr_bytes = generate_qr_image(qr_code)
            ok = send_telegram_photo(qr_bytes, caption, bot_token, chat_id)
            if not ok:
                send_telegram_message(caption + f"\n\n🔗 QR:\n<code>{qr_code}</code>", bot_token, chat_id)
        except:
            send_telegram_message(caption + f"\n\n🔗 QR:\n<code>{qr_code}</code>", bot_token, chat_id)
    else:
        msg = caption
        if qr_code:
            msg += f"\n\n🔗 QR:\n<code>{qr_code}</code>"
        send_telegram_message(msg, bot_token, chat_id)

    return {
        "email": email,
        "order_id": order_id,
        "status": status,
        "qr": qr_code,
        "activation": activation
    }

def load_emails(filename):
    if not os.path.exists(filename):
        print(f"❌ الملف {filename} مش موجود!")
        return []
    with open(filename, "r", encoding="utf-8") as f:
        emails = [line.strip() for line in f if line.strip() and "@" in line]
    return emails

# ==================== التشغيل ====================
if __name__ == "__main__":
    print("=" * 60)
    print("🚀 Sim Local Bulk Buyer + Telegram")
    print("=" * 60)

    bot_token = os.getenv("BOT_TOKEN", "").strip()
    chat_id = os.getenv("CHAT_ID", "").strip()

    if not bot_token or not chat_id:
        print("❌ لازم تضيف BOT_TOKEN و CHAT_ID في Railway Variables")
        exit(1)

    emails = load_emails(EMAILS_FILE)
    if not emails:
        print(f"❌ مفيش إيميلات في {EMAILS_FILE}")
        exit()

    print(f"\n📋 لقيت {len(emails)} إيميل")
    print("-" * 40)
    for i, e in enumerate(emails, 1):
        print(f"  {i}. {e}")
    print("-" * 40)

    

    results = []
    success = 0
    failed = 0

    for idx, email in enumerate(emails, 1):
        print(f"\n{'='*60}")
        print(f"[{idx}/{len(emails)}] جاري الشراء لـ: {email}")
        print("="*60)

        result = buy_one(email, VOUCHER_CODE, bot_token, chat_id)

        if result:
            success += 1
            results.append(result)
            print(f"✅ نجح: {email}")
        else:
            failed += 1
            print(f"❌ فشل: {email}")
            # نبعت تنبيه فشل
            send_telegram_message(
                f"❌ <b>فشل الشراء</b>\n\nEmail: <code>{email}</code>",
                bot_token, chat_id
            )

        # تأخير بين العمليات
        if idx < len(emails):
            print(f"\n⏳ انتظار {DELAY_BETWEEN_ORDERS} ثانية قبل اللي بعده...")
            time.sleep(DELAY_BETWEEN_ORDERS)

    # ملخص نهائي
    print("\n" + "="*60)
    print("📊 الملخص النهائي")
    print("="*60)
    print(f"✅ نجح : {success}")
    print(f"❌ فشل : {failed}")
    print(f"📧 الإجمالي: {len(emails)}")
    print("="*60)

    # إرسال ملخص على تيليجرام
    summary = f"""📊 <b>ملخص الشراء الجماعي</b>

✅ نجح: {success}
❌ فشل: {failed}
📧 الإجمالي: {len(emails)}

"""
    for r in results:
        summary += f"• <code>{r['email']}</code> → {r['order_id'][:8]}...\n"

    send_telegram_message(summary, bot_token, chat_id)
    print("\n✅ تم إرسال الملخص على تيليجرام")
