import os
import re
import datetime
from typing import Tuple, Dict, Any, Optional
from PIL import Image, ImageDraw, ImageFont
from .config import AppConfig

# Very bright alternating colors: White, Vivid Red, Signal Yellow, Electric Blue
STRIPE_COLORS = [
    (255, 255, 255),  # White
    (235, 15, 40),    # Vibrant Red
    (255, 235, 0),    # Bright Yellow
    (0, 105, 255),    # Electric Blue
]

def _get_font(candidates: list, size: int) -> ImageFont.ImageFont:
    """Tries candidate font paths on macOS/Linux/Windows, falling back to default."""
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()

def _draw_vertical_stripes(draw: ImageDraw.ImageDraw, width_px: int, height_px: int, scale: float):
    """Draws full-bleed alternating vertical stripes of thin bright colored lines."""
    stripe_w = max(4, int(round(12 * scale)))
    num_colors = len(STRIPE_COLORS)
    for x in range(0, width_px, stripe_w):
        col = STRIPE_COLORS[(x // stripe_w) % num_colors]
        draw.rectangle([(x, 0), (min(x + stripe_w, width_px), height_px)], fill=col)

def generate_divider_cards(
    person: str = "",
    deck_label: str = "",
    card_count: int = 0,
    cache_dir: str = "",
    config: Optional[AppConfig] = None,
    dpi: int = 300,
    details: Optional[Dict[str, Any]] = None,
    label: Optional[str] = None
) -> Tuple[Tuple[str, str], Tuple[str, str]]:
    """
    Generates TWO high-visibility divider cards with alternating full-bleed vertical stripes
    (White, Red, Yellow, Blue) and NO black border so edges vividly stand out from the side.

    Returns:
        ((card1_front_path, card1_back_path), (card2_front_path, card2_back_path))
    """
    if label and not deck_label:
        deck_label = label
    if config is None:
        config = AppConfig()
    os.makedirs(cache_dir, exist_ok=True)
    scale = dpi / 300.0
    width_px = int(round((config.target_card_width_mm / 25.4) * dpi))
    height_px = int(round((config.target_card_height_mm / 25.4) * dpi))

    font_bold_candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "C:\\Windows\\Fonts\\arialbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    ]
    font_mono_candidates = [
        "/System/Library/Fonts/SFNSMono.ttf",
        "/System/Library/Fonts/Supplemental/Courier New Bold.ttf",
        "/Library/Fonts/Courier New.ttf",
        "C:\\Windows\\Fonts\\consola.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
    ]

    safe_person = re.sub(r'[^a-zA-Z0-9]', '_', person)
    safe_label = re.sub(r'[^a-zA-Z0-9]', '_', deck_label)
    dim_str = f"{config.target_card_width_mm:.0f}x{config.target_card_height_mm:.0f}mm"

    # Shared back image: Full-bleed vertical stripes
    shared_back_path = os.path.join(cache_dir, f"divider_stripes_back_{dim_str}_{dpi}dpi.png")
    if not os.path.exists(shared_back_path) or config.reset_bleed_cache:
        back_img = Image.new("RGB", (width_px, height_px), color=(255, 255, 255))
        b_draw = ImageDraw.Draw(back_img)
        _draw_vertical_stripes(b_draw, width_px, height_px, scale)
        back_img.save(shared_back_path, "PNG", dpi=(dpi, dpi))

    # =========================================================================
    # CARD 1: Identity Divider (Large Person Name, Small Deck Name)
    # =========================================================================
    front1_path = os.path.join(cache_dir, f"divider1_{safe_person}_{safe_label}_{dim_str}_{dpi}dpi.png")
    if not os.path.exists(front1_path) or config.reset_bleed_cache:
        img1 = Image.new("RGB", (width_px, height_px), color=(255, 255, 255))
        d1 = ImageDraw.Draw(img1)
        _draw_vertical_stripes(d1, width_px, height_px, scale)

        box_w = int(round(660 * scale))
        box_h = int(round(460 * scale))
        bx = (width_px - box_w) // 2
        by = (height_px - box_h) // 2
        shadow = int(round(14 * scale))
        border = int(round(7 * scale))

        # 3D Brutalist drop-shadow and white card container
        d1.rectangle([(bx + shadow, by + shadow), (bx + box_w + shadow, by + box_h + shadow)], fill=(0, 0, 0))
        d1.rectangle([(bx, by), (bx + box_w, by + box_h)], fill=(255, 255, 255), outline=(0, 0, 0), width=border)

        tag_font = _get_font(font_mono_candidates, int(round(22 * scale)))
        d1.text((bx + int(32 * scale), by + int(36 * scale)), "[ DECK OWNER & SEPARATOR ]", fill=(110, 110, 110), font=tag_font)

        # Dynamic Font Scaling for Person Name (Large)
        person_font_size = int(round(86 * scale))
        person_font = _get_font(font_bold_candidates, person_font_size)
        while person_font_size >= int(round(36 * scale)):
            person_font = _get_font(font_bold_candidates, person_font_size)
            tb = d1.textbbox((0, 0), person, font=person_font)
            if (tb[2] - tb[0]) <= (box_w - int(64 * scale)):
                break
            person_font_size -= int(round(4 * scale))

        d1.text((bx + int(32 * scale), by + int(120 * scale)), person, fill=(0, 0, 0), font=person_font)

        # Small Deck / List Name
        deck_font_size = int(round(34 * scale))
        deck_font = _get_font(font_bold_candidates, deck_font_size)
        deck_text = f"// LIST: {deck_label}"
        while deck_font_size >= int(round(20 * scale)):
            deck_font = _get_font(font_bold_candidates, deck_font_size)
            tb = d1.textbbox((0, 0), deck_text, font=deck_font)
            if (tb[2] - tb[0]) <= (box_w - int(64 * scale)):
                break
            deck_font_size -= int(round(2 * scale))

        d1.text((bx + int(32 * scale), by + int(270 * scale)), deck_text, fill=(235, 15, 40), font=deck_font)

        # Bottom sub badge
        count_tag = f"TOTAL: {card_count} CARDS"
        d1.text((bx + int(32 * scale), by + int(380 * scale)), count_tag, fill=(0, 105, 255), font=tag_font)

        img1.save(front1_path, "PNG", dpi=(dpi, dpi))

    # =========================================================================
    # CARD 2: Details Divider (Number of cards, very small print details)
    # =========================================================================
    front2_path = os.path.join(cache_dir, f"divider2_{safe_person}_{safe_label}_{dim_str}_{dpi}dpi.png")
    if not os.path.exists(front2_path) or config.reset_bleed_cache:
        img2 = Image.new("RGB", (width_px, height_px), color=(255, 255, 255))
        d2 = ImageDraw.Draw(img2)
        _draw_vertical_stripes(d2, width_px, height_px, scale)

        box_w = int(round(660 * scale))
        box_h = int(round(580 * scale))
        bx = (width_px - box_w) // 2
        by = (height_px - box_h) // 2
        shadow = int(round(14 * scale))
        border = int(round(7 * scale))

        d2.rectangle([(bx + shadow, by + shadow), (bx + box_w + shadow, by + box_h + shadow)], fill=(0, 0, 0))
        d2.rectangle([(bx, by), (bx + box_w, by + box_h)], fill=(255, 255, 255), outline=(0, 0, 0), width=border)

        tag_font = _get_font(font_mono_candidates, int(round(20 * scale)))
        d2.text((bx + int(32 * scale), by + int(32 * scale)), "[ BATCH PRODUCTION SPECIFICATION ]", fill=(110, 110, 110), font=tag_font)

        count_font = _get_font(font_bold_candidates, int(round(72 * scale)))
        d2.text((bx + int(32 * scale), by + int(80 * scale)), f"{card_count} CARDS", fill=(0, 105, 255), font=count_font)

        # Thin separator line
        d2.line(
            [(bx + int(32 * scale), by + int(195 * scale)), (bx + box_w - int(32 * scale), by + int(195 * scale))],
            fill=(220, 220, 220),
            width=int(round(3 * scale))
        )

        detail_title_font = _get_font(font_bold_candidates, int(round(22 * scale)))
        detail_val_font = _get_font(font_mono_candidates, int(round(18 * scale)))

        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        detail_rows = [
            ("Customer / Person:", person),
            ("Deck / List Name:", deck_label),
            ("Target Cut Dimensions:", f"{config.cut_width_mm:.0f}x{config.cut_height_mm:.0f}mm (+{config.bleed_margin_mm:.0f}mm Bleed -> {config.target_card_width_mm:.0f}x{config.target_card_height_mm:.0f}mm)"),
            ("Resolution & Mode:", f"{dpi} DPI ({config.mode.upper()})"),
            ("Imposition Scheme:", f"18 Cards / Sheet (Cut & Stack)"),
            ("Generation Date:", now_str),
        ]

        if details:
            for k, v in details.items():
                detail_rows.append((k, str(v)))

        dy = by + int(215 * scale)
        row_step = int(round(46 * scale))
        val_offset = int(round(240 * scale))

        for title, val in detail_rows:
            d2.text((bx + int(32 * scale), dy), title, fill=(0, 0, 0), font=detail_title_font)
            # Truncate very long value
            display_val = val if len(val) <= 36 else val[:33] + "..."
            d2.text((bx + val_offset, dy + int(2 * scale)), display_val, fill=(70, 70, 70), font=detail_val_font)
            dy += row_step

        img2.save(front2_path, "PNG", dpi=(dpi, dpi))

    return ((front1_path, shared_back_path), (front2_path, shared_back_path))

def generate_divider_card(label: str, cache_dir: str, config: AppConfig, dpi: int = 300) -> Tuple[str, str]:
    """
    Backwards-compatible wrapper returning the first divider card.
    """
    cards = generate_divider_cards(
        person=label.split()[0] if label else "Deck",
        deck_label=label,
        card_count=100,
        cache_dir=cache_dir,
        config=config,
        dpi=dpi
    )
    return cards[0]
