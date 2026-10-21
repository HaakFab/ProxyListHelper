#!/usr/bin/env python3
import os
import sys
import glob
import time
import hashlib
from collections import defaultdict
from typing import Dict, List, Optional, Tuple, Any
from PIL import Image
import numpy as np

from proxy_parser import (
    AppConfig,
    parse_arguments,
    CardEntry,
    Deck,
    parse_moxfield_file,
    ScryfallClient,
    RealESRGANUpscaler,
    get_torch_device,
    redact_copyright,
    add_intelligent_bleed,
    process_custom_cardback,
    generate_divider_card,
    generate_divider_cards,
    GDriveManager,
    PDFBatchBuilder,
    write_summary_csv,
    create_progress_bar,
    console
)

def reuse_legacy_png_bleed_as_jpeg(bleed_jpg_path: str, quality: int = 95) -> bool:
    """Fast path for caches built before the PNG->JPEG bleed switch.

    The pre-existing `_bleed.png` at the same name (just a different
    extension) already has the finished, bleed-extended image -- upscaled if
    that run used --mode upscaled. Re-encoding it as JPEG is a cheap format
    conversion; redoing `add_intelligent_bleed` would repeat the corner-mask/
    distance-transform work, and if the raw source were ever missing would
    force a full Real-ESRGAN re-upscale for no reason. Returns True if it
    produced `bleed_jpg_path` this way.
    """
    legacy_png_path = os.path.splitext(bleed_jpg_path)[0] + ".png"
    if not os.path.exists(legacy_png_path):
        return False
    try:
        with Image.open(legacy_png_path) as img:
            dpi = img.info.get("dpi", (300, 300))
            os.makedirs(os.path.dirname(os.path.abspath(bleed_jpg_path)), exist_ok=True)
            img.convert("RGB").save(bleed_jpg_path, "JPEG", quality=quality, dpi=dpi)
        return True
    except Exception:
        return False

def process_card_asset(
    card: CardEntry,
    meta: dict,
    scryfall_client: ScryfallClient,
    upscaler: RealESRGANUpscaler,
    config: AppConfig,
    default_back: str,
    gdrive: GDriveManager
) -> Tuple[str, str]:
    """
    Processes single or double-faced card asset:
    - Single-faced: front image (scryfall or upscaled) + default_back
    - Double-faced: front face and back face both processed through upscaler/bleed
    - Returns: (front_bleed_path, back_bleed_path)
    """
    key = card.key
    raw_front = os.path.join(config.cache_dir, f"{key}_front.png")
    raw_back = os.path.join(config.cache_dir, f"{key}_back.png")

    has_scryfall_back = os.path.exists(raw_back)
    mode = config.mode
    copyright_suffix = "_nocopyright" if config.remove_copyright else ""

    if not os.path.exists(raw_front):
        console.print(f"[bold red]Warning:[/bold red] Raw front image not found for '{card.name}' ({card.key}). Skipping.")
        return "", ""

    if mode == "scryfall":
        # Standard Scryfall mode (300 DPI)
        front_bleed = os.path.join(config.cache_dir, f"{key}_front{copyright_suffix}_bleed.jpg")
        if not os.path.exists(front_bleed) or config.reset_bleed_cache:
            if config.reset_bleed_cache or not reuse_legacy_png_bleed_as_jpeg(front_bleed):
                if config.remove_copyright and meta:
                    with Image.open(raw_front) as img:
                        img_np = np.array(img.convert("RGB"))
                    img_np = redact_copyright(img_np, meta, face_idx=0 if has_scryfall_back else None)
                    redacted_img = Image.fromarray(img_np)
                    add_intelligent_bleed(redacted_img, front_bleed, config, dpi=300, meta=meta)
                else:
                    add_intelligent_bleed(raw_front, front_bleed, config, dpi=300, meta=meta)

        if has_scryfall_back:
            back_bleed = os.path.join(config.cache_dir, f"{key}_back{copyright_suffix}_bleed.jpg")
            if not os.path.exists(back_bleed) or config.reset_bleed_cache:
                if config.reset_bleed_cache or not reuse_legacy_png_bleed_as_jpeg(back_bleed):
                    if config.remove_copyright and meta:
                        with Image.open(raw_back) as img:
                            img_np = np.array(img.convert("RGB"))
                        img_np = redact_copyright(img_np, meta, face_idx=1)
                        redacted_img = Image.fromarray(img_np)
                        add_intelligent_bleed(redacted_img, back_bleed, config, dpi=300, meta=meta)
                    else:
                        add_intelligent_bleed(raw_back, back_bleed, config, dpi=300, meta=meta)
            return front_bleed, back_bleed
        else:
            # Single-faced card: use local default back as-is (already has bleed)
            return front_bleed, default_back

    else:
        # Upscaled Real-ESRGAN mode (600 DPI)
        scale = config.upscale_factor
        dpi = 300 * scale # e.g. 600 DPI for 2x
        os.makedirs(config.upscaled_cache_dir, exist_ok=True)
        model_tag = getattr(config, "upscale_model", "4x-UltraSharp").lower().replace("-", "_").replace(" ", "_")

        # 1. Front face
        front_bleed = os.path.join(
            config.upscaled_cache_dir,
            f"{key}_front_{model_tag}_x{scale}{copyright_suffix}_bleed.jpg"
        )
        if not os.path.exists(front_bleed) or config.reset_bleed_cache:
            if config.reset_bleed_cache or not reuse_legacy_png_bleed_as_jpeg(front_bleed):
                cached_sr = os.path.join(config.upscaled_cache_dir, f"{key}_front_{model_tag}_x{scale}.png")
                # Fallback to legacy un-tagged cache file if present and using baseline RealESRGAN
                if not os.path.exists(cached_sr) and ("x2" in model_tag or "realesrgan" in model_tag):
                    legacy_sr = os.path.join(config.upscaled_cache_dir, f"{key}_front_x{scale}.png")
                    if os.path.exists(legacy_sr):
                        cached_sr = legacy_sr

                if os.path.exists(cached_sr):
                    sr_img = Image.open(cached_sr)
                else:
                    with Image.open(raw_front) as src_img:
                        sr_img = upscaler.predict(src_img)
                    sr_img.save(cached_sr, "PNG")
                    if gdrive.offload_cache:
                        gdrive.offload_cache_file(cached_sr)

                if config.remove_copyright and meta:
                    img_np = np.array(sr_img.convert("RGB"))
                    img_np = redact_copyright(img_np, meta, face_idx=0 if has_scryfall_back else None)
                    proc_img = Image.fromarray(img_np)
                else:
                    proc_img = sr_img

                add_intelligent_bleed(proc_img, front_bleed, config, dpi=dpi, meta=meta)

        # 2. Back face (Double-faced / Transform / MDFC)
        if has_scryfall_back:
            back_bleed = os.path.join(
                config.upscaled_cache_dir,
                f"{key}_back_{model_tag}_x{scale}{copyright_suffix}_bleed.jpg"
            )
            if not os.path.exists(back_bleed) or config.reset_bleed_cache:
                if config.reset_bleed_cache or not reuse_legacy_png_bleed_as_jpeg(back_bleed):
                    cached_sr_back = os.path.join(config.upscaled_cache_dir, f"{key}_back_{model_tag}_x{scale}.png")
                    if not os.path.exists(cached_sr_back) and ("x2" in model_tag or "realesrgan" in model_tag):
                        legacy_sr_back = os.path.join(config.upscaled_cache_dir, f"{key}_back_x{scale}.png")
                        if os.path.exists(legacy_sr_back):
                            cached_sr_back = legacy_sr_back

                    if os.path.exists(cached_sr_back):
                        sr_back_img = Image.open(cached_sr_back)
                    else:
                        with Image.open(raw_back) as src_back:
                            sr_back_img = upscaler.predict(src_back)
                        sr_back_img.save(cached_sr_back, "PNG")
                        if gdrive.offload_cache:
                            gdrive.offload_cache_file(cached_sr_back)

                    if config.remove_copyright and meta:
                        img_np = np.array(sr_back_img.convert("RGB"))
                        img_np = redact_copyright(img_np, meta, face_idx=1)
                        proc_back = Image.fromarray(img_np)
                    else:
                        proc_back = sr_back_img

                    add_intelligent_bleed(proc_back, back_bleed, config, dpi=dpi, meta=meta)

            return front_bleed, back_bleed
        else:
            # Single-faced card: use local default back as-is (already has bleed)
            return front_bleed, default_back

def resolve_requested_face_image(
    key: str,
    requested_name: str,
    card_assets: Dict[str, Tuple[str, str]],
    metadata_map: Dict[str, dict],
) -> Optional[str]:
    """Picks the processed image that matches a specific requested card name.

    Almost every lookup is single-faced, or names a double-faced card's own
    front face -- for those the front-bleed image already fetched for `key`
    is correct. A custom double-sided ("&&&") pairing can instead name a real
    Scryfall card's BACK face explicitly (e.g. gluing two faces of a genuine
    Transform/MDFC card together the same way Scryfall already would, just
    spelled with each face on its own side of "&&&"): in that case the
    already-fetched back-bleed image is the one that actually matches what
    was typed, and using the front-bleed image instead would silently print
    the front face's art on both sides of the card.
    """
    pair = card_assets.get(key)
    if not pair or not pair[0]:
        return None
    front_img, back_img = pair

    meta = metadata_map.get(key)
    faces = meta.get("card_faces") if meta else None
    if faces and len(faces) >= 2 and back_img:
        face_names = [(f.get("name") or "").strip().lower() for f in faces]
        requested = requested_name.strip().lower()
        if requested == face_names[1] and requested != face_names[0]:
            return back_img

    return front_img

def reset_local_bleed_cache(config: AppConfig):
    """Removes all old cached bleed files to force recalculation.

    Matches both `_bleed.jpg` (current format) and `_bleed.png` (pre-JPEG-switch
    cache from older runs) so a reset also reclaims space from now-orphaned PNGs
    that the new path scheme will never look at again.
    """
    patterns = [
        os.path.join(config.cache_dir, "*_bleed.jpg"),
        os.path.join(config.cache_dir, "*_bleed.png"),
        os.path.join(config.cache_dir, "divider_*.png"),
        os.path.join(config.upscaled_cache_dir, "*_bleed.jpg"),
        os.path.join(config.upscaled_cache_dir, "*_bleed.png"),
    ]
    removed_count = 0
    for pattern in patterns:
        for f in glob.glob(pattern):
            try:
                os.remove(f)
                removed_count += 1
            except OSError:
                pass
    if removed_count > 0:
        console.print(f"[yellow]Cleared {removed_count} stale bleed/divider cache file(s).[/yellow]")

def report_superseded_png_bleed_cache(config: AppConfig):
    """Heads-up about pre-JPEG-switch '_bleed.png' files that are now dead weight.

    A legacy '_bleed.png' is actively useful as long as its '.jpg' sibling
    doesn't exist yet: reuse_legacy_png_bleed_as_jpeg() converts it instead of
    redoing the (expensive) bleed computation or a full re-upscale. Once that
    conversion has happened -- i.e. the '.jpg' sibling exists -- the PNG is
    pure leftover disk usage. Only counts PNGs in that superseded state, and
    only ever reports; nothing here deletes anything.
    """
    patterns = [
        os.path.join(config.cache_dir, "*_bleed.png"),
        os.path.join(config.upscaled_cache_dir, "*_bleed.png"),
    ]
    superseded_files = [
        f for pattern in patterns for f in glob.glob(pattern)
        if os.path.exists(os.path.splitext(f)[0] + ".jpg")
    ]
    if not superseded_files:
        return
    total_bytes = sum(os.path.getsize(f) for f in superseded_files if os.path.exists(f))
    total_gb = total_bytes / (1024 ** 3)
    console.print(
        f"[yellow]Note:[/yellow] {len(superseded_files)} old-format '_bleed.png' file(s) "
        f"(~{total_gb:.1f} GB) have already been converted to their '.jpg' equivalent and are now "
        "unused leftovers. Safe to delete manually, e.g.:\n"
        f"  [dim]find \"{config.cache_dir}\" \"{config.upscaled_cache_dir}\" -name '*_bleed.png' "
        "-exec sh -c 'test -f \"${1%.png}.jpg\" && rm \"$1\"' _ {} \\;[/dim]"
    )

def run_pipeline(
    config: AppConfig,
    specific_entries: Optional[List[Tuple[str, Optional[str]]]] = None,
    progress_callback=None,
    log_callback=None,
    is_cancelled=None
) -> Dict[str, Any]:
    """Executes the complete proxy generation and PDF compilation pipeline."""
    def log(msg: str):
        console.print(msg)
        if log_callback:
            import re
            plain = re.sub(r"\[.*?\]", "", str(msg))
            log_callback(plain)

    log("\n[bold magenta]=====================================================[/bold magenta]")
    log(f"[bold cyan] MTG Moxfield Proxy to PDF Generator[/bold cyan] [green]v2.1[/green]")
    log(f" Mode: [yellow]{config.mode}[/yellow] | Upscale: [yellow]x{config.upscale_factor}[/yellow] | Remove Copyright: [yellow]{config.remove_copyright}[/yellow]")
    log(f" Card Dimensions: [bold green]{config.cut_width_mm:.0f}x{config.cut_height_mm:.0f}mm[/bold green] (+{config.bleed_margin_mm:.0f}mm bleed -> [bold green]{config.target_card_width_mm:.0f}x{config.target_card_height_mm:.0f}mm[/bold green])")
    log(f" Bleed: [yellow]{config.bleed_mode}[/yellow] | Break PDF: [yellow]{config.break_by}[/yellow]")
    log(f" Dividers: [yellow]{'Two cards (Striped)' if getattr(config, 'two_divider_cards', True) else 'Single card'}[/yellow] | Suborders: [yellow]{getattr(config, 'enable_suborders', False)}[/yellow]")
    log("[bold magenta]=====================================================[/bold magenta]\n")

    if config.reset_bleed_cache:
        reset_local_bleed_cache(config)

    # 1. Check default cardback
    if not os.path.exists(config.default_cardback):
        log(f"[bold red]Error:[/bold red] Cardback '{config.default_cardback}' not found.")
        log(f"Please place your default card back inside the '{config.backs_dir}/' folder.")
        return {"success": False, "error": f"Cardback '{config.default_cardback}' not found."}

    # 2. Setup Google Drive integration
    gdrive = GDriveManager(config.gdrive_dir, config.offload_cache)

    # Output directory: Always write locally first to prevent 0-byte sync locks
    local_output_dir = "output"
    os.makedirs(local_output_dir, exist_ok=True)

    # 3. Discover decklist files
    if specific_entries is not None:
        discovered_entries = specific_entries
    else:
        discovered_entries = []
        if os.path.exists(config.input_dir):
            for item in sorted(os.listdir(config.input_dir)):
                item_path = os.path.join(config.input_dir, item)
                if os.path.isfile(item_path) and item.endswith(".txt"):
                    discovered_entries.append((item_path, None))
                elif os.path.isdir(item_path):
                    sub_txts = sorted(glob.glob(os.path.join(item_path, "*.txt")))
                    if sub_txts:
                        decklist_file = os.path.join(item_path, "decklist.txt")
                        chosen_txt = decklist_file if os.path.exists(decklist_file) else sub_txts[0]
                        custom_back = None
                        for ext in [".jpg", ".jpeg", ".png", ".webp"]:
                            for b_name in ["back", "cardback", "custom_back"]:
                                cand = os.path.join(item_path, f"{b_name}{ext}")
                                if os.path.exists(cand):
                                    custom_back = cand
                                    break
                            if custom_back:
                                break
                        discovered_entries.append((chosen_txt, custom_back))

    if not discovered_entries:
        log(f"[bold yellow]No deck files found in '{config.input_dir}/'. Place your Moxfield .txt files or deck folders there.[/bold yellow]")
        return {"success": False, "error": f"No deck files found in '{config.input_dir}/'"}

    log(f"Found [bold green]{len(discovered_entries)}[/bold green] decklist(s) for print run.")


    # 4. Parse all deck files
    decks: List[Deck] = []
    unique_cards: Dict[str, CardEntry] = {}
    person_card_counts: Dict[str, int] = defaultdict(int)

    for path, custom_back in discovered_entries:
        deck = parse_moxfield_file(path)
        if custom_back:
            deck.custom_cardback = custom_back
        decks.append(deck)
        person_card_counts[deck.person] += deck.total_cards
        for card in deck.cards:
            if card.key not in unique_cards:
                unique_cards[card.key] = card
            if card.is_custom_double_sided:
                back = card.back_entry
                if back.key not in unique_cards:
                    unique_cards[back.key] = back

    # Sort deck lists by person name (case-insensitive), then by list label
    decks.sort(key=lambda d: (d.person.lower(), d.label.lower(), d.file_path.lower()))

    total_cards_across_decks = sum(d.total_cards for d in decks)
    console.print(f"Total cards to process: [bold green]{total_cards_across_decks}[/bold green] ([bold cyan]{len(unique_cards)}[/bold cyan] unique).")
    console.print(f"Sorted [bold green]{len(decks)}[/bold green] decklist(s) alphabetically by person name for output compilation.")

    # 5. Scryfall Client Fetching
    scryfall_client = ScryfallClient(config.cache_dir)
    upscaler = None
    if config.mode == "upscaled":
        upscaler = RealESRGANUpscaler(
            model_name=getattr(config, "upscale_model", "4x-UltraSharp"),
            scale=config.upscale_factor,
            device_pref=config.device,
            weights_dir=getattr(config, "weights_dir", "weights")
        )

    # 6. Progress UI Execution
    with create_progress_bar() as progress:
        # Step A: Fetch Scryfall metadata & raw images
        fetch_task = progress.add_task("[cyan]Fetching Scryfall card data...", total=len(unique_cards))
        
        def on_fetch_progress(current, total):
            progress.update(fetch_task, completed=current, total=total)
            if progress_callback:
                pct = (current / max(total, 1)) * 100.0
                progress_callback("scryfall", current, total, pct, f"Fetching Scryfall card data ({current}/{total})...")

        metadata_map = scryfall_client.fetch_cards_batch(list(unique_cards.values()), on_fetch_progress)
        progress.update(fetch_task, completed=len(unique_cards), description="[green]Scryfall data verified.")

        # Step B: Upscale & Bleed processing
        target_dpi = 600 if config.mode == "upscaled" else 300
        effective_default_back = config.default_cardback
        if os.path.exists(config.default_cardback) and config.default_cardback != os.path.join("backs", "proxy1.jpg"):
            def_hash = hashlib.md5(config.default_cardback.encode()).hexdigest()[:8]
            processed_def = os.path.join(config.cache_dir, f"default_back_{def_hash}_bleed.jpg")
            if not os.path.exists(processed_def) or config.reset_bleed_cache:
                process_custom_cardback(config.default_cardback, processed_def, config, dpi=target_dpi, upscaler=upscaler)
            effective_default_back = processed_def

        process_task = progress.add_task(
            f"[yellow]Processing card assets ({config.mode})...",
            total=len(unique_cards)
        )

        card_assets: Dict[str, Tuple[str, str]] = {}
        for idx, (key, card) in enumerate(unique_cards.items(), start=1):
            if is_cancelled and is_cancelled():
                log("[yellow]Operation cancelled by user.[/yellow]")
                return {"success": False, "error": "Cancelled by user"}

            meta = metadata_map.get(key, {})
            front_path, back_path = process_card_asset(
                card=card,
                meta=meta,
                scryfall_client=scryfall_client,
                upscaler=upscaler,
                config=config,
                default_back=effective_default_back,
                gdrive=gdrive
            )
            card_assets[key] = (front_path, back_path)
            progress.update(
                process_task,
                completed=idx,
                description=f"[yellow]Processing card {idx}/{len(unique_cards)}: [white]{card.name[:25]}[/white]"
            )
            if progress_callback:
                pct = (idx / max(len(unique_cards), 1)) * 100.0
                progress_callback("upscale", idx, len(unique_cards), pct, f"Processing card {idx}/{len(unique_cards)}: {card.name[:25]}")

        progress.update(process_task, description=f"[green]All {len(unique_cards)} card assets ready.")

        # Step C: Prepare Decks and Dividers
        deck_items = []
        unprintable: List[Tuple[str, str, CardEntry]] = []

        # Determine ordering ID for each deck ([SuborderNumber//ListNumber], e.g. [1//4])
        deck_order_map: Dict[int, str] = {}
        if getattr(config, "enable_suborders", False):
            from proxy_parser.grouping import optimize_suborders
            temp_items = [
                {
                    "deck": d,
                    "card_pairs": [None] * sum(c.qty for c in d.cards),
                    "dividers": [None, None] if getattr(config, "two_divider_cards", True) else [None]
                }
                for d in decks
            ]
            batches = optimize_suborders(
                temp_items,
                cards_per_sheet=getattr(config, "cards_per_sheet", 18),
                target_suborder_cards=getattr(config, "target_suborder_cards", 1440)
            )
            for b in batches:
                for l_idx, it in enumerate(b.deck_items, start=1):
                    deck_order_map[id(it["deck"])] = f"[{b.batch_index}//{l_idx}]"
        else:
            for l_idx, d in enumerate(decks, start=1):
                deck_order_map[id(d)] = f"[1//{l_idx}]"

        for d_idx, deck in enumerate(decks, start=1):
            if is_cancelled and is_cancelled():
                log("[yellow]Operation cancelled by user.[/yellow]")
                return {"success": False, "error": "Cancelled by user"}

            if progress_callback:
                pct = (d_idx / max(len(decks), 1)) * 100.0
                progress_callback("dividers", d_idx, len(decks), pct, f"Generating divider cards ({d_idx}/{len(decks)}): {deck.label}")

            order_id = deck_order_map.get(id(deck), f"[1//{d_idx}]")
            card_count = sum(card.qty for card in deck.cards)
            if getattr(config, "two_divider_cards", True):
                dividers = generate_divider_cards(
                    label=deck.label,
                    person=deck.person,
                    card_count=card_count,
                    cache_dir=config.cache_dir,
                    config=config,
                    dpi=target_dpi,
                    order_id=order_id
                )
            else:
                div_front, div_back = generate_divider_card(
                    label=deck.label,
                    cache_dir=config.cache_dir,
                    config=config,
                    dpi=target_dpi,
                    order_id=order_id
                )
                dividers = [(div_front, div_back)]

            card_pairs = []
            # Check for deck-specific custom cardback
            deck_custom_back_bleed = None
            if getattr(deck, "custom_cardback", None) and os.path.exists(deck.custom_cardback):
                back_hash = hashlib.md5(deck.custom_cardback.encode()).hexdigest()[:8]
                cback_bleed = os.path.join(config.cache_dir, f"custom_back_{back_hash}_bleed.jpg")
                if not os.path.exists(cback_bleed) or config.reset_bleed_cache:
                    process_custom_cardback(
                        deck.custom_cardback,
                        cback_bleed,
                        config,
                        dpi=target_dpi,
                        upscaler=upscaler
                    )
                deck_custom_back_bleed = cback_bleed

            for card in deck.cards:
                if card.is_custom_double_sided:
                    # Custom pairing: each side is looked up independently by
                    # its own name/printing. Usually that means each side's
                    # own front face; resolve_requested_face_image also
                    # covers the case where a side names a real MDFC's BACK
                    # face specifically.
                    back_entry = card.back_entry
                    front_img = resolve_requested_face_image(card.key, card.name, card_assets, metadata_map)
                    back_img = resolve_requested_face_image(back_entry.key, back_entry.name, card_assets, metadata_map)
                    if front_img and back_img:
                        for _ in range(card.qty):
                            card_pairs.append((front_img, back_img))
                    else:
                        unprintable.append((deck.person, deck.label, card))
                    continue

                pair = card_assets.get(card.key)
                if pair and pair[0]:
                    front_p, back_p = pair
                    # If this single-faced card uses default back and this deck has a custom back, use it!
                    if deck_custom_back_bleed and (back_p == config.default_cardback or back_p == effective_default_back):
                        final_pair = (front_p, deck_custom_back_bleed)
                    else:
                        final_pair = pair
                    for _ in range(card.qty):
                        card_pairs.append(final_pair)
                else:
                    # Never drop a card silently: the person expects it in their
                    # stack, and with prepayment they have already been charged.
                    unprintable.append((deck.person, deck.label, card))

            deck_items.append({
                "deck": deck,
                "dividers": dividers,
                "card_pairs": card_pairs
            })

        # Step D: Compile PDF(s) into local output directory
        total_cards_for_pdf = max(
            sum(len(item["card_pairs"]) + len(item.get("dividers", [])) for item in deck_items),
            1
        )
        pdf_task = progress.add_task(
            f"[magenta]Compiling PDF document(s)... (0/{total_cards_for_pdf} cards)",
            total=total_cards_for_pdf
        )

        pdf_builder = PDFBatchBuilder(config)

        def on_pdf_progress(curr, tot):
            progress.update(
                pdf_task,
                completed=curr,
                total=tot,
                description=f"[magenta]Compiling PDF document(s)... ({curr}/{tot} cards)"
            )
            if progress_callback:
                pct = (curr / max(tot, 1)) * 100.0
                progress_callback("pdf", curr, tot, pct, f"Compiling PDF document(s)... ({curr}/{tot} cards)")

        file_summaries = pdf_builder.build_pdfs(
            deck_items=deck_items,
            output_dir=local_output_dir,
            progress_callback=on_pdf_progress
        )
        progress.update(pdf_task, description="[green]PDF compilation complete.")

        # Step E: Sync to Google Drive if configured.
        if gdrive.enabled and gdrive.output_dir:
            log(
                "[dim]Note: writing large files directly into a Google Drive-synced folder is "
                "inherently much slower than local disk -- this can take a while for big upscaled "
                f"PDFs. The finished file is already saved locally at '{local_output_dir}/' "
                "regardless of how this step goes.[/dim]"
            )
            for s in file_summaries:
                size_mb = os.path.getsize(s["file"]) / (1024 * 1024)
                sync_task = progress.add_task(
                    f"[cyan]Syncing {os.path.basename(s['file'])} to Google Drive... (0.0/{size_mb:.0f} MB)",
                    total=max(size_mb, 1)
                )

                def on_sync_progress(copied_bytes, total_bytes, _task=sync_task, _name=os.path.basename(s["file"])):
                    copied_mb = copied_bytes / (1024 * 1024)
                    total_mb = max(total_bytes / (1024 * 1024), 1)
                    progress.update(
                        _task,
                        completed=copied_mb,
                        total=total_mb,
                        description=f"[cyan]Syncing {_name} to Google Drive... ({copied_mb:.0f}/{total_mb:.0f} MB)"
                    )

                try:
                    gdrive.sync_file_to_drive(s["file"], progress_callback=on_sync_progress)
                    progress.update(sync_task, description=f"[green]Synced {os.path.basename(s['file'])} to Google Drive.")
                except OSError as ex:
                    progress.update(sync_task, description=f"[bold red]Google Drive sync failed for {os.path.basename(s['file'])}: {ex}")
                    log(
                        f"[bold red]⚠ Could not sync '{s['file']}' to Google Drive:[/bold red] {ex}\n"
                        f"[yellow]The file is still safe locally at '{s['file']}'. "
                        "Copy it to Drive manually once the issue is resolved.[/yellow]"
                    )

            # Copy CSV
            csv_output_path = os.path.join(local_output_dir, config.output_csv)
            write_summary_csv(csv_output_path, person_card_counts, file_summaries)
            try:
                gdrive.sync_file_to_drive(csv_output_path)
            except OSError as ex:
                log(f"[bold red]⚠ Could not sync summary CSV to Google Drive:[/bold red] {ex}")
        else:
            csv_output_path = os.path.join(local_output_dir, config.output_csv)
            write_summary_csv(csv_output_path, person_card_counts, file_summaries)

    # 7. Final Report
    log("\n[bold green]✓ Processing Complete![/bold green]")
    log(f"Summary CSV: [bold]{csv_output_path}[/bold]")
    log("[bold]Generated PDF Files (Local):[/bold]")
    for s in file_summaries:
        size_mb = os.path.getsize(s["file"]) / (1024 * 1024)
        log(f"  • [cyan]{s['file']}[/cyan] ({s['cards']} cards, {s['pages']} pages, {size_mb:.1f} MB) - [yellow]{s['label']}[/yellow]")

    if gdrive.enabled and gdrive.output_dir:
        log(f"[bold green]✓ Synchronized to Google Drive:[/bold green] {gdrive.output_dir}")

    if not config.reset_bleed_cache:
        report_superseded_png_bleed_cache(config)

    # 8. Unprintable cards
    if unprintable:
        missing_qty = sum(card.qty for _, _, card in unprintable)
        log(
            f"\n[bold red]⚠ {missing_qty} card(s) across {len(unprintable)} entries "
            f"could NOT be printed and are MISSING from the PDFs:[/bold red]"
        )
        by_person: Dict[str, List[str]] = {}
        for person, label, card in unprintable:
            printing = f" ({card.set.upper()}) {card.cn}" if card.set and card.cn else ""
            back_info = ""
            if card.is_custom_double_sided:
                back_printing = f" ({card.back_set.upper()}) {card.back_cn}" if card.back_set and card.back_cn else ""
                back_info = f" &&& {card.back_name}{back_printing}"
            by_person.setdefault(person, []).append(f"{card.qty}x {card.name}{printing}{back_info}  [{label}]")
        for person in sorted(by_person):
            log(f"  [bold]{person}[/bold]")
            for entry in by_person[person]:
                log(f"    • [red]{entry}[/red]")
        log(
            "\n[yellow]These cards were ordered but no image could be resolved. "
            "Fix the decklist or the Scryfall printing and re-run before printing.[/yellow]"
        )

    serializable_unprintable = [
        {
            "person": person,
            "label": label,
            "name": getattr(card, "name", str(card)),
            "qty": getattr(card, "qty", 1),
            "set": getattr(card, "set", ""),
            "cn": getattr(card, "cn", ""),
            "is_custom_double_sided": getattr(card, "is_custom_double_sided", False),
            "back_name": getattr(card, "back_name", "")
        }
        for person, label, card in unprintable
    ]

    if progress_callback:
        progress_callback("done", total_cards_for_pdf, total_cards_for_pdf, 100.0, "All PDF documents compiled successfully!")

    return {
        "success": True,
        "file_summaries": file_summaries,
        "csv_path": csv_output_path,
        "total_cards": total_cards_across_decks,
        "unique_cards": len(unique_cards),
        "unprintable": serializable_unprintable,
        "decks": len(decks),
    }

def main():
    config = parse_arguments()
    if getattr(config, "open_ui", False):
        from generator_server import run_server
        run_server(config)
        return

    result = run_pipeline(config)
    if not result.get("success", False):
        sys.exit(1)
    if result.get("unprintable"):
        sys.exit(4)

if __name__ == "__main__":
    main()