import re
from typing import Dict, List, Optional, Tuple
from .scryfall import (
    preload_cards_batch,
    get_card_by_set_cn,
    get_card_by_name,
    get_card_image_url,
    is_foil_only,
    has_planeswalker_stamp,
    has_date_stamp,
    is_digital_card,
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
        original_image_url: Optional[str] = None,
        fixed_image_url: Optional[str] = None,
        original_version_label: str = "",
        fixed_version_label: str = "",
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
        self.original_image_url = original_image_url
        self.fixed_image_url = fixed_image_url
        self.original_version_label = original_version_label
        self.fixed_version_label = fixed_version_label
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
            "original_image_url": self.original_image_url,
            "fixed_image_url": self.fixed_image_url,
            "original_version_label": self.original_version_label,
            "fixed_version_label": self.fixed_version_label,
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
    replace_date_stamps: bool = True,
    replace_digital: bool = True,
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
    
    # Preload all cards via Scryfall collection batch API for fast sub-second analysis
    preload_cards_batch(raw_cards)
    
    for item in raw_cards:
        qty = item["qty"]
        name = item["name"]
        set_code = (item.get("set") or "").lower()
        cn = str(item.get("cn") or "").lstrip("#").strip()
        back_name = item.get("back_name")
        back_set = (item.get("back_set") or "").lower() if item.get("back_set") else None
        back_cn = item.get("back_cn")
        
        orig_line = format_card_line(qty, name, set_code, cn, back_name, back_set, back_cn)
        card_flagged = False

        # 0. Check for Arena Alchemy Rebalanced Card (e.g. A-Teferi, Time Raveler, A-Unholy Heat)
        if name.startswith("A-") or name.startswith("a-"):
            raw_name = name
            clean_name = name[2:].strip()
            clean_cn = re.sub(r"^[Aa]-", "", cn).strip()
            
            paper_card = None
            if set_code and clean_cn:
                paper_card = get_card_by_set_cn(set_code, clean_cn)
            if not paper_card:
                paper_card = get_card_by_name(clean_name)

            if paper_card:
                name = clean_name
                orig_set, orig_cn = set_code, cn
                set_code = paper_card.get("set", "").lower()
                cn = paper_card.get("collector_number", "").strip()
                fixed = format_card_line(qty, name, set_code, cn, back_name, back_set, back_cn)
                audit_item = CardAuditItem(
                    qty=qty, name=name, set_code=orig_set or set_code, cn=orig_cn or cn,
                    status="YELLOW", category="ALCHEMY_REPLACED",
                    message=f"Arena Alchemy rebalanced card '{raw_name}' converted to official paper printing ({set_code.upper()} #{cn}).",
                    original_line=orig_line, fixed_line=fixed, can_autofix=True,
                    suggested_set=set_code, suggested_cn=cn,
                    original_image_url=None,
                    fixed_image_url=get_card_image_url(paper_card),
                    original_version_label=f"{raw_name} ({orig_set.upper() if orig_set else 'ARENA'} #{orig_cn})",
                    fixed_version_label=f"{name} ({set_code.upper()} #{cn})",
                    back_name=back_name, back_set=back_set, back_cn=back_cn
                )
                audit_items.append(audit_item)
                card_flagged = True
                if overall_status != "RED":
                    overall_status = "YELLOW"
                card_data = paper_card
            else:
                card_data = None
        else:
            card_data = None
        
        # 1. Scryfall lookup if not already resolved by Alchemy fallback
        if not card_data:
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
                original_version_label=f"{name} ({set_code.upper() if set_code else 'No Set'} #{cn or '?'})",
                back_name=back_name, back_set=back_set, back_cn=back_cn
            )
            audit_items.append(audit_item)
            overall_status = "RED"
            continue

        initial_img = get_card_image_url(card_data)
            
        # 2. Check Digital / MTGO Promos
        is_digital = is_digital_card(card_data)
        if is_digital:
            if replace_digital:
                paper_fallback = find_clean_paper_fallback(
                    name,
                    prefer_nonfoil=convert_foils,
                    avoid_stamped=replace_stamps,
                    avoid_datestamped=replace_date_stamps
                )
                if paper_fallback:
                    fb_set = paper_fallback.get("set", "").lower()
                    fb_cn = paper_fallback.get("collector_number", "").strip()
                    fixed = format_card_line(qty, name, fb_set, fb_cn, back_name, back_set, back_cn)
                    audit_item = CardAuditItem(
                        qty=qty, name=name, set_code=set_code, cn=cn,
                        status="YELLOW", category="DIGITAL_REPLACED",
                        message=f"Digital/MTGO promo replaced with official paper printing ({fb_set.upper()} #{fb_cn}).",
                        original_line=orig_line, fixed_line=fixed, can_autofix=True,
                        suggested_set=fb_set, suggested_cn=fb_cn,
                        original_image_url=initial_img,
                        fixed_image_url=get_card_image_url(paper_fallback),
                        original_version_label=f"Digital ({set_code.upper()} #{cn})",
                        fixed_version_label=f"Paper ({fb_set.upper()} #{fb_cn})",
                        back_name=back_name, back_set=back_set, back_cn=back_cn
                    )
                    audit_items.append(audit_item)
                    card_flagged = True
                    if overall_status != "RED":
                        overall_status = "YELLOW"
                    set_code, cn = fb_set, fb_cn
                    card_data = paper_fallback
                    initial_img = get_card_image_url(paper_fallback)
                else:
                    audit_item = CardAuditItem(
                        qty=qty, name=name, set_code=set_code, cn=cn,
                        status="RED", category="DIGITAL_ONLY",
                        message=f"'{name}' is digital-only and has no paper release.",
                        original_line=orig_line, fixed_line=orig_line, can_autofix=False,
                        original_image_url=initial_img,
                        original_version_label=f"Digital ({set_code.upper()} #{cn})",
                        back_name=back_name, back_set=back_set, back_cn=back_cn
                    )
                    audit_items.append(audit_item)
                    overall_status = "RED"
                    continue
            else:
                games = card_data.get("games", [])
                if card_data.get("digital") is True and games and "paper" not in games:
                    paper_check = find_clean_paper_fallback(name)
                    if not paper_check:
                        audit_item = CardAuditItem(
                            qty=qty, name=name, set_code=set_code, cn=cn,
                            status="RED", category="DIGITAL_ONLY",
                            message=f"'{name}' is digital-only and has no paper release.",
                            original_line=orig_line, fixed_line=orig_line, can_autofix=False,
                            original_image_url=initial_img,
                            original_version_label=f"Digital ({set_code.upper()} #{cn})",
                            back_name=back_name, back_set=back_set, back_cn=back_cn
                        )
                        audit_items.append(audit_item)
                        overall_status = "RED"
                        continue
                
        # 3. Check Promo Planeswalker Stamp
        if replace_stamps and has_planeswalker_stamp(card_data):
            clean_fallback = find_clean_paper_fallback(
                name,
                prefer_nonfoil=convert_foils,
                avoid_stamped=True,
                avoid_datestamped=replace_date_stamps
            )
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
                    original_image_url=initial_img,
                    fixed_image_url=get_card_image_url(clean_fallback),
                    original_version_label=f"Promo Stamped ({set_code.upper()} #{cn})",
                    fixed_version_label=f"Clean Paper ({fb_set.upper()} #{fb_cn})",
                    back_name=back_name, back_set=back_set, back_cn=back_cn
                )
                audit_items.append(audit_item)
                card_flagged = True
                if overall_status != "RED":
                    overall_status = "YELLOW"
                set_code, cn = fb_set, fb_cn
                card_data = clean_fallback
                initial_img = get_card_image_url(clean_fallback)

        # 3b. Check Date-Stamped Promo (Prerelease, etc.)
        if replace_date_stamps and has_date_stamp(card_data):
            clean_fallback = find_clean_paper_fallback(
                name,
                prefer_nonfoil=convert_foils,
                avoid_stamped=replace_stamps,
                avoid_datestamped=True
            )
            if clean_fallback:
                fb_set = clean_fallback.get("set", "").lower()
                fb_cn = clean_fallback.get("collector_number", "").strip()
                fixed = format_card_line(qty, name, fb_set, fb_cn, back_name, back_set, back_cn)
                audit_item = CardAuditItem(
                    qty=qty, name=name, set_code=set_code, cn=cn,
                    status="YELLOW", category="DATE_STAMP_REPLACED",
                    message=f"Date-stamped promo replaced with clean printing ({fb_set.upper()} #{fb_cn}).",
                    original_line=orig_line, fixed_line=fixed, can_autofix=True,
                    suggested_set=fb_set, suggested_cn=fb_cn,
                    original_image_url=initial_img,
                    fixed_image_url=get_card_image_url(clean_fallback),
                    original_version_label=f"Date Stamped ({set_code.upper()} #{cn})",
                    fixed_version_label=f"Clean Paper ({fb_set.upper()} #{fb_cn})",
                    back_name=back_name, back_set=back_set, back_cn=back_cn
                )
                audit_items.append(audit_item)
                card_flagged = True
                if overall_status != "RED":
                    overall_status = "YELLOW"
                set_code, cn = fb_set, fb_cn
                card_data = clean_fallback
                initial_img = get_card_image_url(clean_fallback)

        # 4. Check Foil-only
        if convert_foils and is_foil_only(card_data):
            nonfoil_fallback = find_clean_paper_fallback(
                name,
                prefer_nonfoil=True,
                avoid_stamped=replace_stamps,
                avoid_datestamped=replace_date_stamps
            )
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
                    original_image_url=initial_img,
                    fixed_image_url=get_card_image_url(nonfoil_fallback),
                    original_version_label=f"Foil-Only ({set_code.upper()} #{cn})",
                    fixed_version_label=f"Non-Foil Paper ({fb_set.upper()} #{fb_cn})",
                    back_name=back_name, back_set=back_set, back_cn=back_cn
                )
                audit_items.append(audit_item)
                card_flagged = True
                if overall_status != "RED":
                    overall_status = "YELLOW"
                set_code, cn = fb_set, fb_cn
                card_data = nonfoil_fallback
                initial_img = get_card_image_url(nonfoil_fallback)
                
        # 5. Check Language Scan Availability
        has_localized = True
        if preferred_language != "en":
            has_localized, localized_name = check_localized_scan(set_code, cn, preferred_language)
            
        final_line = format_card_line(qty, name, set_code, cn, back_name, back_set, back_cn)
        converted_lines.append(final_line)
        
        # If no issues were flagged above, card is 🟢 GREEN
        if not card_flagged:
            audit_item = CardAuditItem(
                qty=qty, name=name, set_code=set_code, cn=cn,
                status="GREEN", category="CLEAN",
                message="Card verified on Scryfall.",
                original_line=orig_line, fixed_line=final_line, can_autofix=False,
                localized_scan_available=has_localized,
                original_image_url=initial_img,
                fixed_image_url=initial_img,
                original_version_label=f"{name} ({set_code.upper()} #{cn})",
                fixed_version_label=f"{name} ({set_code.upper()} #{cn})",
                back_name=back_name, back_set=back_set, back_cn=back_cn
            )
            audit_items.append(audit_item)

    converted_text = "\n".join(converted_lines) + "\n"
    return audit_items, overall_status, converted_text
