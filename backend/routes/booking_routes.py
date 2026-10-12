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


# Booking-page UI text. Same languages and the same `wz_lang` visitor preference as
# the generated website (services/site_generator.SITE_I18N_LANGS), so a visitor who
# picked a language on the site lands on a booking page in that language.
# Business-owned content (business/service names) and server-provided availability
# reasons / API error details are NOT translated here - see the audit report.
BOOKING_I18N = {
  "en": {"book_title": "Book an appointment", "loading_services": "Loading services…",
         "no_services": "No services listed yet. Please contact the business directly.",
         "load_failed": "Could not load this business's booking page.", "date": "Date",
         "times": "Available Times", "cont": "Continue", "back": "← Back", "your_name": "Your Name",
         "phone": "Phone Number", "email_opt": "Email (optional)", "notes_opt": "Notes (optional)",
         "notes_ph": "Anything we should know?", "confirm": "Confirm Booking",
         "confirmed": "Booking Confirmed!", "loading": "Loading…",
         "no_times": "No times available for this day", "avail_failed": "Could not load availability.",
         "need_name_phone": "Please enter your name and phone number.",
         "booking_failed": "Could not create booking",
         "went_wrong": "Something went wrong. Please try again.", "at": "at"},
  "pl": {"book_title": "Umów wizytę", "loading_services": "Ładowanie usług…",
         "no_services": "Brak usług na liście. Skontaktuj się bezpośrednio z firmą.",
         "load_failed": "Nie udało się wczytać strony rezerwacji tej firmy.", "date": "Data",
         "times": "Dostępne godziny", "cont": "Dalej", "back": "← Wstecz", "your_name": "Imię i nazwisko",
         "phone": "Numer telefonu", "email_opt": "E-mail (opcjonalnie)", "notes_opt": "Uwagi (opcjonalnie)",
         "notes_ph": "Czy jest coś, co powinniśmy wiedzieć?", "confirm": "Potwierdź rezerwację",
         "confirmed": "Rezerwacja potwierdzona!", "loading": "Ładowanie…",
         "no_times": "Brak dostępnych godzin w tym dniu", "avail_failed": "Nie udało się wczytać dostępności.",
         "need_name_phone": "Podaj imię i nazwisko oraz numer telefonu.",
         "booking_failed": "Nie udało się utworzyć rezerwacji",
         "went_wrong": "Coś poszło nie tak. Spróbuj ponownie.", "at": "o godz."},
  "fr": {"book_title": "Prendre rendez-vous", "loading_services": "Chargement des services…",
         "no_services": "Aucun service n'est encore répertorié. Veuillez contacter directement l'entreprise.",
         "load_failed": "Impossible de charger la page de réservation de cette entreprise.", "date": "Date",
         "times": "Horaires disponibles", "cont": "Continuer", "back": "← Retour", "your_name": "Votre nom",
         "phone": "Numéro de téléphone", "email_opt": "E-mail (facultatif)", "notes_opt": "Notes (facultatif)",
         "notes_ph": "Y a-t-il quelque chose que nous devrions savoir ?", "confirm": "Confirmer la réservation",
         "confirmed": "Réservation confirmée !", "loading": "Chargement…",
         "no_times": "Aucun horaire disponible ce jour-là",
         "avail_failed": "Impossible de charger les disponibilités.",
         "need_name_phone": "Veuillez saisir votre nom et votre numéro de téléphone.",
         "booking_failed": "Impossible de créer la réservation",
         "went_wrong": "Une erreur s'est produite. Veuillez réessayer.", "at": "à"},
  "pt": {"book_title": "Marcar uma consulta", "loading_services": "A carregar serviços…",
         "no_services": "Ainda não há serviços listados. Contacte diretamente a empresa.",
         "load_failed": "Não foi possível carregar a página de reservas desta empresa.", "date": "Data",
         "times": "Horários disponíveis", "cont": "Continuar", "back": "← Voltar", "your_name": "O seu nome",
         "phone": "Número de telefone", "email_opt": "E-mail (opcional)", "notes_opt": "Notas (opcional)",
         "notes_ph": "Há algo que devamos saber?", "confirm": "Confirmar reserva",
         "confirmed": "Reserva confirmada!", "loading": "A carregar…",
         "no_times": "Sem horários disponíveis neste dia",
         "avail_failed": "Não foi possível carregar a disponibilidade.",
         "need_name_phone": "Introduza o seu nome e número de telefone.",
         "booking_failed": "Não foi possível criar a reserva",
         "went_wrong": "Algo correu mal. Tente novamente.", "at": "às"},
  "es": {"book_title": "Reservar una cita", "loading_services": "Cargando servicios…",
         "no_services": "Aún no hay servicios publicados. Ponte en contacto directamente con el negocio.",
         "load_failed": "No se pudo cargar la página de reservas de este negocio.", "date": "Fecha",
         "times": "Horarios disponibles", "cont": "Continuar", "back": "← Volver", "your_name": "Tu nombre",
         "phone": "Número de teléfono", "email_opt": "Correo electrónico (opcional)",
         "notes_opt": "Notas (opcional)", "notes_ph": "¿Hay algo que debamos saber?",
         "confirm": "Confirmar reserva", "confirmed": "¡Reserva confirmada!", "loading": "Cargando…",
         "no_times": "No hay horarios disponibles para este día",
         "avail_failed": "No se pudo cargar la disponibilidad.",
         "need_name_phone": "Introduce tu nombre y número de teléfono.",
         "booking_failed": "No se pudo crear la reserva",
         "went_wrong": "Algo salió mal. Inténtalo de nuevo.", "at": "a las"},
  "de": {"book_title": "Termin buchen", "loading_services": "Leistungen werden geladen…",
         "no_services": "Noch keine Leistungen aufgelistet. Bitte kontaktieren Sie das Unternehmen direkt.",
         "load_failed": "Die Buchungsseite dieses Unternehmens konnte nicht geladen werden.", "date": "Datum",
         "times": "Verfügbare Zeiten", "cont": "Weiter", "back": "← Zurück", "your_name": "Ihr Name",
         "phone": "Telefonnummer", "email_opt": "E-Mail (optional)", "notes_opt": "Notizen (optional)",
         "notes_ph": "Gibt es etwas, das wir wissen sollten?", "confirm": "Buchung bestätigen",
         "confirmed": "Buchung bestätigt!", "loading": "Wird geladen…",
         "no_times": "Für diesen Tag sind keine Zeiten verfügbar",
         "avail_failed": "Die Verfügbarkeit konnte nicht geladen werden.",
         "need_name_phone": "Bitte geben Sie Ihren Namen und Ihre Telefonnummer ein.",
         "booking_failed": "Die Buchung konnte nicht erstellt werden",
         "went_wrong": "Etwas ist schiefgelaufen. Bitte versuchen Sie es erneut.", "at": "um"},
  "ar": {"book_title": "حجز موعد", "loading_services": "جارٍ تحميل الخدمات…",
         "no_services": "لا توجد خدمات مدرجة بعد. يُرجى التواصل مع النشاط التجاري مباشرة.",
         "load_failed": "تعذّر تحميل صفحة الحجز لهذا النشاط التجاري.", "date": "التاريخ",
         "times": "الأوقات المتاحة", "cont": "متابعة", "back": "→ رجوع", "your_name": "اسمك",
         "phone": "رقم الهاتف", "email_opt": "البريد الإلكتروني (اختياري)", "notes_opt": "ملاحظات (اختياري)",
         "notes_ph": "هل هناك أي شيء يجب أن نعرفه؟", "confirm": "تأكيد الحجز",
         "confirmed": "تم تأكيد الحجز!", "loading": "جارٍ التحميل…",
         "no_times": "لا توجد أوقات متاحة في هذا اليوم", "avail_failed": "تعذّر تحميل التوافر.",
         "need_name_phone": "يُرجى إدخال اسمك ورقم هاتفك.", "booking_failed": "تعذّر إنشاء الحجز",
         "went_wrong": "حدث خطأ ما. يُرجى المحاولة مرة أخرى.", "at": "في"},
}


def _render_booking_page_html(slug: str, biz: dict) -> str:
    """
    Server-rendered shell; all data (services, availability) loads via the
    JSON API above. Deliberately reuses WaziBot's existing dark-theme CSS
    variable system and font pairing (DM Mono / Syne) from site_generator.py
    — this must look native to WaziBot, not like a bolted-on third-party
    booking widget.
    """
    import html as _h
    import json as _json
    name = _h.escape(biz.get("name", "Book an Appointment"))
    try:
        from services.site_generator import _site_language, _get_site_settings, SITE_I18N_RTL_LANGS
        _lang = _site_language(biz, _get_site_settings(biz.get("features_json")))
        _dir = "rtl" if _lang in SITE_I18N_RTL_LANGS else "ltr"
    except Exception:
        _lang, _dir = "en", "ltr"
    # JSON for embedding in <script>: "</" can never close the tag.
    _safe = lambda v: _json.dumps(v, ensure_ascii=False).replace("</", "<\\/")
    _i18n_json, _slug_js = _safe(BOOKING_I18N), _safe(slug)
    _name_js, _lang_js = _safe(biz.get("name", "")), _safe(_lang)
    accent = biz.get("theme_colour") or "#22c55e"
    if not (isinstance(accent, str) and accent.startswith("#") and len(accent) == 7):
        accent = "#22c55e"

    return f"""<!DOCTYPE html>
<html lang="{_lang}" dir="{_dir}">
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
  <div class="sub" data-i18n="book_title">Book an appointment</div>

  <div id="step-service" class="step active">
    <div id="services-list" class="msg" data-i18n="loading_services">Loading services…</div>
  </div>

  <div id="step-datetime" class="step">
    <label data-i18n="date">Date</label>
    <input type="date" id="f-date" onchange="loadSlots()"/>
    <label data-i18n="times">Available Times</label>
    <div id="slots-list" class="slots"></div>
    <button class="btn" id="btn-to-details" disabled onclick="showStep('details')" data-i18n="cont">Continue</button>
    <button class="btn btn-ghost" onclick="showStep('service')" data-i18n="back">← Back</button>
  </div>

  <div id="step-details" class="step">
    <label data-i18n="your_name">Your Name</label>
    <input type="text" id="f-name" placeholder="Jane Doe" maxlength="120"/>
    <label data-i18n="phone">Phone Number</label>
    <input type="tel" id="f-phone" placeholder="+48 500 000 000" maxlength="40"/>
    <label data-i18n="email_opt">Email (optional)</label>
    <input type="email" id="f-email" placeholder="jane@example.com" maxlength="200"/>
    <label data-i18n="notes_opt">Notes (optional)</label>
    <textarea id="f-notes" rows="2" placeholder="Anything we should know?" data-i18n-ph="notes_ph" maxlength="1000"></textarea>
    <button class="btn" onclick="submitBooking()" data-i18n="confirm">Confirm Booking</button>
    <button class="btn btn-ghost" onclick="showStep('datetime')" data-i18n="back">← Back</button>
  </div>

  <div id="step-confirm" class="step">
    <div class="confirm">
      <div class="big">✅</div>
      <h1 id="confirm-title" data-i18n="confirmed">Booking Confirmed!</h1>
      <div class="sub" id="confirm-detail"></div>
    </div>
  </div>
</div>

<script>
const SLUG = {_slug_js};
const BK_I18N = {_i18n_json};
const BK_DEFAULT = {_lang_js};
const BK_NAME = {_name_js};
const BK_RTL = ['ar'];
let _bkLang = BK_DEFAULT;
let selectedService = null, selectedDate = null, selectedTime = null;

function esc(v) {{
  return String(v == null ? '' : v).replace(/[&<>"']/g, c =>
    ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
}}
function _bt(key) {{
  return (BK_I18N[_bkLang] && BK_I18N[_bkLang][key]) || BK_I18N.en[key] || key;
}}
function _bkApply(lang) {{
  _bkLang = BK_I18N[lang] ? lang : 'en';
  document.documentElement.setAttribute('lang', _bkLang);
  document.documentElement.setAttribute('dir', BK_RTL.includes(_bkLang) ? 'rtl' : 'ltr');
  document.querySelectorAll('[data-i18n]').forEach(el => {{
    const k = el.getAttribute('data-i18n'); if (BK_I18N[_bkLang][k]) el.textContent = BK_I18N[_bkLang][k];
  }});
  document.querySelectorAll('[data-i18n-ph]').forEach(el => {{
    const k = el.getAttribute('data-i18n-ph'); if (BK_I18N[_bkLang][k]) el.setAttribute('placeholder', BK_I18N[_bkLang][k]);
  }});
  document.title = _bt('book_title') + ' — ' + BK_NAME;
}}
(function _bkInit() {{
  let lang = BK_DEFAULT;
  try {{
    const saved = localStorage.getItem('wz_lang');
    if (saved && BK_I18N[saved]) lang = saved;
    else {{
      const b = (navigator.language || 'en').slice(0, 2).toLowerCase();
      if (BK_I18N[b]) lang = b;
    }}
  }} catch (e) {{}}
  _bkApply(lang);
}})();

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
      list.innerHTML = '<div class="msg">' + esc(_bt('no_services')) + '</div>';
      return;
    }}
    list.innerHTML = data.services.map((s, i) => `
      <div class="card" onclick="pickService(${{i}}, this)">
        <div class="card-name">${{esc(s.name)}}</div>
        <div class="card-price">${{esc(data.currency_symbol)}}${{Number(s.price).toFixed(2)}}</div>
      </div>`).join('');
    window._services = data.services;
  }} catch (e) {{
    document.getElementById('services-list').innerHTML =
      '<div class="msg">⚠️ ' + esc(_bt('load_failed')) + '</div>';
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
  slotsEl.innerHTML = '<div class="msg">' + esc(_bt('loading')) + '</div>';
  document.getElementById('btn-to-details').disabled = true;
  try {{
    const res = await fetch(`/api/public/booking/${{SLUG}}/availability?booking_date=${{selectedDate}}&duration_hrs=1`);
    const data = await res.json();
    if (!data.slots || !data.slots.length) {{
      slotsEl.innerHTML = `<div class="msg">${{esc(data.reason || _bt('no_times'))}}</div>`;
      return;
    }}
    slotsEl.innerHTML = data.slots.map(t => `<div class="slot" data-t="${{esc(t)}}" onclick="pickTime(this.dataset.t, this)">${{esc(t)}}</div>`).join('');
  }} catch (e) {{
    slotsEl.innerHTML = '<div class="msg">⚠️ ' + esc(_bt('avail_failed')) + '</div>';
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
  if (!name || !phone) {{ alert(_bt('need_name_phone')); return; }}
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
    if (!res.ok) throw new Error((typeof data.detail === 'string' && data.detail) || _bt('booking_failed'));
    document.getElementById('confirm-detail').textContent =
      `${{selectedService}} — ${{selectedDate}} ${{_bt('at')}} ${{selectedTime}}`;
    showStep('confirm');
  }} catch (e) {{
    alert(e.message || _bt('went_wrong'));
  }}
}}

loadServices();
</script>
</body>
</html>"""
