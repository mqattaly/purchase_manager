import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse
from zipfile import BadZipFile, ZipFile
from zoneinfo import ZoneInfo

import jdatetime
from dotenv import load_dotenv
from flask import (
    Flask,
    g,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_user,
    logout_user,
)
from flask_sqlalchemy import SQLAlchemy
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import inspect, or_, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import joinedload
from sqlalchemy.pool import NullPool
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
os.makedirs(INSTANCE_DIR, exist_ok=True)

logger = logging.getLogger(__name__)


def env_flag(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def installation_secret(filename, purpose, database_url=None):
    """Return a stable per-installation secret without a source-visible fallback.

    Production deployments should set the corresponding environment variable.
    A PostgreSQL URL is a stable, installation-specific last resort. Local SQLite
    installs receive a random secret in the ignored instance directory.
    """
    explicit = os.environ.get(filename.upper())
    if explicit:
        if len(explicit) < 32:
            logger.warning("%s should contain at least 32 characters", filename.upper())
        return explicit

    if database_url and database_url.startswith(("postgresql://", "postgres://")):
        logger.warning(
            "%s is not set; deriving %s from DATABASE_URL. Set an explicit secret in production.",
            filename.upper(),
            purpose,
        )
        material = f"listia:{purpose}:v2:{database_url}".encode()
        return hashlib.sha256(material).hexdigest()

    secret_path = Path(INSTANCE_DIR) / f".{filename.lower()}"
    try:
        secret = secret_path.read_text(encoding="utf-8").strip()
        if len(secret) >= 32:
            return secret
    except FileNotFoundError:
        pass

    secret = secrets.token_urlsafe(48)
    try:
        descriptor = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return secret_path.read_text(encoding="utf-8").strip()
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(secret)
    return secret


db_url = os.environ.get("DATABASE_URL")
if db_url and db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app = Flask(__name__)
app.secret_key = installation_secret("SECRET_KEY", "session-signing", db_url)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

CROSS_SITE_COOKIES = env_flag("CROSS_SITE_COOKIES", False)
COOKIE_SECURE = env_flag("COOKIE_SECURE", True)
app.config.update(
    SESSION_COOKIE_SAMESITE="None" if CROSS_SITE_COOKIES else "Lax",
    SESSION_COOKIE_SECURE=COOKIE_SECURE,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_PARTITIONED=CROSS_SITE_COOKIES,
    REMEMBER_COOKIE_SAMESITE="None" if CROSS_SITE_COOKIES else "Lax",
    REMEMBER_COOKIE_SECURE=COOKIE_SECURE,
    REMEMBER_COOKIE_HTTPONLY=True,
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    MAX_CONTENT_LENGTH=8 * 1024 * 1024,
    SEND_FILE_MAX_AGE_DEFAULT=60 * 60 * 24 * 365,
    CSRF_PROTECT=True,
    RATE_LIMITING=True,
)

if db_url:
    app.config["SQLALCHEMY_DATABASE_URI"] = db_url
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.join(INSTANCE_DIR, "purchase.db")

if os.environ.get("VERCEL"):
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"poolclass": NullPool}
elif db_url and db_url.startswith("postgresql://"):
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_pre_ping": True,
        "pool_recycle": int(os.environ.get("DB_POOL_RECYCLE", "300")),
    }

try:
    from whitenoise import WhiteNoise

    app.wsgi_app = WhiteNoise(
        app.wsgi_app,
        root=os.path.join(BASE_DIR, "static"),
        prefix="static/",
        max_age=60 * 60 * 24 * 7,
        autorefresh=app.debug or env_flag("STATIC_AUTOREFRESH", False),
    )
except ImportError:  # pragma: no cover - Flask can still serve static files
    logger.info("WhiteNoise is unavailable; Flask will serve static assets.")

from licensing import (
    FREE_MAX_PRODUCTS,
    FREE_MAX_SUPPLIERS,
    generate_key,
    generate_master_key,
    get_user_code,
    normalize_duration,
    validate_duration,
    verify_key,
)

AUTH_COOKIE = "listia_auth"
AUTH_MAX_AGE = 60 * 60 * 24 * 30
CSRF_MAX_AGE = 60 * 60 * 12


def _auth_serializer():
    return URLSafeTimedSerializer(app.secret_key, salt="listia-auth-v2")


def _csrf_serializer():
    return URLSafeTimedSerializer(app.secret_key, salt="listia-csrf-v1")


def password_fingerprint(user):
    return hashlib.sha256(user.password_hash.encode()).hexdigest()[:16]


def issue_auth_token(user):
    if not hasattr(user, "id"):
        user = db.session.get(User, int(user))
    return _auth_serializer().dumps(
        {"uid": int(user.id), "password_version": password_fingerprint(user)}
    )


def user_from_auth_token(token):
    if not token:
        return None
    try:
        data = _auth_serializer().loads(token, max_age=AUTH_MAX_AGE)
        user = db.session.get(User, int(data["uid"]))
        if not user or not hmac.compare_digest(
            str(data["password_version"]), password_fingerprint(user)
        ):
            return None
        return user
    except (BadSignature, SignatureExpired, KeyError, TypeError, ValueError):
        return None


def issue_csrf_token():
    user_id = int(current_user.id) if current_user.is_authenticated else 0
    return _csrf_serializer().dumps({"uid": user_id})


def csrf_token_is_valid(token):
    if not token:
        return False
    try:
        data = _csrf_serializer().loads(token, max_age=CSRF_MAX_AGE)
        expected_user_id = int(current_user.id) if current_user.is_authenticated else 0
        return int(data.get("uid", -1)) == expected_user_id
    except (BadSignature, SignatureExpired, TypeError, ValueError):
        return False


def attach_auth_cookie(response, user):
    kwargs = {
        "key": AUTH_COOKIE,
        "value": issue_auth_token(user),
        "max_age": AUTH_MAX_AGE,
        "httponly": True,
        "secure": COOKIE_SECURE,
        "samesite": "None" if CROSS_SITE_COOKIES else "Lax",
        "path": "/",
    }
    if CROSS_SITE_COOKIES:
        try:
            response.set_cookie(partitioned=True, **kwargs)
        except TypeError:  # pragma: no cover - compatibility with older Werkzeug
            response.set_cookie(**kwargs)
    else:
        response.set_cookie(**kwargs)
    return response


def login_handoff(user):
    login_user(user, remember=True)
    return attach_auth_cookie(redirect(url_for("home")), user)


@app.after_request
def apply_security_headers(response):
    frame_ancestors = os.environ.get("FRAME_ANCESTORS", "'self'").strip() or "'self'"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "base-uri 'self'; object-src 'none'; form-action 'self'; "
        "img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "script-src 'self' 'unsafe-inline'; connect-src 'self'; "
        f"frame-ancestors {frame_ancestors}"
    )
    if frame_ancestors == "'self'":
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
    else:
        response.headers.pop("X-Frame-Options", None)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if request.is_secure:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

    cacheable_endpoints = {"static", "brand_asset", "web_manifest"}
    if request.endpoint not in cacheable_endpoints:
        response.headers["Cache-Control"] = "private, no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"

    # Flask-Login has no Partitioned option for its remember cookie yet.
    if CROSS_SITE_COOKIES:
        cookies = response.headers.getlist("Set-Cookie")
        if cookies:
            response.headers.remove("Set-Cookie")
            for cookie in cookies:
                patched = cookie
                if "SameSite=" in patched:
                    patched = patched.replace("SameSite=Lax", "SameSite=None")
                else:
                    patched += "; SameSite=None"
                if "Secure" not in patched:
                    patched += "; Secure"
                if "Partitioned" not in patched:
                    patched += "; Partitioned"
                response.headers.add("Set-Cookie", patched)
    return response


db = SQLAlchemy(app)

UNIT_TYPES = ["عدد", "کارتن", "بسته", "گونی", "کیلو"]
PRIMARY_ADMIN_USERNAME = "smq2458"
RESERVED_USERNAMES = {PRIMARY_ADMIN_USERNAME, "admin"}
ALLOWED_LICENSE_TIERS = {"PRO", "UNLIMITED", "ENTERPRISE"}
MIN_PASSWORD_LENGTH = 8
MAX_IMPORT_ROWS = 2_000

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"


def normalize_name(name):
    name = " ".join((name or "").split())
    name = name.replace("ي", "ی").replace("ك", "ک")
    return name.strip().lower()


_DIGIT_TRANSLATION = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def clean_person_name(value, max_length=100):
    """Normalize whitespace and Arabic keyboard variants in profile names."""
    value = " ".join((value or "").split())
    return value.replace("ي", "ی").replace("ك", "ک")[:max_length]


def clean_username(value):
    """Canonicalize usernames while keeping Persian and common safe symbols."""
    username = (value or "").strip().replace("ي", "ی").replace("ك", "ک").lower()
    if len(username) > 100 or any(char.isspace() or ord(char) < 32 for char in username):
        return None
    return username


def username_is_valid(username):
    return bool(
        username
        and 2 <= len(username) <= 100
        and all(char.isalnum() or char in "._-" for char in username)
    )


def user_by_username(username):
    username = clean_username(username)
    if not username:
        return None
    return User.query.filter(db.func.lower(User.username) == username).first()


def utcnow():
    """Naive UTC timestamp, matching the app's existing database columns."""
    return datetime.now(UTC).replace(tzinfo=None)


def local_today():
    return datetime.now(ZoneInfo("Asia/Tehran")).date()


def normalize_quantity(value):
    """Return a positive, bounded decimal quantity in a stable display form."""
    raw = (value or "").strip().translate(_DIGIT_TRANSLATION)
    raw = raw.replace("٫", ".").replace(",", "")
    if not raw or len(raw) > 50:
        return None
    try:
        quantity = Decimal(raw)
    except InvalidOperation:
        return None
    if not quantity.is_finite() or quantity <= 0 or quantity > Decimal(999999999):
        return None
    if quantity.as_tuple().exponent < -3:
        return None
    return format(quantity.normalize(), "f")


def normalize_mobile(value):
    """Return an Iranian mobile number in canonical ``09xxxxxxxxx`` form.

    Persian/Arabic digits and common +98/0098 forms are accepted so users do
    not have to switch keyboards while signing up.
    """
    raw = (value or "").strip().translate(_DIGIT_TRANSLATION)
    digits = "".join(char for char in raw if char.isdigit())

    if digits.startswith("0098"):
        digits = "0" + digits[4:]
    elif digits.startswith("98"):
        digits = "0" + digits[2:]
    elif len(digits) == 10 and digits.startswith("9"):
        digits = "0" + digits

    if len(digits) == 11 and digits.startswith("09"):
        return digits
    return None


# ترتیب الفبای فارسی — چون مرتب‌سازی پیش‌فرض دیتابیس روی حروف فارسی درست نیست
PERSIAN_ALPHABET = "آابپتثجچحخدذرزژسشصضطظعغفقکگلمنوهی"
_PERSIAN_RANK = {ch: index for index, ch in enumerate(PERSIAN_ALPHABET)}


def supplier_sort_key(name):
    """کلید مرتب‌سازی نام‌ها بر اساس الفبای فارسی (لاتین و عدد بعد از آن)."""
    cleaned = (name or "").replace("ي", "ی").replace("ك", "ک").replace("\u200c", " ").strip().lower()
    key = []
    for ch in cleaned:
        if ch in _PERSIAN_RANK:
            key.append((0, _PERSIAN_RANK[ch]))
        elif ch.isspace():
            key.append((1, 0))
        elif ch.isdigit():
            key.append((2, ord(ch)))
        else:
            key.append((3, ord(ch)))
    return key


def wants_json():
    return request.headers.get("X-Requested-With") == "fetch"



def safe_redirect(fallback):
    ref = request.referrer
    if not ref:
        return redirect(fallback)
    parsed = urlparse(ref)
    if parsed.netloc and parsed.netloc != request.host:
        return redirect(fallback)
    return redirect(ref)


def cached_limits():
    if not hasattr(g, "limits"):
        if current_user.is_authenticated:
            g.limits = get_user_limits(
                current_user,
                supplier_count=getattr(g, "quota_supplier_count", None),
                product_count=getattr(g, "quota_product_count", None),
            )
        else:
            g.limits = None
    return g.limits


_rate_events = {}


def rate_limited(scope, identity, limit, window_seconds, *, consume=True):
    """Small per-process guard against brute force and accidental request floods."""
    if not app.config.get("RATE_LIMITING", True):
        return False
    now = time.time()
    key = (scope, str(identity))
    stamps = [stamp for stamp in _rate_events.get(key, ()) if now - stamp < window_seconds]
    blocked = len(stamps) >= limit
    if consume and not blocked:
        stamps.append(now)
    _rate_events[key] = stamps

    if len(_rate_events) > 5_000:
        stale_keys = [
            event_key
            for event_key, values in _rate_events.items()
            if not values or now - values[-1] > 3_600
        ]
        for event_key in stale_keys:
            _rate_events.pop(event_key, None)
    return blocked


def login_is_throttled(ip):
    return rate_limited("login", ip, 12, 300, consume=False)


def record_login_fail(ip):
    rate_limited("login", ip, 12, 300)


def shamsi_label(value):
    if not value:
        return None
    return jdatetime.date.fromgregorian(date=value).strftime("%Y/%m/%d")


class Supplier(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    name = db.Column(db.String(200), nullable=False)
    products = db.relationship("Product", backref="supplier", cascade="all, delete-orphan")

    __table_args__ = (db.UniqueConstraint("owner_id", "name", name="uq_supplier_owner_name"),)


class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    supplier_id = db.Column(db.Integer, db.ForeignKey("supplier.id"), nullable=False)
    product_name = db.Column(db.String(300), nullable=False)
    quantity = db.Column(db.String(50))
    unit = db.Column(db.String(50))
    description = db.Column(db.String(500))
    ordered = db.Column(db.Boolean, default=False)
    ordered_date = db.Column(db.Date, nullable=True)


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False)
    password_hash = db.Column(db.String(300), nullable=False)
    # اطلاعات تماس؛ nullable می‌مانند تا حساب‌های قدیمی بدون مهاجرت اجباری کار کنند.
    first_name = db.Column(db.String(100), nullable=True)
    last_name = db.Column(db.String(100), nullable=True)
    mobile = db.Column(db.String(20), nullable=True)
    # کلید شخصی برای ثبت از بیرون اپ (مثلاً Shortcuts آیفون)
    # ``api_token`` is retained only for a one-time legacy migration. New keys
    # are stored as SHA-256 digests so a database read does not expose them.
    api_token = db.Column(db.String(64), unique=True, nullable=True)
    api_token_hash = db.Column(db.String(64), unique=True, nullable=True)
    # لایسنس نرم‌افزار لیستیا
    is_licensed = db.Column(db.Boolean, default=False)
    license_key = db.Column(db.String(160), nullable=True)
    licensed_at = db.Column(db.DateTime, nullable=True)
    license_expires_at = db.Column(db.DateTime, nullable=True)  # None = مادام‌العمر
    license_type = db.Column(db.String(50), default="free")
    # دسترسی مدیریت و صدور لایسنس
    is_admin = db.Column(db.Boolean, default=False)

    def get_id(self):
        # Binding Flask-Login sessions to the password hash revokes every old
        # browser session after a password reset without an extra schema field.
        return f"{self.id}:{password_fingerprint(self)}"


class LicenseActivation(db.Model):
    """One-way activation history prevents a timed key from resetting forever."""

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    key_hash = db.Column(db.String(64), nullable=False)
    activated_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        db.UniqueConstraint("user_id", "key_hash", name="uq_license_activation_user_key"),
    )


@login_manager.user_loader
def load_user(session_id):
    try:
        raw_user_id, fingerprint = str(session_id).split(":", 1)
        user = db.session.get(User, int(raw_user_id))
    except (TypeError, ValueError):
        # Legacy ID-only cookies are deliberately invalidated during this
        # security upgrade and require one fresh login.
        return None
    if user and hmac.compare_digest(fingerprint, password_fingerprint(user)):
        return user
    return None


def is_admin_user(user=None):
    """Only an explicit database role grants administrative access."""
    user = user or current_user
    if not user or not user.is_authenticated:
        return False
    return bool(getattr(user, "is_admin", False))


def get_user_limits(user=None, supplier_count=None, product_count=None):
    """محاسبه وضعیت سهمیه، مدت اعتبار و لایسنس کاربر"""
    if user is None:
        if current_user.is_authenticated:
            user = current_user
        else:
            return {
                "is_licensed": False,
                "is_admin": False,
                "is_expired": False,
                "is_lifetime": False,
                "user_code": "",
                "supplier_count": 0,
                "product_count": 0,
                "max_suppliers": FREE_MAX_SUPPLIERS,
                "max_products": FREE_MAX_PRODUCTS,
                "can_add_supplier": False,
                "can_add_product": False,
                "license_type": "free",
                "licensed_at": None,
                "license_expires_at": None,
                "expires_at_label": None,
                "remaining_days": None,
                "license_key": None,
            }

    admin = is_admin_user(user)
    is_lic = bool(user.is_licensed or admin)
    is_expired = False
    is_lifetime = False
    remaining_days = None
    expires_label = None

    if admin:
        is_lic = True
        is_lifetime = True
    elif user.is_licensed:
        if user.license_expires_at is None:
            is_lifetime = True
            is_lic = True
        else:
            now = utcnow()
            if user.license_expires_at > now:
                is_lic = True
                diff = user.license_expires_at - now
                remaining_days = max(1, diff.days + (1 if diff.seconds > 0 else 0))
                expires_label = shamsi_label(user.license_expires_at.date())
            else:
                is_lic = False
                is_expired = True
                remaining_days = 0
                expires_label = shamsi_label(user.license_expires_at.date())

    s_count = (
        Supplier.query.filter_by(owner_id=user.id).count()
        if supplier_count is None
        else int(supplier_count)
    )
    p_count = (
        Product.query.filter_by(owner_id=user.id).count()
        if product_count is None
        else int(product_count)
    )

    return {
        "is_licensed": is_lic,
        "is_admin": admin,
        "is_expired": is_expired,
        "is_lifetime": is_lifetime,
        "user_code": get_user_code(user),
        "supplier_count": s_count,
        "product_count": p_count,
        "max_suppliers": None if is_lic else FREE_MAX_SUPPLIERS,
        "max_products": None if is_lic else FREE_MAX_PRODUCTS,
        "can_add_supplier": is_lic or (s_count < FREE_MAX_SUPPLIERS),
        "can_add_product": is_lic or (p_count < FREE_MAX_PRODUCTS),
        "license_type": user.license_type if is_lic else ("expired" if is_expired else "free"),
        "licensed_at": user.licensed_at,
        "license_expires_at": user.license_expires_at,
        "expires_at_label": expires_label,
        "remaining_days": remaining_days,
        "license_key": user.license_key,
    }


def product_payload(product, supplier_name=None):
    if supplier_name is None:
        supplier_name = product.supplier.name if product.supplier else ""
    return {
        "id": product.id,
        "supplier_id": product.supplier_id,
        "supplier_name": supplier_name,
        "product_name": product.product_name,
        "quantity": product.quantity,
        "unit": product.unit,
        "description": product.description or "",
        "ordered": bool(product.ordered),
        "ordered_date": product.ordered_date.isoformat() if product.ordered_date else None,
        "ordered_date_label": shamsi_label(product.ordered_date),
    }


def api_token_digest(token):
    return hashlib.sha256((token or "").encode()).hexdigest()


def issue_api_token(user):
    """Create a key, store only its digest, and return the key once."""
    raw_token = secrets.token_urlsafe(32)
    user.api_token = None
    user.api_token_hash = api_token_digest(raw_token)
    db.session.commit()
    return raw_token


def license_key_hash(key):
    return hashlib.sha256((key or "").strip().upper().encode()).hexdigest()


def mark_license_key_used(user, key):
    """Record a key once for this account; return the existing/new row."""
    if not key:
        return None
    digest = license_key_hash(key)
    existing = LicenseActivation.query.filter_by(
        user_id=user.id, key_hash=digest
    ).first()
    if existing:
        return existing
    activation = LicenseActivation(user_id=user.id, key_hash=digest, activated_at=utcnow())
    db.session.add(activation)
    return activation


def user_from_api_token():
    """Resolve external-API credentials from headers or a POST body only."""
    if hasattr(g, "api_user"):
        return g.api_user

    token = (request.headers.get("X-API-Key") or request.form.get("key") or "").strip()
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()

    if not token or len(token) > 128:
        g.api_user = None
    else:
        digest = api_token_digest(token)
        g.api_user = User.query.filter(
            or_(User.api_token_hash == digest, User.api_token == token)
        ).first()
    return g.api_user


def json_error(message, status=400, extra=None):
    payload = {"success": False, "message": message}
    if extra:
        payload.update(extra)
    return payload, status


def locked_user(user_id):
    """Serialize quota-changing operations for one user on PostgreSQL."""
    return User.query.filter_by(id=user_id).with_for_update().one()


def user_suppliers():
    """All suppliers of the logged-in user, alphabetically."""
    if hasattr(g, "suppliers"):
        return g.suppliers
    rows = Supplier.query.filter_by(owner_id=current_user.id).order_by(Supplier.name).all()
    g.suppliers = sorted(rows, key=lambda s: supplier_sort_key(s.name))
    g.quota_supplier_count = len(g.suppliers)
    return g.suppliers


def owned_supplier_by_normalized_name(name, owner_id=None):
    owner_id = owner_id if owner_id is not None else current_user.id
    target = normalize_name(name)
    if not target:
        return None
    return next(
        (
            supplier
            for supplier in Supplier.query.filter_by(owner_id=owner_id).all()
            if normalize_name(supplier.name) == target
        ),
        None,
    )


def active_clause():
    """A product counts as «active» when it is not ordered yet.

    Rows written by older versions of the app (or imported straight into the
    database) can carry ``ordered = NULL`` instead of ``FALSE``. In SQL,
    ``ordered = FALSE`` skips those rows, which used to make the per-supplier
    counters show 0 for data registered before. Treat NULL as «not ordered».
    """
    return or_(Product.ordered.is_(False), Product.ordered.is_(None))


def archived_clause():
    return Product.ordered.is_(True)


def user_products(**filters):
    """Base product query scoped to the logged-in user (supplier eagerly loaded)."""
    ordered = filters.pop("ordered", None)
    query = (
        Product.query.options(joinedload(Product.supplier))
        .filter_by(owner_id=current_user.id, **filters)
    )
    if ordered is True:
        query = query.filter(archived_clause())
    elif ordered is False:
        query = query.filter(active_clause())
    return query


def active_counts_by_supplier(supplier_ids=None):
    """{supplier_id: active product count} for the logged-in user."""
    query = (
        db.session.query(Product.supplier_id, db.func.count(Product.id))
        .filter(Product.owner_id == current_user.id, active_clause())
    )
    if supplier_ids is not None:
        if not supplier_ids:
            return {}
        query = query.filter(Product.supplier_id.in_(list(supplier_ids)))
    return {sid: total for sid, total in query.group_by(Product.supplier_id).all()}


def dashboard_counters():
    """Live numbers shown on the dashboard, always read straight from the DB."""
    active_count = archived_count = 0
    for ordered_flag, total in (
        db.session.query(Product.ordered, db.func.count(Product.id))
        .filter(Product.owner_id == current_user.id)
        .group_by(Product.ordered)
        .all()
    ):
        if ordered_flag:
            archived_count += total
        else:
            active_count += total

    product_count = active_count + archived_count
    supplier_count = len(user_suppliers())
    g.quota_product_count = product_count
    g.quota_supplier_count = supplier_count

    return {
        "active_count": active_count,
        "archived_count": archived_count,
        # The trial quota counts every saved product, including archived ones.
        "product_count": product_count,
        "supplier_count": supplier_count,
        "suppliers": active_counts_by_supplier(),
    }


def recent_product_names(limit=150):
    """Names already used by this user — feeds the browser's autocomplete."""
    rows = (
        db.session.query(Product.product_name)
        .filter(Product.owner_id == current_user.id)
        .order_by(Product.id.desc())
        .limit(600)
        .all()
    )
    seen = []
    known = set()
    for (name,) in rows:
        key = (name or "").strip().lower()
        if not key or key in known:
            continue
        known.add(key)
        seen.append(name)
        if len(seen) >= limit:
            break
    return seen


def read_product_form(source=None):
    """Validate the shared product form. Returns ``(fields, error)``."""
    source = source or request.form
    product_raw = source.get("product", "").strip()
    quantity_raw = source.get("quantity", "").strip()
    description_raw = source.get("description", "").strip()
    fields = {
        "supplier_id": source.get("supplier", "") or source.get("supplier_id", ""),
        "product_name": product_raw,
        "quantity": normalize_quantity(quantity_raw),
        "unit": source.get("unit", "").strip(),
        "description": description_raw,
    }

    if not fields["product_name"]:
        return fields, "نام محصول را وارد کنید."
    if len(product_raw) > 300:
        return fields, "نام محصول حداکثر ۳۰۰ کاراکتر باشد."
    if not quantity_raw:
        return fields, "تعداد را وارد کنید."
    if not fields["quantity"]:
        return fields, "تعداد باید عددی مثبت و حداکثر دارای ۳ رقم اعشار باشد."
    if fields["unit"] not in UNIT_TYPES:
        return fields, "نوع تعداد را انتخاب کنید."
    if len(description_raw) > 500:
        return fields, "توضیحات حداکثر ۵۰۰ کاراکتر باشد."
    return fields, None


def owned_supplier_or_404(supplier_id):
    return Supplier.query.filter_by(id=supplier_id, owner_id=current_user.id).first_or_404()


def owned_product_or_404(product_id):
    return (
        Product.query.options(joinedload(Product.supplier))
        .filter_by(id=product_id, owner_id=current_user.id)
        .first_or_404()
    )


def claim_orphaned_data(user_id):
    """Give leftover rows (no owner) to the first account so old data is not lost."""
    user_count = User.query.count()
    if user_count != 1:
        return
    Supplier.query.filter(Supplier.owner_id.is_(None)).update(
        {Supplier.owner_id: user_id}, synchronize_session=False
    )
    Product.query.filter(Product.owner_id.is_(None)).update(
        {Product.owner_id: user_id}, synchronize_session=False
    )
    db.session.commit()


def _table_columns(table_name):
    inspector = inspect(db.engine)
    names = inspector.get_table_names()
    if table_name not in names:
        return set()
    return {col["name"] for col in inspector.get_columns(table_name)}


def _run_schema_statement(conn, statement, label):
    """Execute idempotent DDL without hiding an unexpected migration failure."""
    try:
        with conn.begin_nested():
            conn.execute(text(statement))
        return True
    except SQLAlchemyError as exc:
        logger.warning("Schema step %s was skipped: %s", label, exc)
        return False


def ensure_schema():
    """Create new tables and upgrade the small legacy schema in place."""
    db.create_all()

    missing_owner_cols = [
        table
        for table in ("supplier", "product")
        if _table_columns(table) and "owner_id" not in _table_columns(table)
    ]
    user_cols = _table_columns("user")

    with db.engine.begin() as conn:
        for table in missing_owner_cols:
            _run_schema_statement(
                conn,
                f"ALTER TABLE {table} ADD COLUMN owner_id INTEGER",
                f"{table}.owner_id",
            )

        legacy_token_column = User.__table__.c.api_token.name
        token_hash_column = User.__table__.c.api_token_hash.name
        user_column_definitions = {
            "first_name": "VARCHAR(100)",
            "last_name": "VARCHAR(100)",
            "mobile": "VARCHAR(20)",
            legacy_token_column: "VARCHAR(64)",
            token_hash_column: "VARCHAR(64)",
            "is_licensed": "BOOLEAN DEFAULT FALSE",
            "license_key": "VARCHAR(160)",
            "licensed_at": "TIMESTAMP",
            "license_expires_at": "TIMESTAMP",
            "license_type": "VARCHAR(50) DEFAULT 'free'",
            "is_admin": "BOOLEAN DEFAULT FALSE",
        }
        for column_name, column_type in user_column_definitions.items():
            if user_cols and column_name not in user_cols:
                _run_schema_statement(
                    conn,
                    f'ALTER TABLE "user" ADD COLUMN {column_name} {column_type}',
                    f"user.{column_name}",
                )

        index_statements = {
            "supplier owner/name": (
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_supplier_owner_name "
                "ON supplier (owner_id, name)"
            ),
            "supplier owner": "CREATE INDEX IF NOT EXISTS ix_supplier_owner ON supplier (owner_id)",
            "product owner": "CREATE INDEX IF NOT EXISTS ix_product_owner ON product (owner_id)",
            "product owner/ordered": (
                "CREATE INDEX IF NOT EXISTS ix_product_owner_ordered ON product (owner_id, ordered)"
            ),
            "product supplier/order date": (
                "CREATE INDEX IF NOT EXISTS ix_product_supplier_ordered "
                "ON product (supplier_id, ordered, ordered_date)"
            ),
            "username case-insensitive uniqueness": (
                'CREATE UNIQUE INDEX IF NOT EXISTS uq_user_username_lower ON "user" (LOWER(username))'
            ),
            "legacy API token uniqueness": (
                'CREATE UNIQUE INDEX IF NOT EXISTS uq_user_api_token ON "user" (api_token)'
            ),
            "API token hash uniqueness": (
                'CREATE UNIQUE INDEX IF NOT EXISTS uq_user_api_token_hash ON "user" (api_token_hash)'
            ),
            "license activation lookup": (
                "CREATE INDEX IF NOT EXISTS ix_license_activation_user "
                "ON license_activation (user_id)"
            ),
        }
        for label, statement in index_statements.items():
            _run_schema_statement(conn, statement, label)

        _run_schema_statement(
            conn,
            "UPDATE product SET ordered = FALSE WHERE ordered IS NULL",
            "normalize product.ordered",
        )
        # Only the intended owner account is bootstrapped. Public registration
        # can never create this reserved username (see signup()).
        _run_schema_statement(
            conn,
            'UPDATE "user" SET is_licensed = TRUE, is_admin = TRUE, '
            "license_type = 'UNLIMITED' WHERE LOWER(username) = 'smq2458'",
            "bootstrap primary administrator",
        )

    required_columns = {
        "supplier": {"owner_id"},
        "product": {"owner_id"},
        "user": set(user_column_definitions),
    }
    missing = {
        table: sorted(columns - _table_columns(table))
        for table, columns in required_columns.items()
        if columns - _table_columns(table)
    }
    if missing:
        raise RuntimeError(f"Database schema upgrade is incomplete: {missing}")

    # Upgrade plaintext legacy API credentials in place. Existing Shortcuts keep
    # working because incoming keys are hashed before lookup.
    legacy_api_users = User.query.filter(
        User.api_token.is_not(None), User.api_token_hash.is_(None)
    ).all()
    for legacy_user in legacy_api_users:
        legacy_user.api_token_hash = api_token_digest(legacy_user.api_token)
        legacy_user.api_token = None
    if legacy_api_users:
        db.session.commit()

    first = db.session.execute(text('SELECT id FROM "user" ORDER BY id LIMIT 1')).scalar()
    if first:
        db.session.execute(
            text("UPDATE supplier SET owner_id = :uid WHERE owner_id IS NULL"),
            {"uid": first},
        )
        db.session.execute(
            text("UPDATE product SET owner_id = :uid WHERE owner_id IS NULL"),
            {"uid": first},
        )
        db.session.commit()


BRAND_FILES = (
    "style.css",
    "app.js",
    "logo.png",
    "logo-192.png",
    "logo-512.png",
    "favicon.png",
    "favicon-32.png",
    "favicon.ico",
    "apple-touch-icon.png",
)


def _asset_version():
    """Cache-busting stamp so browsers can cache CSS/JS/آیکون‌ها for a year.

    اندازه فایل‌ها هم در امضا می‌آید تا اگر لوگو یا فاوآیکون عوض شود — حتی
    وقتی تاریخ فایل‌ها روی سرور یکسان است (مثل دیپلوی‌های Vercel) — مرورگر
    نسخه تازه را بگیرد و نسخه کش‌شده قدیمی را نشان ندهد.
    """
    stamp = 0
    total_size = 0
    for name in BRAND_FILES:
        path = os.path.join(BASE_DIR, "static", name)
        try:
            stat = os.stat(path)
        except OSError:
            continue
        stamp = max(stamp, int(stat.st_mtime))
        total_size += stat.st_size
    return f"{stamp}-{total_size}"


ASSET_VERSION = _asset_version()


@app.context_processor
def inject_template_globals():
    limits = cached_limits()
    admin = bool(limits and limits.get("is_admin"))
    return {
        "asset_v": ASSET_VERSION,
        "unit_types": UNIT_TYPES,
        "app_name": "لیستیا",
        "developer_name": "س.م.قتالی",
        "limits": limits,
        "is_admin": admin,
        "csrf_token": issue_csrf_token(),
    }


_schema_ready = False
_schema_lock = threading.Lock()


@app.before_request
def _prepare_request():
    global _schema_ready

    asset_endpoints = {"static", "brand_asset", "web_manifest"}
    if request.endpoint in asset_endpoints:
        return None

    if not _schema_ready:
        with _schema_lock:
            if not _schema_ready:
                try:
                    ensure_schema()
                    _schema_ready = True
                except Exception:
                    logger.exception("Database initialization failed")
                    return json_error(
                        "پایگاه داده موقتاً در دسترس نیست. کمی بعد دوباره تلاش کنید.",
                        503,
                    )

    if not current_user.is_authenticated:
        user = user_from_auth_token(request.cookies.get(AUTH_COOKIE))
        if user:
            login_user(user, remember=True)

    public_endpoints = {"login", "signup", "api_quick_add", "api_suppliers"}
    if (
        request.endpoint not in public_endpoints
        and request.endpoint is not None
        and not current_user.is_authenticated
    ):
        if wants_json() or request.path.startswith("/api/"):
            return json_error("نشست شما پایان یافته است؛ دوباره وارد شوید.", 401)
        return redirect(url_for("login"))

    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and app.config.get(
        "CSRF_PROTECT", True
    ):
        is_api_key_request = request.endpoint == "api_quick_add" and user_from_api_token()
        if not is_api_key_request:
            token = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
            if not csrf_token_is_valid(token):
                return json_error(
                    "نشست امنیتی منقضی یا نامعتبر است؛ صفحه را تازه‌سازی و دوباره تلاش کنید.",
                    403,
                    {"csrf_failed": True},
                )

    return None


# ---------------------------------------------------------------------------
#  لوگو و آیکون‌ها — فایل‌ها در پوشه static می‌مانند، ولی از ریشه هم سرو می‌شوند
#  تا حتی اگر هاست مسیر /static را جای دیگری بفرستد، آیکون‌ها نمایش داده شوند.
# ---------------------------------------------------------------------------

BRAND_ROUTE_FILES = {
    "favicon.ico": "image/x-icon",
    "favicon.png": "image/png",
    "favicon-32.png": "image/png",
    "favicon-16.png": "image/png",
    "apple-touch-icon.png": "image/png",
    "apple-touch-icon-precomposed.png": "image/png",
    "logo.png": "image/png",
    "logo-192.png": "image/png",
    "logo-512.png": "image/png",
}


@app.route("/favicon.ico")
@app.route("/favicon.png")
@app.route("/favicon-32.png")
@app.route("/favicon-16.png")
@app.route("/apple-touch-icon.png")
@app.route("/apple-touch-icon-precomposed.png")
@app.route("/logo.png")
@app.route("/logo-192.png")
@app.route("/logo-512.png")
def brand_asset():
    filename = os.path.basename(request.path)
    mimetype = BRAND_ROUTE_FILES.get(filename, "image/png")
    path = os.path.join(BASE_DIR, "static", filename)
    if not os.path.exists(path):
        fallback = "logo.png" if filename.startswith(("logo", "apple")) else "favicon.png"
        path = os.path.join(BASE_DIR, "static", fallback)
        if not os.path.exists(path):
            return json_error("فایل آیکون پیدا نشد.", 404)
        mimetype = "image/png"
    return send_file(path, mimetype=mimetype, max_age=60 * 60 * 24 * 7)


@app.route("/manifest.webmanifest")
def web_manifest():
    """معرفی اپ برای نصب روی موبایل (Add to Home Screen) با لوگوی خودمان."""
    response = make_response({
        "name": "لیستیا — مدیریت سفارش و خرید",
        "short_name": "لیستیا",
        "description": "سامانه مدیریت هوشمند خرید و سفارش‌های فروشگاه",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "dir": "rtl",
        "lang": "fa",
        "background_color": "#F3F6F8",
        "theme_color": "#142430",
        "icons": [
            {"src": f"/logo-192.png?v={ASSET_VERSION}", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": f"/logo-512.png?v={ASSET_VERSION}", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": f"/logo-512.png?v={ASSET_VERSION}", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    })
    response.headers["Content-Type"] = "application/manifest+json; charset=utf-8"
    return response


@app.route("/")
def home():
    """صفحه اول اپلیکیشن: داشبورد"""
    suppliers = user_suppliers()

    counters = dashboard_counters()
    active_count = counters["active_count"]
    archived_count = counters["archived_count"]

    recent = (
        user_products(ordered=False).order_by(Product.id.desc()).limit(15).all()
    )

    active_by_supplier = counters["suppliers"]

    limits = cached_limits()

    return render_template(
        "dashboard.html",
        suppliers=suppliers,
        product_names=recent_product_names(),
        active_count=active_count,
        archived_count=archived_count,
        recent=recent,
        active_by_supplier=active_by_supplier,
        limits=limits,
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("home"))

    if request.method == "POST":
        ip = request.remote_addr or "?"
        if login_is_throttled(ip):
            return render_template(
                "login.html",
                mode="login",
                error="تلاش‌های ورود زیاد است. چند دقیقه بعد دوباره امتحان کنید.",
            ), 429
        username = clean_username(request.form.get("username", ""))
        password = request.form.get("password", "")
        user = user_by_username(username)
        if user and len(password) <= 128 and check_password_hash(user.password_hash, password):
            _rate_events.pop(("login", str(ip)), None)
            return login_handoff(user)
        record_login_fail(ip)
        return render_template(
            "login.html", mode="login", error="نام کاربری یا رمز عبور اشتباه است."
        ), 401

    return render_template("login.html", mode="login", error=None)


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for("home"))

    if request.method == "POST":
        if rate_limited("signup", request.remote_addr or "?", 6, 600):
            return render_template(
                "login.html",
                mode="signup",
                error="تعداد ثبت‌نام از این اتصال زیاد است؛ کمی بعد دوباره تلاش کنید.",
            ), 429
        first_name_raw = request.form.get("first_name", "")
        last_name_raw = request.form.get("last_name", "")
        first_name = clean_person_name(first_name_raw)
        last_name = clean_person_name(last_name_raw)
        mobile_raw = request.form.get("mobile", "")
        mobile = normalize_mobile(mobile_raw)
        username = clean_username(request.form.get("username", ""))
        password = request.form.get("password", "")

        def signup_error(message, status=400):
            return render_template("login.html", mode="signup", error=message), status

        if not first_name or not last_name or not mobile_raw.strip() or not username or not password:
            return signup_error(
                "نام، نام خانوادگی، شماره موبایل، نام کاربری و رمز عبور الزامی است."
            )
        if len(first_name_raw.strip()) > 100 or len(last_name_raw.strip()) > 100:
            return signup_error("نام و نام خانوادگی هرکدام حداکثر ۱۰۰ کاراکتر باشند.")
        if not mobile:
            return signup_error(
                "شماره موبایل معتبر نیست؛ شماره را مانند ۰۹۱۲۱۲۳۴۵۶۷ وارد کنید."
            )
        if not username_is_valid(username):
            return signup_error(
                "نام کاربری فقط می‌تواند شامل حروف، عدد، نقطه، خط تیره و زیرخط باشد."
            )
        if username in RESERVED_USERNAMES:
            return signup_error("این نام کاربری رزرو شده و قابل ثبت عمومی نیست.", 403)
        if not MIN_PASSWORD_LENGTH <= len(password) <= 128:
            return signup_error(
                f"رمز عبور باید بین {MIN_PASSWORD_LENGTH} و ۱۲۸ کاراکتر باشد."
            )
        if user_by_username(username):
            return signup_error("این نام کاربری قبلاً وجود دارد.", 409)

        user = User(
            username=username,
            password_hash=generate_password_hash(password),
            first_name=first_name,
            last_name=last_name,
            mobile=mobile,
            is_licensed=False,
            license_type="free",
            is_admin=False,
        )
        db.session.add(user)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return signup_error("این نام کاربری هم‌زمان توسط حساب دیگری ثبت شد.", 409)
        claim_orphaned_data(user.id)
        return login_handoff(user)

    return render_template("login.html", mode="signup", error=None)


@app.route("/logout", methods=["POST"])
def logout():
    logout_user()
    response = redirect(url_for("login"))
    response.delete_cookie(
        AUTH_COOKIE,
        path="/",
        secure=COOKIE_SECURE,
        samesite="None" if CROSS_SITE_COOKIES else "Lax",
    )
    return response


@app.route("/search")
def search():
    return render_template("search.html")


@app.route("/new-purchase", methods=["GET", "POST"])
def new_purchase():
    if request.method == "GET":
        return redirect(url_for("home"))

    fields, error = read_product_form()
    if error:
        return json_error(error)

    try:
        user = locked_user(current_user.id)
        limits = get_user_limits(user)
        if not limits["can_add_product"]:
            message = "سقف نسخه آزمایشی (۵ محصول) تکمیل شده است. برای ثبت محصول جدید باید لایسنس لیستیا را تهیه کنید."
            if limits.get("is_expired"):
                message = "مدت زمان لایسنس شما به پایان رسیده است. جهت ثبت محصولات بیشتر، لایسنس خود را تمدید فرمایید."
            db.session.rollback()
            return json_error(message, 403, {"license_locked": True})

        try:
            supplier_id = int(fields["supplier_id"])
        except (TypeError, ValueError):
            db.session.rollback()
            return json_error("تأمین‌کننده معتبر نیست.")

        supplier = Supplier.query.filter_by(id=supplier_id, owner_id=user.id).first()
        if not supplier:
            db.session.rollback()
            return json_error("تأمین‌کننده معتبر نیست.")

        new_product = Product(
            owner_id=user.id,
            supplier_id=supplier.id,
            product_name=fields["product_name"],
            quantity=fields["quantity"],
            unit=fields["unit"],
            description=fields["description"],
        )
        db.session.add(new_product)
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("Could not create product for user %s", current_user.id)
        return json_error("ثبت محصول انجام نشد؛ دوباره تلاش کنید.", 500)

    return {
        "success": True,
        "message": "خرید ثبت شد",
        "product": product_payload(new_product, supplier.name),
    }


@app.route("/check-duplicate")
def check_duplicate():
    product_name = request.args.get("product", "").strip()
    current_supplier_id = request.args.get("supplier")

    if not product_name or len(product_name) > 300:
        return {"matches": []}

    matches = (
        user_products(ordered=False)
        .filter(db.func.lower(Product.product_name) == product_name.lower())
        .limit(20)
        .all()
    )

    return {
        "matches": [
            {
                "id": m.id,
                "supplier_name": m.supplier.name,
                "quantity": m.quantity,
                "unit": m.unit,
                "same_supplier": str(m.supplier_id) == str(current_supplier_id),
            }
            for m in matches
        ]
    }


@app.route("/purchases")
def purchases():
    # اول بر اساس تأمین‌کننده گروه می‌شود، بعد داخل هر تأمین‌کننده به ترتیب ورود.
    all_products = (
        user_products(ordered=False)
        .join(Supplier, Product.supplier_id == Supplier.id)
        .order_by(Supplier.name.asc(), Product.id.asc())
        .all()
    )
    all_products.sort(
        key=lambda item: (
            supplier_sort_key(item.supplier.name if item.supplier else ""),
            item.id,
        )
    )
    return render_template("purchases.html", products=all_products, suppliers=user_suppliers())


@app.route("/toggle-order/<int:product_id>", methods=["POST"])
def toggle_order(product_id):
    product = owned_product_or_404(product_id)
    product.ordered = True
    product.ordered_date = local_today()
    db.session.commit()

    if wants_json():
        return {"success": True, "product": product_payload(product)}
    return safe_redirect("/purchases")


@app.route("/unarchive/<int:product_id>", methods=["POST"])
def unarchive_product(product_id):
    product = owned_product_or_404(product_id)
    product.ordered = False
    product.ordered_date = None
    db.session.commit()

    if wants_json():
        return {"success": True, "product": product_payload(product)}
    return safe_redirect(f"/supplier/{product.supplier_id}")


@app.route("/product/<int:product_id>/edit", methods=["GET", "POST"])
def edit_product(product_id):
    product = owned_product_or_404(product_id)
    if request.method == "GET":
        return redirect(f"/supplier/{product.supplier_id}?highlight={product.id}")

    fields, error = read_product_form()
    if error:
        return json_error(error)
    try:
        supplier_id = int(fields["supplier_id"])
    except (TypeError, ValueError):
        return json_error("تأمین‌کننده معتبر نیست.")

    target_supplier = Supplier.query.filter_by(
        id=supplier_id, owner_id=current_user.id
    ).first()
    if not target_supplier:
        return json_error("تأمین‌کننده معتبر نیست.")

    product.supplier_id = target_supplier.id
    product.product_name = fields["product_name"]
    product.quantity = fields["quantity"]
    product.unit = fields["unit"]
    product.description = fields["description"]
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("Could not edit product %s", product_id)
        return json_error("ویرایش محصول انجام نشد؛ دوباره تلاش کنید.", 500)

    if wants_json():
        return {"success": True, "product": product_payload(product, target_supplier.name)}
    return redirect(f"/supplier/{product.supplier_id}")


@app.route("/product/<int:product_id>/delete", methods=["POST"])
def delete_product(product_id):
    product = owned_product_or_404(product_id)
    supplier_id = product.supplier_id
    db.session.delete(product)
    db.session.commit()

    if wants_json():
        return {"success": True}
    return redirect(f"/supplier/{supplier_id}")


@app.route("/product/<int:product_id>/info")
def product_info(product_id):
    product = owned_product_or_404(product_id)
    return {"product": product_payload(product)}


@app.route("/supplier/<int:supplier_id>/archive/<date_str>/delete", methods=["POST"])
def delete_archive_group(supplier_id, date_str):
    owned_supplier_or_404(supplier_id)

    query = Product.query.filter_by(
        owner_id=current_user.id,
        supplier_id=supplier_id,
        ordered=True,
    )
    if date_str == "unknown":
        query = query.filter(Product.ordered_date.is_(None))
    else:
        try:
            target_date = date.fromisoformat(date_str)
        except ValueError:
            return json_error("تاریخ آرشیو معتبر نیست.")
        query = query.filter(Product.ordered_date == target_date)

    deleted = query.delete(synchronize_session=False)
    db.session.commit()

    if wants_json():
        return {"success": True, "deleted": deleted}
    return redirect(f"/supplier/{supplier_id}")


@app.route("/suppliers", methods=["GET", "POST"])
def suppliers():
    if request.method == "POST":
        name = " ".join(request.form.get("name", "").split())
        if not name:
            return json_error("نام تأمین‌کننده را وارد کنید.")
        if len(name) > 200:
            return json_error("نام تأمین‌کننده حداکثر ۲۰۰ کاراکتر باشد.")

        try:
            user = locked_user(current_user.id)
            limits = get_user_limits(user)
            if not limits["can_add_supplier"]:
                message = "سقف نسخه آزمایشی (۱ تأمین‌کننده) تکمیل شده است. برای ثبت تأمین‌کنندگان بیشتر باید لایسنس لیستیا را تهیه کنید."
                if limits.get("is_expired"):
                    message = "مدت زمان لایسنس شما به پایان رسیده است. جهت ثبت تأمین‌کنندگان بیشتر، لایسنس خود را تمدید فرمایید."
                db.session.rollback()
                return json_error(message, 403, {"license_locked": True})

            if owned_supplier_by_normalized_name(name, user.id):
                db.session.rollback()
                return json_error("این تأمین‌کننده قبلاً ثبت شده است.", 409)

            new_supplier = Supplier(name=name, owner_id=user.id)
            db.session.add(new_supplier)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return json_error("این تأمین‌کننده هم‌زمان ثبت شده است.", 409)
        except SQLAlchemyError:
            db.session.rollback()
            logger.exception("Could not create supplier for user %s", current_user.id)
            return json_error("ثبت تأمین‌کننده انجام نشد؛ دوباره تلاش کنید.", 500)

        if wants_json():
            return {"success": True, "id": new_supplier.id, "name": new_supplier.name}
        return redirect(url_for("suppliers"))

    limits = get_user_limits(current_user)
    g.limits = limits
    return render_template("suppliers.html", suppliers=user_suppliers(), limits=limits)


@app.route("/supplier/<int:supplier_id>")
def supplier_detail(supplier_id):
    supplier = owned_supplier_or_404(supplier_id)
    active_products = (
        Product.query.filter_by(owner_id=current_user.id, supplier_id=supplier_id)
        .filter(active_clause())
        .order_by(Product.id.desc())
        .all()
    )

    archived_products = (
        Product.query.filter_by(owner_id=current_user.id, supplier_id=supplier_id)
        .filter(archived_clause())
        .order_by(Product.ordered_date.desc().nullslast(), Product.id.desc())
        .all()
    )

    groups = {}
    for item in archived_products:
        if item.ordered_date:
            iso_key = item.ordered_date.isoformat()
            label = shamsi_label(item.ordered_date)
        else:
            iso_key = "unknown"
            label = "بدون تاریخ"

        if iso_key not in groups:
            groups[iso_key] = {"label": label, "products": []}
        groups[iso_key]["products"].append(item)

    today = local_today()
    return render_template(
        "supplier_detail.html",
        supplier=supplier,
        suppliers=user_suppliers(),
        products=active_products,
        groups=groups,
        today_iso=today.isoformat(),
        today_label=shamsi_label(today),
        highlight_id=request.args.get("highlight", ""),
    )


@app.route("/supplier/<int:supplier_id>/edit", methods=["GET", "POST"])
def edit_supplier(supplier_id):
    supplier = owned_supplier_or_404(supplier_id)
    if request.method != "POST":
        return redirect(url_for("suppliers"))

    new_name = " ".join(request.form.get("name", "").split())
    if not new_name:
        return json_error("نام تأمین‌کننده را وارد کنید.")
    if len(new_name) > 200:
        return json_error("نام تأمین‌کننده حداکثر ۲۰۰ کاراکتر باشد.")

    clash = owned_supplier_by_normalized_name(new_name)
    if clash and clash.id != supplier.id:
        return json_error("این نام قبلاً ثبت شده است.", 409)

    supplier.name = new_name
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return json_error("این نام قبلاً ثبت شده است.", 409)
    return {"success": True, "id": supplier.id, "name": supplier.name}


@app.route("/supplier/<int:supplier_id>/delete", methods=["POST"])
def delete_supplier(supplier_id):
    supplier = owned_supplier_or_404(supplier_id)
    Product.query.filter_by(
        owner_id=current_user.id, supplier_id=supplier.id
    ).delete(synchronize_session=False)
    db.session.delete(supplier)
    db.session.commit()

    if wants_json():
        return {"success": True}
    return redirect(url_for("suppliers"))


@app.route("/download-template")
def download_template():
    return send_file(
        os.path.join(BASE_DIR, "static", "sample_import.xlsx"),
        as_attachment=True,
        download_name="نمونه_ایمپورت_لیستیا.xlsx",
    )


@app.route("/api/search")
def api_search():
    query = request.args.get("q", "").strip()

    if not query or len(query) < 2:
        return {"results": [], "suppliers": []}
    if len(query) > 100:
        return json_error("عبارت جستجو حداکثر ۱۰۰ کاراکتر باشد.")

    escaped_query = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped_query}%"
    matches = (
        user_products()
        .join(Supplier, Product.supplier_id == Supplier.id)
        .filter(
            or_(
                Product.product_name.ilike(pattern, escape="\\"),
                Supplier.name.ilike(pattern, escape="\\"),
            )
        )
        .order_by(Product.ordered.asc(), Product.id.desc())
        .limit(50)
        .all()
    )

    matching_suppliers = (
        Supplier.query.filter(
            Supplier.owner_id == current_user.id,
            Supplier.name.ilike(pattern, escape="\\"),
        )
        .order_by(Supplier.name)
        .limit(8)
        .all()
    )
    supplier_ids = [supplier.id for supplier in matching_suppliers]
    active_counts = active_counts_by_supplier(supplier_ids)

    return {
        "results": [product_payload(item) for item in matches],
        "suppliers": [
            {
                "id": supplier.id,
                "name": supplier.name,
                "active_count": active_counts.get(supplier.id, 0),
            }
            for supplier in matching_suppliers
        ],
    }


@app.route("/import", methods=["GET", "POST"])
def import_excel():
    limits = get_user_limits(current_user)
    g.limits = limits

    if request.method == "GET":
        return render_template("import_excel.html", message=None, errors=None, limits=limits)

    def render_import_error(message, status=400):
        return render_template(
            "import_excel.html",
            message=message,
            success=False,
            errors=None,
            limits=limits,
        ), status

    uploaded_file = request.files.get("file")
    if not uploaded_file or not uploaded_file.filename:
        return render_import_error("فایلی انتخاب نشده است.")

    import difflib

    import pandas as pd

    filename = uploaded_file.filename.lower()
    if not filename.endswith((".csv", ".xlsx")):
        return render_import_error("فقط فایل CSV یا Excel با پسوند xlsx مجاز است.")

    if filename.endswith(".xlsx"):
        try:
            with ZipFile(uploaded_file.stream) as archive:
                members = archive.infolist()
                uncompressed_size = sum(item.file_size for item in members)
                if len(members) > 1_000 or uncompressed_size > 64 * 1024 * 1024:
                    return render_import_error("حجم بازشده فایل اکسل بیش از حد مجاز است.", 413)
            uploaded_file.stream.seek(0)
        except (BadZipFile, OSError):
            return render_import_error("فایل xlsx معتبر نیست.")

    try:
        if filename.endswith(".csv"):
            dataframe = pd.read_csv(
                uploaded_file,
                encoding="utf-8-sig",
                nrows=MAX_IMPORT_ROWS + 1,
            )
        else:
            dataframe = pd.read_excel(uploaded_file, nrows=MAX_IMPORT_ROWS + 1)
    except Exception:  # Third-party parsers expose many format-specific errors.
        logger.info("Rejected unreadable import file", exc_info=True)
        return render_import_error("خطا در خواندن فایل. قالب و پسوند فایل را بررسی کنید.")

    dataframe = dataframe.dropna(how="all")
    if len(dataframe) > MAX_IMPORT_ROWS:
        return render_import_error(
            f"هر فایل حداکثر می‌تواند {MAX_IMPORT_ROWS:,} ردیف داده داشته باشد."
        )
    if len(dataframe.columns) < 4:
        return render_import_error(
            "فایل باید دست‌کم چهار ستون تأمین‌کننده، محصول، تعداد و واحد داشته باشد."
        )

    shown_errors = []
    error_count = 0

    def add_error(message):
        nonlocal error_count
        error_count += 1
        if len(shown_errors) < 100:
            shown_errors.append(message)

    added_count = 0
    try:
        user = locked_user(current_user.id)
        limits = get_user_limits(user)
        existing_suppliers = Supplier.query.filter_by(owner_id=user.id).all()
        normalized_map = {normalize_name(item.name): item for item in existing_suppliers}
        normalized_names = list(normalized_map)
        current_product_count = limits["product_count"]
        current_supplier_count = limits["supplier_count"]

        for excel_row_number, row in enumerate(dataframe.itertuples(index=False, name=None), start=2):
            supplier_name = "" if pd.isna(row[0]) else " ".join(str(row[0]).split())
            product_name = "" if pd.isna(row[1]) else str(row[1]).strip()
            quantity_raw = "" if pd.isna(row[2]) else str(row[2]).strip()
            unit = "" if pd.isna(row[3]) else str(row[3]).strip()
            description = (
                "" if len(row) < 5 or pd.isna(row[4]) else str(row[4]).strip()
            )

            if not supplier_name:
                add_error(f"ردیف {excel_row_number}: نام تأمین‌کننده خالی است")
                continue
            if len(supplier_name) > 200:
                add_error(f"ردیف {excel_row_number}: نام تأمین‌کننده بیش از ۲۰۰ کاراکتر است")
                continue
            if not product_name:
                add_error(f"ردیف {excel_row_number}: نام محصول خالی است")
                continue
            if len(product_name) > 300:
                add_error(f"ردیف {excel_row_number}: نام محصول بیش از ۳۰۰ کاراکتر است")
                continue
            if len(description) > 500:
                add_error(f"ردیف {excel_row_number}: توضیحات بیش از ۵۰۰ کاراکتر است")
                continue
            quantity = normalize_quantity(quantity_raw)
            if not quantity:
                add_error(f"ردیف {excel_row_number}: تعداد باید عددی مثبت و معتبر باشد")
                continue
            if unit not in UNIT_TYPES:
                add_error(f"ردیف {excel_row_number}: نوع تعداد «{unit}» معتبر نیست")
                continue
            if not limits["is_licensed"] and current_product_count + added_count >= FREE_MAX_PRODUCTS:
                add_error(
                    f"ردیف {excel_row_number}: سقف ۵ محصول نسخه آزمایشی پر است؛ «{product_name}» ثبت نشد"
                )
                continue

            normalized_supplier_name = normalize_name(supplier_name)
            supplier = normalized_map.get(normalized_supplier_name)
            if not supplier:
                close = difflib.get_close_matches(
                    normalized_supplier_name, normalized_names, n=1, cutoff=0.82
                )
                if close:
                    add_error(
                        f"ردیف {excel_row_number}: نام «{supplier_name}» شبیه «{normalized_map[close[0]].name}» است؛ نام را یکسان کنید"
                    )
                    continue
                if not limits["is_licensed"] and current_supplier_count >= FREE_MAX_SUPPLIERS:
                    add_error(
                        f"ردیف {excel_row_number}: سقف یک تأمین‌کننده نسخه آزمایشی پر است؛ «{supplier_name}» ساخته نشد"
                    )
                    continue

                supplier = Supplier(name=supplier_name, owner_id=user.id)
                db.session.add(supplier)
                db.session.flush()
                normalized_map[normalized_supplier_name] = supplier
                normalized_names.append(normalized_supplier_name)
                current_supplier_count += 1

            db.session.add(
                Product(
                    owner_id=user.id,
                    supplier_id=supplier.id,
                    product_name=product_name,
                    quantity=quantity,
                    unit=unit,
                    description=description,
                )
            )
            added_count += 1

        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("Import failed for user %s", current_user.id)
        return render_import_error("ذخیره‌سازی فایل انجام نشد؛ دوباره تلاش کنید.", 500)

    message = f"✓ {added_count} ردیف با موفقیت ثبت شد."
    if error_count:
        message += f" ({error_count} ردیف رد شد)"
        if error_count > len(shown_errors):
            shown_errors.append(
                f"و {error_count - len(shown_errors)} خطای دیگر؛ فایل را به بخش‌های کوچک‌تر تقسیم کنید."
            )

    refreshed_limits = get_user_limits(current_user)
    g.limits = refreshed_limits
    return render_template(
        "import_excel.html",
        message=message,
        success=added_count > 0,
        errors=shown_errors or None,
        limits=refreshed_limits,
    )

# ---------------------------------------------------------------------------
#  API کلیددار — برای ثبت از بیرون اپ (Shortcuts آیفون، ویجت، هر چیز دیگر)
# ---------------------------------------------------------------------------


def resolve_supplier(user, raw, limits=None):
    """Resolve a supplier by owned ID/name, or create one within quota."""
    raw = " ".join((raw or "").split())
    limits = limits or get_user_limits(user)
    if len(raw) > 200:
        return None, False, "نام تأمین‌کننده حداکثر ۲۰۰ کاراکتر باشد."

    if raw.isdigit():
        found = Supplier.query.filter_by(id=int(raw), owner_id=user.id).first()
        if found:
            return found, False, None

    if raw:
        found = owned_supplier_by_normalized_name(raw, user.id)
        if found:
            return found, False, None
        if not limits["can_add_supplier"]:
            return None, False, "سقف ۱ تأمین‌کننده نسخه آزمایشی پر شده است. نیاز به لایسنس."

        created = Supplier(name=raw, owner_id=user.id)
        db.session.add(created)
        db.session.flush()
        return created, True, None

    last = Product.query.filter_by(owner_id=user.id).order_by(Product.id.desc()).first()
    if last:
        supplier = Supplier.query.filter_by(id=last.supplier_id, owner_id=user.id).first()
        if supplier:
            return supplier, False, None

    first = Supplier.query.filter_by(owner_id=user.id).order_by(Supplier.id).first()
    if first:
        return first, False, None
    if not limits["can_add_supplier"]:
        return None, False, "سقف ۱ تأمین‌کننده نسخه آزمایشی پر شده است. نیاز به لایسنس."

    created = Supplier(name="نامشخص", owner_id=user.id)
    db.session.add(created)
    db.session.flush()
    return created, True, None


@app.route("/api/quick-add", methods=["POST"])
def api_quick_add():
    """Register one item with an API key; no browser session is required."""
    authenticated_user = user_from_api_token()
    if not authenticated_user:
        return json_error("کلید معتبر نیست.", 401)
    if rate_limited("quick-add", authenticated_user.id, 120, 60):
        return json_error("تعداد درخواست‌ها زیاد است؛ یک دقیقه بعد دوباره تلاش کنید.", 429)

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        body = {}

    def field(name, default=""):
        value = request.form.get(name)
        if value is None:
            value = body.get(name, default)
        return str(value if value is not None else default).strip()

    product_name = field("product") or field("name") or field("text")
    form_data = {
        "product": product_name,
        "quantity": field("quantity") or field("qty") or "1",
        "unit": field("unit") or UNIT_TYPES[0],
        "description": field("description"),
        "supplier": field("supplier"),
    }
    fields, error = read_product_form(form_data)
    if error:
        return json_error(error)

    try:
        user = locked_user(authenticated_user.id)
        limits = get_user_limits(user)
        if not limits["can_add_product"]:
            db.session.rollback()
            return json_error(
                "سقف ۵ محصول در نسخه آزمایشی تکمیل شده است. برای ثبت محصولات بیشتر باید لایسنس تهیه کنید.",
                403,
                {"license_locked": True},
            )

        supplier, supplier_created, error = resolve_supplier(
            user, fields["supplier_id"], limits
        )
        if error:
            db.session.rollback()
            return json_error(error, 403, {"license_locked": True})

        product = Product(
            owner_id=user.id,
            supplier_id=supplier.id,
            product_name=fields["product_name"],
            quantity=fields["quantity"],
            unit=fields["unit"],
            description=fields["description"],
        )
        db.session.add(product)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return json_error("تأمین‌کننده تکراری است؛ فهرست را تازه و دوباره تلاش کنید.", 409)
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("Quick-add failed for user %s", authenticated_user.id)
        return json_error("ثبت کالا انجام نشد؛ دوباره تلاش کنید.", 500)

    message = f"«{fields['product_name']}» {fields['quantity']} {fields['unit']} برای {supplier.name} ثبت شد"
    if supplier_created:
        message += " (تأمین‌کننده جدید ساخته شد)"

    return {
        "success": True,
        "message": message,
        "product": product_payload(product, supplier.name),
    }


@app.route("/api/suppliers")
def api_suppliers():
    """لیست تأمین‌کننده‌ها برای منوی انتخابی در Shortcuts."""
    user = user_from_api_token()
    if not user:
        return json_error("کلید معتبر نیست.", 401)

    rows = (
        Supplier.query.filter_by(owner_id=user.id).order_by(Supplier.name).all()
    )
    return {
        "success": True,
        "units": UNIT_TYPES,
        "suppliers": [{"id": s.id, "name": s.name} for s in rows],
    }


@app.route("/api/dashboard/stats")
def api_dashboard_stats():
    """شمارنده‌های زنده داشبورد و سهمیه آزمایشی سایدبار.

    رابط بعد از باز شدن صفحه و هر تغییر این مسیر را می‌خواند تا تعداد کل
    محصول/تأمین‌کننده و کارت‌های داشبورد دقیقاً با دیتابیس یکی باشند؛ حتی
    برای داده‌های ثبت‌شده در نسخه‌های قبلی یا دستگاه دیگر.
    """
    if not current_user.is_authenticated:
        return json_error("ابتدا وارد شوید.", 401)

    counters = dashboard_counters()
    limits = get_user_limits(
        current_user,
        supplier_count=counters["supplier_count"],
        product_count=counters["product_count"],
    )
    return {
        "success": True,
        "active_count": counters["active_count"],
        "archived_count": counters["archived_count"],
        "product_count": counters["product_count"],
        "supplier_count": counters["supplier_count"],
        "max_products": limits["max_products"],
        "max_suppliers": limits["max_suppliers"],
        "can_add_product": limits["can_add_product"],
        "can_add_supplier": limits["can_add_supplier"],
        "is_licensed": limits["is_licensed"],
        "suppliers": [
            {
                "id": s.id,
                "name": s.name,
                "active_count": counters["suppliers"].get(s.id, 0),
            }
            for s in user_suppliers()
        ],
    }


@app.route("/api/license-status")
def api_license_status():
    """دریافت وضعیت لایسنس کاربر برای UI"""
    limits = get_user_limits(current_user)
    return {"success": True, "limits": limits}


@app.route("/account/license", methods=["POST"])
def activate_license():
    """Activate a valid key once for the current account."""
    key = request.form.get("license_key", "").strip().upper()
    if not key:
        return json_error("لطفاً کلید لایسنس را وارد کنید.")
    if len(key) > 160:
        return json_error("کلید لایسنس معتبر نیست.")
    if rate_limited("license", current_user.id, 20, 300):
        return json_error("تلاش‌های فعال‌سازی زیاد است؛ چند دقیقه بعد دوباره امتحان کنید.", 429)

    try:
        user = locked_user(current_user.id)
        is_valid, tier, days, message = verify_key(user, key)
        if not is_valid:
            db.session.rollback()
            return json_error(message)

        digest = license_key_hash(key)
        already_used = LicenseActivation.query.filter_by(
            user_id=user.id, key_hash=digest
        ).first()
        if already_used or (
            user.license_key and hmac.compare_digest(user.license_key.upper(), key)
        ):
            db.session.rollback()
            return json_error(
                "این کلید قبلاً برای این حساب فعال شده است. برای تمدید، کلید تازه تهیه کنید.",
                409,
            )

        # Preserve the previous key in history before replacing it, including
        # installations upgraded from versions that had no activation table.
        mark_license_key_used(user, user.license_key)
        mark_license_key_used(user, key)

        activated_at = utcnow()
        user.is_licensed = True
        user.license_key = key
        user.licensed_at = activated_at
        user.license_type = tier or "PRO"
        if days is not None:
            user.license_expires_at = activated_at + timedelta(days=days)
            validity_text = (
                f"اعتبار به مدت {days} روز (تا {shamsi_label(user.license_expires_at.date())})"
            )
        else:
            user.license_expires_at = None
            validity_text = "اعتبار مادام‌العمر (دائمی)"
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return json_error("این کلید قبلاً برای این حساب فعال شده است.", 409)
    except SQLAlchemyError:
        db.session.rollback()
        logger.exception("License activation failed for user %s", current_user.id)
        return json_error("فعال‌سازی انجام نشد؛ دوباره تلاش کنید.", 500)

    return {
        "success": True,
        "message": f"✓ لایسنس با موفقیت فعال شد! {validity_text}",
        "license_type": user.license_type,
        "is_licensed": True,
        "expires_at": user.license_expires_at.isoformat() if user.license_expires_at else None,
        "validity_text": validity_text,
    }


# ---------------------------------------------------------------------------
#  پنل ادمین: تولید لایسنس داخل وب‌اپلیکیشن (مخصوص س.م.قتالی smq2458)
# ---------------------------------------------------------------------------

def admin_user_payload(user, supplier_count=None, product_count=None):
    """اطلاعات یک کاربر برای جدول مدیریت (پنل مدیر)."""
    user_limits = get_user_limits(
        user,
        supplier_count=supplier_count,
        product_count=product_count,
    )
    return {
        "id": user.id,
        "username": user.username,
        "first_name": user.first_name or "",
        "last_name": user.last_name or "",
        "full_name": " ".join(
            part for part in (user.first_name or "", user.last_name or "") if part
        ),
        "mobile": user.mobile or "",
        "user_code": user_limits["user_code"],
        "is_licensed": user_limits["is_licensed"],
        "is_expired": user_limits["is_expired"],
        "is_lifetime": user_limits["is_lifetime"],
        "remaining_days": user_limits["remaining_days"],
        "expires_at_label": user_limits["expires_at_label"],
        "expires_at_iso": user.license_expires_at.date().isoformat() if user.license_expires_at else "",
        "supplier_count": user_limits["supplier_count"],
        "product_count": user_limits["product_count"],
        "license_type": user.license_type or "free",
        "license_key": user.license_key or "",
        "is_admin": bool(is_admin_user(user)),
        "is_protected": bool(
            user.username and user.username.lower() == PRIMARY_ADMIN_USERNAME
        ),
        "is_self": bool(current_user.is_authenticated and user.id == current_user.id),
    }


def admin_target_user(user_id):
    """(user, error) — کاربر هدف عملیات مدیریتی."""
    if not is_admin_user(current_user):
        return None, json_error("دسترسی به این بخش فقط برای مدیر نرم‌افزار مجاز است.", 403)
    user = db.session.get(User, user_id)
    if not user:
        return None, json_error("کاربر پیدا نشد.", 404)
    return user, None


def refresh_license_key(user):
    """Reissue the stored key after a username change without extending time."""
    if not user.is_licensed:
        return None
    mark_license_key_used(user, user.license_key)
    if user.license_expires_at:
        days = max(1, (user.license_expires_at - utcnow()).days + 1)
        period_code, _, _ = normalize_duration(str(days))
    else:
        period_code = "LIFE"
    user.license_key = generate_key(
        user.username, user.license_type or "PRO", period_code
    )
    mark_license_key_used(user, user.license_key)
    return user.license_key


@app.route("/admin/license-generator")
def admin_license_generator():
    if not is_admin_user(current_user):
        return json_error("دسترسی به این بخش فقط برای مدیر نرم‌افزار مجاز است.", 403)

    all_users = User.query.order_by(User.id.desc()).all()
    supplier_counts = dict(
        db.session.query(Supplier.owner_id, db.func.count(Supplier.id))
        .group_by(Supplier.owner_id)
        .all()
    )
    product_counts = dict(
        db.session.query(Product.owner_id, db.func.count(Product.id))
        .group_by(Product.owner_id)
        .all()
    )
    users = [
        admin_user_payload(
            user,
            supplier_count=supplier_counts.get(user.id, 0),
            product_count=product_counts.get(user.id, 0),
        )
        for user in all_users
    ]
    g.quota_supplier_count = supplier_counts.get(current_user.id, 0)
    g.quota_product_count = product_counts.get(current_user.id, 0)
    return render_template("admin_license.html", users=users)


@app.route("/api/admin/users/<int:user_id>/update", methods=["POST"])
def api_admin_update_user(user_id):
    """Edit profile, username, password, and role as an administrator."""
    user, error = admin_target_user(user_id)
    if error:
        return error

    username_was_sent = "username" in request.form
    username = clean_username(request.form.get("username", ""))
    new_password = request.form.get("new_password", "")
    admin_flag = request.form.get("is_admin")
    changes = []

    for field_name, label in (
        ("first_name", "نام"),
        ("last_name", "نام خانوادگی"),
    ):
        if field_name not in request.form:
            continue
        raw_value = request.form.get(field_name, "")
        if len(raw_value.strip()) > 100:
            return json_error(f"{label} حداکثر ۱۰۰ کاراکتر باشد.")
        value = clean_person_name(raw_value)
        if not value:
            return json_error(f"{label} کاربر را وارد کنید.")
        if value != (getattr(user, field_name) or ""):
            setattr(user, field_name, value)
            changes.append(f"{label} تغییر کرد")

    if "mobile" in request.form:
        mobile = normalize_mobile(request.form.get("mobile", ""))
        if not mobile:
            return json_error(
                "شماره موبایل معتبر نیست؛ شماره را مانند ۰۹۱۲۱۲۳۴۵۶۷ وارد کنید."
            )
        if mobile != (user.mobile or ""):
            user.mobile = mobile
            changes.append("شماره موبایل تغییر کرد")

    if username_was_sent:
        if not username_is_valid(username):
            return json_error(
                "نام کاربری فقط می‌تواند شامل حروف، عدد، نقطه، خط تیره و زیرخط باشد."
            )
        if username != user.username:
            if user.username.lower() == PRIMARY_ADMIN_USERNAME:
                return json_error("نام کاربری مدیر اصلی سامانه قابل تغییر نیست.")
            if username in RESERVED_USERNAMES:
                return json_error("این نام کاربری رزرو شده است.")
            if user_by_username(username):
                return json_error("این نام کاربری قبلاً وجود دارد.", 409)
            old_username = user.username
            user.username = username
            refresh_license_key(user)
            changes.append(
                f"نام کاربری از «{old_username}» به «{username}» تغییر کرد"
            )

    if new_password:
        if not MIN_PASSWORD_LENGTH <= len(new_password) <= 128:
            return json_error(
                f"رمز عبور جدید باید بین {MIN_PASSWORD_LENGTH} و ۱۲۸ کاراکتر باشد."
            )
        if check_password_hash(user.password_hash, new_password):
            return json_error("رمز عبور جدید با رمز فعلی یکسان است.")
        user.password_hash = generate_password_hash(new_password)
        changes.append("رمز عبور بازنشانی شد و نشست‌های قبلی بسته شدند")

    if admin_flag is not None:
        wants_admin = admin_flag in ("1", "true", "on")
        if not wants_admin and user.id == current_user.id:
            return json_error("دسترسی مدیریت حساب خودتان را نمی‌توانید بردارید.")
        if not wants_admin and user.username.lower() == PRIMARY_ADMIN_USERNAME:
            return json_error("این حساب مدیر اصلی سامانه است و قابل تغییر نیست.")
        if bool(user.is_admin) != wants_admin:
            user.is_admin = wants_admin
            if wants_admin:
                user.is_licensed = True
                user.license_type = "UNLIMITED"
                user.license_expires_at = None
            changes.append(
                "دسترسی مدیریت " + ("داده شد" if wants_admin else "برداشته شد")
            )

    if not changes:
        return json_error("تغییری برای ذخیره وجود ندارد.")

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return json_error("نام کاربری قبلاً ثبت شده است.", 409)
    return {
        "success": True,
        "message": "✓ " + "، ".join(changes),
        "user": admin_user_payload(user),
    }


@app.route("/api/admin/users/<int:user_id>/license", methods=["POST"])
def api_admin_update_license(user_id):
    """Grant, update, or revoke one user's license as an administrator."""
    user, error = admin_target_user(user_id)
    if error:
        return error

    action = request.form.get("action", "grant").strip().lower()
    if action in {"revoke", "delete", "remove"}:
        if user.username.lower() == PRIMARY_ADMIN_USERNAME:
            return json_error("لایسنس حساب مدیر اصلی قابل حذف نیست.")
        if user.is_admin:
            return json_error("پیش از حذف لایسنس، دسترسی مدیریت این کاربر را بردارید.")
        mark_license_key_used(user, user.license_key)
        user.is_licensed = False
        user.license_key = None
        user.licensed_at = None
        user.license_expires_at = None
        user.license_type = "free"
        db.session.commit()
        return {
            "success": True,
            "message": f"لایسنس «{user.username}» حذف شد و حساب به نسخه آزمایشی برگشت.",
            "user": admin_user_payload(user),
        }
    if action != "grant":
        return json_error("عملیات لایسنس معتبر نیست.")

    tier = request.form.get("tier", "PRO").strip().upper() or "PRO"
    if tier not in ALLOWED_LICENSE_TIERS:
        return json_error("نوع لایسنس معتبر نیست.")

    duration = request.form.get("duration", "LIFE").strip()
    expires_at_raw = request.form.get("expires_at", "").strip()
    if duration.upper() == "CUSTOM":
        duration = request.form.get("custom_days", "").strip()

    if expires_at_raw:
        try:
            target = date.fromisoformat(expires_at_raw)
        except ValueError:
            return json_error("تاریخ انقضا معتبر نیست.")
        days = (target - local_today()).days
        if not 1 <= days <= 3650:
            return json_error("تاریخ انقضا باید بین فردا و ۱۰ سال آینده باشد.")
        period_info = validate_duration(str(days))
        local_expiry = datetime.combine(target, datetime.max.time()).replace(
            tzinfo=ZoneInfo("Asia/Tehran")
        )
        expires_at = local_expiry.astimezone(UTC).replace(tzinfo=None)
    else:
        period_info = validate_duration(duration)
        if not period_info:
            return json_error("مدت لایسنس معتبر نیست؛ بین ۱ روز تا ۱۰ سال انتخاب کنید.")
        _period_code, days, _duration_label = period_info
        expires_at = utcnow() + timedelta(days=days) if days is not None else None

    period_code, days, duration_label = period_info
    mark_license_key_used(user, user.license_key)
    user.is_licensed = True
    user.license_type = tier
    user.licensed_at = utcnow()
    user.license_expires_at = expires_at
    user.license_key = generate_key(user.username, tier, period_code)
    mark_license_key_used(user, user.license_key)
    db.session.commit()

    validity = (
        "مادام‌العمر (دائمی)"
        if expires_at is None
        else f"تا {shamsi_label(expires_at.date())}"
    )
    return {
        "success": True,
        "message": f"لایسنس «{user.username}» به‌روزرسانی شد — {duration_label} ({validity}).",
        "license_key": user.license_key,
        "user": admin_user_payload(user),
    }


@app.route("/api/admin/users/<int:user_id>/delete", methods=["POST"])
def api_admin_delete_user(user_id):
    """حذف کامل یک کاربر همراه با تأمین‌کننده‌ها و محصولاتش."""
    user, error = admin_target_user(user_id)
    if error:
        return error

    if user.id == current_user.id:
        return json_error("حساب خودتان را نمی‌توانید حذف کنید.")
    if user.username.lower() == PRIMARY_ADMIN_USERNAME:
        return json_error("حساب مدیر اصلی سامانه قابل حذف نیست.")

    username = user.username
    Product.query.filter_by(owner_id=user.id).delete(synchronize_session=False)
    Supplier.query.filter_by(owner_id=user.id).delete(synchronize_session=False)
    LicenseActivation.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    db.session.delete(user)
    db.session.commit()

    return {"success": True, "message": f"کاربر «{username}» و همه داده‌هایش حذف شد.", "id": user_id}


@app.route("/api/admin/generate-license", methods=["POST"])
def api_admin_generate_license():
    if not is_admin_user(current_user):
        return json_error("دسترسی غیرمجاز.", 403)

    ident = request.form.get("identifier", "").strip()
    tier = request.form.get("tier", "PRO").strip().upper()
    duration = request.form.get("duration", "LIFE").strip()
    is_master = request.form.get("is_master") in ["true", "1", "on"]

    if tier not in ALLOWED_LICENSE_TIERS:
        return json_error("نوع لایسنس معتبر نیست.")
    if is_master and tier == "ENTERPRISE":
        return json_error("کلید سراسری فقط برای پلن PRO یا UNLIMITED صادر می‌شود.")
    period_info = validate_duration(duration)
    if not period_info:
        return json_error("مدت لایسنس معتبر نیست؛ بین ۱ روز تا ۱۰ سال انتخاب کنید.")
    period_code, days, duration_label = period_info
    validity_desc = "مادام‌العمر (دائمی)" if days is None else f"{days} روز پس از فعال‌سازی"

    if is_master:
        key = generate_master_key(tier, period_code)
        ident_display = "کلید سراسری (Universal Master Key)"
        code_display = "همه دستگاه‌ها و حساب‌ها"
    else:
        if not ident:
            return json_error("نام کاربری یا شناسه فعال‌سازی مشتری را وارد کنید.")
        if len(ident) > 100:
            return json_error("شناسه مشتری حداکثر ۱۰۰ کاراکتر باشد.")
        key = generate_key(ident, tier, period_code)
        ident_display = ident
        code_display = get_user_code(ident)

    customer_msg = (
        f"با سلام، لایسنس نسخه نامحدود «لیستیا» ({duration_label}) برای شما صادر شد:\n\n"
        f"🔑 کلید لایسنس شما:\n{key}\n\n"
        f"⏳ مدت اعتبار: {validity_desc}\n"
        f"روش فعال‌سازی: وارد نرم‌افزار شوید، روی «نسخه آزمایشی» در منو کلیک کنید و کلید بالا را وارد نمایید.\n"
        f"با آرزوی موفقیت · طراحی و توسعه: س.م.قتالی"
    )

    return {
        "success": True,
        "license_key": key,
        "identifier": ident_display,
        "user_code": code_display,
        "tier": tier,
        "duration": period_code,
        "duration_label": duration_label,
        "validity_desc": validity_desc,
        "customer_message": customer_msg,
    }


@app.route("/account/token", methods=["POST"])
def regenerate_token():
    user = db.session.get(User, current_user.id)
    token = issue_api_token(user)
    return {"success": True, "token": token}


@app.route("/account")
def account():
    """Personal account page — only ever shows the logged-in user's own data."""
    active_count = archived_count = 0
    for ordered, total in (
        db.session.query(Product.ordered, db.func.count(Product.id))
        .filter(Product.owner_id == current_user.id)
        .group_by(Product.ordered)
        .all()
    ):
        if ordered:
            archived_count += total
        else:
            active_count += total
    supplier_count = Supplier.query.filter_by(owner_id=current_user.id).count()

    user = db.session.get(User, current_user.id)
    limits = get_user_limits(
        user,
        supplier_count=supplier_count,
        product_count=active_count + archived_count,
    )
    g.limits = limits

    return render_template(
        "account.html",
        supplier_count=supplier_count,
        active_count=active_count,
        archived_count=archived_count,
        shortcut_key_value="",
        shortcut_key_configured=bool(user.api_token_hash or user.api_token),
        api_base=request.url_root.rstrip("/"),
        limits=limits,
    )


@app.route("/account/profile", methods=["POST"])
def update_profile():
    first_name_raw = request.form.get("first_name", "")
    last_name_raw = request.form.get("last_name", "")
    if len(first_name_raw.strip()) > 100 or len(last_name_raw.strip()) > 100:
        return json_error("نام و نام خانوادگی هرکدام حداکثر ۱۰۰ کاراکتر باشند.")

    first_name = clean_person_name(first_name_raw)
    last_name = clean_person_name(last_name_raw)
    mobile = normalize_mobile(request.form.get("mobile", ""))
    if not first_name or not last_name:
        return json_error("نام و نام خانوادگی را کامل وارد کنید.")
    if not mobile:
        return json_error(
            "شماره موبایل معتبر نیست؛ شماره را مانند ۰۹۱۲۱۲۳۴۵۶۷ وارد کنید."
        )

    user = db.session.get(User, current_user.id)
    user.first_name = first_name
    user.last_name = last_name
    user.mobile = mobile
    db.session.commit()
    return {
        "success": True,
        "message": "اطلاعات حساب ذخیره شد.",
        "profile": {
            "first_name": first_name,
            "last_name": last_name,
            "mobile": mobile,
        },
    }


@app.route("/account/username", methods=["POST"])
def change_username():
    """تغییر نام کاربری فقط از پنل مدیر انجام می‌شود."""
    return json_error(
        "تغییر نام کاربری فقط توسط مدیر سامانه انجام می‌شود. لطفاً با پشتیبانی تماس بگیرید.",
        403,
    )


@app.route("/account/password", methods=["POST"])
def change_password():
    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")

    user = db.session.get(User, current_user.id)

    if not check_password_hash(user.password_hash, current_password):
        return json_error("رمز عبور فعلی درست نیست.")
    if not MIN_PASSWORD_LENGTH <= len(new_password) <= 128:
        return json_error(
            f"رمز عبور جدید باید بین {MIN_PASSWORD_LENGTH} و ۱۲۸ کاراکتر باشد."
        )
    if new_password != confirm_password:
        return json_error("تکرار رمز عبور جدید یکسان نیست.")
    if check_password_hash(user.password_hash, new_password):
        return json_error("رمز جدید با رمز فعلی فرقی ندارد.")

    user.password_hash = generate_password_hash(new_password)
    db.session.commit()
    login_user(user, remember=True)
    response = make_response({
        "success": True,
        "message": "رمز عبور عوض شد و نشست‌های قبلی بسته شدند.",
    })
    return attach_auth_cookie(response, user)


@app.route("/users")
def users_redirect():
    return redirect(url_for("account"))


@app.errorhandler(413)
def request_too_large(_error):
    return json_error("حجم درخواست یا فایل بیش از سقف ۸ مگابایت است.", 413)


@app.errorhandler(SQLAlchemyError)
def database_error(error):
    db.session.rollback()
    logger.error(
        "Unhandled database error",
        exc_info=(type(error), error, error.__traceback__),
    )
    return json_error("عملیات پایگاه داده انجام نشد؛ دوباره تلاش کنید.", 500)


if __name__ == "__main__":
    with app.app_context():
        ensure_schema()
    debug = os.environ.get("FLASK_DEBUG", "0") in ("1", "true", "True")
    # Containers need the wildcard interface so Chabokan can reach the process.
    host = os.environ.get("HOST") or "0.0.0.0"  # nosec
    app.run(host=host, port=int(os.environ.get("PORT", "5000")), debug=debug)
