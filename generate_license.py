#!/usr/bin/env python3
"""
ابزار تولید لایسنس نرم‌افزار لیستیا (Listia)
پشتیبانی از لایسنس‌های مدت‌دار (۱ ماهه، ۳ ماهه، ۱ ساله و...) و مادام‌العمر
طراحی و توسعه: س.م.قتالی
"""

import sys
import os
from licensing import generate_key, generate_master_key, get_user_code, normalize_duration, DURATION_CHOICES


def banner():
    print("=" * 65)
    print("      🔑 سامانه تولید لایسنس نرم‌افزار لیستیا (Listia)      ")
    print("           طراحی و توسعه: س.م.قتالی                        ")
    print("=" * 65)


def show_license(ident, key, tier="PRO", duration="LIFE", is_master=False):
    _, days, duration_label = normalize_duration(duration)
    validity_text = "دائمی (مادام‌العمر)" if days is None else f"{days} روز پس از تاریخ فعال‌سازی"

    print("\n" + "─" * 65)
    if is_master:
        print("★ نوع لایسنس: کلید سراسری (Universal Master Key)")
        print("  این کلید روی تمام حساب‌ها و دستگاه‌ها معتبر است.")
    else:
        print(f"👤 نام کاربری / شناسه: {ident}")
        print(f"🏷️  کد فعال‌سازی سیستم: {get_user_code(ident)}")

    print(f"📦 پلن لایسنس: {tier} (نامحدود)")
    print(f"⏳ مدت زمان اعتبار: {duration_label} [{validity_text}]")

    print(f"\n🔑 کلید لایسنس (License Key):")
    print(f"   >>>  {key}  <<<")
    print("─" * 65)
    print("\n📋 متن ارسالی به مشتری:")
    print("┌───────────────────────────────────────────────────────────────┐")
    print(f"│ با سلام، لایسنس نسخه نامحدود «لیستیا» ({duration_label}) صادر شد: │")
    print(f"│ کلید لایسنس: {key.ljust(48)}│")
    print(f"│ مدت اعتبار: {validity_text.ljust(49)}│")
    print("│ لطفاً در منوی «حساب من» یا پنجره فعال‌سازی لایسنس وارد فرمایید. │")
    print("│ طراحی و توسعه: س.م.قتالی                                      │")
    print("└───────────────────────────────────────────────────────────────┘\n")


def main():
    banner()

    if len(sys.argv) > 1:
        arg = sys.argv[1].strip()
        duration = sys.argv[2].strip() if len(sys.argv) > 2 else "LIFE"
        tier = sys.argv[3].strip().upper() if len(sys.argv) > 3 else "PRO"

        if arg in ["--master", "-m", "master"]:
            key = generate_master_key(tier, duration)
            show_license("MASTER", key, tier, duration, is_master=True)
            return

        ident = arg
        key = generate_key(ident, tier, duration)
        show_license(ident, key, tier, duration)
        return

    print("نوع صدور لایسنس را انتخاب کنید:")
    print("  1) صدور لایسنس اختصاصی برای یک کاربر")
    print("  2) صدور کلید لایسنس سراسری (Master Key)")
    print("  q) خروج")

    choice = input("\nانتخاب شما [1]: ").strip() or "1"
    if choice == "q":
        print("خروج.")
        return

    is_master = (choice == "2")
    ident = "MASTER"
    if not is_master:
        ident = input("\nنام کاربری یا شناسه فعال‌سازی مشتری را وارد کنید: ").strip()
        if not ident:
            print("خطا: شناسه یا نام کاربری نمی‌تواند خالی باشد.")
            sys.exit(1)

    print("\nمدت زمان اعتبار لایسنس را انتخاب کنید:")
    print("  1) مادام‌العمر (دائمی) [پیش‌فرض]")
    print("  2) ۱ ماهه (۳۰ روز)")
    print("  3) ۳ ماهه (۹۰ روز)")
    print("  4) ۶ ماهه (۱۸۰ روز)")
    print("  5) ۱ ساله (۳۶۵ روز)")
    print("  6) تعداد روز دلخواه (مثلاً: 14 یا 45 یا 60)")

    dur_choice = input("\nانتخاب مدت زمان [1]: ").strip() or "1"
    dur_map = {
        "1": "LIFE",
        "2": "30D",
        "3": "90D",
        "4": "180D",
        "5": "365D",
    }

    if dur_choice in dur_map:
        duration = dur_map[dur_choice]
    elif dur_choice == "6":
        custom_days = input("تعداد روز اعتبار را وارد کنید: ").strip()
        duration = f"{custom_days}D" if custom_days.isdigit() else "LIFE"
    else:
        duration = "LIFE"

    if is_master:
        key = generate_master_key("UNLIMITED", duration)
        show_license("MASTER", key, "UNLIMITED", duration, is_master=True)
    else:
        key = generate_key(ident, "PRO", duration)
        show_license(ident, key, "PRO", duration)


if __name__ == "__main__":
    main()
