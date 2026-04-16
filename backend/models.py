from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Boolean, UniqueConstraint
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
    created_at = Column(DateTime, server_default=func.now())

    business = relationship("Business", back_populates="orders")


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, index=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False)
    phone = Column(String, index=True)
    direction = Column(String)   # "in" or "out"
    message = Column(String)
    created_at = Column(DateTime, server_default=func.now())

    business = relationship("Business", back_populates="messages")


# ── Customer: one record per unique WhatsApp number per business ──
class Customer(Base):
    __tablename__ = "customers"

    id = Column(Integer, primary_key=True, index=True)
    phone = Column(String, nullable=False)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False)
    created_at = Column(DateTime, server_default=func.now())

    business = relationship("Business", back_populates="customers")
    customer_messages = relationship("Message", back_populates="customer", cascade="all, delete")

    __table_args__ = (
        UniqueConstraint("phone", "business_id", name="uq_customer_phone_business"),
    )


# ── Message: every individual message, incoming or outgoing ──
class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False)
    text = Column(String, nullable=False)
    direction = Column(String, nullable=False)   # "incoming" or "outgoing"
    created_at = Column(DateTime, server_default=func.now())

    customer = relationship("Customer", back_populates="customer_messages")
