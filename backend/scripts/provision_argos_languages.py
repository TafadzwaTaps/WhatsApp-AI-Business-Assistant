#!/usr/bin/env python3
"""
backend/scripts/provision_argos_languages.py

One-time, MANUALLY-RUN provisioning script that downloads and installs
only the Argos Translate language-pair models WaziBot's site generator
actually supports (services.translation_service.SUPPORTED_SITE_LANGS) —
deliberately NOT "every available Argos language", per the fix spec this
was written against.

This is NOT run automatically by the application, by a migration, or by
any deploy hook — consistent with this codebase's existing convention of
never auto-running database migrations or deploy steps (see
backend/migrations/0001_bookings_schema_and_atomic_rpc.sql's own header
for the same principle applied to the database). An operator runs this
by hand, once, after installing the optional dependency:

    pip install -r backend/requirements-translation.txt
    python backend/scripts/provision_argos_languages.py

It requires outbound network access to Argos's model index
(https://www.argosopentech.com) — the sandboxed environment this fix was
developed in does not have that access, so this script has been written
and reviewed but NOT executed end-to-end here; translation_service.py's
graceful "no models installed yet" fallback path is what was actually
exercised and tested in this environment (see
backend/tests/test_translation_service.py). Run this for real in an
environment with normal internet access (e.g. a Render build shell) before
relying on live Argos translation in production.

WHICH LANGUAGE PAIRS ARE INSTALLED
───────────────────────────────────
Argos models translate a single source→target pair each. WaziBot only
ever translates business-supplied content that starts out in whatever
language the business wrote it in (source_lang passed into
translate_text(), defaulting to "en") into one of the site generator's
supported target languages. Rather than install every combination of the
7 supported languages (42 directed pairs), this script installs:
  - English → every other supported language (the overwhelmingly common
    case: WaziBot businesses write their descriptions in English)
  - every other supported language → English (so a business who wrote
    their own description in, say, Portuguese can still have it shown
    in English/other languages)
This covers the realistic set WaziBot needs without over-provisioning.
If a business's source language is something other than English and a
non-English target is requested, translate_text() already degrades
gracefully to the original text (no crash, no missing content) — that is
a known, documented limitation, not a silent bug.
"""
from __future__ import annotations

import sys

# Keep in sync with services.translation_service.SUPPORTED_SITE_LANGS —
# imported directly (not hardcoded here) so the two can never drift.
sys.path.insert(0, __file__.rsplit("/backend/", 1)[0] + "/backend")
from services.translation_service import SUPPORTED_SITE_LANGS  # noqa: E402


def main() -> int:
    try:
        import argostranslate.package
    except ImportError:
        print(
            "argostranslate is not installed. Run:\n"
            "  pip install -r backend/requirements-translation.txt\n"
            "before running this script.",
            file=sys.stderr,
        )
        return 1

    targets = [l for l in SUPPORTED_SITE_LANGS if l != "en"]
    pairs = [("en", t) for t in targets] + [(t, "en") for t in targets]

    print("Updating Argos package index (requires network access)…")
    argostranslate.package.update_package_index()
    available = argostranslate.package.get_available_packages()

    installed, skipped, failed = [], [], []
    for from_code, to_code in pairs:
        match = next(
            (p for p in available if p.from_code == from_code and p.to_code == to_code),
            None,
        )
        if not match:
            skipped.append((from_code, to_code))
            continue
        try:
            print(f"Installing {from_code} → {to_code}…")
            path = match.download()
            argostranslate.package.install_from_path(path)
            installed.append((from_code, to_code))
        except Exception as exc:
            print(f"  FAILED {from_code} → {to_code}: {exc}", file=sys.stderr)
            failed.append((from_code, to_code))

    print(f"\nInstalled: {installed}")
    if skipped:
        print(f"No model available in Argos's index for: {skipped}")
    if failed:
        print(f"Failed to install: {failed}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
