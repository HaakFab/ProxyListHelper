"""person_slug is the sharp edge in portal_implementation.md §9: the print
pipeline's parse_filename infers a person from the letters-only prefix of a
filename, stopping at the first digit. These tests prove the three concrete
cases named in the spec round-trip correctly, and that collisions never
produce a digit suffix (which would silently break that regex).
"""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portal.slugs import (
    PERSON_SLUG_PATTERN,
    base_person_slug,
    make_reference_code,
    make_title_slug,
    make_unique_person_slug,
    worker_filename,
)
from proxy_parser.deck_parser import parse_filename


class TestBasePersonSlug:
    def test_simon_muenchberg(self):
        assert base_person_slug("Simon Münchberg") == "SimonMuenchberg"

    def test_jan_david_wirtz(self):
        assert base_person_slug("Jan David Wirtz") == "JanDavidWirtz"

    def test_ascii_name_unchanged(self):
        assert base_person_slug("Chris") == "Chris"

    def test_eszett(self):
        assert base_person_slug("Größe") == "Groesse"

    def test_all_symbols_falls_back(self):
        assert base_person_slug("!!!") == "Person"

    @pytest.mark.parametrize("name", ["Simon Münchberg", "Jan David Wirtz", "Chris", "Größe"])
    def test_result_matches_person_slug_pattern(self, name):
        assert PERSON_SLUG_PATTERN.match(base_person_slug(name))


class TestUniquePersonSlug:
    def test_no_collision_returns_base(self):
        assert make_unique_person_slug("Chris", taken=set()) == "Chris"

    def test_collision_resolves_with_a_letter_not_a_digit(self):
        slug = make_unique_person_slug("Chris", taken={"Chris"})
        assert slug == "ChrisA"
        assert PERSON_SLUG_PATTERN.match(slug)

    def test_second_collision_skips_taken_letters(self):
        slug = make_unique_person_slug("Chris", taken={"Chris", "ChrisA"})
        assert slug == "ChrisB"

    def test_never_produces_a_digit_suffix(self):
        """The one property that must never break: a digit here would make
        deck_parser.parse_filename misattribute the deck to a truncated name."""
        taken = {"Chris"}
        for _ in range(30):
            slug = make_unique_person_slug("Chris", taken)
            assert PERSON_SLUG_PATTERN.match(slug), f"{slug!r} contains a non-letter"
            taken.add(slug)


class TestWorkerFilenameRoundTrip:
    """The actual guarantee: what the worker writes, the pipeline must read
    back as exactly the intended person -- not a truncated prefix."""

    @pytest.mark.parametrize(
        "display_name,ordinal,title",
        [
            ("Simon Münchberg", 1, "Sephiroth"),
            ("Jan David Wirtz", 2, "Commander deck"),
            ("Chris", 4, "glarbDC"),
        ],
    )
    def test_round_trips_to_the_intended_person(self, display_name, ordinal, title):
        slug = base_person_slug(display_name)
        filename = worker_filename(slug, ordinal, title)
        _label, person, _doc_id = parse_filename(filename)
        assert person == slug

    def test_forced_collision_still_round_trips(self):
        """Two people who both fold to 'Chris' must each still parse back to
        their own (distinct) slug, not into each other."""
        first = make_unique_person_slug("Chris", taken=set())
        second = make_unique_person_slug("Chris", taken={first})
        assert first != second

        for slug in (first, second):
            filename = worker_filename(slug, 1, "TestDeck")
            _label, person, _doc_id = parse_filename(filename)
            assert person == slug

    def test_rejects_a_slug_containing_a_digit(self):
        with pytest.raises(ValueError):
            worker_filename("Chris2", 1, "Deck")

    def test_filename_matches_expected_shape(self):
        assert worker_filename("Chris", 4, "Glarb DC") == "Chris4_GlarbDC.txt"


class TestMakeTitleSlug:
    def test_strips_punctuation_and_spaces(self):
        assert make_title_slug("Glarb, cEDH #2") == "GlarbcEDH2"

    def test_umlauts_transliterate(self):
        assert make_title_slug("Über Deck") == "UeberDeck"

    def test_empty_title_falls_back(self):
        assert make_title_slug("???") == "Liste"


class TestReferenceCode:
    def test_shape(self):
        code = make_reference_code("2611", "Chris", "K7Q2")
        assert code == "MTG-2611-CHRIS-K7Q2"

    def test_uppercases_person_slug(self):
        code = make_reference_code("2611", "chrisb", "AB12")
        assert "CHRISB" in code

    def test_ambiguous_characters_excluded_from_alphabet(self):
        from portal.security import generate_readable_code
        for _ in range(200):
            code = generate_readable_code(8)
            assert not re.search(r"[IOU10]", code), f"ambiguous char in {code!r}"
