from sqlalchemy.orm import Session
from sqlalchemy import func, distinct
import models
from crypto import encrypt_token, decrypt_token, is_encrypted


# ── Business ──────────────────────────────────────────────
def create_business(db: Session, data):
    # Encrypt token before storing
    raw_token = data.whatsapp_token or ""
    stored_token = encrypt_token(raw_token) if raw_token else None

    b = models.Business(
        name=data.name,
        owner_username=data.owner_username,
        owner_password=data.owner_password,
        whatsapp_phone_id=data.whatsapp_phone_id or None,
        whatsapp_token=stored_token,
    )
    db.add(b)
    db.commit()
    db.refresh(b)
    return b


def get_business_by_username(db: Session, username: str):
    return db.query(models.Business).filter(
        models.Business.owner_username == username
    ).first()


def get_business_by_phone_id(db: Session, phone_id: str):
    return db.query(models.Business).filter(
        models.Business.whatsapp_phone_id == phone_id
    ).first()


def get_all_businesses(db: Session):
    return db.query(models.Business).order_by(models.Business.id).all()


def get_business_by_id(db: Session, business_id: int):
    return db.query(models.Business).filter(
        models.Business.id == business_id
    ).first()


def get_decrypted_token(business: models.Business) -> str:
    """Get the plaintext WhatsApp token for a business."""
    if not business.whatsapp_token:
        return ""
    # Handle both encrypted and legacy plaintext tokens
    if is_encrypted(business.whatsapp_token):
        return decrypt_token(business.whatsapp_token)
    # Plaintext token (e.g. from migrate.py) — return as-is
    return business.whatsapp_token


def update_business(db: Session, business_id: int, data):
    b = get_business_by_id(db, business_id)
    if not b:
        return None
    update_dict = data.dict(exclude_none=True)
    # Encrypt token if it's being updated
    if "whatsapp_token" in update_dict and update_dict["whatsapp_token"]:
        raw = update_dict["whatsapp_token"]
        if not is_encrypted(raw):
            update_dict["whatsapp_token"] = encrypt_token(raw)
    for field, value in update_dict.items():
        setattr(b, field, value)
    db.commit()
    db.refresh(b)
    return b


def delete_business(db: Session, business_id: int):
    b = get_business_by_id(db, business_id)
    if b:
        db.delete(b)
        db.commit()
    return b


# ── Products ──────────────────────────────────────────────
def create_product(db: Session, business_id: int, product):
    p = models.Product(
        business_id=business_id,
        name=product.name,
        price=product.price
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def get_products(db: Session, business_id: int):
    return db.query(models.Product).filter(
        models.Product.business_id == business_id
    ).all()


def get_product_price(db: Session, business_id: int, name: str) -> float:
    p = db.query(models.Product).filter(
        models.Product.business_id == business_id,
        models.Product.name.ilike(name)  # case-insensitive match
    ).first()
    return p.price if p else 0.0


def delete_product(db: Session, product_id: int, business_id: int):
    p = db.query(models.Product).filter(
        models.Product.id == product_id,
        models.Product.business_id == business_id
    ).first()
    if p:
        db.delete(p)
        db.commit()
    return p


# ── Orders ────────────────────────────────────────────────
def create_order(db: Session, business_id: int, order):
    total = order.quantity * get_product_price(db, business_id, order.product_name)
    o = models.Order(
        business_id=business_id,
        customer_phone=order.customer_phone,
        product_name=order.product_name,
        quantity=order.quantity,
        total_price=total
    )
    db.add(o)
    db.commit()
    db.refresh(o)
    return o


def get_orders(db: Session, business_id: int):
    return db.query(models.Order).filter(
        models.Order.business_id == business_id
    ).order_by(models.Order.id.desc()).all()


# ── Chat ──────────────────────────────────────────────────
def log_message(db: Session, business_id: int, phone: str, direction: str, message: str):
    m = models.ChatMessage(
        business_id=business_id,
        phone=phone,
        direction=direction,
        message=message
    )
    db.add(m)
    db.commit()


def get_conversations(db: Session, business_id: int):
    subq = (
        db.query(
            models.ChatMessage.phone,
            func.max(models.ChatMessage.id).label("max_id")
        )
        .filter(models.ChatMessage.business_id == business_id)
        .group_by(models.ChatMessage.phone)
        .subquery()
    )
    return (
        db.query(models.ChatMessage)
        .join(subq, models.ChatMessage.id == subq.c.max_id)
        .order_by(models.ChatMessage.id.desc())
        .all()
    )


def get_messages_for_phone(db: Session, business_id: int, phone: str):
    return (
        db.query(models.ChatMessage)
        .filter(
            models.ChatMessage.business_id == business_id,
            models.ChatMessage.phone == phone
        )
        .order_by(models.ChatMessage.created_at.asc())
        .all()
    )


def get_all_customer_phones(db: Session, business_id: int):
    rows = (
        db.query(distinct(models.ChatMessage.phone))
        .filter(
            models.ChatMessage.business_id == business_id,
            models.ChatMessage.direction == "in"
        )
        .all()
    )
    return [r[0] for r in rows]
