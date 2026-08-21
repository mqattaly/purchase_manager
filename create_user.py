from app import app, db, User, normalize_phone, clean_person_name
from werkzeug.security import generate_password_hash

with app.app_context():
    db.create_all()

    username = input("نام کاربری: ").strip()
    password = input("رمز عبور: ")
    first_name = clean_person_name(input("نام: "))
    last_name = clean_person_name(input("نام خانوادگی: "))
    phone = normalize_phone(input("شماره موبایل: "))

    if User.query.filter(db.func.lower(User.username) == username.lower()).first():
        print("این نام کاربری قبلاً وجود دارد.")
    else:
        user = User(
            username=username,
            password_hash=generate_password_hash(password),
            first_name=first_name,
            last_name=last_name,
            phone=phone or None,
        )
        db.session.add(user)
        db.session.commit()
        print("✓ کاربر ساخته شد.")
