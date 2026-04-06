from sqlalchemy.orm import Session
import models

def create_product(db: Session, product):
    db_product = models.Product(name=product.name, price=product.price)
    db.add(db_product)
    db.commit()
    db.refresh(db_product)
    return db_product

def get_products(db: Session):
    return db.query(models.Product).all()

def create_order(db: Session, order):
    total = order.quantity * get_product_price(db, order.product_name)
    
    db_order = models.Order(
        customer_phone=order.customer_phone,
        product_name=order.product_name,
        quantity=order.quantity,
        total_price=total
    )
    db.add(db_order)
    db.commit()
    db.refresh(db_order)
    return db_order

def get_product_price(db, name):
    product = db.query(models.Product).filter(models.Product.name == name).first()
    return product.price if product else 0