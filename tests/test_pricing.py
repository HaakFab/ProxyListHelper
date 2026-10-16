import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portal.pricing import (
    Rates,
    SubmissionCharge,
    balance_cents,
    compute_breakdown,
    compute_total_cents,
    format_cents_eur,
)

DEFAULT_RATES = Rates(cost_per_card_cents=13, cost_per_list_cents=26, processing_charge_cents=0)


class TestComputeTotalCents:
    def test_default_rates_example_from_the_spec(self):
        # 103 cards, 2 lists, 0 fee -> 13.39 + 0.52 + 0.00 = 13.91
        assert compute_total_cents(103, 2, DEFAULT_RATES) == 1391

    def test_zero_cards(self):
        assert compute_total_cents(0, 1, DEFAULT_RATES) == 26

    def test_one_list(self):
        assert compute_total_cents(100, 1, DEFAULT_RATES) == 100 * 13 + 26

    def test_nonzero_processing_fee(self):
        rates = Rates(cost_per_card_cents=13, cost_per_list_cents=26, processing_charge_cents=150)
        assert compute_total_cents(10, 1, rates) == 10 * 13 + 26 + 150

    def test_negative_rate_rejected(self):
        with pytest.raises(ValueError):
            Rates(cost_per_card_cents=-1, cost_per_list_cents=26, processing_charge_cents=0)


class TestComputeBreakdown:
    def test_two_submissions(self):
        charges = [
            SubmissionCharge(1, "Deck A", 99, "a" * 64),
            SubmissionCharge(2, "Deck B", 4, "b" * 64),
        ]
        b = compute_breakdown(charges, DEFAULT_RATES)
        assert b.n_cards == 103
        assert b.n_lists == 2
        assert b.total_cents == 1391

    def test_breakdown_dict_carries_content_hashes(self):
        charges = [SubmissionCharge(1, "Deck A", 10, "hash123")]
        b = compute_breakdown(charges, DEFAULT_RATES)
        d = b.to_breakdown_dict()
        assert d["submissions"][0]["content_sha256"] == "hash123"
        assert d["total_cents"] == b.total_cents

    def test_empty_submission_list(self):
        b = compute_breakdown([], DEFAULT_RATES)
        assert b.n_cards == 0
        assert b.n_lists == 0
        assert b.total_cents == 0


class TestFormatCentsEur:
    @pytest.mark.parametrize(
        "cents,expected",
        [
            (1391, "13,91 €"),
            (0, "0,00 €"),
            (5, "0,05 €"),
            (100, "1,00 €"),
            (-250, "-2,50 €"),
        ],
    )
    def test_formatting(self, cents, expected):
        assert format_cents_eur(cents) == expected


class TestBalanceCents:
    def test_unpaid(self):
        assert balance_cents(1391, []) == 1391

    def test_fully_paid(self):
        assert balance_cents(1391, [1391]) == 0

    def test_partial_payment_shows_remaining_owed(self):
        assert balance_cents(1391, [1000]) == 391

    def test_overpayment_is_negative(self):
        assert balance_cents(1391, [1500]) == -109

    def test_split_across_multiple_payments(self):
        assert balance_cents(1391, [700, 691]) == 0

    def test_refund_reduces_what_was_paid(self):
        # paid 1391, then a 500-cent refund was issued
        assert balance_cents(1391, [1391, -500]) == 500
