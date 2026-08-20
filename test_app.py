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

    def test_smq2458_admin_and_license_generator(self):
        # 1. Register or login as smq2458
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
            self.assertTrue(limits["can_add_supplier"])
            self.assertTrue(limits["can_add_product"])
            self.assertIsNone(limits["max_suppliers"])
            self.assertIsNone(limits["max_products"])

        # 2. Access Admin License Generator page
        res = self.client.get("/admin/license-generator")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn("پنل صدور لایسنس", html)
        self.assertIn("س.م.قتالی", html)

        # 3. Generate license for customer 'reza'
        res = self.client.post("/api/admin/generate-license", data={
            "identifier": "reza",
            "tier": "PRO",
            "is_master": "0"
        }, headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertTrue(data["license_key"].startswith("LST-"))
        self.assertIn("reza", data["identifier"])
        self.assertIn("س.م.قتالی", data["customer_message"])

        # 4. Generate universal master key
        res = self.client.post("/api/admin/generate-license", data={
            "is_master": "1",
            "tier": "UNLIMITED"
        }, headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertTrue(data["license_key"].startswith("LST-"))

        # 5. Normal user should NOT have access to license generator
        self.client.get("/logout")
        self.register_and_login("customer1", "pass1234")
        res = self.client.get("/admin/license-generator")
        self.assertEqual(res.status_code, 403)
        res = self.client.post("/api/admin/generate-license", data={"identifier": "test"}, headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 403)

    def test_signup_and_trial_limits(self):
        self.register_and_login("ali", "123456")

        with app.app_context():
            user = User.query.filter_by(username="ali").first()
            self.assertIsNotNone(user)
            self.assertFalse(user.is_licensed)
            limits = get_user_limits(user)
            self.assertFalse(limits["is_licensed"])
            self.assertEqual(limits["max_suppliers"], 1)
            self.assertEqual(limits["max_products"], 5)
            self.assertTrue(limits["can_add_supplier"])
            self.assertTrue(limits["can_add_product"])

        # 1. Add 1st supplier -> should succeed
        res = self.client.post("/suppliers", data={"name": "تأمین کننده ۱"}, headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        s1_id = data["id"]

        # 2. Try to add 2nd supplier -> should be rejected (limit is 1)
        res = self.client.post("/suppliers", data={"name": "تأمین کننده ۲"}, headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 403)
        data = res.get_json()
        self.assertFalse(data["success"])
        self.assertIn("نسخه آزمایشی", data["message"])

        # 3. Add 5 products under supplier 1 -> should succeed
        for i in range(1, 6):
            res = self.client.post("/new-purchase", data={
                "supplier": str(s1_id),
                "product": f"کالا {i}",
                "quantity": str(i),
                "unit": "عدد",
                "description": f"توضیح {i}"
            }, headers={"X-Requested-With": "fetch"})
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertTrue(data["success"])

        # 4. Try to add 6th product -> should be rejected (limit is 5)
        res = self.client.post("/new-purchase", data={
            "supplier": str(s1_id),
            "product": "کالا ۶",
            "quantity": "6",
            "unit": "عدد",
            "description": "اضافی"
        }, headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 403)
        data = res.get_json()
        self.assertFalse(data["success"])
        self.assertIn("نسخه آزمایشی", data["message"])

        # 5. Verify existing 5 products and 1 supplier can still be used and edited
        with app.app_context():
            user = User.query.filter_by(username="ali").first()
            p1 = Product.query.filter_by(owner_id=user.id, product_name="کالا 1").first()
            self.assertIsNotNone(p1)
            p1_id = p1.id

        # Edit product 1
        res = self.client.post(f"/product/{p1_id}/edit", data={
            "product": "کالا ۱ ویرایش شده",
            "quantity": "20",
            "unit": "کارتن",
            "description": "تغییر یافت"
        }, headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])

        # Toggle order (archive)
        res = self.client.post(f"/toggle-order/{p1_id}", headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 200)

        # Unarchive
        res = self.client.post(f"/unarchive/{p1_id}", headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 200)

        # Edit supplier 1
        res = self.client.post(f"/supplier/{s1_id}/edit", data={"name": "تأمین کننده ۱ تغییر یافته"}, headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 200)

        # 6. Test License Activation:
        # Invalid key:
        res = self.client.post("/account/license", data={"license_key": "INVALID-KEY-1234"}, headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 400)
        self.assertFalse(res.get_json()["success"])

        # Valid key:
        valid_key = generate_key("ali", "PRO")
        res = self.client.post("/account/license", data={"license_key": valid_key}, headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["success"])

        # Check that user is now licensed
        with app.app_context():
            user = User.query.filter_by(username="ali").first()
            self.assertTrue(user.is_licensed)
            limits = get_user_limits(user)
            self.assertTrue(limits["is_licensed"])
            self.assertIsNone(limits["max_suppliers"])
            self.assertIsNone(limits["max_products"])
            self.assertTrue(limits["can_add_supplier"])
            self.assertTrue(limits["can_add_product"])

        # 7. Add 2nd supplier now -> should SUCCEED
        res = self.client.post("/suppliers", data={"name": "تأمین کننده ۲"}, headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        s2_id = data["id"]

        # 8. Add 6th, 7th product now -> should SUCCEED
        res = self.client.post("/new-purchase", data={
            "supplier": str(s2_id),
            "product": "کالا ۶ جدید",
            "quantity": "10",
            "unit": "کیلو",
            "description": "بدون محدودیت"
        }, headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["success"])

    def test_master_key_activation(self):
        self.register_and_login("reza", "123456")
        master_key = generate_master_key()
        res = self.client.post("/account/license", data={"license_key": master_key}, headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["success"])

        with app.app_context():
            user = User.query.filter_by(username="reza").first()
            self.assertTrue(user.is_licensed)

    def test_user_code_activation(self):
        self.register_and_login("sara", "123456")
        with app.app_context():
            user = User.query.filter_by(username="sara").first()
            ucode = get_user_code(user)
        
        key = generate_key(ucode, "PRO")
        res = self.client.post("/account/license", data={"license_key": key}, headers={"X-Requested-With": "fetch"})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()["success"])

    def test_api_quick_add_license_limits(self):
        self.register_and_login("apitest", "123456")
        with app.app_context():
            user = User.query.filter_by(username="apitest").first()
            token = issue_api_token(user)

        # 1. Quick add 1 product and create supplier -> success
        res = self.client.post("/api/quick-add", json={
            "product": "محصول API 1",
            "quantity": "1",
            "unit": "عدد",
            "supplier": "تامین کننده API"
        }, headers={"X-API-Key": token})
        self.assertEqual(res.status_code, 200)

        # 2. Try to create 2nd supplier via API on trial -> should fail (403)
        res = self.client.post("/api/quick-add", json={
            "product": "محصول API 2",
            "quantity": "1",
            "unit": "عدد",
            "supplier": "تامین کننده دوم API"
        }, headers={"X-API-Key": token})
        self.assertEqual(res.status_code, 403)

    def test_branding_and_developer_credit(self):
        self.register_and_login("admin", "pass123")
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn("لیستیا", html)
        self.assertIn("س.م.قتالی", html)

        res = self.client.get("/account")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn("لیستیا", html)
        self.assertIn("س.م.قتالی", html)
        self.assertIn("مدیریت لایسنس", html)

        res = self.client.get("/suppliers")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn("لیستیا", html)

        self.client.get("/logout")
        res = self.client.get("/login")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn("لیستیا", html)
        self.assertIn("س.م.قتالی", html)


if __name__ == "__main__":
    unittest.main()
