import hashlib
import hmac
import os
from datetime import datetime

LICENSE_SECRET = os.environ.get("LICENSE_SECRET", "LISTIA-GHATTALI-LICENSE-SECURE-KEY-2026")

FREE_MAX_SUPPLIERS = 1
FREE_MAX_PRODUCTS = 5


def normalize_ident(ident):
    if hasattr(ident, "username"):
        ident = ident.username
    return str(ident or "").strip().lower()


def get_user_code(user_or_username):
    """
    تولید شناسه فعال‌سازی کاربر (مثلاً LST-7842-9901)
    این شناسه ثابت و خوانا برای ارسال به پشتیبانی است.
    """
    ident = normalize_ident(user_or_username)
    raw = f"LISTIA-USER:{ident}:{LICENSE_SECRET[:12]}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest().upper()
    return f"LST-{digest[:4]}-{digest[4:8]}"


def generate_key(user_or_ident, tier="PRO"):
    """
    تولید کلید لایسنس معتبر برای کاربر یا کد فعال‌سازی
    فرمت: LST-XXXX-XXXX-XXXX-XXXX
    """
    ident = normalize_ident(user_or_ident)
    user_code = get_user_code(ident)
    tier = str(tier or "PRO").strip().upper()

    msg = f"LISTIA:{ident}:{user_code}:{tier}"
    sig = hmac.new(LICENSE_SECRET.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest().upper()

    return f"LST-{sig[0:4]}-{sig[4:8]}-{sig[8:12]}-{sig[12:16]}"


def generate_master_key(tier="UNLIMITED"):
    """
    تولید یک کلید سراسری (Universal Master Key)
    """
    tier = str(tier or "UNLIMITED").strip().upper()
    msg = f"LISTIA:MASTER_KEY_UNIVERSAL:{tier}"
    sig = hmac.new(LICENSE_SECRET.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest().upper()
    return f"LST-{sig[0:4]}-{sig[4:8]}-{sig[8:12]}-{sig[12:16]}"


def verify_key(user_or_ident, key):
    """
    بررسی صحت کلید لایسنس وارد شده.
    خروجی: (is_valid, tier, message)
    """
    if not key or not isinstance(key, str):
        return False, None, "لطفاً کلید لایسنس را وارد کنید."

    clean_key = key.strip().upper()

    ident = normalize_ident(user_or_ident)
    user_code = get_user_code(ident)

    # بررسی تطابق با نام کاربری یا کد کاربر
    for candidate in [ident, user_code]:
        for tier in ["PRO", "UNLIMITED", "ENTERPRISE"]:
            if clean_key == generate_key(candidate, tier):
                return True, tier, "لایسنس با موفقیت فعال شد."

    # بررسی کلید مستر
    for tier in ["UNLIMITED", "PRO"]:
        if clean_key == generate_master_key(tier):
            return True, tier, "کلید لایسنس سراسری با موفقیت تأیید شد."

    return False, None, "کلید لایسنس وارد شده نامعتبر است یا برای این حساب صادر نشده است."
