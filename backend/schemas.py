from pydantic import BaseModel

class ProductCreate(BaseModel):
    name: str
    price: float

class OrderCreate(BaseModel):
    customer_phone: str
    product_name: str
    quantity: int