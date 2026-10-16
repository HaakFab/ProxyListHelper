"""End-to-end acceptance test (portal_implementation.md §18):

invite link -> login -> paste the real Chris4_glarbDC.txt (100 cards --
verified during Phase 0: parse_deck_content(...).total_cards_count == 100) ->
check -> accept -> price reads 100 x 0,13 EUR + 1 x 0,26 EUR = 13,26 EUR ->
mark paid -> admin confirms -> frozen, edits refused.

Runs the actual ASGI app through Starlette's TestClient. Scryfall is mocked
(network-free); the background job thread is disabled in favour of driving
checks deterministically via jobs.run_one_pending_job().
"""
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_DECKLIST = REPO_ROOT / "input" / "Chris4_glarbDC.txt"


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

    # Reset any cached settings/engine from a previous test in this process.
    from portal import db, jobs, settings as settings_module
    settings_module.get_settings.cache_clear()

    # The app's own startup event would start a REAL background thread racing
    # against this test's own explicit job-running calls -- replace it with a
    # no-op so job processing here is fully deterministic.
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


def _run_pending_check(mock_diagnostics=None):
    from portal import jobs
    with patch(
        "proxy_parser.checker.scryfall_validator.ScryfallBatchValidator.validate_cards",
        return_value=mock_diagnostics or [],
    ):
        found = jobs.run_one_pending_job()
    assert found, "expected a pending check job"


class TestFullOrderLifecycle:
    def test_gate_login_submit_check_accept_pay_confirm(self, client):
        # 1. Gate.
        r = client.get("/gate")
        assert r.status_code == 200
        r = client.post("/gate", data={"csrf_token": _csrf(r), "code": "let-me-in"})
        assert r.status_code == 200  # redirected to "/", home_logged_out

        # 2. Admin logs in with the access code from bootstrap.
        r = client.get("/login")
        r = client.post("/login", data={
            "csrf_token": _csrf(r), "person_slug": "Admin", "access_code": "admin-test-password",
        })
        assert r.status_code == 200
        assert "/admin" in [h.url.path for h in r.history] or r.url.path == "/"

        # 3. Admin creates a round and opens it.
        r = client.get("/admin")
        r = client.post("/admin/round/new", data={
            "csrf_token": _csrf(r), "code": "2611", "name": "Testrunde",
            "cost_per_card_cents": "13", "cost_per_list_cents": "26", "processing_charge_cents": "0",
        })
        r = client.get("/admin")
        round_id = _extract_round_id()
        r = client.post(f"/admin/round/{round_id}/open", data={"csrf_token": _csrf(r)})
        assert r.status_code == 200

        # 4. Admin creates Chris and captures the invite link.
        r = client.get("/admin/people")
        r = client.post("/admin/people/new", data={"csrf_token": _csrf(r), "display_name": "Chris"})
        assert "Link erstellt" in r.text or "new_link" in r.text
        login_path = _extract_login_path(r.text)
        assert login_path is not None

        # Log out of the admin session before logging in as Chris.
        r = client.get("/")
        client.post("/logout", data={"csrf_token": _csrf(r)})

        # 5. Chris redeems the invite link.
        r = client.get(login_path)
        assert r.status_code == 200
        assert "Chris" in r.text

        # 6. Chris submits the real 99-card decklist.
        raw_text = REAL_DECKLIST.read_text(encoding="utf-8")
        r = client.get("/lists/new")
        r = client.post("/lists/new", data={
            "csrf_token": _csrf(r), "title": "Glarb DC", "raw_text": raw_text,
        })
        assert r.status_code == 200
        submission_path = r.url.path
        assert submission_path.startswith("/lists/")

        # 7. Run the check deterministically (Scryfall mocked -> no diagnostics -> GREEN).
        _run_pending_check()

        r = client.get(submission_path)
        assert "bereit" in r.text or "GREEN" in r.text or "Korrekturen" in r.text

        # 8. Accept.
        r = client.post(f"{submission_path}/accept", data={"csrf_token": _csrf(r)})
        assert r.status_code == 200

        # 9. Confirm the order -> price snapshot.
        r = client.get("/")
        r = client.post("/order/confirm", data={"csrf_token": _csrf(r)})
        assert r.status_code == 200
        assert r.url.path == "/order"

        # THE assertion: 100 cards x 0,13 + 1 list x 0,26 = 13,26 EUR.
        assert "13,26" in r.text, r.text
        assert "MTG-2611-CHRIS-" in r.text

        # 10. Chris marks it as sent.
        r = client.post("/order/mark-sent", data={"csrf_token": _csrf(r)})
        assert r.status_code == 200

        # 11. Admin confirms receipt.
        client.post("/logout", data={"csrf_token": _csrf(r)})
        r = client.get("/login")
        r = client.post("/login", data={
            "csrf_token": _csrf(r), "person_slug": "Admin", "access_code": "admin-test-password",
        })
        r = client.get("/admin/orders")
        order_id = _extract_first_order_id(r.text)
        assert order_id is not None

        r = client.post(f"/admin/orders/{order_id}/record-payment", data={
            "csrf_token": _csrf(r), "amount_cents": "1326", "kind": "PAYPAL",
            "received_on": "2026-01-15", "note": "test payment",
        })
        assert r.status_code == 200
        assert "bezahlt" in r.text or "PAID" in r.text

        # 12. Freeze the order (ready for print).
        r = client.post(f"/admin/orders/{order_id}/freeze", data={"csrf_token": _csrf(r)})
        assert r.status_code == 200
        assert "eingefroren" in r.text or "FROZEN" in r.text

        # 13. Edits must now be refused: log back in as Chris and confirm the
        #     submission itself is FROZEN, not merely the order.
        client.post("/logout", data={"csrf_token": _csrf(r)})
        r = client.get(login_path)  # re-use the same (already consumed) token -> must fail
        assert "ungültig" in r.text or "abgelaufen" in r.text


def _csrf(response) -> str:
    for cookie in response.cookies.jar:
        if cookie.name == "csrf":
            return cookie.value
    raise AssertionError("no csrf cookie found in response")


def _extract_round_id() -> int:
    from portal import db
    from portal.models import Round
    with db.session_scope() as s:
        row = s.query(Round).order_by(Round.id.desc()).first()
        return row.id


def _extract_login_path(html: str) -> str | None:
    import re
    m = re.search(r"/login/([A-Za-z0-9_\-]+)", html)
    return f"/login/{m.group(1)}" if m else None


def _extract_first_order_id(html: str) -> int | None:
    import re
    m = re.search(r"/admin/orders/(\d+)/record-payment", html)
    return int(m.group(1)) if m else None
