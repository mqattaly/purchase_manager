import io
import unittest
from datetime import datetime, timedelta
import pandas as pd
from app import app, db, User, Supplier, Product, get_user_limits, issue_api_token, is_admin_user
from licensing import get_user_code, generate_key, generate_master_key, verify_key


class ListiaTestCase(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
        self.client = app.test_client()
        with app.app_context():
            db.create_all()

    def tearDown(self):
        with app.app_context():
            db.session.remove()
            db.drop_all()

    def register_and_login(self, username="testuser", password="password123"):
        res = self.client.post("/signup", data={"username": username, "password": password}, follow_redirects=True)
        self.assertEqual(res.status_code, 200)
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
