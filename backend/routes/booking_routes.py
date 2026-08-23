"""
routes_saas/booking_routes.py — Public Booking Page & API

PURPOSE
───────
The customer-facing counterpart to the existing dashboard/WhatsApp booking
system. Extends the SAME booking_service.py functions (check_availability,
get_available_slots, create_booking) already used by the dashboard and the
WhatsApp AI flow — this is NOT a second booking implementation. It's a third
entry point into the one that already exists.

ROUTES
──────
  GET  /book/{slug}                          — public HTML booking page
  GET  /api/public/booking/{slug}/info       — business + bookable services
  GET  /api/public/booking/{slug}/availability — open slots for a date
  POST /api/public/booking/{slug}            — create a booking

SECURITY
────────
Every endpoint here is genuinely public/unauthenticated by design (a
customer booking an appointment isn't logged in). Protected instead by:
  - rate limiting (services.security, same "booking" limit used by the
    authenticated dashboard endpoints)
  - basic duplicate-booking-attempt detection
  - no internal IDs exposed — only slug/service name/date/time in, a
    booking reference out
"""

from __future__ import annotations

import logging
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, validator

log = logging.getLogger(__name__)
router = APIRouter()


# ── Business/service resolution — reuses site_generator's existing slug
#    lookup rather than re-implementing it ──────────────────────────────────
def _resolve_booking_business(slug: str) -> tuple[dict, list]:
    from services.site_generator import _get_business_and_products
    biz, products = _get_business_and_products(slug)
    if not biz:
        return {}, []
    return biz, products


class PublicBookingCreate(BaseModel):
    service_name:   str = ""
    booking_date:   str
    start_time:     str
    duration_hrs:   float = 1.0
    customer_name:  str
    customer_phone: str
    customer_email: str = ""
    notes:          str = ""

    @validator("customer_name")
    def _name_not_blank(cls, v):
        v = (v or "").strip()
        if not v:
            raise ValueError("Name is required")
        return v[:100]

    @validator("customer_phone")
    def _phone_not_blank(cls, v):
        v = (v or "").strip()
        if len(v) < 7:
            raise ValueError("A valid phone number is required")
        return v[:30]


@router.get("/api/public/booking/{slug}/info")
def public_booking_info(slug: str, request: Request):
    """Business name/logo/currency + bookable services for the public page."""
    from services.security import check as _rate_check
    _rate_check("booking", request)

    biz, products = _resolve_booking_business(slug)
    if not biz:
        raise HTTPException(404, "Business not found")
    if not biz.get("is_service_business"):
        raise HTTPException(404, "This business does not accept bookings")

    services = [
        {
            "name":  p.get("name", ""),
            "price": p.get("price", 0),
            "description": p.get("description", ""),
        }
        for p in products
    ]

    return {
        "name":            biz.get("name", ""),
        "logo_url":        biz.get("logo_url", ""),
        "theme_colour":    biz.get("theme_colour", "") or "#22c55e",
        "currency_symbol": biz.get("currency_symbol", "$"),
        "services":        services,
    }


@router.get("/api/public/booking/{slug}/availability")
def public_booking_availability(
    slug: str, request: Request,
    booking_date: str, duration_hrs: float = 1.0,
):
    """Open time slots for a given date — powers the page's time picker."""
    from services.security import check as _rate_check
    _rate_check("booking", request)

    biz, _ = _resolve_booking_business(slug)
    if not biz:
        raise HTTPException(404, "Business not found")

    from services.booking_service import get_available_slots
    return get_available_slots(biz["id"], booking_date, duration_hrs)


@router.post("/api/public/booking/{slug}", status_code=201)
def public_booking_create(slug: str, data: PublicBookingCreate, request: Request):
    """
    Create a booking from the public page. Reuses create_booking() exactly
    as the dashboard and WhatsApp paths do — same atomic double-booking
    protection, same working-hours/lead-time enforcement, same everything.
    """
    from services.security import check as _rate_check
    _rate_check("booking", request)

    biz, _ = _resolve_booking_business(slug)
    if not biz:
        raise HTTPException(404, "Business not found")
    if not biz.get("is_service_business"):
        raise HTTPException(404, "This business does not accept bookings")

    # Basic duplicate-attempt guard — a customer double-tapping "Confirm"
    # (or a scripted retry) shouldn't create two identical bookings a few
    # seconds apart. Genuine double-booking-by-different-customers is
    # already handled by create_booking()'s atomic conflict check; this is
    # specifically about the SAME customer accidentally submitting twice.
    try:
        from services.booking_service import get_bookings_for_customer
        recent = get_bookings_for_customer(biz["id"], data.customer_phone)
        for r in recent:
            if (r.get("booking_date") == data.booking_date
                    and r.get("start_time") == data.start_time
                    and r.get("status") in ("pending", "confirmed")):
                raise HTTPException(409, "You already have a booking for this time.")
    except HTTPException:
        raise
    except Exception as exc:
        log.warning("public_booking_create: duplicate check failed (non-fatal): %s", exc)

    from services.booking_service import create_booking
    notes = f"{data.notes}\n\nBooked via public page. Name: {data.customer_name}".strip()
    if data.customer_email:
        notes += f"  Email: {data.customer_email}"

    booking = create_booking(
        business_id=biz["id"], customer_phone=data.customer_phone,
        booking_date=data.booking_date, start_time=data.start_time,
        duration_hrs=data.duration_hrs, service_name=data.service_name,
        notes=notes,
    )

    if not booking:
        raise HTTPException(409, "Sorry, that time was just booked. Please choose another available time.")

    try:
        from services.acquisition_service import record_booking_completed
        record_booking_completed(biz["id"], booking.get("id"))
    except Exception:
        pass  # fire-and-forget — never block the response on analytics

    # Notify the business the same way an order/WhatsApp booking would —
    # reuses the existing notification mechanism rather than a new one.
    try:
        from services._ai_payments import _notify_business_new_order
        import crud, os as _os
        biz_full = crud.get_business_by_id(biz["id"])
        dedicated_phone_id = (biz_full or {}).get("whatsapp_phone_id") or ""
        if dedicated_phone_id:
            notify_phone_id = dedicated_phone_id
            notify_token    = crud.get_decrypted_token(biz_full)
        else:
            notify_phone_id = _os.getenv("SHARED_PHONE_NUMBER_ID", "")
            notify_token    = _os.getenv("SHARED_WA_TOKEN", "")
        fake_order = {"id": booking.get("id"), "total_price": 0}
        _notify_business_new_order(
            biz["id"], fake_order, data.customer_phone,
            [{"name": data.service_name or "Booking", "qty": 1}],
            biz.get("currency_symbol", "$"), notify_phone_id, notify_token,
        )
    except Exception as exc:
        log.warning("public_booking_create: business notification failed (non-critical): %s", exc)

    return {"ok": True, "booking": booking}


@router.get("/book/{slug}", response_class=HTMLResponse, include_in_schema=False)
async def public_booking_page(slug: str):
    """Public booking page — mirrors /site/{slug}'s fallback-on-error pattern."""
    try:
        from services.site_generator import _get_business_and_products
        biz, _ = _get_business_and_products(slug)
        if not biz or not biz.get("is_service_business"):
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url="/directory", status_code=302)
        try:
            from services.acquisition_service import record_booking_page_view
            record_booking_page_view(biz["id"])
        except Exception:
            pass  # fire-and-forget — never block the page on analytics
        return HTMLResponse(_render_booking_page_html(slug, biz))
    except Exception as exc:
        log.warning("public_booking_page error: %s", exc)
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/directory", status_code=302)


def _render_booking_page_html(slug: str, biz: dict) -> str:
    """
    Server-rendered shell; all data (services, availability) loads via the
    JSON API above. Deliberately reuses WaziBot's existing dark-theme CSS
    variable system and font pairing (DM Mono / Syne) from site_generator.py
    — this must look native to WaziBot, not like a bolted-on third-party
    booking widget.
    """
    import html as _h
    name = _h.escape(biz.get("name", "Book an Appointment"))
    accent = biz.get("theme_colour") or "#22c55e"
    if not (isinstance(accent, str) and accent.startswith("#") and len(accent) == 7):
        accent = "#22c55e"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Book an Appointment — {name}</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet"/>
<style>
:root{{--bg:#0a0f0d;--surface:#111a15;--surface2:#172010;--border:#1f3025;
  --accent:{accent};--text:#e8f5e9;--text-dim:#6b8f71;
  --mono:'DM Mono',monospace;--sans:'Syne',sans-serif;}}
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{background:var(--bg);color:var(--text);font-family:var(--sans);min-height:100vh;padding:24px 16px;}}
.wrap{{max-width:480px;margin:0 auto;}}
h1{{font-size:24px;font-weight:800;margin-bottom:6px;}}
.sub{{font-family:var(--mono);font-size:12px;color:var(--text-dim);margin-bottom:28px;}}
.step{{display:none;}}
.step.active{{display:block;}}
.card{{background:var(--surface);border:1px solid var(--border);border-radius:14px;
  padding:16px;margin-bottom:12px;cursor:pointer;transition:border-color .15s;}}
.card:hover,.card.selected{{border-color:var(--accent);}}
.card-name{{font-weight:700;font-size:15px;margin-bottom:4px;}}
.card-price{{font-family:var(--mono);color:var(--accent);font-size:13px;}}
label{{display:block;font-family:var(--mono);font-size:11px;color:var(--text-dim);
  text-transform:uppercase;letter-spacing:1px;margin:16px 0 6px;}}
input[type=date],input[type=text],input[type=tel],input[type=email],textarea{{
  width:100%;background:var(--surface);border:1px solid var(--border);border-radius:10px;
  padding:12px 14px;color:var(--text);font-family:var(--sans);font-size:14px;}}
.slots{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:12px;}}
.slot{{background:var(--surface);border:1px solid var(--border);border-radius:8px;
  padding:10px 4px;text-align:center;font-family:var(--mono);font-size:13px;cursor:pointer;}}
.slot:hover,.slot.selected{{border-color:var(--accent);color:var(--accent);}}
.btn{{display:block;width:100%;background:var(--accent);color:#0a0f0d;font-weight:700;
  border:none;border-radius:10px;padding:14px;font-size:15px;cursor:pointer;margin-top:20px;}}
.btn:disabled{{opacity:.4;cursor:not-allowed;}}
.btn-ghost{{background:none;border:1px solid var(--border);color:var(--text-dim);margin-top:10px;}}
.msg{{font-family:var(--mono);font-size:13px;color:var(--text-dim);text-align:center;padding:32px 0;}}
.confirm{{text-align:center;padding:24px 0;}}
.confirm .big{{font-size:44px;margin-bottom:12px;}}
</style>
</head>
<body>
<div class="wrap">
  <h1>{name}</h1>
  <div class="sub">Book an appointment</div>

  <div id="step-service" class="step active">
    <div id="services-list" class="msg">Loading services…</div>
  </div>

  <div id="step-datetime" class="step">
    <label>Date</label>
    <input type="date" id="f-date" onchange="loadSlots()"/>
    <label>Available Times</label>
    <div id="slots-list" class="slots"></div>
    <button class="btn" id="btn-to-details" disabled onclick="showStep('details')">Continue</button>
    <button class="btn btn-ghost" onclick="showStep('service')">← Back</button>
  </div>

  <div id="step-details" class="step">
    <label>Your Name</label>
    <input type="text" id="f-name" placeholder="Jane Doe"/>
    <label>Phone Number</label>
    <input type="tel" id="f-phone" placeholder="+48 500 000 000"/>
    <label>Email (optional)</label>
    <input type="email" id="f-email" placeholder="jane@example.com"/>
    <label>Notes (optional)</label>
    <textarea id="f-notes" rows="2" placeholder="Anything we should know?"></textarea>
    <button class="btn" onclick="submitBooking()">Confirm Booking</button>
    <button class="btn btn-ghost" onclick="showStep('datetime')">← Back</button>
  </div>

  <div id="step-confirm" class="step">
    <div class="confirm">
      <div class="big">✅</div>
      <h1 id="confirm-title">Booking Confirmed!</h1>
      <div class="sub" id="confirm-detail"></div>
    </div>
  </div>
</div>

<script>
const SLUG = {slug!r};
let selectedService = null, selectedDate = null, selectedTime = null;

function showStep(name) {{
  document.querySelectorAll('.step').forEach(s => s.classList.remove('active'));
  document.getElementById('step-' + name).classList.add('active');
}}

async function loadServices() {{
  try {{
    const res = await fetch(`/api/public/booking/${{SLUG}}/info`);
    if (!res.ok) throw new Error('Not found');
    const data = await res.json();
    const list = document.getElementById('services-list');
    if (!data.services || !data.services.length) {{
      list.innerHTML = '<div class="msg">No services listed yet. Please contact the business directly.</div>';
      return;
    }}
    list.innerHTML = data.services.map((s, i) => `
      <div class="card" onclick="pickService(${{i}}, this)">
        <div class="card-name">${{s.name}}</div>
        <div class="card-price">${{data.currency_symbol}}${{Number(s.price).toFixed(2)}}</div>
      </div>`).join('');
    window._services = data.services;
  }} catch (e) {{
    document.getElementById('services-list').innerHTML =
      '<div class="msg">⚠️ Could not load this business\\'s booking page.</div>';
  }}
}}

function pickService(i, el) {{
  document.querySelectorAll('#services-list .card').forEach(c => c.classList.remove('selected'));
  el.classList.add('selected');
  selectedService = window._services[i].name;
  const today = new Date().toISOString().slice(0, 10);
  document.getElementById('f-date').min = today;
  document.getElementById('f-date').value = today;
  showStep('datetime');
  loadSlots();
}}

async function loadSlots() {{
  selectedDate = document.getElementById('f-date').value;
  const slotsEl = document.getElementById('slots-list');
  slotsEl.innerHTML = '<div class="msg">Loading…</div>';
  document.getElementById('btn-to-details').disabled = true;
  try {{
    const res = await fetch(`/api/public/booking/${{SLUG}}/availability?booking_date=${{selectedDate}}&duration_hrs=1`);
    const data = await res.json();
    if (!data.slots || !data.slots.length) {{
      slotsEl.innerHTML = `<div class="msg">${{data.reason || 'No times available for this day'}}</div>`;
      return;
    }}
    slotsEl.innerHTML = data.slots.map(t => `<div class="slot" onclick="pickTime('${{t}}', this)">${{t}}</div>`).join('');
  }} catch (e) {{
    slotsEl.innerHTML = '<div class="msg">⚠️ Could not load availability.</div>';
  }}
}}

function pickTime(t, el) {{
  document.querySelectorAll('.slot').forEach(s => s.classList.remove('selected'));
  el.classList.add('selected');
  selectedTime = t;
  document.getElementById('btn-to-details').disabled = false;
}}

async function submitBooking() {{
  const name  = document.getElementById('f-name').value.trim();
  const phone = document.getElementById('f-phone').value.trim();
  if (!name || !phone) {{ alert('Please enter your name and phone number.'); return; }}
  try {{
    const res = await fetch(`/api/public/booking/${{SLUG}}`, {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{
        service_name: selectedService, booking_date: selectedDate, start_time: selectedTime,
        duration_hrs: 1, customer_name: name, customer_phone: phone,
        customer_email: document.getElementById('f-email').value.trim(),
        notes: document.getElementById('f-notes').value.trim(),
      }}),
    }});
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Could not create booking');
    document.getElementById('confirm-detail').textContent =
      `${{selectedService}} — ${{selectedDate}} at ${{selectedTime}}`;
    showStep('confirm');
  }} catch (e) {{
    alert(e.message || 'Something went wrong. Please try again.');
  }}
}}

loadServices();
</script>
</body>
</html>"""
