from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import JSONResponse
import hashlib
from app.config import get_settings
from app.database import get_db
from sqlalchemy.orm import Session
from app.models import Transaction
from app.tasks.payment_tasks import process_midtrans_webhook

router = APIRouter()

@router.post("/midtrans")
async def midtrans_webhook(request: Request, db: Session = Depends(get_db)):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")
        
    order_id = payload.get("order_id")
    status_code = payload.get("status_code")
    gross_amount = payload.get("gross_amount")
    received_signature = payload.get("signature_key")
    
    if not all([order_id, status_code, gross_amount, received_signature]):
        raise HTTPException(status_code=400, detail="Missing required fields")

    settings = get_settings()
    server_key = settings.midtrans_server_key
    
    # Validate Signature using SHA512(order_id + status_code + gross_amount + server_key)
    signature_data = f"{order_id}{status_code}{gross_amount}{server_key}"
    expected_signature = hashlib.sha512(signature_data.encode("utf-8")).hexdigest()
    
    if expected_signature != received_signature:
        raise HTTPException(status_code=403, detail="Invalid signature")

    # Idempotency Check: if transaction is already marked 'settlement' or 'success'
    transaction = db.query(Transaction).filter(Transaction.order_id == order_id).first()
    if transaction and transaction.status in ["settlement", "success"]:
        return JSONResponse(content={"status": "ok", "message": "already processed"}, status_code=200)
        
    # Dispatch payload to Celery worker
    process_midtrans_webhook.delay(payload)
    
    # Return 200 OK immediately
    return JSONResponse(content={"status": "ok"}, status_code=200)
