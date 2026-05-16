"""Payment helpers — Midtrans status verification and tier upgrades.

Both the Celery webhook task and the synchronous /verify endpoint share these
helpers so the upgrade logic stays in sync. All operations are idempotent
(a paid user upgrading twice doesn't double-grant quota).
"""

import base64
import logging
from typing import Optional

import httpx
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Transaction, User

logger = logging.getLogger(__name__)


# Quota granted on Pro upgrade — 10 hours per the product spec.
PRO_QUOTA_BONUS_SECONDS = 36000  # 10 hours

# Statuses that indicate the payment landed successfully
SUCCESS_STATUSES = {"settlement", "capture"}


def _is_sandbox() -> bool:
    """Determine Midtrans environment.

    Priority:
      1. Explicit ``MIDTRANS_ENV`` env var (``sandbox`` or ``production``).
      2. Key prefix heuristic — ``SB-`` indicates sandbox.
      3. Default: sandbox (safer default for dev).

    NOTE: Midtrans **production** server keys typically start with
    ``Mid-server-``, but their **sandbox** keys ALSO often start with
    ``Mid-server-`` rather than ``SB-Mid-server-``. The prefix heuristic
    alone is therefore unreliable; always set ``MIDTRANS_ENV`` explicitly
    in your ``.env``.
    """
    settings = get_settings()
    explicit = (settings.midtrans_env or "").strip().lower()
    if explicit in ("sandbox", "sb", "dev", "development"):
        return True
    if explicit in ("production", "prod", "live"):
        return False

    # Fallback heuristic
    key = (settings.midtrans_server_key or "").strip()
    if key.startswith("SB-"):
        return True
    # Anything ambiguous → sandbox (safe default for unintended dev runs)
    return True


def get_midtrans_api_base() -> str:
    """Return the appropriate Midtrans API base URL for the current key."""
    return (
        "https://api.sandbox.midtrans.com"
        if _is_sandbox()
        else "https://api.midtrans.com"
    )


def get_midtrans_snap_base() -> str:
    """Return the appropriate Midtrans Snap base URL for the current key."""
    return (
        "https://app.sandbox.midtrans.com"
        if _is_sandbox()
        else "https://app.midtrans.com"
    )


def _auth_header() -> str:
    settings = get_settings()
    server_key = settings.midtrans_server_key or ""
    auth_string = base64.b64encode(f"{server_key}:".encode()).decode()
    return f"Basic {auth_string}"


def fetch_midtrans_status(order_id: str, timeout: float = 10.0) -> dict:
    """Call Midtrans Status API for a given order_id.

    Returns the parsed JSON response. Raises ``RuntimeError`` on non-2xx.
    """
    url = f"{get_midtrans_api_base()}/v2/{order_id}/status"
    headers = {
        "Accept": "application/json",
        "Authorization": _auth_header(),
    }
    with httpx.Client(timeout=timeout) as client:
        resp = client.get(url, headers=headers)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Midtrans status check failed: {resp.status_code} {resp.text[:300]}"
            )
        return resp.json()


def apply_payment_to_user(
    db: Session,
    transaction: Transaction,
    transaction_status: str,
    payment_type: Optional[str] = None,
) -> tuple[bool, str]:
    """Idempotently apply a Midtrans payment outcome to the user's account.

    - Updates ``Transaction.status`` and ``payment_type`` always.
    - On success status (``settlement`` / ``capture``) and only if not yet
      processed, upgrades the user to Pro and increments their quota.

    Returns ``(upgraded, message)``.
    """
    # Track whether this transaction has already been credited.
    already_credited = transaction.status in SUCCESS_STATUSES

    transaction.status = transaction_status
    if payment_type:
        transaction.payment_type = payment_type

    if transaction_status not in SUCCESS_STATUSES:
        return False, f"Status {transaction_status} — no upgrade applied"

    if already_credited:
        return False, "Already credited"

    user = db.query(User).filter(User.id == transaction.user_id).first()
    if not user:
        logger.error(
            f"[payment] Transaction {transaction.order_id} references missing "
            f"user_id={transaction.user_id}"
        )
        return False, "User not found"

    user.subscription_tier = "pro"
    user.quota_seconds_remaining = (
        (user.quota_seconds_remaining or 0) + PRO_QUOTA_BONUS_SECONDS
    )
    logger.info(
        f"[payment] Upgraded user {user.id} ({user.email}) to Pro and granted "
        f"+{PRO_QUOTA_BONUS_SECONDS}s quota (order_id={transaction.order_id})"
    )
    return True, "Upgraded to Pro"
