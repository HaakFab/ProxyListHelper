"""Scryfall API client with caching, foil fallback & promo stamp replacement."""
import json
import os
import re
import time
import urllib.request
import urllib.parse
import urllib.error
from typing import Dict, List, Optional, Tuple

import unicodedata

SCRYFALL_HEADERS = {
    "User-Agent": "ProxyListHelper/1.0 (https://github.com/HaakFab/ProxyListHelper)",
    "Accept": "application/json",
}

# In-memory session cache to avoid redundant hits
_CACHE: Dict[str, dict] = {}

def get_card_image_url(card_data: Optional[dict]) -> Optional[str]:
    """Returns high quality image URL for a card or its front face."""
    if not card_data:
        return None
    if "image_uris" in card_data:
        return card_data["image_uris"].get("normal") or card_data["image_uris"].get("small") or card_data["image_uris"].get("large")
    if "card_faces" in card_data and len(card_data["card_faces"]) > 0:
        face = card_data["card_faces"][0]
        if "image_uris" in face:
            return face["image_uris"].get("normal") or face["image_uris"].get("small")
    return None

def preload_cards_batch(cards: List[dict]):
    """
    Batches Scryfall card lookups using the /cards/collection endpoint (up to 75 cards per batch).
    Populates _CACHE so subsequent lookups are instant.
    Preserves exact collector number casing to support The List (PLST) and variants.
    """
    identifiers = []
    seen = set()
    for c in cards:
        s = (c.get("set") or "").lower().strip()
        cn = str(c.get("cn") or "").lstrip("#").strip()
        name = (c.get("name") or "").strip()

        # Handle Alchemy 'A-' prefixes if present
        clean_name = re.sub(r"^A-", "", name).strip()
        clean_cn = re.sub(r"^[Aa]-", "", cn).strip()

        if s and cn:
            key = (s, cn)
            if key not in seen:
                seen.add(key)
                identifiers.append({"set": s, "collector_number": cn})
            if cn != cn.upper():
                upper_key = (s, cn.upper())
                if upper_key not in seen:
                    seen.add(upper_key)
                    identifiers.append({"set": s, "collector_number": cn.upper()})
            if clean_cn != cn:
                clean_key = (s, clean_cn)
                if clean_key not in seen:
                    seen.add(clean_key)
                    identifiers.append({"set": s, "collector_number": clean_cn})
        elif name:
            clean_split = clean_name.split(" // ")[0].split(" / ")[0].strip()
            key = ("name", clean_split.lower())
            if key not in seen:
                seen.add(key)
                identifiers.append({"name": clean_split})

    if not identifiers:
        return

    chunk_size = 75
    for i in range(0, len(identifiers), chunk_size):
        chunk = identifiers[i:i + chunk_size]
        payload = json.dumps({"identifiers": chunk}).encode("utf-8")
        req = urllib.request.Request(
            "https://api.scryfall.com/cards/collection",
            data=payload,
            headers={**SCRYFALL_HEADERS, "Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                for card in data.get("data", []):
                    c_set = card.get("set", "").lower()
                    c_cn = str(card.get("collector_number", "")).strip()
                    if c_set and c_cn:
                        _CACHE[f"https://api.scryfall.com/cards/{c_set}/{c_cn}"] = card
                        _CACHE[f"https://api.scryfall.com/cards/{c_set}/{c_cn.upper()}"] = card
                        _CACHE[f"https://api.scryfall.com/cards/{c_set}/{c_cn.lower()}"] = card
                    c_name = card.get("name", "").split(" // ")[0].strip()
                    if c_name:
                        _CACHE[f"https://api.scryfall.com/cards/named?exact={urllib.parse.quote(c_name)}"] = card
                for nf in data.get("not_found", []):
                    nf_set = nf.get("set", "").lower()
                    nf_cn = str(nf.get("collector_number", "")).strip()
                    if nf_set and nf_cn:
                        _CACHE[f"https://api.scryfall.com/cards/{nf_set}/{nf_cn}"] = None
                    if "name" in nf:
                        _CACHE[f"https://api.scryfall.com/cards/named?exact={urllib.parse.quote(nf['name'])}"] = None
        except Exception:
            pass


def _scryfall_get(url: str) -> Optional[dict]:
    if url in _CACHE:
        return _CACHE[url]
    
    time.sleep(0.08)  # Respect Scryfall 50-100ms rate limit
    req = urllib.request.Request(url, headers=SCRYFALL_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            _CACHE[url] = data
            return data
    except urllib.error.HTTPError as e:
        if e.code == 404:
            _CACHE[url] = None
            return None
        return None
    except Exception:
        return None

def get_card_by_set_cn(set_code: str, collector_number: str) -> Optional[dict]:
    """Look up a card by set code and collector number with case and zero fallbacks."""
    if not set_code or not collector_number:
        return None
    s = set_code.lower().strip()
    cn = str(collector_number).lstrip("#").strip()

    # 1. Try exact collector number
    data = _scryfall_get(f"https://api.scryfall.com/cards/{s}/{cn}")
    if data:
        return data

    # 2. Try uppercase collector number (crucial for PLST e.g. MH3-215, SLD variants)
    if cn != cn.upper():
        data = _scryfall_get(f"https://api.scryfall.com/cards/{s}/{cn.upper()}")
        if data:
            return data

    # 3. Try stripped leading zeros (e.g. 0401 -> 401)
    stripped_zeros = cn.lstrip("0")
    if stripped_zeros and stripped_zeros != cn:
        data = _scryfall_get(f"https://api.scryfall.com/cards/{s}/{stripped_zeros}")
        if data:
            return data

    # 4. Try stripping Arena Alchemy prefix 'A-' if present
    if cn.startswith("A-") or cn.startswith("a-"):
        data = _scryfall_get(f"https://api.scryfall.com/cards/{s}/{cn[2:].strip()}")
        if data:
            return data

    return None

def get_card_by_name(card_name: str) -> Optional[dict]:
    """Look up a card by name with Unicode punctuation and diacritic fallbacks."""
    if not card_name:
        return None
    # Normalize curly quotes and dashes
    clean_name = card_name.replace("’", "'").replace("‘", "'").replace("—", "-").replace("–", "-")
    # Normalize split card slashes
    clean_name = clean_name.replace(" / ", " // ")
    clean_name = clean_name.split(" // ")[0].strip()

    encoded = urllib.parse.quote(clean_name)
    url = f"https://api.scryfall.com/cards/named?exact={encoded}"
    data = _scryfall_get(url)
    if data:
        return data

    # Fallback with diacritics removed (e.g. Lim-Dûl -> Lim-Dul)
    decomposed = unicodedata.normalize('NFKD', clean_name)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    if stripped != clean_name:
        data_stripped = _scryfall_get(f"https://api.scryfall.com/cards/named?exact={urllib.parse.quote(stripped)}")
        if data_stripped:
            return data_stripped

    # Fallback to fuzzy search
    url_fuzzy = f"https://api.scryfall.com/cards/named?fuzzy={encoded}"
    return _scryfall_get(url_fuzzy)

def is_foil_only(card_data: dict) -> bool:
    """Checks if a card is only available in foil or etched."""
    finishes = card_data.get("finishes", [])
    return bool(finishes and "nonfoil" not in finishes)

def has_planeswalker_stamp(card_data: dict) -> bool:
    """Checks if a card has a promo planeswalker stamp."""
    promo_types = card_data.get("promo_types", []) or []
    if "planeswalkerstamped" in promo_types or "stamped" in promo_types or "promopack" in promo_types:
        return True
    return False

def has_date_stamp(card_data: dict) -> bool:
    """Checks if a card has a date stamp (prerelease promo, release promo, etc.)."""
    promo_types = card_data.get("promo_types", []) or []
    return any(pt in promo_types for pt in ("datestamped", "prerelease", "datestamp", "date_stamped"))

def is_digital_card(card_data: dict) -> bool:
    """Checks if a card printing is digital (MTGO, Arena, Alchemy, digital promo, etc.)."""
    if card_data.get("digital") is True:
        return True
    games = card_data.get("games", [])
    if games and "paper" not in games:
        return True
    set_code = (card_data.get("set") or "").lower()
    if set_code in ("vma", "tpr", "me1", "me2", "me3", "me4", "pz1", "pz2", "prm", "pmod"):
        return True
    promo_types = card_data.get("promo_types", []) or []
    if "digital" in promo_types:
        return True
    return False

def find_clean_paper_fallback(
    card_name: str,
    prefer_nonfoil: bool = True,
    avoid_stamped: bool = True,
    avoid_datestamped: bool = True
) -> Optional[dict]:
    """
    Finds a standard paper printing for a card name.
    Prioritizes non-foil, non-stamped, non-digital releases.
    """
    clean_name = card_name.split(" // ")[0].strip()
    encoded = urllib.parse.quote(clean_name)
    
    query_parts = [f'!\"{encoded}\"', "game:paper"]
    if prefer_nonfoil:
        query_parts.append("is:nonfoil")
    if avoid_stamped:
        query_parts.append("-is:stamped")
    if avoid_datestamped:
        query_parts.append("-is:datestamped")
    query_parts.append("not:digital")
        
    query_str = "+".join(query_parts)
    url = f"https://api.scryfall.com/cards/search?q={query_str}&order=released&dir=desc"
    data = _scryfall_get(url)
    
    if data and "data" in data and len(data["data"]) > 0:
        return data["data"][0]
        
    # Fallback to any paper printing if strict query had no hits
    url_fallback = f"https://api.scryfall.com/cards/search?q=!\"{encoded}\"+game:paper&order=released&dir=desc"
    data_fallback = _scryfall_get(url_fallback)
    if data_fallback and "data" in data_fallback and len(data_fallback["data"]) > 0:
        return data_fallback["data"][0]
        
    return None

def check_localized_scan(set_code: str, collector_number: str, lang: str) -> Tuple[bool, Optional[str]]:
    """
    Checks if Scryfall has a localized high-res scan for set/cn in lang.
    Returns: (has_scan, localized_printed_name)
    """
    if not lang or lang.lower() == "en":
        return True, None
        
    if not set_code or not collector_number:
        return False, None
        
    url = f"https://api.scryfall.com/cards/{set_code.lower()}/{collector_number.lower()}/{lang.lower()}"
    data = _scryfall_get(url)
    if not data:
        return False, None
        
    has_image = False
    if "image_uris" in data:
        has_image = bool(data["image_uris"].get("png") or data["image_uris"].get("large"))
    elif "card_faces" in data and len(data["card_faces"]) > 0:
        f0 = data["card_faces"][0]
        has_image = bool(f0.get("image_uris", {}).get("png") or f0.get("image_uris", {}).get("large"))
        
    printed_name = data.get("printed_name") or data.get("name")
    return has_image, printed_name
