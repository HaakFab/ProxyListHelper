"""User-facing routes for deck submission, preview, interactive review, and success."""
import json
import os
import random
import re
import string
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..i18n import LANGUAGE_NAMES, t
from ..models import Deck, Order
from ..moxfield import extract_deck_cards, fetch_moxfield_deck, parse_pasted_text
from ..pricing import compute_total_cents, format_cents_eur, get_pricing_settings
from ..converter import format_card_line, process_deck_cards
from ..scryfall import get_card_versions

router = APIRouter()

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

def generate_reference_code(submitter_name: str) -> str:
    """Generates a memorable, clean payment reference code."""
    clean_name = re.sub(r"[^A-Za-z]", "", submitter_name).upper()[:8] or "ORDER"
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
    return f"MTG-{clean_name}-{suffix}"

@router.get("/", response_class=HTMLResponse)
def index_page(request: Request, lang: str = "en", error: Optional[str] = None, db: Session = Depends(get_db)):
    rates = get_pricing_settings(db)
    return templates.TemplateResponse(request, "index.html", {
        "lang": lang,
        "error": error,
        "t": lambda key, *args: t(key, lang, *args),
        "language_names": LANGUAGE_NAMES,
        "rates": rates,
        "format_cents": format_cents_eur,
    })

@router.post("/api/moxfield/preview")
def moxfield_preview(payload: dict):
    """Quickly fetches deck title and basic counts from Moxfield URL."""
    url = payload.get("url", "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="Missing URL")
    try:
        data = fetch_moxfield_deck(url)
        name, cards = extract_deck_cards(data, include_sideboard=False, include_considering=False, include_tokens=False)
        total_count = sum(c["qty"] for c in cards)
        return {
            "success": True,
            "name": name,
            "main_count": total_count,
            "has_sideboard": bool(data.get("boards", {}).get("sideboard", {}).get("cards")),
            "has_considering": bool(data.get("boards", {}).get("maybeboard", {}).get("cards")),
            "has_tokens": bool(data.get("tokens") or data.get("boards", {}).get("tokens", {}).get("cards")),
            "has_stickers": bool(data.get("stickers") or data.get("boards", {}).get("stickers", {}).get("cards")),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}

def _is_checkbox_checked(val) -> bool:
    if not val:
        return False
    if isinstance(val, str):
        return val.strip().lower() in ("on", "true", "1", "yes")
    return bool(val)


@router.post("/submit")
async def submit_decks(
    request: Request,
    db: Session = Depends(get_db),
):
    form_data = await request.form()
    submitter_name = str(form_data.get("submitter_name", "")).strip()
    ui_lang = str(form_data.get("ui_lang", "en")).strip()
    
    if not submitter_name:
        return RedirectResponse(f"/?lang={ui_lang}&error=missing_name", status_code=303)
        
    try:
        deck_count = int(form_data.get("deck_count", 1))
    except ValueError:
        deck_count = 1
        
    rates = get_pricing_settings(db)
    order_id = uuid.uuid4().hex
    ref_code = generate_reference_code(submitter_name)
    
    order = Order(
        order_id=order_id,
        submitter_name=submitter_name,
        reference_code=ref_code,
        status="SUBMITTED",
        price_per_card_cents=rates["price_per_card_cents"],
        price_per_list_cents=rates["price_per_list_cents"],
        order_fee_cents=rates["order_fee_cents"],
    )
    db.add(order)
    db.flush()
    
    has_any_red = False
    total_order_cards = 0
    decks_processed = 0
    
    for i in range(deck_count):
        deck_name = str(form_data.get(f"deck_name_{i}", f"Deck {i+1}")).strip() or f"Deck {i+1}"
        source_type = str(form_data.get(f"source_type_{i}", "url")).strip()
        moxfield_url = str(form_data.get(f"moxfield_url_{i}", "")).strip()
        raw_pasted = str(form_data.get(f"raw_text_{i}", "")).strip()
        
        pref_lang = str(form_data.get(f"preferred_language_{i}", "en")).strip()
        inc_sideboard = _is_checkbox_checked(form_data.get(f"include_sideboard_{i}"))
        inc_considering = _is_checkbox_checked(form_data.get(f"include_considering_{i}"))
        inc_tokens = _is_checkbox_checked(form_data.get(f"include_tokens_{i}"))
        inc_stickers = _is_checkbox_checked(form_data.get(f"include_stickers_{i}"))
        conv_foils = _is_checkbox_checked(form_data.get(f"convert_foils_{i}"))
        rep_stamps = _is_checkbox_checked(form_data.get(f"replace_stamps_{i}"))
        rep_date_stamps = _is_checkbox_checked(form_data.get(f"replace_date_stamps_{i}"))
        rep_digital = _is_checkbox_checked(form_data.get(f"replace_digital_{i}"))
        
        raw_cards = []
        raw_text_storage = ""
        
        if source_type == "url" and moxfield_url:
            try:
                mox_data = fetch_moxfield_deck(moxfield_url)
                fetched_title, raw_cards = extract_deck_cards(
                    mox_data,
                    include_sideboard=inc_sideboard,
                    include_considering=inc_considering,
                    include_tokens=inc_tokens,
                    include_stickers=inc_stickers
                )
                if not deck_name or deck_name == f"Deck {i+1}":
                    deck_name = fetched_title
                raw_text_storage = "\n".join(f"{c['qty']} {c['name']} ({c['set'].upper()}) {c['cn']}" for c in raw_cards)
            except Exception as e:
                # If Moxfield fetch fails, fall back to pasted text if available
                if raw_pasted:
                    raw_cards = parse_pasted_text(raw_pasted)
                    raw_text_storage = raw_pasted
                else:
                    continue
        elif raw_pasted:
            raw_cards = parse_pasted_text(raw_pasted)
            raw_text_storage = raw_pasted
            
        if not raw_cards:
            continue
            
        # Handle optional custom back upload
        custom_back_file = form_data.get(f"custom_back_{i}")
        back_data = None
        back_filename = None
        back_content_type = None
        
        if custom_back_file and hasattr(custom_back_file, "filename") and custom_back_file.filename:
            content = await custom_back_file.read()
            if len(content) > 0 and len(content) < 15 * 1024 * 1024:  # Max 15MB
                back_data = content
                back_filename = custom_back_file.filename
                back_content_type = custom_back_file.content_type or "image/jpeg"
                
        # Run conversion and Scryfall checks
        audit_items, deck_status, converted_text = process_deck_cards(
            raw_cards=raw_cards,
            preferred_language=pref_lang,
            convert_foils=conv_foils,
            replace_stamps=rep_stamps,
            replace_date_stamps=rep_date_stamps,
            replace_digital=rep_digital,
            deck_name=deck_name
        )
        
        if deck_status == "RED":
            has_any_red = True
            
        card_count = sum(a.qty for a in audit_items if a.status != "RED" or a.can_autofix)
        total_order_cards += card_count
        decks_processed += 1
        
        deck = Deck(
            order_id=order.id,
            name=deck_name,
            ordinal=decks_processed,
            moxfield_url=moxfield_url if source_type == "url" else None,
            preferred_language=pref_lang,
            include_sideboard=inc_sideboard,
            include_considering=inc_considering,
            include_tokens=inc_tokens,
            include_stickers=inc_stickers,
            convert_foils=conv_foils,
            replace_stamps=rep_stamps,
            replace_date_stamps=rep_date_stamps,
            replace_digital=rep_digital,
            custom_back_filename=back_filename,
            custom_back_data=back_data,
            custom_back_content_type=back_content_type,
            raw_text=raw_text_storage,
            converted_text=converted_text,
            card_count=card_count,
            status=deck_status,
            diagnostics_json=json.dumps([a.to_dict() for a in audit_items]),
        )
        db.add(deck)

    if decks_processed == 0:
        db.delete(order)
        db.commit()
        return RedirectResponse(f"/?lang={ui_lang}&error=empty_decks", status_code=303)

    order.total_lists = decks_processed
    order.total_cards = total_order_cards
    order.total_price_cents = compute_total_cents(total_order_cards, decks_processed, rates)
    db.commit()
    
    # Always redirect to review page so the user sees the complete overview of parsed cards and changes before accepting
    return RedirectResponse(f"/review/{order_id}?lang={ui_lang}", status_code=303)

@router.get("/review/{order_id}", response_class=HTMLResponse)
def review_order(request: Request, order_id: str, lang: str = "en", db: Session = Depends(get_db)):
    order = db.query(Order).filter(Order.order_id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
        
    rates = {
        "price_per_card_cents": order.price_per_card_cents,
        "price_per_list_cents": order.price_per_list_cents,
        "order_fee_cents": order.order_fee_cents,
    }
    
    parsed_decks = []
    has_issues = False
    
    for d in order.decks:
        diagnostics = json.loads(d.diagnostics_json or "[]")
        red_items = []
        yellow_items = []
        green_items = []
        for idx, diag in enumerate(diagnostics):
            diag["diag_idx"] = idx
            st = diag.get("status")
            if st == "RED":
                red_items.append(diag)
            elif st == "YELLOW":
                yellow_items.append(diag)
            elif st == "GREEN":
                green_items.append(diag)
        if red_items:
            has_issues = True
            
        parsed_decks.append({
            "deck": d,
            "red_items": red_items,
            "yellow_items": yellow_items,
            "green_items": green_items,
        })
        
    return templates.TemplateResponse(request, "review.html", {
        "order": order,
        "parsed_decks": parsed_decks,
        "has_issues": has_issues,
        "lang": lang,
        "t": lambda key, *args: t(key, lang, *args),
        "rates": rates,
        "format_cents": format_cents_eur,
    })

@router.get("/api/card-versions")
def api_card_versions(name: str):
    """Returns all Scryfall printings for a card to populate the version switcher modal."""
    if not name or not name.strip():
        return {"success": False, "versions": [], "error": "Missing card name"}
    versions = get_card_versions(name.strip())
    return {"success": True, "card_name": name.strip(), "versions": versions}

@router.post("/api/deck/{deck_id}/switch-version")
async def switch_deck_card_version(
    deck_id: int,
    request: Request,
    db: Session = Depends(get_db)
):
    """Updates a card replacement in a deck with a user-selected printing."""
    deck = db.query(Deck).filter(Deck.id == deck_id).first()
    if not deck:
        raise HTTPException(status_code=404, detail="Deck not found")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    item_idx = body.get("item_idx")
    new_set = (body.get("set") or "").lower().strip()
    new_cn = str(body.get("cn") or "").strip()
    new_img = body.get("image_url")
    version_label = body.get("version_label") or f"{new_set.upper()} #{new_cn}"

    if item_idx is None or not new_set or not new_cn:
        raise HTTPException(status_code=400, detail="Missing item_idx, set, or cn")

    diagnostics = json.loads(deck.diagnostics_json or "[]")
    if item_idx < 0 or item_idx >= len(diagnostics):
        raise HTTPException(status_code=400, detail="item_idx out of range")

    item = diagnostics[item_idx]
    qty = int(item.get("qty", 1))
    card_name = item.get("name", "")

    # Format new line
    new_line = format_card_line(
        qty, card_name, new_set, new_cn,
        item.get("back_name"), item.get("back_set"), item.get("back_cn")
    )

    # Update item in diagnostics
    item["set"] = new_set
    item["cn"] = new_cn
    item["suggested_set"] = new_set
    item["suggested_cn"] = new_cn
    item["fixed_line"] = new_line
    if new_img:
        item["fixed_image_url"] = new_img
    item["fixed_version_label"] = version_label
    item["user_custom_version"] = True
    item["message"] = f"Replaced with user-selected version ({new_set.upper()} #{new_cn})."

    # Rebuild deck converted_text
    final_lines = [
        f"// Deck: {deck.name}",
        f"// Language: {deck.preferred_language}",
    ]
    for d in diagnostics:
        line = d.get("fixed_line") or d.get("original_line")
        if not line:
            line = format_card_line(
                int(d.get("qty", 1)), d.get("name", ""),
                d.get("set"), d.get("cn"),
                d.get("back_name"), d.get("back_set"), d.get("back_cn")
            )
        final_lines.append(str(line))
    deck.converted_text = "\n".join(final_lines) + "\n"
    deck.diagnostics_json = json.dumps(diagnostics)
    db.commit()

    return {
        "success": True,
        "deck_id": deck.id,
        "item_idx": item_idx,
        "set": new_set,
        "cn": new_cn,
        "fixed_line": new_line,
        "fixed_version_label": version_label,
        "fixed_image_url": item.get("fixed_image_url"),
        "message": item["message"]
    }

@router.post("/review/{order_id}/resolve")
async def resolve_review_issues(
    request: Request,
    order_id: str,
    db: Session = Depends(get_db)
):
    order = db.query(Order).filter(Order.order_id == order_id).first()
    form_data = await request.form()
    ui_lang = str(form_data.get("ui_lang", "en")).strip()

    if not order:
        return RedirectResponse(f"/?lang={ui_lang}", status_code=303)

    if order.status == "SUBMITTED":
        return RedirectResponse(f"/success/{order_id}?lang={ui_lang}", status_code=303)

    total_order_cards = 0

    try:
        for deck in order.decks:
            diagnostics = json.loads(deck.diagnostics_json or "[]")
            final_lines = [
                f"// Deck: {deck.name}",
                f"// Language: {deck.preferred_language}",
            ]
            deck_card_count = 0

            for idx, item in enumerate(diagnostics):
                status = item.get("status")
                action = form_data.get(f"action_{deck.id}_{idx}", "substitute")
                custom_line = form_data.get(f"custom_v2_line_{deck.id}_{idx}")
                qty = int(item.get("qty", 1))
                name = item.get("name", "")

                if status == "RED":
                    if action == "drop":
                        # Drop the card
                        item["resolved_action"] = "dropped"
                        continue
                    elif action == "substitute" and item.get("suggested_set"):
                        # Use suggested paper replacement
                        item["set"] = item["suggested_set"]
                        item["cn"] = item["suggested_cn"]
                        line = custom_line or format_card_line(qty, name, item["set"], item["cn"],
                                                item.get("back_name"), item.get("back_set"), item.get("back_cn"))
                        final_lines.append(line)
                        deck_card_count += qty
                    else:
                        # Keep original line
                        line = format_card_line(qty, name, item.get("set"), item.get("cn"),
                                                item.get("back_name"), item.get("back_set"), item.get("back_cn"))
                        final_lines.append(line)
                        deck_card_count += qty
                else:
                    # Green or Yellow
                    line = custom_line or item.get("fixed_line") or item.get("original_line")
                    if not line:
                        line = format_card_line(qty, name, item.get("set"), item.get("cn"),
                                                item.get("back_name"), item.get("back_set"), item.get("back_cn"))
                    final_lines.append(str(line))
                    deck_card_count += qty

            deck.converted_text = "\n".join(final_lines) + "\n"
            deck.card_count = deck_card_count
            deck.status = "READY"
            deck.diagnostics_json = json.dumps(diagnostics)
            total_order_cards += deck_card_count

        order.total_cards = total_order_cards
        rates = {
            "price_per_card_cents": order.price_per_card_cents or 13,
            "price_per_list_cents": order.price_per_list_cents or 26,
            "order_fee_cents": order.order_fee_cents or 0,
        }
        order.total_price_cents = compute_total_cents(total_order_cards, len(order.decks), rates)
        order.status = "SUBMITTED"
        db.commit()
    except Exception as exc:
        db.rollback()
        # Even on rollback, keep order status resilient
        pass

    return RedirectResponse(f"/success/{order_id}?lang={ui_lang}", status_code=303)

@router.get("/success/{order_id}", response_class=HTMLResponse)
def success_page(request: Request, order_id: str, lang: str = "en", db: Session = Depends(get_db)):
    order = db.query(Order).filter(Order.order_id == order_id).first()

    default_rates = {
        "price_per_card_cents": 13,
        "price_per_list_cents": 26,
        "order_fee_cents": 0,
    }

    if not order:
        return templates.TemplateResponse(request, "success.html", {
            "order": None,
            "deck_changes": [],
            "lang": lang,
            "t": lambda key, *args: t(key, lang, *args),
            "rates": default_rates,
            "format_cents": format_cents_eur,
        })

    rates = {
        "price_per_card_cents": order.price_per_card_cents or 13,
        "price_per_list_cents": order.price_per_list_cents or 26,
        "order_fee_cents": order.order_fee_cents or 0,
    }

    deck_changes = []
    for d in order.decks:
        try:
            diag = json.loads(d.diagnostics_json or "[]")
            yellows = [i for i in diag if i.get("status") == "YELLOW" or i.get("can_autofix") or i.get("category") in ("FOIL_REPLACED", "STAMP_REPLACED", "DATE_STAMP_REPLACED", "ALCHEMY_REPLACED", "DIGITAL_REPLACED")]
            if yellows:
                deck_changes.append({"deck": d, "changes": yellows, "items": yellows})
        except Exception:
            pass

    return templates.TemplateResponse(request, "success.html", {
        "order": order,
        "deck_changes": deck_changes,
        "lang": lang,
        "t": lambda key, *args: t(key, lang, *args),
        "rates": rates,
        "format_cents": format_cents_eur,
    })

@router.get("/api/deck/{deck_id}/back")
def serve_deck_cardback(deck_id: int, db: Session = Depends(get_db)):
    deck = db.query(Deck).filter(Deck.id == deck_id).first()
    if not deck or not deck.custom_back_data:
        raise HTTPException(status_code=404, detail="No custom back image for this deck")
    return Response(
        content=deck.custom_back_data,
        media_type=deck.custom_back_content_type or "image/jpeg"
    )

@router.get("/order/{order_id}/download")
def download_order_overview(order_id: str, db: Session = Depends(get_db)):
    """Downloads a complete order summary text ticket including decks overview, cost breakdown, and payment reference code."""
    order = db.query(Order).filter(Order.order_id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    lines = []
    lines.append("================================================================================")
    lines.append("                 PROXY//CORE - ORDER OVERVIEW & PAYMENT VOUCHER                 ")
    lines.append("================================================================================")
    lines.append("")
    lines.append(f"SUBMITTER:              {order.submitter_name}")
    lines.append(f"PAYMENT REFERENCE CODE: {order.reference_code}")
    lines.append(f"STATUS:                 {order.status}")
    lines.append(f"ORDER DATE (UTC):       {order.created_at.strftime('%Y-%m-%d %H:%M')}")
    lines.append("")
    lines.append("--------------------------------------------------------------------------------")
    lines.append("PAYMENT INFORMATION / ZAHLUNGSINFORMATION")
    lines.append("--------------------------------------------------------------------------------")
    lines.append(f"TOTAL AMOUNT TO PAY:    {order.total_price_cents / 100.0:.2f} EUR")
    lines.append(f"PAYMENT REFERENCE CODE: {order.reference_code}")
    lines.append("NOTE:                   Please include the exact payment reference code above")
    lines.append("                        in your payment note / Verwendungszweck.")
    lines.append("")
    lines.append("--------------------------------------------------------------------------------")
    lines.append("COST BREAKDOWN / KOSTENÜBERSICHT")
    lines.append("--------------------------------------------------------------------------------")
    card_rate = (order.price_per_card_cents or 13) / 100.0
    list_rate = (order.price_per_list_cents or 26) / 100.0
    fee_rate = (order.order_fee_cents or 0) / 100.0
    card_subtotal = (order.total_cards * (order.price_per_card_cents or 13)) / 100.0
    list_subtotal = (order.total_lists * (order.price_per_list_cents or 26)) / 100.0

    lines.append(f"Total Cards:     {order.total_cards:4d} cards  x  {card_rate:.2f} EUR  =  {card_subtotal:7.2f} EUR")
    lines.append(f"Total Lists:     {order.total_lists:4d} lists  x  {list_rate:.2f} EUR  =  {list_subtotal:7.2f} EUR")
    if order.order_fee_cents and order.order_fee_cents > 0:
        lines.append(f"Order Fee:                                            {fee_rate:7.2f} EUR")
    lines.append("--------------------------------------------------------------------------------")
    lines.append(f"TOTAL:                                               {order.total_price_cents / 100.0:7.2f} EUR")
    lines.append("")
    lines.append("--------------------------------------------------------------------------------")
    lines.append("REGISTERED DECKLISTS & CARDS OVERVIEW")
    lines.append("--------------------------------------------------------------------------------")
    for d in order.decks:
        lines.append(f"DECK #{d.ordinal}: {d.name}")
        lines.append(f"  - Language:    {d.preferred_language.upper()}")
        lines.append(f"  - Card Count:  {d.card_count} cards")
        lines.append(f"  - Card Back:   {'Custom back: ' + d.custom_back_filename if d.custom_back_filename else 'Default back'}")
        if d.moxfield_url:
            lines.append(f"  - Moxfield:    {d.moxfield_url}")
        lines.append("")
        if d.converted_text:
            lines.append("  [Decklist Content]")
            for card_line in d.converted_text.strip().split("\n"):
                lines.append(f"    {card_line}")
        lines.append("")

    lines.append("================================================================================")
    lines.append("                   THANK YOU FOR PARTICIPATING IN THIS PRINT RUN!               ")
    lines.append("================================================================================")

    content = "\n".join(lines) + "\n"
    filename = f"order_overview_{order.reference_code}.txt"
    return Response(
        content=content.encode("utf-8"),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )

