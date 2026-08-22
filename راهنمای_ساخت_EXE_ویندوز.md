# راهنمای ساخت نسخهٔ EXE ویندوز «لیستیا»

**طراحی و توسعه: س.م.قتالی**

این راهنما توضیح می‌دهد چطور از وب‌اپ لیستیا (که روی چابکان + PostgreSQL اجرا می‌شود)
یک **فایل EXE بومی ویندوز** بسازید که کاربران بدون نصب پایتون، بدون نصب دیتابیس و
بدون هیچ پیش‌نیاز خاصی، فقط با دابل‌کلیک روی همان یک فایل اجرا کنند.

---

## ۱) معماری راه‌حل (چرا این روش؟)

- **داده‌ها همان‌جا می‌مانند:** نسخهٔ ویندوزی یک «پوستهٔ بومی» است که داخل خودش
  همان وب‌اپ میزبان‌شده روی چابکان را باز می‌کند. یعنی همهٔ کاربران به همان
  دیتابیس PostgreSQL متمرکز چابکان وصل می‌شوند و هیچ تغییری در سمت سرور لازم نیست.
- **اتصال مستقیم به دیتابیس ممنوع:** EXE هرگز مستقیم به PostgreSQL وصل نمی‌شود.
  (اولاً دیتابیس چابکان معمولاً فقط داخل خود سرویس در دسترس است؛ ثانیاً اگر رمز
  دیتابیس داخل EXE قرار بگیرد، هر کاربر می‌تواند استخراجش کند. اتصال از طریق خودِ
  وب‌اپِ HTTPS این مشکل امنیتی را کامل حل می‌کند.)
- **پنجرهٔ بومی، نه مرورگر:** از موتور `Edge WebView2` (همان موتوری که مرورگر Edge
  دارد) استفاده می‌شود؛ نتیجه یک پنجرهٔ مستقل ویندوزی است، نه یک تب مرورگر.
- **ورود ماندگار:** پروفایل مرورگر داخلی در پوشهٔ کاربر ذخیره می‌شود، بنابراین کاربر
  یک بار وارد می‌شود و دفعات بعدی بدون ورود مجدد وارد داشبورد می‌شود.

فایل‌های اضافه‌شده به پروژه:

| فایل | کار |
|------|-----|
| `desktop_launcher.py` | نقطهٔ ورود EXE (پنجرهٔ WebView2، مدیریت نشست، دانلود و لینک‌ها) |
| `build_windows/build.ps1` | اسکریپت ساخت EXE روی ویندوز |
| `build_windows/requirements.txt` | وابستگی‌های فقط-ساخت (pywebview + pythonnet + pyinstaller) |
| `build_windows/version_info.txt` | اطلاعات نسخهٔ فایل EXE (Properties) |
| `build_windows/icon/Listia.ico` | آیکون چندسایزهٔ برنامه (برای EXE و پنجره) |
| `.github/workflows/build-windows-exe.yml` | ساخت خودکار EXE با GitHub Actions |

---

## ۲) قبل از ساخت: آدرس واقعی اپ را مشخص کنید

EXE باید بداند اپ کجاست. آدرس به ترتیب اولویت از این‌ها خوانده می‌شود:

1. **پخت داخل EXE هنگام ساخت** (پیشنهادی برای ارسال به همهٔ کاربران)
2. متغیر محیطی `LISTIA_APP_URL`
3. فایل `‎.env` کنار `Listia.exe` با محتوای `LISTIA_APP_URL=https://...`
4. مقدار پیش‌فرض داخل `desktop_launcher.py` (الان `https://listia.chbk.app` که یک
   **نمونه/placeholder** است — حتماً با دامنهٔ واقعی سرویس چابکان خودتان عوض کنید.)

> دامنهٔ واقعی را از پنل چابکان (بخش «دامنه‌ها» یا «آدرس برنامه») بردارید؛ مثلاً
> چیزی شبیه `https://listia.chbk.app` یا دامنهٔ اختصاصی خودتان.

---

## ۳) روش الف — ساخت خودکار با GitHub Actions (پیشنهادی)

این روش روی رانر ویندوزی گیت‌هاب، بدون نیاز به ویندوز روی سیستم شما، EXE می‌سازد.

### قدم ۰ — افزودن فایل workflow در گیت‌هاب (فقط یک‌بار)

> چرا این قدم لازم است؟ دسترسی ربات Arena اجازهٔ نوشتن فایل‌های `.github/workflows`
> را ندارد، بنابراین این یک فایل را باید خودتان از طریق سایت گیت‌هاب اضافه کنید.
> بقیهٔ فایل‌های پروژه قبلاً push شده‌اند.

۱. در سایت گیت‌هاب، به صفحهٔ مخزن بروید: **github.com/mqattaly/Listia**

۲. روی دکمهٔ **Add file ← Create new file** کلیک کنید.

۳. در کادر بالای صفحه (نام فایل) دقیقاً این عبارت را بنویسید:

```
.github/workflows/build-windows-exe.yml
```

(وقتی این مسیر را بنویسید، گیت‌هاب خودش پوشه‌ها را می‌سازد.)

۴. محتوای زیر را کپی و در کادر بزرگ ویرایشگر paste کنید:

```yaml
name: Build Windows EXE

on:
  workflow_dispatch:
    inputs:
      app_url:
        description: "آدرس وب‌اپ چابکان (برای پخت داخل EXE) — خالی بگذارید تا پیش‌فرض استفاده شود"
        required: false
        default: ""
  push:
    tags:
      - "v*"

permissions:
  contents: write

jobs:
  build:
    runs-on: windows-latest

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install build dependencies
        run: |
          python -m pip install --upgrade pip
          python -m pip install -r build_windows/requirements.txt

      - name: Bake app URL (if provided)
        shell: pwsh
        env:
          APP_URL: ${{ github.event.inputs.app_url }}
        run: |
          $url = "$env:APP_URL".Trim().TrimEnd('/')
          if ($url) {
            "APP_URL = '$url'" | Out-File -FilePath desktop_siteconfig.py -Encoding utf8
            Write-Host "Baked app URL: $url"
          } elseif (Test-Path desktop_siteconfig.py) {
            Remove-Item desktop_siteconfig.py -Force
          }

      - name: Build EXE with PyInstaller
        shell: pwsh
        run: |
          python -m PyInstaller `
            --noconfirm --clean --onefile --windowed `
            --name Listia `
            --icon build_windows\icon\Listia.ico `
            --version-file build_windows\version_info.txt `
            --add-data "build_windows\icon\Listia.ico;desktop_assets" `
            --hidden-import clr `
            --hidden-import clr_loader `
            desktop_launcher.py

      - name: Upload EXE artifact
        uses: actions/upload-artifact@v4
        with:
          name: Listia-Windows-EXE
          path: dist/Listia.exe
          if-no-files-found: error

      - name: Publish GitHub Release (on tag)
        if: startsWith(github.ref, 'refs/tags/')
        uses: softprops/action-gh-release@v2
        with:
          files: dist/Listia.exe
          generate_release_notes: true
```

۵. پایین صفحه، در بخش **Commit changes**، یک پیام بنویسید (مثلاً «افزودن وورک‌فلو ساخت EXE»)،
   گزینهٔ **Commit directly to the `arena/01a026b0-listia` branch** را انتخاب کنید و
   دکمهٔ **Commit changes** را بزنید.

۶. تمام. حالا مراحل زیر را ادامه دهید.

### اجرای ساخت

1. در مخزن، آدرس اپ را به‌صورت secret ثبت کنید (اختیاری ولی تمیز):
   - **Settings ← Secrets and variables ← Actions ← New repository secret**
   - نام: `LISTIA_APP_URL` — مقدار: آدرس واقعی اپ چابکان.

2. **ساخت دستی:**
   - تب **Actions ← Build Windows EXE ← Run workflow**
   - فیلد `app_url` را با آدرس واقعی اپ چابکان پر کنید (یا خالی بگذارید تا پیش‌فرض استفاده شود).
   - خروجی در بخش Artifacts همان run با نام `Listia-Windows-EXE` قابل دانلود است.

3. **ساخت خودکار + انتشار در Releases:**
   - یک تگ بزنید: `git tag v1.0.0 && git push origin v1.0.0`
   - بعد از اتمام، فایل `Listia.exe` به‌صورت خودکار در صفحهٔ **Releases** منتشر می‌شود
     و می‌توانید لینک دانلودش را به کاربران بدهید.

> نکته: برای اینکه secret `LISTIA_APP_URL` هنگام ساخت داخل EXE پخت شود، می‌توانید
> در workflow یک قدم کوتاه اضافه کنید که مقدار secret را در `desktop_siteconfig.py`
> بنویسد (یا فقط همان فیلد `app_url` را هنگام Run workflow پر کنید).

---

## ۴) روش ب — ساخت دستی روی ویندوز خودتان

پیش‌نیاز: فقط **پایتون ۳.۱۰ تا ۳.۱۳** نصب روی ویندوز (از `python.org` — حتماً تیک
`Add python.exe to PATH` را بزنید). بقیه به‌صورت خودکار نصب می‌شود.

در پوشهٔ پروژه، یک PowerShell باز کنید و اجرا کنید:

```powershell
powershell -ExecutionPolicy Bypass -File build_windows\build.ps1
```

یا با پخت آدرس واقعی اپ:

```powershell
powershell -ExecutionPolicy Bypass -File build_windows\build.ps1 -AppUrl "https://example.chbk.app"
```

خروجی نهایی: **`dist\Listia.exe`** — یک فایل مستقل یک‌تکه.

اسکریپت این کارها را خودکار انجام می‌دهد:
1. ساخت محیط مجازی `.venv-build`
2. نصب `pywebview`، `pythonnet` و `pyinstaller`
3. ساخت EXE یک‌فایلی بدون پنجرهٔ کنسول با آیکون و اطلاعات نسخه

---

## ۵) نیازمندی WebView2 (مهم برای «روی همهٔ ویندوزها»)

EXE از موتور **Microsoft Edge WebView2** استفاده می‌کند:

- روی **ویندوز ۱۱** و **ویندوز ۱۰ (نسخه‌های به‌روز)** این مؤلفه معمولاً از قبل نصب است.
- اگر روی یک ویندوز قدیمی نصب نباشد، خود برنامه تشخیص می‌دهد و پیام راهنما نشان می‌دهد
  و صفحهٔ دانلود رسمی آن را باز می‌کند (یک نصب یک‌بارهٔ کوچک).
- برای اطمینان ۱۰۰٪ در محیط‌های سازمانی، می‌توانید «Evergreen Bootstrapper» رسمی
  مایکروسافت را هم کنار EXE به کاربر بدهید:
  <https://developer.microsoft.com/microsoft-edge/webview2/>

---

## ۶) ارسال به کاربران

- فقط فایل `Listia.exe` را بفرستید؛ کاربر دابل‌کلیک می‌کند و پنجرهٔ برنامه باز می‌شود.
- **SmartScreen ویندوز:** چون EXE تازه‌ساخته‌شده امضای دیجیتال ندارد، بار اول ویندوز
  ممکن است هشدار «Windows protected your PC» نشان دهد. کاربر باید
  `More info ← Run anyway` را بزند. برای حذف کامل این هشدار باید EXE را با
  **کد امضا (Code Signing Certificate)** امضا کنید.
- پیشنهاد: فایل را به‌صورت ZIP هم بگذارید (بعضی سیستم‌ها دانلود مستقیم exe را
  سخت‌تر می‌گیرند) و یک فایل `README.txt` کوتاه کنارش بگذارید.

---

## ۷) شخصی‌سازی

با یک فایل `‎.env` کنار `Listia.exe` (یا متغیر محیطی) می‌توانید بدون ساخت مجدد:

```env
LISTIA_APP_URL=https://example.chbk.app
LISTIA_TITLE=لیستیا
```

- `LISTIA_APP_URL`: آدرس اپ روی چابکان
- `LISTIA_TITLE`: عنوان پنجره

---

## ۸) نکات و سؤال‌های پرتکرار

- **آیا داده‌ها آفلاین هم کار می‌کنند؟** خیر. چون داده‌ها متمرکز روی چابکان است،
  برنامه به اینترنت نیاز دارد. اگر نسخهٔ کاملاً آفلاین (دیتابیس محلی روی سیستم هر
  کاربر) بخواهید، معماری متفاوتی لازم است (SQLite محلی) و می‌توان بعداً اضافه کرد.
- **چرا مستقیم به PostgreSQL وصل نمی‌شود؟** امنیت: رمز دیتابیس داخل EXE قابل استخراج
  است و چابکان هم معمولاً اتصال خارجی دیتابیس را نمی‌دهد. اتصال از طریق وب‌اپِ خودتان
  امن و پایدار است.
- **اندازهٔ EXE:** حدود چند ده مگابایت (پایتون + پوستهٔ WebView2)؛ این فایل شامل
  خود اپ/دیتابیس نمی‌شود، چون آن‌ها روی سرورند.
- **به‌روزرسانی اپ:** چون رابط کاربری از سرور لود می‌شود، بیشتر به‌روزرسانی‌های ظاهری
  بدون ساخت EXE جدید بلافاصله برای همه اعمال می‌شود. فقط اگر «پوسته» (لانچر) تغییر کند
  لازم است EXE جدید بسازید.
