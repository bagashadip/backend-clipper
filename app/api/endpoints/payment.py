from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session
from app.database import get_db
from app.config import get_settings
from app.models import User, Transaction
from app.services.auth import get_current_user
from pydantic import BaseModel
import httpx
import uuid
import time
import base64

router = APIRouter()

class SnapTokenRequest(BaseModel):
    tier_id: str
    amount: float

@router.post("/snap-token")
async def generate_snap_token(
    request: SnapTokenRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
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
        status="pending"
    )
    db.add(transaction)
    db.commit()

    # Create Midtrans Snap Token
    auth_string = base64.b64encode(f"{server_key}:".encode()).decode()
    
    url = "https://app.sandbox.midtrans.com/snap/v1/transactions"
    
    payload = {
        "transaction_details": {
            "order_id": order_id,
            "gross_amount": int(request.amount)
        },
        "customer_details": {
            "email": user.email,
        },
        "custom_field1": request.tier_id
    }

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Basic {auth_string}"
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload, headers=headers)
        
        if response.status_code != 201:
            raise HTTPException(status_code=500, detail=f"Failed to generate snap token: {response.text}")
            
        data = response.json()
        return {"snap_token": data.get("token")}
