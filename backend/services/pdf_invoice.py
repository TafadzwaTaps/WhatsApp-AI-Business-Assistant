# pdf_invoice.py
"""
PDF invoice generator using reportlab.platypus only.
Saves to: invoices/invoice_{order_id}.pdf
Returns the file path.
"""

import json
import os
import logging
from datetime import datetime

from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable,
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4

log = logging.getLogger(__name__)

INVOICES_DIR = "invoices"


def _ensure_dir() -> None:
    os.makedirs(INVOICES_DIR, exist_ok=True)


def _parse_items(items_raw) -> list:
    """Parse items from JSON string or list. Returns list of dicts."""
    if not items_raw:
        return []
    try:
        if isinstance(items_raw, str):
            return json.loads(items_raw)
        if isinstance(items_raw, list):
            return items_raw
    except Exception as exc:
        log.warning("pdf_invoice: could not parse items — %s", exc)
    return []


def generate_pdf_invoice(order: dict) -> str:
    """
    Generate a PDF invoice for an order dict.

    Required keys in order: id, items, total_price (or total), customer_phone, status
    Optional: payment_status, payment_reference, business_name

    Returns the path to the generated PDF file.
    """
    _ensure_dir()

    order_id       = order.get("id", "N/A")
    items_raw      = order.get("items", "")
    total          = float(order.get("total_price") or order.get("total") or 0)
    status         = order.get("status", "pending")
    payment_status = order.get("payment_status", "pending")
    payment_ref    = order.get("payment_reference") or f"ORDER-{order_id}"
    customer_phone = order.get("customer_phone", "")
    business_name  = order.get("business_name", "")
    date_str       = datetime.now().strftime("%Y-%m-%d %H:%M")

    file_path = os.path.join(INVOICES_DIR, f"invoice_{order_id}.pdf")

    doc = SimpleDocTemplate(
        file_path,
        pagesize=A4,
        rightMargin=0.75 * inch,
        leftMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
    )

    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "InvoiceTitle",
        parent=styles["Title"],
        fontSize=22,
        spaceAfter=4,
        textColor=colors.HexColor("#1a1a2e"),
    )
    heading_style = ParagraphStyle(
        "SectionHead",
        parent=styles["Heading2"],
        fontSize=12,
        spaceBefore=8,
        spaceAfter=4,
        textColor=colors.HexColor("#16213e"),
    )
    normal_style = styles["Normal"]
    small_style = ParagraphStyle(
        "Small",
        parent=styles["Normal"],
        fontSize=9,
        textColor=colors.grey,
    )

    story = []

    # ── Header ──────────────────────────────────────────────────────────────
    if business_name:
        story.append(Paragraph(business_name, heading_style))
    story.append(Paragraph("INVOICE", title_style))
    story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor("#1a1a2e")))
    story.append(Spacer(1, 8))

    # ── Meta info table ──────────────────────────────────────────────────────
    meta_data = [
        ["Order ID",  f"#{order_id}",     "Date",   date_str],
        ["Customer",  customer_phone,      "Status", status.upper()],
        ["Payment",   payment_status.upper(), "",    ""],
    ]
    meta_table = Table(meta_data, colWidths=[1.1 * inch, 2.2 * inch, 1 * inch, 2.2 * inch])
    meta_table.setStyle(TableStyle([
        ("FONTNAME",    (0, 0), (-1, -1), "Helvetica"),
        ("FONTNAME",    (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME",    (2, 0), (2, -1), "Helvetica-Bold"),
        ("FONTSIZE",    (0, 0), (-1, -1), 9),
        ("VALIGN",      (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",  (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("TEXTCOLOR",   (0, 0), (0, -1), colors.HexColor("#444444")),
        ("TEXTCOLOR",   (2, 0), (2, -1), colors.HexColor("#444444")),
    ]))
    story.append(meta_table)
    story.append(Spacer(1, 12))

    # ── Items table ──────────────────────────────────────────────────────────
    story.append(Paragraph("Order Items", heading_style))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.lightgrey))
    story.append(Spacer(1, 4))

    items = _parse_items(items_raw)
    item_rows = [["Item", "Qty", "Unit Price", "Subtotal"]]

    if items:
        for item in items:
            name     = item.get("name", "?")
            qty      = item.get("qty", item.get("quantity", 1))
            price    = float(item.get("price", 0))
            subtotal = float(item.get("subtotal", price * qty))
            item_rows.append([name, str(qty), f"${price:.2f}", f"${subtotal:.2f}"])
    else:
        item_rows.append(["—", "—", "—", "—"])

    col_widths = [3.0 * inch, 0.6 * inch, 1.1 * inch, 1.1 * inch]
    items_table = Table(item_rows, colWidths=col_widths)
    items_table.setStyle(TableStyle([
        # Header row
        ("BACKGROUND",    (0, 0), (-1, 0), colors.HexColor("#1a1a2e")),
        ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
        ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, 0), 10),
        ("ALIGN",         (1, 0), (-1, 0), "CENTER"),
        ("TOPPADDING",    (0, 0), (-1, 0), 6),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
        # Data rows
        ("FONTNAME",      (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE",      (0, 1), (-1, -1), 9),
        ("ALIGN",         (1, 1), (-1, -1), "CENTER"),
        ("ALIGN",         (2, 1), (-1, -1), "RIGHT"),
        ("ALIGN",         (3, 1), (-1, -1), "RIGHT"),
        ("TOPPADDING",    (0, 1), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 5),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8f8f8")]),
        ("GRID",          (0, 0), (-1, -1), 0.5, colors.lightgrey),
    ]))
    story.append(items_table)
    story.append(Spacer(1, 8))

    # ── Total ────────────────────────────────────────────────────────────────
    total_data = [["", "", "TOTAL", f"${total:.2f}"]]
    total_table = Table(total_data, colWidths=col_widths)
    total_table.setStyle(TableStyle([
        ("FONTNAME",      (0, 0), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, -1), 11),
        ("ALIGN",         (2, 0), (-1, -1), "RIGHT"),
        ("TEXTCOLOR",     (2, 0), (-1, -1), colors.HexColor("#1a1a2e")),
        ("TOPPADDING",    (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEABOVE",     (2, 0), (-1, 0), 1.5, colors.HexColor("#1a1a2e")),
    ]))
    story.append(total_table)
    story.append(Spacer(1, 16))

    # ── Payment instructions ─────────────────────────────────────────────────
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.lightgrey))
    story.append(Spacer(1, 6))
    story.append(Paragraph("Payment Instructions", heading_style))
    story.append(Paragraph("Pay via EcoCash / Mobile Money", normal_style))
    story.append(Spacer(1, 4))

    ref_style = ParagraphStyle(
        "Ref",
        parent=styles["Normal"],
        fontSize=13,
        fontName="Helvetica-Bold",
        textColor=colors.HexColor("#e94560"),
    )
    story.append(Paragraph(f"Reference: {payment_ref}", ref_style))
    story.append(Spacer(1, 4))
    story.append(Paragraph(
        "Please use the exact reference when making payment so we can confirm your order automatically.",
        small_style,
    ))
    story.append(Spacer(1, 16))

    # ── Footer ───────────────────────────────────────────────────────────────
    story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#1a1a2e")))
    story.append(Spacer(1, 6))
    story.append(Paragraph("Thank you for your order! 🙏", normal_style))

    doc.build(story)
    log.info("📄 PDF invoice generated  path=%s  order_id=%s", file_path, order_id)
    return file_path
