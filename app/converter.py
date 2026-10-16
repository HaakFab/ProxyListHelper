"""Deck converter that applies pre-run transformations and traffic-light validation."""
from typing import Dict, List, Optional, Tuple
from .scryfall import (
    get_card_by_set_cn,
    get_card_by_name,
    is_foil_only,
    has_planeswalker_stamp,
    find_clean_paper_fallback,
    check_localized_scan
)

class CardAuditItem:
    def __init__(
        self,
        qty: int,
        name: str,
        set_code: str,
        cn: str,
        status: str = "GREEN",  # GREEN, YELLOW, RED
        category: str = "CLEAN",
        message: str = "",
        original_line: str = "",
        fixed_line: str = "",
        can_autofix: bool = False,
        suggested_set: str = "",
        suggested_cn: str = "",
        localized_scan_available: bool = True,
        back_name: Optional[str] = None,
        back_set: Optional[str] = None,
        back_cn: Optional[str] = None,
    ):
        self.qty = qty
        self.name = name
        self.set_code = set_code
        self.cn = cn
        self.status = status
        self.category = category
        self.message = message
        self.original_line = original_line
        self.fixed_line = fixed_line
        self.can_autofix = can_autofix
        self.suggested_set = suggested_set
        self.suggested_cn = suggested_cn
        self.localized_scan_available = localized_scan_available
        self.back_name = back_name
        self.back_set = back_set
        self.back_cn = back_cn

    def to_dict(self) -> dict:
        return {
            "qty": self.qty,
            "name": self.name,
            "set": self.set_code,
            "cn": self.cn,
            "status": self.status,
            "category": self.category,
            "message": self.message,
            "original_line": self.original_line,
            "fixed_line": self.fixed_line,
            "can_autofix": self.can_autofix,
            "suggested_set": self.suggested_set,
            "suggested_cn": self.suggested_cn,
            "localized_scan_available": self.localized_scan_available,
            "back_name": self.back_name,
            "back_set": self.back_set,
            "back_cn": self.back_cn,
        }

def format_card_line(qty: int, name: str, set_code: Optional[str], cn: Optional[str],
                     back_name: Optional[str] = None, back_set: Optional[str] = None,
                     back_cn: Optional[str] = None) -> str:
    """Formats a card line into canonical export format."""
    if set_code and cn:
        front = f"{qty} {name} ({set_code.upper()}) {cn}".strip()
    else:
        front = f"{qty} {name}".strip()
        
    if back_name:
        if back_set and back_cn:
            back = f"{back_name} ({back_set.upper()}) {back_cn}".strip()
        else:
            back = back_name.strip()
        return f"{front} &&& {back}"
    return front

def process_deck_cards(
    raw_cards: List[dict],
    preferred_language: str = "en",
    convert_foils: bool = True,
    replace_stamps: bool = True,
    deck_name: str = "Deck"
) -> Tuple[List[CardAuditItem], str, str]:
    """
    Processes all cards in a deck, applying transformations and traffic light checks.
    Returns: (audit_items, overall_status, converted_text)
    """
    audit_items: List[CardAuditItem] = []
    overall_status = "GREEN"
    converted_lines: List[str] = [
        f"// Deck: {deck_name}",
        f"// Language: {preferred_language}",
    ]
    
    for item in raw_cards:
        qty = item["qty"]
        name = item["name"]
        set_code = (item.get("set") or "").lower()
        cn = str(item.get("cn") or "").strip()
        back_name = item.get("back_name")
        back_set = (item.get("back_set") or "").lower() if item.get("back_set") else None
        back_cn = item.get("back_cn")
        
        orig_line = format_card_line(qty, name, set_code, cn, back_name, back_set, back_cn)
        
        # 1. Scryfall lookup
        card_data = None
        if set_code and cn:
            card_data = get_card_by_set_cn(set_code, cn)
            
        if not card_data:
            # Fallback by name
            card_data = get_card_by_name(name)
            if card_data:
                # Update set & cn to what Scryfall found
                set_code = card_data.get("set", "").lower()
                cn = card_data.get("collector_number", "").strip()
                
        if not card_data:
            # 🔴 Card not found on Scryfall
            audit_item = CardAuditItem(
                qty=qty, name=name, set_code=set_code, cn=cn,
                status="RED", category="NOT_FOUND",
                message=f"Card '{name}' could not be found on Scryfall.",
                original_line=orig_line,
                fixed_line=orig_line,
                can_autofix=False,
                back_name=back_name, back_set=back_set, back_cn=back_cn
            )
            audit_items.append(audit_item)
            overall_status = "RED"
            continue
            
        # 2. Check Digital-only
        if card_data.get("digital") is True:
            paper_fallback = find_clean_paper_fallback(name, prefer_nonfoil=True, avoid_stamped=True)
            if paper_fallback:
                fb_set = paper_fallback.get("set", "").lower()
                fb_cn = paper_fallback.get("collector_number", "").strip()
                fixed = format_card_line(qty, name, fb_set, fb_cn, back_name, back_set, back_cn)
                audit_item = CardAuditItem(
                    qty=qty, name=name, set_code=set_code, cn=cn,
                    status="YELLOW", category="DIGITAL_REPLACED",
                    message=f"Digital printing replaced with paper printing ({fb_set.upper()} #{fb_cn}).",
                    original_line=orig_line, fixed_line=fixed, can_autofix=True,
                    suggested_set=fb_set, suggested_cn=fb_cn,
                    back_name=back_name, back_set=back_set, back_cn=back_cn
                )
                audit_items.append(audit_item)
                if overall_status != "RED":
                    overall_status = "YELLOW"
                set_code, cn = fb_set, fb_cn
            else:
                audit_item = CardAuditItem(
                    qty=qty, name=name, set_code=set_code, cn=cn,
                    status="RED", category="DIGITAL_ONLY",
                    message=f"'{name}' is digital-only and has no paper release.",
                    original_line=orig_line, fixed_line=orig_line, can_autofix=False,
                    back_name=back_name, back_set=back_set, back_cn=back_cn
                )
                audit_items.append(audit_item)
                overall_status = "RED"
                continue
                
        # 3. Check Promo Planeswalker Stamp
        if replace_stamps and has_planeswalker_stamp(card_data):
            clean_fallback = find_clean_paper_fallback(name, prefer_nonfoil=convert_foils, avoid_stamped=True)
            if clean_fallback:
                fb_set = clean_fallback.get("set", "").lower()
                fb_cn = clean_fallback.get("collector_number", "").strip()
                fixed = format_card_line(qty, name, fb_set, fb_cn, back_name, back_set, back_cn)
                audit_item = CardAuditItem(
                    qty=qty, name=name, set_code=set_code, cn=cn,
                    status="YELLOW", category="STAMP_REPLACED",
                    message=f"Planeswalker-stamped promo replaced with clean printing ({fb_set.upper()} #{fb_cn}).",
                    original_line=orig_line, fixed_line=fixed, can_autofix=True,
                    suggested_set=fb_set, suggested_cn=fb_cn,
                    back_name=back_name, back_set=back_set, back_cn=back_cn
                )
                audit_items.append(audit_item)
                if overall_status != "RED":
                    overall_status = "YELLOW"
                set_code, cn = fb_set, fb_cn
                card_data = clean_fallback

        # 4. Check Foil-only
        if convert_foils and is_foil_only(card_data):
            nonfoil_fallback = find_clean_paper_fallback(name, prefer_nonfoil=True, avoid_stamped=replace_stamps)
            if nonfoil_fallback:
                fb_set = nonfoil_fallback.get("set", "").lower()
                fb_cn = nonfoil_fallback.get("collector_number", "").strip()
                fixed = format_card_line(qty, name, fb_set, fb_cn, back_name, back_set, back_cn)
                audit_item = CardAuditItem(
                    qty=qty, name=name, set_code=set_code, cn=cn,
                    status="YELLOW", category="FOIL_REPLACED",
                    message=f"Foil-only printing replaced with non-foil paper version ({fb_set.upper()} #{fb_cn}).",
                    original_line=orig_line, fixed_line=fixed, can_autofix=True,
                    suggested_set=fb_set, suggested_cn=fb_cn,
                    back_name=back_name, back_set=back_set, back_cn=back_cn
                )
                audit_items.append(audit_item)
                if overall_status != "RED":
                    overall_status = "YELLOW"
                set_code, cn = fb_set, fb_cn
                card_data = nonfoil_fallback
                
        # 5. Check Language Scan Availability
        has_localized = True
        if preferred_language != "en":
            has_localized, localized_name = check_localized_scan(set_code, cn, preferred_language)
            
        final_line = format_card_line(qty, name, set_code, cn, back_name, back_set, back_cn)
        converted_lines.append(final_line)
        
        # If no issues were flagged above, card is 🟢 GREEN
        if not any(a.name == name and a.set_code == set_code and a.cn == cn for a in audit_items):
            audit_item = CardAuditItem(
                qty=qty, name=name, set_code=set_code, cn=cn,
                status="GREEN", category="CLEAN",
                message="Card verified on Scryfall.",
                original_line=orig_line, fixed_line=final_line, can_autofix=False,
                localized_scan_available=has_localized,
                back_name=back_name, back_set=back_set, back_cn=back_cn
            )
            audit_items.append(audit_item)

    converted_text = "\n".join(converted_lines) + "\n"
    return audit_items, overall_status, converted_text
