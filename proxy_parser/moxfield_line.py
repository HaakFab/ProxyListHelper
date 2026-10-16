"""Single source of truth for parsing one line of a Moxfield/Archidekt export.

This module exists because the line grammar was previously duplicated verbatim in
``proxy_parser/deck_parser.py`` (the print pipeline) and
``proxy_parser/checker/content_validator.py`` (the validator). If those two copies
ever drift, the checker counts one set of cards and the pipeline prints another --
which, once a portal quotes a price from the checker, means billing for cards that
never get printed.

Both call sites now delegate here. ``tests/test_moxfield_line.py`` asserts the two
paths agree across every archived decklist.
"""

import re
from dataclasses import dataclass
from typing import Optional, Tuple

SECTION_HEADER_PATTERNS = [
    r"^(?:commander|sideboard|maybeboard|mainboard|deck|companions?|tokens?|planes|phenomena):?.*$",
    r"^//.*$",
    r"^#.*$",
    r"^==+.*$",
    r"^\d+\s+(?:cards?|lands?|creatures?|spells?)\s*:?$",
    r"^(?:\d+x?\s+)?\d+/\d+\s*-\s*.*$",
]

# 1 Card Name (SET) 123 *F*   |   1 Card Name (Retro Frame)   |   Card Name
LINE_PATTERN = r"^(?:(\d+)x?\s+)?([^(]+?)(?:\s+\(([a-zA-Z0-9\s]+)\)\s*([a-zA-Z0-9\-★]+)?)?(?:\s+\*.*)?$"

# A parenthesised token is only a set code if it is short and unspaced.
# "(Retro Frame)" and "(Borderless)" are annotations, not sets.
MAX_SET_CODE_LEN = 5

# A custom double-sided proxy: "1 Front Card Info &&& Back Card Info". Unlike a
# real Scryfall double-faced card (one object, two faces), the two sides here
# are unrelated printings the submitter wants glued front-to-back.
CUSTOM_DOUBLE_SIDED_SEPARATOR = "&&&"


@dataclass
class ParsedLine:
    """The card identity carried by one decklist line."""

    qty: int
    name: str
    set_code: Optional[str] = None          # lowercased, or None
    collector_number: Optional[str] = None  # as written, or None
    back_name: Optional[str] = None
    back_set_code: Optional[str] = None
    back_collector_number: Optional[str] = None

    @property
    def has_set_code(self) -> bool:
        return bool(self.set_code)

    @property
    def has_collector_number(self) -> bool:
        return bool(self.collector_number)

    @property
    def is_custom_double_sided(self) -> bool:
        return bool(self.back_name)


def is_section_header(line: str) -> bool:
    """True for headers, comments and rubric lines that carry no card."""
    clean = line.strip().lower()
    return any(re.match(p, clean) for p in SECTION_HEADER_PATTERNS)


def normalize_line(raw: str) -> str:
    """Strip list decoration and normalise trailing finish flags.

    Handles the shapes that actually turn up in submitted files: "- ", "* ",
    "1. " prefixes, and a bare finish letter (" 373 F") where Moxfield would
    write " 373 *F*".
    """
    line = raw.strip()
    line = re.sub(r"^[-*•]\s+", "", line)
    line = re.sub(r"^\d+\.\s+", "", line)
    line = re.sub(r"\s+[*]?([FEfe])[*]?(?:\s+.*)?$", r" *\1*", line)
    return line


def _clean_set_and_cn(set_code: Optional[str], collector_number: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Normalises a raw regex-captured (set, collector_number) pair.

    A parenthesised token is only a set code if it is short and unspaced;
    otherwise it is an annotation such as "(Retro Frame)", and whatever
    followed it was never a collector number either.
    """
    if set_code and (" " in set_code or len(set_code) > MAX_SET_CODE_LEN):
        return None, None
    set_code = set_code.strip().lower() if set_code else None
    collector_number = collector_number.strip() if collector_number else None
    return set_code, collector_number


def _parse_one_side(text: str) -> Optional[Tuple[Optional[str], str, Optional[str], Optional[str]]]:
    """Parses a single "[qty] Name (SET) CN" fragment. Returns raw regex groups."""
    match = re.match(LINE_PATTERN, text)
    if not match:
        return None
    qty_str, name, set_code, collector_number = match.groups()
    set_code, collector_number = _clean_set_and_cn(set_code, collector_number)
    return qty_str, name.strip(), set_code, collector_number


def _parse_custom_double_sided(normalized: str) -> Optional[ParsedLine]:
    """Parses "[qty] Front Info &&& Back Info" -- a custom, non-Scryfall pairing.

    The quantity belongs to the physical card and is only ever written once,
    on the front side. The back side is looked up as its own independent
    printing (see :func:`proxy_parser.deck_parser.CardEntry.back_entry`);
    Scryfall's real double-faced cards never need this, since one Scryfall
    object already carries both faces.
    """
    front_raw, _, back_raw = normalized.partition(CUSTOM_DOUBLE_SIDED_SEPARATOR)
    front_raw = front_raw.strip()
    back_raw = back_raw.strip()
    if not front_raw or not back_raw:
        return None

    front = _parse_one_side(front_raw)
    if front is None:
        return None
    qty_str, name, set_code, collector_number = front

    # The back side never carries its own quantity; the shared grammar just
    # needs a placeholder qty to match the same "[qty] Name (SET) CN" shape.
    back = _parse_one_side(f"1 {back_raw}")
    if back is None:
        return None
    _, back_name, back_set_code, back_collector_number = back

    return ParsedLine(
        qty=int(qty_str) if qty_str else 1,
        name=name,
        set_code=set_code,
        collector_number=collector_number,
        back_name=back_name,
        back_set_code=back_set_code,
        back_collector_number=back_collector_number,
    )


def parse_line(raw: str) -> Optional[ParsedLine]:
    """Parse one decklist line, or return None if it is not a card line.

    Callers are expected to have filtered blanks and section headers already
    (see :func:`is_section_header`); a None here means genuinely malformed input.
    """
    normalized = normalize_line(raw)

    if CUSTOM_DOUBLE_SIDED_SEPARATOR in normalized:
        return _parse_custom_double_sided(normalized)

    front = _parse_one_side(normalized)
    if front is None:
        return None
    qty_str, name, set_code, collector_number = front

    return ParsedLine(
        qty=int(qty_str) if qty_str else 1,
        name=name,
        set_code=set_code,
        collector_number=collector_number,
    )


def render_line(
    qty: int,
    name: str,
    set_code: Optional[str],
    collector_number: Optional[str],
    back_name: Optional[str] = None,
    back_set_code: Optional[str] = None,
    back_collector_number: Optional[str] = None,
) -> str:
    """Render a card back into canonical Moxfield form.

    The inverse of :func:`parse_line`, used when writing auto-fixed lists so the
    text handed to the print pipeline re-parses identically. When ``back_name``
    is given, renders the custom double-sided "&&&" form instead.
    """
    if set_code:
        front = f"{qty} {name} ({set_code.upper()}) {collector_number or ''}".strip()
    else:
        front = f"{qty} {name}"

    if not back_name:
        return front

    if back_set_code:
        back = f"{back_name} ({back_set_code.upper()}) {back_collector_number or ''}".strip()
    else:
        back = back_name

    return f"{front} {CUSTOM_DOUBLE_SIDED_SEPARATOR} {back}"
