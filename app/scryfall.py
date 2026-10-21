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

        # Preload back face if card is custom double-sided
        bs = (c.get("back_set") or "").lower().strip()
        bcn = str(c.get("back_cn") or "").lstrip("#").strip()
        bname = (c.get("back_name") or "").strip()
        if bs and bcn:
            bkey = (bs, bcn)
            if bkey not in seen:
                seen.add(bkey)
                identifiers.append({"set": bs, "collector_number": bcn})
        elif bname:
            b_split = bname.split(" // ")[0].split(" / ")[0].strip()
            bkey = ("name", b_split.lower())
            if bkey not in seen:
                seen.add(bkey)
                identifiers.append({"name": b_split})

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

def is_exact_or_front_face_match(candidate_card: dict, target_name: str) -> bool:
    """
    Checks if a candidate card object has an identical name to target_name.
    Discards cards where the target card is merely a secondary face / prepared spell
    (e.g. discarding 'Grave Researcher // Reanimate' when searching for 'Reanimate').
    """
    if not candidate_card or not target_name:
        return False

    def _norm(s: str) -> str:
        s = s.strip().lower()
        s = s.replace("’", "'").replace("‘", "'").replace("—", "-").replace("–", "-")
        return s

    target_norm = _norm(target_name)
    target_primary = _norm(target_name.split(" // ")[0].split(" / ")[0])

    card_name = _norm(candidate_card.get("name") or "")

    # 1. Exact full name match (e.g. 'Reanimate' == 'Reanimate' or 'Fire // Ice' == 'Fire // Ice')
    if card_name == target_norm or card_name == target_primary:
        return True

    # 2. Front face exact match for multi-faced cards
    faces = candidate_card.get("card_faces") or []
    if faces and len(faces) > 0:
        front_face_name = _norm(faces[0].get("name") or "")
        if front_face_name == target_norm or front_face_name == target_primary:
            return True

    # 3. Split card full name starts with primary target face (e.g. 'Fire // Ice' when input was 'Fire')
    if card_name.startswith(f"{target_primary} //") or card_name.startswith(f"{target_primary} /"):
        return True

    return False


def _pick_best_name_match(candidates: List[dict], target_name: str) -> Optional[dict]:
    """
    Selects the candidate that genuinely matches target_name.
    Discards any candidate where the card name or primary front face does not match
    (e.g. discarding 'Grave Researcher // Reanimate' when searching for 'Reanimate').
    """
    if not candidates or not target_name:
        return None

    # Discard any candidate whose name does not match target_name
    valid = [c for c in candidates if is_exact_or_front_face_match(c, target_name)]
    if not valid:
        return None

    clean = target_name.strip().lower().replace("’", "'").replace("‘", "'").replace("—", "-").replace("–", "-")
    clean_split = clean.split(" // ")[0].split(" / ")[0].strip()

    # Priority 1: Full card name exact match (e.g. 'Reanimate' == 'Reanimate')
    for c in valid:
        c_name = (c.get("name") or "").strip().lower()
        if c_name == clean or c_name == clean_split:
            return c

    # Priority 2: Front face exact match (e.g. 'Brazen Borrower' on 'Brazen Borrower // Petty Theft')
    for c in valid:
        faces = c.get("card_faces") or []
        if faces and len(faces) > 0:
            f0_name = (faces[0].get("name") or "").strip().lower()
            if f0_name == clean or f0_name == clean_split:
                return c

    return valid[0]

def find_clean_paper_fallback(
    card_name: str,
    prefer_nonfoil: bool = True,
    avoid_stamped: bool = True,
    avoid_datestamped: bool = True,
    preferred_set: Optional[str] = None
) -> Optional[dict]:
    """
    Finds a standard paper printing for a card name.
    Prioritizes same-set printing if available, and non-foil, non-stamped, non-digital releases.
    Properly URL-encodes queries to prevent Scryfall 400 Bad Request errors.
    """
    clean_name = card_name.split(" // ")[0].strip()
    clean_name = clean_name.replace("’", "'").replace("‘", "'").replace("—", "-").replace("–", "-")
    
    # Base strict search parameters: paper, non-digital, and user-selected filters
    query_parts = [f'!\"{clean_name}\"', "game:paper", "not:digital"]
    if prefer_nonfoil:
        query_parts.append("is:nonfoil")
    if avoid_stamped:
        query_parts.append("-is:stamped")
    if avoid_datestamped:
        query_parts.append("-is:datestamped")

    # 0. Same-set priority: if a preferred set is specified, try same set in non-foil first
    if preferred_set:
        clean_set = preferred_set.strip().lower()
        if clean_set:
            same_set_query = query_parts + [f"s:{clean_set}"]
            params_set = urllib.parse.urlencode({"q": " ".join(same_set_query), "order": "released", "dir": "desc"})
            data_set = _scryfall_get(f"https://api.scryfall.com/cards/search?{params_set}")
            if data_set and "data" in data_set and len(data_set["data"]) > 0:
                match = _pick_best_name_match(data_set["data"], clean_name)
                if match:
                    return match
        
    params = urllib.parse.urlencode({"q": " ".join(query_parts), "order": "released", "dir": "desc"})
    url = f"https://api.scryfall.com/cards/search?{params}"
    data = _scryfall_get(url)
    if data and "data" in data and len(data["data"]) > 0:
        match = _pick_best_name_match(data["data"], clean_name)
        if match:
            return match
        
    # 2. Relaxed search: keep avoid_stamped and avoid_datestamped, relax nonfoil
    if prefer_nonfoil:
        relaxed_parts = [f'!\"{clean_name}\"', "game:paper", "not:digital"]
        if avoid_stamped:
            relaxed_parts.append("-is:stamped")
        if avoid_datestamped:
            relaxed_parts.append("-is:datestamped")
        params_rel = urllib.parse.urlencode({"q": " ".join(relaxed_parts), "order": "released", "dir": "desc"})
        data_rel = _scryfall_get(f"https://api.scryfall.com/cards/search?{params_rel}")
        if data_rel and "data" in data_rel and len(data_rel["data"]) > 0:
            match = _pick_best_name_match(data_rel["data"], clean_name)
            if match:
                return match

    # 3. Paper non-digital fallback
    params_pnd = urllib.parse.urlencode({"q": f'!\"{clean_name}\" game:paper not:digital', "order": "released", "dir": "desc"})
    data_pnd = _scryfall_get(f"https://api.scryfall.com/cards/search?{params_pnd}")
    if data_pnd and "data" in data_pnd and len(data_pnd["data"]) > 0:
        match = _pick_best_name_match(data_pnd["data"], clean_name)
        if match:
            return match

    # 4. Any paper fallback
    params_paper = urllib.parse.urlencode({"q": f'!\"{clean_name}\" game:paper', "order": "released", "dir": "desc"})
    data_paper = _scryfall_get(f"https://api.scryfall.com/cards/search?{params_paper}")
    if data_paper and "data" in data_paper and len(data_paper["data"]) > 0:
        match = _pick_best_name_match(data_paper["data"], clean_name)
        if match:
            return match
        
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


def get_card_versions(card_name: str) -> List[dict]:
    """
    Returns all printings/versions of a card from Scryfall ordered by release date descending.
    Each version dictionary contains set code, collector number, set name, release year,
    image URL, finishes (foil/nonfoil), flags for stamps, promo, and digital.
    """
    if not card_name:
        return []

    clean_name = card_name.split(" // ")[0].strip()
    clean_name = clean_name.replace("’", "'").replace("‘", "'").replace("—", "-").replace("–", "-")
    clean_name = re.sub(r"^[Aa]-", "", clean_name).strip()

    # 1. Try finding card first to get prints_search_uri or oracle_id
    card = get_card_by_name(clean_name)
    prints_url = None
    if card and card.get("prints_search_uri"):
        prints_url = card["prints_search_uri"]
    else:
        params = urllib.parse.urlencode({
            "q": f'!\"{clean_name}\"',
            "unique": "prints",
            "order": "released",
            "dir": "desc"
        })
        prints_url = f"https://api.scryfall.com/cards/search?{params}"

    data = _scryfall_get(prints_url)
    if not data or "data" not in data:
        return []

    raw_list = list(data["data"])
    # If there's a second page and total is under 200, fetch next page
    if data.get("has_more") and data.get("next_page") and len(raw_list) < 200:
        data_p2 = _scryfall_get(data["next_page"])
        if data_p2 and "data" in data_p2:
            raw_list.extend(data_p2["data"])

    versions = []
    seen = set()
    for c in raw_list:
        # Verify card genuinely matches target card name (exclude cards where target is only a secondary/prepared face)
        if c.get("name") and not is_exact_or_front_face_match(c, clean_name):
            continue

        c_set = (c.get("set") or "").lower()
        c_cn = str(c.get("collector_number") or "").strip()
        key = (c_set, c_cn)
        if key in seen:
            continue
        seen.add(key)

        finishes = c.get("finishes", []) or []
        promo_types = c.get("promo_types", []) or []
        frame_effects = c.get("frame_effects", []) or []

        is_digital = is_digital_card(c)
        is_foil_only_val = is_foil_only(c)
        has_pw_stamp = has_planeswalker_stamp(c)
        has_dt_stamp = has_date_stamp(c)

        set_name = c.get("set_name") or c_set.upper()
        released_at = c.get("released_at", "")
        released_year = released_at[:4] if released_at else ""

        version_label = f"{c_set.upper()} #{c_cn}"
        if promo_types:
            promo_note = promo_types[0].replace("_", " ").title()
            version_label += f" ({promo_note})"

        img_url = get_card_image_url(c)

        versions.append({
            "set": c_set,
            "collector_number": c_cn,
            "set_name": set_name,
            "released_at": released_at,
            "released_year": released_year,
            "version_label": version_label,
            "image_url": img_url,
            "finishes": finishes,
            "is_foil_only": is_foil_only_val,
            "has_planeswalker_stamp": has_pw_stamp,
            "has_date_stamp": has_dt_stamp,
            "is_digital": is_digital,
            "rarity": c.get("rarity", ""),
            "frame_effects": frame_effects,
            "promo_types": promo_types,
            "artist": c.get("artist", ""),
        })

    return versions

