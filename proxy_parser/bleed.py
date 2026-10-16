import os
import numpy as np
from PIL import Image
from typing import Tuple, Optional, Dict, Any
import scipy.ndimage as ndi
from .config import AppConfig

def calculate_target_dimensions(
    orig_w: int,
    orig_h: int,
    config: AppConfig
) -> Tuple[int, int, int, int, int, int]:
    """
    Computes (target_w, target_h, pad_top, pad_bottom, pad_left, pad_right)
    dynamically based on config.bleed_mode:
      - 'target_mm' (default): 63x88mm cut + 3mm bleed margin -> 69x94mm
      - 'fixed_pixels': Exact pixel border padding
      - 'fixed_percentage': Percentage of original image dimensions
      - 'target_inches': Backwards-compatible inch calculation
    """
    mode = config.bleed_mode

    if mode == "fixed_pixels":
        scale_ratio = orig_h / 1040.0
        pad_px = int(round(config.bleed_pixels * scale_ratio))
        target_w = orig_w + (2 * pad_px)
        target_h = orig_h + (2 * pad_px)
        pad_top = pad_px
        pad_bottom = pad_px
        pad_left = pad_px
        pad_right = pad_px

    elif mode == "fixed_percentage":
        pad_w = int(round(orig_w * (config.bleed_percentage / 100.0)))
        pad_h = int(round(orig_h * (config.bleed_percentage / 100.0)))
        target_w = orig_w + (2 * pad_w)
        target_h = orig_h + (2 * pad_h)
        pad_top = pad_h
        pad_bottom = pad_h
        pad_left = pad_w
        pad_right = pad_w

    else: # "target_mm" (default: 63x88mm -> 69x94mm)
        ratio_w = config.target_card_width_mm / config.cut_width_mm   # 69.0 / 63.0
        ratio_h = config.target_card_height_mm / config.cut_height_mm # 94.0 / 88.0
        target_w = int(round(orig_w * ratio_w))
        target_h = int(round(orig_h * ratio_h))
        
        pad_top = (target_h - orig_h) // 2
        pad_bottom = target_h - orig_h - pad_top
        pad_left = (target_w - orig_w) // 2
        pad_right = target_w - orig_w - pad_left

    return target_w, target_h, pad_top, pad_bottom, pad_left, pad_right

def is_card_borderless(
    img_np: np.ndarray,
    meta: Optional[Dict[str, Any]] = None
) -> Tuple[bool, np.ndarray]:
    """
    Determines whether a card is full-art / borderless using Scryfall metadata
    and edge color analysis.
    Returns: (is_borderless: bool, mean_border_color: np.ndarray)
    """
    orig_h, orig_w = img_np.shape[:2]

    corner_excl_x = int(orig_w * 0.15)
    corner_excl_y = int(orig_h * 0.15)
    sample_depth = max(4, int(orig_h * 0.006))
    sample_offset = max(3, int(orig_h * 0.005))

    top_strip = img_np[sample_offset:sample_offset + sample_depth, corner_excl_x:orig_w - corner_excl_x]
    bot_strip = img_np[orig_h - sample_offset - sample_depth:orig_h - sample_offset, corner_excl_x:orig_w - corner_excl_x]
    left_strip = img_np[corner_excl_y:orig_h - corner_excl_y, sample_offset:sample_offset + sample_depth]
    right_strip = img_np[corner_excl_y:orig_h - corner_excl_y, orig_w - sample_offset - sample_depth:orig_w - sample_offset]

    border_pixels = np.concatenate([
        top_strip.reshape(-1, 3),
        bot_strip.reshape(-1, 3),
        left_strip.reshape(-1, 3),
        right_strip.reshape(-1, 3)
    ], axis=0)

    std_dev = np.std(border_pixels, axis=0).mean()
    mean_border_color = np.median(border_pixels, axis=0)

    top_mean = top_strip.mean(axis=(0, 1))
    bot_mean = bot_strip.mean(axis=(0, 1))
    edge_delta = np.linalg.norm(top_mean - bot_mean)

    # 1. Check metadata if available
    if meta:
        border_color = (meta.get("border_color") or "").lower()
        if border_color == "borderless":
            return True, mean_border_color
        if meta.get("full_art") is True:
            return True, mean_border_color
        frame_effects = [str(f).lower() for f in meta.get("frame_effects", [])]
        if any(f in frame_effects for f in ("extendedart", "showcase", "inverted", "fullart")):
            return True, mean_border_color

    # 2. Pixel variance & edge delta heuristic
    if std_dev > 12.0 or edge_delta > 25.0:
        return True, mean_border_color

    return False, mean_border_color

def add_intelligent_bleed(
    image_input: Image.Image | str,
    output_path: str,
    config: AppConfig,
    dpi: int = 300,
    meta: Optional[Dict[str, Any]] = None
) -> str:
    """
    Intelligent Bleed Expansion 2.0:
    - Target dimensions: 63x88mm cut with 3mm bleed margin (69x94mm total).
    - Borderless Bleed 2.0:
      1. Geometric Corner Radius Mask:
         Scryfall MTG card scans have physical rounded corners with radius r ~ 28px per 1040h.
         Pixels in the 4 corners outside the effective radius are explicitly marked as non-art,
         even if the image has lost its alpha channel during upscaling.
      2. Combined Alpha Mask + Fringe Stripping:
         If an alpha channel exists, alpha > 200 is combined with the geometric mask.
      3. Scanner Edge Insetting:
         Insets outer straight edges by 3-4px to discard scanner edge black lines at row h-1.
      4. Euclidean Distance Transform Inpainting:
         Uses scipy.ndimage.distance_transform_edt to smoothly project true card artwork
         radially into both the bleed corners AND the card's rounded corner cutouts.
      5. Permanently eliminates non-extended black corners and bottom line artifacts.
    - Bordered:
      Cleans scanner halos by feathered inward painting using median border color.
    - Single-padding guarantee: Never adds double padding.
    """
    if isinstance(image_input, str):
        img = Image.open(image_input)
        close_img = True
    else:
        img = image_input
        close_img = False

    try:
        orig_w, orig_h = img.size
        target_w, target_h, pad_top, pad_bottom, pad_left, pad_right = calculate_target_dimensions(
            orig_w, orig_h, config
        )

        # Check existing cached bleed image for valid size match
        if not config.reset_bleed_cache and os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
            try:
                with Image.open(output_path) as cached_bleed:
                    if cached_bleed.size == (target_w, target_h):
                        return output_path
            except Exception:
                pass

        # Extract RGB and Alpha
        raw_rgba = np.array(img)
        if raw_rgba.ndim == 3 and raw_rgba.shape[2] == 4:
            raw_rgb = raw_rgba[:, :, :3]
            alpha = raw_rgba[:, :, 3]
        else:
            raw_rgb = np.array(img.convert("RGB"))
            alpha = None

        is_borderless, mean_border_color = is_card_borderless(raw_rgb, meta)

        if is_borderless:
            # ==================== BORDERLESS BLEED 2.0 ====================
            # 1. Exact MTG Card Geometric Corner Mask
            # Every MTG card has a standard corner radius: ~28px at 1040h (~56px at 2080h).
            r = int(round(28.0 * (orig_h / 1040.0)))
            # Effective radius slightly inset to peel away anti-aliasing edge artifacts
            effective_r = r - int(round(3.0 * (orig_h / 1040.0)))

            ys, xs = np.indices((orig_h, orig_w))
            dist_tl_sq = np.maximum(0, r - xs)**2 + np.maximum(0, r - ys)**2
            dist_tr_sq = np.maximum(0, xs - (orig_w - 1 - r))**2 + np.maximum(0, r - ys)**2
            dist_bl_sq = np.maximum(0, r - xs)**2 + np.maximum(0, ys - (orig_h - 1 - r))**2
            dist_br_sq = np.maximum(0, xs - (orig_w - 1 - r))**2 + np.maximum(0, ys - (orig_h - 1 - r))**2

            geometric_card_mask = (
                (dist_tl_sq <= effective_r**2) &
                (dist_tr_sq <= effective_r**2) &
                (dist_bl_sq <= effective_r**2) &
                (dist_br_sq <= effective_r**2)
            )

            # 2. Combined with Alpha Mask (if available)
            if alpha is not None:
                alpha_mask = (alpha > 200)
                card_valid = geometric_card_mask & alpha_mask
            else:
                card_valid = geometric_card_mask

            # 3. Inset outer straight edges by 3-4px to discard scanner edge black lines
            inset = max(3, int(round(orig_h * 0.004)))
            card_valid[:inset, :] = False
            card_valid[-inset:, :] = False
            card_valid[:, :inset] = False
            card_valid[:, -inset:] = False

            # 4. Map valid mask onto padded target canvas
            valid_canvas_mask = np.zeros((target_h, target_w), dtype=bool)
            valid_canvas_mask[pad_top:pad_top + orig_h, pad_left:pad_left + orig_w] = card_valid

            # 5. Initialize canvas with original card art
            canvas_rgb = np.zeros((target_h, target_w, 3), dtype=np.uint8)
            canvas_rgb[pad_top:pad_top + orig_h, pad_left:pad_left + orig_w] = raw_rgb

            # 6. Euclidean distance transform finds nearest valid art pixel for every
            #    bleed pixel, extended corner, and rounded corner cutout
            _, (nearest_y, nearest_x) = ndi.distance_transform_edt(~valid_canvas_mask, return_indices=True)
            padded_np = canvas_rgb[nearest_y, nearest_x]

        else:
            # ==================== BORDERED INWARD CLEANING ====================
            padded_np = np.full((target_h, target_w, 3), mean_border_color, dtype=np.float32)
            img_np = raw_rgb.astype(np.float32)

            ys = np.arange(orig_h)
            xs = np.arange(orig_w)
            dist_y = np.minimum(ys, orig_h - 1 - ys)
            dist_x = np.minimum(xs, orig_w - 1 - xs)
            dist_edge = np.minimum(dist_y[:, None], dist_x[None, :]).astype(np.float32)

            corner_scale = orig_h / 1040.0
            corner_radius = 45.0 * corner_scale
            corner_inward_boost = 22.0 * corner_scale
            corner_dist = np.sqrt(dist_y[:, None]**2 + dist_x[None, :]**2)
            corner_mask = np.clip(1.0 - (corner_dist / corner_radius), 0.0, 1.0)
            
            inward_threshold = (8.0 * corner_scale) + (corner_mask * corner_inward_boost)
            feather_width = 7.0 * corner_scale

            alpha_feather = np.clip((dist_edge - inward_threshold) / feather_width, 0.0, 1.0)[:, :, None]
            blended_card = (img_np * alpha_feather) + (mean_border_color * (1.0 - alpha_feather))
            padded_np[pad_top:pad_top + orig_h, pad_left:pad_left + orig_w] = blended_card
            padded_np = np.clip(padded_np, 0, 255).astype(np.uint8)

        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        padded_img = Image.fromarray(padded_np)
        # JPEG, not PNG: ReportLab embeds a JPEG by copying its already-compressed
        # bytes almost for free, but a PNG must be fully decoded and re-Flate-
        # compressed for every unique card at PDF-build time -- the actual cost
        # behind a slow "Compiling PDF..." step on a large batch. Quality 95 is
        # visually lossless at print resolution and is standard for MPC proxies;
        # `output_path` must end in .jpg/.jpeg for ReportLab's fast path to apply.
        padded_img.save(output_path, "JPEG", dpi=(dpi, dpi), quality=95)

    finally:
        if close_img:
            img.close()

    return output_path
