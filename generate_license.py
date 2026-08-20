#!/usr/bin/env python3
"""
ابزار تولید لایسنس نرم‌افزار لیستیا (Listia)
طراحی و توسعه: س.م.قتالی
"""

import sys
import os
from licensing import generate_key, generate_master_key, get_user_code


def banner():
    print("=" * 60)
    print("      🔑 سامانه تولید لایسنس نرم‌افزار لیستیا (Listia)      ")
    print("           طراحی و توسعه: س.م.قتالی                        ")
    print("=" * 60)


def show_license(ident, key, tier="PRO", is_master=False):
    print("\n" + "─" * 60)
    if is_master:
        print("★ نوع لایسنس: کلید سراسری (Universal Master Key)")
        print("  این کلید روی تمام حساب‌ها و دستگاه‌ها معتبر است.")
    else:
        print(f"👤 نام کاربری / شناسه: {ident}")
        print(f"🏷️  کد فعال‌سازی سیستم: {get_user_code(ident)}")
        print(f"📦 پلن لایسنس: {tier} (نامحدود)")

    print(f"\n🔑 کلید لایسنس (License Key):")
    print(f"   >>>  {key}  <<<")
    print("─" * 60)
    print("\n📋 متن ارسالی به مشتری:")
    print("┌──────────────────────────────────────────────────────────┐")
    print("│ با سلام، لایسنس نسخه نامحدود «لیستیا» برای شما صادر شد:   │")
    print(f"│ کلید لایسنس: {key.ljust(43)}│")
    print("│ لطفاً در منوی «حساب من» یا پنجره فعال‌سازی لایسنس وارد    │")
    print("│ فرمایید تا سقف ثبت تأمین‌کننده و محصول نامحدود شود.       │")
    print("│ طراحی و توسعه: س.م.قتالی                                 │")
    print("└──────────────────────────────────────────────────────────┘\n")


def main():
    banner()

    if len(sys.argv) > 1:
        arg = sys.argv[1].strip()
        if arg in ["--master", "-m", "master"]:
            key = generate_master_key("UNLIMITED")
            show_license("MASTER", key, "UNLIMITED", is_master=True)
            return

        ident = arg
        tier = sys.argv[2].strip().upper() if len(sys.argv) > 2 else "PRO"
        key = generate_key(ident, tier)
        show_license(ident, key, tier)
        return

    print("گزینه مورد نظر را انتخاب کنید:")
    print("  1) صدور لایسنس اختصاصی برای یک کاربر (بر اساس نام کاربری یا شناسه)")
    print("  2) صدور کلید لایسنس سراسری (Master Key)")
    print("  q) خروج")

    choice = input("\nانتخاب شما [1]: ").strip() or "1"

    if choice == "2":
        key = generate_master_key("UNLIMITED")
        show_license("MASTER", key, "UNLIMITED", is_master=True)
    elif choice in ["1", ""]:
        ident = input("\nنام کاربری یا شناسه فعال‌سازی مشتری را وارد کنید: ").strip()
        if not ident:
            print("خطا: شناسه یا نام کاربری نمی‌تواند خالی باشد.")
            sys.exit(1)
        key = generate_key(ident, "PRO")
        show_license(ident, key, "PRO")
    else:
        print("خروج.")


if __name__ == "__main__":
    main()
