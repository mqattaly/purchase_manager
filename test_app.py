import io
import unittest
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
        self.assertIn("کالای تستی", html)

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
            self.assertEqual(prod.quantity, "12")
            self.assertEqual(prod.unit, "کارتن")

        # Toggle order (archive) from dashboard action
        res = self.client.post(f"/toggle-order/{p_id}", headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 200)
        with app.app_context():
            prod = db.session.get(Product, p_id)
            self.assertTrue(prod.ordered)

        # Delete product
        res = self.client.post(f"/product/{p_id}/delete", headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 200)
        with app.app_context():
            prod = db.session.get(Product, p_id)
            self.assertIsNone(prod)

    def test_smq2458_admin_and_license_generator(self):
        self.register_and_login("smq2458", "adminpassword")

        with app.app_context():
            user = User.query.filter_by(username="smq2458").first()
            self.assertIsNotNone(user)
            self.assertTrue(user.is_licensed)
            self.assertTrue(user.is_admin)
            self.assertTrue(is_admin_user(user))
            limits = get_user_limits(user)
            self.assertTrue(limits["is_licensed"])
            self.assertTrue(limits["is_admin"])

        # Access Admin License Generator page
        res = self.client.get("/admin/license-generator")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn("پنل صدور لایسنس", html)
        self.assertIn("س.م.قتالی", html)

        # Generate license for customer 'reza'
        res = self.client.post("/api/admin/generate-license", data={
            "identifier": "reza",
            "tier": "PRO",
            "is_master": "0"
        }, headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertTrue(data["license_key"].startswith("LST-"))

        # Non-admin access denied
        self.client.get("/logout")
        self.register_and_login("customer1", "pass1234")
        res = self.client.get("/admin/license-generator")
        self.assertEqual(res.status_code, 403)

    def test_signup_and_trial_limits(self):
        self.register_and_login("ali", "123456")

        # 1. Add 1st supplier -> should succeed
        res = self.client.post("/suppliers", data={"name": "تأمین کننده ۱"}, headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 200)
        s1_id = res.get_json()["id"]

        # 2. Try to add 2nd supplier -> rejected
        res = self.client.post("/suppliers", data={"name": "تأمین کننده ۲"}, headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 403)

        # 3. Add 5 products
        for i in range(1, 6):
            res = self.client.post("/new-purchase", data={
                "supplier": str(s1_id),
                "product": f"کالا {i}",
                "quantity": str(i),
                "unit": "عدد",
                "description": f"توضیح {i}"
            }, headers={"X-Requested-With": "fetch"})
            self.assertEqual(res.status_code, 200)

        # 4. Try 6th product -> rejected
        res = self.client.post("/new-purchase", data={
            "supplier": str(s1_id),
            "product": "کالا ۶",
            "quantity": "6",
            "unit": "عدد"
        }, headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 403)

        # 5. Activate license
        valid_key = generate_key("ali", "PRO")
        res = self.client.post("/account/license", data={"license_key": valid_key}, headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 200)

        # 6. Add 2nd supplier and 6th product now -> succeed
        res = self.client.post("/suppliers", data={"name": "تأمین کننده ۲"}, headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 200)


if __name__ == "__main__":
    unittest.main()
