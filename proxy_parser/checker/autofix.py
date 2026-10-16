import os
import re
import shutil
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict
from ..moxfield_line import parse_line, render_line
from .file_validator import FileMetadata
from .content_validator import DeckContentReport, ParsedCardLine
from .scryfall_validator import CardDiagnostic

@dataclass
class FileAuditResult:
    metadata: FileMetadata
    content_report: Optional[DeckContentReport]
    card_diagnostics: List[CardDiagnostic] = field(default_factory=list)
    
    # Traffic Light System
    traffic_light: str = "GREEN"    # "GREEN", "YELLOW", "RED"
    status_summary: str = ""
    can_auto_fix: bool = False
    auto_fix_actions: List[str] = field(default_factory=list)
    manual_fix_actions: List[str] = field(default_factory=list)
    applied_changes: List[str] = field(default_factory=list)

def evaluate_traffic_light(
    meta: FileMetadata,
    content: Optional[DeckContentReport],
    diagnostics: List[CardDiagnostic]
) -> FileAuditResult:
    """
    Evaluates file audit and classifies into Traffic Light System:
    🟢 GREEN: No errors, ready for production.
    🟡 YELLOW: Errors detected, but CAN BE AUTO-FIXED without human intervention.
    🔴 RED: Fatal errors that REQUIRE USER/HUMAN FIXING.
    """
    red_reasons = []
    yellow_reasons = []
    auto_fixes = []
    manual_fixes = []

    # 1. File type evaluation
    if meta.file_type == "GDOC_SHORTCUT":
        red_reasons.append("Google Docs shortcut (.gdoc) uploaded instead of plaintext file")
        manual_fixes.append("Open the Google Doc -> File -> Download -> Plain Text (.txt), then upload the .txt file")
    elif meta.file_type == "EMPTY":
        red_reasons.append("File has 0 bytes (empty file)")
        manual_fixes.append("Re-upload a non-empty decklist file")
    elif meta.file_type == "PDF_DOCUMENT":
        # PDF documents can often be auto-extracted if they contain text!
        if content and len(content.cards) > 0:
            yellow_reasons.append("PDF document uploaded instead of .txt (text extractable)")
            auto_fixes.append(f"Auto-extract cards from PDF into '{meta.suggested_filename}'")
        else:
            red_reasons.append("PDF document contains no parseable text")
            manual_fixes.append("Export decklist from Moxfield/Archidekt as Plain Text (.txt)")
    elif meta.file_type not in ("PLAIN_TEXT", "PDF_DOCUMENT"):
        red_reasons.append(f"Unsupported file format '{meta.file_type}'")
        manual_fixes.append("Upload decklist in standard .txt format")

    # 2. Filename evaluation
    if not meta.is_valid_filename:
        if meta.filename_issues:
            yellow_reasons.extend(meta.filename_issues)
            auto_fixes.append(f"Auto-rename file from '{meta.file_name}' to '{meta.suggested_filename}'")

    # 3. Content evaluation
    if content:
        if content.unparseable_lines:
            red_reasons.append(f"{len(content.unparseable_lines)} line(s) could not be parsed as MTG cards")
            for lnum, ltext in content.unparseable_lines[:3]:
                manual_fixes.append(f"Line {lnum}: Fix malformed syntax '{ltext}'")

        # Card count uses actual card count, not treated as an issue


        if content.section_headers:
            yellow_reasons.append(f"Contains {len(content.section_headers)} section header(s) / non-card line(s)")
            auto_fixes.append(f"Auto-strip section headers / comments ({len(content.section_headers)} detected)")
            for lnum, htext in content.section_headers:
                auto_fixes.append(f"Line {lnum}: Strip section header '{htext}'")

        if content.duplicate_cards:
            yellow_reasons.append(f"{len(content.duplicate_cards)} duplicate card entries found")
            for dname, lnums in content.duplicate_cards:
                auto_fixes.append(f"Auto-consolidate duplicate '{dname}' (lines {', '.join(map(str, lnums))})")

        if content.missing_set_codes_count > 0:
            yellow_reasons.append(f"{content.missing_set_codes_count} card(s) have no set code specified")
            auto_fixes.append(f"Auto-resolve {content.missing_set_codes_count} card(s) to latest/popular printings on Scryfall")

    # 4. Scryfall diagnostics evaluation
    for d in diagnostics:
        if d.severity == "RED":
            red_reasons.append(f"{d.card_name}: {d.message}")
            if d.category == "NOT_FOUND":
                manual_fixes.append(f"Line {d.line_number}: Card '{d.card_name}' not found on Scryfall. Correct typo or card name.")
            elif d.category == "DIGITAL_ONLY":
                manual_fixes.append(f"Line {d.line_number}: Card '{d.card_name}' is digital-only (MTG Arena). Replace with a paper card.")
        elif d.severity == "YELLOW":
            yellow_reasons.append(f"{d.card_name}: {d.message}")
            if d.can_autofix and d.suggested_fix:
                if re.match(r"^\d+\s+", d.suggested_fix):
                    auto_fixes.append(f"Line {d.line_number}: Substitute '{d.card_name}' ➔ '{d.suggested_fix}'")
                else:
                    auto_fixes.append(f"Line {d.line_number} ({d.card_name}): {d.suggested_fix}")

    # Determine Traffic Light
    if red_reasons:
        traffic_light = "RED"
        can_auto_fix = False
        status_summary = f"NEEDS USER FIXING ({len(red_reasons)} blocking issue(s))"
    elif yellow_reasons:
        traffic_light = "YELLOW"
        can_auto_fix = True
        status_summary = f"CAN BE AUTO-FIXED ({len(yellow_reasons)} warning(s))"
    else:
        traffic_light = "GREEN"
        can_auto_fix = True
        status_summary = "READY (No issues found)"

    return FileAuditResult(
        metadata=meta,
        content_report=content,
        card_diagnostics=diagnostics,
        traffic_light=traffic_light,
        status_summary=status_summary,
        can_auto_fix=can_auto_fix,
        auto_fix_actions=auto_fixes,
        manual_fix_actions=manual_fixes
    )

def render_fixed_lines(audit: FileAuditResult) -> Tuple[List[str], List[str]]:
    """Renders the auto-fixed decklist as lines, without writing anything.

    Applies, in order:
    - Scryfall substitutions (digital-only / foil-only / wrong printing -> a
      printable paper printing), from the diagnostics' suggested fixes.
    - Moxfield line normalisation (e.g. '373 F' -> '(SET) 373').
    - Section headers and comments dropped (they are simply not in `content.cards`).
    - Consolidation of repeated entries into one line with a summed quantity.

    Returns (lines, change_descriptions). The caller decides where they go: the
    CLI writes a file, the portal stores the text on the submission.
    """
    content = audit.content_report
    if not content or len(content.cards) == 0:
        return [], []

    meta = audit.metadata

    # Diagnostic suggested fixes, keyed by the line they apply to.
    card_fix_map: Dict[int, str] = {}
    for d in audit.card_diagnostics:
        if d.can_autofix and d.suggested_fix and re.match(r"^\d+\s+", d.suggested_fix):
            card_fix_map[d.line_number] = d.suggested_fix

    changes: List[str] = []

    if content.section_headers:
        for lnum, htext in content.section_headers:
            changes.append(f"Line {lnum}: Stripped section header '{htext}'")

    if meta.file_type == "PDF_DOCUMENT":
        changes.append("Extracted card text from PDF document")

    # Resolve every card to its final line first, so consolidation sees the
    # post-substitution printing rather than what the submitter typed.
    resolved: List[Tuple[ParsedCardLine, str]] = []
    for card in content.cards:
        line_str = (
            card_fix_map.get(card.line_number)
            or card.autofixed_line
            or render_line(
                card.qty, card.name, card.set_code, card.collector_number,
                back_name=card.back_name,
                back_set_code=card.back_set_code,
                back_collector_number=card.back_collector_number,
            )
        ).strip()
        if card.raw_line.strip() != line_str:
            changes.append(f"Line {card.line_number}: '{card.raw_line.strip()}' ➔ '{line_str}'")
        resolved.append((card, line_str))

    lines, merge_changes = _consolidate_duplicates(resolved)
    changes.extend(merge_changes)
    return lines, changes


def _consolidate_duplicates(
    resolved: List[Tuple[ParsedCardLine, str]]
) -> Tuple[List[str], List[str]]:
    """Merges repeated entries into a single line with a summed quantity.

    Cards are merged only when name, set code and collector number all match, so
    two deliberate printings of the same card stay separate lines. Total quantity
    is preserved, which keeps this price-neutral. A custom double-sided pairing's
    back side is part of the match key too, so two proxies sharing a front but
    not a back never collapse into one.
    """
    DedupKey = Tuple[str, Optional[str], Optional[str], Optional[str], Optional[str], Optional[str]]
    order: List[DedupKey] = []
    groups: Dict[DedupKey, Dict] = {}

    for card, line_str in resolved:
        parsed = parse_line(line_str)
        if parsed is None:
            # Should not happen, but never silently drop a card the user paid for.
            key = ("__raw__", line_str, None)
            if key not in groups:
                order.append(key)
                groups[key] = {"qty": 0, "line": line_str, "raw": True, "lines": []}
            groups[key]["lines"].append(card.line_number)
            continue

        key = (
            parsed.name.lower(),
            parsed.set_code,
            parsed.collector_number,
            parsed.back_name.lower() if parsed.back_name else None,
            parsed.back_set_code,
            parsed.back_collector_number,
        )
        if key not in groups:
            order.append(key)
            groups[key] = {"qty": 0, "parsed": parsed, "raw": False, "lines": []}
        groups[key]["qty"] += parsed.qty
        groups[key]["lines"].append(card.line_number)

    lines: List[str] = []
    changes: List[str] = []
    for key in order:
        g = groups[key]
        if g["raw"]:
            lines.append(g["line"])
            continue

        parsed = g["parsed"]
        lines.append(render_line(
            g["qty"], parsed.name, parsed.set_code, parsed.collector_number,
            back_name=parsed.back_name,
            back_set_code=parsed.back_set_code,
            back_collector_number=parsed.back_collector_number,
        ))
        if len(g["lines"]) > 1:
            source_lines = ", ".join(str(n) for n in g["lines"])
            changes.append(
                f"Consolidated duplicate '{parsed.name}' from lines {source_lines} "
                f"into a single entry of {g['qty']}"
            )

    return lines, changes


def apply_auto_fix_for_file(audit: FileAuditResult, output_dir: str) -> Optional[str]:
    """
    Applies automatic fixes and writes a clean .txt decklist.
    Returns path of generated/fixed file, or None if file cannot be auto-fixed.
    """
    if not audit.can_auto_fix:
        return None

    meta = audit.metadata
    content = audit.content_report
    if not content or len(content.cards) == 0:
        return None

    # Determine clean filename
    target_filename = meta.suggested_filename or meta.file_name
    if not target_filename.endswith(".txt"):
        target_filename = os.path.splitext(target_filename)[0] + ".txt"

    fixed_path = os.path.join(output_dir, target_filename)

    lines, changes = render_fixed_lines(audit)

    if meta.file_name != target_filename:
        changes.insert(0, f"Renamed file from '{meta.file_name}' ➔ '{target_filename}'")

    with open(fixed_path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")

    audit.applied_changes = changes
    return fixed_path

def export_ready_deck(audit: FileAuditResult, ready_dir: str) -> Optional[str]:
    """
    Exports a valid decklist (either GREEN or auto-fixed YELLOW) into the ready directory:
    - If GREEN: copies the clean, validated decklist.
    - If YELLOW and can_auto_fix: applies auto-fixes (strips headers, substitutes non-foil/fallbacks, normalizes syntax) and writes clean decklist.
    - If RED: returns None (cannot be exported to ready).
    Returns path of generated/copied file in ready_dir, or None if not ready.
    """
    if audit.traffic_light == "RED":
        return None

    if audit.traffic_light == "YELLOW" and not audit.can_auto_fix:
        return None

    meta = audit.metadata
    content = audit.content_report
    if not content or len(content.cards) == 0:
        return None

    os.makedirs(ready_dir, exist_ok=True)

    target_filename = meta.suggested_filename or meta.file_name
    if not target_filename.endswith(".txt"):
        target_filename = os.path.splitext(target_filename)[0] + ".txt"

    ready_path = os.path.join(ready_dir, target_filename)

    if audit.traffic_light == "GREEN":
        shutil.copy2(meta.file_path, ready_path)
        return ready_path
    elif audit.traffic_light == "YELLOW" and audit.can_auto_fix:
        return apply_auto_fix_for_file(audit, ready_dir)

    return None

