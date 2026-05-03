from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import JSONResponse
import hashlib
from app.config import get_settings
from app.database import get_db
from sqlalchemy.orm import Session
from app.models import Transaction
from app.tasks.payment_tasks import process_midtrans_webhook

import logging

router = APIRouter()
logger = logging.getLogger(__name__)

@router.post("/midtrans")
async def midtrans_webhook(request: Request, db: Session = Depends(get_db)):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(content={"status": "ok"}, status_code=200)

    order_id = payload.get("order_id")
    status_code = payload.get("status_code")
    gross_amount = payload.get("gross_amount")
    received_signature = payload.get("signature_key")

    # Missing fields — likely a verification ping from Midtrans console
    if not all([order_id, status_code, gross_amount, received_signature]):
        return JSONResponse(content={"status": "ok"}, status_code=200)

    settings = get_settings()
    server_key = settings.midtrans_server_key

    # Validate Signature: SHA512(order_id + status_code + gross_amount + server_key)
    signature_data = f"{order_id}{status_code}{gross_amount}{server_key}"
    expected_signature = hashlib.sha512(signature_data.encode("utf-8")).hexdigest()

    if expected_signature != received_signature:
        logger.warning(f"Invalid Midtrans signature for order_id={order_id}. Ignoring.")
        return JSONResponse(content={"status": "ok"}, status_code=200)

    # Idempotency check
    transaction = db.query(Transaction).filter(Transaction.order_id == order_id).first()
    if transaction and transaction.status in ["settlement", "success"]:
        return JSONResponse(content={"status": "ok", "message": "already processed"}, status_code=200)

    # Dispatch to Celery worker
    process_midtrans_webhook.delay(payload)

    return JSONResponse(content={"status": "ok"}, status_code=200)
