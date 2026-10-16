"""Test suite for the new ProxyListHelper web application and pipelines."""
import io
import json
import zipfile
import pytest
from starlette.testclient import TestClient

from app.config import settings
from app.database import Base, engine, init_db
from app.main import app
from app.moxfield import extract_deck_cards, parse_deck_id, parse_pasted_text
from app.pricing import compute_total_cents, format_cents_eur
from proxy_parser.deck_parser import parse_moxfield_file

@pytest.fixture(autouse=True)
def setup_test_db():
    init_db()
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

def test_extract_tokens_option():
    mock_data = {
        "name": "Token Deck",
        "boards": {
            "mainboard": {
                "cards": {
                    "c1": {"quantity": 1, "card": {"name": "Raise the Alarm", "set": "cmm", "cn": "50"}}
                }
            },
            "tokens": {
                "cards": {
                    "t1": {"quantity": 2, "card": {"name": "Soldier Token", "set": "tcmm", "cn": "1"}}
                }
            }
        },
        "tokens": [
            {"name": "Treasure Token", "set": "tcmm", "cn": "15"}
        ]
    }
    
    # Without tokens
    _, without_tok = extract_deck_cards(mock_data, include_tokens=False)
    assert len(without_tok) == 1
    assert without_tok[0]["name"] == "Raise the Alarm"

    # With tokens
    _, with_tok = extract_deck_cards(mock_data, include_tokens=True)
    assert len(with_tok) == 3
    names = [c["name"] for c in with_tok]
    assert "Raise the Alarm" in names
    assert "Soldier Token" in names
    assert "Treasure Token" in names

def test_empty_deck_submission_redirect_and_error_banner(client):
    # Submit with no valid deck content
    resp = client.post("/submit", data={
        "ui_lang": "en",
        "deck_count": "1",
        "submitter_name": "Fabian",
        "deck_name_0": "Empty Deck",
        "source_type_0": "url",
        "moxfield_url_0": "",
        "raw_text_0": "",
    }, follow_redirects=False)

    assert resp.status_code == 303
    assert "error=empty_decks" in resp.headers["location"]

    # Following the redirect displays the error banner
    follow_resp = client.get(resp.headers["location"])
    assert follow_resp.status_code == 200
    assert "No valid deck found" in follow_resp.text
    assert "Include Tokens" in follow_resp.text


def test_alchemy_rebalanced_card_conversion():
    from app.converter import process_deck_cards
    from unittest.mock import patch

    mock_card = {
        "name": "Teferi, Time Raveler",
        "set": "war",
        "collector_number": "221",
        "image_uris": {"normal": "https://cards.scryfall.io/normal/front/war/221.jpg"},
        "games": ["paper", "arena", "mtgo"]
    }

    with patch("app.converter.get_card_by_set_cn", return_value=mock_card), \
         patch("app.converter.preload_cards_batch"):
        cards = [{"qty": 1, "name": "A-Teferi, Time Raveler", "set": "war", "cn": "A-221"}]
        audit_items, overall_status, converted_text = process_deck_cards(cards)

        assert overall_status == "YELLOW"
        assert len(audit_items) == 1
        item = audit_items[0]
        assert item.category == "ALCHEMY_REPLACED"
        assert item.name == "Teferi, Time Raveler"
        assert "1 Teferi, Time Raveler (WAR) 221" in converted_text
        assert item.fixed_image_url == "https://cards.scryfall.io/normal/front/war/221.jpg"


def test_admin_new_order_cycle(client):
    import uuid
    from app.models import Order, Deck
    from app.database import get_db

    db = next(get_db())
    unique_id = uuid.uuid4().hex[:8]
    test_order = Order(
        order_id=f"test_ord_{unique_id}",
        submitter_name="Test User",
        reference_code=f"REF-{unique_id}",
        total_lists=1,
        total_cards=10,
        total_price_cents=100,
        price_per_card_cents=10,
        price_per_list_cents=20,
        order_fee_cents=50
    )
    db.add(test_order)
    db.commit()

    test_deck = Deck(
        order_id=test_order.id,
        name="Test Deck",
        card_count=10,
        raw_text="1 Sol Ring",
        converted_text="1 Sol Ring"
    )
    db.add(test_deck)
    db.commit()

    assert db.query(Order).count() >= 1
    assert db.query(Deck).count() >= 1

    # Call /admin/cycle/new with admin cookie
    client.cookies.set("admin_session", settings.admin_password)
    resp = client.post("/admin/cycle/new", data={"lang": "en"}, follow_redirects=False)
    assert resp.status_code == 303
    assert "cycle_reset=1" in resp.headers["location"]

    # Decks and orders should be wiped
    assert db.query(Order).count() == 0
    assert db.query(Deck).count() == 0


def test_moxfield_tokens_excludes_spellbook_and_regular_cards():
    from app.moxfield import extract_deck_cards

    mock_deck = {
        "name": "Spellbook Test",
        "boards": {
            "mainboard": {
                "cards": {
                    "m1": {"quantity": 1, "card": {"name": "Ponder", "set": "who", "cn": "217"}}
                }
            }
        },
        "tokens": [
            {"name": "Treasure", "set": "tsos", "cn": "12", "isToken": True, "layout": "token", "type_line": "Token Artifact — Treasure"},
            {"name": "Preponderant Pearl", "set": "yecl", "cn": "5", "isToken": False, "layout": "normal", "type_line": "Kindred Artifact — Merfolk"},
            {"name": "Tome of the Infinite", "set": "j21", "cn": "13", "isToken": False, "layout": "normal", "type_line": "Legendary Artifact — Book"},
            {"name": "Command Tower", "set": "slz", "cn": "360", "isToken": False, "layout": "normal", "type_line": "Land"}
        ]
    }

    _, cards = extract_deck_cards(mock_deck, include_tokens=True)
    names = [c["name"] for c in cards]
    assert "Ponder" in names
    assert "Treasure" in names
    assert "Preponderant Pearl" not in names
    assert "Tome of the Infinite" not in names
    assert "Command Tower" not in names


def test_replace_date_stamped_promo_cards():
    from app.converter import process_deck_cards
    from unittest.mock import patch

    mock_promo_card = {
        "name": "Smothering Tithe",
        "set": "prna",
        "collector_number": "22",
        "promo_types": ["prerelease", "datestamped"],
        "image_uris": {"normal": "https://cards.scryfall.io/normal/front/prna/22.jpg"}
    }
    mock_clean_card = {
        "name": "Smothering Tithe",
        "set": "cmm",
        "collector_number": "57",
        "promo_types": [],
        "image_uris": {"normal": "https://cards.scryfall.io/normal/front/cmm/57.jpg"}
    }

    with patch("app.converter.get_card_by_set_cn", return_value=mock_promo_card), \
         patch("app.converter.find_clean_paper_fallback", return_value=mock_clean_card), \
         patch("app.converter.preload_cards_batch"):
        cards = [{"qty": 1, "name": "Smothering Tithe", "set": "prna", "cn": "22"}]
        audit_items, overall_status, converted_text = process_deck_cards(cards, replace_date_stamps=True)

        assert overall_status == "YELLOW"
        assert len(audit_items) == 1
        assert audit_items[0].category == "DATE_STAMP_REPLACED"
        assert "1 Smothering Tithe (CMM) 57" in converted_text


def test_replace_digital_promo_cards():
    from app.converter import process_deck_cards
    from unittest.mock import patch

    mock_digital_card = {
        "name": "Force of Will",
        "set": "vma",
        "collector_number": "4",
        "digital": True,
        "games": ["mtgo"],
        "image_uris": {"normal": "https://cards.scryfall.io/normal/front/vma/4.jpg"}
    }
    mock_paper_card = {
        "name": "Force of Will",
        "set": "dmr",
        "collector_number": "60",
        "digital": False,
        "games": ["paper", "mtgo"],
        "image_uris": {"normal": "https://cards.scryfall.io/normal/front/dmr/60.jpg"}
    }

    with patch("app.converter.get_card_by_set_cn", return_value=mock_digital_card), \
         patch("app.converter.find_clean_paper_fallback", return_value=mock_paper_card), \
         patch("app.converter.preload_cards_batch"):
        cards = [{"qty": 1, "name": "Force of Will", "set": "vma", "cn": "4"}]
        audit_items, overall_status, converted_text = process_deck_cards(cards, replace_digital=True)

        assert overall_status == "YELLOW"
        assert len(audit_items) == 1
        assert audit_items[0].category == "DIGITAL_REPLACED"
        assert "1 Force of Will (DMR) 60" in converted_text



