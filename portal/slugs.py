"""Filename-safe identifiers that the print pipeline's parser must read back
correctly: person_slug, title slugs, and order reference codes.

`person_slug` is the sharp edge described in portal_implementation.md §9. The
print pipeline infers the person and the divider-card label FROM THE FILENAME,
via `proxy_parser.deck_parser.parse_filename`, whose person regex is:

    re.match(r"^([^\\W\\d_]+)", base)

-- i.e. "everything from the start that is a letter", stopping at the first
digit, underscore, or other non-word character. The worker writes filenames
as `{person_slug}{ordinal}_{title_slug}.txt`, so person_slug MUST be pure
letters: if it contained a digit, the regex would silently truncate at that
digit and misattribute the deck to the wrong (partial) name. This is exactly
the bug that made "Jan David Wirtz1_158.txt" parse as person "Jan" in the old
filename-based workflow -- here we retire it by construction, by never
deriving identity from a filename in the first place.

Collisions (two people both named "Chris") are resolved with a LETTER suffix
(ChrisB, ChrisC, ...), never a digit suffix, for the same reason.
"""
import re
import string
import unicodedata

PERSON_SLUG_PATTERN = re.compile(r"^[A-Za-z]+$")

# Applied before NFKD folding, because NFKD alone turns "ü" into "u" (dropping
# the combining diaeresis), giving "Münchberg" -> "Munchberg". German
# transliteration convention is "ue", which is what participants will expect
# to see on their divider card.
_UMLAUT_MAP = {
    "ä": "ae", "ö": "oe", "ü": "ue",
    "Ä": "Ae", "Ö": "Oe", "Ü": "Ue",
    "ß": "ss",
}


def _fold_umlauts(text: str) -> str:
    return "".join(_UMLAUT_MAP.get(ch, ch) for ch in text)


def _ascii_letters_only(text: str) -> str:
    folded = _fold_umlauts(text)
    nfkd = unicodedata.normalize("NFKD", folded)
    ascii_text = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"[^A-Za-z]", "", ascii_text)


def base_person_slug(display_name: str) -> str:
    """Letters-only slug from a display name, before collision resolution.

    >>> base_person_slug("Simon Münchberg")
    'SimonMuenchberg'
    >>> base_person_slug("Jan David Wirtz")
    'JanDavidWirtz'
    """
    letters = _ascii_letters_only(display_name)
    return letters or "Person"  # display names that are entirely emoji/symbols


def make_unique_person_slug(display_name: str, taken: set[str]) -> str:
    """Resolves collisions by APPENDING LETTERS ONLY -- see module docstring
    for why a digit suffix would be a silent correctness bug, not just an
    aesthetic one.
    """
    base = base_person_slug(display_name)
    if base not in taken:
        return base

    for letter in string.ascii_uppercase:
        candidate = base + letter
        if candidate not in taken:
            return candidate

    # 26 people sharing one folded name is not a realistic scenario at this
    # project's scale, but never silently fail -- widen to two-letter suffixes.
    for a in string.ascii_uppercase:
        for b in string.ascii_uppercase:
            candidate = base + a + b
            if candidate not in taken:
                return candidate

    raise RuntimeError(f"Could not generate a unique person_slug for {display_name!r}")


def make_title_slug(title: str) -> str:
    """Filename-safe slug for a submission's title, e.g. 'Glarb, cEDH #2' ->
    'GlarbCEDH2'. Only used in the visible part of the divider label; unlike
    person_slug it has no parsing contract to satisfy beyond staying a single
    word, so digits are fine here."""
    folded = _fold_umlauts(title)
    nfkd = unicodedata.normalize("NFKD", folded)
    ascii_text = "".join(c for c in nfkd if not unicodedata.combining(c))
    slug = re.sub(r"[^A-Za-z0-9]", "", ascii_text)
    return slug or "Liste"


def worker_filename(person_slug: str, ordinal: int, title: str) -> str:
    """The exact filename the Phase-2 worker writes to `ready/`. Must round-trip
    through proxy_parser.deck_parser.parse_filename back to (person_slug, ...).
    """
    if not PERSON_SLUG_PATTERN.match(person_slug):
        raise ValueError(f"person_slug must be letters-only, got {person_slug!r}")
    return f"{person_slug}{ordinal}_{make_title_slug(title)}.txt"


# Crockford base32 minus I, O, U, 1, 0 -- see portal/security.py for why.
_REF_CHARS = "ABCDEFGHJKLMNPQRSTVWXYZ23456789"


def make_reference_code(round_code: str, person_slug: str, random_part: str) -> str:
    """'MTG-2611-CHRIS-K7Q2'. `random_part` should come from
    security.generate_readable_code(4) -- kept as a parameter here so this
    function stays pure and testable without touching the RNG."""
    return f"MTG-{round_code}-{person_slug.upper()}-{random_part}"
