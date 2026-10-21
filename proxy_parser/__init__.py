"""
MTG Moxfield Proxy to High-Resolution MPC-Ready PDF Parser Package

Attributes are loaded lazily (PEP 562): nothing in this package eagerly imports
torch, Pillow, reportlab, numpy or scipy. Those only load the moment something
actually accesses a name that needs them (e.g. ``proxy_parser.ScryfallClient``).

This matters because the web portal imports ``proxy_parser.checker`` directly,
and Python always runs a package's ``__init__.py`` before its submodules. If this
file eagerly imported the print pipeline, the portal -- which never installs
torch/reportlab/Pillow/numpy/scipy (see requirements-web.txt) -- would fail to
import the checker at all. `moxfield_mpc_to_pdf.py` and the worker are unaffected:
they access the heavy names directly, which still imports the real modules.
"""
import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import AppConfig, parse_arguments
    from .deck_parser import CardEntry, Deck, parse_filename, parse_moxfield_file
    from .scryfall_client import ScryfallClient
    from .upscaler import RealESRGANUpscaler, get_torch_device
    from .formatter import CardFrame, CardType, redact_copyright
    from .bleed import add_intelligent_bleed, calculate_target_dimensions
    from .divider import generate_divider_card
    from .gdrive import GDriveManager
    from .pdf_builder import PDFBatchBuilder, write_summary_csv
    from .progress import create_progress_bar, console

__all__ = [
    "AppConfig",
    "parse_arguments",
    "CardEntry",
    "Deck",
    "parse_filename",
    "parse_moxfield_file",
    "ScryfallClient",
    "RealESRGANUpscaler",
    "get_torch_device",
    "AVAILABLE_MODELS",
    "list_available_models",
    "CardFrame",
    "CardType",
    "redact_copyright",
    "add_intelligent_bleed",
    "calculate_target_dimensions",
    "process_custom_cardback",
    "generate_divider_card",
    "generate_divider_cards",
    "optimize_suborders",
    "GDriveManager",
    "PDFBatchBuilder",
    "write_summary_csv",
    "create_progress_bar",
    "console",
]

# name -> submodule that defines it. Kept explicit (rather than importlib scanning)
# so a typo here fails loudly at first use instead of silently.
_ATTR_SOURCE = {
    "AppConfig": "config",
    "parse_arguments": "config",
    "CardEntry": "deck_parser",
    "Deck": "deck_parser",
    "parse_filename": "deck_parser",
    "parse_moxfield_file": "deck_parser",
    "ScryfallClient": "scryfall_client",
    "RealESRGANUpscaler": "upscaler",
    "get_torch_device": "upscaler",
    "AVAILABLE_MODELS": "upscaler",
    "list_available_models": "upscaler",
    "CardFrame": "formatter",
    "CardType": "formatter",
    "redact_copyright": "formatter",
    "add_intelligent_bleed": "bleed",
    "calculate_target_dimensions": "bleed",
    "process_custom_cardback": "bleed",
    "generate_divider_card": "divider",
    "generate_divider_cards": "divider",
    "optimize_suborders": "grouping",
    "GDriveManager": "gdrive",
    "PDFBatchBuilder": "pdf_builder",
    "write_summary_csv": "pdf_builder",
    "create_progress_bar": "progress",
    "console": "progress",
}


def __getattr__(name: str):
    module_name = _ATTR_SOURCE.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(f".{module_name}", __name__)
    value = getattr(module, name)
    globals()[name] = value  # cache on the package so later access skips __getattr__
    return value


def __dir__():
    return sorted(__all__)
