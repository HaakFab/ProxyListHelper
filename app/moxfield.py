"""Moxfield decklist fetcher and parser."""
import json
import re
import urllib.request
import urllib.error
from typing import Dict, List, Optional, Tuple

from proxy_parser.moxfield_line import is_section_header, parse_line

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

def parse_deck_id(url_or_id: str) -> str:
    """Extracts public deck ID from a Moxfield URL or raw ID."""
    url_or_id = url_or_id.strip()
    match = re.search(r"decks/([a-zA-Z0-9_\-]+)", url_or_id)
    if match:
        return match.group(1)
    return url_or_id

def fetch_moxfield_deck(deck_id: str) -> dict:
    """
    Fetches full deck JSON from Moxfield API v3.
    Raises ValueError or urllib.error.URLError on failure.
    """
    clean_id = parse_deck_id(deck_id)
    url = f"https://api2.moxfield.com/v3/decks/all/{clean_id}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Referer": "https://www.moxfield.com/",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise ValueError(f"Deck '{clean_id}' not found on Moxfield (is it private or deleted?).")
        elif e.code == 403:
            raise ValueError("Moxfield blocked the request (Cloudflare challenge). Please use 'Paste Text' tab.")
        raise ValueError(f"Moxfield API returned HTTP {e.code}: {e.reason}")
    except Exception as e:
        raise ValueError(f"Could not connect to Moxfield: {e}")

def extract_deck_cards(
    deck_data: dict,
    include_sideboard: bool = False,
    include_considering: bool = False,
    include_tokens: bool = False,
    include_stickers: bool = False
) -> Tuple[str, List[dict]]:
    """
    Extracts cards from Moxfield deck JSON according to options.
    Returns: (deck_title, list_of_card_dicts)
    """
    deck_name = deck_data.get("name", "Untitled Deck").strip()
    boards = deck_data.get("boards", {})
    
    # Boards to include
    target_boards = ["commanders", "mainboard", "companions", "signatureSpells"]
    if include_sideboard:
        target_boards.append("sideboard")
    if include_considering:
        target_boards.append("maybeboard")  # Moxfield calls 'Considering' maybeboard
        
    extracted_cards: List[dict] = []
    
    for board_name in target_boards:
        board_data = boards.get(board_name, {})
        cards_dict = board_data.get("cards", {})
        
        for item in cards_dict.values():
            qty = int(item.get("quantity", 1))
            finish = item.get("finish", "nonFoil")
            is_foil = bool(item.get("isFoil", False) or finish in ("foil", "etched"))
            card_info = item.get("card", {})
            
            name = card_info.get("name", "")
            set_code = card_info.get("set", "").lower()
            collector_number = str(card_info.get("cn", "")).strip()
            promo_types = card_info.get("promo_types", []) or []
            
            extracted_cards.append({
                "qty": qty,
                "name": name,
                "set": set_code,
                "cn": collector_number,
                "is_foil": is_foil,
                "promo_types": promo_types,
                "board": board_name,
            })
            
    # Include Tokens if selected (only 1 Token of each type)
    if include_tokens:
        seen_token_types = set()

        def _token_type_key(t_name: str) -> str:
            k = (t_name or "").strip().lower()
            if k.endswith(" token") and len(k) > 6:
                k = k[:-6].strip()
            return k

        # 1. From boards.tokens (user-explicit tokens take precedence)
        board_tokens = boards.get("tokens", {}).get("cards", {})
        for item in board_tokens.values():
            card_info = item.get("card", {})
            t_name = card_info.get("name", "")
            key = _token_type_key(t_name)
            if not key or key in seen_token_types:
                continue
            seen_token_types.add(key)
            extracted_cards.append({
                "qty": 1,
                "name": t_name,
                "set": card_info.get("set", "").lower(),
                "cn": str(card_info.get("cn", "")).strip(),
                "is_foil": False,
                "promo_types": card_info.get("promo_types", []) or [],
                "board": "tokens",
            })
            
        # 2. From deck_data.tokens list (filter out spellbook/related normal cards)
        for tok in deck_data.get("tokens", []):
            is_token_flag = tok.get("isToken")
            layout = tok.get("layout", "")
            type_line = tok.get("type_line", "")
            set_code = (tok.get("set") or "").lower()

            # Ignore cards explicitly marked as non-token or having a standard normal layout without token type
            if is_token_flag is False and layout == "normal":
                continue
            is_real_token = (
                is_token_flag is True
                or layout in ("token", "double_faced_token", "emblem")
                or type_line.startswith("Token")
                or type_line.startswith("Emblem")
                or set_code.startswith("t")
            )
            if not is_real_token:
                continue

            t_name = tok.get("name", "")
            key = _token_type_key(t_name)
            if not key or key in seen_token_types:
                continue
            seen_token_types.add(key)
            extracted_cards.append({
                "qty": 1,
                "name": t_name,
                "set": set_code,
                "cn": str(tok.get("cn", "")).strip(),
                "is_foil": False,
                "promo_types": tok.get("promo_types", []) or [],
                "board": "tokens",
            })

    # Include Stickers if selected
    if include_stickers:
        seen_stickers = set()
        # 1. From boards.stickers
        board_stickers = boards.get("stickers", {}).get("cards", {})
        for item in board_stickers.values():
            card_info = item.get("card", {})
            key = (card_info.get("name"), card_info.get("set", "").lower(), str(card_info.get("cn", "")))
            seen_stickers.add(key)
            extracted_cards.append({
                "qty": int(item.get("quantity", 1)),
                "name": card_info.get("name", ""),
                "set": card_info.get("set", "").lower(),
                "cn": str(card_info.get("cn", "")).strip(),
                "is_foil": False,
                "promo_types": card_info.get("promo_types", []) or [],
                "board": "stickers",
            })

        # 2. From deck_data.stickers list (if present)
        for st in deck_data.get("stickers", []):
            if isinstance(st, dict):
                card_obj = st.get("card", st)
                name = card_obj.get("name", "")
                set_code = (card_obj.get("set") or "").lower()
                cn = str(card_obj.get("cn") or card_obj.get("collector_number") or "").strip()
                key = (name, set_code, cn)
                if key not in seen_stickers and name:
                    seen_stickers.add(key)
                    extracted_cards.append({
                        "qty": int(st.get("quantity", 1)),
                        "name": name,
                        "set": set_code,
                        "cn": cn,
                        "is_foil": False,
                        "promo_types": card_obj.get("promo_types", []) or [],
                        "board": "stickers",
                    })

    return deck_name, extracted_cards

def parse_pasted_text(text: str) -> List[dict]:
    """Parses standard Moxfield pasted text lines."""
    cards: List[dict] = []
    current_board = "mainboard"
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if is_section_header(line):
            low = line.lower()
            if "token" in low:
                current_board = "tokens"
            elif "sideboard" in low:
                current_board = "sideboard"
            elif "maybeboard" in low or "considering" in low:
                current_board = "maybeboard"
            elif "sticker" in low:
                current_board = "stickers"
            elif "main" in low or "commander" in low:
                current_board = "mainboard"
            continue
        parsed = parse_line(line)
        if parsed:
            is_foil = "*f*" in line.lower() or "*e*" in line.lower()
            cards.append({
                "qty": parsed.qty,
                "name": parsed.name,
                "set": (parsed.set_code or "").lower(),
                "cn": parsed.collector_number or "",
                "back_name": parsed.back_name,
                "back_set": (parsed.back_set_code or "").lower() if parsed.back_set_code else None,
                "back_cn": parsed.back_collector_number,
                "is_foil": is_foil,
                "promo_types": [],
                "board": current_board,
            })
    return cards
