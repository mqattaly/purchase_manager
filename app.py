import os
import secrets
from datetime import date, datetime

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, render_template, request, redirect, url_for, send_file
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
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
os.makedirs(INSTANCE_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")

db_url = os.environ.get("DATABASE_URL")
if db_url:
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = db_url
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.join(INSTANCE_DIR, "purchase.db")

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
# Static files are versioned with ?v=<hash>, so they can be cached hard.
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 60 * 60 * 24 * 365

if os.environ.get("VERCEL"):
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"poolclass": NullPool}


@app.after_request
def _allow_iframe_preview(response):
    # Arena shows the app inside an iframe; don't block embedding.
    response.headers.pop("X-Frame-Options", None)
    response.headers["Content-Security-Policy"] = "frame-ancestors *"
    host = (request.host or "").lower()
    if "e2b.app" in host:
        app.config["SESSION_COOKIE_SAMESITE"] = "None"
        app.config["SESSION_COOKIE_SECURE"] = True
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


def wants_json():
    return request.headers.get("X-Requested-With") == "fetch"


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
    """محاسبه وضعیت سهمیه و لایسنس کاربر"""
    if user is None:
        if current_user.is_authenticated:
            user = current_user
        else:
            return {
                "is_licensed": False,
                "is_admin": False,
                "user_code": "",
                "supplier_count": 0,
                "product_count": 0,
                "max_suppliers": FREE_MAX_SUPPLIERS,
                "max_products": FREE_MAX_PRODUCTS,
                "can_add_supplier": False,
                "can_add_product": False,
                "license_type": "free",
                "licensed_at": None,
                "license_key": None,
            }

    admin = is_admin_user(user)
    is_lic = bool(user.is_licensed or admin)
    s_count = Supplier.query.filter_by(owner_id=user.id).count()
    p_count = Product.query.filter_by(owner_id=user.id).count()

    return {
        "is_licensed": is_lic,
        "is_admin": admin,
        "user_code": get_user_code(user),
        "supplier_count": s_count,
        "product_count": p_count,
        "max_suppliers": None if is_lic else FREE_MAX_SUPPLIERS,
        "max_products": None if is_lic else FREE_MAX_PRODUCTS,
        "can_add_supplier": is_lic or (s_count < FREE_MAX_SUPPLIERS),
        "can_add_product": is_lic or (p_count < FREE_MAX_PRODUCTS),
        "license_type": user.license_type if is_lic else "free",
        "licensed_at": user.licensed_at,
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
    return Supplier.query.filter_by(owner_id=current_user.id).order_by(Supplier.name).all()


def user_products(**filters):
    """Base product query scoped to the logged-in user (supplier eagerly loaded)."""
    return (
        Product.query.options(joinedload(Product.supplier))
        .filter_by(owner_id=current_user.id, **filters)
    )


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
        "product_name": request.form.get("product", "").strip(),
        "quantity": request.form.get("quantity", "").strip(),
        "unit": request.form.get("unit", ""),
        "description": request.form.get("description", "").strip(),
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

        # ارتقای خودکار کاربر smq2458 به عنوان مدیر و دارای لایسنس کامل
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
    limits = get_user_limits(current_user) if current_user.is_authenticated else None
    admin = is_admin_user(current_user) if current_user.is_authenticated else False
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

    allowed = {"login", "signup", "static", "api_quick_add", "api_suppliers"}
    if request.endpoint in allowed or request.endpoint is None:
        return
    if not current_user.is_authenticated:
        return redirect(url_for("login"))


@app.route("/")
def home():
    """صفحه اول اپلیکیشن: داشبورد"""
    suppliers = user_suppliers()

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

    recent = (
        user_products(ordered=False).order_by(Product.id.desc()).limit(15).all()
    )

    limits = get_user_limits(current_user)

    return render_template(
        "dashboard.html",
        suppliers=suppliers,
        product_names=recent_product_names(),
        active_count=active_count,
        archived_count=archived_count,
        recent=recent,
        limits=limits,
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect("/")

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password_hash, password):
            if user.username.lower() in ADMIN_USERNAMES:
                user.is_admin = True
                user.is_licensed = True
                user.license_type = "UNLIMITED"
                db.session.commit()
            login_user(user)
            return redirect("/")
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
        login_user(user)
        return redirect("/")

    return render_template("login.html", mode="signup", error=None)


@app.route("/logout")
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/search")
def search():
    return render_template("search.html")


@app.route("/new-purchase", methods=["GET", "POST"])
def new_purchase():
    limits = get_user_limits(current_user)

    if request.method == "POST":
        if not limits["can_add_product"]:
            msg = "سقف نسخه آزمایشی (۵ محصول) تکمیل شده است. برای ثبت محصول جدید باید لایسنس لیستیا را تهیه کنید."
            if wants_json():
                return json_error(msg, 403, {"license_locked": True})
            return render_template(
                "new_purchase.html",
                suppliers=user_suppliers(),
                message=msg,
                success=False,
                limits=limits,
            )

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
            if wants_json():
                return json_error(error)
            return render_template(
                "new_purchase.html",
                suppliers=user_suppliers(),
                message=error,
                success=False,
                limits=limits,
            )

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

        if wants_json():
            return {
                "success": True,
                "message": "خرید ثبت شد",
                "product": product_payload(new_product, supplier.name),
            }

        return redirect("/new-purchase")

    return render_template(
        "new_purchase.html",
        suppliers=user_suppliers(),
        product_names=recent_product_names(),
        message=None,
        limits=limits,
    )


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
    all_products = user_products(ordered=False).order_by(Product.id.desc()).all()
    return render_template("purchases.html", products=all_products, suppliers=user_suppliers())


@app.route("/toggle-order/<int:product_id>", methods=["POST"])
def toggle_order(product_id):
    product = owned_product_or_404(product_id)
    product.ordered = True
    product.ordered_date = date.today()
    db.session.commit()

    if wants_json():
        return {"success": True, "product": product_payload(product)}
    return redirect(request.referrer or "/purchases")


@app.route("/unarchive/<int:product_id>", methods=["POST"])
def unarchive_product(product_id):
    product = owned_product_or_404(product_id)
    product.ordered = False
    product.ordered_date = None
    db.session.commit()

    if wants_json():
        return {"success": True, "product": product_payload(product)}
    return redirect(request.referrer or f"/supplier/{product.supplier_id}")


@app.route("/product/<int:product_id>/edit", methods=["GET", "POST"])
def edit_product(product_id):
    product = owned_product_or_404(product_id)
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
            return render_template("product_edit.html", product=product, suppliers=suppliers)

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

    return render_template("product_edit.html", product=product, suppliers=suppliers)


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
        Product.query.filter_by(
            owner_id=current_user.id, supplier_id=supplier_id, ordered=False
        )
        .order_by(Product.id.desc())
        .all()
    )

    archived_products = (
        Product.query.filter_by(
            owner_id=current_user.id, supplier_id=supplier_id, ordered=True
        )
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

    if request.method == "POST":
        new_name = request.form.get("name", "").strip()
        if not new_name:
            if wants_json():
                return json_error("نام تأمین‌کننده را وارد کنید.")
            return render_template("supplier_edit.html", supplier=supplier)

        clash = (
            Supplier.query.filter_by(owner_id=current_user.id, name=new_name)
            .filter(Supplier.id != supplier.id)
            .first()
        )
        if clash:
            if wants_json():
                return json_error("این نام قبلاً ثبت شده است.")
            return render_template("supplier_edit.html", supplier=supplier)

        supplier.name = new_name
        db.session.commit()

        if wants_json():
            return {"success": True, "id": supplier.id, "name": supplier.name}
        return redirect("/suppliers")

    return render_template("supplier_edit.html", supplier=supplier)


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
    active_counts = {}
    if supplier_ids:
        active_counts = dict(
            db.session.query(Product.supplier_id, db.func.count(Product.id))
            .filter(
                Product.owner_id == current_user.id,
                Product.ordered.is_(False),
                Product.supplier_id.in_(supplier_ids),
            )
            .group_by(Product.supplier_id)
            .all()
        )

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

    filename = uploaded_file.filename.lower()

    try:
        if filename.endswith(".csv"):
            df = pd.read_csv(uploaded_file, encoding="utf-8-sig")
        else:
            df = pd.read_excel(uploaded_file)
    except Exception as exc:
        return render_template(
            "import_excel.html",
            message=f"خطا در خواندن فایل: {exc}",
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


@app.route("/api/license-status")
def api_license_status():
    """دریافت وضعیت لایسنس کاربر برای UI"""
    limits = get_user_limits(current_user)
    return {"success": True, "limits": limits}


@app.route("/account/license", methods=["POST"])
def activate_license():
    """فعال‌سازی کلید لایسنس برای حساب کاربر جاری"""
    key = request.form.get("license_key", "").strip()
    if not key:
        return json_error("لطفاً کلید لایسنس را وارد کنید.")

    is_valid, tier, msg = verify_key(current_user, key)
    if not is_valid:
        return json_error(msg, 400)

    user = db.session.get(User, current_user.id)
    user.is_licensed = True
    user.license_key = key.strip().upper()
    user.licensed_at = datetime.now()
    user.license_type = tier or "pro"
    db.session.commit()

    return {
        "success": True,
        "message": "✓ لایسنس با موفقیت فعال شد! محدودیت‌ها برای همیشه حذف شدند.",
        "license_type": user.license_type,
        "is_licensed": True,
    }


# ---------------------------------------------------------------------------
#  پنل ادمین: تولید لایسنس داخل وب‌اپلیکیشن (مخصوص س.م.قتالی smq2458)
# ---------------------------------------------------------------------------

@app.route("/admin/license-generator")
def admin_license_generator():
    if not is_admin_user(current_user):
        return json_error("دسترسی به این بخش فقط برای مدیر نرم‌افزار مجاز است.", 403)

    all_users = User.query.order_by(User.id.desc()).all()
    user_list = []
    for u in all_users:
        user_limits = get_user_limits(u)
        user_list.append({
            "id": u.id,
            "username": u.username,
            "user_code": user_limits["user_code"],
            "is_licensed": user_limits["is_licensed"],
            "supplier_count": user_limits["supplier_count"],
            "product_count": user_limits["product_count"],
            "license_type": u.license_type,
            "license_key": u.license_key,
        })

    return render_template("admin_license.html", users=user_list)


@app.route("/api/admin/generate-license", methods=["POST"])
def api_admin_generate_license():
    if not is_admin_user(current_user):
        return json_error("دسترسی غیرمجاز.", 403)

    ident = request.form.get("identifier", "").strip()
    tier = request.form.get("tier", "PRO").strip().upper()
    is_master = request.form.get("is_master") in ["true", "1", "on"]

    if is_master:
        key = generate_master_key(tier)
        ident_display = "کلید سراسری (Universal Master Key)"
        code_display = "همه دستگاه‌ها و حساب‌ها"
    else:
        if not ident:
            return json_error("نام کاربری یا شناسه فعال‌سازی مشتری را وارد کنید.")
        key = generate_key(ident, tier)
        ident_display = ident
        code_display = get_user_code(ident)

    customer_msg = (
        f"با سلام، لایسنس نسخه نامحدود «لیستیا» برای شما فعال شد:\n\n"
        f"🔑 کلید لایسنس شما:\n{key}\n\n"
        f"روش فعال‌سازی: وارد نرم‌افزار شوید، روی «نسخه آزمایشی» در منو کلیک کنید و کلید بالا را وارد نمایید.\n"
        f"با آرزوی موفقیت · طراحی و توسعه: س.م.قتالی"
    )

    return {
        "success": True,
        "license_key": key,
        "identifier": ident_display,
        "user_code": code_display,
        "tier": tier,
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
    active_count = Product.query.filter_by(owner_id=current_user.id, ordered=False).count()
    archived_count = Product.query.filter_by(owner_id=current_user.id, ordered=True).count()

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
    new_username = request.form.get("username", "").strip()
    password = request.form.get("current_password", "")

    user = db.session.get(User, current_user.id)

    if not new_username:
        return json_error("نام کاربری را وارد کنید.")
    if len(new_username) < 2:
        return json_error("نام کاربری خیلی کوتاه است.")
    if not check_password_hash(user.password_hash, password):
        return json_error("رمز عبور فعلی درست نیست.")
    if new_username == user.username:
        return json_error("این همان نام کاربری فعلی است.")
    if User.query.filter(User.username == new_username, User.id != user.id).first():
        return json_error("این نام کاربری قبلاً وجود دارد.")

    user.username = new_username
    db.session.commit()
    return {"success": True, "username": user.username}


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
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
