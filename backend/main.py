from fastapi import FastAPI
from database import Base, engine
import models
from whatsapp import router as whatsapp_router

app = FastAPI()

# Create tables
Base.metadata.create_all(bind=engine)

app.include_router(whatsapp_router, prefix="/whatsapp")

@app.get("/")
def root():
    return {"message": "WhatsApp AI SaaS is running 🚀"}