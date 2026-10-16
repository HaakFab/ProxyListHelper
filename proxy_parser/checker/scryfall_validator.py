import time
import requests
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple, Any
from ..moxfield_line import render_line
from .content_validator import ParsedCardLine
from .scryfall_cache import (
    NAME,
    NONFOIL,
    PAPER,
    PRINTING,
    FileScryfallCache,
    ScryfallCache,
)

# Scryfall asks clients to identify themselves and provide a contact. Keep this a
# role address, never a personal one: it is sent to a third party on every request.
API_HEADERS = {
    "User-Agent": "MTGProxyChecker/2.0 (private non-commercial proxy printing tool)",
    "Content-Type": "application/json",
    "Accept": "application/json"
}

# Retry schedule for transient Scryfall failures (429 / 5xx / network error).
RETRY_DELAYS_SECONDS = (1, 4, 16)


def _canonical_name(name: str) -> str:
    """Scryfall spells double-faced cards 'A // B'; exports often write 'A / B'."""
    return name.replace(" / ", " // ").strip()


class ScryfallUnavailable(RuntimeError):
    """Scryfall could not be reached, so validation is incomplete.

    Raised rather than returning partial results: a batch that silently yields no
    diagnostics looks identical to a clean list, which would let an unvalidated
    decklist pass as GREEN.
    """

@dataclass
class CardDiagnostic:
    card_name: str
    line_number: int
    set_code: Optional[str]
    collector_number: Optional[str]
    severity: str                   # "RED" (fatal), "YELLOW" (warning/auto-fixable), "INFO"
    category: str                   # "NOT_FOUND", "PRINTING_NOT_FOUND", "DIGITAL_ONLY", "FOIL_ONLY", "LOW_RES", "OVERSIZED", "DOUBLE_FACED"
    message: str
    can_autofix: bool = False
    suggested_fix: Optional[str] = None
    scryfall_uri: Optional[str] = None

class ScryfallBatchValidator:
    def __init__(self, cache_dir: str = "scryfall_cache", cache: Optional[ScryfallCache] = None):
        self.cache_dir = cache_dir
        self.cache: ScryfallCache = cache or FileScryfallCache(cache_dir)

    def validate_cards(self, cards: List[ParsedCardLine]) -> List[CardDiagnostic]:
        """Validates a list of parsed cards against Scryfall API.

        A custom double-sided card ("1 Front &&& Back") contributes two
        independent queries here -- the front (the `ParsedCardLine` itself)
        and a synthetic query for the back's own printing. Both must resolve
        for the physical card to be printable, so both are validated exactly
        like any other card entry.

        Raises ScryfallUnavailable if the API cannot be reached, rather than
        returning an incomplete (and therefore falsely clean) diagnosis.
        """
        diagnostics: List[CardDiagnostic] = []

        # (query, front_ref): front_ref is set only when `query` is the back
        # half of a composite line, and points back at the original front
        # ParsedCardLine so fixes can be rendered as a full "&&&" line.
        query_list: List[Tuple[ParsedCardLine, Optional[ParsedCardLine]]] = []
        for card in cards:
            query_list.append((card, None))
            if card.is_custom_double_sided:
                query_list.append((self._back_query(card), card))

        needed_queries: List[Tuple[ParsedCardLine, Optional[ParsedCardLine]]] = []

        # 1. Check the local cache
        for card, front_ref in query_list:
            meta = None
            if card.has_set_code and card.has_collector_number:
                meta = self.cache.get(PRINTING, f"{card.set_code}_{card.collector_number}")
            if meta is None and not card.has_set_code:
                # Only fall back to a name lookup when no printing was requested;
                # otherwise a wrong set code would silently validate against
                # some other printing of the same card.
                meta = self.cache.get(NAME, _canonical_name(card.name))

            if meta:
                self._inspect_card_metadata(card, meta, diagnostics, front_ref=front_ref)
            else:
                needed_queries.append((card, front_ref))

        if not needed_queries:
            return diagnostics

        # 2. Batch fetch missing cards via Scryfall Collection API
        url = "https://api.scryfall.com/cards/collection"

        for i in range(0, len(needed_queries), 75):
            chunk = needed_queries[i:i + 75]
            identifiers = []
            for c, _ in chunk:
                if c.has_set_code and c.has_collector_number:
                    identifiers.append({"set": c.set_code, "collector_number": c.collector_number})
                else:
                    identifiers.append({"name": _canonical_name(c.name)})

            time.sleep(0.12)
            data = self._post_collection(url, identifiers)

            found_cards = data.get("data", [])

            # Map found cards
            by_printing: Dict[str, dict] = {}
            by_name: Dict[str, dict] = {}
            for obj in found_cards:
                s_code = obj.get("set", "").lower()
                cn = obj.get("collector_number", "").lower()
                name = obj.get("name", "").lower()

                if s_code and cn:
                    by_printing[f"{s_code}_{cn}"] = obj
                    self.cache.put(PRINTING, f"{s_code}_{cn}", obj)
                if name:
                    by_name.setdefault(name, obj)
                    self.cache.put(NAME, name, obj)

            for c, front_ref in chunk:
                meta = None
                if c.has_set_code and c.has_collector_number:
                    meta = by_printing.get(f"{c.set_code}_{c.collector_number}".lower())
                if meta is None and not c.has_set_code:
                    meta = by_name.get(_canonical_name(c.name).lower())
                if meta:
                    self._inspect_card_metadata(c, meta, diagnostics, front_ref=front_ref)
                else:
                    # Card was not found with set/cn identifier: check if name exists in Scryfall
                    self._handle_not_found(c, diagnostics, front_ref=front_ref)

        return diagnostics

    @staticmethod
    def _back_query(card: ParsedCardLine) -> ParsedCardLine:
        """Builds the independent lookup for a composite line's back side."""
        return ParsedCardLine(
            line_number=card.line_number,
            raw_line=card.raw_line,
            qty=card.qty,
            name=card.back_name,
            set_code=card.back_set_code,
            collector_number=card.back_collector_number,
            has_set_code=bool(card.back_set_code),
            has_collector_number=bool(card.back_collector_number),
        )

    @staticmethod
    def _render_suggested_line(
        card: ParsedCardLine,
        front_ref: Optional[ParsedCardLine],
        name: str,
        set_code: Optional[str],
        collector_number: Optional[str],
    ) -> str:
        """Builds a full replacement line for a substitution fix.

        `card` is the side being resolved; `name`/`set_code`/`collector_number`
        are its replacement printing. When this query is one half of a custom
        double-sided ("&&&") pairing, the other half is preserved unchanged so
        the fix never silently drops it.
        """
        if front_ref is not None:
            # `card` is the back query; keep the front side as originally
            # written and swap in the resolved back printing.
            return render_line(
                front_ref.qty, front_ref.name, front_ref.set_code, front_ref.collector_number,
                back_name=name, back_set_code=set_code, back_collector_number=collector_number,
            )
        if card.is_custom_double_sided:
            # `card` is the front of a composite line; keep the back side as
            # originally written and swap in the resolved front printing.
            return render_line(
                card.qty, name, set_code, collector_number,
                back_name=card.back_name,
                back_set_code=card.back_set_code,
                back_collector_number=card.back_collector_number,
            )
        return render_line(card.qty, name, set_code, collector_number)

    def _post_collection(self, url: str, identifiers: List[dict]) -> dict:
        """POSTs one batch to the Scryfall collection endpoint, with retries.

        Raises ScryfallUnavailable once the retries are exhausted. Returning
        partial results instead would make an unvalidated decklist
        indistinguishable from a clean one.
        """
        last_error = "unknown error"

        for delay in (0,) + RETRY_DELAYS_SECONDS:
            if delay:
                time.sleep(delay)
            try:
                resp = requests.post(
                    url, json={"identifiers": identifiers}, headers=API_HEADERS, timeout=25
                )
            except Exception as ex:
                last_error = f"network error: {ex}"
                continue

            if resp.status_code == 200:
                try:
                    return resp.json()
                except Exception as ex:
                    last_error = f"malformed response: {ex}"
                    continue

            last_error = f"HTTP {resp.status_code}"

            # 4xx other than rate limiting is a bad request; retrying will not help.
            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                break

            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                if retry_after:
                    try:
                        time.sleep(min(float(retry_after), 30))
                    except ValueError:
                        pass

        raise ScryfallUnavailable(
            f"Scryfall validation failed after {len(RETRY_DELAYS_SECONDS) + 1} attempts "
            f"({last_error}). Card validation is incomplete; results cannot be trusted."
        )

    def _handle_not_found(
        self,
        card: ParsedCardLine,
        diagnostics: List[CardDiagnostic],
        front_ref: Optional[ParsedCardLine] = None,
    ):
        """Runs a fallback search to determine if the set/number was wrong vs entire card not existing."""
        label = card.name if front_ref is None else f"{card.name} (custom back, line {card.line_number})"
        meta = self._find_nonfoil_fallback(card.name)
        if meta and ("image_uris" in meta or "card_faces" in meta):
            meta_set = meta.get("set", "").upper()
            meta_cn = meta.get("collector_number", "")
            diagnostics.append(CardDiagnostic(
                card_name=label,
                line_number=card.line_number,
                set_code=card.set_code,
                collector_number=card.collector_number,
                severity="YELLOW",
                category="PRINTING_NOT_FOUND",
                message=f"Printing ({card.set_code or '?'}) #{card.collector_number or '?'} not found on Scryfall for '{label}', but valid non-foil paper printing exists in '{meta_set} #{meta_cn}'",
                can_autofix=True,
                suggested_fix=self._render_suggested_line(
                    card, front_ref, meta.get("name", card.name), meta_set, meta_cn
                ),
                scryfall_uri=meta.get("scryfall_uri")
            ))
        else:
            diagnostics.append(CardDiagnostic(
                card_name=label,
                line_number=card.line_number,
                set_code=card.set_code,
                collector_number=card.collector_number,
                severity="RED",
                category="NOT_FOUND",
                message=f"Card name '{label}' was not found on Scryfall (possible typo or unreleased card)",
                can_autofix=False,
                suggested_fix=None,
                scryfall_uri=None
            ))

    def _find_nonfoil_fallback(self, card_name: str) -> Optional[dict]:
        """Looks up whether a non-foil paper printing exists for this card name."""
        clean_name = card_name.split(" // ")[0].strip()
        cached = self.cache.get(NONFOIL, clean_name)
        if cached is not None:
            return cached

        time.sleep(0.08)
        try:
            url = f"https://api.scryfall.com/cards/search?q=!%22{requests.utils.quote(clean_name)}%22+is:nonfoil+game:paper&order=released&dir=desc"
            r = requests.get(url, headers=API_HEADERS, timeout=12)
            if r.status_code == 200:
                data = r.json()
                if "data" in data and len(data["data"]) > 0:
                    cand = data["data"][0]
                    self.cache.put(NONFOIL, clean_name, cand)
                    return cand
        except Exception:
            pass

        return self._find_paper_fallback(card_name)

    def _find_paper_fallback(self, card_name: str) -> Optional[dict]:
        """Looks up whether a physical paper printing exists for this card name."""
        clean_name = card_name.split(" // ")[0].strip()
        cached = self.cache.get(PAPER, clean_name)
        if cached is not None:
            return cached

        try:
            time.sleep(0.1)
            url = f"https://api.scryfall.com/cards/named?exact={requests.utils.quote(clean_name)}"
            resp = requests.get(url, headers=API_HEADERS, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if not data.get("digital") or "paper" in data.get("games", []):
                    self.cache.put(PAPER, clean_name, data)
                    return data
        except Exception:
            pass
        return None

    def _inspect_card_metadata(
        self,
        card: ParsedCardLine,
        meta: dict,
        diagnostics: List[CardDiagnostic],
        front_ref: Optional[ParsedCardLine] = None,
    ):
        name = meta.get("name", card.name)
        uri = meta.get("scryfall_uri")

        label = name if front_ref is None else f"{name} (custom back, line {card.line_number})"

        # 1. Digital-only check (MTG Arena / Magic Online exclusive)
        if meta.get("digital") is True:
            paper_meta = self._find_nonfoil_fallback(name)
            if paper_meta and (not paper_meta.get("digital") or "paper" in paper_meta.get("games", [])):
                paper_set = paper_meta.get("set", "").upper()
                paper_cn = paper_meta.get("collector_number", "")
                diagnostics.append(CardDiagnostic(
                    card_name=label,
                    line_number=card.line_number,
                    set_code=card.set_code,
                    collector_number=card.collector_number,
                    severity="YELLOW",
                    category="DIGITAL_PRINTING",
                    message=f"'{label}' was requested in digital printing ({card.set_code.upper() if card.set_code else '?'} #{card.collector_number or '?'}), but paper printing exists in {paper_set} #{paper_cn}",
                    can_autofix=True,
                    suggested_fix=self._render_suggested_line(card, front_ref, name, paper_set, paper_cn),
                    scryfall_uri=paper_meta.get("scryfall_uri")
                ))
            else:
                diagnostics.append(CardDiagnostic(
                    card_name=label,
                    line_number=card.line_number,
                    set_code=card.set_code,
                    collector_number=card.collector_number,
                    severity="RED",
                    category="DIGITAL_ONLY",
                    message=f"'{label}' is a digital-only card (MTG Arena/Alchemy). No high-res physical paper scan exists.",
                    can_autofix=False,
                    scryfall_uri=uri
                ))

        # 2. Foil-only printing
        finishes = meta.get("finishes", [])
        if finishes and all(f in ("foil", "etched") for f in finishes):
            nonfoil_meta = self._find_nonfoil_fallback(name)
            if nonfoil_meta and "nonfoil" in nonfoil_meta.get("finishes", []):
                nf_set = nonfoil_meta.get("set", "").upper()
                nf_cn = nonfoil_meta.get("collector_number", "")
                diagnostics.append(CardDiagnostic(
                    card_name=label,
                    line_number=card.line_number,
                    set_code=card.set_code,
                    collector_number=card.collector_number,
                    severity="YELLOW",
                    category="FOIL_ONLY",
                    message=f"'{label}' ({meta.get('set','').upper()} #{meta.get('collector_number','')}) is only available in foil/etched. Auto-substituting non-foil paper version ({nf_set} #{nf_cn}).",
                    can_autofix=True,
                    suggested_fix=self._render_suggested_line(card, front_ref, name, nf_set, nf_cn),
                    scryfall_uri=nonfoil_meta.get("scryfall_uri")
                ))
            else:
                diagnostics.append(CardDiagnostic(
                    card_name=label,
                    line_number=card.line_number,
                    set_code=card.set_code,
                    collector_number=card.collector_number,
                    severity="YELLOW",
                    category="FOIL_ONLY",
                    message=f"'{label}' ({meta.get('set','').upper()}) exists only in foil across all printings. Will print on standard non-foil paper.",
                    can_autofix=True,
                    suggested_fix=None,
                    scryfall_uri=uri
                ))

        # 3. Oversized card
        if meta.get("oversized") is True:
            diagnostics.append(CardDiagnostic(
                card_name=label,
                line_number=card.line_number,
                set_code=card.set_code,
                collector_number=card.collector_number,
                severity="YELLOW",
                category="OVERSIZED",
                message=f"'{label}' is an oversized card (Planechase/Archenemy/Display). It will be scaled to 63x88mm.",
                can_autofix=True,
                suggested_fix="Automatic aspect-ratio scaling to standard card dimensions",
                scryfall_uri=uri
            ))

        # 4. Low-resolution image
        if meta.get("highres_image") is False:
            diagnostics.append(CardDiagnostic(
                card_name=label,
                line_number=card.line_number,
                set_code=card.set_code,
                collector_number=card.collector_number,
                severity="INFO",
                category="LOW_RES",
                message=f"'{label}' lacks a high-res scan on Scryfall (preview/low-res). Neural upscaling will enhance it.",
                can_autofix=False,
                suggested_fix=None,
                scryfall_uri=uri
            ))

        # 5. Double-faced / transform. A composite ("&&&") line only ever uses
        # one face of whichever side turns out to be a real Scryfall MDFC --
        # the other face is simply discarded, unlike a normal double-faced
        # card where both faces get printed.
        if "card_faces" in meta and len(meta["card_faces"]) >= 2:
            if front_ref is not None:
                message = (
                    f"'{name}' (used as the custom back for line {card.line_number}) is itself a "
                    "double-faced card on Scryfall; only its front face will be printed on the back "
                    "of that card -- its own back face is discarded."
                )
            elif card.is_custom_double_sided:
                message = (
                    f"'{name}' is itself a double-faced card on Scryfall, but this line pairs it with "
                    f"a custom back ('{card.back_name}'); only '{name}''s front face will be used -- "
                    "its own back face is discarded."
                )
            else:
                message = f"'{name}' is a double-faced card (Transform/MDFC). Both front and back faces will be printed."
            diagnostics.append(CardDiagnostic(
                card_name=label,
                line_number=card.line_number,
                set_code=card.set_code,
                collector_number=card.collector_number,
                severity="INFO",
                category="DOUBLE_FACED",
                message=message,
                can_autofix=True,
                suggested_fix="Both faces automatically fetched and paired",
                scryfall_uri=uri
            ))
