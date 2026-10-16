import os
import time
import json
import requests
from typing import List, Dict, Tuple, Optional
from PIL import Image
from .deck_parser import CardEntry

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
}

API_HEADERS = {
    "User-Agent": "MoxfieldBatchProxyPDF/2.0",
    "Content-Type": "application/json",
    "Accept": "application/json"
}

def download_image_safe(url: str, dest_path: str, retries: int = 3) -> bool:
    """Downloads an image and verifies it contains valid image data before saving."""
    if os.path.exists(dest_path) and os.path.getsize(dest_path) > 1000:
        try:
            with Image.open(dest_path) as img:
                img.verify()
            return True
        except Exception:
            try:
                os.remove(dest_path)
            except OSError:
                pass

    os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)

    for attempt in range(retries):
        try:
            time.sleep(0.08)
            resp = requests.get(url, headers=HEADERS, timeout=30)
            if resp.status_code == 200 and len(resp.content) > 1000:
                with open(dest_path, "wb") as f:
                    f.write(resp.content)

                with Image.open(dest_path) as img:
                    img.verify()
                return True
        except Exception as e:
            if attempt == retries - 1:
                print(f"  [!] Failed to download image from {url}: {e}")
            time.sleep(1)

    return False

class ScryfallClient:
    def __init__(self, cache_dir: str):
        self.cache_dir = cache_dir
        os.makedirs(self.cache_dir, exist_ok=True)

    def get_card_metadata_path(self, key: str) -> str:
        return os.path.join(self.cache_dir, f"{key}_meta.json")

    def _is_foil_only(self, card_obj: dict) -> bool:
        """Checks if a card object is only available in foil or etched finishes."""
        finishes = card_obj.get("finishes", [])
        return bool(finishes and "nonfoil" not in finishes)

    def _find_nonfoil_or_paper_fallback(self, card_name: str) -> Optional[dict]:
        """
        Finds a non-foil, paper printing for the card.
        Queries Scryfall: '!"Name" is:nonfoil game:paper'.
        """
        clean_name = card_name.split(" // ")[0].strip()
        time.sleep(0.08)
        try:
            # 1. Search for non-foil paper printing
            url = f"https://api.scryfall.com/cards/search?q=!%22{requests.utils.quote(clean_name)}%22+is:nonfoil+game:paper&order=released&dir=desc"
            r = requests.get(url, headers=API_HEADERS, timeout=15)
            if r.status_code == 200:
                data = r.json()
                if "data" in data and len(data["data"]) > 0:
                    return data["data"][0]
        except Exception:
            pass

        time.sleep(0.08)
        try:
            # 2. Fallback to named exact
            url = f"https://api.scryfall.com/cards/named?exact={requests.utils.quote(clean_name)}"
            r = requests.get(url, headers=API_HEADERS, timeout=15)
            if r.status_code == 200:
                data = r.json()
                if "image_uris" in data or "card_faces" in data:
                    return data
        except Exception:
            pass

        return None

    def _find_localized_card(self, set_code: Optional[str], cn: Optional[str], lang: Optional[str]) -> Optional[dict]:
        """Queries Scryfall for a specific localized printing."""
        if not set_code or not cn or not lang or lang.lower() == "en":
            return None
        time.sleep(0.08)
        try:
            url = f"https://api.scryfall.com/cards/{set_code.lower()}/{cn.lower()}/{lang.lower()}"
            r = requests.get(url, headers=API_HEADERS, timeout=15)
            if r.status_code == 200:
                data = r.json()
                if "image_uris" in data or "card_faces" in data:
                    return data
        except Exception:
            pass
        return None

    def _save_card_asset(self, card_obj: dict, card_key: str) -> bool:
        """Downloads images and saves JSON metadata for card_key."""
        # 1. Single-faced card
        if "image_uris" in card_obj:
            img_url = card_obj["image_uris"].get("png") or card_obj["image_uris"].get("large")
            front_path = os.path.join(self.cache_dir, f"{card_key}_front.png")
            if img_url and download_image_safe(img_url, front_path):
                meta_path = self.get_card_metadata_path(card_key)
                with open(meta_path, "w", encoding="utf-8") as mf:
                    json.dump(card_obj, mf, indent=2)
                return True

        # 2. Double-faced / Transform / MDFC
        elif "card_faces" in card_obj and len(card_obj["card_faces"]) >= 2:
            face1 = card_obj["card_faces"][0]
            face2 = card_obj["card_faces"][1]

            url_f1 = face1.get("image_uris", {}).get("png") or face1.get("image_uris", {}).get("large")
            url_f2 = face2.get("image_uris", {}).get("png") or face2.get("image_uris", {}).get("large")

            front_path = os.path.join(self.cache_dir, f"{card_key}_front.png")
            back_path = os.path.join(self.cache_dir, f"{card_key}_back.png")

            f1_ok = download_image_safe(url_f1, front_path) if url_f1 else False
            f2_ok = download_image_safe(url_f2, back_path) if url_f2 else False

            if f1_ok:
                meta_path = self.get_card_metadata_path(card_key)
                with open(meta_path, "w", encoding="utf-8") as mf:
                    json.dump(card_obj, mf, indent=2)
                return True

        return False

    def fetch_cards_batch(self, cards: List[CardEntry], progress_callback=None) -> Dict[str, dict]:
        """
        Fetches cards via Scryfall Collection API in chunks of 75.
        - Enforces non-foil preference (if a card is foil-only, substitutes non-foil version).
        - Enforces fallback printing if requested printing is not found.
        - Saves raw images and JSON metadata into cache_dir.
        Returns mapping: {card_key: card_metadata_dict}
        """
        results: Dict[str, dict] = {}
        missing_cards: List[CardEntry] = []

        # Check existing cached metadata
        for card in cards:
            key = card.key
            meta_path = self.get_card_metadata_path(key)
            front_raw = os.path.join(self.cache_dir, f"{key}_front.png")
            if os.path.exists(meta_path) and os.path.exists(front_raw):
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                    
                    # If cached printing is foil-only or digital, attempt replacement with non-foil paper
                    if self._is_foil_only(meta) or meta.get("digital") is True:
                        fallback = self._find_nonfoil_or_paper_fallback(card.name)
                        if fallback and fallback.get("id") != meta.get("id"):
                            if self._save_card_asset(fallback, key):
                                results[key] = fallback
                                continue
                    results[key] = meta
                    continue
                except Exception:
                    pass
            missing_cards.append(card)

        if not missing_cards:
            return results

        url = "https://api.scryfall.com/cards/collection"
        total_missing = len(missing_cards)
        
        for i in range(0, total_missing, 75):
            chunk = missing_cards[i:i + 75]
            identifiers = []
            chunk_key_map: Dict[str, CardEntry] = {}

            for card in chunk:
                if card.set and card.cn:
                    s_code = card.set.lower()
                    c_num = card.cn.lower()
                    chunk_key_map[f"{s_code}_{c_num}"] = card
                    identifiers.append({"set": card.set, "collector_number": card.cn})
                else:
                    clean_name = card.name.replace(" / ", " // ")
                    identifiers.append({"name": clean_name})

                chunk_key_map[card.name.lower()] = card

            payload = {"identifiers": identifiers}
            time.sleep(0.12)
            
            resolved_keys = set()
            try:
                resp = requests.post(url, json=payload, headers=API_HEADERS, timeout=30)
                if resp.status_code == 200:
                    data = resp.json()
                    
                    for card_obj in data.get("data", []):
                        s_code = card_obj.get("set", "").lower()
                        c_num = card_obj.get("collector_number", "").lower()
                        c_name = card_obj.get("name", "").lower()

                        target_card = chunk_key_map.get(f"{s_code}_{c_num}") or chunk_key_map.get(c_name)
                        if not target_card:
                            continue

                        # User rule: if foil-only or digital, find non-foil paper printing
                        if self._is_foil_only(card_obj) or card_obj.get("digital") is True:
                            fallback = self._find_nonfoil_or_paper_fallback(card_obj.get("name", target_card.name))
                            if fallback and (not self._is_foil_only(fallback) or not fallback.get("digital")):
                                print(f"  [i] Replacing foil/digital printing for '{target_card.name}' ({target_card.set} {target_card.cn}) with non-foil paper printing ({fallback.get('set', '').upper()} #{fallback.get('collector_number', '')})")
                                card_obj = fallback

                        # If a non-English language was requested, attempt to fetch localized asset
                        if getattr(target_card, "lang", None) and target_card.lang.lower() != "en":
                            localized = self._find_localized_card(
                                card_obj.get("set", target_card.set),
                                card_obj.get("collector_number", target_card.cn),
                                target_card.lang
                            )
                            if localized:
                                card_obj = localized

                        if self._save_card_asset(card_obj, target_card.key):
                            results[target_card.key] = card_obj
                            resolved_keys.add(target_card.key)

            except Exception as ex:
                print(f"  [!] Error during Scryfall API batch request: {ex}")

            # Fallback for any cards in chunk not resolved via collection API
            for card in chunk:
                if card.key not in resolved_keys:
                    if getattr(card, "lang", None) and card.lang.lower() != "en":
                        localized = self._find_localized_card(card.set, card.cn, card.lang)
                        if localized and self._save_card_asset(localized, card.key):
                            results[card.key] = localized
                            resolved_keys.add(card.key)
                            continue

                    fallback = self._find_nonfoil_or_paper_fallback(card.name)
                    if fallback:
                        print(f"  [i] Fallback printing for '{card.name}' ({card.set or '?'} #{card.cn or '?'}) -> using non-foil paper ({fallback.get('set', '').upper()} #{fallback.get('collector_number', '')})")
                        if self._save_card_asset(fallback, card.key):
                            results[card.key] = fallback
                    else:
                        print(f"  [!] Card '{card.name}' not found on Scryfall.")

            if progress_callback:
                progress_callback(min(i + 75, total_missing), total_missing)

        return results
