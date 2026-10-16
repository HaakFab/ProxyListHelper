"""The parser-agreement guarantee.

The portal quotes a price from the checker's card count; the Mac prints whatever
the pipeline's parser produces. If those two ever disagree, someone pays for
cards that never arrive. Both now delegate to proxy_parser.moxfield_line, and
this module is what keeps it that way.
"""

import glob
import os

import pytest

from proxy_parser.checker.content_validator import parse_deck_content
from proxy_parser.deck_parser import parse_moxfield_file
from proxy_parser.moxfield_line import (
    is_section_header,
    normalize_line,
    parse_line,
    render_line,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _archived_decklists():
    """Every real decklist available locally, as (path, id) pairs."""
    paths = sorted(
        glob.glob(os.path.join(REPO_ROOT, "input_final", "*.txt"))
        + glob.glob(os.path.join(REPO_ROOT, "input", "*.txt"))
    )
    return [(p, os.path.basename(p)) for p in paths]


_DECKLISTS = _archived_decklists()


@pytest.mark.skipif(not _DECKLISTS, reason="no archived decklists available")
@pytest.mark.parametrize(
    "path", [p for p, _ in _DECKLISTS], ids=[i for _, i in _DECKLISTS]
)
def test_checker_and_pipeline_parse_identically(path):
    """The golden test: both parsers must see the same cards in the same order."""
    checker = parse_deck_content(path)
    pipeline = parse_moxfield_file(path)

    from_checker = [
        (c.qty, c.name, c.set_code, c.collector_number) for c in checker.cards
    ]
    from_pipeline = [(c.qty, c.name, c.set, c.cn) for c in pipeline.cards]

    assert from_checker == from_pipeline
    assert checker.total_cards_count == pipeline.total_cards


class TestParseLine:
    def test_full_form(self):
        p = parse_line("1 Bloodthirsty Conqueror (FDN) 58")
        assert (p.qty, p.name, p.set_code, p.collector_number) == (
            1,
            "Bloodthirsty Conqueror",
            "fdn",
            "58",
        )

    def test_quantity_defaults_to_one(self):
        assert parse_line("Sol Ring").qty == 1

    def test_bare_name_has_no_printing(self):
        p = parse_line("Sol Ring")
        assert not p.has_set_code and not p.has_collector_number

    def test_double_faced_slash_form(self):
        p = parse_line("1 Barkchannel Pathway / Tidechannel Pathway (KHM) 251")
        assert p.name == "Barkchannel Pathway / Tidechannel Pathway"
        assert p.set_code == "khm"

    @pytest.mark.parametrize(
        "line,expected_cn",
        [
            ("1 Boseiju, Who Endures (PNEO) 266p", "266p"),
            ("1 Cling to Dust (PLST) THB-87", "THB-87"),
        ],
    )
    def test_exotic_collector_numbers(self, line, expected_cn):
        assert parse_line(line).collector_number == expected_cn

    def test_annotation_is_not_a_set_code(self):
        """'(Retro Frame)' is a note; it must not be mistaken for a printing."""
        p = parse_line("1 Lightning Bolt (Retro Frame)")
        assert p.name == "Lightning Bolt"
        assert p.set_code is None
        assert p.collector_number is None

    def test_bullet_and_numbering_prefixes_are_stripped(self):
        for prefix in ("- ", "* ", "• ", "1. "):
            p = parse_line(f"{prefix}2 Sol Ring (LEA) 1")
            assert (p.qty, p.name) == (2, "Sol Ring")

    def test_bare_finish_flag_is_normalised(self):
        """' 373 F' is how some exports write a foil; Moxfield writes ' 373 *F*'."""
        p = parse_line("1 Arcane Signet (ELD) 373 F")
        assert p.set_code == "eld"
        assert p.collector_number == "373"

    def test_quantity_with_x_suffix(self):
        assert parse_line("4x Lightning Bolt (LEA) 161").qty == 4


class TestCustomDoubleSided:
    """The '&&&' syntax for pairing two unrelated printings front-to-back."""

    def test_full_form(self):
        p = parse_line(
            "1 Dion, Bahamut's Dominant &&& Bahamut, Warden of Light (FIN) 16"
        )
        assert p.qty == 1
        assert p.name == "Dion, Bahamut's Dominant"
        assert p.set_code is None and p.collector_number is None
        assert p.back_name == "Bahamut, Warden of Light"
        assert p.back_set_code == "fin"
        assert p.back_collector_number == "16"
        assert p.is_custom_double_sided

    def test_both_sides_have_printings(self):
        p = parse_line("2 Sol Ring (LEA) 1 &&& Lightning Bolt (LEA) 161")
        assert p.qty == 2
        assert (p.name, p.set_code, p.collector_number) == ("Sol Ring", "lea", "1")
        assert (p.back_name, p.back_set_code, p.back_collector_number) == (
            "Lightning Bolt",
            "lea",
            "161",
        )

    def test_back_has_no_printing(self):
        p = parse_line("1 Sol Ring &&& Lightning Bolt")
        assert p.back_name == "Lightning Bolt"
        assert p.back_set_code is None and p.back_collector_number is None

    def test_missing_side_is_unparseable(self):
        assert parse_line("1 Sol Ring &&&") is None
        assert parse_line("&&& Lightning Bolt") is None

    def test_normal_lines_are_not_composite(self):
        assert not parse_line("1 Sol Ring (LEA) 1").is_custom_double_sided


class TestRenderCustomDoubleSided:
    def test_round_trips_through_parse(self):
        original = "1 Dion, Bahamut's Dominant &&& Bahamut, Warden of Light (FIN) 16"
        p = parse_line(original)
        rendered = render_line(
            p.qty, p.name, p.set_code, p.collector_number,
            back_name=p.back_name,
            back_set_code=p.back_set_code,
            back_collector_number=p.back_collector_number,
        )
        assert rendered == original
        assert parse_line(rendered) == p

    def test_omits_separator_when_no_back(self):
        assert render_line(1, "Sol Ring", None, None, back_name=None) == "1 Sol Ring"


class TestSectionHeaders:
    @pytest.mark.parametrize(
        "line",
        [
            "Commander:",
            "SIDEBOARD",
            "// a comment",
            "# another comment",
            "=== Deck ===",
            "100 Cards",
            "10/10 - something",
        ],
    )
    def test_recognised(self, line):
        assert is_section_header(line)

    @pytest.mark.parametrize(
        "line", ["1 Sol Ring (LEA) 1", "Sol Ring", "2 Deathrite Shaman (RTR) 155"]
    )
    def test_card_lines_are_not_headers(self, line):
        assert not is_section_header(line)


class TestRenderLine:
    def test_round_trips_through_parse(self):
        original = "3 Deathrite Shaman (RTR) 155"
        p = parse_line(original)
        assert render_line(p.qty, p.name, p.set_code, p.collector_number) == original

    def test_omits_empty_printing(self):
        assert render_line(1, "Sol Ring", None, None) == "1 Sol Ring"

    def test_set_code_is_uppercased(self):
        assert render_line(1, "Sol Ring", "lea", "1") == "1 Sol Ring (LEA) 1"

    @pytest.mark.parametrize("path", [p for p, _ in _DECKLISTS][:5])
    def test_rendered_lines_reparse_unchanged(self, path):
        """Auto-fixed output must re-parse to the same cards the pipeline will print."""
        for card in parse_deck_content(path).cards:
            rendered = render_line(
                card.qty, card.name, card.set_code, card.collector_number
            )
            again = parse_line(rendered)
            assert (again.qty, again.name, again.set_code, again.collector_number) == (
                card.qty,
                card.name,
                card.set_code,
                card.collector_number,
            )


def test_normalize_line_is_idempotent():
    for line in ("- 1 Sol Ring (LEA) 1", "1 Arcane Signet (ELD) 373 F", "Sol Ring"):
        once = normalize_line(line)
        assert normalize_line(once) == once


def test_unparseable_line_returns_none():
    assert parse_line("((((") is None
