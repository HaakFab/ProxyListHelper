"""Test suite for the new ProxyListHelper web application and pipelines."""
import io
import json
import zipfile
import pytest
from starlette.testclient import TestClient

from app.config import settings
from app.database import Base, engine
from app.main import app
from app.moxfield import extract_deck_cards, parse_deck_id, parse_pasted_text
from app.pricing import compute_total_cents, format_cents_eur
from proxy_parser.deck_parser import parse_moxfield_file

@pytest.fixture(autouse=True)
def setup_test_db():
    Base.metadata.create_all(bind=engine)
    yield

@pytest.fixture
def client():
    return TestClient(app)

def test_parse_moxfield_id():
    assert parse_deck_id("https://moxfield.com/decks/HjBNu72nG0eQ6C_8meQ5TQ") == "HjBNu72nG0eQ6C_8meQ5TQ"
    assert parse_deck_id("https://www.moxfield.com/decks/abc123_xyz") == "abc123_xyz"
    assert parse_deck_id("simple_id_123") == "simple_id_123"

def test_extract_moxfield_boards():
    mock_deck_data = {
        "name": "Test Deck",
        "boards": {
            "commanders": {
                "cards": {
                    "c1": {"quantity": 1, "card": {"name": "Glarb", "set": "blb", "cn": "210"}}
                }
            },
            "mainboard": {
                "cards": {
                    "m1": {"quantity": 2, "card": {"name": "Island", "set": "unf", "cn": "236"}},
                    "m2": {"quantity": 1, "card": {"name": "Sol Ring", "set": "cmm", "cn": "401"}}
                }
            },
            "sideboard": {
                "cards": {
                    "s1": {"quantity": 1, "card": {"name": "Flusterstorm", "set": "mh1", "cn": "255"}}
                }
            },
            "maybeboard": {
                "cards": {
                    "mb1": {"quantity": 3, "card": {"name": "Forest", "set": "unf", "cn": "239"}}
                }
            }
        }
    }

    # 1. Mainboard + commanders only
    title, cards = extract_deck_cards(mock_deck_data, include_sideboard=False, include_considering=False)
    assert title == "Test Deck"
    assert len(cards) == 3
    assert sum(c["qty"] for c in cards) == 4

    # 2. Include sideboard
    _, cards_with_sb = extract_deck_cards(mock_deck_data, include_sideboard=True, include_considering=False)
    assert len(cards_with_sb) == 4
    assert sum(c["qty"] for c in cards_with_sb) == 5

    # 3. Include considering (maybeboard)
    _, cards_with_all = extract_deck_cards(mock_deck_data, include_sideboard=True, include_considering=True)
    assert len(cards_with_all) == 5
    assert sum(c["qty"] for c in cards_with_all) == 8

def test_parse_pasted_text():
    pasted = """
    // Mainboard
    1 Sol Ring (CMM) 401 *F*
    2 Rhystic Study (WOT) 25
    1 Island
    """
    cards = parse_pasted_text(pasted)
    assert len(cards) == 3
    assert cards[0]["name"] == "Sol Ring"
    assert cards[0]["set"] == "cmm"
    assert cards[0]["cn"] == "401"
    assert cards[0]["is_foil"] is True
    assert cards[1]["qty"] == 2

def test_pricing_arithmetic():
    rates = {
        "price_per_card_cents": 13,
        "price_per_list_cents": 26,
        "order_fee_cents": 100,
    }
    # 100 cards across 2 lists + 1.00 fee:
    # 100 * 13 + 2 * 26 + 100 = 1300 + 52 + 100 = 1452 cents (€14.52)
    total = compute_total_cents(100, 2, rates)
    assert total == 1452
    assert format_cents_eur(total) == "14.52 €"

def test_deck_parser_language_directive(tmp_path):
    deck_file = tmp_path / "decklist.txt"
    deck_file.write_text(
        "// Language: de\n"
        "1 Sol Ring (CMM) 401\n"
        "2 Island\n"
    )
    deck = parse_moxfield_file(str(deck_file))
    assert deck.preferred_language == "de"
    assert len(deck.cards) == 2
    assert deck.cards[0].lang == "de"
    assert deck.cards[0].key == "cmm_401_de"
    assert deck.cards[1].lang == "de"

def test_web_routes_flow(client):
    # 1. Landing page loads
    resp = client.get("/")
    assert resp.status_code == 200
    assert "ProxyListHelper" in resp.text

    # 2. German landing page
    resp_de = client.get("/?lang=de")
    assert resp_de.status_code == 200
    assert "Dein Name" in resp_de.text

    # 3. Submit a deck with pasted text
    form_data = {
        "submitter_name": "FabianTest",
        "ui_lang": "en",
        "deck_count": "1",
        "deck_name_0": "Test Commander",
        "source_type_0": "text",
        "raw_text_0": "1 Sol Ring (CMM) 401\n1 Island (UNF) 236",
        "preferred_language_0": "en",
        "convert_foils_0": "on",
        "replace_stamps_0": "on",
    }
    submit_resp = client.post("/submit", data=form_data, follow_redirects=False)
    assert submit_resp.status_code == 303
    redirect_url = submit_resp.headers["location"]
    assert "/success/" in redirect_url or "/review/" in redirect_url

    # Follow redirect
    review_resp = client.get(redirect_url)
    assert review_resp.status_code == 200

def test_admin_flow_and_exports(client):
    # 1. Login required for dashboard
    dash_resp = client.get("/admin", follow_redirects=False)
    assert dash_resp.status_code == 303
    assert "/admin/login" in dash_resp.headers["location"]

    # 2. Login with correct password
    login_resp = client.post("/admin/login", data={"password": settings.admin_password}, follow_redirects=False)
    assert login_resp.status_code == 303
    admin_cookie = login_resp.cookies.get("admin_session")

    # 3. Access dashboard with cookie
    client.cookies.set("admin_session", admin_cookie)
    dash_auth = client.get("/admin")
    assert dash_auth.status_code == 200
    assert "Admin Dashboard" in dash_auth.text

    # 4. Export CSV
    csv_resp = client.get("/admin/export/csv")
    assert csv_resp.status_code == 200
    assert "text/csv" in csv_resp.headers["content-type"]
    assert "=== Person Summary ===" in csv_resp.text

    # 5. Export Master ZIP
    zip_resp = client.get("/admin/export/zip")
    assert zip_resp.status_code == 200
    assert "application/zip" in zip_resp.headers["content-type"]
    
    # Verify ZIP contents
    zf = zipfile.ZipFile(io.BytesIO(zip_resp.content))
    assert "summary_costs.csv" in zf.namelist()
