import os
import re
from typing import Tuple
from PIL import Image, ImageDraw, ImageFont
from .config import AppConfig

def generate_divider_card(label: str, cache_dir: str, config: AppConfig, dpi: int = 300) -> Tuple[str, str]:
    """
    Generates front and back images for a neon-yellow divider card matching target mm dimensions and DPI.
    815 x 1110 px @ 300 DPI, or 1630 x 2220 px @ 600 DPI.
    """
    os.makedirs(cache_dir, exist_ok=True)
    width_px = int(round((config.target_card_width_mm / 25.4) * dpi))
    height_px = int(round((config.target_card_height_mm / 25.4) * dpi))

    neon_yellow = (225, 255, 0)
    black = (0, 0, 0)

    # Inset & border proportional to DPI
    scale = dpi / 300.0
    border_inset = int(round(45 * scale))
    border_width = int(round(16 * scale))

    # 1. Front with multiline label text
    front_img = Image.new("RGB", (width_px, height_px), color=neon_yellow)
    draw = ImageDraw.Draw(front_img)
    draw.rectangle(
        [(border_inset, border_inset), (width_px - border_inset, height_px - border_inset)],
        outline=black,
        width=border_width
    )

    max_text_w = width_px - (2 * border_inset) - int(60 * scale)
    max_text_h = height_px - (2 * border_inset) - int(100 * scale)

    words = label.split()
    if len(words) > 1:
        lines = [" ".join(words[:-1]), words[-1]]
    elif "_" in label:
        parts = label.split("_")
        lines = ["_".join(parts[:-1]), parts[-1]]
    else:
        lines = [label]

    font_candidates = [
        "/Library/Fonts/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial.ttf",
        "C:\\Windows\\Fonts\\arialbd.ttf",
        "C:\\Windows\\Fonts\\arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    ]
    font_path = None
    for candidate in font_candidates:
        if os.path.exists(candidate):
            font_path = candidate
            break

    font_size = int(round(180 * scale))
    min_font_size = int(round(36 * scale))
    selected_font = None

    while font_size >= min_font_size:
        try:
            if font_path:
                current_font = ImageFont.truetype(font_path, font_size)
            else:
                current_font = ImageFont.load_default()
        except Exception:
            current_font = ImageFont.load_default()

        line_boxes = [draw.textbbox((0, 0), l, font=current_font) for l in lines]
        total_w = max(b[2] - b[0] for b in line_boxes)
        line_heights = [b[3] - b[1] for b in line_boxes]
        total_h = sum(line_heights) + int(font_size * 0.3 * (len(lines) - 1))

        if total_w <= max_text_w and total_h <= max_text_h:
            selected_font = current_font
            break
        
        font_size -= int(round(4 * scale))

    if not selected_font:
        selected_font = ImageFont.load_default()

    line_boxes = [draw.textbbox((0, 0), l, font=selected_font) for l in lines]
    line_heights = [b[3] - b[1] for b in line_boxes]
    spacing = int(font_size * 0.25)
    total_block_h = sum(line_heights) + spacing * (len(lines) - 1)
    
    current_y = (height_px - total_block_h) / 2

    for idx, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line, font=selected_font)
        lw = bbox[2] - bbox[0]
        lx = (width_px - lw) / 2
        draw.text((lx, current_y), line, fill=black, font=selected_font)
        current_y += line_heights[idx] + spacing

    safe_label = re.sub(r'[^a-zA-Z0-9]', '_', label)
    dim_str = f"{config.target_card_width_mm:.0f}x{config.target_card_height_mm:.0f}mm"
    front_path = os.path.join(cache_dir, f"divider_front_{safe_label}_{dim_str}_{dpi}dpi.png")
    front_img.save(front_path, "PNG", dpi=(dpi, dpi))

    # 2. Solid neon-yellow back with border
    back_img = Image.new("RGB", (width_px, height_px), color=neon_yellow)
    back_draw = ImageDraw.Draw(back_img)
    back_draw.rectangle(
        [(border_inset, border_inset), (width_px - border_inset, height_px - border_inset)],
        outline=black,
        width=border_width
    )
    back_path = os.path.join(cache_dir, f"divider_back_{safe_label}_{dim_str}_{dpi}dpi.png")
    back_img.save(back_path, "PNG", dpi=(dpi, dpi))

    return front_path, back_path
