from app import app, db, Supplier

with app.app_context():
    db.session.add(Supplier(name="یونس زاده"))
    db.session.add(Supplier(name="شرکت فلزکار"))
    db.session.add(Supplier(name="بازرگانی احمدی"))
    db.session.commit()
    print(Supplier.query.all())