"""لیستیا — لانچر دسکتاپ ویندوز (نسخهٔ EXE).

این فایل نقطهٔ ورود فایل اجرایی ویندوز است. کاری که می‌کند:
  ۱) یک پنجرهٔ بومی ویندوز (WebView2) باز می‌کند و داخل آن همان وب‌اپ «لیستیا»
     را که روی چابکان میزبانی شده است نمایش می‌دهد؛ بنابراین داده‌ها همان
     دادهٔ متمرکز روی چابکان/PostgreSQL باقی می‌مانند و هیچ اتصال مستقیمی به
     دیتابیس از سمت کلاینت وجود ندارد.
  ۲) پروفایل مرورگر داخلی را در پوشهٔ کاربر ذخیره می‌کند (private_mode خاموش)
     تا نشست و ورود کاربر بین دفعات اجرا حفظ شود و نیازی به ورود مجدد نباشد.
  ۳) دانلود فایل نمونهٔ اکسل را با دیالوگ «ذخیره در…» بومی ویندوز باز می‌کند
     و لینک‌های target=_blank را در مرورگر پیش‌فرض سیستم باز می‌کند.

تنظیمات اختیاری (متغیر محیطی یا فایل .env کنار EXE):
  LISTIA_APP_URL    آدرس وب‌اپ روی چابکان (پیش‌فرض: https://listia.chbk.app)
  LISTIA_TITLE      عنوان پنجره (پیش‌فرض: لیستیا)

برای ساخت EXE به فایل build_windows/build.ps1 (یا وورک‌فلو GitHub Actions)
مراجعه کنید.
"""

import os
import sys

DEFAULT_APP_URL = "https://listia.chbk.app"
DEFAULT_TITLE = "لیستیا"

# فایل desktop_siteconfig.py فقط هنگام ساخت (build.ps1 / GitHub Actions) تولید
# می‌شود تا آدرس واقعی اپ روی چابکان داخل EXE «پخت» شود. اگر وجود نداشته باشد،
# مقدار پیش‌فرض بالا (یا متغیر محیطی / فایل .env کنار EXE) استفاده می‌شود.
try:
    from desktop_siteconfig import APP_URL as _BUILTIN_APP_URL
except Exception:
    _BUILTIN_APP_URL = None


def _load_env_file():
    """فایل .env کنار EXE/اسکریپت را در صورت وجود بخواند (بدون نیاز به python-dotenv)."""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(base, ".env")
    if not os.path.exists(env_path):
        return
    try:
        with open(env_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


def _app_url():
    return (
        os.environ.get("LISTIA_APP_URL") or _BUILTIN_APP_URL or DEFAULT_APP_URL
    ).strip().rstrip("/")


def _window_title():
    return (os.environ.get("LISTIA_TITLE") or DEFAULT_TITLE).strip()


def _user_data_dir():
    local = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local"
    )
    return os.path.join(local, "Listia", "WebView2Profile")


def _icon_path():
    """آیکون پنجره: داخل باندل PyInstaller یا کنار فایل اجرایی."""
    candidates = []
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        candidates.append(os.path.join(base, "desktop_assets", "Listia.ico"))
        candidates.append(os.path.join(base, "static", "favicon.ico"))
        candidates.append(os.path.join(os.path.dirname(sys.executable), "Listia.ico"))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
        candidates.append(os.path.join(base, "build_windows", "icon", "Listia.ico"))
        candidates.append(os.path.join(base, "static", "favicon.ico"))
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None


def _message_box(text, title, error=False):
    """جعبهٔ پیام بومی ویندوز بدون هیچ وابستگی اضافه."""
    try:
        import ctypes

        flags = 0x10 if error else 0x40  # MB_ICONERROR / MB_ICONINFORMATION
        ctypes.windll.user32.MessageBoxW(0, text, title, flags)
    except Exception:
        if sys.stdout is not None:  # در حالت --windowed ممکن است stdout وجود نداشته باشد
            print(text)


def _check_reachable(url, timeout=8):
    try:
        import urllib.request

        req = urllib.request.Request(
            url, method="GET", headers={"User-Agent": "ListiaDesktop/1.0"}
        )
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except Exception:
        return False


# شناسه‌های نصب Edge WebView2 (همان‌هایی که pywebview برای تشخیص استفاده می‌کند)
_WEBVIEW2_CLIENT_IDS = [
    "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",  # Microsoft Edge WebView2 Runtime
    "{2CD8A007-E189-409D-A2C8-9AF4EF3C72AA}",  # WebView2 Beta
    "{0D50BFEC-CD6A-4F9A-964C-C7416E3ACB10}",  # WebView2 Developer
    "{65C35B14-6C1D-4122-AC46-7148CC9D6497}",  # WebView2 Canary
]

WEBVIEW2_DOWNLOAD_URL = "https://developer.microsoft.com/microsoft-edge/webview2/"
# نصب‌کنندهٔ رسمی Evergreen Bootstrapper مایکروسافت (کوچک؛ آخرین نسخه را نصب می‌کند)
WEBVIEW2_BOOTSTRAPPER_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"

_MIN_WEBVIEW2_VERSION = (86, 0, 622, 0)


def _version_tuple(version_str):
    parts = []
    for chunk in str(version_str or "").split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])


def _read_reg_pv(root, path):
    """مقدار pv (نسخه) را از یک کلید رجیستری بخواند؛ اگر نبود None برمی‌گرداند."""
    try:
        import winreg

        with winreg.OpenKey(root, path) as key:
            version, _ = winreg.QueryValueEx(key, "pv")
            return version
    except Exception:
        return None


def _webview2_registry_paths():
    """همهٔ مسیرهای رجیستری که WebView2 می‌تواند در آن‌ها ثبت شده باشد.

    نکتهٔ مهم: در ویندوز ۶۴ بیتی، نصبِ «per-machine» زیر WOW6432Node (نمای ۳۲ بیتی)
    نوشته می‌شود؛ اگر فقط نمای ۶۴ بیتی را بخوانیم اشتباهاً «نصب نیست» گزارش می‌شود.
    به همین دلیل هر دو نما + HKEY_CURRENT_USER را بررسی می‌کنیم.
    """
    try:
        import winreg

        roots = (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER)
    except Exception:
        return []

    paths = []
    for client_id in _WEBVIEW2_CLIENT_IDS:
        for root in roots:
            for sub in (r"SOFTWARE\Microsoft\EdgeUpdate\Clients", r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"):
                paths.append((root, rf"{sub}\{client_id}"))
    return paths


def _webview2_files_present():
    """روش دوم: اگر فایل msedgewebview2.exe در پوشه‌های استاندارد وجود داشته باشد."""
    candidates = []
    for env_name, tail in (
        ("LOCALAPPDATA", r"Microsoft\EdgeWebView\Application"),
        ("ProgramFiles(x86)", r"Microsoft\EdgeWebView\Application"),
        ("ProgramFiles", r"Microsoft\EdgeWebView\Application"),
    ):
        base = os.environ.get(env_name)
        if base:
            candidates.append(os.path.join(base, tail))
    for base in candidates:
        try:
            if not os.path.isdir(base):
                continue
            for sub in os.listdir(base):
                if os.path.isfile(os.path.join(base, sub, "msedgewebview2.exe")):
                    return True
        except OSError:
            continue
    return False


def _webview2_installed():
    """آیا Edge WebView2 Runtime (نسخهٔ مناسب) روی این ویندوز در دسترس است؟"""
    for root, path in _webview2_registry_paths():
        version = _read_reg_pv(root, path)
        if version and _version_tuple(version) >= _MIN_WEBVIEW2_VERSION:
            return True
    return _webview2_files_present()


def _bundled_webview2_installer():
    """مسیر نصب‌کنندهٔ WebView2 اگر داخل باندل EXE (یا کنار آن) گذاشته شده باشد."""
    candidates = []
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        candidates.append(os.path.join(base, "desktop_assets", "MicrosoftEdgeWebview2Setup.exe"))
        candidates.append(os.path.join(os.path.dirname(sys.executable), "MicrosoftEdgeWebview2Setup.exe"))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
        candidates.append(os.path.join(base, "build_windows", "MicrosoftEdgeWebview2Setup.exe"))
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None


def _install_webview2():
    """نصب خودکار WebView2 Runtime (ترجیحاً از فایل باندل‌شده، وگرنه دانلود رسمی)."""
    import shutil
    import subprocess
    import tempfile

    installer = _bundled_webview2_installer()
    local = installer is not None

    if not local:
        try:
            import urllib.request

            tmp_dir = tempfile.mkdtemp(prefix="listia_wv2_")
            installer = os.path.join(tmp_dir, "MicrosoftEdgeWebview2Setup.exe")
            req = urllib.request.Request(
                WEBVIEW2_BOOTSTRAPPER_URL, headers={"User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(req, timeout=120) as resp, open(installer, "wb") as fh:
                shutil.copyfileobj(resp, fh)
        except Exception:
            return False

    try:
        subprocess.run([installer, "/silent", "/install"], timeout=900, check=False)
    except Exception:
        return False
    finally:
        if not local and installer:
            shutil.rmtree(os.path.dirname(installer), ignore_errors=True)

    return 
