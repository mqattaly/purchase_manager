#!/usr/bin/env python3
"""Interactive/command-line Listia license generator for the system owner."""

import sys

from licensing import (
    LICENSE_TIERS,
    generate_key,
    generate_master_key,
    get_user_code,
    validate_duration,
)


def banner():
    print("=" * 65)
    print("      🔑 سامانه تولید لایسنس نرم‌افزار لیستیا (Listia)      ")
    print("           طراحی و توسعه: س.م.قتالی                        ")
    print("=" * 65)


def checked_duration(raw):
    result = validate_duration(raw)
    if not result:
        raise SystemExit("خطا: مدت اعتبار باید LIFE یا بین ۱ روز تا ۱۰ سال باشد.")
    return result


def checked_tier(raw, *, master=False):
    tier = str(raw or "PRO").strip().upper()
    if tier not in LICENSE_TIERS or (master and tier == "ENTERPRISE"):
        raise SystemExit("خطا: پلن لایسنس معتبر نیست.")
    return tier


def show_license(ident, key, tier, period_info, *, is_master=False):
    _period_code, days, duration_label = period_info
    validity_text = (
        "دائمی (مادام‌العمر)"
        if days is None
        else f"{days} روز پس از تاریخ فعال‌سازی"
    )

    print("\n" + "─" * 65)
    if is_master:
        print("★ نوع لایسنس: کلید سراسری (Universal Master Key)")
        print("  این کلید روی تمام حساب‌ها معتبر است و باید محرمانه بماند.")
    else:
        print(f"👤 نام کاربری / شناسه: {ident}")
        print(f"🏷️  کد فعال‌سازی سیستم: {get_user_code(ident)}")
    print(f"📦 پلن لایسنس: {tier} (نامحدود)")
    print(f"⏳ مدت زمان اعتبار: {duration_label} [{validity_text}]")
    print(f"\n🔑 کلید لایسنس:\n   >>>  {key}  <<<")
    print("─" * 65)
    print("\n📋 متن ارسالی به مشتری:")
    print(f"با سلام، لایسنس نسخه نامحدود «لیستیا» ({duration_label}) صادر شد.")
    print(f"کلید لایسنس: {key}")
    print(f"مدت اعتبار: {validity_text}")
    print("لطفاً آن را در بخش «حساب من» فعال کنید.\n")


def issue(ident, duration, tier, *, is_master=False):
    period_info = checked_duration(duration)
    period_code = period_info[0]
    tier = checked_tier(tier, master=is_master)
    if is_master:
        key = generate_master_key(tier, period_code)
        show_license("MASTER", key, tier, period_info, is_master=True)
    else:
        ident = ident.strip()
        if not ident or len(ident) > 100:
            raise SystemExit("خطا: شناسه مشتری الزامی و حداکثر ۱۰۰ کاراکتر است.")
        key = generate_key(ident, tier, period_code)
        show_license(ident, key, tier, period_info)


def interactive():
    print("نوع صدور: 1) اختصاصی  2) سراسری  q) خروج")
    choice = input("انتخاب [1]: ").strip() or "1"
    if choice.lower() == "q":
        return
    if choice not in {"1", "2"}:
        raise SystemExit("انتخاب معتبر نیست.")

    is_master = choice == "2"
    ident = "MASTER" if is_master else input("نام کاربری یا شناسه مشتری: ").strip()
    print("مدت: 1) دائمی  2) 30 روز  3) 90 روز  4) 180 روز  5) 365 روز  6) دلخواه")
    duration_choice = input("انتخاب [1]: ").strip() or "1"
    durations = {"1": "LIFE", "2": "30D", "3": "90D", "4": "180D", "5": "365D"}
    if duration_choice == "6":
        days = input("تعداد روز (۱ تا ۳۶۵۰): ").strip()
        duration = f"{days}D"
    elif duration_choice in durations:
        duration = durations[duration_choice]
    else:
        raise SystemExit("انتخاب مدت معتبر نیست.")
    issue(ident, duration, "UNLIMITED" if is_master else "PRO", is_master=is_master)


def main():
    banner()
    if len(sys.argv) == 1:
        interactive()
        return

    first = sys.argv[1].strip()
    is_master = first.lower() in {"--master", "-m", "master"}
    ident = "MASTER" if is_master else first
    duration = sys.argv[2].strip() if len(sys.argv) > 2 else "LIFE"
    tier = sys.argv[3].strip() if len(sys.argv) > 3 else ("UNLIMITED" if is_master else "PRO")
    issue(ident, duration, tier, is_master=is_master)


if __name__ == "__main__":
    main()
