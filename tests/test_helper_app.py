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

import os
import tempfile
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.database import Base, get_db

@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    test_db_path = tmp_path / "test_isolated.db"
    test_engine = create_engine(
        f"sqlite:///{test_db_path}",
        connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=test_engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    yield
    app.dependency_overrides.clear()
    test_engine.dispose()

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
    db_gen = app.dependency_overrides.get(get_db, get_db)
    db = next(db_gen())
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

def test_replace_promo_pack_stamped_broodlord():
    from app.converter import process_deck_cards
    from unittest.mock import patch

    mock_stamped_broodlord = {
        "name": "Hoarding Broodlord",
        "set": "pmom",
        "collector_number": "110p",
        "promo_types": ["promopack", "stamped"],
        "finishes": ["nonfoil", "foil"],
        "games": ["paper"],
        "image_uris": {"normal": "https://cards.scryfall.io/normal/front/pmom/110p.jpg"}
    }
    mock_clean_broodlord = {
        "name": "Hoarding Broodlord",
        "set": "mom",
        "collector_number": "110",
        "promo_types": [],
        "finishes": ["nonfoil", "foil"],
        "games": ["paper"],
        "image_uris": {"normal": "https://cards.scryfall.io/normal/front/mom/110.jpg"}
    }

    with patch("app.converter.get_card_by_set_cn", return_value=mock_stamped_broodlord), \
         patch("app.converter.find_clean_paper_fallback", return_value=mock_clean_broodlord), \
         patch("app.converter.preload_cards_batch"):
        cards = [{"qty": 1, "name": "Hoarding Broodlord", "set": "pmom", "cn": "110p"}]
        audit_items, overall_status, converted_text = process_deck_cards(cards, replace_stamps=True)

        assert overall_status == "YELLOW"
        assert len(audit_items) == 1
        assert audit_items[0].category == "STAMP_REPLACED"
        assert audit_items[0].original_image_url == "https://cards.scryfall.io/normal/front/pmom/110p.jpg"
        assert audit_items[0].fixed_image_url == "https://cards.scryfall.io/normal/front/mom/110.jpg"
        assert "1 Hoarding Broodlord (MOM) 110" in converted_text


def test_find_clean_paper_fallback_query_encoding():
    from app.scryfall import find_clean_paper_fallback
    from unittest.mock import patch

    captured_urls = []
    def mock_scryfall_get(url):
        captured_urls.append(url)
        return {"data": [{"name": "Hoarding Broodlord", "set": "mom", "collector_number": "110"}]}

    with patch("app.scryfall._scryfall_get", side_effect=mock_scryfall_get):
        res = find_clean_paper_fallback("Hoarding Broodlord")
        assert res is not None
        assert res["set"] == "mom"
        assert len(captured_urls) > 0
        # Verify query is properly URL-encoded and has no raw quotes
        assert '"' not in captured_urls[0]
        assert "%22" in captured_urls[0] or "%21" in captured_urls[0]


def test_order_confirm_and_submit_success_rendering(client):
    import uuid
    from app.models import Order, Deck
    db_gen = app.dependency_overrides.get(get_db, get_db)
    db = next(db_gen())
    unique_id = uuid.uuid4().hex[:8]
    test_order = Order(
        order_id=f"order_{unique_id}",
        submitter_name="Test Submitter",
        reference_code=f"REF-{unique_id}",
        total_lists=1,
        total_cards=1,
        total_price_cents=100,
        price_per_card_cents=13,
        price_per_list_cents=26,
        order_fee_cents=100,
        status="REVIEW"
    )
    db.add(test_order)
    db.commit()

    diag = [{
        "status": "YELLOW",
        "category": "FOIL_REPLACED",
        "name": "Sol Ring",
        "qty": 1,
        "set": "cmm",
        "cn": "401",
        "message": "Foil replaced with non-foil",
        "original_line": "1 Sol Ring (CMM) 401 *F*",
        "fixed_line": "1 Sol Ring (CMM) 401",
        "original_version_label": "CMM #401 (Foil)",
        "fixed_version_label": "CMM #401 (Non-foil)",
        "original_image_url": "https://cards.scryfall.io/v1.jpg",
        "fixed_image_url": "https://cards.scryfall.io/v2.jpg"
    }]

    test_deck = Deck(
        order_id=test_order.id,
        name="Test Deck",
        card_count=1,
        raw_text="1 Sol Ring (CMM) 401 *F*",
        converted_text="1 Sol Ring (CMM) 401",
        diagnostics_json=json.dumps(diag),
        status="REVIEW"
    )
    db.add(test_deck)
    db.commit()

    # Step 1: POST to /review/{order_id}/resolve (Confirm and submit)
    resolve_resp = client.post(
        f"/review/{test_order.order_id}/resolve",
        data={"ui_lang": "en"},
        follow_redirects=False
    )
    assert resolve_resp.status_code == 303
    assert f"/success/{test_order.order_id}" in resolve_resp.headers["location"]

    # Step 2: GET /success/{order_id}
    success_resp = client.get(resolve_resp.headers["location"])
    assert success_resp.status_code == 200
    assert "Order submitted, please download your overview including payment information" in success_resp.text
    assert "HERE" in success_resp.text
    assert f"/order/{test_order.order_id}/download" in success_resp.text
    assert "DOWNLOAD ORDER OVERVIEW" in success_resp.text
    assert "copyRefCode" in success_resp.text
    assert "COPY REFERENCE CODE" in success_resp.text
    assert 'meta http-equiv="refresh"' not in success_resp.text
    assert test_order.reference_code in success_resp.text
    assert "DETAILED REVISIONS LOG" in success_resp.text
    assert "Sol Ring" in success_resp.text
    assert "FOIL REPLACED" in success_resp.text
    assert "data-v1-img=\"https://cards.scryfall.io/v1.jpg\"" in success_resp.text
    assert "data-v2-img=\"https://cards.scryfall.io/v2.jpg\"" in success_resp.text

    # Step 3: GET /order/{order_id}/download
    download_resp = client.get(f"/order/{test_order.order_id}/download")
    assert download_resp.status_code == 200
    assert "text/plain" in download_resp.headers["content-type"]
    assert f'order_overview_{test_order.reference_code}.txt' in download_resp.headers["content-disposition"]
    text_content = download_resp.text
    assert test_order.submitter_name in text_content
    assert test_order.reference_code in text_content
    assert "PAYMENT INFORMATION" in text_content
    assert "COST BREAKDOWN" in text_content
    assert "REGISTERED DECKLISTS & CARDS OVERVIEW" in text_content
    assert "Test Deck" in text_content
    assert "1 Sol Ring (CMM) 401" in text_content


def test_moxfield_include_stickers():
    from app.moxfield import extract_deck_cards

    mock_deck = {
        "name": "Stickers Test",
        "boards": {
            "mainboard": {
                "cards": {
                    "m1": {"quantity": 1, "card": {"name": "Ticketomaton", "set": "unf", "cn": "189"}}
                }
            },
            "stickers": {
                "cards": {
                    "s1": {"quantity": 1, "card": {"name": "Ancestral Hot Dog Minotaur", "set": "sunf", "cn": "1"}}
                }
            }
        },
        "stickers": [
            {"quantity": 1, "card": {"name": "Carnival Elephant Meteor", "set": "sunf", "cn": "2"}}
        ]
    }

    # Without stickers
    _, without_stk = extract_deck_cards(mock_deck, include_stickers=False)
    names_without = [c["name"] for c in without_stk]
    assert "Ticketomaton" in names_without
    assert "Ancestral Hot Dog Minotaur" not in names_without
    assert "Carnival Elephant Meteor" not in names_without

    # With stickers
    _, with_stk = extract_deck_cards(mock_deck, include_stickers=True)
    names_with = [c["name"] for c in with_stk]
    assert "Ticketomaton" in names_with
    assert "Ancestral Hot Dog Minotaur" in names_with
    assert "Carnival Elephant Meteor" in names_with


def test_admin_toggle_paid_and_delete(client):
    from app.models import Order
    from app.database import get_db

    db = next(app.dependency_overrides[get_db]())
    order = Order(
        order_id="test_admin_pay_toggle",
        submitter_name="Bob Admin Test",
        reference_code="REF-BOB123",
        status="SUBMITTED",
        total_cards=60,
        total_lists=1,
        total_price_cents=806,
        price_per_card_cents=13,
        price_per_list_cents=26,
        order_fee_cents=0,
    )
    db.add(order)
    db.commit()

    # Set admin session cookie
    client.cookies.set("admin_session", settings.admin_password)

    # 1. Initial dashboard view: Order is visible and marked [UNPAID]
    dash_init = client.get("/admin?lang=en")
    assert dash_init.status_code == 200
    assert "Bob Admin Test" in dash_init.text
    assert "REF-BOB123" in dash_init.text
    assert "[UNPAID]" in dash_init.text
    assert "[✓ MARK AS PAID]" in dash_init.text

    # 2. Toggle paid: POST to /admin/order/{order_id}/toggle-paid
    toggle_resp = client.post(
        f"/admin/order/{order.order_id}/toggle-paid",
        data={"lang": "en"},
        follow_redirects=False
    )
    assert toggle_resp.status_code == 303
    assert "/admin?lang=en" in toggle_resp.headers["location"]

    # 3. Follow redirect: Order MUST NOT disappear! It must switch to [PAID]
    dash_after_paid = client.get(toggle_resp.headers["location"])
    assert dash_after_paid.status_code == 200
    assert "Bob Admin Test" in dash_after_paid.text
    assert "REF-BOB123" in dash_after_paid.text
    assert "[PAID]" in dash_after_paid.text
    assert "[MARK UNPAID]" in dash_after_paid.text

    # 4. Verify DB status is updated to PAID
    db.refresh(order)
    assert order.status == "PAID"

    # 5. Toggle again: Switches back to SUBMITTED
    toggle_back = client.post(
        f"/admin/order/{order.order_id}/toggle-paid",
        data={"lang": "en"},
        follow_redirects=False
    )
    assert toggle_back.status_code == 303
    db.refresh(order)
    assert order.status == "SUBMITTED"

    dash_unpaid = client.get("/admin?lang=en")
    assert "[UNPAID]" in dash_unpaid.text

    # 6. Delete order: Redirects properly to /admin and deletes order
    delete_resp = client.post(
        f"/admin/order/{order.order_id}/delete",
        data={"lang": "en"},
        follow_redirects=False
    )
    assert delete_resp.status_code == 303
    assert "/admin?lang=en" in delete_resp.headers["location"]

    dash_after_del = client.get("/admin?lang=en")
    assert "REF-BOB123" not in dash_after_del.text


def test_get_card_versions_mocked():
    from unittest.mock import patch
    from app.scryfall import get_card_versions

    mock_prints_data = {
        "object": "list",
        "data": [
            {
                "set": "pmom",
                "collector_number": "110p",
                "set_name": "March of the Machine Promos",
                "released_at": "2023-04-21",
                "image_uris": {"normal": "https://cards.scryfall.io/pmom110p.jpg"},
                "finishes": ["nonfoil", "foil"],
                "promo_types": ["promopack", "stamped"],
                "digital": False,
                "rarity": "rare",
            },
            {
                "set": "mom",
                "collector_number": "360",
                "set_name": "March of the Machine",
                "released_at": "2023-04-21",
                "image_uris": {"normal": "https://cards.scryfall.io/mom360.jpg"},
                "finishes": ["nonfoil", "foil"],
                "promo_types": ["boosterfun"],
                "digital": False,
                "rarity": "rare",
            },
            {
                "set": "mom",
                "collector_number": "110",
                "set_name": "March of the Machine",
                "released_at": "2023-04-21",
                "image_uris": {"normal": "https://cards.scryfall.io/mom110.jpg"},
                "finishes": ["nonfoil", "foil"],
                "promo_types": [],
                "digital": False,
                "rarity": "rare",
            }
        ]
    }

    with patch("app.scryfall.get_card_by_name", return_value={"prints_search_uri": "https://api.scryfall.com/cards/search?prints"}), \
         patch("app.scryfall._scryfall_get", return_value=mock_prints_data):
        versions = get_card_versions("Hoarding Broodlord")
        assert len(versions) == 3
        assert versions[0]["set"] == "pmom"
        assert versions[0]["collector_number"] == "110p"
        assert versions[0]["has_planeswalker_stamp"] is True
        assert versions[1]["set"] == "mom"
        assert versions[1]["collector_number"] == "360"
        assert versions[1]["has_planeswalker_stamp"] is False
        assert versions[1]["is_digital"] is False
        assert versions[2]["set"] == "mom"
        assert versions[2]["collector_number"] == "110"


def test_switch_card_version_api_and_review_flow(client):
    import json
    import uuid
    from app.models import Order, Deck

    db_gen = app.dependency_overrides.get(get_db, get_db)
    db = next(db_gen())

    unique = uuid.uuid4().hex[:8]
    order_id = f"ord_switch_{unique}"

    diag_item = {
        "qty": 1,
        "name": "Hoarding Broodlord",
        "set": "pmom",
        "cn": "110p",
        "status": "YELLOW",
        "category": "STAMP_REPLACED",
        "message": "Planeswalker-stamped promo replaced with clean printing (MOM #110).",
        "original_line": "1 Hoarding Broodlord (PMOM) 110p",
        "fixed_line": "1 Hoarding Broodlord (MOM) 110",
        "suggested_set": "mom",
        "suggested_cn": "110",
        "original_version_label": "PMOM #110p",
        "fixed_version_label": "MOM #110",
        "original_image_url": "https://cards.scryfall.io/pmom110p.jpg",
        "fixed_image_url": "https://cards.scryfall.io/mom110.jpg"
    }

    deck = Deck(
        name="Switch Version Test Deck",
        ordinal=1,
        preferred_language="en",
        card_count=1,
        status="YELLOW",
        raw_text="1 Hoarding Broodlord (PMOM) 110p",
        converted_text="// Deck: Switch Version Test Deck\n// Language: en\n1 Hoarding Broodlord (MOM) 110\n",
        diagnostics_json=json.dumps([diag_item])
    )

    order = Order(
        order_id=order_id,
        submitter_name="Card Switcher User",
        reference_code=f"REF-SW-{unique}",
        status="PENDING_REVIEW",
        total_lists=1,
        total_cards=1,
        total_price_cents=39,
        price_per_card_cents=13,
        price_per_list_cents=26,
        order_fee_cents=0,
        decks=[deck]
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    db.refresh(deck)

    # 1. Verify GET /review/{order_id} shows switch version button and modal
    review_resp = client.get(f"/review/{order_id}?lang=en")
    assert review_resp.status_code == 200
    assert "CARD REPLACEMENTS LOG" in review_resp.text
    assert "Switch Version" in review_resp.text
    assert "version-picker-modal" in review_resp.text
    assert f"openVersionPicker({deck.id}, 0" in review_resp.text

    # 2. Test GET /api/card-versions
    from unittest.mock import patch
    mock_versions = [
        {"set": "mom", "collector_number": "360", "version_label": "MOM #360", "image_url": "https://cards.scryfall.io/mom360.jpg"}
    ]
    with patch("app.routes.user.get_card_versions", return_value=mock_versions):
        versions_resp = client.get("/api/card-versions?name=Hoarding+Broodlord")
        assert versions_resp.status_code == 200
        data = versions_resp.json()
        assert data["success"] is True
        assert len(data["versions"]) == 1
        assert data["versions"][0]["collector_number"] == "360"

    # 3. Test POST /api/deck/{deck_id}/switch-version
    switch_resp = client.post(
        f"/api/deck/{deck.id}/switch-version",
        json={
            "item_idx": 0,
            "set": "mom",
            "cn": "360",
            "image_url": "https://cards.scryfall.io/mom360.jpg",
            "version_label": "MOM #360 (Boosterfun)"
        }
    )
    assert switch_resp.status_code == 200
    switch_data = switch_resp.json()
    assert switch_data["success"] is True
    assert switch_data["fixed_line"] == "1 Hoarding Broodlord (MOM) 360"
    assert switch_data["fixed_version_label"] == "MOM #360 (Boosterfun)"

    # Verify database was updated
    db.refresh(deck)
    db_diags = json.loads(deck.diagnostics_json)
    assert db_diags[0]["set"] == "mom"
    assert db_diags[0]["cn"] == "360"
    assert db_diags[0]["fixed_line"] == "1 Hoarding Broodlord (MOM) 360"
    assert "1 Hoarding Broodlord (MOM) 360" in deck.converted_text

    # 4. Resolve / Submit the order and verify the chosen replacement is preserved!
    resolve_resp = client.post(
        f"/review/{order_id}/resolve",
        data={"ui_lang": "en"},
        follow_redirects=False
    )
    assert resolve_resp.status_code == 303
    assert f"/success/{order_id}" in resolve_resp.headers["location"]

    db.refresh(order)
    db.refresh(deck)
    assert order.status == "SUBMITTED"
    assert "1 Hoarding Broodlord (MOM) 360" in deck.converted_text


def test_same_set_nonfoil_priority():
    from unittest.mock import patch
    from app.scryfall import find_clean_paper_fallback

    # Mock _scryfall_get to verify queries
    queries_made = []

    def mock_scryfall_get(url):
        queries_made.append(url)
        if "s%3Acmm" in url or "s:cmm" in url:
            return {
                "data": [
                    {"set": "cmm", "collector_number": "100", "name": "Sol Ring", "finishes": ["nonfoil"]}
                ]
            }
        return {"data": [{"set": "lea", "collector_number": "269", "name": "Sol Ring", "finishes": ["nonfoil"]}]}

    with patch("app.scryfall._scryfall_get", side_effect=mock_scryfall_get):
        res = find_clean_paper_fallback("Sol Ring", prefer_nonfoil=True, preferred_set="CMM")
        assert res is not None
        assert res["set"] == "cmm"
        assert "s%3Acmm" in queries_made[0] or "s:cmm" in queries_made[0]


def test_unchecked_options_not_replaced(client):
    from unittest.mock import patch

    mock_foil_sol_ring = {
        "name": "Sol Ring",
        "set": "cmm",
        "collector_number": "401",
        "finishes": ["foil"],
        "games": ["paper"],
        "digital": False,
        "promo_types": []
    }

    with patch("app.converter.get_card_by_set_cn", return_value=mock_foil_sol_ring):
        # 1. Submit with convert_foils UNCHECKED (omitted from form)
        resp_unchecked = client.post(
            "/submit",
            data={
                "submitter_name": "NoFoilConversion",
                "ui_lang": "en",
                "deck_count": "1",
                "deck_name_0": "Keep Foil Deck",
                "source_type_0": "text",
                "raw_text_0": "1 Sol Ring (CMM) 401",
                # convert_foils_0 is omitted!
                # replace_stamps_0 is omitted!
            },
            follow_redirects=False
        )
        assert resp_unchecked.status_code == 303
        order_id = resp_unchecked.headers["location"].split("/")[-1].split("?")[0]
        
        from app.models import Order
        db_gen = app.dependency_overrides.get(get_db, get_db)
        db = next(db_gen())
        order = db.query(Order).filter(Order.order_id == order_id).first()
        assert order is not None
        deck = order.decks[0]
        assert deck.convert_foils is False
        assert deck.replace_stamps is False
        assert "1 Sol Ring (CMM) 401" in deck.converted_text
        diags = json.loads(deck.diagnostics_json)
        assert diags[0]["status"] == "GREEN"
        assert diags[0]["category"] == "CLEAN"

        # 2. Submit with convert_foils CHECKED
        resp_checked = client.post(
            "/submit",
            data={
                "submitter_name": "DoFoilConversion",
                "ui_lang": "en",
                "deck_count": "1",
                "deck_name_0": "Convert Foil Deck",
                "source_type_0": "text",
                "raw_text_0": "1 Sol Ring (CMM) 401",
                "convert_foils_0": "on",
            },
            follow_redirects=False
        )
        assert resp_checked.status_code == 303
        order_id_2 = resp_checked.headers["location"].split("/")[-1].split("?")[0]
        order_2 = db.query(Order).filter(Order.order_id == order_id_2).first()
        deck_2 = order_2.decks[0]
        assert deck_2.convert_foils is True

