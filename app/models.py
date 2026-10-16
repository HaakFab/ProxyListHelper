import datetime
from sqlalchemy import (
    Column, Integer, String, Text, Boolean, LargeBinary,
    DateTime, ForeignKey, UniqueConstraint
)
from sqlalchemy.orm import relationship
from .database import Base

def utcnow():
    return datetime.datetime.now(datetime.timezone.utc)

class Setting(Base):
    __tablename__ = "settings"
    key = Column(String(64), primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(String(36), unique=True, index=True, nullable=False)
    submitter_name = Column(String(128), nullable=False)
    reference_code = Column(String(64), unique=True, index=True, nullable=False)
    status = Column(String(32), default="SUBMITTED", nullable=False)  # SUBMITTED, PAID, CANCELLED
    
    total_cards = Column(Integer, default=0, nullable=False)
    total_lists = Column(Integer, default=0, nullable=False)
    total_price_cents = Column(Integer, default=0, nullable=False)
    
    price_per_card_cents = Column(Integer, nullable=False)
    price_per_list_cents = Column(Integer, nullable=False)
    order_fee_cents = Column(Integer, nullable=False)
    
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    
    decks = relationship("Deck", back_populates="order", cascade="all, delete-orphan", order_by="Deck.ordinal")

class Deck(Base):
    __tablename__ = "decks"
    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(Integer, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    
    name = Column(String(128), nullable=False)
    ordinal = Column(Integer, default=1, nullable=False)
    moxfield_url = Column(String(256), nullable=True)
    
    preferred_language = Column(String(8), default="en", nullable=False)
    include_sideboard = Column(Boolean, default=False, nullable=False)
    include_considering = Column(Boolean, default=False, nullable=False)
    include_tokens = Column(Boolean, default=False, nullable=False)
    include_stickers = Column(Boolean, default=False, nullable=False)
    convert_foils = Column(Boolean, default=True, nullable=False)
    replace_stamps = Column(Boolean, default=True, nullable=False)
    replace_date_stamps = Column(Boolean, default=True, nullable=False)
    replace_digital = Column(Boolean, default=True, nullable=False)
    
    custom_back_filename = Column(String(128), nullable=True)
    custom_back_data = Column(LargeBinary, nullable=True)
    custom_back_content_type = Column(String(64), nullable=True)
    
    raw_text = Column(Text, nullable=False)
    converted_text = Column(Text, nullable=False)
    card_count = Column(Integer, default=0, nullable=False)
    
    status = Column(String(32), default="READY", nullable=False)  # READY, HAS_ISSUES
    diagnostics_json = Column(Text, nullable=True)
    
    order = relationship("Order", back_populates="decks")
