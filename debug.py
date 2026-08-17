from app import app, db, Product

with app.app_context():
    all_products = Product.query.all()
    for p in all_products:
        print(f"id={p.id} | name={p.product_name} | ordered={p.ordered} | ordered_date={p.ordered_date} | supplier_id={p.supplier_id}")