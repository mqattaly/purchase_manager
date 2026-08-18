import os
from datetime import date

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
from sqlalchemy import inspect, text
from sqlalchemy.orm import joinedload
from sqlalchemy.pool import NullPool
import jdatetime

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


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def product_payload(product):
    return {
        "id": product.id,
        "supplier_id": product.supplier_id,
        "supplier_name": product.supplier.name if product.supplier else "",
        "product_name": product.product_name,
        "quantity": product.quantity,
        "unit": product.unit,
        "description": product.description or "",
        "ordered": bool(product.ordered),
        "ordered_date": product.ordered_date.isoformat() if product.ordered_date else None,
        "ordered_date_label": shamsi_label(product.ordered_date),
    }


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

    with db.engine.begin() as conn:
        for table in missing_owner_cols:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN owner_id INTEGER"))
        try:
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_supplier_owner_name "
                    "ON supplier (owner_id, name)"
                )
            )
        except Exception:
            pass
        try:
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_supplier_owner ON supplier (owner_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_product_owner ON product (owner_id)"))
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


_schema_ready = False


@app.before_request
def _prepare_request():
    global _schema_ready
    if not _schema_ready:
        try:
            ensure_schema()
            _schema_ready = True
        except Exception:
            # Retry on the next request if the database is temporarily unavailable.
            pass

    allowed = {"login", "signup", "static"}
    if request.endpoint in allowed or request.endpoint is None:
        return
    if not current_user.is_authenticated:
        return redirect(url_for("login"))


@app.route("/")
def home():
    all_suppliers = (
        Supplier.query.filter_by(owner_id=current_user.id).order_by(Supplier.name).all()
    )
    return render_template("dashboard.html", suppliers=all_suppliers)


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect("/")

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password_hash, password):
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

        user = User(username=username, password_hash=generate_password_hash(password))
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
    suppliers = (
        Supplier.query.filter_by(owner_id=current_user.id).order_by(Supplier.name).all()
    )

    if request.method == "POST":
        supplier_id = request.form.get("supplier")
        product_name = request.form.get("product", "").strip()
        quantity = request.form.get("quantity", "").strip()
        unit = request.form.get("unit")
        description = request.form.get("description", "").strip()

        error = None
        supplier = None
        if not supplier_id:
            error = "تأمین‌کننده را انتخاب کنید."
        else:
            supplier = Supplier.query.filter_by(
                id=supplier_id, owner_id=current_user.id
            ).first()
            if not supplier:
                error = "تأمین‌کننده معتبر نیست."
        if not error and not product_name:
            error = "نام محصول را وارد کنید."
        elif not error and not quantity:
            error = "تعداد را وارد کنید."
        elif not error and unit not in UNIT_TYPES:
            error = "نوع تعداد را انتخاب کنید."

        if error:
            if wants_json():
                return {"success": False, "message": error}, 400
            return render_template(
                "new_purchase.html", suppliers=suppliers, message=error, success=False
            )

        new_product = Product(
            owner_id=current_user.id,
            supplier_id=supplier.id,
            product_name=product_name,
            quantity=quantity,
            unit=unit,
            description=description,
        )
        db.session.add(new_product)
        db.session.commit()

        if wants_json():
            return {
                "success": True,
                "message": "خرید با موفقیت ثبت شد",
                "product": product_payload(new_product),
            }

        return render_template(
            "new_purchase.html",
            suppliers=suppliers,
            message="✓ خرید با موفقیت ثبت شد",
            success=True,
        )

    return render_template("new_purchase.html", suppliers=suppliers, message=None)


@app.route("/check-duplicate")
def check_duplicate():
    product_name = request.args.get("product", "").strip()
    current_supplier_id = request.args.get("supplier")

    if not product_name:
        return {"matches": []}

    matches = (
        Product.query.options(joinedload(Product.supplier))
        .filter_by(owner_id=current_user.id, ordered=False)
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
    all_products = (
        Product.query.options(joinedload(Product.supplier))
        .filter_by(owner_id=current_user.id, ordered=False)
        .order_by(Product.id.desc())
        .all()
    )
    return render_template("purchases.html", products=all_products)


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

    if request.method == "POST":
        name = request.form.get("product", "").strip()
        quantity = request.form.get("quantity", "").strip()
        unit = request.form.get("unit")
        description = request.form.get("description", "").strip()

        if not name or not quantity or unit not in UNIT_TYPES:
            if wants_json():
                return {"success": False, "message": "اطلاعات محصول کامل نیست."}, 400
            return render_template("product_edit.html", product=product)

        product.product_name = name
        product.quantity = quantity
        product.unit = unit
        product.description = description
        db.session.commit()

        if wants_json():
            return {"success": True, "product": product_payload(product)}
        return redirect(f"/supplier/{product.supplier_id}")

    return render_template("product_edit.html", product=product)


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
    if request.method == "POST":
        name = request.form.get("name", "").strip()

        if not name:
            if wants_json():
                return {"success": False, "message": "نام تأمین‌کننده را وارد کنید."}, 400
            return redirect("/suppliers")

        existing = Supplier.query.filter_by(owner_id=current_user.id, name=name).first()
        if existing:
            if wants_json():
                return {"success": False, "message": "این تأمین‌کننده قبلاً ثبت شده است."}, 400
            return redirect("/suppliers")

        new_supplier = Supplier(name=name, owner_id=current_user.id)
        db.session.add(new_supplier)
        db.session.commit()

        if wants_json():
            return {"success": True, "id": new_supplier.id, "name": new_supplier.name}
        return redirect("/suppliers")

    all_suppliers = (
        Supplier.query.filter_by(owner_id=current_user.id).order_by(Supplier.name).all()
    )
    return render_template("suppliers.html", suppliers=all_suppliers)


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
        .order_by(Product.ordered_date.desc(), Product.id.desc())
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
                return {"success": False, "message": "نام تأمین‌کننده را وارد کنید."}, 400
            return render_template("supplier_edit.html", supplier=supplier)

        clash = (
            Supplier.query.filter_by(owner_id=current_user.id, name=new_name)
            .filter(Supplier.id != supplier.id)
            .first()
        )
        if clash:
            if wants_json():
                return {"success": False, "message": "این نام قبلاً ثبت شده است."}, 400
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
        download_name="نمونه_ایمپورت_خرید.xlsx",
    )


@app.route("/api/search")
def api_search():
    query = request.args.get("q", "").strip()

    if not query or len(query) < 2:
        return {"results": []}

    matches = (
        Product.query.options(joinedload(Product.supplier))
        .filter_by(owner_id=current_user.id)
        .filter(Product.product_name.ilike(f"%{query}%"))
        .order_by(Product.ordered.asc(), Product.id.desc())
        .limit(50)
        .all()
    )

    return {"results": [product_payload(item) for item in matches]}


@app.route("/import", methods=["GET", "POST"])
def import_excel():
    if request.method == "GET":
        return render_template("import_excel.html", message=None, errors=None)

    uploaded_file = request.files.get("file")

    if not uploaded_file or uploaded_file.filename == "":
        return render_template(
            "import_excel.html",
            message="فایلی انتخاب نشده است.",
            success=False,
            errors=None,
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
        )

    df = df.dropna(how="all")

    existing_suppliers = Supplier.query.filter_by(owner_id=current_user.id).all()
    normalized_map = {normalize_name(s.name): s for s in existing_suppliers}
    normalized_names = list(normalized_map.keys())

    added_count = 0
    errors = []

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

            new_supplier = Supplier(name=supplier_name, owner_id=current_user.id)
            db.session.add(new_supplier)
            db.session.flush()

            normalized_map[norm_name] = new_supplier
            normalized_names.append(norm_name)
            supplier_id = new_supplier.id

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
        success=True,
        errors=errors if errors else None,
    )


@app.route("/account")
def account():
    """Personal account page — only ever shows the logged-in user's own data."""
    supplier_count = Supplier.query.filter_by(owner_id=current_user.id).count()
    active_count = Product.query.filter_by(owner_id=current_user.id, ordered=False).count()
    archived_count = Product.query.filter_by(owner_id=current_user.id, ordered=True).count()

    return render_template(
        "account.html",
        supplier_count=supplier_count,
        active_count=active_count,
        archived_count=archived_count,
    )


@app.route("/account/username", methods=["POST"])
def change_username():
    new_username = request.form.get("username", "").strip()
    password = request.form.get("current_password", "")

    user = db.session.get(User, current_user.id)

    if not new_username:
        return {"success": False, "message": "نام کاربری را وارد کنید."}, 400
    if len(new_username) < 2:
        return {"success": False, "message": "نام کاربری خیلی کوتاه است."}, 400
    if not check_password_hash(user.password_hash, password):
        return {"success": False, "message": "رمز عبور فعلی درست نیست."}, 400
    if new_username == user.username:
        return {"success": False, "message": "این همان نام کاربری فعلی است."}, 400
    if User.query.filter(User.username == new_username, User.id != user.id).first():
        return {"success": False, "message": "این نام کاربری قبلاً وجود دارد."}, 400

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
        return {"success": False, "message": "رمز عبور فعلی درست نیست."}, 400
    if len(new_password) < 4:
        return {"success": False, "message": "رمز عبور جدید حداقل ۴ کاراکتر باشد."}, 400
    if new_password != confirm_password:
        return {"success": False, "message": "تکرار رمز عبور جدید یکسان نیست."}, 400
    if check_password_hash(user.password_hash, new_password):
        return {"success": False, "message": "رمز جدید با رمز فعلی فرقی ندارد."}, 400

    user.password_hash = generate_password_hash(new_password)
    db.session.commit()
    login_user(user)  # refresh the session after the credentials change
    return {"success": True, "message": "رمز عبور عوض شد."}


@app.route("/users")
def users_redirect():
    """Old users list is gone — nobody may see or manage other accounts."""
    return redirect(url_for("account"))


@app.route("/users/<int:user_id>/delete", methods=["GET", "POST"])
@app.route("/users/<int:user_id>", methods=["GET", "POST", "DELETE"])
def users_gone(user_id):
    """Legacy account-management endpoints are closed off to prevent abuse."""
    return {"success": False, "message": "این مسیر غیرفعال است."}, 403


if __name__ == "__main__":
    with app.app_context():
        ensure_schema()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
