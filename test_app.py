import os

# مهم: این متغیر باید «قبل» از import اپ ست شود. Flask-SQLAlchemy موتور دیتابیس
# را هنگام init با مقدار همان لحظه می‌سازد و تغییر بعدی SQLALCHEMY_DATABASE_URI
# اثری ندارد؛ در غیر این صورت تست‌ها خاموشی/حذف جدول‌ها را روی فایل دیتابیسِ
# واقعی (یا حتی DATABASE_URL محیط) انجام می‌دهند.
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

import io
import unittest
from datetime import datetime, timedelta
import pandas as pd
from werkzeug.security import check_password_hash
from app import (
    app, db, User, Supplier, Product,
    get_user_limits, issue_api_token, is_admin_user, normalize_phone,
    _rate_buckets,
)
from licensing import get_user_code, generate_key, generate_master_key, verify_key


class ListiaTestCase(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        # سقف نرخ ورود/ثبت‌نام برای هر تست ریست شود تا تست‌ها به هم نخورند
        _rate_buckets.clear()
        self.client = app.test_client()
        with app.app_context():
            # با انجین in-memoryِ ساخته‌شده هنگام import، جداول تازه می‌سازد
            db.create_all()

    def tearDown(self):
        with app.app_context():
            db.session.remove()
            db.drop_all()

    def register_and_login(self, username="testuser", password="password123",
                           first_name="کاربر", last_name="تستی", phone="09120000000"):
        res = self.client.post("/signup", data={
            "username": username,
            "password": password,
            "first_name": first_name,
            "last_name": last_name,
            "phone": phone,
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        # اگر ثبت‌نام خطا داشته باشد، صفحه ورود با پیام خطا برمی‌گردد — آن را زود شکار کن
        if "login-error" in res.get_data(as_text=True):
            with app.app_context():
                existing = [(u.id, u.username) for u in User.query.all()]
            self.fail(f"signup برای {username} خطا خورد؛ کاربران موجود در دیتابیس: {existing}")
        return res

    def test_time_limited_and_lifetime_licenses(self):
        self.register_and_login("morteza", "123456")

        # 1. Generate 30-day license for morteza
        key_30d = generate_key("morteza", "PRO", "30D")
        self.assertTrue(key_30d.startswith("LST-30D-"))

        # 2. Activate 30-day license
        res = self.client.post("/account/license", data={"license_key": key_30d}, headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertIn("30 روز", data["message"])

        with app.app_context():
            user = User.query.filter_by(username="morteza").first()
            self.assertTrue(user.is_licensed)
            self.assertIsNotNone(user.license_expires_at)
            limits = get_user_limits(user)
            self.assertTrue(limits["is_licensed"])
            self.assertFalse(limits["is_lifetime"])
            self.assertEqual(limits["remaining_days"], 30)

        # 3. Simulate expired license by setting expires_at to past
        with app.app_context():
            user = User.query.filter_by(username="morteza").first()
            user.license_expires_at = datetime.now() - timedelta(days=1)
            db.session.commit()

            limits = get_user_limits(user)
            self.assertFalse(limits["is_licensed"])
            self.assertTrue(limits["is_expired"])
            self.assertEqual(limits["remaining_days"], 0)

        # 4. Generate & activate Lifetime license
        key_life = generate_key("morteza", "PRO", "LIFE")
        self.assertTrue(key_life.startswith("LST-LIFE-"))

        res = self.client.post("/account/license", data={"license_key": key_life}, headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 200)

        with app.app_context():
            user = User.query.filter_by(username="morteza").first()
            self.assertTrue(user.is_licensed)
            self.assertIsNone(user.license_expires_at)
            limits = get_user_limits(user)
            self.assertTrue(limits["is_licensed"])
            self.assertTrue(limits["is_lifetime"])

    def test_dashboard_and_product_editing_with_supplier_change(self):
        self.register_and_login("smq2458", "adminpassword")

        # Add 2 suppliers
        res = self.client.post("/suppliers", data={"name": "تأمین کننده الف"}, headers={"X-Requested-With": "fetch"})
        s1_id = res.get_json()["id"]
        res = self.client.post("/suppliers", data={"name": "تأمین کننده ب"}, headers={"X-Requested-With": "fetch"})
        s2_id = res.get_json()["id"]

        # Add 1 product
        res = self.client.post("/new-purchase", data={
            "supplier": str(s1_id),
            "product": "کالای تستی",
            "quantity": "5",
            "unit": "عدد",
            "description": "توضیح اولیه"
        }, headers={"X-Requested-With": "fetch"})
        p_id = res.get_json()["product"]["id"]

        # Check Dashboard page
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn("داشبورد", html)
        self.assertIn("آخرین سفارش‌های فعال", html)
        self.assertIn("supplier-home-card", html)
        self.assertIn("تأمین کننده الف", html)
        self.assertIn('id="stat-active"', html)

        # Edit product: change name and change supplier to Supplier B
        res = self.client.post(f"/product/{p_id}/edit", data={
            "supplier_id": str(s2_id),
            "product": "کالای تستی منتقل شده",
            "quantity": "12",
            "unit": "کارتن",
            "description": "تغییر تأمین کننده"
        }, headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])

        with app.app_context():
            prod = db.session.get(Product, p_id)
            self.assertEqual(prod.supplier_id, s2_id)
            self.assertEqual(prod.product_name, "کالای تستی منتقل شده")

    def test_supplier_card_counters_include_previous_products(self):
        """کارت تأمین‌کننده باید محصولات قبلی را هم بشمارد، نه فقط ثبت‌های جدید."""
        self.register_and_login("smq2458", "adminpassword")

        res = self.client.post("/suppliers", data={"name": "تأمین کننده قدیمی"}, headers={"X-Requested-With": "fetch"})
        s_id = res.get_json()["id"]

        with app.app_context():
            user = User.query.filter_by(username="smq2458").first()
            # سه ردیف «قدیمی» مثل داده‌های نسخه‌های قبلی که ordered آن‌ها NULL است
            for i in range(3):
                db.session.add(Product(
                    owner_id=user.id,
                    supplier_id=s_id,
                    product_name=f"کالای قدیمی {i}",
                    quantity="2",
                    unit="عدد",
                    ordered=None,
                ))
            db.session.commit()

        res = self.client.get("/api/dashboard/stats", headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 200)
        stats = res.get_json()
        self.assertTrue(stats["success"])
        self.assertEqual(stats["active_count"], 3)
        self.assertEqual(stats["archived_count"], 0)
        card = next(item for item in stats["suppliers"] if item["id"] == s_id)
        self.assertEqual(card["active_count"], 3)

        # همان عدد باید در HTML داشبورد هم رندر شود (نه صفر)
        html = self.client.get("/").get_data(as_text=True)
        marker = html.split(f'data-supplier-id="{s_id}"', 1)[1]
        rendered = marker.split('data-count>', 1)[1].split("<", 1)[0]
        self.assertEqual(rendered.strip(), "3")
        self.assertIn("کالای قدیمی 0", html)

        # ثبت محصول جدید: شمارنده باید زنده جلو برود (۴ شود)
        self.client.post("/new-purchase", data={
            "supplier": str(s_id),
            "product": "کالای جدید",
            "quantity": "1",
            "unit": "عدد",
            "description": "",
        }, headers={"X-Requested-With": "fetch"})

        stats = self.client.get("/api/dashboard/stats", headers={"X-Requested-With": "fetch"}).get_json()
        card = next(item for item in stats["suppliers"] if item["id"] == s_id)
        self.assertEqual(card["active_count"], 4)
        self.assertEqual(stats["active_count"], 4)

        # ثبت سفارش یکی از محصولات قدیمی: شمارنده باید کم شود و به آرشیو برود
        with app.app_context():
            old_id = Product.query.filter_by(product_name="کالای قدیمی 0").first().id
        self.client.post(f"/toggle-order/{old_id}", headers={"X-Requested-With": "fetch"})

        stats = self.client.get("/api/dashboard/stats", headers={"X-Requested-With": "fetch"}).get_json()
        card = next(item for item in stats["suppliers"] if item["id"] == s_id)
        self.assertEqual(card["active_count"], 3)
        self.assertEqual(stats["active_count"], 3)
        self.assertEqual(stats["archived_count"], 1)

    def test_purchases_sorted_by_supplier_then_entry_order(self):
        """لیست خریدها: اول گروه تأمین‌کننده، بعد ترتیب ورود محصول‌ها."""
        self.register_and_login("smq2458", "adminpassword")

        ids = {}
        for name in ("ب تأمین دوم", "الف تأمین اول"):
            res = self.client.post("/suppliers", data={"name": name}, headers={"X-Requested-With": "fetch"})
            ids[name] = res.get_json()["id"]

        # ثبت به‌صورت ضربدری تا ترتیب ورود با ترتیب تأمین‌کننده یکی نباشد
        order = [
            ("ب تأمین دوم", "ب-۱"),
            ("الف تأمین اول", "الف-۱"),
            ("ب تأمین دوم", "ب-۲"),
            ("الف تأمین اول", "الف-۲"),
        ]
        for supplier_name, product in order:
            self.client.post("/new-purchase", data={
                "supplier": str(ids[supplier_name]),
                "product": product,
                "quantity": "1",
                "unit": "عدد",
                "description": "",
            }, headers={"X-Requested-With": "fetch"})

        html = self.client.get("/purchases").get_data(as_text=True)
        body = html.split('id="purchases-body"', 1)[1]
        positions = [(body.index(name), name) for _, name in order]
        self.assertEqual(
            [name for _, name in sorted(positions)],
            ["الف-۱", "الف-۲", "ب-۱", "ب-۲"],
        )
        # هر گروه یک ردیف شروع دارد
        rows_html = body.split("</table>", 1)[0]
        self.assertEqual(rows_html.count('class="supplier-group-start"'), 2)

    def test_admin_can_manage_users_and_licenses(self):
        """مدیر می‌تواند کاربر و لایسنس را ویرایش و حذف کند؛ کاربر عادی نه."""
        # یک کاربر عادی می‌سازیم
        self.register_and_login("mohammad", "123456")
        with app.app_context():
            target_id = User.query.filter_by(username="mohammad").first().id
        # کاربر عادی نباید به عملیات مدیریتی دسترسی داشته باشد
        res = self.client.post(f"/api/admin/users/{target_id}/update",
                               data={"username": "hacker"}, headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 403)
        # کاربر عادی نباید بتواند نام کاربری خودش را عوض کند
        res = self.client.post("/account/username",
                               data={"username": "newname", "current_password": "123456"},
                               headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 403)
        with app.app_context():
            self.assertIsNotNone(User.query.filter_by(username="mohammad").first())

        # حالا با حساب مدیر
        self.client.get("/logout")
        self.register_and_login("smq2458", "adminpassword")

        # ۱) ویرایش کاربر: نام کاربری و رمز عبور
        res = self.client.post(f"/api/admin/users/{target_id}/update",
                               data={"username": "mohammad-new", "new_password": "987654"},
                               headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["success"])
        with app.app_context():
            target = db.session.get(User, target_id)
            self.assertEqual(target.username, "mohammad-new")
            self.assertTrue(check_password_hash(target.password_hash, "987654"))

        # ۲) ویرایش لایسنس: ۶۰ روزه
        res = self.client.post(f"/api/admin/users/{target_id}/license",
                               data={"action": "grant", "tier": "PRO", "duration": "60D"},
                               headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 200)
        payload = res.get_json()
        self.assertTrue(payload["user"]["is_licensed"])
        self.assertFalse(payload["user"]["is_lifetime"])
        with app.app_context():
            target = db.session.get(User, target_id)
            self.assertTrue(target.is_licensed)
            self.assertIsNotNone(target.license_expires_at)
            # کلید ذخیره‌شده باید برای نام کاربری جدید معتبر باشد
            valid, _, _, _ = verify_key(target, target.license_key)
            self.assertTrue(valid)

        # ۳) حذف لایسنس
        res = self.client.post(f"/api/admin/users/{target_id}/license",
                               data={"action": "revoke"}, headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 200)
        with app.app_context():
            target = db.session.get(User, target_id)
            self.assertFalse(target.is_licensed)
            self.assertIsNone(target.license_key)
            self.assertEqual(target.license_type, "free")

        # ۴) حساب مدیر و حساب خود کاربر جاری قابل حذف نیست
        with app.app_context():
            admin_id = User.query.filter_by(username="smq2458").first().id
        res = self.client.post(f"/api/admin/users/{admin_id}/delete", headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 400)

        # ۵) حذف کامل کاربر به همراه داده‌هایش
        with app.app_context():
            supplier = Supplier(name="تأمین کننده کاربر", owner_id=target_id)
            db.session.add(supplier)
            db.session.commit()
            db.session.add(Product(owner_id=target_id, supplier_id=supplier.id,
                                   product_name="کالای کاربر", quantity="1", unit="عدد"))
            db.session.commit()

        res = self.client.post(f"/api/admin/users/{target_id}/delete", headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 200)
        with app.app_context():
            self.assertIsNone(db.session.get(User, target_id))
            self.assertEqual(Supplier.query.filter_by(owner_id=target_id).count(), 0)
            self.assertEqual(Product.query.filter_by(owner_id=target_id).count(), 0)

    def test_signup_requires_profile_and_normalizes_phone(self):
        """ثبت‌نام باید نام/نام خانوادگی/موبایل بخواهد و ارقام فارسی را نرمال کند."""
        # بدون مشخصات → خطا
        res = self.client.post("/signup", data={"username": "nouser", "password": "123456"},
                               follow_redirects=True)
        self.assertIn("نام و نام خانوادگی", res.get_data(as_text=True))
        with app.app_context():
            self.assertIsNone(User.query.filter_by(username="nouser").first())

        # موبایل نامعتبر → خطا
        res = self.client.post("/signup", data={
            "username": "nouser", "password": "123456",
            "first_name": "الهه", "last_name": "رضایی", "phone": "12345",
        }, follow_redirects=True)
        self.assertIn("موبایل", res.get_data(as_text=True))

        # رمز کوتاه → خطا
        res = self.client.post("/signup", data={
            "username": "nouser", "password": "12345",
            "first_name": "الهه", "last_name": "رضایی", "phone": "09123456789",
        }, follow_redirects=True)
        self.assertIn("حداقل ۶", res.get_data(as_text=True))

        # ثبت‌نام موفق با ارقام فارسی → نرمال‌سازی به 09xxxxxxxxx
        res = self.client.post("/signup", data={
            "username": "elahe", "password": "123456",
            "first_name": "الهه", "last_name": "رضایی", "phone": "۰۹۱۲۳۴۵۶۷۸۹",
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        with app.app_context():
            user = User.query.filter_by(username="elahe").first()
            self.assertIsNotNone(user)
            self.assertEqual(user.first_name, "الهه")
            self.assertEqual(user.last_name, "رضایی")
            self.assertEqual(user.phone, "09123456789")

    def test_username_uniqueness_is_case_insensitive(self):
        """ثبت نسخه بزرگ‌حرف از نام کاربری موجود (مثل SMQ2458) نباید حساب جدید بسازد."""
        self.register_and_login("smq2458", "adminpassword")
        self.client.get("/logout")

        res = self.client.post("/signup", data={
            "username": "SMQ2458", "password": "123456",
            "first_name": "مهاجم", "last_name": "فرضی", "phone": "09121112222",
        }, follow_redirects=True)
        self.assertIn("قبلاً وجود دارد", res.get_data(as_text=True))
        with app.app_context():
            self.assertEqual(
                User.query.filter(db.func.lower(User.username) == "smq2458").count(), 1
            )

    def test_admin_can_edit_user_profile(self):
        """مدیر می‌تواند نام، نام خانوادگی و موبایل کاربر را ویرایش کند."""
        self.register_and_login("mohammad", "123456")
        with app.app_context():
            target_id = User.query.filter_by(username="mohammad").first().id

        self.client.get("/logout")
        self.register_and_login("smq2458", "adminpassword")

        res = self.client.post(f"/api/admin/users/{target_id}/update",
                               data={
                                   "username": "mohammad",
                                   "first_name": "محمد",
                                   "last_name": "محمدی",
                                   "phone": "09129876543",
                               }, headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["user"]["full_name"], "محمد محمدی")
        self.assertEqual(data["user"]["phone"], "09129876543")
        with app.app_context():
            target = db.session.get(User, target_id)
            self.assertEqual(target.first_name, "محمد")
            self.assertEqual(target.phone, "09129876543")

        # موبایل نامعتبر نباید ذخیره شود
        res = self.client.post(f"/api/admin/users/{target_id}/update",
                               data={"username": "mohammad", "first_name": "محمد",
                                     "last_name": "محمدی", "phone": "0000"},
                               headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 400)
        with app.app_context():
            self.assertEqual(db.session.get(User, target_id).phone, "09129876543")

        # نام رزروشده مدیر به کاربر دیگر داده نمی‌شود
        res = self.client.post(f"/api/admin/users/{target_id}/update",
                               data={"username": "smq2458"},
                               headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 400)
        with app.app_context():
            self.assertEqual(db.session.get(User, target_id).username, "mohammad")

    def test_user_can_edit_own_profile(self):
        """کاربر باید بتواند مشخصات خودش را از صفحه حساب ویرایش کند."""
        self.register_and_login("reza", "123456")
        res = self.client.post("/account/profile", data={
            "first_name": "رضا", "last_name": "کریمی", "phone": "09351234567",
        }, headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["success"])
        with app.app_context():
            user = User.query.filter_by(username="reza").first()
            self.assertEqual(user.last_name, "کریمی")
            self.assertEqual(user.phone, "09351234567")

        res = self.client.post("/account/profile", data={
            "first_name": "رضا", "last_name": "کریمی", "phone": "123",
        }, headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 400)

    def test_product_delete_endpoint(self):
        """حذف محصول (رفع باگ «حذف انجام نشد») باید موفق برگردد."""
        self.register_and_login("smq2458", "adminpassword")
        res = self.client.post("/suppliers", data={"name": "تأمین الف"},
                               headers={"X-Requested-With": "fetch"})
        s_id = res.get_json()["id"]
        res = self.client.post("/new-purchase", data={
            "supplier": str(s_id), "product": "کالای حذف‌شدنی",
            "quantity": "1", "unit": "عدد", "description": "",
        }, headers={"X-Requested-With": "fetch"})
        p_id = res.get_json()["product"]["id"]

        res = self.client.post(f"/product/{p_id}/delete",
                               headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["success"])
        with app.app_context():
            self.assertIsNone(db.session.get(Product, p_id))

        # حذف دوباره همان محصول → 404 JSON
        res = self.client.post(f"/product/{p_id}/delete",
                               headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 404)
        self.assertFalse(res.get_json()["success"])

    def test_normalize_phone_helper(self):
        self.assertEqual(normalize_phone("09123456789"), "09123456789")
        self.assertEqual(normalize_phone("+989123456789"), "09123456789")
        self.assertEqual(normalize_phone("9123456789"), "09123456789")
        self.assertEqual(normalize_phone("۰۹۱۲۳۴۵۶۷۸۹"), "09123456789")
        self.assertEqual(normalize_phone(""), "")
        self.assertIsNone(normalize_phone("123"))
        self.assertIsNone(normalize_phone("08123456789"))

    def test_api_dashboard_stats_includes_quota(self):
        """وضعیت لایسنس باید برای شمارنده‌های زنده سهمیه قابل خواندن باشد."""
        self.register_and_login("neda", "123456")
        res = self.client.get("/api/license-status", headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 200)
        limits = res.get_json()["limits"]
        self.assertEqual(limits["supplier_count"], 0)
        self.assertEqual(limits["product_count"], 0)
        self.assertFalse(limits["is_licensed"])
        self.assertTrue(limits["can_add_product"])

    def test_unauthenticated_api_returns_json_401(self):
        """درخواست fetch بدون لاگین باید 401 JSON بگیرد، نه ریدایرکت HTML."""
        res = self.client.post("/new-purchase", data={"product": "x"},
                               headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 401)
        self.assertFalse(res.get_json()["success"])

        res = self.client.get("/api/dashboard/stats",
                              headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 401)

    def test_smq2458_admin_and_license_generator(self):
        self.register_and_login("smq2458", "adminpassword")

        with app.app_context():
            user = User.query.filter_by(username="smq2458").first()
            self.assertIsNotNone(user)
            self.assertTrue(user.is_licensed)
            self.assertTrue(user.is_admin)
            self.assertTrue(is_admin_user(user))

        # Access Admin License Generator page
        res = self.client.get("/admin/license-generator")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn("پنل صدور لایسنس", html)

        # Generate 90-day license for customer 'reza'
        res = self.client.post("/api/admin/generate-license", data={
            "identifier": "reza",
            "tier": "PRO",
            "duration": "90D",
            "is_master": "0"
        }, headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertTrue(data["license_key"].startswith("LST-90D-"))


if __name__ == "__main__":
    unittest.main()
