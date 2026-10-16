"""Pricing arithmetic and currency formatting in integer cents."""
from sqlalchemy.orm import Session
from .config import settings
from .models import Setting

def get_pricing_settings(db: Session) -> dict:
    """Reads current pricing rates from DB, falling back to config defaults."""
    card_row = db.query(Setting).filter(Setting.key == "price_per_card_cents").first()
    list_row = db.query(Setting).filter(Setting.key == "price_per_list_cents").first()
    fee_row = db.query(Setting).filter(Setting.key == "order_fee_cents").first()
    
    price_card = int(card_row.value) if card_row else settings.price_per_card_cents
    price_list = int(list_row.value) if list_row else settings.price_per_list_cents
    order_fee = int(fee_row.value) if fee_row else settings.order_fee_cents
    
    return {
        "price_per_card_cents": price_card,
        "price_per_list_cents": price_list,
        "order_fee_cents": order_fee,
    }

def update_pricing_settings(db: Session, price_card: int, price_list: int, order_fee: int):
    """Saves pricing rates to DB."""
    for key, val in [
        ("price_per_card_cents", str(price_card)),
        ("price_per_list_cents", str(price_list)),
        ("order_fee_cents", str(order_fee)),
    ]:
        row = db.query(Setting).filter(Setting.key == key).first()
        if row:
            row.value = val
        else:
            db.add(Setting(key=key, value=val))
    db.commit()

def compute_total_cents(n_cards: int, n_lists: int, rates: dict) -> int:
    """Computes total cost in integer cents."""
    return (
        n_cards * rates["price_per_card_cents"]
        + n_lists * rates["price_per_list_cents"]
        + rates["order_fee_cents"]
    )

def format_cents_eur(cents: int) -> str:
    """Formats cents into German-style or standard decimal euro string."""
    euros = cents / 100.0
    return f"{euros:.2f} €"
