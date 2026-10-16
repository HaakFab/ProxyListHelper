"""Cost-share arithmetic. Integer cents only -- see portal_implementation.md §2:
money in float eventually renders 13.000000000002 and, worse, can accumulate a
rounding error that doesn't match what the person actually owes.

The rule, stated once here so it never drifts from what the price page tells
users: total = (cards, counted as sum(qty) i.e. per COPY, not per unique card)
x price-per-card + (number of lists) x price-per-list + a flat processing fee.
A double-faced card counts once (it is one physical card front+back). The
per-list charge covers the printed divider card for that list.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Rates:
    cost_per_card_cents: int
    cost_per_list_cents: int
    processing_charge_cents: int

    def __post_init__(self):
        for name in ("cost_per_card_cents", "cost_per_list_cents", "processing_charge_cents"):
            value = getattr(self, name)
            if value < 0:
                raise ValueError(f"{name} cannot be negative, got {value}")


@dataclass(frozen=True)
class SubmissionCharge:
    submission_id: int
    title: str
    n_cards: int
    content_sha256: str


@dataclass(frozen=True)
class PriceBreakdown:
    rates: Rates
    n_cards: int
    n_lists: int
    total_cents: int
    per_submission: tuple[SubmissionCharge, ...]

    def to_breakdown_dict(self) -> dict:
        """What gets stored in PriceSnapshot.breakdown_json -- exactly what
        the user was shown and agreed to."""
        return {
            "cost_per_card_cents": self.rates.cost_per_card_cents,
            "cost_per_list_cents": self.rates.cost_per_list_cents,
            "processing_charge_cents": self.rates.processing_charge_cents,
            "n_cards": self.n_cards,
            "n_lists": self.n_lists,
            "total_cents": self.total_cents,
            "submissions": [
                {
                    "submission_id": s.submission_id,
                    "title": s.title,
                    "n_cards": s.n_cards,
                    "content_sha256": s.content_sha256,
                }
                for s in self.per_submission
            ],
        }


def compute_total_cents(n_cards: int, n_lists: int, rates: Rates) -> int:
    return (
        n_cards * rates.cost_per_card_cents
        + n_lists * rates.cost_per_list_cents
        + rates.processing_charge_cents
    )


def compute_breakdown(submissions: list[SubmissionCharge], rates: Rates) -> PriceBreakdown:
    n_cards = sum(s.n_cards for s in submissions)
    n_lists = len(submissions)
    total = compute_total_cents(n_cards, n_lists, rates)
    return PriceBreakdown(
        rates=rates,
        n_cards=n_cards,
        n_lists=n_lists,
        total_cents=total,
        per_submission=tuple(submissions),
    )


def format_cents_eur(cents: int) -> str:
    """German display convention: comma decimal separator, no thousands
    separator (amounts here are always well under 1000 EUR)."""
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    euros, remainder = divmod(cents, 100)
    return f"{sign}{euros},{remainder:02d} €"


def balance_cents(total_cents: int, payments_cents: list[int]) -> int:
    """Positive: still owed. Zero: settled. Negative: overpaid / refund due.
    `payments_cents` includes negative entries for refunds -- see
    PaymentRecord.amount_cents in portal/models.py."""
    return total_cents - sum(payments_cents)
