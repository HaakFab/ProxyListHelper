import os
import re
from dataclasses import dataclass, field
from typing import List, Tuple, Dict, Optional

from .moxfield_line import is_section_header, parse_line

@dataclass
class CardEntry:
    qty: int
    name: str
    set: Optional[str] = None
    cn: Optional[str] = None
    lang: Optional[str] = None
    # A "custom" double-sided proxy ("1 Front &&& Back"): the back is an
    # unrelated card's own printing, not a Scryfall card_faces pairing.
    back_name: Optional[str] = None
    back_set: Optional[str] = None
    back_cn: Optional[str] = None

    @property
    def key(self) -> str:
        """Generates unique identifier string for caching/fetching."""
        lang_suffix = f"_{self.lang.lower()}" if self.lang and self.lang.lower() != "en" else ""
        if self.set and self.cn:
            return f"{self.set}_{self.cn}{lang_suffix}".lower()
        return f"{re.sub(r'[^a-zA-Z0-9_\-]', '_', self.name.lower())}{lang_suffix}"

    @property
    def is_custom_double_sided(self) -> bool:
        return bool(self.back_name)

    @property
    def back_entry(self) -> Optional["CardEntry"]:
        """The back side as its own independent single-faced lookup.

        Scryfall is queried for this exactly like any other card in the
        order; only its front face image is used, glued onto the back of
        this physical card.
        """
        if not self.back_name:
            return None
        return CardEntry(qty=1, name=self.back_name, set=self.back_set, cn=self.back_cn, lang=self.lang)

@dataclass
class Deck:
    doc_id: str             # e.g., "Fabian2_100"
    file_path: str
    label: str              # e.g., "Fabian 2"
    person: str             # e.g., "Fabian"
    cards: List[CardEntry] = field(default_factory=list)
    preferred_language: str = "en"
    custom_cardback: Optional[str] = None
    
    @property
    def total_cards(self) -> int:
        return sum(c.qty for c in self.cards)

def parse_filename(filename: str) -> Tuple[str, str, str]:
    """
    Parses filenames according to rules:
    - The person name is treated as everything before the first non-letter character.
    - Divider cards use the full list name (label).
    - Every filename is allowed.
    Examples:
      - Fabian2_100.txt              -> label: 'Fabian2_100',              person: 'Fabian', doc_id: 'Fabian2_100'
      - Mischa (7-Thranduil)(100).txt -> label: 'Mischa (7-Thranduil)(100)', person: 'Mischa', doc_id: 'Mischa (7-Thranduil)(100)'
      - Chefski_100.txt.txt          -> label: 'Chefski_100',              person: 'Chefski', doc_id: 'Chefski_100'
      - decks/Chris1_GlarbCEDH/decklist.txt -> label: 'Chris1_GlarbCEDH', person: 'Chris', doc_id: 'Chris1_GlarbCEDH'
    Returns: (label, person, doc_id)
    """
    base = os.path.splitext(os.path.basename(filename))[0]
    while base.lower().endswith(".txt"):
        base = os.path.splitext(base)[0]

    # If the file is a generic "decklist.txt", derive identity from folder name
    if base.lower() in ("decklist", "deck", "cards"):
        parent_dir = os.path.basename(os.path.dirname(filename))
        if parent_dir:
            base = parent_dir

    # Person name is everything before the first non-letter character
    match = re.match(r"^([^\W\d_]+)", base)
    person = match.group(1) if match else base.strip()

    # Label for divider card is the full list name
    label = base
    doc_id = base
    return label, person, doc_id

def parse_moxfield_file(file_path: str) -> Deck:
    """Parses standard Moxfield export format into a Deck object using actual card counts."""
    label, person, doc_id = parse_filename(file_path)
    cards = []
    preferred_language = "en"

    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            clean = line.strip()
            if not clean:
                continue
            if clean.lower().startswith("// language:") or clean.lower().startswith("# language:"):
                preferred_language = clean.split(":", 1)[1].strip().lower()
                continue
            if is_section_header(clean):
                continue

            parsed = parse_line(clean)
            if parsed:
                card_lang = getattr(parsed, "lang", None) or preferred_language
                cards.append(CardEntry(
                    qty=parsed.qty,
                    name=parsed.name,
                    set=parsed.set_code,
                    cn=parsed.collector_number,
                    lang=card_lang,
                    back_name=parsed.back_name,
                    back_set=parsed.back_set_code,
                    back_cn=parsed.back_collector_number,
                ))

    return Deck(
        doc_id=doc_id,
        file_path=file_path,
        label=label,
        person=person,
        cards=cards,
        preferred_language=preferred_language
    )

