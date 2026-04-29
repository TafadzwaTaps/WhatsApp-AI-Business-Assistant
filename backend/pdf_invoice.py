# pdf_invoice.py

from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from datetime import datetime
import os


def generate_pdf_invoice(order):
    if not os.path.exists("invoices"):
        os.makedirs("invoices")

    file_path = f"invoices/invoice_{order['id']}.pdf"

    doc = SimpleDocTemplate(file_path)
    styles = getSampleStyleSheet()

    content = []

    content.append(Paragraph("INVOICE", styles["Title"]))
    content.append(Spacer(1, 12))

    content.append(Paragraph(f"Order ID: {order['id']}", styles["Normal"]))
    content.append(Paragraph(f"Date: {datetime.now()}", styles["Normal"]))
    content.append(Spacer(1, 12))

    content.append(Paragraph(f"Items: {order['items']}", styles["Normal"]))
    content.append(Spacer(1, 12))

    content.append(Paragraph(f"Total: ${order['total']}", styles["Heading2"]))
    content.append(Spacer(1, 12))

    content.append(Paragraph("Pay via EcoCash", styles["Normal"]))
    content.append(Paragraph(f"Reference: ORDER-{order['id']}", styles["Normal"]))

    doc.build(content)

    return file_path