"""Celery worker tasks for payment processing.

The Midtrans webhook handler enqueues ``process_midtrans_webhook`` so that
the HTTP response returns immediately. The actual DB update (transaction
status + tier upgrade) runs here in the worker.

All upgrades go through ``app.services.payment.apply_payment_to_user`` so
the synchronous ``POST /api/payment/verify`` endpoint and this webhook share
the same idempotent logic.
"""

import logging

from app.tasks.celery_app import celery_app
from app.database import SessionLocal
from app.models import Transaction
from app.services.payment import apply_payment_to_user

logger = logging.getLogger(__name__)


@celery_app.task(name="tasks.process_midtrans_webhook")
def process_midtrans_webhook(payload: dict):
    """Process a verified Midtrans webhook payload (signature already
    validated by the HTTP handler before enqueueing).
    """
    order_id = payload.get("order_id")
    transaction_status = payload.get("transaction_status")
    payment_type = payload.get("payment_type")

    if not order_id or not transaction_status:
        logger.warning(f"[webhook] Skipping incomplete payload: {payload}")
        return

    db = SessionLocal()
    try:
        transaction = (
            db.query(Transaction).filter(Transaction.order_id == order_id).first()
        )
        if not transaction:
            logger.error(
                f"[webhook] Transaction with order_id={order_id} not found"
            )
            return

        upgraded, msg = apply_payment_to_user(
            db,
            transaction,
            transaction_status,
            payment_type=payment_type,
        )
        db.commit()
        logger.info(
            f"[webhook] order_id={order_id} status={transaction_status} → {msg}"
        )

    except Exception as e:
        db.rollback()
        logger.exception(
            f"[webhook] Failed processing order_id={order_id}: {e}"
        )
        raise
    finally:
        db.close()
