"""
services/_ai_lazy.py — Lazy module accessors for ai.py.

All imports are deferred to call-time to avoid circular imports at module level.
These five functions are the only public API of this file.
"""

import logging

log = logging.getLogger(__name__)


def _states():
    from services import conversation_service as conversation_states
    return conversation_states


def _fuzzy():
    import utils.fuzzy_matcher as fuzzy_matcher
    return fuzzy_matcher


def _order_parser():
    """Lazy import of order_parser_service to avoid circular imports."""
    try:
        from services.order_parser_service import parse_order, build_order_preview
        return parse_order, build_order_preview
    except ImportError:
        from order_parser_service import parse_order, build_order_preview
        return parse_order, build_order_preview


def _sales_ai():
    """Lazy import of sales_ai_service to avoid circular imports."""
    try:
        from services.sales_ai_service import (
            get_suggestions, get_basket_suggestions,
            get_upsell, format_suggestion_text,
        )
        return get_suggestions, get_basket_suggestions, get_upsell, format_suggestion_text
    except ImportError:
        try:
            from sales_ai_service import (
                get_suggestions, get_basket_suggestions,
                get_upsell, format_suggestion_text,
            )
            return get_suggestions, get_basket_suggestions, get_upsell, format_suggestion_text
        except ImportError:
            return None, None, None, None


def _handoff_mod():
    from workflows import human_handoff
    return human_handoff
