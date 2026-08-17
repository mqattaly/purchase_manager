from app import app, db, User
from werkzeug.security import generate_password_hash

with app.app_context():
    db.create_all()

    username = input("نام کاربری: ")
    password = input("رمز عبور: ")

    if User.query.filter_by(username=username).first():
        print("این نام کاربری قبلاً وجود دارد.")
    else:
        user = User(username=username, password_hash=generate_password_hash(password))
        db.session.add(user)
        db.session.commit()
        print("✓ کاربر ساخته شد.")