"""A Scryfall outage must never look like a clean decklist.

Before this was fixed, a 429/5xx/timeout made validate_cards skip a batch of 75
cards, producing zero diagnostics -- which evaluate_traffic_light reads as "no
issues found" and classifies GREEN. On the CLI that wastes an evening. Once a
portal quotes a price from that verdict, it takes money for an unprintable order.
"""

import tempfile
from unittest.mock import patch

import pytest

from proxy_parser.checker import (
    ScryfallBatchValidator,
    ScryfallUnavailable,
    classify_upload,
    evaluate_traffic_light,
    parse_deck_lines,
)
import proxy_parser.checker.scryfall_validator as sv

DECK = "1 Sol Ring (LEA) 1\n1 Lightning Bolt (LEA) 161\n"


class FakeResponse:
    def __init__(self, status_code, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._payload


@pytest.fixture
def validator():
    """A validator with an empty cache, so every card must hit the network."""
    with tempfile.TemporaryDirectory() as tmp:
        yield ScryfallBatchValidator(cache_dir=tmp)


@pytest.fixture(autouse=True)
def _offline():
    """Keep the retry schedule fast and the suite off the live Scryfall API.

    The not-found fallback path issues GETs of its own; left unpatched they would
    make these tests both slow and dependent on Scryfall being up.
    """
    with patch.object(sv.time, "sleep"), \
         patch.object(sv.requests, "get", return_value=FakeResponse(404)):
        yield


def _cards():
    return parse_deck_lines(DECK.splitlines()).cards


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_transient_failure_raises_instead_of_returning_empty(validator, status):
    with patch.object(sv.requests, "post", return_value=FakeResponse(status)):
        with pytest.raises(ScryfallUnavailable):
            validator.validate_cards(_cards())


def test_network_error_raises(validator):
    with patch.object(sv.requests, "post", side_effect=OSError("connection reset")):
        with pytest.raises(ScryfallUnavailable):
            validator.validate_cards(_cards())


def test_malformed_json_raises(validator):
    class Broken(FakeResponse):
        def json(self):
            raise ValueError("not json")

    with patch.object(sv.requests, "post", return_value=Broken(200)):
        with pytest.raises(ScryfallUnavailable):
            validator.validate_cards(_cards())


def test_outage_never_produces_a_green_light(validator):
    """The end-to-end guarantee: an outage must not yield a ready-to-print verdict."""
    meta = classify_upload("Test1_2.txt", len(DECK))
    content = parse_deck_lines(DECK.splitlines())

    with patch.object(sv.requests, "post", return_value=FakeResponse(503)):
        try:
            diagnostics = validator.validate_cards(content.cards)
        except ScryfallUnavailable:
            return  # The caller is told, and never reaches a traffic light.

        # If validation ever returns instead of raising, it must not claim GREEN.
        audit = evaluate_traffic_light(meta, content, diagnostics)
        assert audit.traffic_light != "GREEN"


def test_retries_then_succeeds(validator):
    """A single hiccup should recover rather than fail the whole check."""
    ok = FakeResponse(200, {"data": [], "not_found": []})
    responses = [FakeResponse(503), FakeResponse(429, headers={"Retry-After": "0"}), ok]

    with patch.object(sv.requests, "post", side_effect=responses) as post:
        validator.validate_cards(_cards())

    assert post.call_count == 3


def test_client_error_is_not_retried(validator):
    """A 404 means a bad request; hammering Scryfall will not fix it."""
    with patch.object(sv.requests, "post", return_value=FakeResponse(404)) as post:
        with pytest.raises(ScryfallUnavailable):
            validator.validate_cards(_cards())

    assert post.call_count == 1


def test_rate_limit_honours_retry_after(validator):
    """429 with Retry-After should wait as instructed before the next attempt."""
    responses = [
        FakeResponse(429, headers={"Retry-After": "2"}),
        FakeResponse(200, {"data": [], "not_found": []}),
    ]
    with patch.object(sv.requests, "post", side_effect=responses):
        with patch.object(sv.time, "sleep") as slept:
            validator.validate_cards(_cards())

    assert 2 in [call.args[0] for call in slept.call_args_list if call.args]
