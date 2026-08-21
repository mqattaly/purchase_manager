#!/usr/bin/env python3
"""Safely create a Listia account from the server command line."""

import argparse
from getpass import getpass

from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash

from app import (
    MIN_PASSWORD_LENGTH,
    PRIMARY_ADMIN_USERNAME,
    User,
    app,
    clean_person_name,
    clean_username,
    db,
    ensure_schema,
    normalize_mobile,
    user_by_username,
    username_is_valid,
)


def parse_args():
    parser = argparse.ArgumentParser(description="Create a Listia account")
    parser.add_argument("username", nargs="?", help="account username")
    parser.add_argument("--first-name", default="")
    parser.add_argument("--last-name", default="")
    parser.add_argument("--mobile", default="")
    parser.add_argument(
        "--admin",
        action="store_true",
        help=f"create the protected owner account ({PRIMARY_ADMIN_USERNAME})",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    username = clean_username(args.username or input("نام کاربری: "))
    if not username_is_valid(username):
        raise SystemExit("نام کاربری معتبر نیست.")
    if args.admin and username != PRIMARY_ADMIN_USERNAME:
        raise SystemExit(f"حساب مدیر اصلی باید {PRIMARY_ADMIN_USERNAME} باشد.")
    if username == PRIMARY_ADMIN_USERNAME and not args.admin:
        raise SystemExit("برای ساخت حساب مدیر اصلی از --admin استفاده کنید.")

    password = getpass("رمز عبور: ")
    confirmation = getpass("تکرار رمز عبور: ")
    if password != confirmation:
        raise SystemExit("تکرار رمز عبور یکسان نیست.")
    if not MIN_PASSWORD_LENGTH <= len(password) <= 128:
        raise SystemExit(
            f"رمز عبور باید بین {MIN_PASSWORD_LENGTH} و ۱۲۸ کاراکتر باشد."
        )

    first_name = clean_person_name(args.first_name)
    last_name = clean_person_name(args.last_name)
    mobile = normalize_mobile(args.mobile) if args.mobile else None
    if args.mobile and not mobile:
        raise SystemExit("شماره موبایل معتبر نیست.")

    with app.app_context():
        ensure_schema()
        if user_by_username(username):
            raise SystemExit("این نام کاربری قبلاً وجود دارد.")
        user = User(
            username=username,
            password_hash=generate_password_hash(password),
            first_name=first_name or None,
            last_name=last_name or None,
            mobile=mobile,
            is_admin=args.admin,
            is_licensed=args.admin,
            license_type="UNLIMITED" if args.admin else "free",
        )
        db.session.add(user)
        try:
            db.session.commit()
        except IntegrityError as error:
            db.session.rollback()
            raise SystemExit("حساب ساخته نشد؛ نام کاربری تکراری است.") from error
        print(f"✓ حساب {username} ساخته شد.")


if __name__ == "__main__":
    main()
