"""
crud.py — Database access layer.

Change vs previous version:
  • get_decrypted_token() now propagates TokenDecryptionError instead of
    returning "". Callers that need a graceful fallback should call
    crypto.safe_decrypt_token() directly or catch TokenDecryptionError.
  • All other functions are UNCHANGED.
"""

from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy import func, distinct

import models
from crypto import encrypt_token, decrypt_token, is_encrypted, TokenDecryptionError


# ── Business ──────────────────────────────────────────────────────────────────

def create_business(db: Session, data):
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
    """
    Return the decrypted WhatsApp access token for *business*.

    Raises:
        TokenDecryptionError: if the stored token cannot be decrypted.
            Callers should catch this and return HTTP 500 / 503 with a
            clear message rather than trying to send with an empty token.

    Returns "" when no token has been configured (normal for new accounts).
    """
    if not business or not business.whatsapp_token:
        return ""
    return decrypt_token(business.whatsapp_token)


def update_business(db: Session, business_id: int, data):
    b = get_business_by_id(db, business_id)
    if not b:
        return None
    update_dict = data.dict(exclude_none=True)

    # Encrypt token before saving — idempotent, so safe even if already encrypted
    if "whatsapp_token" in update_dict and update_dict["whatsapp_token"]:
        update_dict["whatsapp_token"] = encrypt_token(update_dict["whatsapp_token"])

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


# ── Products ──────────────────────────────────────────────────────────────────

def create_product(db: Session, business_id: int, product):
    p = models.Product(
        business_id=business_id,
        name=product.name,
        price=product.price,
        image_url=getattr(product, "image_url", None),
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def get_products(db: Session, business_id: int):
    return (
        db.query(models.Product)
        .filter(models.Product.business_id == business_id)
        .all()
    )


def get_product_price(db: Session, business_id: int, name: str) -> float:
    p = db.query(models.Product).filter(
        models.Product.business_id == business_id,
        models.Product.name.ilike(name),
    ).first()
    return p.price if p else 0.0


def delete_product(db: Session, product_id: int, business_id: int):
    p = db.query(models.Product).filter(
        models.Product.id == product_id,
        models.Product.business_id == business_id,
    ).first()
    if p:
        db.delete(p)
        db.commit()
    return p


# ── Orders ────────────────────────────────────────────────────────────────────

def create_order(db: Session, business_id: int, order):
    total = order.quantity * get_product_price(db, business_id, order.product_name)
    o = models.Order(
        business_id=business_id,
        customer_phone=order.customer_phone,
        product_name=order.product_name,
        quantity=order.quantity,
        total_price=total,
    )
    db.add(o)
    db.commit()
    db.refresh(o)
    return o


def get_orders(db: Session, business_id: int):
    return (
        db.query(models.Order)
        .filter(models.Order.business_id == business_id)
        .order_by(models.Order.id.desc())
        .all()
    )


# ── ChatMessage (legacy) ──────────────────────────────────────────────────────

def log_message(db: Session, business_id: int, phone: str, direction: str, message: str):
    m = models.ChatMessage(
        business_id=business_id,
        phone=phone,
        direction=direction,
        message=message,
    )
    db.add(m)
    db.commit()


def get_conversations(db: Session, business_id: int):
    subq = (
        db.query(
            models.ChatMessage.phone,
            func.max(models.ChatMessage.id).label("max_id"),
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
            models.ChatMessage.phone == phone,
        )
        .order_by(models.ChatMessage.created_at.asc())
        .all()
    )


def get_all_customer_phones(db: Session, business_id: int):
    rows = (
        db.query(distinct(models.ChatMessage.phone))
        .filter(
            models.ChatMessage.business_id == business_id,
            models.ChatMessage.direction == "in",
        )
        .all()
    )
    return [r[0] for r in rows]


# ── Customer (CRM) ────────────────────────────────────────────────────────────

def get_or_create_customer(db: Session, phone: str, business_id: int) -> models.Customer:
    customer = db.query(models.Customer).filter(
        models.Customer.phone == phone,
        models.Customer.business_id == business_id,
    ).first()
    if customer:
        customer.last_seen = datetime.utcnow()
        db.commit()
        return customer
    customer = models.Customer(
        phone=phone,
        business_id=business_id,
        last_seen=datetime.utcnow(),
    )
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


def get_customers_for_business(db: Session, business_id: int, search: str = None):
    q = db.query(models.Customer).filter(
        models.Customer.business_id == business_id
    )
    if search:
        q = q.filter(models.Customer.phone.contains(search))
    return q.order_by(models.Customer.last_seen.desc()).all()


def get_customer_by_id(db: Session, customer_id: int, business_id: int):
    return db.query(models.Customer).filter(
        models.Customer.id == customer_id,
        models.Customer.business_id == business_id,
    ).first()


# ── Message (CRM) ─────────────────────────────────────────────────────────────

def create_message(
    db: Session,
    customer_id: int,
    business_id: int,
    text: str,
    direction: str,
) -> models.Message:
    is_read = direction == "outgoing"
    msg = models.Message(
        customer_id=customer_id,
        business_id=business_id,
        text=text,
        direction=direction,
        is_read=is_read,
        status="sent",
    )
    db.add(msg)

    if direction == "incoming":
        customer = db.query(models.Customer).filter(
            models.Customer.id == customer_id
        ).first()
        if customer:
            customer.unread_count = (customer.unread_count or 0) + 1

    db.commit()
    db.refresh(msg)
    return msg


def get_messages_by_customer(
    db: Session,
    customer_id: int,
    limit: int = 50,
    offset: int = 0,
):
    return (
        db.query(models.Message)
        .filter(models.Message.customer_id == customer_id)
        .order_by(models.Message.created_at.asc())
        .offset(offset)
        .limit(limit)
        .all()
    )


def mark_messages_read(db: Session, customer_id: int, business_id: int):
    db.query(models.Message).filter(
        models.Message.customer_id == customer_id,
        models.Message.business_id == business_id,
        models.Message.direction == "incoming",
        models.Message.is_read == False,
    ).update({"is_read": True, "status": "read"})

    customer = db.query(models.Customer).filter(
        models.Customer.id == customer_id
    ).first()
    if customer:
        customer.unread_count = 0
    db.commit()


def get_chat_conversations(
    db: Session, business_id: int, filter_unread: bool = False
):
    subq = (
        db.query(
            models.Message.customer_id,
            func.max(models.Message.id).label("max_id"),
        )
        .filter(models.Message.business_id == business_id)
        .group_by(models.Message.customer_id)
        .subquery()
    )

    q = (
        db.query(models.Customer, models.Message)
        .join(subq, models.Customer.id == subq.c.customer_id)
        .join(models.Message, models.Message.id == subq.c.max_id)
        .filter(models.Customer.business_id == business_id)
    )

    if filter_unread:
        q = q.filter(models.Customer.unread_count > 0)

    rows = q.order_by(models.Message.id.desc()).all()

    return [
        {
            "customer_id":    customer.id,
            "phone":          customer.phone,
            "customer_since": customer.created_at.isoformat() if customer.created_at else None,
            "last_seen":      customer.last_seen.isoformat() if customer.last_seen else None,
            "unread_count":   customer.unread_count or 0,
            "last_message":   last_msg.text or "",
            "last_direction": last_msg.direction or "",
            "last_message_at": last_msg.created_at.isoformat() if last_msg.created_at else None,
            "last_status":    last_msg.status or "sent",
        }
        for customer, last_msg in rows
    ]
