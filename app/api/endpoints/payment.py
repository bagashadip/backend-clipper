"""Payment endpoints for Midtrans Snap integration.

Two endpoints:

  * ``POST /api/payment/snap-token`` — generate a Snap token for the frontend
    Snap.js modal.
  * ``POST /api/payment/verify``     — frontend-driven payment verification.
    Called from the Snap ``onSuccess`` callback. Hits the Midtrans Status
    API directly so we don't have to wait for the webhook to arrive
    (especially useful behind ephemeral tunnels like Cloudflare quick
    tunnels where the webhook URL changes on every restart).

The webhook at ``/api/midtrans`` remains the canonical async path; ``verify``
is an idempotent backstop. Both share the same upgrade helper.
"""

import base64
import logging
import time
import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import Transaction, User
from app.services.auth import get_current_user
from app.services.payment import (
    apply_payment_to_user,
    fetch_midtrans_status,
    get_midtrans_snap_base,
)

logger = logging.getLogger(__name__)
router = APIRouter()


class SnapTokenRequest(BaseModel):
    tier_id: str
    amount: float


class VerifyPaymentRequest(BaseModel):
    order_id: str


@router.post("/snap-token")
async def generate_snap_token(
    request: SnapTokenRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    settings = get_settings()
    server_key = settings.midtrans_server_key

    if not server_key:
        raise HTTPException(status_code=500, detail="Midtrans server key not configured")

    # Max 50 chars enforced by Midtrans — format is always ~25 chars
    order_id = f"ZC-{uuid.uuid4().hex[:10]}-{int(time.time())}"

    # Save pending transaction to DB
    transaction = Transaction(
        user_id=user.id,
        order_id=order_id,
        gross_amount=request.amount,
        status="pending",
    )
    db.add(transaction)
    db.commit()

    # Create Midtrans Snap Token
    auth_string = base64.b64encode(f"{server_key}:".encode()).decode()
    url = f"{get_midtrans_snap_base()}/snap/v1/transactions"

    payload = {
        "transaction_details": {
            "order_id": order_id,
            "gross_amount": int(request.amount),
        },
        "customer_details": {"email": user.email},
        "custom_field1": request.tier_id,
    }

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Basic {auth_string}",
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload, headers=headers)

        if response.status_code != 201:
            logger.error(
                f"[snap-token] Failed to generate token: {response.status_code} {response.text}"
            )
            raise HTTPException(
                status_code=500,
                detail=f"Failed to generate snap token: {response.text}",
            )

        data = response.json()
        return {"snap_token": data.get("token")}


@router.post("/verify")
def verify_payment(
    request: VerifyPaymentRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Frontend-driven payment verification.

    The frontend calls this from Snap's ``onSuccess`` callback with the
    order_id from the payment result. We:

      1. Look up the Transaction by order_id (must belong to the caller).
      2. Hit the Midtrans Status API to confirm the payment landed.
      3. If status is ``settlement``/``capture`` and not already credited,
         upgrade the user to Pro and grant quota.

    Idempotent — a duplicate call after the webhook already credited the
    user is a no-op.
    """
    transaction = (
        db.query(Transaction).filter(Transaction.order_id == request.order_id).first()
    )
    if not transaction:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if transaction.user_id != user.id:
        # Don't leak existence — return same error
        raise HTTPException(status_code=404, detail="Transaction not found")

    # Hit Midtrans to confirm
    try:
        status_payload = fetch_midtrans_status(request.order_id)
    except Exception as e:
        logger.error(f"[verify] Status check failed for {request.order_id}: {e}")
        raise HTTPException(
            status_code=502,
            detail="Could not reach Midtrans to verify payment. Please try again.",
        )

    transaction_status = status_payload.get("transaction_status") or "pending"
    payment_type = status_payload.get("payment_type")

    upgraded, msg = apply_payment_to_user(
        db,
        transaction,
        transaction_status,
        payment_type=payment_type,
    )
    db.commit()

    # Re-fetch to return the user's current state
    db.refresh(user)

    logger.info(
        f"[verify] order_id={request.order_id} midtrans_status={transaction_status} → {msg}"
    )

    return {
        "status": transaction_status,
        "upgraded": upgraded,
        "message": msg,
        "subscription_tier": user.subscription_tier,
        "quota_seconds_remaining": user.quota_seconds_remaining,
    }


@router.post("/reconcile")
def reconcile_pending_payments(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Reconcile all of the caller's recent non-final transactions.

    Useful as a "Did my payment go through?" recovery action — for example
    when the webhook URL was stale/unreachable and the frontend missed the
    onSuccess callback. Idempotent: already-credited transactions stay
    untouched.
    """
    # Look at recent transactions that aren't in a terminal-success state.
    pending = (
        db.query(Transaction)
        .filter(
            Transaction.user_id == user.id,
            Transaction.status.notin_(["settlement", "capture"]),
        )
        .order_by(Transaction.created_at.desc())
        .limit(10)
        .all()
    )

    results: list[dict] = []
    for tx in pending:
        try:
            payload = fetch_midtrans_status(tx.order_id)
        except Exception as e:
            logger.warning(
                f"[reconcile] Skipping {tx.order_id} — status fetch failed: {e}"
            )
            results.append({"order_id": tx.order_id, "error": str(e)[:200]})
            continue

        status = payload.get("transaction_status") or "pending"
        payment_type = payload.get("payment_type")
        upgraded, msg = apply_payment_to_user(db, tx, status, payment_type=payment_type)
        db.commit()
        results.append(
            {
                "order_id": tx.order_id,
                "status": status,
                "upgraded": upgraded,
                "message": msg,
            }
        )

    db.refresh(user)
    return {
        "subscription_tier": user.subscription_tier,
        "quota_seconds_remaining": user.quota_seconds_remaining,
        "checked": results,
    }
