"""The ONLY module that touches proxy_parser. See portal_implementation.md §8.

Everything here is deliberately thin: the real logic (parsing, traffic-light
evaluation, auto-fix rendering, Scryfall validation) lives in
proxy_parser.checker and must stay shared with the CLI (deck_checker.py).
Two CLI-only behaviours are suppressed here by feeding evaluate_traffic_light
a clean FileMetadata -- never by editing that function itself:

  1. The "auto-rename file" YELLOW reason: filenames are server-generated here,
     so meta.is_valid_filename is forced True.
  2. The stated-count-in-filename mismatch: there is no count-in-filename
     convention on the web, so meta.stated_count is forced None.
"""
import dataclasses
import json
from dataclasses import dataclass

from proxy_parser.checker import (
    CardDiagnostic,
    FileAuditResult,
    ScryfallBatchValidator,
    ScryfallUnavailable,
    classify_upload,
    evaluate_traffic_light,
    parse_deck_lines,
    render_fixed_lines,
)
from proxy_parser.checker.scryfall_cache import ScryfallCache

from . import slugs

__all__ = ["CheckOutcome", "run_check", "ScryfallUnavailable"]

# Web-side caps, enforced before a single byte reaches the parser. Generous
# for the real workload (the largest archived list is ~300 lines) while still
# closing off a pathological-input DoS on the Moxfield regex.
MAX_TEXT_BYTES = 256_000
MAX_LINES = 2000
MAX_LINE_CHARS = 500


class SubmissionTooLarge(ValueError):
    """Raised by normalize_submitted_text; the route turns this into a 413."""


@dataclass
class CheckOutcome:
    audit: FileAuditResult
    fixed_text: str
    changes: list[str]

    @property
    def traffic_light(self) -> str:
        return self.audit.traffic_light

    @property
    def total_cards(self) -> int:
        return self.audit.content_report.total_cards_count if self.audit.content_report else 0

    @property
    def unique_lines(self) -> int:
        return self.audit.content_report.unique_card_lines if self.audit.content_report else 0

    def to_audit_json(self) -> str:
        """Serialises the whole audit (including nested dataclasses) for
        storage in CheckRun.audit_json and for rendering the result page."""
        return json.dumps(_audit_to_dict(self.audit))


def _audit_to_dict(audit: FileAuditResult) -> dict:
    return {
        "traffic_light": audit.traffic_light,
        "status_summary": audit.status_summary,
        "can_auto_fix": audit.can_auto_fix,
        "auto_fix_actions": audit.auto_fix_actions,
        "manual_fix_actions": audit.manual_fix_actions,
        "applied_changes": audit.applied_changes,
        "content_report": (
            _content_report_to_dict(audit.content_report) if audit.content_report else None
        ),
        "card_diagnostics": [dataclasses.asdict(d) for d in audit.card_diagnostics],
    }


def _content_report_to_dict(content) -> dict:
    d = dataclasses.asdict(content)
    # `cards` carries every ParsedCardLine (with autofixed_line etc.) which the
    # result page renders per-line; keep it, it's already JSON-safe via asdict.
    return d


def normalize_submitted_text(raw: str) -> str:
    """UTF-8 already assumed (FastAPI decodes the form/body); this handles the
    remaining real-world mess: BOM (input_final/Baris_300.txt has one), CRLF,
    NULs, and caps line/byte counts. Raises SubmissionTooLarge rather than
    silently truncating -- a silently truncated list would under-bill and
    under-print without anyone noticing.
    """
    if len(raw.encode("utf-8", errors="replace")) > MAX_TEXT_BYTES:
        raise SubmissionTooLarge(f"Submission exceeds {MAX_TEXT_BYTES} bytes")

    text = raw.lstrip("﻿")  # strip a leading BOM
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\x00", "")

    lines = text.split("\n")
    if len(lines) > MAX_LINES:
        raise SubmissionTooLarge(f"Submission has more than {MAX_LINES} lines")
    for line in lines:
        if len(line) > MAX_LINE_CHARS:
            raise SubmissionTooLarge(f"A line exceeds {MAX_LINE_CHARS} characters")

    return text


def run_check(
    raw_text: str,
    display_name: str,
    person_slug: str,
    ordinal: int,
    title: str,
    cache: ScryfallCache,
    skip_scryfall: bool = False,
) -> CheckOutcome:
    """Runs the full check pipeline against submitted text.

    Raises ScryfallUnavailable if Scryfall cannot be reached -- this MUST
    propagate to the caller (portal/jobs.py), which marks the CheckRun FAILED
    with scryfall_ok=False. Never catch this here and continue: a batch with
    no diagnostics looks identical to a clean list (see the fix in
    proxy_parser/checker/scryfall_validator.py and tests/test_scryfall_resilience.py).
    """
    filename = slugs.worker_filename(person_slug, ordinal, title)

    # 1. Synthesize FileMetadata WITHOUT touching the filesystem.
    meta = classify_upload(
        file_name=filename,
        file_size=len(raw_text.encode("utf-8")),
        file_type="PLAIN_TEXT",
    )
    meta.person = display_name           # authoritative: the account, not the filename
    meta.is_valid_filename = True        # suppress the "auto-rename" CLI reason
    meta.filename_issues = []
    meta.stated_count = None             # suppress the filename-count-mismatch CLI reason

    # 2. Pure parse -- the one and only Moxfield line grammar (proxy_parser.moxfield_line).
    content = parse_deck_lines(raw_text.splitlines())

    # 3. Scryfall validation. Propagates ScryfallUnavailable; see docstring.
    if skip_scryfall:
        diagnostics: list[CardDiagnostic] = []
    else:
        validator = ScryfallBatchValidator(cache=cache)
        diagnostics = validator.validate_cards(content.cards)

    # 4. Verdict -- UNCHANGED shared logic, never edited for the web.
    audit = evaluate_traffic_light(meta, content, diagnostics)

    # 5. Pure renderer: auto-fix substitutions + duplicate consolidation.
    fixed_lines, changes = render_fixed_lines(audit)
    fixed_text = "\n".join(fixed_lines) + ("\n" if fixed_lines else "")

    return CheckOutcome(audit=audit, fixed_text=fixed_text, changes=changes)
