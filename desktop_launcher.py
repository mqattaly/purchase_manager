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
_WEBVIEW2_CLIENTS = [
    ("{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}", "Microsoft Edge WebView2 Runtime"),
    ("{2CD8A007-E189-409D-A2C8-9AF4EF3C72AA}", "Microsoft Edge WebView2 Beta"),
    ("{0D50BFEC-CD6A-4F9A-964C-C7416E3ACB10}", "Microsoft Edge WebView2 Developer"),
    ("{65C35B14-6C1D-4122-AC46-7148CC9D6497}", "Microsoft Edge WebView2 Canary"),
]

WEBVIEW2_DOWNLOAD_URL = "https://developer.microsoft.com/microsoft-edge/webview2/"


def _version_tuple(version_str):
    parts = []
    for chunk in str(version_str or "").split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])


def _webview2_installed():
    """آیا Edge WebView2 Runtime (نسخهٔ Evergreen) روی این ویندوز نصب است؟"""
    try:
        import winreg
    except Exception:
        return False

    min_version = (86, 0, 622, 0)
    roots = (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE)
    for client_id, _desc in _WEBVIEW2_CLIENTS:
        for root in roots:
            try:
                with winreg.OpenKey(
                    root, rf"Software\Microsoft\EdgeUpdate\Clients\{client_id}"
                ) as key:
                    version, _ = winreg.QueryValueEx(key, "pv")
                    if _version_tuple(version) >= min_version:
                        return True
            except OSError:
                continue
    return False


def main():
    _load_env_file()

    if sys.platform != "win32":
        _message_box(
            "این برنامه مخصوص ویندوز است و روی سیستم‌عامل فعلی اجرا نمی‌شود.",
            _window_title(),
            error=True,
        )
        return 1

    import webview

    app_url = _app_url()
    title = _window_title()
    user_data_dir = _user_data_dir()
    os.makedirs(user_data_dir, exist_ok=True)

    # اجازهٔ دانلود (فایل نمونهٔ اکسل) با دیالوگ «ذخیره در…» بومی ویندوز.
    webview.settings["ALLOW_DOWNLOADS"] = True

    # روی ویندوزهای قدیمی ممکن است موتور WebView2 نصب نباشد؛ در این صورت
    # به‌جای نمایش صفحهٔ خالی، راهنمای نصب را نشان می‌دهیم.
    if not _webview2_installed():
        _message_box(
            "برای اجرای لیستیا به «Microsoft Edge WebView2 Runtime» نیاز است.\n\n"
            "این مؤلفهٔ کوچک معمولاً همراه ویندوز ۱۰/۱۱ نصب است، اما روی این سیستم پیدا نشد.\n"
            "بعد از نصب یک‌بارهٔ آن، برنامه بدون مشکل اجرا می‌شود.",
            title,
            error=True,
        )
        try:
            import webbrowser

            webbrowser.open(WEBVIEW2_DOWNLOAD_URL)
        except Exception:
            pass
        return 1

    if not _check_reachable(app_url):
        _message_box(
            "به سرور لیستیا دسترسی پیدا نشد.\n\n"
            "لطفاً اتصال اینترنت را بررسی و دوباره تلاش کنید.\n"
            f"آدرس برنامه: {app_url}",
            title,
            error=True,
        )

    window = webview.create_window(
        title=title,
        url=app_url,
        width=1280,
        height=820,
        min_size=(980, 640),
        background_color="#142430",
    )

    webview.start(
        private_mode=False,          # حفظ نشست/ورود بین دفعات اجرا
        storage_path=user_data_dir,  # پوشهٔ پروفایل WebView2
        icon=_icon_path(),
        gui="edgechromium",
        debug=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
