import hashlib
import hmac
import os
from datetime import datetime, timedelta

LICENSE_SECRET = os.environ.get("LICENSE_SECRET", "LISTIA-GHATTALI-LICENSE-SECURE-KEY-2026")

FREE_MAX_SUPPLIERS = 1
FREE_MAX_PRODUCTS = 5

DURATION_CHOICES = {
    "LIFE": {"label": "مادام‌العمر (دائمی)", "days": None},
    "30D": {"label": "۱ ماهه (۳۰ روز)", "days": 30},
    "90D": {"label": "۳ ماهه (۹۰ روز)", "days": 90},
    "180D": {"label": "۶ ماهه (۱۸۰ روز)", "days": 180},
    "365D": {"label": "۱ ساله (۳۶۵ روز)", "days": 365},
}


def normalize_ident(ident):
    if hasattr(ident, "username"):
        ident = ident.username
    return str(ident or "").strip().lower()


def normalize_duration(duration):
    """
    نرمال‌سازی مدت زمان به کد استاندارد و تعداد روزها
    خروجی: (code, days, label)
    """
    if duration is None:
        return "LIFE", None, "مادام‌العمر (دائمی)"

    d_str = str(duration).strip().upper()
    if d_str in ["LIFE", "LIFETIME", "PERMANENT", "0", "NONE"]:
        return "LIFE", None, "مادام‌العمر (دائمی)"

    if d_str in DURATION_CHOICES:
        item = DURATION_CHOICES[d_str]
        return d_str, item["days"], item["label"]

    if d_str.startswith("D") and d_str[1:].isdigit():
        days = int(d_str[1:])
        return f"{days}D", days, f"{days} روزه"

    if d_str.isdigit():
        days = int(d_str)
        if days <= 0:
            return "LIFE", None, "مادام‌العمر (دائمی)"
        return f"{days}D", days, f"{days} روزه"

    return "LIFE", None, "مادام‌العمر (دائمی)"


def get_user_code(user_or_username):
    """
    تولید شناسه فعال‌سازی کاربر (مثلاً LST-7842-9901)
    این شناسه ثابت و خوانا برای ارسال به پشتیبانی است.
    """
    ident = normalize_ident(user_or_username)
    raw = f"LISTIA-USER:{ident}:{LICENSE_SECRET[:12]}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest().upper()
    return f"LST-{digest[:4]}-{digest[4:8]}"


def generate_key(user_or_ident, tier="PRO", duration="LIFE"):
    """
    تولید کلید لایسنس معتبر برای کاربر با مدت زمان دلخواه یا مادام‌العمر
    فرمت: LST-<PERIOD>-<SIG1>-<SIG2>-<SIG3>
    مثال: LST-LIFE-9A2F-84E1-7B3C یا LST-30D-8F1A-2C3D-4E5F
    """
    ident = normalize_ident(user_or_ident)
    user_code = get_user_code(ident)
    tier = str(tier or "PRO").strip().upper()
    period_code, days, label = normalize_duration(duration)

    msg = f"LISTIA:{ident}:{user_code}:{tier}:{period_code}"
    sig = hmac.new(LICENSE_SECRET.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest().upper()

    return f"LST-{period_code}-{sig[0:4]}-{sig[4:8]}-{sig[8:12]}"


def generate_master_key(tier="UNLIMITED", duration="LIFE"):
    """
    تولید یک کلید سراسری (Universal Master Key) با مدت مشخص یا مادام‌العمر
    """
    tier = str(tier or "UNLIMITED").strip().upper()
    period_code, days, label = normalize_duration(duration)

    msg = f"LISTIA:MASTER_KEY_UNIVERSAL:{tier}:{period_code}"
    sig = hmac.new(LICENSE_SECRET.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest().upper()

    return f"LST-{period_code}-{sig[0:4]}-{sig[4:8]}-{sig[8:12]}"


def verify_key(user_or_ident, key):
    """
    بررسی صحت کلید لایسنس وارد شده و استخراج مدت اعتبار
    خروجی: (is_valid, tier, days, message)
    days: تعداد روزهای اعتبار پس از فعال‌سازی (یا None برای مادام‌العمر)
    """
    if not key or not isinstance(key, str):
        return False, None, None, "لطفاً کلید لایسنس را وارد کنید."

    clean_key = key.strip().upper()
    ident = normalize_ident(user_or_ident)
    user_code = get_user_code(ident)

    # ۱. بررسی کلیدهای دارای برچسب مدت: LST-<PERIOD>-XXXX-XXXX-XXXX
    parts = clean_key.split("-")
    if len(parts) >= 4 and parts[0] == "LST":
        period_candidate = parts[1]
        period_code, days, label = normalize_duration(period_candidate)

        for candidate in [ident, user_code]:
            for tier in ["PRO", "UNLIMITED", "ENTERPRISE"]:
                expected = generate_key(candidate, tier, period_code)
                if hmac.compare_digest(clean_key, expected):
                    return True, tier, days, f"لایسنس {label} با موفقیت فعال شد."

        for tier in ["UNLIMITED", "PRO"]:
            expected_master = generate_master_key(tier, period_code)
            if hmac.compare_digest(clean_key, expected_master):
                return True, tier, days, f"کلید لایسنس سراسری ({label}) با موفقیت تأیید شد."

    # ۲. پشتیبانی از کلیدهای بدون تگ مدت (فرمت 4 بلوکی استاندارد - مادام‌العمر)
    for candidate in [ident, user_code]:
        for tier in ["PRO", "UNLIMITED", "ENTERPRISE"]:
            msg = f"LISTIA:{candidate}:{get_user_code(candidate)}:{tier}"
            sig = hmac.new(LICENSE_SECRET.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest().upper()
            legacy_expected = f"LST-{sig[0:4]}-{sig[4:8]}-{sig[8:12]}-{sig[12:16]}"
            if hmac.compare_digest(clean_key, legacy_expected):
                return True, tier, None, "لایسنس مادام‌العمر با موفقیت فعال شد."

    for tier in ["UNLIMITED", "PRO"]:
        msg = f"LISTIA:MASTER_KEY_UNIVERSAL:{tier}"
        sig = hmac.new(LICENSE_SECRET.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest().upper()
        legacy_master = f"LST-{sig[0:4]}-{sig[4:8]}-{sig[8:12]}-{sig[12:16]}"
        if hmac.compare_digest(clean_key, legacy_master):
            return True, tier, None, "کلید لایسنس سراسری مادام‌العمر با موفقیت تأیید شد."

    return False, None, None, "کلید لایسنس وارد شده نامعتبر است یا برای این حساب صادر نشده است."
