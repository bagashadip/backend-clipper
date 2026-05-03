from app.tasks.celery_app import celery_app
from app.database import SessionLocal
from app.models import Transaction, User
import logging

logger = logging.getLogger(__name__)

@celery_app.task(name="tasks.process_midtrans_webhook")
def process_midtrans_webhook(payload: dict):
    order_id = payload.get("order_id")
    transaction_status = payload.get("transaction_status")
    
    db = SessionLocal()
    try:
        transaction = db.query(Transaction).filter(Transaction.order_id == order_id).first()
        if not transaction:
            logger.error(f"Transaction with order_id {order_id} not found.")
            return

        transaction.status = transaction_status
        transaction.payment_type = payload.get("payment_type")
        
        # Process ZClip tier upgrade if transaction is successful
        if transaction_status in ["settlement", "capture"]:
            user = db.query(User).filter(User.id == transaction.user_id).first()
            if user:
                # Upgrade user to pro and add quota as per rules
                user.subscription_tier = "pro"
                user.quota_seconds_remaining += 18000  # Example quota increment
                logger.info(f"User {user.id} tier upgraded to pro.")
        
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Error processing midtrans webhook for order_id {order_id}: {e}")
        raise e
    finally:
        db.close()
