"""
services/account_deletion.py — self-service account deletion.

Order of operations matters here, and it's the opposite of this
codebase's usual fail-soft convention on purpose: everywhere else,
persistence failures must never block the real action (a login still
works if the audit log write fails). Here, the backup and its record
ARE the safety guarantee the business was promised, so they must
succeed BEFORE the one irreversible-from-the-business's-side step
(deactivating the account) happens:

    1. build a CSV backup of everything this business owns
    2. upload that backup to Supabase Storage
    3. record where it is + when it becomes eligible for permanent purge
    4. ONLY THEN deactivate the business and invalidate its sessions

If step 1-3 fail for any reason, request_deletion() raises and NOTHING
is deactivated — the business keeps working and can just try again,
rather than ending up deactivated with no backup anywhere.

Purging the backup + the business row itself, 90 days later, is a
separate scheduled job (routes_saas/billing_routes.py
purge_deleted_accounts), not this module — this module only ever
creates the backup and flips the account into "pending purge".
"""

from __future__ import annotations

import csv
import io
import logging
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Optional

from core.db import supabase

log = logging.getLogger("wazibot.account_deletion")

PURGE_AFTER_DAYS = 90
BACKUP_BUCKET = "account-backups"

# Each business-owned table backed up on deletion, with an explicit field
# allowlist — same reasoning as services/data_export.py's EXPORT_DATASETS:
# never include secrets (password hash, encrypted WhatsApp token) even
# though this backup is only ever handed back to the business itself or
# support, not published anywhere.
_BACKUP_DATASETS: dict[str, dict] = {
    "business_profile": {
        "table": "businesses",
        "fields": ["id", "name", "owner_username", "owner_email", "category",
                   "contact_phone", "tagline", "logo_url", "theme_colour",
                   "currency", "subscription_tier", "billing_status",
                   "created_at"],
        "filter_field": "id",
    },
    "products": {
        "table": "products",
        "fields": ["id", "name", "price", "image_url", "stock", "low_stock_threshold"],
        "filter_field": "business_id",
    },
    "orders": {
        "table": "orders",
        "fields": ["id", "customer_phone", "product_name", "quantity", "items",
                   "total_price", "status", "payment_status", "created_at"],
        "filter_field": "business_id",
    },
    "customers": {
        "table": "customers",
        "fields": ["id", "phone", "created_at", "last_seen", "unread_count"],
        "filter_field": "business_id",
    },
    "chat_messages": {
        "table": "chat_messages",
        "fields": ["id", "phone", "direction", "message", "created_at"],
        "filter_field": "business_id",
    },
}


def _fetch_dataset(name: str, cfg: dict, business_id: int) -> Optional[list[dict]]:
    """None means the table doesn't exist / query failed — skipped, not
    fatal, since not every deployment has every optional table. Returning
    [] (empty list) is a real "no rows", which still counts as backed up."""
    try:
        res = (
            supabase.table(cfg["table"])
            .select(",".join(cfg["fields"]))
            .eq(cfg["filter_field"], business_id)
            .limit(20000)
            .execute()
        )
        return res.data or []
    except Exception as exc:
        log.warning("account_deletion: could not fetch %s for business_id=%s: %s", name, business_id, exc)
        return None


def build_backup_zip(business_id: int) -> bytes:
    """
    Builds an in-memory ZIP of one CSV per dataset. Raises RuntimeError if
    NOTHING could be fetched at all (e.g. total DB outage) — a backup that
    backed up nothing is not a backup, and the caller must not proceed to
    deactivate the account on the strength of it.
    """
    buf = io.BytesIO()
    datasets_included = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, cfg in _BACKUP_DATASETS.items():
            rows = _fetch_dataset(name, cfg, business_id)
            if rows is None:
                continue  # table missing/query failed — noted in the log, not fatal
            datasets_included += 1
            csv_buf = io.StringIO()
            writer = csv.DictWriter(csv_buf, fieldnames=cfg["fields"], extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
            zf.writestr(f"{name}.csv", csv_buf.getvalue())
        zf.writestr(
            "README.txt",
            "This is your WaziBot account data backup, generated automatically "
            "when your account deletion was requested. It is kept for "
            f"{PURGE_AFTER_DAYS} days from the request date and then permanently "
            "deleted along with your account. Contact support before then if "
            "you need your account restored.\n",
        )
    if datasets_included == 0:
        raise RuntimeError("Could not read any account data — refusing to proceed without a real backup")
    return buf.getvalue()


def upload_backup(business_id: int, zip_bytes: bytes) -> str:
    """Raises on failure — an unstored backup is the same as no backup."""
    timestamp = int(datetime.now(timezone.utc).timestamp())
    path = f"{business_id}/{timestamp}.zip"
    try:
        supabase.storage.create_bucket(BACKUP_BUCKET, options={"public": False})
    except Exception:
        pass  # already exists, or this Supabase project restricts bucket creation — upload attempt below is the real check
    supabase.storage.from_(BACKUP_BUCKET).upload(
        path, zip_bytes, {"content-type": "application/zip", "upsert": "true"},
    )
    return path


def deactivate_and_invalidate_sessions(business_id: int) -> None:
    """
    Flips is_active off (blocks future logins — already-enforced at
    auth_routes.py login) AND bumps password_changed_at, which reuses the
    existing session-invalidation check in core/auth.get_current_user(): any
    access/refresh token already issued embeds the OLD password_changed_at
    as its pwd_ts claim, so within the existing ~60s cache window every
    outstanding session stops working too — deletion doesn't wait out an
    8-hour token lifetime.
    """
    import crud
    now_iso = datetime.now(timezone.utc).isoformat()

    class _D:
        def dict(self, **_):
            return {"is_active": False, "password_changed_at": now_iso}

    crud.update_business(business_id, _D())


def request_deletion(*, business_id: int, business_row: dict, reason: str) -> dict:
    """
    Full flow: backup -> upload -> record -> THEN deactivate. Raises on
    any failure in the first three steps (nothing is deactivated in that
    case). Returns {"purge_after": <iso>, "backup_format": "csv"} on success.
    """
    import crud.account_deletions as deletions_crud

    zip_bytes = build_backup_zip(business_id)          # raises if truly nothing backed up
    backup_path = upload_backup(business_id, zip_bytes)  # raises if storage write fails

    purge_after = (datetime.now(timezone.utc) + timedelta(days=PURGE_AFTER_DAYS)).isoformat()
    deletions_crud.insert_deletion_record(                # raises if the record can't be written
        business_id=business_id,
        business_name=business_row.get("name", ""),
        owner_username=business_row.get("owner_username", ""),
        owner_email=business_row.get("owner_email", ""),
        reason=reason,
        backup_path=backup_path,
        backup_format="csv",
        purge_after_iso=purge_after,
    )

    # Only now, with a confirmed backup + record in hand, do we actually
    # deactivate the account and cut off its sessions.
    deactivate_and_invalidate_sessions(business_id)

    log.info("account deletion requested  business_id=%s  purge_after=%s  backup=%s",
              business_id, purge_after, backup_path)
    return {"purge_after": purge_after, "backup_format": "csv"}


def purge_due_accounts(limit: int = 200) -> dict:
    """
    Called by the scheduled job (routes_saas/billing_routes.py). For every
    account_deletions row past its purge_after that's still pending:
    delete the backup file from storage, hard-delete the business row,
    mark the record purged. Each account is handled independently — one
    failure doesn't stop the rest of the batch.
    """
    import crud
    import crud.account_deletions as deletions_crud

    now_iso = datetime.now(timezone.utc).isoformat()
    due = deletions_crud.list_due_for_purge(now_iso, limit=limit)
    purged, errors = 0, 0
    for record in due:
        biz_id = record.get("business_id")
        try:
            backup_path = record.get("backup_path")
            if backup_path:
                try:
                    supabase.storage.from_(BACKUP_BUCKET).remove([backup_path])
                except Exception as exc:
                    log.warning("purge: could not remove backup %s: %s", backup_path, exc)
            if biz_id is not None:
                crud.delete_business(biz_id)
            deletions_crud.mark_purged(record["id"])
            purged += 1
            log.info("account permanently purged  business_id=%s  record_id=%s", biz_id, record.get("id"))
        except Exception as exc:
            errors += 1
            log.error("purge failed for business_id=%s: %s", biz_id, exc)
    return {"purged": purged, "errors": errors, "checked": len(due)}
