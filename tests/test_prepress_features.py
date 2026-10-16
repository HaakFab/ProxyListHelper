import os
import tempfile
import pytest
from PIL import Image

from proxy_parser.config import AppConfig
from proxy_parser.divider import generate_divider_cards, generate_divider_card
from proxy_parser.grouping import optimize_suborders
from proxy_parser.deck_parser import Deck, CardEntry

def test_generate_two_divider_cards():
    with tempfile.TemporaryDirectory() as tmpdir:
        config = AppConfig()
        cards = generate_divider_cards(
            label="Commander Urza",
            person="Alice",
            card_count=100,
            cache_dir=tmpdir,
            config=config,
            dpi=300
        )
        assert len(cards) == 2, "Should return exactly two divider card pairs"
        
        # Verify card 1 (Identity) files exist and have valid dimensions
        front1, back1 = cards[0]
        assert os.path.exists(front1)
        assert os.path.exists(back1)
        with Image.open(front1) as im:
            assert im.width > 500 and im.height > 700

        # Verify card 2 (Details) files exist
        front2, back2 = cards[1]
        assert os.path.exists(front2)
        assert os.path.exists(back2)
        with Image.open(front2) as im:
            assert im.width > 500 and im.height > 700

def test_suborder_grouping_no_deck_split():
    # Create mock decks for multiple persons
    # Alice: Deck 1 (100 cards), Deck 2 (100 cards) = 200
    # Bob: Deck 3 (100 cards) = 100
    # Charlie: Deck 4 (150 cards) = 150
    alice_deck1 = Deck(doc_id="a1", person="Alice", label="Alice Deck 1", file_path="a1.txt", cards=[CardEntry(name="Card", qty=100)])
    alice_deck2 = Deck(doc_id="a2", person="Alice", label="Alice Deck 2", file_path="a2.txt", cards=[CardEntry(name="Card", qty=100)])
    bob_deck = Deck(doc_id="b1", person="Bob", label="Bob Deck 1", file_path="b1.txt", cards=[CardEntry(name="Card", qty=100)])
    charlie_deck = Deck(doc_id="c1", person="Charlie", label="Charlie Deck 1", file_path="c1.txt", cards=[CardEntry(name="Card", qty=150)])

    deck_items = [
        {"deck": alice_deck1, "card_pairs": [("f", "b")] * 100, "dividers": [("d1", "d1"), ("d2", "d2")]},
        {"deck": alice_deck2, "card_pairs": [("f", "b")] * 100, "dividers": [("d1", "d1"), ("d2", "d2")]},
        {"deck": bob_deck, "card_pairs": [("f", "b")] * 100, "dividers": [("d1", "d1"), ("d2", "d2")]},
        {"deck": charlie_deck, "card_pairs": [("f", "b")] * 150, "dividers": [("d1", "d1"), ("d2", "d2")]},
    ]

    # Target small suborder of 300 cards per suborder (with 18 cards per sheet)
    batches = optimize_suborders(
        deck_items=deck_items,
        cards_per_sheet=18,
        target_suborder_cards=300
    )

    assert len(batches) >= 1
    # Check that each deck belongs to exactly one batch
    all_assigned_labels = []
    for b in batches:
        for item in b.deck_items:
            all_assigned_labels.append(item["deck"].label)
        assert b.sheets_required > 0
        assert b.waste_slots >= 0
        assert b.waste_slots < 18
        # Formula: (sheets * 18) - total == waste
        assert (b.sheets_required * 18) - b.total_cards == b.waste_slots

    assert sorted(all_assigned_labels) == sorted(["Alice Deck 1", "Alice Deck 2", "Bob Deck 1", "Charlie Deck 1"])

    # Check Alice's decks stay together
    alice_batches = set()
    for b in batches:
        for item in b.deck_items:
            if item["deck"].person == "Alice":
                alice_batches.add(b.batch_index)
    assert len(alice_batches) == 1, "Alice's decks should not be separated into different suborders"

def test_preferred_language_header_variants():
    from proxy_parser.deck_parser import parse_moxfield_file
    with tempfile.TemporaryDirectory() as tmpdir:
        # Test 1: // Preferred Language: de
        p1 = os.path.join(tmpdir, "Fabian1_Test.txt")
        with open(p1, "w", encoding="utf-8") as f:
            f.write("// Preferred Language: de\n1 Sol Ring\n1 Command Tower\n")
        d1 = parse_moxfield_file(p1)
        assert d1.preferred_language == "de"
        assert all(c.lang == "de" for c in d1.cards)

        # Test 2: # Language: ja
        p2 = os.path.join(tmpdir, "Bob_Japan.txt")
        with open(p2, "w", encoding="utf-8") as f:
            f.write("# Language: ja\n1 Forest\n")
        d2 = parse_moxfield_file(p2)
        assert d2.preferred_language == "ja"
        assert d2.cards[0].lang == "ja"

        # Test 3: // Lang: fr
        p3 = os.path.join(tmpdir, "Claire_French.txt")
        with open(p3, "w", encoding="utf-8") as f:
            f.write("// Lang: fr\n1 Island\n")
        d3 = parse_moxfield_file(p3)
        assert d3.preferred_language == "fr"
        assert d3.cards[0].lang == "fr"

def test_process_custom_cardback_aspect_ratios_and_orientation():
    from proxy_parser.bleed import process_custom_cardback
    config = AppConfig()

    with tempfile.TemporaryDirectory() as tmpdir:
        # Case A: Square image (500x500) -> should crop to 63:88, scale to target 300 DPI, and add 3mm bleed
        sq_path = os.path.join(tmpdir, "square.png")
        Image.new("RGB", (500, 500), (20, 40, 80)).save(sq_path)

        out_sq = os.path.join(tmpdir, "out_sq.jpg")
        process_custom_cardback(sq_path, out_sq, config, dpi=300)
        assert os.path.exists(out_sq)
        with Image.open(out_sq) as im:
            # Expected full-bleed @ 300 DPI: round(69 / 25.4 * 300) = 815, round(94 / 25.4 * 300) = 1110
            assert im.width == 815
            assert im.height == 1110

        # Case B: Landscape image (1920x1080) -> should auto-rotate to portrait 1080x1920, crop, scale, and bleed
        land_path = os.path.join(tmpdir, "landscape.png")
        Image.new("RGB", (1920, 1080), (120, 30, 30)).save(land_path)

        out_land = os.path.join(tmpdir, "out_land.jpg")
        process_custom_cardback(land_path, out_land, config, dpi=300)
        assert os.path.exists(out_land)
        with Image.open(out_land) as im:
            assert im.width == 815
            assert im.height == 1110

        # Case C: Already-bled MPC image (822x1122, aspect ratio ~0.7326) -> recognized as already bled and sized
        bled_path = os.path.join(tmpdir, "already_bled.png")
        Image.new("RGB", (822, 1122), (10, 80, 20)).save(bled_path)

        out_bled = os.path.join(tmpdir, "out_bled.jpg")
        process_custom_cardback(bled_path, out_bled, config, dpi=300)
        assert os.path.exists(out_bled)
        with Image.open(out_bled) as im:
            assert im.width == 815
            assert im.height == 1110

