import os
import tempfile
import pytest
from proxy_parser.checker.file_validator import (
    analyze_filename,
    discover_and_validate_files,
    FileMetadata
)
from proxy_parser.checker.content_validator import (
    parse_deck_content,
    is_section_header,
    ParsedCardLine,
)
from proxy_parser.checker.autofix import (
    evaluate_traffic_light,
    apply_auto_fix_for_file,
    export_ready_deck,
    FileAuditResult
)
from proxy_parser.checker.reporter import (
    generate_markdown_report,
    generate_csv_reports
)
from proxy_parser.checker.scryfall_validator import CardDiagnostic, ScryfallBatchValidator

def test_analyze_filename_canonical():
    is_valid, person, list_id, count, issues, suggested = analyze_filename("DominikB1_100.txt")
    assert is_valid is True
    assert person == "DominikB"
    assert list_id == "1_100"
    assert count == 100
    assert len(issues) == 0
    assert suggested == "DominikB1_100.txt"

def test_analyze_filename_all_fine():
    # User rule: basically every filename is fine, e.g. "Mischa (1-Azula) (100).txt"
    is_valid, person, list_id, count, issues, suggested = analyze_filename("Mischa (1-Azula) (100).txt")
    assert is_valid is True
    assert person == "Mischa"
    assert list_id == "(1-Azula) (100)"
    assert count == 100
    assert len(issues) == 0
    assert suggested == "Mischa (1-Azula) (100).txt"

def test_multiple_lists_per_person_require_name():
    with tempfile.TemporaryDirectory() as tmpdir:
        # Person "Alice" has 2 lists, but one lacks any list identifier
        f1 = os.path.join(tmpdir, "Alice.txt")
        f2 = os.path.join(tmpdir, "Alice_Deck2.txt")
        with open(f1, "w") as fp:
            fp.write("1 Sol Ring\n")
        with open(f2, "w") as fp:
            fp.write("1 Sol Ring\n")

        results = discover_and_validate_files(tmpdir)
        alice_files = [f for f in results if f.person == "Alice"]
        assert len(alice_files) == 2
        # The file with no list identifier should be flagged
        has_multi_list_issue = any(
            any("multiple decklists" in iss for iss in f.filename_issues)
            for f in alice_files
        )
        assert has_multi_list_issue is True

def test_gdoc_shortcut_detection():
    with tempfile.TemporaryDirectory() as tmpdir:
        gdoc_file = os.path.join(tmpdir, "Baris_300.txt.gdoc")
        with open(gdoc_file, "w") as fp:
            fp.write('{"doc_id": "12345"}')

        results = discover_and_validate_files(tmpdir)
        assert len(results) == 1
        res = results[0]
        assert res.file_type == "GDOC_SHORTCUT"
        assert res.is_valid_extension is False

        # Traffic light must be RED
        audit = evaluate_traffic_light(res, None, [])
        assert audit.traffic_light == "RED"
        assert audit.can_auto_fix is False
        assert any("Google Doc" in act for act in audit.manual_fix_actions)

def test_section_headers_and_parsing():
    content = """// Commander
1 Atraxa, Praetors' Voice (2XM) 198

# Mainboard
1 Sol Ring (C20) 247
1 Arcane Signet
1 Sol Ring (Duplicate test)
"""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tf:
        tf.write(content)
        tf_path = tf.name

    try:
        report = parse_deck_content(tf_path, stated_count=4)
        assert report.total_cards_count == 4
        assert len(report.section_headers) >= 2
        assert len(report.duplicate_cards) >= 1
        assert report.missing_set_codes_count >= 1
    finally:
        os.remove(tf_path)

def test_traffic_light_yellow_autofix():
    meta = FileMetadata(
        file_path="/dummy/Mischa (1-Azula) (100).txt",
        file_name="Mischa (1-Azula) (100).txt",
        file_size=500,
        file_type="PLAIN_TEXT",
        is_valid_extension=True,
        is_valid_filename=True,
        person="Mischa",
        list_identifier="(1-Azula) (100)",
        stated_count=100,
        filename_issues=[],
        suggested_filename="Mischa (1-Azula) (100).txt"
    )

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tf:
        tf.write("// COMMANDER\n1 Sol Ring (C20) 247\n1 Command Tower (C20) 264\n")
        tf_path = tf.name

    try:
        report = parse_deck_content(tf_path, stated_count=2)
        audit = evaluate_traffic_light(meta, report, [])
        assert audit.traffic_light == "YELLOW"
        assert audit.can_auto_fix is True
        assert any("section headers" in act.lower() for act in audit.auto_fix_actions)

        with tempfile.TemporaryDirectory() as out_dir:
            fixed_file = apply_auto_fix_for_file(audit, out_dir)
            assert fixed_file is not None
            assert os.path.exists(fixed_file)
            assert os.path.basename(fixed_file) == "Mischa (1-Azula) (100).txt"
    finally:
        os.remove(tf_path)

def test_generate_reports():
    meta = FileMetadata(
        file_path="/dummy/DominikB1_100.txt",
        file_name="DominikB1_100.txt",
        file_size=500,
        file_type="PLAIN_TEXT",
        is_valid_extension=True,
        is_valid_filename=True,
        person="DominikB",
        list_identifier="1",
        stated_count=100,
        filename_issues=[],
        suggested_filename="DominikB1_100.txt"
    )
    audit = FileAuditResult(
        metadata=meta,
        content_report=None,
        card_diagnostics=[
            CardDiagnostic(
                card_name="Test Card",
                line_number=1,
                set_code="LEA",
                collector_number="1",
                severity="YELLOW",
                category="FOIL_ONLY",
                message="Foil only test",
                can_autofix=True,
                suggested_fix="Print nonfoil"
            )
        ],
        traffic_light="YELLOW",
        status_summary="CAN BE AUTO-FIXED (1 warning)",
        can_auto_fix=True,
        auto_fix_actions=["Auto-fix action"],
        manual_fix_actions=[]
    )

    with tempfile.TemporaryDirectory() as out_dir:
        md_path = os.path.join(out_dir, "report.md")
        summary_csv = os.path.join(out_dir, "summary.csv")
        detail_csv = os.path.join(out_dir, "detail.csv")

        generate_markdown_report([audit], md_path)
        generate_csv_reports([audit], summary_csv, detail_csv)

        assert os.path.exists(md_path)
        assert os.path.exists(summary_csv)
        assert os.path.exists(detail_csv)

def test_export_ready_deck():
    with tempfile.TemporaryDirectory() as temp_dir:
        green_path = os.path.join(temp_dir, "Fabian1_heybear.txt")
        with open(green_path, "w") as f:
            f.write("1 Sol Ring (C20) 247\n1 Command Tower (C20) 264\n")

        yellow_path = os.path.join(temp_dir, "Chris1_stella.txt")
        with open(yellow_path, "w") as f:
            f.write("// COMMANDER\n1 Sol Ring (C20) 247\nSIDEBOARD:\n1 Command Tower (C20) 264\n")

        meta_green = FileMetadata(
            file_path=green_path,
            file_name="Fabian1_heybear.txt",
            file_size=50,
            file_type="PLAIN_TEXT",
            is_valid_extension=True,
            is_valid_filename=True,
            person="Fabian",
            list_identifier="1_heybear",
            stated_count=2,
            filename_issues=[],
            suggested_filename="Fabian1_heybear.txt"
        )
        content_green = parse_deck_content(green_path, stated_count=2)
        audit_green = evaluate_traffic_light(meta_green, content_green, [])

        meta_yellow = FileMetadata(
            file_path=yellow_path,
            file_name="Chris1_stella.txt",
            file_size=80,
            file_type="PLAIN_TEXT",
            is_valid_extension=True,
            is_valid_filename=True,
            person="Chris",
            list_identifier="1_stella",
            stated_count=2,
            filename_issues=[],
            suggested_filename="Chris1_stella.txt"
        )
        content_yellow = parse_deck_content(yellow_path, stated_count=2)
        audit_yellow = evaluate_traffic_light(meta_yellow, content_yellow, [])

        audit_red = FileAuditResult(
            metadata=meta_green,
            content_report=None,
            card_diagnostics=[],
            traffic_light="RED",
            status_summary="NEEDS USER FIXING",
            can_auto_fix=False
        )

        ready_dir = os.path.join(temp_dir, "ready")

        # Green deck exported directly
        green_out = export_ready_deck(audit_green, ready_dir)
        assert green_out is not None
        assert os.path.exists(green_out)
        assert os.path.basename(green_out) == "Fabian1_heybear.txt"

        # Yellow deck auto-fixed and exported
        yellow_out = export_ready_deck(audit_yellow, ready_dir)
        assert yellow_out is not None
        assert os.path.exists(yellow_out)
        assert os.path.basename(yellow_out) == "Chris1_stella.txt"
        with open(yellow_out) as yf:
            lines = yf.read()
            assert "SIDEBOARD:" not in lines
            assert "// COMMANDER" not in lines

        # Red deck rejected
        red_out = export_ready_deck(audit_red, ready_dir)
        assert red_out is None


def test_custom_double_sided_line_survives_content_parsing():
    """'&&&' lines parse as one composite card, not a garbled name."""
    content = "1 Dion, Bahamut's Dominant &&& Bahamut, Warden of Light (FIN) 16\n1 Sol Ring (LEA) 1\n"
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tf:
        tf.write(content)
        tf_path = tf.name

    try:
        report = parse_deck_content(tf_path, stated_count=2)
        assert report.total_cards_count == 2
        assert len(report.unparseable_lines) == 0
        composite = report.cards[0]
        assert composite.is_custom_double_sided
        assert composite.name == "Dion, Bahamut's Dominant"
        assert composite.back_name == "Bahamut, Warden of Light"
        assert composite.back_set_code == "fin"
        assert composite.back_collector_number == "16"
    finally:
        os.remove(tf_path)


def test_custom_double_sided_line_exported_intact():
    """The '&&&' pairing must round-trip through auto-fix/export, not just parsing.

    The front side here has no set code (expected -- the submitter only knows
    the back's exact printing), which makes the file YELLOW and routes it
    through the auto-fix renderer. If that renderer ever forgot the back half,
    this would silently turn a double-sided proxy into a single-faced one.
    """
    with tempfile.TemporaryDirectory() as temp_dir:
        path = os.path.join(temp_dir, "Chefski2_100.txt")
        with open(path, "w") as f:
            f.write(
                "1 Dion, Bahamut's Dominant &&& Bahamut, Warden of Light (FIN) 16\n"
                "1 Sol Ring (LEA) 1\n"
            )

        meta = FileMetadata(
            file_path=path,
            file_name="Chefski2_100.txt",
            file_size=os.path.getsize(path),
            file_type="PLAIN_TEXT",
            is_valid_extension=True,
            is_valid_filename=True,
            person="Chefski",
            list_identifier="2_100",
            stated_count=2,
            filename_issues=[],
            suggested_filename="Chefski2_100.txt",
        )
        content = parse_deck_content(path, stated_count=2)
        audit = evaluate_traffic_light(meta, content, [])
        assert audit.traffic_light == "YELLOW"
        assert audit.can_auto_fix is True

        ready_dir = os.path.join(temp_dir, "ready")
        out_path = export_ready_deck(audit, ready_dir)
        assert out_path is not None
        with open(out_path) as f:
            exported = f.read()
        assert "&&&" in exported
        assert "Bahamut, Warden of Light" in exported


def test_custom_double_sided_back_not_found_blocks_as_red():
    """A typo'd back card must block the file, not silently vanish."""
    front = ParsedCardLine(
        line_number=1,
        raw_line="1 Sol Ring &&& Not A Real Card Name Xyzzy",
        qty=1,
        name="Sol Ring",
        back_name="Not A Real Card Name Xyzzy",
    )
    back_query = ScryfallBatchValidator._back_query(front)

    diagnostics: list[CardDiagnostic] = []

    class NeverFindsAFallback(ScryfallBatchValidator):
        def _find_nonfoil_fallback(self, card_name):
            return None

    validator = NeverFindsAFallback(cache_dir=tempfile.mkdtemp())
    validator._handle_not_found(back_query, diagnostics, front_ref=front)

    assert len(diagnostics) == 1
    assert diagnostics[0].severity == "RED"
    assert diagnostics[0].category == "NOT_FOUND"

    meta = FileMetadata(
        file_path="/dummy/Chefski2_100.txt",
        file_name="Chefski2_100.txt",
        file_size=10,
        file_type="PLAIN_TEXT",
        is_valid_extension=True,
        is_valid_filename=True,
        person="Chefski",
        list_identifier="2_100",
        stated_count=1,
        filename_issues=[],
        suggested_filename="Chefski2_100.txt",
    )
    audit = evaluate_traffic_light(meta, None, diagnostics)
    assert audit.traffic_light == "RED"
    assert audit.can_auto_fix is False

