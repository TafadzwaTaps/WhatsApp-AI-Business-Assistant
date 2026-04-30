from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Boolean, UniqueConstraint, Index, Numeric, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from database import Base


class Business(Base):
    __tablename__ = "businesses"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    owner_username = Column(String, unique=True, nullable=False)
    owner_password = Column(String, nullable=False)

    whatsapp_phone_id = Column(String, nullable=True)
    whatsapp_token = Column(String, nullable=True)

    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow, server_default=func.now())

    products = relationship("Product", back_populates="business", cascade="all, delete")
    orders = relationship("Order", back_populates="business", cascade="all, delete")
    messages = relationship("ChatMessage", back_populates="business", cascade="all, delete")
    customers = relationship("Customer", back_populates="business", cascade="all, delete")


class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False)

    name = Column(String, nullable=False)
    price = Column(Numeric(10, 2), nullable=False)
    image_url = Column(String, nullable=True)

    stock = Column(Integer, default=0)
    low_stock_threshold = Column(Integer, default=5)

    business = relationship("Business", back_populates="products")

    __table_args__ = (
        Index("ix_product_business", "business_id"),
    )


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False)

    customer_phone = Column(String, nullable=False)
    product_name = Column(String, nullable=False)
    quantity = Column(Integer, nullable=False)

    # Full serialised cart: "[{name, qty, price, subtotal}, ...]"
    items = Column(Text, nullable=True)

    total_price = Column(Numeric(10, 2), nullable=False)

    # Lifecycle: pending → confirmed → paid → delivered
    status = Column(String, default="pending")

    # Payment tracking
    payment_status = Column(String, default="pending")       # pending | paid
    payment_reference = Column(String, nullable=True)        # ORDER-{id}

    created_at = Column(DateTime, default=datetime.utcnow, server_default=func.now())

    business = relationship("Business", back_populates="orders")

    __table_args__ = (
        Index("ix_order_business", "business_id"),
    )

    @property
    def total(self):
        return self.total_price


class ChatMessage(Base):
    """Legacy table — keep for compatibility"""
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False)

    phone = Column(String, index=True)
    direction = Column(String)  # incoming | outgoing
    message = Column(String, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, server_default=func.now())

    business = relationship("Business", back_populates="messages")


class Customer(Base):
    __tablename__ = "customers"

    id = Column(Integer, primary_key=True, index=True)
    phone = Column(String, nullable=False)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, server_default=func.now())
    last_seen = Column(DateTime, nullable=True)
    unread_count = Column(Integer, default=0)

    business = relationship("Business", back_populates="customers")
    customer_messages = relationship("Message", back_populates="customer", cascade="all, delete")

    __table_args__ = (
        UniqueConstraint("phone", "business_id", name="uq_customer_phone_business"),
        Index("ix_customer_business", "business_id"),
    )


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False)

    text = Column(String, nullable=False)
    direction = Column(String, nullable=False)  # incoming | outgoing

    is_read = Column(Boolean, default=False)
    status = Column(String, default="sent")  # sent | delivered | read

    created_at = Column(DateTime, default=datetime.utcnow, server_default=func.now())

    customer = relationship("Customer", back_populates="customer_messages")
    business = relationship("Business")

    __table_args__ = (
        Index("ix_message_customer_created", "customer_id", "created_at"),
        Index("ix_message_business_created", "business_id", "created_at"),
    )
