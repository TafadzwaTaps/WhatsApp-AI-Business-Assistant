"""
billing/messages.py — customer-facing subscription messages, localized.

The backend picks the message from the ACTUAL Stripe result (never from the
browser) and returns it already localized, so the dashboard shows exactly what
happened in the owner's language instead of hardcoding English.

Keys (A–F are the wording requested for cancellation handling):
  cancel_scheduled   A  cancellation set for the end of the paid period   {date}
  cancel_completed   B  cancellation completed, paid access ended
  cancel_failed      C  cancellation request failed, nothing changed
  already_scheduled  D  already scheduled to cancel                       {date}
  already_canceled   E  already canceled
  cancel_reversed    F  scheduled cancellation reversed
  not_cancelling     –  nothing to reverse (no cancellation scheduled)
  no_subscription    –  there is no active subscription

Languages: the generated-site languages (en pl fr pt es de ar) plus Swahili (sw),
which is one of the dashboard's profile-language options. Shona/Ndebele (sn, nd)
are profile options too but have no reviewed translation here, so they fall back
to English rather than shipping unreviewed text.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

log = logging.getLogger(__name__)

DEFAULT_LANG = "en"

MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        "cancel_scheduled": "Your subscription has been set to cancel at the end of your current billing period. You'll continue to enjoy your paid WaziBot features until {date}. You won't be charged for another billing period. You can manage your subscription from your billing settings.",
        "cancel_completed": "Your WaziBot subscription has been canceled. Your paid access has ended. Your business data has not been deleted, and you can choose a plan again whenever you're ready.",
        "cancel_failed": "We couldn't cancel your subscription right now. Your current subscription status has not been changed. Please try again or contact support if the problem continues.",
        "already_scheduled": "Your subscription is already scheduled to end on {date}. Your paid features remain available until then.",
        "already_canceled": "Your subscription is already canceled. You can review available plans if you'd like to reactivate WaziBot.",
        "cancel_reversed": "Your cancellation has been reversed, and your subscription is active according to your current billing schedule.",
        "not_cancelling": "Your subscription is not scheduled to cancel, so there is nothing to reverse.",
        "no_subscription": "You don't have an active subscription.",
    },
    "pl": {
        "cancel_scheduled": "Twoja subskrypcja została ustawiona na anulowanie na koniec bieżącego okresu rozliczeniowego. Do {date} możesz nadal korzystać z płatnych funkcji WaziBot. Za kolejny okres rozliczeniowy nie zostaniesz obciążony(-a). Subskrypcją możesz zarządzać w ustawieniach rozliczeń.",
        "cancel_completed": "Twoja subskrypcja WaziBot została anulowana. Dostęp do płatnych funkcji został zakończony. Dane Twojej firmy nie zostały usunięte i możesz ponownie wybrać plan, kiedy tylko będziesz gotowy(-a).",
        "cancel_failed": "Nie możemy teraz anulować Twojej subskrypcji. Stan Twojej subskrypcji nie został zmieniony. Spróbuj ponownie lub skontaktuj się z pomocą techniczną, jeśli problem będzie się powtarzał.",
        "already_scheduled": "Twoja subskrypcja jest już zaplanowana do zakończenia w dniu {date}. Płatne funkcje pozostają dostępne do tego czasu.",
        "already_canceled": "Twoja subskrypcja została już anulowana. Jeśli chcesz ponownie aktywować WaziBot, możesz przejrzeć dostępne plany.",
        "cancel_reversed": "Anulowanie zostało cofnięte, a Twoja subskrypcja jest aktywna zgodnie z bieżącym harmonogramem rozliczeń.",
        "not_cancelling": "Twoja subskrypcja nie jest zaplanowana do anulowania, więc nie ma czego cofać.",
        "no_subscription": "Nie masz aktywnej subskrypcji.",
    },
    "fr": {
        "cancel_scheduled": "Votre abonnement est configuré pour être annulé à la fin de votre période de facturation en cours. Vous continuerez à profiter de vos fonctionnalités WaziBot payantes jusqu'au {date}. Vous ne serez pas facturé pour une nouvelle période. Vous pouvez gérer votre abonnement depuis vos paramètres de facturation.",
        "cancel_completed": "Votre abonnement WaziBot a été annulé. Votre accès payant a pris fin. Les données de votre entreprise n'ont pas été supprimées et vous pouvez choisir à nouveau une offre quand vous le souhaitez.",
        "cancel_failed": "Nous n'avons pas pu annuler votre abonnement pour le moment. L'état de votre abonnement n'a pas été modifié. Veuillez réessayer ou contacter l'assistance si le problème persiste.",
        "already_scheduled": "Votre abonnement est déjà programmé pour se terminer le {date}. Vos fonctionnalités payantes restent disponibles jusqu'à cette date.",
        "already_canceled": "Votre abonnement est déjà annulé. Vous pouvez consulter les offres disponibles si vous souhaitez réactiver WaziBot.",
        "cancel_reversed": "Votre annulation a été annulée et votre abonnement est actif selon votre calendrier de facturation actuel.",
        "not_cancelling": "Votre abonnement n'est pas programmé pour être annulé, il n'y a donc rien à annuler.",
        "no_subscription": "Vous n'avez pas d'abonnement actif.",
    },
    "pt": {
        "cancel_scheduled": "A sua subscrição foi definida para ser cancelada no final do período de faturação atual. Continuará a usufruir das funcionalidades pagas do WaziBot até {date}. Não será cobrado por outro período de faturação. Pode gerir a sua subscrição nas definições de faturação.",
        "cancel_completed": "A sua subscrição WaziBot foi cancelada. O seu acesso pago terminou. Os dados da sua empresa não foram eliminados e pode escolher um plano novamente quando quiser.",
        "cancel_failed": "Não foi possível cancelar a sua subscrição neste momento. O estado da sua subscrição não foi alterado. Tente novamente ou contacte o suporte se o problema persistir.",
        "already_scheduled": "A sua subscrição já está programada para terminar em {date}. As funcionalidades pagas continuam disponíveis até lá.",
        "already_canceled": "A sua subscrição já está cancelada. Pode consultar os planos disponíveis se quiser reativar o WaziBot.",
        "cancel_reversed": "O cancelamento foi revertido e a sua subscrição está ativa de acordo com o seu ciclo de faturação atual.",
        "not_cancelling": "A sua subscrição não está programada para ser cancelada, por isso não há nada a reverter.",
        "no_subscription": "Não tem uma subscrição ativa.",
    },
    "es": {
        "cancel_scheduled": "Tu suscripción se ha configurado para cancelarse al final de tu período de facturación actual. Seguirás disfrutando de las funciones de pago de WaziBot hasta el {date}. No se te cobrará por otro período de facturación. Puedes gestionar tu suscripción desde la configuración de facturación.",
        "cancel_completed": "Tu suscripción de WaziBot ha sido cancelada. Tu acceso de pago ha finalizado. Los datos de tu negocio no se han eliminado y puedes elegir un plan de nuevo cuando quieras.",
        "cancel_failed": "No hemos podido cancelar tu suscripción en este momento. El estado de tu suscripción no ha cambiado. Inténtalo de nuevo o contacta con soporte si el problema continúa.",
        "already_scheduled": "Tu suscripción ya está programada para finalizar el {date}. Las funciones de pago siguen disponibles hasta entonces.",
        "already_canceled": "Tu suscripción ya está cancelada. Puedes consultar los planes disponibles si quieres reactivar WaziBot.",
        "cancel_reversed": "Tu cancelación se ha revertido y tu suscripción está activa según tu calendario de facturación actual.",
        "not_cancelling": "Tu suscripción no está programada para cancelarse, así que no hay nada que revertir.",
        "no_subscription": "No tienes una suscripción activa.",
    },
    "de": {
        "cancel_scheduled": "Ihr Abonnement wurde so eingestellt, dass es zum Ende Ihres aktuellen Abrechnungszeitraums endet. Bis zum {date} können Sie die kostenpflichtigen WaziBot-Funktionen weiterhin nutzen. Für einen weiteren Abrechnungszeitraum wird Ihnen nichts berechnet. Ihr Abonnement können Sie in den Abrechnungseinstellungen verwalten.",
        "cancel_completed": "Ihr WaziBot-Abonnement wurde gekündigt. Ihr kostenpflichtiger Zugang ist beendet. Ihre Unternehmensdaten wurden nicht gelöscht, und Sie können jederzeit wieder einen Tarif wählen.",
        "cancel_failed": "Wir konnten Ihr Abonnement derzeit nicht kündigen. Der Status Ihres Abonnements wurde nicht geändert. Bitte versuchen Sie es erneut oder wenden Sie sich an den Support, falls das Problem weiterhin besteht.",
        "already_scheduled": "Ihr Abonnement endet bereits am {date}. Die kostenpflichtigen Funktionen bleiben bis dahin verfügbar.",
        "already_canceled": "Ihr Abonnement ist bereits gekündigt. Sie können sich die verfügbaren Tarife ansehen, wenn Sie WaziBot reaktivieren möchten.",
        "cancel_reversed": "Ihre Kündigung wurde rückgängig gemacht, und Ihr Abonnement ist gemäß Ihrem aktuellen Abrechnungsplan aktiv.",
        "not_cancelling": "Ihr Abonnement ist nicht zur Kündigung vorgemerkt, daher gibt es nichts rückgängig zu machen.",
        "no_subscription": "Sie haben kein aktives Abonnement.",
    },
    "ar": {
        "cancel_scheduled": "تم ضبط اشتراكك ليُلغى في نهاية فترة الفوترة الحالية. ستواصل الاستمتاع بميزات WaziBot المدفوعة حتى {date}. لن يتم تحصيل رسوم منك عن فترة فوترة أخرى. يمكنك إدارة اشتراكك من إعدادات الفوترة.",
        "cancel_completed": "تم إلغاء اشتراكك في WaziBot. انتهى وصولك المدفوع. لم يتم حذف بيانات نشاطك التجاري، ويمكنك اختيار خطة مرة أخرى متى كنت مستعدًا.",
        "cancel_failed": "تعذّر علينا إلغاء اشتراكك في الوقت الحالي. لم تتغير حالة اشتراكك. يُرجى المحاولة مرة أخرى أو التواصل مع الدعم إذا استمرت المشكلة.",
        "already_scheduled": "اشتراكك مُجدول بالفعل للانتهاء في {date}. تظل الميزات المدفوعة متاحة حتى ذلك الحين.",
        "already_canceled": "اشتراكك ملغى بالفعل. يمكنك الاطلاع على الخطط المتاحة إذا رغبت في إعادة تفعيل WaziBot.",
        "cancel_reversed": "تم التراجع عن الإلغاء، واشتراكك نشط وفق جدول الفوترة الحالي.",
        "not_cancelling": "اشتراكك غير مجدول للإلغاء، لذا لا يوجد ما يمكن التراجع عنه.",
        "no_subscription": "ليس لديك اشتراك نشط.",
    },
    "sw": {
        "cancel_scheduled": "Usajili wako umewekwa kughairiwa mwishoni mwa kipindi chako cha sasa cha malipo. Utaendelea kufurahia huduma za WaziBot za kulipia hadi {date}. Hutatozwa kwa kipindi kingine cha malipo. Unaweza kudhibiti usajili wako kwenye mipangilio ya malipo.",
        "cancel_completed": "Usajili wako wa WaziBot umeghairiwa. Ufikiaji wako wa kulipia umeisha. Data ya biashara yako haijafutwa, na unaweza kuchagua mpango tena wakati wowote ukiwa tayari.",
        "cancel_failed": "Hatukuweza kughairi usajili wako kwa sasa. Hali ya usajili wako haijabadilishwa. Tafadhali jaribu tena au wasiliana na msaada ikiwa tatizo litaendelea.",
        "already_scheduled": "Usajili wako tayari umepangwa kuisha tarehe {date}. Huduma za kulipia zitaendelea kupatikana hadi wakati huo.",
        "already_canceled": "Usajili wako tayari umeghairiwa. Unaweza kuangalia mipango inayopatikana ukitaka kuwasha upya WaziBot.",
        "cancel_reversed": "Kughairi kwako kumebatilishwa, na usajili wako unatumika kulingana na ratiba yako ya sasa ya malipo.",
        "not_cancelling": "Usajili wako haujapangwa kughairiwa, kwa hivyo hakuna cha kubatilisha.",
        "no_subscription": "Huna usajili unaotumika.",
    },
}

_MONTHS: dict[str, tuple[str, ...]] = {
    "en": ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"),
    "pl": ("stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca", "lipca", "sierpnia", "września", "października", "listopada", "grudnia"),
    "fr": ("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"),
    "pt": ("janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"),
    "es": ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"),
    "de": ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"),
    "ar": ("يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو", "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"),
    "sw": ("Januari", "Februari", "Machi", "Aprili", "Mei", "Juni", "Julai", "Agosti", "Septemba", "Oktoba", "Novemba", "Desemba"),
}
_DATE_FMT = {"en": "{month} {d}, {y}", "de": "{d}. {month} {y}", "es": "{d} de {month} de {y}",
             "pt": "{d} de {month} de {y}"}          # everything else: "{d} {month} {y}"


def normalize_lang(lang: Optional[str]) -> str:
    code = (lang or "").strip().lower()[:2]
    return code if code in MESSAGES else DEFAULT_LANG


def format_date(value, lang: str = DEFAULT_LANG) -> str:
    """Localized calendar date from an ISO string / datetime. '' if unparseable —
    callers must not show a made-up date."""
    if not value:
        return ""
    try:
        d = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return ""
    lang = normalize_lang(lang)
    return _DATE_FMT.get(lang, "{d} {month} {y}").format(d=d.day, month=_MONTHS[lang][d.month - 1], y=d.year)


def billing_message(key: str, lang: Optional[str] = None, date=None) -> str:
    """Localized message for `key`. {date} is filled from a REAL date only; if the
    date is unknown it renders as an em dash rather than an invented date, so callers
    should only use the dated keys (cancel_scheduled, already_scheduled) when Stripe
    supplied one."""
    lang = normalize_lang(lang)
    text = MESSAGES[lang].get(key) or MESSAGES[DEFAULT_LANG][key]
    if "{date}" in text:
        text = text.replace("{date}", format_date(date, lang) or "—")
    return text


def business_language(business_id: int) -> str:
    """The owner's profile language (features_json.language) if we have a reviewed
    translation for it, else English. Never raises."""
    try:
        from core.db import supabase
        res = (supabase.table("businesses").select("features_json")
               .eq("id", business_id).limit(1).execute())
        fj = (res.data or [{}])[0].get("features_json")
        return normalize_lang(fj.get("language") if isinstance(fj, dict) else None)
    except Exception as exc:
        log.debug("business_language lookup failed: %s", type(exc).__name__)
        return DEFAULT_LANG
