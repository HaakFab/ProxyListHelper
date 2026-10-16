import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portal import orders_service, state_machine
from portal.db import Base, init_engine, session_scope
from portal.models import CheckRun, Round, Submission, SubmissionVersion, User


@pytest.fixture
def db():
    engine = init_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield


@pytest.fixture
def round_and_user(db):
    with session_scope() as s:
        r = Round(
            code="2611", name="Test Round", state="OPEN",
            cost_per_card_cents=13, cost_per_list_cents=26, processing_charge_cents=0,
        )
        s.add(r)
        u = User(display_name="Chris", person_slug="Chris", access_code_hash="x")
        s.add(u)
        s.flush()
        return r.id, u.id


def _accepted_submission(session, round_id, user_id, ordinal, n_cards, title="Deck"):
    sub = Submission(round_id=round_id, user_id=user_id, title=title, ordinal=ordinal, state="CHECKING")
    session.add(sub)
    session.flush()
    version = SubmissionVersion(
        submission_id=sub.id, version_no=1, raw_text=f"{n_cards} Sol Ring (LEA) 1",
        content_sha256="raw" + "x" * 61,
    )
    session.add(version)
    session.flush()
    check_run = CheckRun(
        submission_version_id=version.id, state="DONE", scryfall_ok=True,
        traffic_light="GREEN", total_cards=n_cards, unique_lines=1,
        fixed_text=f"{n_cards} Sol Ring (LEA) 1\n", fixed_sha256="fixed" + "y" * 59,
    )
    session.add(check_run)
    sub.current_version_id = version.id
    session.flush()
    state_machine.apply_check_result(sub, "GREEN")
    state_machine.accept(sub, version.id)
    session.flush()
    return sub


class TestConfirmOrder:
    def test_locks_a_single_submission_with_correct_price(self, round_and_user):
        round_id, user_id = round_and_user
        with session_scope() as s:
            round_ = s.get(Round, round_id)
            user = s.get(User, user_id)
            _accepted_submission(s, round_id, user_id, ordinal=1, n_cards=100)

            order = orders_service.confirm_order(s, round_, user)
            s.flush()

            assert order.state == "LOCKED"
            assert order.reference_code.startswith("MTG-2611-CHRIS-")
            snapshot = order.price_snapshot
            assert snapshot.n_cards == 100
            assert snapshot.n_lists == 1
            assert snapshot.total_cents == 100 * 13 + 26

    def test_locks_multiple_submissions_and_sums_correctly(self, round_and_user):
        round_id, user_id = round_and_user
        with session_scope() as s:
            round_ = s.get(Round, round_id)
            user = s.get(User, user_id)
            _accepted_submission(s, round_id, user_id, ordinal=1, n_cards=99, title="Deck A")
            _accepted_submission(s, round_id, user_id, ordinal=2, n_cards=4, title="Deck B")

            order = orders_service.confirm_order(s, round_, user)
            snapshot = order.price_snapshot

            assert snapshot.n_cards == 103
            assert snapshot.n_lists == 2
            assert snapshot.total_cents == 103 * 13 + 2 * 26

    def test_breakdown_json_records_what_the_user_agreed_to(self, round_and_user):
        round_id, user_id = round_and_user
        with session_scope() as s:
            round_ = s.get(Round, round_id)
            user = s.get(User, user_id)
            sub = _accepted_submission(s, round_id, user_id, ordinal=1, n_cards=100)

            order = orders_service.confirm_order(s, round_, user)
            breakdown = json.loads(order.price_snapshot.breakdown_json)

            assert breakdown["submissions"][0]["submission_id"] == sub.id
            assert breakdown["submissions"][0]["content_sha256"] == "fixed" + "y" * 59

    def test_only_locks_submissions_that_are_accepted(self, round_and_user):
        """A NEEDS_FIXES or still-CHECKING submission must never be billed."""
        round_id, user_id = round_and_user
        with session_scope() as s:
            round_ = s.get(Round, round_id)
            user = s.get(User, user_id)
            _accepted_submission(s, round_id, user_id, ordinal=1, n_cards=50)
            unaccepted = Submission(round_id=round_id, user_id=user_id, title="Broken",
                                     ordinal=2, state="NEEDS_FIXES")
            s.add(unaccepted)

            order = orders_service.confirm_order(s, round_, user)
            assert order.price_snapshot.n_lists == 1
            assert order.price_snapshot.n_cards == 50

    def test_no_accepted_submissions_raises(self, round_and_user):
        round_id, user_id = round_and_user
        with session_scope() as s:
            round_ = s.get(Round, round_id)
            user = s.get(User, user_id)
            with pytest.raises(orders_service.NoAcceptedSubmissions):
                orders_service.confirm_order(s, round_, user)

    def test_second_order_gets_seq_two_and_a_fresh_reference(self, round_and_user):
        round_id, user_id = round_and_user
        with session_scope() as s:
            round_ = s.get(Round, round_id)
            user = s.get(User, user_id)
            _accepted_submission(s, round_id, user_id, ordinal=1, n_cards=10)
            first_order = orders_service.confirm_order(s, round_, user)
            first_reference = first_order.reference_code

        with session_scope() as s:
            round_ = s.get(Round, round_id)
            user = s.get(User, user_id)
            _accepted_submission(s, round_id, user_id, ordinal=2, n_cards=5)
            second_order = orders_service.confirm_order(s, round_, user)

            assert second_order.seq == 2
            assert second_order.reference_code != first_reference

    def test_reopening_a_locked_order_and_reconfirming_creates_a_new_snapshot(self, round_and_user):
        """Editing after LOCKED (but before payment) must never leave the old
        price snapshot in effect once the order is re-confirmed."""
        round_id, user_id = round_and_user
        with session_scope() as s:
            round_ = s.get(Round, round_id)
            user = s.get(User, user_id)
            sub = _accepted_submission(s, round_id, user_id, ordinal=1, n_cards=100)
            order = orders_service.confirm_order(s, round_, user)
            order_id = order.id
            first_snapshot_id = order.price_snapshot_id

            # User edits the (unpaid, LOCKED) list -> reopens the order.
            state_machine.edit(s, sub)
            assert order.state == "OPEN"
            assert order.price_snapshot_id is None

            # Re-accept with a different card count and reconfirm.
            new_version = SubmissionVersion(
                submission_id=sub.id, version_no=2, raw_text="120 Sol Ring (LEA) 1",
                content_sha256="raw2" + "x" * 60,
            )
            s.add(new_version)
            s.flush()
            new_check = CheckRun(
                submission_version_id=new_version.id, state="DONE", scryfall_ok=True,
                traffic_light="GREEN", total_cards=120, unique_lines=1,
                fixed_text="120 Sol Ring (LEA) 1\n", fixed_sha256="fixed2" + "y" * 58,
            )
            s.add(new_check)
            sub.current_version_id = new_version.id
            s.flush()
            state_machine.apply_check_result(sub, "GREEN")
            state_machine.accept(sub, new_version.id)

            reconfirmed = orders_service.confirm_order(s, round_, user)
            assert reconfirmed.id == order_id  # same order row, relocked
            assert reconfirmed.price_snapshot_id != first_snapshot_id
            assert reconfirmed.price_snapshot.n_cards == 120
