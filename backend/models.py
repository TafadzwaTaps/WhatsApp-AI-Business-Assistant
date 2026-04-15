from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from database import Base

class Business(Base):
    __tablename__ = "businesses"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    owner_username = Column(String, unique=True, nullable=False)
    owner_password = Column(String, nullable=False)
    whatsapp_phone_id = Column(String, unique=True, nullable=True)  # Meta Phone Number ID
    whatsapp_token = Column(String, nullable=True)                  # Meta Access Token
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())

    products = relationship("Product", back_populates="business", cascade="all, delete")
    orders = relationship("Order", back_populates="business", cascade="all, delete")
    messages = relationship("ChatMessage", back_populates="business", cascade="all, delete")


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

# ADD THIS

class WhatsAppCredential(Base):
    __tablename__ = "whatsapp_credentials"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True)
    phone_number_id = Column(String, unique=True, index=True)
    access_token = Column(String)