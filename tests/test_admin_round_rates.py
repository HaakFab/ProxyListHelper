"""Round rates are set directly at creation and can only be adjusted while
the round is still DRAFT. This is what closes a real gap the earlier design
had: a round used to carry no rates of its own until "Runde öffnen" read
whatever Settings said AT THAT MOMENT -- so editing Settings between creating
a round and opening it silently changed the round's price out from under
whoever set it up. These tests exist specifically to keep that fixed.
"""
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _set_env(tmp_path):
    os.environ["SECRET_KEY"] = "x" * 32
    os.environ["WORKER_TOKEN"] = "y" * 32
    os.environ["ADMIN_PASSWORD"] = "admin-test-password"
    os.environ["INVITE_CODE"] = "let-me-in"
    os.environ["PAYPAL_ME_HANDLE"] = "testhandle"
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp_path}/portal.db"
    os.environ["MAIL_BACKEND"] = "none"


@pytest.fixture
def client(tmp_path):
    _set_env(tmp_path)
    from portal import db, jobs, settings as settings_module
    settings_module.get_settings.cache_clear()

    with patch.object(jobs, "run_worker_loop", lambda stop_event: None):
        from portal.main import create_app
        from portal.db import Base

        app = create_app()
        Base.metadata.create_all(db.get_engine())

        from portal.cli import bootstrap
        bootstrap()

        from starlette.testclient import TestClient
        with TestClient(app, base_url="https://testserver") as c:
            yield c


def _csrf(response) -> str:
    for cookie in response.cookies.jar:
        if cookie.name == "csrf":
            return cookie.value
    raise AssertionError("no csrf cookie found in response")


def _login_as_admin(client):
    r = client.get("/gate")
    r = client.post("/gate", data={"csrf_token": _csrf(r), "code": "let-me-in"})
    r = client.get("/login")
    r = client.post("/login", data={
        "csrf_token": _csrf(r), "person_slug": "Admin", "access_code": "admin-test-password",
    })
    return r


class TestRoundCreationCarriesItsOwnRates:
    def test_custom_rates_are_stored_on_the_round(self, client):
        r = _login_as_admin(client)
        r = client.get("/admin")
        r = client.post("/admin/round/new", data={
            "csrf_token": _csrf(r), "code": "3001", "name": "Custom Rates Round",
            "cost_per_card_cents": "20", "cost_per_list_cents": "50", "processing_charge_cents": "100",
        })
        assert r.status_code == 200

        from portal import db
        from portal.models import Round
        with db.session_scope() as s:
            round_ = s.query(Round).filter(Round.code == "3001").one()
            assert round_.cost_per_card_cents == 20
            assert round_.cost_per_list_cents == 50
            assert round_.processing_charge_cents == 100
            assert round_.state == "DRAFT"

    def test_rates_survive_unchanged_when_settings_change_before_opening(self, client):
        """The actual regression test for the bug this feature fixes."""
        r = _login_as_admin(client)
        r = client.get("/admin")
        r = client.post("/admin/round/new", data={
            "csrf_token": _csrf(r), "code": "3002", "name": "Drift Test",
            "cost_per_card_cents": "20", "cost_per_list_cents": "50", "processing_charge_cents": "100",
        })

        from portal import db
        from portal.models import Round
        with db.session_scope() as s:
            round_id = s.query(Round).filter(Round.code == "3002").one().id

        # Change the SITE-WIDE defaults after the round was created -- this
        # must not leak into the already-created round.
        r = client.get("/admin/settings")
        client.post("/admin/settings", data={
            "csrf_token": _csrf(r),
            "cost_per_card_cents": "999", "cost_per_list_cents": "999",
            "processing_charge_cents": "999", "paypal_me_handle": "testhandle",
            "retention_days": "90",
        })

        r = client.get("/admin")
        client.post(f"/admin/round/{round_id}/open", data={"csrf_token": _csrf(r)})

        with db.session_scope() as s:
            round_ = s.get(Round, round_id)
            assert round_.state == "OPEN"
            assert round_.cost_per_card_cents == 20      # unchanged, not 999
            assert round_.cost_per_list_cents == 50       # unchanged, not 999
            assert round_.processing_charge_cents == 100  # unchanged, not 999


class TestUpdatingRatesWhileDraft:
    def test_rates_can_be_adjusted_before_opening(self, client):
        r = _login_as_admin(client)
        r = client.get("/admin")
        client.post("/admin/round/new", data={
            "csrf_token": _csrf(r), "code": "3003", "name": "Edit Me",
            "cost_per_card_cents": "13", "cost_per_list_cents": "26", "processing_charge_cents": "0",
        })

        from portal import db
        from portal.models import Round
        with db.session_scope() as s:
            round_id = s.query(Round).filter(Round.code == "3003").one().id

        r = client.get("/admin")
        r = client.post(f"/admin/round/{round_id}/update-rates", data={
            "csrf_token": _csrf(r),
            "cost_per_card_cents": "15", "cost_per_list_cents": "30", "processing_charge_cents": "50",
        })
        assert r.status_code == 200

        with db.session_scope() as s:
            round_ = s.get(Round, round_id)
            assert round_.cost_per_card_cents == 15
            assert round_.cost_per_list_cents == 30
            assert round_.processing_charge_cents == 50

    def test_rates_cannot_be_changed_after_opening(self, client):
        r = _login_as_admin(client)
        r = client.get("/admin")
        client.post("/admin/round/new", data={
            "csrf_token": _csrf(r), "code": "3004", "name": "Locked After Open",
            "cost_per_card_cents": "13", "cost_per_list_cents": "26", "processing_charge_cents": "0",
        })

        from portal import db
        from portal.models import Round
        with db.session_scope() as s:
            round_id = s.query(Round).filter(Round.code == "3004").one().id

        r = client.get("/admin")
        client.post(f"/admin/round/{round_id}/open", data={"csrf_token": _csrf(r)})

        r = client.get("/admin")
        client.post(f"/admin/round/{round_id}/update-rates", data={
            "csrf_token": _csrf(r),
            "cost_per_card_cents": "999", "cost_per_list_cents": "999", "processing_charge_cents": "999",
        })

        with db.session_scope() as s:
            round_ = s.get(Round, round_id)
            assert round_.state == "OPEN"
            assert round_.cost_per_card_cents == 13   # untouched
            assert round_.cost_per_list_cents == 26
            assert round_.processing_charge_cents == 0


class TestOpeningRefusesUndefinedRates:
    def test_a_round_with_null_rates_cannot_be_opened(self, client):
        """Defense in depth: even though the current UI always sets rates at
        creation, opening must still refuse a round that somehow has none --
        orders_service would otherwise crash multiplying against None."""
        from portal import db
        from portal.models import Round
        with db.session_scope() as s:
            round_ = Round(code="3005", name="No Rates", state="DRAFT")
            s.add(round_)
            s.flush()
            round_id = round_.id

        r = _login_as_admin(client)
        r = client.get("/admin")
        client.post(f"/admin/round/{round_id}/open", data={"csrf_token": _csrf(r)})

        with db.session_scope() as s:
            round_ = s.get(Round, round_id)
            assert round_.state == "DRAFT"  # refused, not opened
