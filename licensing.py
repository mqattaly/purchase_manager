import hashlib
import hmac
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
INSTANCE_DIR.mkdir(exist_ok=True)

FREE_MAX_SUPPLIERS = 1
FREE_MAX_PRODUCTS = 5
LICENSE_TIERS = ("PRO", "UNLIMITED", "ENTERPRISE")
SIGNATURE_BLOCKS = 5  # 80-bit online-verifiable signature

DURATION_CHOICES = {
    "LIFE": {"label": "مادام‌العمر (دائمی)", "days": None},
    "30D": {"label": "۱ ماهه (۳۰ روز)", "days": 30},
    "90D": {"label": "۳ ماهه (۹۰ روز)", "days": 90},
    "180D": {"label": "۶ ماهه (۱۸۰ روز)", "days": 180},
    "365D": {"label": "۱ ساله (۳۶۵ روز)", "days": 365},
}


def _load_license_secret():
    """Load a deployment-specific signing key without a public fallback."""
    explicit = os.environ.get("LICENSE_SECRET")
    if explicit:
        return explicit

    installation_material = os.environ.get("SECRET_KEY") or os.environ.get("DATABASE_URL")
    if installation_material:
        return hashlib.sha256(
            f"listia:license-signing:v2:{installation_material}".encode()
        ).hexdigest()

    path = INSTANCE_DIR / ".license_secret"
    try:
        saved = path.read_text(encoding="utf-8").strip()
        if len(saved) >= 32:
            return saved
    except FileNotFoundError:
        pass

    generated = secrets.token_urlsafe(48)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text(encoding="utf-8").strip()
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(generated)
    return generated


LICENSE_SECRET = _load_license_secret()


def normalize_ident(ident):
    if hasattr(ident, "username"):
        ident = ident.username
    return str(ident or "").strip().lower()


def normalize_duration(duration):
    """Return ``(period code, days, Persian label)`` for a trusted value."""
    if duration is None:
        return "LIFE", None, DURATION_CHOICES["LIFE"]["label"]

    value = str(duration).strip().upper()
    if value in {"LIFE", "LIFETIME", "PERMANENT", "0", "NONE"}:
        return "LIFE", None, DURATION_CHOICES["LIFE"]["label"]

    if value in DURATION_CHOICES:
        item = DURATION_CHOICES[value]
        return value, item["days"], item["label"]

    if value.startswith("D") and value[1:].isdigit():
        value = value[1:] + "D"
    if value.endswith("D") and value[:-1].isdigit():
        days = int(value[:-1])
        if days > 0:
            return f"{days}D", days, f"{days} روزه"
        return "LIFE", None, DURATION_CHOICES["LIFE"]["label"]
    if value.isdigit():
        days = int(value)
        if days > 0:
            return f"{days}D", days, f"{days} روزه"

    return "LIFE", None, DURATION_CHOICES["LIFE"]["label"]


def validate_duration(value, max_days=3650):
    """Strictly validate an untrusted duration instead of defaulting to LIFE."""
    raw = str(value or "").strip().upper()
    if raw in {"LIFE", "LIFETIME", "PERMANENT"}:
        return normalize_duration("LIFE")
    if raw.startswith("D"):
        raw = raw[1:] + "D"
    if raw.endswith("D") and raw[:-1].isdigit():
        days = int(raw[:-1])
        if 1 <= days <= max_days:
            return normalize_duration(f"{days}D")
    if raw.isdigit():
        days = int(raw)
        if 1 <= days <= max_days:
            return normalize_duration(f"{days}D")
    return None


def _valid_period(value):
    value = str(value or "").strip().upper()
    if value == "LIFE":
        return normalize_duration(value)
    if value.endswith("D") and value[:-1].isdigit() and int(value[:-1]) > 0:
        return normalize_duration(value)
    return None


def _legacy_user_code(user_or_username):
    ident = normalize_ident(user_or_username)
    raw = f"LISTIA-USER:{ident}:{LICENSE_SECRET[:12]}"
    digest = hashlib.sha256(raw.encode()).hexdigest().upper()
    return f"LST-{digest[:4]}-{digest[4:8]}"


def get_user_code(user_or_username):
    """Generate a stable, non-secret activation identifier for one account."""
    ident = normalize_ident(user_or_username)
    digest = hmac.new(
        LICENSE_SECRET.encode(),
        f"LISTIA-USER:{ident}".encode(),
        hashlib.sha256,
    ).hexdigest().upper()
    return f"LST-{digest[:4]}-{digest[4:8]}"


def _format_signature(prefix, signature, blocks=SIGNATURE_BLOCKS):
    chunks = [signature[index : index + 4] for index in range(0, blocks * 4, 4)]
    return prefix + "-" + "-".join(chunks)


def _user_key(
    ident,
    tier,
    period_code,
    *,
    blocks,
    legacy_user_code=False,
    nonce=None,
):
    ident = normalize_ident(ident)
    user_code = _legacy_user_code(ident) if legacy_user_code else get_user_code(ident)
    nonce_suffix = f":{nonce}" if nonce else ""
    message = f"LISTIA:{ident}:{user_code}:{tier}:{period_code}{nonce_suffix}"
    signature = hmac.new(
        LICENSE_SECRET.encode(), message.encode(), hashlib.sha256
    ).hexdigest().upper()
    prefix = f"LST-{period_code}" + (f"-{nonce}" if nonce else "")
    return _format_signature(prefix, signature, blocks)


def _master_key(tier, period_code, *, blocks, nonce=None):
    nonce_suffix = f":{nonce}" if nonce else ""
    message = f"LISTIA:MASTER_KEY_UNIVERSAL:{tier}:{period_code}{nonce_suffix}"
    signature = hmac.new(
        LICENSE_SECRET.encode(), message.encode(), hashlib.sha256
    ).hexdigest().upper()
    prefix = f"LST-{period_code}" + (f"-{nonce}" if nonce else "")
    return _format_signature(prefix, signature, blocks)


def generate_key(user_or_ident, tier="PRO", duration="LIFE"):
    ident = normalize_ident(user_or_ident)
    tier = str(tier or "PRO").strip().upper()
    period_code, _days, _label = normalize_duration(duration)
    nonce = secrets.token_hex(4).upper()
    return _user_key(
        ident,
        tier,
        period_code,
        blocks=SIGNATURE_BLOCKS,
        nonce=nonce,
    )


def generate_master_key(tier="UNLIMITED", duration="LIFE"):
    tier = str(tier or "UNLIMITED").strip().upper()
    period_code, _days, _label = normalize_duration(duration)
    nonce = secrets.token_hex(4).upper()
    return _master_key(
        tier,
        period_code,
        blocks=SIGNATURE_BLOCKS,
        nonce=nonce,
    )


def verify_key(user_or_ident, key):
    """Verify current keys and compatible 48-bit keys issued with this secret."""
    if not key or not isinstance(key, str):
        return False, None, None, "لطفاً کلید لایسنس را وارد کنید."

    clean_key = key.strip().upper()
    if len(clean_key) > 160:
        return False, None, None, "کلید لایسنس وارد شده نامعتبر است."

    ident = normalize_ident(user_or_ident)
    user_codes = {get_user_code(ident), _legacy_user_code(ident)}
    candidates = {ident, *user_codes}
    parts = clean_key.split("-")

    # Current format: a signed nonce makes every renewal key unique.
    if len(parts) == 8 and parts[0] == "LST":
        period = _valid_period(parts[1])
        nonce = parts[2]
        nonce_is_valid = len(nonce) == 8 and all(
            char in "0123456789ABCDEF" for char in nonce
        )
        if period and nonce_is_valid:
            period_code, days, label = period
            for candidate in candidates:
                for tier in LICENSE_TIERS:
                    expected_variants = {
                        _user_key(
                            candidate,
                            tier,
                            period_code,
                            blocks=SIGNATURE_BLOCKS,
                            nonce=nonce,
                        ),
                        _user_key(
                            candidate,
                            tier,
                            period_code,
                            blocks=SIGNATURE_BLOCKS,
                            legacy_user_code=True,
                            nonce=nonce,
                        ),
                    }
                    if any(
                        hmac.compare_digest(clean_key, item)
                        for item in expected_variants
                    ):
                        return True, tier, days, f"لایسنس {label} با موفقیت فعال شد."
            for tier in ("UNLIMITED", "PRO"):
                if hmac.compare_digest(
                    clean_key,
                    _master_key(
                        tier,
                        period_code,
                        blocks=SIGNATURE_BLOCKS,
                        nonce=nonce,
                    ),
                ):
                    return (
                        True,
                        tier,
                        days,
                        f"کلید لایسنس سراسری ({label}) با موفقیت تأیید شد.",
                    )

    # Older tagged keys: 80-bit deterministic and 48-bit deterministic formats.
    if len(parts) in {5, 7} and parts[0] == "LST":
        period = _valid_period(parts[1])
        if period:
            period_code, days, label = period
            blocks = 5 if len(parts) == 7 else 3
            for candidate in candidates:
                for tier in LICENSE_TIERS:
                    expected_variants = {
                        _user_key(candidate, tier, period_code, blocks=blocks),
                        _user_key(
                            candidate,
                            tier,
                            period_code,
                            blocks=blocks,
                            legacy_user_code=True,
                        ),
                    }
                    if any(hmac.compare_digest(clean_key, item) for item in expected_variants):
                        return True, tier, days, f"لایسنس {label} با موفقیت فعال شد."

            for tier in ("UNLIMITED", "PRO"):
                if hmac.compare_digest(
                    clean_key, _master_key(tier, period_code, blocks=blocks)
                ):
                    return (
                        True,
                        tier,
                        days,
                        f"کلید لایسنس سراسری ({label}) با موفقیت تأیید شد.",
                    )

    # Legacy untagged lifetime format: LST-XXXX-XXXX-XXXX-XXXX.
    if len(parts) == 5 and parts[0] == "LST":
        for candidate in candidates:
            for tier in LICENSE_TIERS:
                message = f"LISTIA:{candidate}:{_legacy_user_code(candidate)}:{tier}"
                signature = hmac.new(
                    LICENSE_SECRET.encode(), message.encode(), hashlib.sha256
                ).hexdigest().upper()
                expected = _format_signature("LST", signature, 4)
                if hmac.compare_digest(clean_key, expected):
                    return True, tier, None, "لایسنس مادام‌العمر با موفقیت فعال شد."

        for tier in ("UNLIMITED", "PRO"):
            message = f"LISTIA:MASTER_KEY_UNIVERSAL:{tier}"
            signature = hmac.new(
                LICENSE_SECRET.encode(), message.encode(), hashlib.sha256
            ).hexdigest().upper()
            expected = _format_signature("LST", signature, 4)
            if hmac.compare_digest(clean_key, expected):
                return (
                    True,
                    tier,
                    None,
                    "کلید لایسنس سراسری مادام‌العمر با موفقیت تأیید شد.",
                )

    return False, None, None, "کلید لایسنس وارد شده نامعتبر است یا برای این حساب صادر نشده است."
