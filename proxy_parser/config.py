import os
import argparse
from dataclasses import dataclass
from typing import Optional

@dataclass
class AppConfig:
    # Mode & Quality
    mode: str = "scryfall"             # "scryfall" or "upscaled"
    upscale_model: str = "4x-UltraSharp" # Model ID from available models registry
    upscale_factor: int = 2            # 2 (for ~600 DPI) or 4
    remove_copyright: bool = False     # Toggle off by default per user requirement
    device: str = "auto"               # "auto", "mps", "cuda", "cpu"
    
    # Target Card Dimensions (in mm)
    # Printer specification: 63x88mm cut size with 3mm bleed -> 69x94mm total
    cut_width_mm: float = 63.0
    cut_height_mm: float = 88.0
    bleed_margin_mm: float = 3.0       # Standard 3mm bleed margin per side
    
    # Bleed Configuration Modes: "target_mm", "fixed_pixels", "fixed_percentage", "target_inches"
    bleed_mode: str = "target_mm"
    bleed_pixels: int = 36             # For fixed_pixels mode
    bleed_percentage: float = 5.0      # For fixed_percentage mode
    reset_bleed_cache: bool = False    # Force recalculation of all bleed images
    
    # PDF Output & Breaking
    # Modes: "none", "name", "document", "count"
    break_by: str = "none"
    break_count: int = 100             # Max cards per PDF if break_by == "count"
    output_pdf: str = "all_cards_individual_pages.pdf"
    output_csv: str = "card_totals_summary.csv"
    
    # File Paths & Directories
    input_dir: str = "input"
    backs_dir: str = "backs"
    default_cardback: str = os.path.join("backs", "proxy1.jpg")
    output_dir: str = "."
    cache_dir: str = "scryfall_cache"
    upscaled_cache_dir: str = "upscaled_cache"
    weights_dir: str = "weights"
    # Divider Cards Configuration
    two_divider_cards: bool = True     # 2 cards per list: 1=Identity (Person large), 2=Details (card count & specs)

    # Custom Card Back Processing Toggles
    custom_back_check_ratio: bool = True  # Center-crop non-standard ratios to 63:88 standard
    custom_back_upscale: bool = True      # Upscale low-res card backs to print DPI match
    custom_back_add_bleed: bool = True    # Add intelligent bleed extension (3mm)

    # Smart Suborder Grouping for Sheet Cut & Stack
    # Minimizes sheet waste and blade draw on large proxy orders
    enable_suborders: bool = False
    cards_per_sheet: int = 18          # Default 18 cards/sheet (3x6), supports 16 (4x4) or custom
    target_suborder_cards: int = 1440  # Target proxy count per suborder batch (e.g. 1440 = 18*80 sheets)
    
    # UI Mode
    open_ui: bool = False

    # Google Drive Integration
    gdrive_dir: Optional[str] = None   # Path or "auto"
    offload_cache: bool = False        # Store/move image cache to Google Drive

    @property
    def target_card_width_mm(self) -> float:
        return self.cut_width_mm + (2 * self.bleed_margin_mm)

    @property
    def target_card_height_mm(self) -> float:
        return self.cut_height_mm + (2 * self.bleed_margin_mm)

    @property
    def target_card_width_inch(self) -> float:
        return self.target_card_width_mm / 25.4

    @property
    def target_card_height_inch(self) -> float:
        return self.target_card_height_mm / 25.4

    def set_cache_base_dir(self, path: str) -> None:
        """
        Configures cache directories, supporting external drive roots or specific folders.
        """
        if not path or not str(path).strip():
            return
        path = os.path.abspath(str(path).strip())
        base_name = os.path.basename(path.rstrip("/\\"))
        if base_name == "scryfall_cache":
            self.cache_dir = path
            self.upscaled_cache_dir = os.path.join(os.path.dirname(path), "upscaled_cache")
        elif base_name == "upscaled_cache":
            self.cache_dir = os.path.join(os.path.dirname(path), "scryfall_cache")
            self.upscaled_cache_dir = path
        elif os.path.isdir(os.path.join(path, "scryfall_cache")) or os.path.isdir(os.path.join(path, "upscaled_cache")):
            self.cache_dir = os.path.join(path, "scryfall_cache")
            self.upscaled_cache_dir = os.path.join(path, "upscaled_cache")
        else:
            try:
                if os.path.isdir(path) and any(f.endswith("_meta.json") or f.endswith("_front.png") for f in os.listdir(path) if os.path.isfile(os.path.join(path, f))):
                    self.cache_dir = path
                    self.upscaled_cache_dir = os.path.join(path, "upscaled_cache")
                else:
                    self.cache_dir = os.path.join(path, "scryfall_cache")
                    self.upscaled_cache_dir = os.path.join(path, "upscaled_cache")
            except Exception:
                self.cache_dir = os.path.join(path, "scryfall_cache")
                self.upscaled_cache_dir = os.path.join(path, "upscaled_cache")
        os.makedirs(self.cache_dir, exist_ok=True)
        os.makedirs(self.upscaled_cache_dir, exist_ok=True)

def parse_arguments() -> AppConfig:
    parser = argparse.ArgumentParser(
        description="MTG Moxfield Proxy to High-Resolution MPC-Ready PDF Generator",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )

    # Core mode & upscale options
    parser.add_argument(
        "--mode",
        choices=["scryfall", "upscaled"],
        default="scryfall",
        help="Processing mode: 'scryfall' for fast high-res Scryfall scans, 'upscaled' for Real-ESRGAN upscaling"
    )
    parser.add_argument(
        "--upscale-model",
        type=str,
        default="4x-UltraSharp",
        help="Super-resolution AI model to use (e.g. '4x-UltraSharp', '4x_Remacri', '4x_NMKD-Superscale', '4x-AnimeSharp', 'RealESRGAN_x4plus', 'RealESRGAN_x2')"
    )
    parser.add_argument(
        "--upscale-factor",
        type=int,
        choices=[2, 4],
        default=2,
        help="Upscale magnification factor (2 = ~600 DPI, 4 = ultra-high DPI)"
    )
    parser.add_argument(
        "--remove-copyright",
        action="store_true",
        default=False,
        help="Remove copyright line and legal notice from card bottom (default: False/keep copyright)"
    )
    parser.add_argument(
        "--device",
        choices=["auto", "mps", "cuda", "cpu"],
        default="auto",
        help="Compute device for upscaling (auto selects MPS on Apple Silicon, CUDA on Nvidia, else CPU)"
    )

    # Dimension & Bleed options (default in mm)
    parser.add_argument(
        "--cut-width-mm",
        type=float,
        default=63.0,
        help="Target card cut width in mm"
    )
    parser.add_argument(
        "--cut-height-mm",
        type=float,
        default=88.0,
        help="Target card cut height in mm"
    )
    parser.add_argument(
        "--bleed-margin-mm",
        type=float,
        default=3.0,
        help="Bleed margin per side in mm (default: 3mm -> 69x94mm total)"
    )
    parser.add_argument(
        "--bleed-mode",
        choices=["target_mm", "fixed_pixels", "fixed_percentage", "target_inches"],
        default="target_mm",
        help="Bleed sizing method: 'target_mm' (69x94mm), 'fixed_pixels', or 'fixed_percentage'"
    )
    parser.add_argument(
        "--bleed-pixels",
        type=int,
        default=36,
        help="Padding in pixels per edge when using --bleed-mode fixed_pixels"
    )
    parser.add_argument(
        "--bleed-percentage",
        type=float,
        default=5.0,
        help="Percentage margin added to card dimensions when using --bleed-mode fixed_percentage"
    )
    parser.add_argument(
        "--reset-bleed-cache",
        action="store_true",
        default=False,
        help="Force regeneration of all bleed images (useful when switching dimensions or updating bleed algorithm)"
    )

    # Output splitting options
    parser.add_argument(
        "--break-by",
        choices=["none", "name", "document", "count"],
        default="none",
        help="Break output PDFs by: 'none' (single PDF), 'name' (person), 'document' (input txt file), or 'count' (N cards per PDF)"
    )
    parser.add_argument(
        "--break-count",
        type=int,
        default=100,
        help="Number of cards per PDF when using --break-by count"
    )

    # Google Drive & Storage
    parser.add_argument(
        "--gdrive-dir",
        type=str,
        default=None,
        help="Path to Google Drive folder for output/cache storage, or 'auto' to auto-detect macOS GoogleDrive mount"
    )
    parser.add_argument(
        "--offload-cache",
        action="store_true",
        default=False,
        help="Store or offload high-res cached card images directly to Google Drive to avoid filling local disk"
    )

    # Directories & Files
    parser.add_argument("--input-dir", type=str, default="input", help="Directory containing decklist .txt files")
    parser.add_argument("--backs-dir", type=str, default="backs", help="Directory containing card back images")
    parser.add_argument("--cardback", type=str, default=os.path.join("backs", "proxy1.jpg"), help="Default cardback image path")
    parser.add_argument("--cache-dir", type=str, default="scryfall_cache", help="Directory for Scryfall card downloads, meta, and bleed cache")
    parser.add_argument("--upscaled-cache-dir", type=str, default="upscaled_cache", help="Directory for upscaled card cache")
    parser.add_argument("--weights-dir", type=str, default="weights", help="Directory containing AI model weights")
    parser.add_argument("--output-pdf", type=str, default="all_cards_individual_pages.pdf", help="Output PDF name/path template")
    parser.add_argument("--output-csv", type=str, default="card_totals_summary.csv", help="Output summary CSV name/path")
    # Divider & Suborder Options
    parser.add_argument(
        "--two-divider-cards",
        action="store_true",
        default=True,
        help="Generate two divider cards per deck: 1=Identity (Person large), 2=Details (card count & specs)"
    )
    parser.add_argument(
        "--single-divider-card",
        action="store_false",
        dest="two_divider_cards",
        help="Use a single divider card instead of two"
    )
    parser.add_argument(
        "--enable-suborders",
        action="store_true",
        default=False,
        help="Partition print order into optimal suborders to prevent paper waste and blade draw on large runs"
    )
    parser.add_argument(
        "--cards-per-sheet",
        type=int,
        default=18,
        help="Number of cards per physical print sheet (default: 18 for 3x6, or 16 for 4x4)"
    )
    parser.add_argument(
        "--target-suborder-cards",
        type=int,
        default=1440,
        help="Target card count per suborder batch (e.g. 1440 cards = 80 sheets @ 18/sheet)"
    )
    parser.add_argument(
        "--ui",
        action="store_true",
        default=False,
        help="Launch the local graphical web interface to configure parameters, select decklists, and monitor progress"
    )
    # Custom Card Back options
    parser.add_argument(
        "--no-custom-back-check-ratio",
        action="store_false",
        dest="custom_back_check_ratio",
        default=True,
        help="Disable automatic aspect ratio check and center-cropping for custom card backs"
    )
    parser.add_argument(
        "--no-custom-back-upscale",
        action="store_false",
        dest="custom_back_upscale",
        default=True,
        help="Disable upscaling for low-resolution custom card backs"
    )
    parser.add_argument(
        "--no-custom-back-bleed",
        action="store_false",
        dest="custom_back_add_bleed",
        default=True,
        help="Disable adding intelligent bleed margin to custom card backs"
    )
    parser.add_argument(
        "--info",
        action="store_true",
        default=False,
        help="Display info documentation and exit"
    )

    args = parser.parse_args()

    if getattr(args, "info", False):
        info_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "info.md")
        if not os.path.exists(info_path):
            info_path = "info.md"
        import sys
        from rich.console import Console
        from rich.markdown import Markdown
        c = Console()
        if os.path.exists(info_path):
            with open(info_path, "r", encoding="utf-8") as f:
                c.print(Markdown(f.read()))
        else:
            c.print("[yellow]info.md not found.[/yellow]")
        sys.exit(0)

    return AppConfig(
        mode=args.mode,
        upscale_model=getattr(args, "upscale_model", "4x-UltraSharp"),
        upscale_factor=args.upscale_factor,
        remove_copyright=args.remove_copyright,
        device=args.device,
        cut_width_mm=args.cut_width_mm,
        cut_height_mm=args.cut_height_mm,
        bleed_margin_mm=args.bleed_margin_mm,
        bleed_mode=args.bleed_mode,
        bleed_pixels=args.bleed_pixels,
        bleed_percentage=args.bleed_percentage,
        reset_bleed_cache=args.reset_bleed_cache,
        break_by=args.break_by,
        break_count=args.break_count,
        two_divider_cards=args.two_divider_cards,
        custom_back_check_ratio=args.custom_back_check_ratio,
        custom_back_upscale=args.custom_back_upscale,
        custom_back_add_bleed=args.custom_back_add_bleed,
        enable_suborders=args.enable_suborders,
        cards_per_sheet=args.cards_per_sheet,
        target_suborder_cards=args.target_suborder_cards,
        open_ui=args.ui,
        gdrive_dir=args.gdrive_dir,
        offload_cache=args.offload_cache,
        input_dir=args.input_dir,
        cache_dir=args.cache_dir,
        upscaled_cache_dir=args.upscaled_cache_dir,
        backs_dir=args.backs_dir,
        default_cardback=args.cardback,
        weights_dir=getattr(args, "weights_dir", "weights"),
        output_pdf=args.output_pdf,
        output_csv=args.output_csv
    )

