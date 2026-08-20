import os
import secrets
from datetime import date, datetime, timedelta

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, g, render_template, request, redirect, url_for, send_file, make_response
from urllib.parse import urlparse
import time
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager,
    UserMixin,
    login_user,
    logout_user,
    current_user,
)
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import inspect, or_, text
from sqlalchemy.orm import joinedload
from sqlalchemy.pool import NullPool
import jdatetime

from licensing import (
    FREE_MAX_SUPPLIERS,
    FREE_MAX_PRODUCTS,
    get_user_code,
    generate_key,
    generate_master_key,
    verify_key,
    normalize_duration,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
os.makedirs(INSTANCE_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config["SESSION_COOKIE_SAMESITE"] = "None"
app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["REMEMBER_COOKIE_SAMESITE"] = "None"
app.config["REMEMBER_COOKIE_SECURE"] = True
app.config["REMEMBER_COOKIE_HTTPONLY"] = True

db_url = os.environ.get("DATABASE_URL")
if db_url:
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = db_url
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.join(INSTANCE_DIR, "purchase.db")

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
# Static files are versioned with ?v=<hash>, so they can be cached hard.
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 60 * 60 * 24 * 365

if os.environ.get("VERCEL"):
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"poolclass": NullPool}


AUTH_COOKIE = "listia_auth"
AUTH_MAX_AGE = 60 * 60 * 24 * 30


def _auth_serializer():
    return URLSafeTimedSerializer(app.secret_key, salt="listia-auth")


def issue_auth_token(user_id):
    return _auth_serializer().dumps({"uid": int(user_id)})


def user_from_auth_token(token):
    if not token:
        return None
    try:
        data = _auth_serializer().loads(token, max_age=AUTH_MAX_AGE)
        return db.session.get(User, int(data["uid"]))
    except (BadSignature, SignatureExpired, KeyError, TypeError, ValueError):
        return None


def attach_auth_cookie(response, user_id):
    token = issue_auth_token(user_id)
    kwargs = dict(
        key=AUTH_COOKIE,
        value=token,
        max_age=AUTH_MAX_AGE,
        httponly=False,
        secure=True,
        samesite="None",
        path="/",
    )
    try:
        response.set_cookie(partitioned=True, **kwargs)
    except TypeError:
        response.set_cookie(**kwargs)
        cookies = response.headers.getlist("Set-Cookie")
        response.headers.remove("Set-Cookie")
        for cookie in cookies:
            if AUTH_COOKIE in cookie and "Partitioned" not in cookie:
                cookie += "; Partitioned"
            response.headers.add("Set-Cookie", cookie)
    return token


def login_handoff(user):
    login_user(user, remember=True)
    token = issue_auth_token(user.id)
    resp = make_response(render_template("login_handoff.html", token=token))
    attach_auth_cookie(resp, user.id)
    return resp


@app.after_request
def _allow_iframe_preview(response):
    # Arena shows the app inside an iframe; don't block embedding.
    response.headers.pop("X-Frame-Options", None)
    response.headers["Content-Security-Policy"] = "frame-ancestors *"
    cookies = response.headers.getlist("Set-Cookie")
    if cookies:
        response.headers.remove("Set-Cookie")
        for cookie in cookies:
            patched = (
                cookie.replace("SameSite=Lax", "SameSite=None")
                .replace("SameSite=Strict", "SameSite=None")
                .replace("samesite=lax", "SameSite=None")
            )
            if "SameSite=" not in patched and "samesite=" not in patched.lower():
                patched += "; SameSite=None"
            if "Secure" not in patched:
                patched += "; Secure"
            if "Partitioned" not in patched:
                patched += "; Partitioned"
            response.headers.add("Set-Cookie", patched)
    return response

db = SQLAlchemy(app)

UNIT_TYPES = ["عدد", "کارتن", "بسته", "گونی", "کیلو"]
ADMIN_USERNAMES = {"smq2458", "admin"}

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"


def normalize_name(name):
    name = " ".join((name or "").split())
    name = name.replace("ي", "ی").replace("ك", "ک")
    return name.strip().lower()


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
        g.limits = get_user_limits(current_user) if current_user.is_authenticated else None
    return g.limits


_login_attempts = {}


def login_is_throttled(ip):
    now = time.time()
    stamps = [t for t in _login_attempts.get(ip, []) if now - t < 300]
    _login_attempts[ip] = stamps
    return len(stamps) >= 12


def record_login_fail(ip):
    _login_attempts.setdefault(ip, []).append(time.time())


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
    # کلید شخصی برای ثبت از بیرون اپ (مثلاً Shortcuts آیفون)
    api_token = db.Column(db.String(64), unique=True, nullable=True)
    # لایسنس نرم‌افزار لیستیا
    is_licensed = db.Column(db.Boolean, default=False)
    license_key = db.Column(db.String(100), nullable=True)
    licensed_at = db.Column(db.DateTime, nullable=True)
    license_expires_at = db.Column(db.DateTime, nullable=True)  # تاریخ انقضای لایسنس (None = مادام‌العمر)
    license_type = db.Column(db.String(50), default="free")
    # دسترسی مدیریت و صدور لایسنس
    is_admin = db.Column(db.Boolean, default=False)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def is_admin_user(user=None):
    """بررسی اینکه آیا کاربر مدیر سامانه (س.م.قتالی) است یا خیر"""
    user = user or current_user
    if not user or not user.is_authenticated:
        return False
    return bool(getattr(user, "is_admin", False) or (user.username and user.username.lower() in ADMIN_USERNAMES))


def get_user_limits(user=None):
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
            now = datetime.now()
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

    s_count = Supplier.query.filter_by(owner_id=user.id).count()
    p_count = Product.query.filter_by(owner_id=user.id).count()

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


def issue_api_token(user):
    """یک کلید تازه می‌سازد و ذخیره می‌کند."""
    user.api_token = secrets.token_urlsafe(24)
    db.session.commit()
    return user.api_token


def ensure_api_token(user):
    return user.api_token or issue_api_token(user)


def user_from_api_token():
    """کاربر را از روی کلید در هدر، Bearer یا پارامتر URL پیدا می‌کند."""
    token = (
        request.headers.get("X-API-Key")
        or request.values.get("key")
        or ""
    ).strip()

    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()

    if not token:
        return None
    return User.query.filter_by(api_token=token).first()


def json_error(message, status=400, extra=None):
    payload = {"success": False, "message": message}
    if extra:
        payload.update(extra)
    return payload, status


def user_suppliers():
    """All suppliers of the logged-in user, alphabetically."""
    if hasattr(g, "suppliers"):
        return g.suppliers
    rows = Supplier.query.filter_by(owner_id=current_user.id).order_by(Supplier.name).all()
    g.suppliers = sorted(rows, key=lambda s: supplier_sort_key(s.name))
    return g.suppliers


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

    return {
        "active_count": active_count,
        "archived_count": archived_count,
        "supplier_count": len(user_suppliers()),
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


def read_product_form():
    """Validate the shared product form. Returns (fields, error)."""
    fields = {
        "supplier_id": request.form.get("supplier", "") or request.form.get("supplier_id", ""),
        "product_name": request.form.get("product", "").strip()[:300],
        "quantity": request.form.get("quantity", "").strip()[:50],
        "unit": request.form.get("unit", ""),
        "description": request.form.get("description", "").strip()[:500],
    }

    if not fields["product_name"]:
        return fields, "نام محصول را وارد کنید."
    if not fields["quantity"]:
        return fields, "تعداد را وارد کنید."
    if fields["unit"] not in UNIT_TYPES:
        return fields, "نوع تعداد را انتخاب کنید."
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


def ensure_schema():
    db.create_all()
    missing_owner_cols = []
    for table in ("supplier", "product"):
        cols = _table_columns(table)
        if cols and "owner_id" not in cols:
            missing_owner_cols.append(table)

    user_cols = _table_columns("user")

    with db.engine.begin() as conn:
        for table in missing_owner_cols:
            try:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN owner_id INTEGER"))
            except Exception:
                pass

        if user_cols:
            if "api_token" not in user_cols:
                try:
                    conn.execute(text('ALTER TABLE "user" ADD COLUMN api_token VARCHAR(64)'))
                except Exception:
                    pass
            if "is_licensed" not in user_cols:
                try:
                    conn.execute(text('ALTER TABLE "user" ADD COLUMN is_licensed BOOLEAN DEFAULT 0'))
                except Exception:
                    pass
            if "license_key" not in user_cols:
                try:
                    conn.execute(text('ALTER TABLE "user" ADD COLUMN license_key VARCHAR(100)'))
                except Exception:
                    pass
            if "licensed_at" not in user_cols:
                try:
                    conn.execute(text('ALTER TABLE "user" ADD COLUMN licensed_at TIMESTAMP'))
                except Exception:
                    pass
            if "license_expires_at" not in user_cols:
                try:
                    conn.execute(text('ALTER TABLE "user" ADD COLUMN license_expires_at TIMESTAMP'))
                except Exception:
                    pass
            if "license_type" not in user_cols:
                try:
                    conn.execute(text('ALTER TABLE "user" ADD COLUMN license_type VARCHAR(50) DEFAULT \'free\''))
                except Exception:
                    pass
            if "is_admin" not in user_cols:
                try:
                    conn.execute(text('ALTER TABLE "user" ADD COLUMN is_admin BOOLEAN DEFAULT 0'))
                except Exception:
                    pass

        try:
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_supplier_owner_name "
                    "ON supplier (owner_id, name)"
                )
            )
        except Exception:
            pass
        for statement in (
            "CREATE INDEX IF NOT EXISTS ix_supplier_owner ON supplier (owner_id)",
            "CREATE INDEX IF NOT EXISTS ix_product_owner ON product (owner_id)",
            "CREATE INDEX IF NOT EXISTS ix_product_owner_ordered ON product (owner_id, ordered)",
            "CREATE INDEX IF NOT EXISTS ix_product_supplier_ordered "
            "ON product (supplier_id, ordered, ordered_date)",
        ):
            try:
                conn.execute(text(statement))
            except Exception:
                pass

        # Legacy rows can hold NULL instead of FALSE, which silently dropped them
        # out of every «active order» count. Normalise them once.
        for statement in (
            "UPDATE product SET ordered = FALSE WHERE ordered IS NULL",
            "UPDATE product SET ordered = 0 WHERE ordered IS NULL",
        ):
            try:
                with conn.begin_nested():
                    conn.execute(text(statement))
                break
            except Exception:
                pass

        try:
            conn.execute(
                text(
                    "UPDATE \"user\" SET is_licensed = 1, is_admin = 1, license_type = 'UNLIMITED' "
                    "WHERE LOWER(username) IN ('smq2458', 'admin')"
                )
            )
        except Exception:
            pass

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


def _asset_version():
    """Cache-busting stamp so browsers can cache CSS/JS for a year."""
    stamp = 0
    for name in ("style.css", "app.js"):
        try:
            stamp = max(stamp, int(os.path.getmtime(os.path.join(BASE_DIR, "static", name))))
        except OSError:
            pass
    return str(stamp)


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
    }


_schema_ready = False


@app.before_request
def _prepare_request():
    global _schema_ready
    if not _schema_ready:
        try:
            ensure_schema()
            _schema_ready = True
        except Exception:
            pass

    if not current_user.is_authenticated:
        token = request.cookies.get(AUTH_COOKIE) or request.args.get("auth") or request.form.get("auth")
        user = user_from_auth_token(token)
        if user:
            login_user(user, remember=True)

    allowed = {"login", "signup", "static", "api_quick_add", "api_suppliers"}
    if request.endpoint in allowed or request.endpoint is None:
        return
    if not current_user.is_authenticated:
        return redirect(url_for("login"))


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
        return redirect("/")

    if request.method == "POST":
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip()
        if login_is_throttled(ip):
            return render_template("login.html", mode="login", error="تلاش‌های ورود زیاد است. چند دقیقه بعد دوباره امتحان کنید.")
        username = request.form.get("username", "").strip()[:100]
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password_hash, password):
            _login_attempts.pop(ip, None)
            if user.username.lower() in ADMIN_USERNAMES:
                user.is_admin = True
                user.is_licensed = True
                user.license_type = "UNLIMITED"
                db.session.commit()
            return login_handoff(user)
        record_login_fail(ip)
        return render_template("login.html", mode="login", error="نام کاربری یا رمز عبور اشتباه است.")

    return render_template("login.html", mode="login", error=None)


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if current_user.is_authenticated:
        return redirect("/")

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if not username or not password:
            return render_template(
                "login.html", mode="signup", error="نام کاربری و رمز عبور الزامی است."
            )
        if len(username) < 2:
            return render_template(
                "login.html", mode="signup", error="نام کاربری خیلی کوتاه است."
            )
        if len(password) < 4:
            return render_template(
                "login.html", mode="signup", error="رمز عبور حداقل ۴ کاراکتر باشد."
            )
        if User.query.filter_by(username=username).first():
            return render_template(
                "login.html", mode="signup", error="این نام کاربری قبلاً وجود دارد."
            )

        is_admin_account = username.lower() in ADMIN_USERNAMES
        user = User(
            username=username,
            password_hash=generate_password_hash(password),
            is_licensed=is_admin_account,
            license_type="UNLIMITED" if is_admin_account else "free",
            is_admin=is_admin_account,
        )
        db.session.add(user)
        db.session.commit()
        claim_orphaned_data(user.id)
        return login_handoff(user)

    return render_template("login.html", mode="signup", error=None)


@app.route("/logout")
def logout():
    logout_user()
    resp = make_response(render_template("logout_handoff.html"))
    resp.delete_cookie(AUTH_COOKIE, path="/")
    return resp


@app.route("/search")
def search():
    return render_template("search.html")


@app.route("/new-purchase", methods=["GET", "POST"])
def new_purchase():
    if request.method == "GET":
        return redirect("/")

    limits = cached_limits()
    if not limits["can_add_product"]:
        msg = "سقف نسخه آزمایشی (۵ محصول) تکمیل شده است. برای ثبت محصول جدید باید لایسنس لیستیا را تهیه کنید."
        if limits.get("is_expired"):
            msg = "مدت زمان لایسنس شما به پایان رسیده است. جهت ثبت محصولات بیشتر، لایسنس خود را تمدید فرمایید."
        return json_error(msg, 403, {"license_locked": True})

    fields, error = read_product_form()
    supplier = None
    if not fields["supplier_id"]:
        error = "تأمین‌کننده را انتخاب کنید."
    else:
        supplier = Supplier.query.filter_by(
            id=fields["supplier_id"], owner_id=current_user.id
        ).first()
        if not supplier:
            error = "تأمین‌کننده معتبر نیست."

    if error:
        return json_error(error)

    new_product = Product(
        owner_id=current_user.id,
        supplier_id=supplier.id,
        product_name=fields["product_name"],
        quantity=fields["quantity"],
        unit=fields["unit"],
        description=fields["description"],
    )
    db.session.add(new_product)
    db.session.commit()
    return {
        "success": True,
        "message": "خرید ثبت شد",
        "product": product_payload(new_product, supplier.name),
    }


@app.route("/check-duplicate")
def check_duplicate():
    product_name = request.args.get("product", "").strip()
    current_supplier_id = request.args.get("supplier")

    if not product_name:
        return {"matches": []}

    matches = (
        user_products(ordered=False)
        .filter(db.func.lower(Product.product_name) == product_name.lower())
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
    product.ordered_date = date.today()
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
    suppliers = user_suppliers()

    if request.method == "POST":
        name = request.form.get("product", "").strip()
        quantity = request.form.get("quantity", "").strip()
        unit = request.form.get("unit")
        description = request.form.get("description", "").strip()
        supplier_id = request.form.get("supplier_id") or request.form.get("supplier")

        if not name or not quantity or unit not in UNIT_TYPES:
            if wants_json():
                return json_error("اطلاعات محصول کامل نیست.")
            return json_error("اطلاعات محصول کامل نیست.")

        if supplier_id:
            try:
                target_supplier = Supplier.query.filter_by(id=int(supplier_id), owner_id=current_user.id).first()
                if target_supplier:
                    product.supplier_id = target_supplier.id
            except (ValueError, TypeError):
                pass

        product.product_name = name
        product.quantity = quantity
        product.unit = unit
        product.description = description
        db.session.commit()

        if wants_json():
            return {"success": True, "product": product_payload(product)}
        return redirect(f"/supplier/{product.supplier_id}")

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

    if date_str == "unknown":
        products_to_delete = Product.query.filter_by(
            owner_id=current_user.id,
            supplier_id=supplier_id,
            ordered=True,
            ordered_date=None,
        ).all()
    else:
        target_date = date.fromisoformat(date_str)
        products_to_delete = Product.query.filter_by(
            owner_id=current_user.id,
            supplier_id=supplier_id,
            ordered=True,
            ordered_date=target_date,
        ).all()

    for item in products_to_delete:
        db.session.delete(item)
    db.session.commit()

    if wants_json():
        return {"success": True}
    return redirect(f"/supplier/{supplier_id}")


@app.route("/suppliers", methods=["GET", "POST"])
def suppliers():
    limits = get_user_limits(current_user)

    if request.method == "POST":
        if not limits["can_add_supplier"]:
            msg = "سقف نسخه آزمایشی (۱ تأمین‌کننده) تکمیل شده است. برای ثبت تأمین‌کنندگان بیشتر باید لایسنس لیستیا را تهیه کنید."
            if limits.get("is_expired"):
                msg = "مدت زمان لایسنس شما به پایان رسیده است. جهت ثبت تأمین‌کنندگان بیشتر، لایسنس خود را تمدید فرمایید."
            if wants_json():
                return json_error(msg, 403, {"license_locked": True})
            return render_template("suppliers.html", suppliers=user_suppliers(), error=msg, limits=limits)

        name = request.form.get("name", "").strip()

        if not name:
            if wants_json():
                return json_error("نام تأمین‌کننده را وارد کنید.")
            return redirect("/suppliers")

        existing = Supplier.query.filter_by(owner_id=current_user.id, name=name).first()
        if existing:
            if wants_json():
                return json_error("این تأمین‌کننده قبلاً ثبت شده است.")
            return redirect("/suppliers")

        new_supplier = Supplier(name=name, owner_id=current_user.id)
        db.session.add(new_supplier)
        db.session.commit()

        if wants_json():
            return {"success": True, "id": new_supplier.id, "name": new_supplier.name}
        return redirect("/suppliers")

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

    today = date.today()
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
        return redirect("/suppliers")
    new_name = request.form.get("name", "").strip()[:200]
    if not new_name:
        return json_error("نام تأمین‌کننده را وارد کنید.")
    clash = (
        Supplier.query.filter_by(owner_id=current_user.id, name=new_name)
        .filter(Supplier.id != supplier.id)
        .first()
    )
    if clash:
        return json_error("این نام قبلاً ثبت شده است.")
    supplier.name = new_name
    db.session.commit()
    return {"success": True, "id": supplier.id, "name": supplier.name}


@app.route("/supplier/<int:supplier_id>/delete", methods=["POST"])
def delete_supplier(supplier_id):
    supplier = owned_supplier_or_404(supplier_id)
    db.session.delete(supplier)
    db.session.commit()

    if wants_json():
        return {"success": True}
    return redirect("/suppliers")


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

    pattern = f"%{query}%"
    matches = (
        user_products()
        .join(Supplier, Product.supplier_id == Supplier.id)
        .filter(
            or_(
                Product.product_name.ilike(pattern),
                Supplier.name.ilike(pattern),
            )
        )
        .order_by(Product.ordered.asc(), Product.id.desc())
        .limit(50)
        .all()
    )

    matching_suppliers = (
        Supplier.query.filter(
            Supplier.owner_id == current_user.id,
            Supplier.name.ilike(pattern),
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

    if request.method == "GET":
        return render_template("import_excel.html", message=None, errors=None, limits=limits)

    uploaded_file = request.files.get("file")

    if not uploaded_file or uploaded_file.filename == "":
        return render_template(
            "import_excel.html",
            message="فایلی انتخاب نشده است.",
            success=False,
            errors=None,
            limits=limits,
        )

    import difflib
    import pandas as pd

    filename = (uploaded_file.filename or "").lower()
    if not (filename.endswith(".csv") or filename.endswith(".xlsx") or filename.endswith(".xls")):
        return render_template(
            "import_excel.html",
            message="فقط فایل CSV یا Excel مجاز است.",
            success=False,
            errors=None,
            limits=limits,
        )

    try:
        if filename.endswith(".csv"):
            df = pd.read_csv(uploaded_file, encoding="utf-8-sig")
        else:
            df = pd.read_excel(uploaded_file)
    except Exception:
        return render_template(
            "import_excel.html",
            message="خطا در خواندن فایل. قالب را بررسی کنید.",
            success=False,
            errors=None,
            limits=limits,
        )

    df = df.dropna(how="all")

    existing_suppliers = user_suppliers()
    normalized_map = {normalize_name(s.name): s for s in existing_suppliers}
    normalized_names = list(normalized_map.keys())

    added_count = 0
    errors = []

    current_product_count = Product.query.filter_by(owner_id=current_user.id).count()
    current_supplier_count = len(existing_suppliers)

    for index, row in df.iterrows():
        excel_row_number = index + 2

        try:
            supplier_name = str(row.iloc[0]).strip()
            product_name = str(row.iloc[1]).strip()
            quantity = row.iloc[2]
            unit = str(row.iloc[3]).strip()
            description = (
                str(row.iloc[4]).strip()
                if len(row) > 4 and pd.notna(row.iloc[4])
                else ""
            )
        except IndexError:
            errors.append(f"ردیف {excel_row_number}: تعداد ستون‌ها کافی نیست")
            continue

        if not supplier_name or supplier_name == "nan":
            errors.append(f"ردیف {excel_row_number}: نام تأمین‌کننده خالی است")
            continue

        if not product_name or product_name == "nan":
            errors.append(f"ردیف {excel_row_number}: نام محصول خالی است")
            continue

        if unit not in UNIT_TYPES:
            errors.append(f"ردیف {excel_row_number}: نوع تعداد «{unit}» معتبر نیست")
            continue

        if pd.isna(quantity) or str(quantity).strip() == "":
            errors.append(f"ردیف {excel_row_number}: تعداد خالی است")
            continue

        norm_name = normalize_name(supplier_name)

        if norm_name in normalized_map:
            supplier_id = normalized_map[norm_name].id
        else:
            close = difflib.get_close_matches(norm_name, normalized_names, n=1, cutoff=0.82)
            if close:
                similar_name = normalized_map[close[0]].name
                errors.append(
                    f"ردیف {excel_row_number}: نام «{supplier_name}» شبیه تأمین‌کننده‌ی موجود «{similar_name}» است. "
                    f"برای جلوگیری از ساخت تأمین‌کننده‌ی تکراری، این ردیف وارد نشد — نام را در فایل اصلاح کن یا اگر واقعاً جدید است، دوباره امتحان کن."
                )
                continue

            if not limits["is_licensed"] and current_supplier_count >= FREE_MAX_SUPPLIERS:
                errors.append(
                    f"ردیف {excel_row_number}: ثبت تأمین‌کننده جدید «{supplier_name}» ناموفق بود. در نسخه آزمایشی فقط مجاز به داشتن ۱ تأمین‌کننده هستید. (نیاز به لایسنس)"
                )
                continue

            new_supplier = Supplier(name=supplier_name, owner_id=current_user.id)
            db.session.add(new_supplier)
            db.session.flush()

            normalized_map[norm_name] = new_supplier
            normalized_names.append(norm_name)
            supplier_id = new_supplier.id
            current_supplier_count += 1

        if not limits["is_licensed"] and (current_product_count + added_count) >= FREE_MAX_PRODUCTS:
            errors.append(
                f"ردیف {excel_row_number}: سقف ۵ محصول در نسخه آزمایشی پر شد. محصول «{product_name}» ثبت نشد. (برای افزودن محصولات بیشتر لایسنس تهیه کنید)"
            )
            continue

        new_product = Product(
            owner_id=current_user.id,
            supplier_id=supplier_id,
            product_name=product_name,
            quantity=str(quantity),
            unit=unit,
            description=description,
        )
        db.session.add(new_product)
        added_count += 1

    db.session.commit()

    message = f"✓ {added_count} ردیف با موفقیت ثبت شد."
    if errors:
        message += f" ({len(errors)} ردیف رد شد)"

    return render_template(
        "import_excel.html",
        message=message,
        success=added_count > 0,
        errors=errors if errors else None,
        limits=get_user_limits(current_user),
    )

# ---------------------------------------------------------------------------
#  API کلیددار — برای ثبت از بیرون اپ (Shortcuts آیفون، ویجت، هر چیز دیگر)
# ---------------------------------------------------------------------------


def resolve_supplier(user, raw):
    """تأمین‌کننده را از روی شناسه یا نام پیدا می‌کند؛ نبود، با بررسی لایسنس می‌سازدش."""
    raw = (raw or "").strip()

    if raw.isdigit():
        found = Supplier.query.filter_by(id=int(raw), owner_id=user.id).first()
        if found:
            return found, False, None

    if raw:
        target = normalize_name(raw)
        for supplier in Supplier.query.filter_by(owner_id=user.id).all():
            if normalize_name(supplier.name) == target:
                return supplier, False, None

        s_count = Supplier.query.filter_by(owner_id=user.id).count()
        user_licensed = bool(user.is_licensed or is_admin_user(user))
        if not user_licensed and s_count >= FREE_MAX_SUPPLIERS:
            return None, False, "سقف ۱ تأمین‌کننده نسخه آزمایشی پر شده است. نیاز به لایسنس."

        created = Supplier(name=raw, owner_id=user.id)
        db.session.add(created)
        db.session.flush()
        return created, True, None

    last = (
        Product.query.filter_by(owner_id=user.id)
        .order_by(Product.id.desc())
        .first()
    )
    if last:
        return db.session.get(Supplier, last.supplier_id), False, None

    first = Supplier.query.filter_by(owner_id=user.id).order_by(Supplier.id).first()
    if first:
        return first, False, None

    s_count = Supplier.query.filter_by(owner_id=user.id).count()
    user_licensed = bool(user.is_licensed or is_admin_user(user))
    if not user_licensed and s_count >= FREE_MAX_SUPPLIERS:
        return None, False, "سقف ۱ تأمین‌کننده نسخه آزمایشی پر شده است. نیاز به لایسنس."

    created = Supplier(name="نامشخص", owner_id=user.id)
    db.session.add(created)
    db.session.flush()
    return created, True, None


@app.route("/api/quick-add", methods=["GET", "POST"])
def api_quick_add():
    """ثبت یک کالا فقط با کلید شخصی — بدون لاگین."""
    user = user_from_api_token()
    if not user:
        return json_error("کلید معتبر نیست.", 401)

    user_licensed = bool(user.is_licensed or is_admin_user(user))
    p_count = Product.query.filter_by(owner_id=user.id).count()
    if not user_licensed and p_count >= FREE_MAX_PRODUCTS:
        return json_error(
            "سقف ۵ محصول در نسخه آزمایشی تکمیل شده است. برای ثبت محصولات بیشتر باید لایسنس تهیه کنید.",
            403,
            {"license_locked": True},
        )

    body = request.get_json(silent=True) or {}

    def field(name, default=""):
        value = request.values.get(name)
        if value is None:
            value = body.get(name, default)
        return str(value if value is not None else default).strip()

    product_name = field("product") or field("name") or field("text")
    if not product_name:
        return json_error("نام محصول را بفرست.")

    quantity = field("quantity") or field("qty") or "1"
    unit = field("unit") or UNIT_TYPES[0]
    if unit not in UNIT_TYPES:
        return json_error("واحد باید یکی از این‌ها باشد: " + "، ".join(UNIT_TYPES))

    supplier, supplier_created, err = resolve_supplier(user, field("supplier"))
    if err:
        return json_error(err, 403, {"license_locked": True})

    product = Product(
        owner_id=user.id,
        supplier_id=supplier.id,
        product_name=product_name,
        quantity=quantity,
        unit=unit,
        description=field("description"),
    )
    db.session.add(product)
    db.session.commit()

    message = f"«{product_name}» {quantity} {unit} برای {supplier.name} ثبت شد"
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
    """شمارنده‌های زنده داشبورد (سفارش فعال، آرشیو و تعداد هر تأمین‌کننده).

    داشبورد بعد از باز شدن صفحه و بعد از هر تغییر این را می‌خواند تا عددهای
    کارت تأمین‌کننده‌ها دقیقاً با دیتابیس یکی باشد؛ حتی برای محصولاتی که
    قبلاً ثبت شده‌اند.
    """
    if not current_user.is_authenticated:
        return json_error("ابتدا وارد شوید.", 401)

    counters = dashboard_counters()
    return {
        "success": True,
        "active_count": counters["active_count"],
        "archived_count": counters["archived_count"],
        "supplier_count": counters["supplier_count"],
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
    """فعال‌سازی کلید لایسنس (مدت‌دار یا مادام‌العمر) برای حساب کاربر جاری"""
    key = request.form.get("license_key", "").strip()
    if not key:
        return json_error("لطفاً کلید لایسنس را وارد کنید.")

    is_valid, tier, days, msg = verify_key(current_user, key)
    if not is_valid:
        return json_error(msg, 400)

    user = db.session.get(User, current_user.id)
    user.is_licensed = True
    user.license_key = key.strip().upper()
    user.licensed_at = datetime.now()
    user.license_type = tier or "pro"

    if days:
        user.license_expires_at = datetime.now() + timedelta(days=days)
        validity_text = f"اعتبار به مدت {days} روز (تا {shamsi_label(user.license_expires_at.date())})"
    else:
        user.license_expires_at = None
        validity_text = "اعتبار مادام‌العمر (دائمی)"

    db.session.commit()

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

def admin_user_payload(user):
    """اطلاعات یک کاربر برای جدول مدیریت (پنل مدیر)."""
    user_limits = get_user_limits(user)
    return {
        "id": user.id,
        "username": user.username,
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
        "is_protected": bool(user.username and user.username.lower() in ADMIN_USERNAMES),
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
    """کلید ذخیره‌شده را با نام کاربری فعلی هم‌خوان می‌کند (کلیدها به نام کاربر گره خورده‌اند)."""
    if not user.is_licensed:
        return None
    if user.license_expires_at:
        days = max(1, (user.license_expires_at - datetime.now()).days + 1)
        period_code, _, _ = normalize_duration(str(days))
    else:
        period_code = "LIFE"
    user.license_key = generate_key(user.username, user.license_type or "PRO", period_code)
    return user.license_key


@app.route("/admin/license-generator")
def admin_license_generator():
    if not is_admin_user(current_user):
        return json_error("دسترسی به این بخش فقط برای مدیر نرم‌افزار مجاز است.", 403)

    all_users = User.query.order_by(User.id.desc()).all()
    return render_template("admin_license.html", users=[admin_user_payload(u) for u in all_users])


@app.route("/api/admin/users/<int:user_id>/update", methods=["POST"])
def api_admin_update_user(user_id):
    """ویرایش کاربر توسط مدیر: نام کاربری، رمز عبور و دسترسی مدیریت."""
    user, error = admin_target_user(user_id)
    if error:
        return error

    username = request.form.get("username", "").strip()[:100]
    new_password = request.form.get("new_password", "")
    admin_flag = request.form.get("is_admin")

    changes = []

    if username and username != user.username:
        if len(username) < 2:
            return json_error("نام کاربری خیلی کوتاه است.")
        exists = User.query.filter(
            db.func.lower(User.username) == username.lower(), User.id != user.id
        ).first()
        if exists:
            return json_error("این نام کاربری قبلاً وجود دارد.")
        old_username = user.username
        user.username = username
        # کلید لایسنس به نام کاربری گره خورده؛ با تغییر نام، کلید تازه صادر می‌شود.
        refresh_license_key(user)
        changes.append(f"نام کاربری از «{old_username}» به «{username}» تغییر کرد")

    if new_password:
        if len(new_password) < 4:
            return json_error("رمز عبور جدید حداقل ۴ کاراکتر باشد.")
        user.password_hash = generate_password_hash(new_password)
        changes.append("رمز عبور بازنشانی شد")

    if admin_flag is not None:
        wants_admin = admin_flag in ("1", "true", "on")
        if not wants_admin and user.id == current_user.id:
            return json_error("دسترسی مدیریت حساب خودتان را نمی‌توانید بردارید.")
        if not wants_admin and user.username.lower() in ADMIN_USERNAMES:
            return json_error("این حساب مدیر اصلی سامانه است و قابل تغییر نیست.")
        if bool(user.is_admin) != wants_admin:
            user.is_admin = wants_admin
            if wants_admin:
                user.is_licensed = True
            changes.append("دسترسی مدیریت " + ("داده شد" if wants_admin else "برداشته شد"))

    if not changes:
        return json_error("تغییری برای ذخیره وجود ندارد.")

    db.session.commit()
    return {
        "success": True,
        "message": "✓ " + "، ".join(changes),
        "user": admin_user_payload(user),
    }


@app.route("/api/admin/users/<int:user_id>/license", methods=["POST"])
def api_admin_update_license(user_id):
    """ویرایش یا حذف لایسنس یک کاربر توسط مدیر."""
    user, error = admin_target_user(user_id)
    if error:
        return error

    action = request.form.get("action", "grant").strip().lower()

    if action in ("revoke", "delete", "remove"):
        if user.username.lower() in ADMIN_USERNAMES:
            return json_error("لایسنس حساب مدیر اصلی قابل حذف نیست.")
        user.is_licensed = False
        user.license_key = None
        user.licensed_at = None
        user.license_expires_at = None
        user.license_type = "free"
        if user.id != current_user.id:
            user.is_admin = bool(user.is_admin and user.username.lower() in ADMIN_USERNAMES)
        db.session.commit()
        return {
            "success": True,
            "message": f"لایسنس «{user.username}» حذف شد و حساب به نسخه آزمایشی برگشت.",
            "user": admin_user_payload(user),
        }

    tier = request.form.get("tier", "PRO").strip().upper() or "PRO"
    duration = request.form.get("duration", "LIFE").strip()
    expires_at_raw = request.form.get("expires_at", "").strip()

    if duration.upper() == "CUSTOM":
        duration = request.form.get("custom_days", "").strip() or "LIFE"

    if expires_at_raw:
        try:
            target = date.fromisoformat(expires_at_raw)
        except ValueError:
            return json_error("تاریخ انقضا معتبر نیست.")
        days = (target - date.today()).days
        if days <= 0:
            return json_error("تاریخ انقضا باید بعد از امروز باشد.")
        period_code, days, duration_label = normalize_duration(str(days))
        expires_at = datetime.combine(target, datetime.min.time()).replace(hour=23, minute=59)
    else:
        period_code, days, duration_label = normalize_duration(duration)
        expires_at = datetime.now() + timedelta(days=days) if days else None

    user.is_licensed = True
    user.license_type = tier
    user.licensed_at = user.licensed_at or datetime.now()
    user.license_expires_at = expires_at
    user.license_key = generate_key(user.username, tier, period_code)
    db.session.commit()

    validity = "مادام‌العمر (دائمی)" if expires_at is None else f"تا {shamsi_label(expires_at.date())}"
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
    if user.username.lower() in ADMIN_USERNAMES:
        return json_error("حساب مدیر اصلی سامانه قابل حذف نیست.")

    username = user.username
    Product.query.filter_by(owner_id=user.id).delete(synchronize_session=False)
    Supplier.query.filter_by(owner_id=user.id).delete(synchronize_session=False)
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

    period_code, days, duration_label = normalize_duration(duration)
    validity_desc = "مادام‌العمر (دائمی)" if days is None else f"{days} روز پس از فعال‌سازی"

    if is_master:
        key = generate_master_key(tier, period_code)
        ident_display = "کلید سراسری (Universal Master Key)"
        code_display = "همه دستگاه‌ها و حساب‌ها"
    else:
        if not ident:
            return json_error("نام کاربری یا شناسه فعال‌سازی مشتری را وارد کنید.")
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
    supplier_count = Supplier.query.filter_by(owner_id=current_user.id).count()
    active_count = Product.query.filter_by(owner_id=current_user.id).filter(active_clause()).count()
    archived_count = Product.query.filter_by(owner_id=current_user.id).filter(archived_clause()).count()

    user = db.session.get(User, current_user.id)
    limits = get_user_limits(user)

    return render_template(
        "account.html",
        supplier_count=supplier_count,
        active_count=active_count,
        archived_count=archived_count,
        api_token=ensure_api_token(user),
        api_base=request.url_root.rstrip("/"),
        limits=limits,
    )


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
    if len(new_password) < 4:
        return json_error("رمز عبور جدید حداقل ۴ کاراکتر باشد.")
    if new_password != confirm_password:
        return json_error("تکرار رمز عبور جدید یکسان نیست.")
    if check_password_hash(user.password_hash, new_password):
        return json_error("رمز جدید با رمز فعلی فرقی ندارد.")

    user.password_hash = generate_password_hash(new_password)
    db.session.commit()
    login_user(user)
    return {"success": True, "message": "رمز عبور عوض شد."}


@app.route("/users")
def users_redirect():
    return redirect(url_for("account"))


@app.route("/users/<int:user_id>/delete", methods=["GET", "POST"])
@app.route("/users/<int:user_id>", methods=["GET", "POST", "DELETE"])
def users_gone(user_id):
    return json_error("این مسیر غیرفعال است.", 403)


if __name__ == "__main__":
    with app.app_context():
        ensure_schema()
    debug = os.environ.get("FLASK_DEBUG", "0") in ("1", "true", "True")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=debug)
