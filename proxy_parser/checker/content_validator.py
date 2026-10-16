import os
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Sequence, Tuple

from ..moxfield_line import (
    SECTION_HEADER_PATTERNS,
    is_section_header,
    parse_line,
    render_line,
)

@dataclass
class ParsedCardLine:
    line_number: int
    raw_line: str
    qty: int
    name: str
    set_code: Optional[str] = None
    collector_number: Optional[str] = None
    back_name: Optional[str] = None
    back_set_code: Optional[str] = None
    back_collector_number: Optional[str] = None
    has_set_code: bool = False
    has_collector_number: bool = False
    is_header_or_comment: bool = False
    syntax_warning: Optional[str] = None
    can_autofix: bool = False
    autofixed_line: Optional[str] = None

    @property
    def is_custom_double_sided(self) -> bool:
        return bool(self.back_name)

@dataclass
class DeckContentReport:
    total_lines: int
    total_cards_count: int
    unique_card_lines: int
    cards: List[ParsedCardLine] = field(default_factory=list)
    unparseable_lines: List[Tuple[int, str]] = field(default_factory=list)
    section_headers: List[Tuple[int, str]] = field(default_factory=list)
    duplicate_cards: List[Tuple[str, List[int]]] = field(default_factory=list)
    missing_set_codes_count: int = 0
    count_mismatch: bool = False
    count_discrepancy_msg: Optional[str] = None
    content_issues: List[str] = field(default_factory=list)
    can_autofix_content: bool = True

def parse_deck_content(file_path: str, stated_count: Optional[int] = None) -> DeckContentReport:
    """Reads a .txt or .pdf decklist from disk and parses it.

    Thin I/O wrapper around :func:`parse_deck_lines`; the portal calls that
    directly with submitted text and never touches the filesystem.
    """
    ext = os.path.splitext(file_path)[1].lower()

    if ext == ".txt":
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except Exception as e:
            return _unreadable(f"Failed to read file: {e}")
    elif ext == ".pdf":
        try:
            from pypdf import PdfReader
            reader = PdfReader(file_path)
            extracted_text = ""
            for page in reader.pages:
                extracted_text += (page.extract_text() or "") + "\n"
            lines = [l + "\n" for l in extracted_text.splitlines()]
        except Exception as e:
            return _unreadable(f"Failed to extract text from PDF: {e}")
    else:
        return _unreadable("Cannot parse content: file is not plain text or extractable PDF")

    return parse_deck_lines(lines, stated_count=stated_count)


def _unreadable(issue: str) -> DeckContentReport:
    return DeckContentReport(
        total_lines=0,
        total_cards_count=0,
        unique_card_lines=0,
        content_issues=[issue],
        can_autofix_content=False
    )


def parse_deck_lines(lines: Sequence[str], stated_count: Optional[int] = None) -> DeckContentReport:
    """Parse decklist lines, count cards, and record formatting problems.

    Pure: no filesystem, no network. Line numbers are 1-based and refer to the
    input sequence, so they stay meaningful when reported back to a submitter.
    """
    parsed_cards: List[ParsedCardLine] = []
    unparseable: List[Tuple[int, str]] = []
    headers: List[Tuple[int, str]] = []
    seen_names: Dict[str, List[int]] = {}
    missing_sets = 0
    total_qty = 0
    issues = []
    can_autofix = True

    for idx, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line:
            continue

        if is_section_header(line):
            headers.append((idx, line))
            continue

        parsed = parse_line(line)
        if parsed is None:
            unparseable.append((idx, line))
            can_autofix = False
            continue

        warning = None
        if not parsed.has_set_code:
            missing_sets += 1
            warning = "Missing set code (will default to latest/popular printing)"

        total_qty += parsed.qty
        # A custom double-sided pairing is keyed on both faces, so it is never
        # mistaken for a duplicate of a plain card that happens to share the
        # front name (or vice versa).
        dedup_key = parsed.name.lower()
        if parsed.is_custom_double_sided:
            dedup_key = f"{dedup_key} &&& {parsed.back_name.lower()}"
        seen_names.setdefault(dedup_key, []).append(idx)

        parsed_cards.append(ParsedCardLine(
            line_number=idx,
            raw_line=line,
            qty=parsed.qty,
            name=parsed.name,
            set_code=parsed.set_code,
            collector_number=parsed.collector_number,
            back_name=parsed.back_name,
            back_set_code=parsed.back_set_code,
            back_collector_number=parsed.back_collector_number,
            has_set_code=parsed.has_set_code,
            has_collector_number=parsed.has_collector_number,
            syntax_warning=warning,
            can_autofix=True,
            autofixed_line=render_line(
                parsed.qty, parsed.name, parsed.set_code, parsed.collector_number,
                back_name=parsed.back_name,
                back_set_code=parsed.back_set_code,
                back_collector_number=parsed.back_collector_number,
            )
        ))

    # Check for duplicate entries
    duplicates = [(name, line_nums) for name, line_nums in seen_names.items() if len(line_nums) > 1]
    if duplicates:
        issues.append(f"{len(duplicates)} card(s) are listed multiple times across separate lines")

    if unparseable:
        issues.append(f"{len(unparseable)} unparseable line(s) detected")

    if missing_sets > 0:
        issues.append(f"{missing_sets} card(s) have no set code specified")

    # Card count reconciliation (purely informational; actual card count is used)
    count_mismatch = False
    discrepancy_msg = None
    if stated_count is not None and total_qty != stated_count:
        count_mismatch = True
        diff = total_qty - stated_count
        diff_str = f"+{diff}" if diff > 0 else f"{diff}"
        discrepancy_msg = f"Filename states {stated_count} cards, but file contains {total_qty} actual cards ({diff_str})"

    return DeckContentReport(
        total_lines=len(lines),
        total_cards_count=total_qty,
        unique_card_lines=len(parsed_cards),
        cards=parsed_cards,
        unparseable_lines=unparseable,
        section_headers=headers,
        duplicate_cards=duplicates,
        missing_set_codes_count=missing_sets,
        count_mismatch=count_mismatch,
        count_discrepancy_msg=discrepancy_msg,
        content_issues=issues,
        can_autofix_content=can_autofix and len(unparseable) == 0
    )
