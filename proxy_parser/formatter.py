import enum
import numpy as np
from PIL import Image
from typing import Optional, Tuple, Dict, Any

class CardFrame(enum.Enum):
    MODERN = 1
    OTHER = 2

    @classmethod
    def from_metadata(cls, card_data: Dict[str, Any]) -> "CardFrame":
        frame = card_data.get("frame", "")
        if frame in ("2015", "modern"):
            return CardFrame.MODERN
        return CardFrame.OTHER

class CardType(enum.Enum):
    CREATURE = 1
    PLANESWALKER = 2
    OTHER = 3

    @classmethod
    def from_metadata(cls, card_data: Dict[str, Any], face_idx: Optional[int] = None) -> "CardType":
        target = card_data
        if face_idx is not None and "card_faces" in card_data and len(card_data["card_faces"]) > face_idx:
            target = card_data["card_faces"][face_idx]

        type_line = (target.get("type_line") or "").lower()
        if "creature" in type_line or ("power" in target and "toughness" in target):
            return CardType.CREATURE
        elif "planeswalker" in type_line or "loyalty" in target:
            return CardType.PLANESWALKER
        return CardType.OTHER

class RedactBoxType(enum.Enum):
    MODERN_COPYRIGHT_DEFAULT = (430, 700, 970, 1040)
    MODERN_COPYRIGHT_CREATURE = (430, 700, 990, 1040)
    MODERN_COPYRIGHT_CREATURE_EXTRA_UB = (430, 575, 970, 1040)
    MODERN_COPYRIGHT_PLANESWALKER = (430, 700, 990, 1040)

    def get_box(self, scale: float = 1.0) -> Tuple[int, int, int, int]:
        return tuple(int(round(coord * scale)) for coord in self.value)

def is_universes_beyond(card_data: Dict[str, Any]) -> bool:
    promo_types = card_data.get("promo_types", [])
    if "universesbeyond" in promo_types or "universes-beyond" in promo_types:
        return True
    security_stamp = card_data.get("security_stamp", "")
    if security_stamp in ("triangle", "acorn"):
        return True
    return False

def redact_copyright(
    image_np: np.ndarray,
    card_data: Dict[str, Any],
    face_idx: Optional[int] = None
) -> np.ndarray:
    """
    Redacts the copyright line by sampling border color and painting over the
    standard Scryfall copyright region.
    """
    h, w = image_np.shape[:2]
    # Scryfall base size is 745 x 1040
    scale = h / 1040.0

    frame = CardFrame.from_metadata(card_data)
    card_type = CardType.from_metadata(card_data, face_idx)
    ub = is_universes_beyond(card_data)

    # Sample bottom border band as the fill color
    bottom_y_start = max(0, h - int(32 * scale))
    x_start = int(200 * scale)
    x_end = max(x_start + 10, w - int(200 * scale))
    border_band = image_np[bottom_y_start:, x_start:x_end]
    if border_band.size > 0:
        border_color = np.median(border_band, axis=(0, 1))
    else:
        border_color = np.array([0, 0, 0], dtype=np.uint8)

    if card_type == CardType.CREATURE:
        box = RedactBoxType.MODERN_COPYRIGHT_CREATURE.get_box(scale)
        box_ub = RedactBoxType.MODERN_COPYRIGHT_CREATURE_EXTRA_UB.get_box(scale) if ub else None
    elif card_type == CardType.PLANESWALKER:
        box = RedactBoxType.MODERN_COPYRIGHT_PLANESWALKER.get_box(scale)
        box_ub = None
    else:
        box = RedactBoxType.MODERN_COPYRIGHT_DEFAULT.get_box(scale)
        box_ub = None

    left, right, top, bottom = box
    left = max(0, min(left, w))
    right = max(0, min(right, w))
    top = max(0, min(top, h))
    bottom = max(0, min(bottom, h))
    image_np[top:bottom, left:right, :] = border_color

    if box_ub:
        left, right, top, bottom = box_ub
        left = max(0, min(left, w))
        right = max(0, min(right, w))
        top = max(0, min(top, h))
        bottom = max(0, min(bottom, h))
        image_np[top:bottom, left:right, :] = border_color

    return image_np
