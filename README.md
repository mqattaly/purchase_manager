# لیستیا

وب‌اپ Flask برای مدیریت تأمین‌کننده‌ها، فهرست خرید و آرشیو سفارش‌ها.

## اجرای محلی

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# برای اجرای محلی بدون HTTPS در .env قرار دهید: COOKIE_SECURE=false
python app.py
```

پایگاه داده و کلیدهای محلی داخل `instance/` ساخته می‌شوند و در Git قرار نمی‌گیرند.

## تنظیمات ضروری Chabokan

پیش از استقرار، متغیرهای زیر را در پنل سرویس تعریف کنید:

- `DATABASE_URL`: نشانی PostgreSQL سرویس
- `SECRET_KEY`: مقدار تصادفی و ثابت با حداقل ۳۲ کاراکتر برای نشست‌ها و CSRF
- `LICENSE_SECRET`: مقدار تصادفی **متفاوت و ثابت** برای صدور لایسنس
- `COOKIE_SECURE=true`
- `CROSS_SITE_COOKIES=false`
- `FRAME_ANCESTORS='self'`

برای ساخت هر کلید امن:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

`SECRET_KEY` یا `LICENSE_SECRET` را پس از شروع سرویس بی‌دلیل عوض نکنید. تغییر اول همه نشست‌ها را می‌بندد؛ تغییر دوم کلیدهای لایسنس صادرشده ولی هنوز فعال‌نشده را نامعتبر می‌کند. وضعیت لایسنس‌های فعال در PostgreSQL حفظ می‌شود.

اگر این متغیرها تعریف نشده باشند، برنامه برای PostgreSQL از مشخصات محرمانه و ثابت `DATABASE_URL` کلیدهای جداگانه مشتق می‌کند تا fallback عمومی وجود نداشته باشد؛ بااین‌حال تعریف صریح دو کلید بالا روش توصیه‌شده است.

## حساب مدیر اصلی

در پایگاه داده‌های قبلی، مهاجرت خودکار فقط حساب موجود `smq2458` را مدیر و نامحدود نگه می‌دارد. ثبت‌نام عمومی نام‌های رزروشده را نمی‌پذیرد. برای ساخت امن مدیر در نصب تازه:

```bash
python create_user.py smq2458 --admin --first-name "نام" --last-name "نام خانوادگی" --mobile 09121234567
```

## API ثبت سریع

ثبت کالا فقط با `POST /api/quick-add` انجام می‌شود. کلید را در URL نگذارید؛ از هدر استفاده کنید:

```bash
curl -X POST https://example.com/api/quick-add \
  -H "X-API-Key: YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"product":"روغن","quantity":"2","unit":"عدد","supplier":"فروشنده"}'
```

فهرست تأمین‌کننده‌ها با `GET /api/suppliers` و همان هدر قابل دریافت است. کلید از صفحه «حساب من» قابل تعویض است.

## آزمون و بررسی

```bash
python -m unittest -v
python -m py_compile app.py licensing.py create_user.py generate_license.py
node --check static/app.js
ruff check . --exclude .venv --exclude __pycache__
bandit -q -r . -x ./.venv,./test_app.py,./tools
pip-audit -r requirements.txt
```

تغییرات کوچک سازگاری دیتابیس هنگام اولین درخواست هر پردازش اعمال می‌شوند و داده‌های PostgreSQL موجود حذف نمی‌شوند. پیش از هر استقرار اصلی از PostgreSQL پشتیبان بگیرید.
