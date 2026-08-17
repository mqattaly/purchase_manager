import os
from dotenv import load_dotenv
load_dotenv()

from flask import Flask, render_template, request, redirect, url_for, send_file
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy.pool import NullPool
import jdatetime
from datetime import date
import pandas as pd
from sqlalchemy.orm import joinedload
import difflib
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL")

if os.environ.get("VERCEL"):
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"poolclass": NullPool}

db = SQLAlchemy(app)

UNIT_TYPES = ["عدد", "کارتن", "بسته", "گونی", "کیلو"]
def normalize_name(name):
    name = " ".join(name.split())
    name = name.replace("ي", "ی").replace("ك", "ک")
    return name.strip().lower()
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

class Supplier(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), unique=True, nullable=False)
    products = db.relationship("Product", backref="supplier", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Supplier {self.name}>"


class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
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
    return User.query.get(int(user_id))


@app.before_request
def require_login():
    allowed = ["login", "static"]
    if request.endpoint not in allowed and not current_user.is_authenticated:
        return redirect(url_for("login"))

@app.route("/")
def home():
    all_suppliers = Supplier.query.all()
    return render_template("dashboard.html", suppliers=all_suppliers)
    
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        user = User.query.filter_by(username=username).first()

        if user and check_password_hash(user.password_hash, password):
            login_user(user)
            return redirect("/")

        return render_template("login.html", error="نام کاربری یا رمز عبور اشتباه است.")

    return render_template("login.html", error=None)


@app.route("/logout")
def logout():
    logout_user()
    return redirect(url_for("login"))

@app.route("/new-purchase", methods=["GET", "POST"])
def new_purchase():
    if request.method == "POST":
        supplier_id = request.form.get("supplier")
        product_name = request.form.get("product", "").strip()
        quantity = request.form.get("quantity", "").strip()
        unit = request.form.get("unit")
        description = request.form.get("description", "").strip()

        wants_json = request.headers.get("X-Requested-With") == "fetch"

        error = None
        if not supplier_id:
            error = "تأمین‌کننده را انتخاب کنید."
        elif not product_name:
            error = "نام محصول را وارد کنید."
        elif not quantity:
            error = "تعداد را وارد کنید."
        elif unit not in UNIT_TYPES:
            error = "نوع تعداد را انتخاب کنید."

        if error:
            if wants_json:
                return {"success": False, "message": error}, 400
            suppliers = Supplier.query.all()
            return render_template("new_purchase.html", suppliers=suppliers, message=error, success=False)

        new_product = Product(
            supplier_id=supplier_id,
            product_name=product_name,
            quantity=quantity,
            unit=unit,
            description=description
        )
        db.session.add(new_product)
        db.session.commit()

        if wants_json:
            return {
                "success": True,
                "message": "خرید با موفقیت ثبت شد",
                "product": {
                    "id": new_product.id,
                    "supplier_id": new_product.supplier_id,
                    "supplier_name": new_product.supplier.name,
                    "product_name": new_product.product_name,
                    "quantity": new_product.quantity,
                    "unit": new_product.unit,
                    "description": new_product.description
                }
            }

        suppliers = Supplier.query.all()
        return render_template("new_purchase.html", suppliers=suppliers, message="✓ خرید با موفقیت ثبت شد", success=True)

    suppliers = Supplier.query.all()
    return render_template("new_purchase.html", suppliers=suppliers, message=None)
 
@app.route("/check-duplicate")
def check_duplicate():
    product_name = request.args.get("product", "").strip()
    current_supplier_id = request.args.get("supplier")

    if not product_name:
        return {"matches": []}

    matches = (
        Product.query
        .filter_by(ordered=False)
        .filter(db.func.lower(Product.product_name) == product_name.lower())
        .all()
    )

    return {
        "matches": [
            {
                "supplier_name": m.supplier.name,
                "quantity": m.quantity,
                "unit": m.unit,
                "same_supplier": str(m.supplier_id) == str(current_supplier_id)
            }
            for m in matches
        ]
    }
 
@app.route("/purchases")
def purchases():
    all_products = (
        Product.query
        .options(joinedload(Product.supplier))
        .filter_by(ordered=False)
        .all()
    )
    return render_template("purchases.html", products=all_products)

@app.route("/toggle-order/<int:product_id>", methods=["POST"])
def toggle_order(product_id):
    product = Product.query.get_or_404(product_id)
    product.ordered = True
    product.ordered_date = date.today()
    db.session.commit()

    if request.headers.get("X-Requested-With") == "fetch":
        return {"success": True}

    return redirect(request.referrer or "/purchases")

    
@app.route("/unarchive/<int:product_id>", methods=["POST"])
def unarchive_product(product_id):
    product = Product.query.get_or_404(product_id)
    product.ordered = False
    product.ordered_date = None
    db.session.commit()

    if request.headers.get("X-Requested-With") == "fetch":
        return {"success": True}

    return redirect(request.referrer or f"/supplier/{product.supplier_id}")
    
@app.route("/product/<int:product_id>/edit", methods=["GET", "POST"])
def edit_product(product_id):
    product = Product.query.get_or_404(product_id)
    wants_json = request.headers.get("X-Requested-With") == "fetch"

    if request.method == "POST":
        product.product_name = request.form.get("product", "").strip()
        product.quantity = request.form.get("quantity", "").strip()
        product.unit = request.form.get("unit")
        product.description = request.form.get("description", "").strip()
        db.session.commit()

        if wants_json:
            return {
                "success": True,
                "product": {
                    "id": product.id,
                    "supplier_id": product.supplier_id,
                    "supplier_name": product.supplier.name,
                    "product_name": product.product_name,
                    "quantity": product.quantity,
                    "unit": product.unit,
                    "description": product.description
                }
            }

        return redirect(f"/supplier/{product.supplier_id}")

    return render_template("product_edit.html", product=product)


@app.route("/product/<int:product_id>/delete", methods=["POST"])
def delete_product(product_id):
    product = Product.query.get_or_404(product_id)
    supplier_id = product.supplier_id
    wants_json = request.headers.get("X-Requested-With") == "fetch"

    db.session.delete(product)
    db.session.commit()

    if wants_json:
        return {"success": True}

    return redirect(f"/supplier/{supplier_id}")
    
@app.route("/product/<int:product_id>/info")
def product_info(product_id):
    product = Product.query.get_or_404(product_id)
    return {
        "product": {
            "id": product.id,
            "supplier_id": product.supplier_id,
            "supplier_name": product.supplier.name,
            "product_name": product.product_name,
            "quantity": product.quantity,
            "unit": product.unit,
            "description": product.description
        }
    }    

@app.route("/supplier/<int:supplier_id>/archive/<date_str>/delete", methods=["POST"])
def delete_archive_group(supplier_id, date_str):
    if date_str == "unknown":
        products_to_delete = Product.query.filter_by(
            supplier_id=supplier_id, ordered=True, ordered_date=None
        ).all()
    else:
        target_date = date.fromisoformat(date_str)
        products_to_delete = Product.query.filter_by(
            supplier_id=supplier_id, ordered=True, ordered_date=target_date
        ).all()

    for p in products_to_delete:
        db.session.delete(p)
    db.session.commit()

    if request.headers.get("X-Requested-With") == "fetch":
        return {"success": True}

    return redirect(f"/supplier/{supplier_id}")
@app.route("/suppliers", methods=["GET", "POST"])
def suppliers():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        wants_json = request.headers.get("X-Requested-With") == "fetch"

        if not name:
            if wants_json:
                return {"success": False, "message": "نام تأمین‌کننده را وارد کنید."}, 400
            return redirect("/suppliers")

        existing = Supplier.query.filter_by(name=name).first()
        if existing:
            if wants_json:
                return {"success": False, "message": "این تأمین‌کننده قبلاً ثبت شده است."}, 400
            return redirect("/suppliers")

        new_supplier = Supplier(name=name)
        db.session.add(new_supplier)
        db.session.commit()

        if wants_json:
            return {"success": True, "id": new_supplier.id, "name": new_supplier.name}

        return redirect("/suppliers")

    all_suppliers = Supplier.query.all()
    return render_template("suppliers.html", suppliers=all_suppliers)
    
@app.route("/supplier/<int:supplier_id>")
def supplier_detail(supplier_id):
    supplier = Supplier.query.get_or_404(supplier_id)
    active_products = Product.query.filter_by(supplier_id=supplier_id, ordered=False).all()

    archived_products = (
        Product.query
        .filter_by(supplier_id=supplier_id, ordered=True)
        .order_by(Product.ordered_date.desc())
        .all()
    )

    groups = {}
    for item in archived_products:
        if item.ordered_date:
            iso_key = item.ordered_date.isoformat()
            shamsi = jdatetime.date.fromgregorian(date=item.ordered_date)
            label = shamsi.strftime("%Y/%m/%d")
        else:
            iso_key = "unknown"
            label = "بدون تاریخ"

        if iso_key not in groups:
            groups[iso_key] = {"label": label, "products": []}
        groups[iso_key]["products"].append(item)

    return render_template(
        "supplier_detail.html",
        supplier=supplier,
        products=active_products,
        groups=groups
    )    
    
@app.route("/supplier/<int:supplier_id>/edit", methods=["GET", "POST"])
def edit_supplier(supplier_id):
    supplier = Supplier.query.get_or_404(supplier_id)

    if request.method == "POST":
        new_name = request.form.get("name", "").strip()
        if new_name:
            supplier.name = new_name
            db.session.commit()
        return redirect("/suppliers")

    return render_template("supplier_edit.html", supplier=supplier)


@app.route("/supplier/<int:supplier_id>/delete", methods=["POST"])
def delete_supplier(supplier_id):
    supplier = Supplier.query.get_or_404(supplier_id)
    db.session.delete(supplier)
    db.session.commit()
    return redirect("/suppliers")
    
@app.route("/download-template")
def download_template():
    return send_file(
        "static/sample_import.xlsx",
        as_attachment=True,
        download_name="نمونه_ایمپورت_خرید.xlsx"
    )


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
            errors=None
        )

    filename = uploaded_file.filename.lower()

    try:
        if filename.endswith(".csv"):
            df = pd.read_csv(uploaded_file, encoding="utf-8-sig")
        else:
            df = pd.read_excel(uploaded_file)
    except Exception as e:
        return render_template(
            "import_excel.html",
            message=f"خطا در خواندن فایل: {e}",
            success=False,
            errors=None
        )

    df = df.dropna(how="all")

    # پیش‌بارگذاری تأمین‌کننده‌های موجود برای تطبیق هوشمند
    existing_suppliers = Supplier.query.all()
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
            description = str(row.iloc[4]).strip() if len(row) > 4 and pd.notna(row.iloc[4]) else ""
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

            new_supplier = Supplier(name=supplier_name)
            db.session.add(new_supplier)
            db.session.flush()

            normalized_map[norm_name] = new_supplier
            normalized_names.append(norm_name)
            supplier_id = new_supplier.id

        new_product = Product(
            supplier_id=supplier_id,
            product_name=product_name,
            quantity=str(quantity),
            unit=unit,
            description=description
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
        errors=errors if errors else None
    )
    
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True)
