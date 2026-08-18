from app import app, db, Supplier, User

with app.app_context():
    owner = User.query.order_by(User.id).first()
    if not owner:
        print("اول یک کاربر بساز، بعد seed را اجرا کن.")
    else:
        names = ["یونس زاده", "شرکت فلزکار", "بازرگانی احمدی"]
        for name in names:
            exists = Supplier.query.filter_by(owner_id=owner.id, name=name).first()
            if not exists:
                db.session.add(Supplier(name=name, owner_id=owner.id))
        db.session.commit()
        print(Supplier.query.filter_by(owner_id=owner.id).all())
