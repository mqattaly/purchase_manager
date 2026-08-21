import os
import re
import unittest
from datetime import UTC, datetime, timedelta

# Tests must never connect to the deployed PostgreSQL database or mutate the
# tracked local sample database.
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from sqlalchemy import event
from werkzeug.security import check_password_hash, generate_password_hash

from app import (
    Product,
    Supplier,
    User,
    app,
    db,
    get_user_limits,
    is_admin_user,
    issue_api_token,
)
from licensing import generate_key, verify_key


class ListiaTestCase(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        app.config["CSRF_PROTECT"] = False
        app.config["RATE_LIMITING"] = False
        app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
        self.client = app.test_client()
        with app.app_context():
            db.create_all()

    def tearDown(self):
        with app.app_context():
            db.session.remove()
            db.drop_all()

    def register_and_login(
        self,
        username="testuser",
        password="password123",
        first_name="کاربر",
        last_name="آزمایشی",
        mobile=None,
    ):
        if mobile is None:
            suffix = sum((index + 1) * ord(char) for index, char in enumerate(username)) % 1_000_000_000
            mobile = "09" + f"{suffix:09d}"
        res = self.client.post(
            "/signup",
            data={
                "first_name": first_name,
                "last_name": last_name,
                "mobile": mobile,
                "username": username,
                "password": password,
            },
            follow_redirects=True,
        )
        self.assertEqual(res.status_code, 200)
        return res

    def create_admin_and_login(self):
        with app.app_context():
            admin = User.query.filter_by(username="smq2458").first()
            if not admin:
                admin = User(
                    username="smq2458",
                    password_hash=generate_password_hash("adminpassword"),
                    first_name="مدیر",
                    last_name="سیستم",
                    mobile="09120000000",
                    is_admin=True,
                    is_licensed=True,
                    license_type="UNLIMITED",
                )
                db.session.add(admin)
                db.session.commit()
        response = self.client.post(
            "/login",
            data={"username": "smq2458", "password": "adminpassword"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        return response

    @staticmethod
    def csrf_from(response):
        match = re.search(r'name="csrf-token" content="([^"]+)"', response.get_data(as_text=True))
        if not match:
            match = re.search(r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True))
        if not match:
            raise AssertionError("CSRF token was not rendered")
        return match.group(1)

    def test_signup_saves_profile_and_validates_mobile(self):
        page = self.client.get("/signup")
        html = page.get_data(as_text=True)
        self.assertIn('name="first_name"', html)
        self.assertIn('name="last_name"', html)
        self.assertIn('name="mobile"', html)

        invalid = self.client.post(
            "/signup",
            data={
                "first_name": "علی",
                "last_name": "رضایی",
                "mobile": "12345",
                "username": "ali",
                "password": "12345678",
            },
        )
        self.assertIn("شماره موبایل معتبر نیست", invalid.get_data(as_text=True))
        with app.app_context():
            self.assertIsNone(User.query.filter_by(username="ali").first())

        self.register_and_login(
            "ali",
            "12345678",
            first_name=" علی ",
            last_name=" رضايي ",
            mobile="+۹۸ ۹۱۲ ۱۲۳ ۴۵۶۷",
        )
        with app.app_context():
            user = User.query.filter_by(username="ali").first()
            self.assertEqual(user.first_name, "علی")
            self.assertEqual(user.last_name, "رضایی")
            self.assertEqual(user.mobile, "09121234567")

    def test_time_limited_and_lifetime_licenses(self):
        self.register_and_login("morteza", "12345678")

        # 1. Generate 30-day license for morteza
        key_30d = generate_key("morteza", "PRO", "30D")
        self.assertTrue(key_30d.startswith("LST-30D-"))

        # 2. Activate 30-day license
        res = self.client.post("/account/license", data={"license_key": key_30d}, headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertIn("30 روز", data["message"])

        replay = self.client.post(
            "/account/license",
            data={"license_key": key_30d},
            headers={"X-Requested-With": "fetch"},
        )
        self.assertEqual(replay.status_code, 409)
        self.assertIn("قبلاً", replay.get_json()["message"])

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
            user.license_expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1)
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
        self.create_admin_and_login()

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

    def test_trial_sidebar_quota_and_new_product_delete(self):
        """Trial counts come from the live API and a just-saved row is deletable."""
        self.register_and_login("trial-user", "12345678")
        supplier_res = self.client.post(
            "/suppliers",
            data={"name": "تأمین آزمایشی"},
            headers={"X-Requested-With": "fetch"},
        )
        supplier_id = supplier_res.get_json()["id"]
        product_res = self.client.post(
            "/new-purchase",
            data={
                "supplier": str(supplier_id),
                "product": "محصول تازه",
                "quantity": "1",
                "unit": "عدد",
                "description": "",
            },
            headers={"X-Requested-With": "fetch"},
        )
        product_id = product_res.get_json()["product"]["id"]

        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('id="sidebar-supplier-count" data-quota-kind="supplier">1</b>/1', html)
        self.assertIn('id="sidebar-product-count" data-quota-kind="product">1</b>/5', html)
        self.assertIn('class="mobile-quota-bar"', html)
        self.assertNotIn('class="quota-pill', html)

        stats = self.client.get("/api/dashboard/stats").get_json()
        self.assertEqual(stats["supplier_count"], 1)
        self.assertEqual(stats["product_count"], 1)
        self.assertTrue(stats["can_add_product"])
        self.assertFalse(stats["can_add_supplier"])
        blocked_supplier = self.client.post(
            "/suppliers",
            data={"name": "تأمین دوم"},
            headers={"X-Requested-With": "fetch"},
        )
        self.assertEqual(blocked_supplier.status_code, 403)
        self.assertTrue(blocked_supplier.get_json()["license_locked"])

        for number in range(2, 6):
            added = self.client.post(
                "/new-purchase",
                data={
                    "supplier": str(supplier_id),
                    "product": f"محصول {number}",
                    "quantity": "1",
                    "unit": "عدد",
                },
                headers={"X-Requested-With": "fetch"},
            )
            self.assertEqual(added.status_code, 200)

        blocked = self.client.post(
            "/new-purchase",
            data={
                "supplier": str(supplier_id),
                "product": "محصول ششم",
                "quantity": "1",
                "unit": "عدد",
            },
            headers={"X-Requested-With": "fetch"},
        )
        self.assertEqual(blocked.status_code, 403)
        self.assertTrue(blocked.get_json()["license_locked"])
        stats = self.client.get("/api/dashboard/stats").get_json()
        self.assertEqual(stats["product_count"], 5)
        self.assertFalse(stats["can_add_product"])

        deleted = self.client.post(
            f"/product/{product_id}/delete",
            headers={"X-Requested-With": "fetch"},
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertTrue(deleted.get_json()["success"])
        with app.app_context():
            self.assertIsNone(db.session.get(Product, product_id))

        stats = self.client.get("/api/dashboard/stats").get_json()
        self.assertEqual(stats["product_count"], 4)
        self.assertTrue(stats["can_add_product"])

    def test_supplier_card_counters_include_previous_products(self):
        """کارت تأمین‌کننده باید محصولات قبلی را هم بشمارد، نه فقط ثبت‌های جدید."""
        self.create_admin_and_login()

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
        self.create_admin_and_login()

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
        self.register_and_login("mohammad", "12345678")
        with app.app_context():
            target_id = User.query.filter_by(username="mohammad").first().id
        # کاربر عادی نباید به عملیات مدیریتی دسترسی داشته باشد
        res = self.client.post(f"/api/admin/users/{target_id}/update",
                               data={"username": "hacker"}, headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 403)
        # کاربر عادی نباید بتواند نام کاربری خودش را عوض کند
        res = self.client.post("/account/username",
                               data={"username": "newname", "current_password": "12345678"},
                               headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 403)
        with app.app_context():
            self.assertIsNotNone(User.query.filter_by(username="mohammad").first())

        # حالا با حساب مدیر
        self.client.post("/logout")
        self.create_admin_and_login()

        # ۱) ویرایش کاربر: مشخصات، نام کاربری و رمز عبور
        res = self.client.post(f"/api/admin/users/{target_id}/update",
                               data={
                                   "first_name": "محمد",
                                   "last_name": "محمدی",
                                   "mobile": "۰۹۱۲ ۱۱۱ ۲۲۳۳",
                                   "username": "mohammad-new",
                                   "new_password": "98765432",
                               },
                               headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["success"])
        with app.app_context():
            target = db.session.get(User, target_id)
            self.assertEqual(target.username, "mohammad-new")
            self.assertEqual(target.first_name, "محمد")
            self.assertEqual(target.last_name, "محمدی")
            self.assertEqual(target.mobile, "09121112233")
            self.assertTrue(check_password_hash(target.password_hash, "98765432"))

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

    def test_smq2458_admin_and_license_generator(self):
        self.create_admin_and_login()

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
        self.assertIn("نام و نام خانوادگی", html)
        self.assertIn("شماره موبایل", html)
        self.assertIn('id="edit-first-name"', html)
        self.assertIn('id="edit-last-name"', html)
        self.assertIn('id="edit-mobile"', html)

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

    def test_csrf_reserved_admin_and_secure_api_key_transport(self):
        app.config["CSRF_PROTECT"] = True
        try:
            signup_page = self.client.get("/signup")
            guest_token = self.csrf_from(signup_page)

            missing_token = self.client.post(
                "/signup",
                data={
                    "first_name": "کاربر",
                    "last_name": "امن",
                    "mobile": "09123334444",
                    "username": "secure-user",
                    "password": "securepass123",
                },
            )
            self.assertEqual(missing_token.status_code, 403)

            reserved = self.client.post(
                "/signup",
                data={
                    "csrf_token": guest_token,
                    "first_name": "مهاجم",
                    "last_name": "آزمایشی",
                    "mobile": "09123334445",
                    "username": "SMQ2458",
                    "password": "securepass123",
                },
            )
            self.assertEqual(reserved.status_code, 403)
            with app.app_context():
                self.assertIsNone(User.query.filter_by(username="smq2458").first())

            signup = self.client.post(
                "/signup",
                data={
                    "csrf_token": guest_token,
                    "first_name": "کاربر",
                    "last_name": "امن",
                    "mobile": "09123334444",
                    "username": "secure-user",
                    "password": "securepass123",
                },
            )
            self.assertEqual(signup.status_code, 302)
            cookies = "\n".join(signup.headers.getlist("Set-Cookie"))
            self.assertIn("HttpOnly", cookies)
            self.assertIn("SameSite=Lax", cookies)

            suppliers_page = self.client.get("/suppliers")
            user_token = self.csrf_from(suppliers_page)
            blocked = self.client.post(
                "/suppliers",
                data={"name": "امن"},
                headers={"X-Requested-With": "fetch"},
            )
            self.assertEqual(blocked.status_code, 403)
            created = self.client.post(
                "/suppliers",
                data={"name": "امن"},
                headers={
                    "X-Requested-With": "fetch",
                    "X-CSRF-Token": user_token,
                },
            )
            self.assertEqual(created.status_code, 200)

            with app.app_context():
                user = User.query.filter_by(username="secure-user").first()
                api_key = issue_api_token(user)

            logged_out = self.client.post(
                "/logout", data={"csrf_token": user_token}
            )
            self.assertEqual(logged_out.status_code, 302)

            leaked_query_key = self.client.post(
                f"/api/quick-add?key={api_key}",
                data={"product": "کالای ناامن", "supplier": "امن"},
            )
            self.assertEqual(leaked_query_key.status_code, 403)

            quick_add = self.client.post(
                "/api/quick-add",
                json={
                    "product": "کالای امن",
                    "quantity": "۲",
                    "unit": "عدد",
                    "supplier": "امن",
                },
                headers={"X-API-Key": api_key},
            )
            self.assertEqual(quick_add.status_code, 200)
            self.assertEqual(quick_add.get_json()["product"]["quantity"], "2")
            self.assertEqual(
                self.client.get(
                    "/api/quick-add", headers={"X-API-Key": api_key}
                ).status_code,
                405,
            )
        finally:
            app.config["CSRF_PROTECT"] = False

    def test_cross_account_writes_are_not_authorized(self):
        self.register_and_login("owner-one", "password-one")
        supplier = self.client.post(
            "/suppliers",
            data={"name": "مالک اول"},
            headers={"X-Requested-With": "fetch"},
        ).get_json()
        product = self.client.post(
            "/new-purchase",
            data={
                "supplier": supplier["id"],
                "product": "خصوصی",
                "quantity": "1",
                "unit": "عدد",
            },
            headers={"X-Requested-With": "fetch"},
        ).get_json()["product"]
        self.client.post("/logout")
        self.register_and_login("owner-two", "password-two")

        self.assertEqual(
            self.client.post(
                f"/product/{product['id']}/delete",
                headers={"X-Requested-With": "fetch"},
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                f"/supplier/{supplier['id']}/delete",
                headers={"X-Requested-With": "fetch"},
            ).status_code,
            404,
        )
        with app.app_context():
            self.assertIsNotNone(db.session.get(Product, product["id"]))
            self.assertIsNotNone(db.session.get(Supplier, supplier["id"]))

    def test_password_change_revokes_other_browser_sessions(self):
        self.register_and_login("sessions-user", "old-password")
        second_browser = app.test_client()
        login = second_browser.post(
            "/login",
            data={"username": "sessions-user", "password": "old-password"},
        )
        self.assertEqual(login.status_code, 302)

        changed = self.client.post(
            "/account/password",
            data={
                "current_password": "old-password",
                "new_password": "new-password",
                "confirm_password": "new-password",
            },
            headers={"X-Requested-With": "fetch"},
        )
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(self.client.get("/account").status_code, 200)
        stale = second_browser.get("/account")
        self.assertEqual(stale.status_code, 302)
        self.assertTrue(stale.headers["Location"].endswith("/login"))

    def test_external_lengths_quantities_and_profile_are_validated(self):
        self.create_admin_and_login()
        too_long_supplier = self.client.post(
            "/suppliers",
            data={"name": "س" * 201},
            headers={"X-Requested-With": "fetch"},
        )
        self.assertEqual(too_long_supplier.status_code, 400)

        supplier_id = self.client.post(
            "/suppliers",
            data={"name": "تأمین معتبر"},
            headers={"X-Requested-With": "fetch"},
        ).get_json()["id"]
        for product_name, quantity in (("ک" * 301, "1"), ("نامعتبر", "0"), ("اعشار", "1.2345")):
            response = self.client.post(
                "/new-purchase",
                data={
                    "supplier": supplier_id,
                    "product": product_name,
                    "quantity": quantity,
                    "unit": "عدد",
                },
                headers={"X-Requested-With": "fetch"},
            )
            self.assertEqual(response.status_code, 400)

        profile = self.client.post(
            "/account/profile",
            data={
                "first_name": " مدیر ",
                "last_name": " امن ",
                "mobile": "+۹۸ ۹۱۲ ۳۴۵ ۶۷۸۹",
            },
            headers={"X-Requested-With": "fetch"},
        )
        self.assertEqual(profile.status_code, 200)
        self.assertEqual(profile.get_json()["profile"]["mobile"], "09123456789")

    def test_admin_user_page_uses_aggregate_count_queries(self):
        self.create_admin_and_login()
        with app.app_context():
            for index in range(8):
                user = User(
                    username=f"aggregate-{index}",
                    password_hash=generate_password_hash("password123"),
                    is_admin=False,
                    is_licensed=False,
                )
                db.session.add(user)
            db.session.commit()
            engine = db.engine

        statements = []

        def count_statement(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", count_statement)
        try:
            response = self.client.get("/admin/license-generator")
        finally:
            event.remove(engine, "before_cursor_execute", count_statement)
        self.assertEqual(response.status_code, 200)
        count_queries = [
            statement
            for statement in statements
            if "count(" in statement.lower()
        ]
        self.assertLessEqual(len(count_queries), 4)


if __name__ == "__main__":
    unittest.main()
