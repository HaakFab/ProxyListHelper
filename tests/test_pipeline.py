import os
import shutil
import unittest
import numpy as np
from PIL import Image

from proxy_parser.config import AppConfig
from proxy_parser.deck_parser import parse_filename, parse_moxfield_file
from proxy_parser.bleed import calculate_target_dimensions, add_intelligent_bleed, is_card_borderless
from proxy_parser.formatter import CardFrame, CardType, redact_copyright, is_universes_beyond
from proxy_parser.gdrive import GDriveManager, find_default_macos_gdrive, copy_file_with_progress
from proxy_parser.pdf_builder import PDFBatchBuilder, render_pages_to_pdf
from proxy_parser.upscaler import get_torch_device
from moxfield_mpc_to_pdf import (
    resolve_requested_face_image,
    reuse_legacy_png_bleed_as_jpeg,
    report_superseded_png_bleed_cache,
)

class TestMTGProxyParser(unittest.TestCase):

    def test_filename_parsing(self):
        label, person, doc_id = parse_filename("Fabian2_100.txt")
        self.assertEqual(label, "Fabian2_100")
        self.assertEqual(person, "Fabian")
        self.assertEqual(doc_id, "Fabian2_100")

        label, person, doc_id = parse_filename("Max_60.txt")
        self.assertEqual(label, "Max_60")
        self.assertEqual(person, "Max")
        self.assertEqual(doc_id, "Max_60")

        label, person, doc_id = parse_filename("Mischa (7-Thranduil)(100).txt")
        self.assertEqual(label, "Mischa (7-Thranduil)(100)")
        self.assertEqual(person, "Mischa")
        self.assertEqual(doc_id, "Mischa (7-Thranduil)(100)")

    def test_moxfield_file_parsing(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".txt", prefix="Fabian1_6_", delete=False) as tf:
            tf.write("1 Sol Ring (C20) 247\n1 Command Tower (C20) 264\n4 Forest (MH3) 308\n")
            tf_name = tf.name
        try:
            deck = parse_moxfield_file(tf_name)
            self.assertEqual(deck.person, "Fabian")
            self.assertEqual(deck.total_cards, 6)
            self.assertTrue(len(deck.cards) > 0)
            self.assertIsNotNone(deck.cards[0].key)
        finally:
            if os.path.exists(tf_name):
                os.remove(tf_name)

    def test_custom_double_sided_line(self):
        """'&&&' pairs two unrelated printings front-to-back on one physical card."""
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".txt", prefix="Chefski2_100_", delete=False) as tf:
            tf.write(
                "1 Dion, Bahamut's Dominant &&& Bahamut, Warden of Light (FIN) 16\n"
                "1 Sol Ring (LEA) 1\n"
            )
            tf_name = tf.name
        try:
            deck = parse_moxfield_file(tf_name)
            self.assertEqual(deck.total_cards, 2)
            composite = deck.cards[0]
            self.assertTrue(composite.is_custom_double_sided)
            self.assertEqual(composite.name, "Dion, Bahamut's Dominant")
            back = composite.back_entry
            self.assertEqual(back.name, "Bahamut, Warden of Light")
            self.assertEqual(back.set, "fin")
            self.assertEqual(back.cn, "16")
            # The back is looked up as its own independent card, keyed off its
            # own printing -- not derived from the front's key.
            self.assertEqual(back.key, "fin_16")
            self.assertNotEqual(back.key, composite.key)

            normal = deck.cards[1]
            self.assertFalse(normal.is_custom_double_sided)
            self.assertIsNone(normal.back_entry)
        finally:
            if os.path.exists(tf_name):
                os.remove(tf_name)

    def test_resolve_requested_face_image(self):
        card_assets = {
            "sol_ring": ("sol_ring_front.png", "backs/proxy1.jpg"),
            "znr_90": ("agadeem_front.png", "agadeem_back.png"),
            "lightning_bolt": ("bolt_front.png", "backs/proxy1.jpg"),
        }
        metadata_map = {
            "sol_ring": {"name": "Sol Ring"},
            "znr_90": {
                "card_faces": [
                    {"name": "Agadeem's Awakening"},
                    {"name": "Agadeem, the Undercrypt"},
                ]
            },
            "lightning_bolt": {"name": "Lightning Bolt"},
        }

        # Single-faced card: the front-bleed image, regardless of name casing.
        self.assertEqual(
            resolve_requested_face_image("sol_ring", "Sol Ring", card_assets, metadata_map),
            "sol_ring_front.png",
        )

        # A real MDFC's own front face name -> its front-bleed image.
        self.assertEqual(
            resolve_requested_face_image("znr_90", "Agadeem's Awakening", card_assets, metadata_map),
            "agadeem_front.png",
        )

        # The SAME card's back face name, requested explicitly (as a custom
        # '&&&' pairing would) -> its own back-bleed image, not the front's.
        self.assertEqual(
            resolve_requested_face_image("znr_90", "Agadeem, the Undercrypt", card_assets, metadata_map),
            "agadeem_back.png",
        )

        # A genuinely unrelated custom back: its own front-bleed image.
        self.assertEqual(
            resolve_requested_face_image("lightning_bolt", "Lightning Bolt", card_assets, metadata_map),
            "bolt_front.png",
        )

        # Missing/unfetched key -> None, never a crash.
        self.assertIsNone(resolve_requested_face_image("missing_key", "Nothing", card_assets, metadata_map))

    def test_render_pages_to_pdf_reports_progress_per_card(self):
        """The PDF step must report progress per card, not just 0/1 at the end.

        This was the actual bug behind a run that looked "stuck": a single
        combined PDF only ever fired its progress callback once, after the
        entire (possibly multi-thousand-card) document had already finished.
        """
        import tempfile
        tmp = tempfile.mkdtemp()
        try:
            front = os.path.join(tmp, "front.png")
            back = os.path.join(tmp, "back.png")
            Image.new("RGB", (10, 10), "red").save(front)
            Image.new("RGB", (10, 10), "blue").save(back)

            calls = []
            out_pdf = os.path.join(tmp, "out.pdf")
            render_pages_to_pdf(
                [(front, back)] * 5, out_pdf, 200, 280,
                progress_callback=lambda c, t: calls.append((c, t))
            )
            self.assertEqual(calls, [(1, 5), (2, 5), (3, 5), (4, 5), (5, 5)])
            self.assertTrue(os.path.exists(out_pdf))
            self.assertGreater(os.path.getsize(out_pdf), 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_build_pdfs_progress_is_monotonic_and_bounded_across_modes(self):
        """Progress must never exceed its own total or go backwards.

        A previous version undercounted divider cards in the running total
        while still counting them in what render_pages_to_pdf reported,
        which made progress briefly exceed 100% (e.g. 6/5) on multi-file
        splits ('name'/'document'/'count') whenever a deck had a divider.
        """
        import tempfile

        class FakeDeck:
            def __init__(self, person, doc_id):
                self.person = person
                self.doc_id = doc_id

        tmp = tempfile.mkdtemp()
        try:
            front = os.path.join(tmp, "front.png")
            back = os.path.join(tmp, "back.png")
            Image.new("RGB", (10, 10), "red").save(front)
            Image.new("RGB", (10, 10), "blue").save(back)

            for break_by in ("none", "name", "document", "count"):
                deck_items = [
                    {"deck": FakeDeck("Alice", "Alice1"), "divider": (front, back), "card_pairs": [(front, back)] * 3},
                    {"deck": FakeDeck("Bob", "Bob1"), "divider": (front, back), "card_pairs": [(front, back)] * 2},
                ]
                cfg = AppConfig(break_by=break_by, break_count=100, cut_width_mm=63.0, cut_height_mm=88.0, bleed_margin_mm=3.0)
                builder = PDFBatchBuilder(cfg)
                calls = []
                out_dir = os.path.join(tmp, f"outdir_{break_by}")
                os.makedirs(out_dir, exist_ok=True)

                builder.build_pdfs(deck_items, out_dir, progress_callback=lambda c, t: calls.append((c, t)))

                self.assertTrue(calls, f"no progress reported for mode={break_by}")
                totals = {t for _, t in calls}
                self.assertEqual(len(totals), 1, f"total changed mid-run for mode={break_by}: {totals}")
                total = totals.pop()
                progress_values = [c for c, _ in calls]
                self.assertTrue(all(c <= total for c in progress_values), f"overshoot in mode={break_by}: {calls}")
                self.assertTrue(
                    all(a <= b for a, b in zip(progress_values, progress_values[1:])),
                    f"progress went backwards in mode={break_by}: {calls}"
                )
                self.assertEqual(progress_values[-1], total, f"never reached 100% for mode={break_by}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_copy_file_with_progress(self):
        """Chunked Drive-sync copy: reports byte progress and never leaves a partial file."""
        import tempfile
        tmp = tempfile.mkdtemp()
        try:
            src = os.path.join(tmp, "bigfile.bin")
            payload = os.urandom(3 * 1024 * 1024 + 17)  # not a clean multiple of the chunk size
            with open(src, "wb") as f:
                f.write(payload)

            dst = os.path.join(tmp, "dest", "bigfile.bin")
            progress = []
            copy_file_with_progress(
                src, dst,
                progress_callback=lambda c, t: progress.append((c, t)),
                chunk_size=1024 * 1024
            )

            self.assertTrue(os.path.exists(dst))
            self.assertFalse(os.path.exists(dst + ".tmp"))
            with open(dst, "rb") as f:
                self.assertEqual(f.read(), payload)

            self.assertTrue(progress)
            self.assertEqual(progress[-1], (len(payload), len(payload)))
            # Monotonically increasing and never exceeding the true total.
            self.assertTrue(all(a <= b for a, b in zip([c for c, _ in progress], [c for c, _ in progress][1:])))
            self.assertTrue(all(c <= len(payload) for c, _ in progress))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_reuse_legacy_png_bleed_as_jpeg(self):
        """The upscale/bleed fast path: a pre-existing '_bleed.png' converts to
        '.jpg' directly, without redoing bleed computation or re-upscaling."""
        import tempfile
        tmp = tempfile.mkdtemp()
        try:
            legacy_png = os.path.join(tmp, "abc_front_x2_bleed.png")
            Image.new("RGB", (100, 140), (200, 160, 130)).save(legacy_png, "PNG", dpi=(600, 600))
            target_jpg = os.path.join(tmp, "abc_front_x2_bleed.jpg")

            self.assertTrue(reuse_legacy_png_bleed_as_jpeg(target_jpg))
            self.assertTrue(os.path.exists(target_jpg))
            with Image.open(target_jpg) as img:
                self.assertEqual(img.format, "JPEG")
                self.assertEqual(img.size, (100, 140))
                self.assertEqual(tuple(round(v) for v in img.info.get("dpi", ())), (600, 600))

            # No legacy PNG present -> caller must fall back to full regeneration.
            missing_jpg = os.path.join(tmp, "nope_bleed.jpg")
            self.assertFalse(reuse_legacy_png_bleed_as_jpeg(missing_jpg))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_report_superseded_png_bleed_cache_only_flags_converted_pngs(self):
        """A legacy PNG is only 'dead weight' once its .jpg sibling exists.

        Flagging it before that would wrongly invite deleting a file that
        reuse_legacy_png_bleed_as_jpeg still needs as its fast-path source.
        """
        import tempfile
        import moxfield_mpc_to_pdf as pipeline_module

        tmp = tempfile.mkdtemp()
        try:
            cache_dir = os.path.join(tmp, "cache")
            upscaled_dir = os.path.join(tmp, "upcache")
            os.makedirs(cache_dir, exist_ok=True)
            os.makedirs(upscaled_dir, exist_ok=True)

            # Already converted -> superseded, safe to report/delete.
            Image.new("RGB", (10, 10)).save(os.path.join(cache_dir, "converted_bleed.png"))
            Image.new("RGB", (10, 10)).save(os.path.join(cache_dir, "converted_bleed.jpg"), "JPEG")

            # Not converted yet -> still a useful fast-path source, must NOT be flagged.
            Image.new("RGB", (10, 10)).save(os.path.join(upscaled_dir, "pending_bleed.png"))

            cfg = AppConfig(cache_dir=cache_dir, upscaled_cache_dir=upscaled_dir)

            captured = []
            original_print = pipeline_module.console.print
            pipeline_module.console.print = lambda *a, **k: captured.append(" ".join(str(x) for x in a))
            try:
                report_superseded_png_bleed_cache(cfg)
            finally:
                pipeline_module.console.print = original_print

            joined = "\n".join(captured)
            self.assertIn("1 old-format", joined)
            self.assertNotIn("pending_bleed", joined)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_mm_bleed_dimensions(self):
        orig_w, orig_h = 745, 1040

        # 1. target_mm (63x88mm + 3mm bleed = 69x94mm)
        cfg_mm = AppConfig(bleed_mode="target_mm", cut_width_mm=63.0, cut_height_mm=88.0, bleed_margin_mm=3.0)
        self.assertEqual(cfg_mm.target_card_width_mm, 69.0)
        self.assertEqual(cfg_mm.target_card_height_mm, 94.0)

        tw, th, pt, pb, pl, pr = calculate_target_dimensions(orig_w, orig_h, cfg_mm)
        self.assertEqual(tw, int(round(745 * (69.0 / 63.0))))
        self.assertEqual(th, int(round(1040 * (94.0 / 88.0))))
        self.assertEqual(pt + pb, th - orig_h)
        self.assertEqual(pl + pr, tw - orig_w)

        # 2. fixed_pixels
        cfg_px = AppConfig(bleed_mode="fixed_pixels", bleed_pixels=36)
        tw_px, th_px, pt_px, pb_px, pl_px, pr_px = calculate_target_dimensions(orig_w, orig_h, cfg_px)
        self.assertEqual(tw_px, orig_w + 72)
        self.assertEqual(th_px, orig_h + 72)

        # 3. fixed_percentage
        cfg_pct = AppConfig(bleed_mode="fixed_percentage", bleed_percentage=5.0)
        tw_pct, th_pct, pt_pct, pb_pct, pl_pct, pr_pct = calculate_target_dimensions(orig_w, orig_h, cfg_pct)
        self.assertTrue(tw_pct > orig_w)
        self.assertTrue(th_pct > orig_h)

    def test_borderless_bleed_2_0(self):
        # Create a test RGBA image with rounded corners (alpha=0 at corners)
        w, h = 745, 1040
        rgba = np.full((h, w, 4), 255, dtype=np.uint8)
        # Fill center with orange/tan art
        rgba[:, :, 0] = 200
        rgba[:, :, 1] = 160
        rgba[:, :, 2] = 130
        
        # Transparent corners (circle radius 30)
        ys, xs = np.ogrid[:h, :w]
        tl_corner = (xs < 30) & (ys < 30) & ((xs - 30)**2 + (ys - 30)**2 > 30**2)
        rgba[tl_corner, 3] = 0

        # Also add a dark scanner line at bottom
        rgba[-1, :, :3] = 10

        test_img = Image.fromarray(rgba)
        cfg = AppConfig(bleed_mode="target_mm")
        out_path = "tests/test_borderless_out.png"

        meta = {"border_color": "borderless", "frame_effects": ["showcase"]}
        add_intelligent_bleed(test_img, out_path, cfg, dpi=300, meta=meta)

        self.assertTrue(os.path.exists(out_path))
        with Image.open(out_path) as res:
            res_arr = np.array(res)
            # Corner should be infilled with art color (~[200, 160, 130]), NOT black [0, 0, 0]!
            corner_mean = res_arr[:10, :10].mean(axis=(0, 1))
            self.assertTrue(corner_mean[0] > 100, f"Corner should be art color, got {corner_mean}")
            # Bottom row should NOT have the scanner line [10, 10, 10]
            bot_mean = res_arr[-1, :].mean(axis=0)
            self.assertTrue(bot_mean[0] > 100, f"Bottom row should not be scanner black, got {bot_mean}")

        if os.path.exists(out_path):
            os.remove(out_path)

    def test_formatter_and_redaction(self):
        card_meta = {
            "name": "Test Creature",
            "frame": "2015",
            "type_line": "Creature — Human Soldier",
            "power": "2",
            "toughness": "2",
            "promo_types": ["universesbeyond"]
        }
        self.assertEqual(CardFrame.from_metadata(card_meta), CardFrame.MODERN)
        self.assertEqual(CardType.from_metadata(card_meta), CardType.CREATURE)
        self.assertTrue(is_universes_beyond(card_meta))

        img_np = np.ones((1040, 745, 3), dtype=np.uint8) * 128
        redacted = redact_copyright(img_np.copy(), card_meta)
        self.assertEqual(redacted.shape, (1040, 745, 3))

    def test_torch_device(self):
        device = get_torch_device("auto")
        self.assertIn(device.type, ["mps", "cuda", "cpu"])

    def test_gdrive_manager(self):
        detected = find_default_macos_gdrive()
        gdrive = GDriveManager("auto")
        if detected:
            self.assertTrue(gdrive.enabled)

    def test_non_card_filtering_and_foil_detection(self):
        import tempfile
        from proxy_parser.scryfall_client import ScryfallClient

        # 1. Test non-card line filtering
        content = """// Commander
1 Stella Lee, Wild Card (OTC) 3

# Mainboard
1 Sol Ring (C20) 247
SIDEBOARD:
100 Cards
10/10 - Eldrazi (C)
1 Force of Will (DMR) 284
"""
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tf:
            tf.write(content)
            tf_path = tf.name

        try:
            deck = parse_moxfield_file(tf_path)
            card_names = [c.name for c in deck.cards]
            self.assertIn("Stella Lee, Wild Card", card_names)
            self.assertIn("Sol Ring", card_names)
            self.assertIn("Force of Will", card_names)
            self.assertEqual(len(deck.cards), 3)
            self.assertNotIn("SIDEBOARD:", card_names)
            self.assertNotIn("100 Cards", card_names)
        finally:
            os.remove(tf_path)

        # 2. Test foil-only detection
        client = ScryfallClient(cache_dir="scryfall_cache")
        foil_meta = {"name": "Foil Card", "finishes": ["foil"]}
        nonfoil_meta = {"name": "Regular Card", "finishes": ["nonfoil", "foil"]}
        self.assertTrue(client._is_foil_only(foil_meta))
        self.assertFalse(client._is_foil_only(nonfoil_meta))

if __name__ == "__main__":
    unittest.main()
