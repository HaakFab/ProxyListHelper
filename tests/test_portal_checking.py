"""portal.checking.run_check() is the only place the web app touches
proxy_parser. These tests are the money-safety guarantee from
portal_implementation.md §18: what the portal bills, the pipeline must print.

All tests here run with skip_scryfall=True (no network) -- Scryfall's own
resilience is covered separately in tests/test_scryfall_resilience.py. What's
tested here is the *bridge itself*: does run_check correctly wire
classify_upload -> parse_deck_lines -> evaluate_traffic_light -> render_fixed_lines
without diverging from calling those functions directly.
"""
import glob
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portal import checking
from portal.slugs import base_person_slug
from proxy_parser.checker import classify_upload, evaluate_traffic_light, parse_deck_lines, render_fixed_lines
from proxy_parser.checker.content_validator import parse_deck_content
from proxy_parser.checker.scryfall_cache import MemoryScryfallCache
from proxy_parser.deck_parser import parse_moxfield_file
from proxy_parser.moxfield_line import parse_line

REPO_ROOT = Path(__file__).resolve().parent.parent


def _archived_files():
    paths = sorted(
        glob.glob(str(REPO_ROOT / "input_final" / "*.txt"))
        + glob.glob(str(REPO_ROOT / "input" / "*.txt"))
    )
    return [(p, os.path.basename(p)) for p in paths]


_FILES = _archived_files()


@pytest.fixture
def cache():
    return MemoryScryfallCache()


@pytest.mark.skipif(not _FILES, reason="no archived decklists available")
@pytest.mark.parametrize("path", [p for p, _ in _FILES], ids=[i for _, i in _FILES])
class TestBridgeMatchesDirectCalls:
    """run_check must be pure plumbing: identical to calling the underlying
    checker functions directly with the same overrides. Any divergence here
    is a bug in the bridge, not in proxy_parser."""

    def test_traffic_light_and_counts_match_direct_calls(self, path, cache):
        raw_text = Path(path).read_text(encoding="utf-8", errors="replace")
        display_name = "Testperson"
        slug = base_person_slug(display_name)

        via_bridge = checking.run_check(
            raw_text=raw_text,
            display_name=display_name,
            person_slug=slug,
            ordinal=1,
            title="Deck",
            cache=cache,
            skip_scryfall=True,
        )

        # The exact same sequence, called directly.
        meta = classify_upload(
            file_name=f"{slug}1_Deck.txt",
            file_size=len(raw_text.encode("utf-8")),
            file_type="PLAIN_TEXT",
        )
        meta.person = display_name
        meta.is_valid_filename = True
        meta.filename_issues = []
        meta.stated_count = None
        content = parse_deck_lines(raw_text.splitlines())
        audit = evaluate_traffic_light(meta, content, [])
        fixed_lines, changes = render_fixed_lines(audit)
        expected_fixed_text = "\n".join(fixed_lines) + ("\n" if fixed_lines else "")

        assert via_bridge.traffic_light == audit.traffic_light
        assert via_bridge.total_cards == content.total_cards_count
        assert via_bridge.unique_lines == content.unique_card_lines
        assert via_bridge.fixed_text == expected_fixed_text
        assert via_bridge.changes == changes


@pytest.mark.skipif(not _FILES, reason="no archived decklists available")
@pytest.mark.parametrize("path", [p for p, _ in _FILES], ids=[i for _, i in _FILES])
class TestBilledEqualsPrinted:
    """The actual money guarantee: the card count the portal would bill for
    must equal what the pipeline's OWN parser (deck_parser.parse_moxfield_file)
    would print, for both the original text and the auto-fixed text."""

    def test_original_text_card_count_matches_pipeline_parser(self, path, cache):
        raw_text = Path(path).read_text(encoding="utf-8", errors="replace")
        outcome = checking.run_check(
            raw_text=raw_text,
            display_name="Testperson",
            person_slug="Testperson",
            ordinal=1,
            title="Deck",
            cache=cache,
            skip_scryfall=True,
        )
        pipeline_deck = parse_moxfield_file(path)
        assert outcome.total_cards == pipeline_deck.total_cards

    def test_fixed_text_reparses_to_the_same_total(self, path, cache):
        """render_fixed_lines only substitutes/consolidates -- it must never
        change the total quantity billed, since that total is already locked
        in before fixes are applied to a live order."""
        raw_text = Path(path).read_text(encoding="utf-8", errors="replace")
        outcome = checking.run_check(
            raw_text=raw_text,
            display_name="Testperson",
            person_slug="Testperson",
            ordinal=1,
            title="Deck",
            cache=cache,
            skip_scryfall=True,
        )
        reparsed_total = sum(
            parse_line(line).qty for line in outcome.fixed_text.splitlines() if parse_line(line)
        )
        assert reparsed_total == outcome.total_cards

    def test_no_content_reason_ever_produces_a_false_red(self, path, cache):
        """These files are known to parse cleanly (Phase 0's golden test
        confirms zero unparseable lines across the archive). With Scryfall
        skipped, the only diagnostics come from content_validator -- so RED
        here would mean the portal is inventing a blocking error that
        deck_checker.py doesn't have."""
        raw_text = Path(path).read_text(encoding="utf-8", errors="replace")
        content = parse_deck_content(path)
        assume_parseable = not content.unparseable_lines

        outcome = checking.run_check(
            raw_text=raw_text,
            display_name="Testperson",
            person_slug="Testperson",
            ordinal=1,
            title="Deck",
            cache=cache,
            skip_scryfall=True,
        )
        if assume_parseable:
            assert outcome.traffic_light != "RED", (
                f"{path} parses cleanly via the CLI but the portal marked it RED"
            )


class TestNormalizeSubmittedText:
    def test_strips_leading_bom(self):
        assert checking.normalize_submitted_text("﻿1 Sol Ring").startswith("1 Sol Ring")

    def test_normalizes_crlf(self):
        assert checking.normalize_submitted_text("a\r\nb\r\n") == "a\nb\n"

    def test_strips_nul_bytes(self):
        assert "\x00" not in checking.normalize_submitted_text("a\x00b")

    def test_rejects_oversized_submission(self):
        with pytest.raises(checking.SubmissionTooLarge):
            checking.normalize_submitted_text("x" * (checking.MAX_TEXT_BYTES + 1))

    def test_rejects_too_many_lines(self):
        with pytest.raises(checking.SubmissionTooLarge):
            checking.normalize_submitted_text("\n".join(["1 Sol Ring"] * (checking.MAX_LINES + 1)))

    def test_rejects_a_pathologically_long_line(self):
        with pytest.raises(checking.SubmissionTooLarge):
            checking.normalize_submitted_text("1 " + "A" * (checking.MAX_LINE_CHARS + 1))

    def test_accepts_a_normal_list(self):
        text = "1 Sol Ring (LEA) 1\n1 Lightning Bolt (LEA) 161\n"
        assert checking.normalize_submitted_text(text) == text


class TestDuplicateConsolidationThroughTheBridge:
    """The Phase 0 fix (autofix.py's real duplicate consolidation), exercised
    through the actual portal entry point rather than the checker directly."""

    def test_duplicates_consolidate_and_total_is_preserved(self, cache):
        text = (
            "1 Sol Ring (LEA) 1\n"
            "1 Lightning Bolt (LEA) 161\n"
            "1 Sol Ring (LEA) 1\n"
        )
        outcome = checking.run_check(
            raw_text=text,
            display_name="Testperson",
            person_slug="Testperson",
            ordinal=1,
            title="Deck",
            cache=cache,
            skip_scryfall=True,
        )
        fixed_lines = outcome.fixed_text.strip().splitlines()
        assert len(fixed_lines) == 2  # Sol Ring merged, Lightning Bolt untouched
        assert outcome.total_cards == 3
        assert any("Consolidated duplicate" in c for c in outcome.changes)
