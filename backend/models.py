from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Boolean, UniqueConstraint, Index
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from database import Base


class Business(Base):
    __tablename__ = "businesses"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    owner_username = Column(String, unique=True, nullable=False)
    owner_password = Column(String, nullable=False)
    whatsapp_phone_id = Column(String, unique=True, nullable=True)
    whatsapp_token = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())

    products = relationship("Product", back_populates="business", cascade="all, delete")
    orders = relationship("Order", back_populates="business", cascade="all, delete")
    messages = relationship("ChatMessage", back_populates="business", cascade="all, delete")
    customers = relationship("Customer", back_populates="business", cascade="all, delete")


class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False)
    name = Column(String)
    price = Column(Float)

    business = relationship("Business", back_populates="products")


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False)
    customer_phone = Column(String)
    product_name = Column(String)
    quantity = Column(Integer)
    total_price = Column(Float)
    status = Column(String, default="pending")
    created_at = Column(DateTime, server_default=func.now())

    business = relationship("Business", back_populates="orders")


class ChatMessage(Base):
    """Legacy table — keeps existing dashboard working. Do not remove."""
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False)
    phone = Column(String, index=True)
    direction = Column(String)   # "in" or "out"
    message = Column(String)
    created_at = Column(DateTime, server_default=func.now())

    business = relationship("Business", back_populates="messages")


class Customer(Base):
    __tablename__ = "customers"

    id = Column(Integer, primary_key=True, index=True)
    phone = Column(String, nullable=False)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False)
    created_at = Column(DateTime, server_default=func.now())
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
    direction = Column(String, nullable=False)   # "incoming" or "outgoing"
    is_read = Column(Boolean, default=False)
    status = Column(String, default="sent")       # sent | delivered | read
    created_at = Column(DateTime, server_default=func.now())

    customer = relationship("Customer", back_populates="customer_messages")

    __table_args__ = (
        Index("ix_message_customer_created", "customer_id", "created_at"),
        Index("ix_message_business_created", "business_id", "created_at"),
    )
