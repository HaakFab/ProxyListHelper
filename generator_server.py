#!/usr/bin/env python3
"""
Local Pre-Press Web GUI Server for MTG Moxfield Proxy to PDF Generator.
Runs a zero-dependency ThreadingHTTPServer on localhost:8008.
Provides parameter setup, online backend sync, decklist selection checklist,
suborder cut-stack optimization preview, and real-time live progress monitoring.
"""

import os
import sys
import json
import time
import glob
import shutil
import zipfile
import threading
import subprocess
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from typing import Dict, List, Any, Optional, Tuple

from proxy_parser.config import AppConfig
from proxy_parser.deck_parser import parse_moxfield_file, Deck
from proxy_parser.grouping import optimize_suborders
from proxy_parser.upscaler import list_available_models

PORT = 8008

# Global execution state
_STATE_LOCK = threading.Lock()
_RUN_STATE: Dict[str, Any] = {
    "job_status": "idle",     # idle, running, completed, error
    "stage": "idle",          # idle, scryfall, upscale, dividers, pdf, done
    "progress": {
        "current": 0,
        "total": 0,
        "percent": 0.0,
        "message": "Ready to configure and start print run."
    },
    "logs": [],
    "results": None,
    "error": None,
    "cancel_requested": False
}

def log_event(message: str):
    """Appends a timestamped log to the shared state."""
    timestamp = time.strftime("%H:%M:%S")
    entry = f"[{timestamp}] {message}"
    with _STATE_LOCK:
        _RUN_STATE["logs"].append(entry)
        if len(_RUN_STATE["logs"]) > 1000:
            _RUN_STATE["logs"].pop(0)

def update_progress(stage: str, current: int, total: int, percent: float, message: str):
    """Updates progress status in shared state."""
    with _STATE_LOCK:
        _RUN_STATE["stage"] = stage
        _RUN_STATE["progress"] = {
            "current": current,
            "total": total,
            "percent": round(percent, 1),
            "message": message
        }
    log_event(message)

def scan_local_decks(input_dir: str = "input") -> List[Dict[str, Any]]:
    """Scans local directory for deck files and companion cardbacks, including nested folders and export ZIP contents."""
    results = []
    if not os.path.exists(input_dir):
        try:
            os.makedirs(input_dir, exist_ok=True)
        except Exception:
            pass
        return results

    input_dir = os.path.abspath(input_dir)

    # Check potential sub-roots (e.g. if pointing to root of an unzipped export that has a 'decks/' subfolder)
    scan_roots = [input_dir]
    decks_sub = os.path.join(input_dir, "decks")
    if os.path.isdir(decks_sub):
        scan_roots.append(decks_sub)

    seen_paths = set()
    entries = []

    def find_companion_back(folder: str, stem: str = "") -> Optional[str]:
        exts = [".jpg", ".jpeg", ".png", ".webp"]
        if stem:
            for ext in exts:
                cand = os.path.join(folder, f"{stem}_back{ext}")
                if os.path.exists(cand):
                    return cand
        for ext in exts:
            for b_name in ["back", "cardback", "custom_back", "CardBack"]:
                cand = os.path.join(folder, f"{b_name}{ext}")
                if os.path.exists(cand):
                    return cand
        try:
            imgs = [os.path.join(folder, f) for f in os.listdir(folder) if os.path.splitext(f)[1].lower() in exts]
            if len(imgs) == 1:
                return imgs[0]
        except Exception:
            pass
        return None

    for root in scan_roots:
        try:
            items = sorted(os.listdir(root))
        except Exception:
            continue

        for item in items:
            item_path = os.path.join(root, item)
            # 1. Direct .txt files
            if os.path.isfile(item_path) and item.endswith(".txt"):
                if item_path in seen_paths or item.lower().startswith("summary") or item == "requirements.txt":
                    continue
                seen_paths.add(item_path)
                stem = os.path.splitext(item)[0]
                cb = find_companion_back(root, stem=stem)
                entries.append((item_path, cb))

            # 2. Subdirectories
            elif os.path.isdir(item_path):
                if item == "decks" or item.startswith("."):
                    continue
                sub_txts = sorted(glob.glob(os.path.join(item_path, "*.txt")))
                sub_txts = [f for f in sub_txts if not os.path.basename(f).lower().startswith("summary")]
                if sub_txts:
                    decklist_file = os.path.join(item_path, "decklist.txt")
                    chosen_txt = decklist_file if os.path.exists(decklist_file) else sub_txts[0]
                    if chosen_txt not in seen_paths:
                        seen_paths.add(chosen_txt)
                        cb = find_companion_back(item_path)
                        entries.append((chosen_txt, cb))

    for path, custom_back in entries:
        try:
            deck = parse_moxfield_file(path)
            results.append({
                "path": path,
                "person": deck.person,
                "label": deck.label,
                "card_count": deck.total_cards,
                "has_custom_back": bool(custom_back),
                "custom_back_path": custom_back,
                "source": "local"
            })
        except Exception as ex:
            results.append({
                "path": path,
                "person": "Unknown",
                "label": os.path.basename(path),
                "card_count": 0,
                "has_custom_back": False,
                "custom_back_path": None,
                "source": "local",
                "error": str(ex)
            })
    return results

def run_worker_task(config: AppConfig, selected_deck_paths: Optional[List[str]] = None, custom_back_map: Optional[Dict[str, str]] = None):
    """Executes the print run pipeline in a background thread with full error isolation."""
    with _STATE_LOCK:
        _RUN_STATE["job_status"] = "running"
        _RUN_STATE["stage"] = "starting"
        _RUN_STATE["error"] = None
        _RUN_STATE["results"] = None
        _RUN_STATE["cancel_requested"] = False
        _RUN_STATE["progress"] = {
            "current": 0,
            "total": 0,
            "percent": 0.0,
            "message": "Initializing generator pipeline..."
        }

    log_event("Starting print run pipeline...")
    log_event(f"Mode: {config.mode} | Model: {getattr(config, 'upscale_model', '4x-UltraSharp')} | Scale: x{config.upscale_factor} | Device: {config.device}")
    log_event(f"Suborders: {config.enable_suborders} (Sheet: {config.cards_per_sheet}, Target: {config.target_suborder_cards})")
    log_event(f"Two Divider Cards: {config.two_divider_cards} (Stripes: White/Red/Yellow/Blue)")
    log_event(f"Custom Back Options -> Check Ratio: {getattr(config, 'custom_back_check_ratio', True)} | Upscale: {getattr(config, 'custom_back_upscale', True)} | Add Bleed: {getattr(config, 'custom_back_add_bleed', True)}")

    try:
        from moxfield_mpc_to_pdf import run_pipeline

        # Prepare specific deck entries if selected
        specific_entries = None
        if selected_deck_paths:
            specific_entries = []
            cb_map = custom_back_map or {}
            for path in selected_deck_paths:
                custom_back = cb_map.get(path)
                if not custom_back:
                    parent_dir = os.path.dirname(os.path.abspath(path))
                    stem = os.path.splitext(os.path.basename(path))[0]
                    for ext in [".jpg", ".jpeg", ".png", ".webp"]:
                        for b_name in ["back", "cardback", "custom_back", f"{stem}_back"]:
                            cand = os.path.join(parent_dir, f"{b_name}{ext}")
                            if os.path.exists(cand):
                                custom_back = cand
                                break
                        if custom_back:
                            break
                specific_entries.append((path, custom_back))

        def on_progress(stage, current, total, percent, msg):
            update_progress(stage, current, total, percent, msg)

        def on_log(msg):
            log_event(msg)

        results = run_pipeline(
            config=config,
            specific_entries=specific_entries,
            progress_callback=on_progress,
            log_callback=on_log,
            is_cancelled=lambda: _RUN_STATE.get("cancel_requested", False)
        )

        with _STATE_LOCK:
            if results.get("success", False):
                _RUN_STATE["job_status"] = "completed"
                _RUN_STATE["stage"] = "done"
                _RUN_STATE["results"] = results
                _RUN_STATE["progress"]["percent"] = 100.0
                _RUN_STATE["progress"]["message"] = "Print Run Completed Successfully!"
                log_event("Print Run Completed Successfully! Output files are ready.")
            else:
                _RUN_STATE["job_status"] = "error"
                _RUN_STATE["error"] = results.get("error", "Unknown pipeline error")
                log_event(f"Print Run Failed: {_RUN_STATE['error']}")

    except Exception as ex:
        import traceback
        tb = traceback.format_exc()
        with _STATE_LOCK:
            _RUN_STATE["job_status"] = "error"
            _RUN_STATE["error"] = str(ex)
            _RUN_STATE["stage"] = "error"
            _RUN_STATE["progress"]["message"] = f"Pipeline execution failed: {ex}"
        log_event(f"Fatal Exception: {ex}")
        log_event(tb)

class GeneratorRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Suppress noisy standard HTTP logs in console
        return

    def send_json_response(self, data: Any, status: int = 200):
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Admin-Password")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ["/", "/index.html"]:
            self.serve_html_ui()
            return

        if path == "/api/status":
            with _STATE_LOCK:
                data = dict(_RUN_STATE)
            self.send_json_response(data)
            return

        if path == "/api/config":
            cfg = AppConfig()
            self.send_json_response({
                "mode": cfg.mode,
                "upscale_model": getattr(cfg, "upscale_model", "4x-UltraSharp"),
                "upscale_factor": cfg.upscale_factor,
                "device": cfg.device,
                "cut_width_mm": cfg.cut_width_mm,
                "cut_height_mm": cfg.cut_height_mm,
                "bleed_margin_mm": cfg.bleed_margin_mm,
                "bleed_mode": cfg.bleed_mode,
                "break_by": cfg.break_by,
                "two_divider_cards": getattr(cfg, "two_divider_cards", True),
                "custom_back_check_ratio": getattr(cfg, "custom_back_check_ratio", True),
                "custom_back_upscale": getattr(cfg, "custom_back_upscale", True),
                "custom_back_add_bleed": getattr(cfg, "custom_back_add_bleed", True),
                "enable_suborders": getattr(cfg, "enable_suborders", False),
                "cards_per_sheet": getattr(cfg, "cards_per_sheet", 18),
                "target_suborder_cards": getattr(cfg, "target_suborder_cards", 1440),
                "remove_copyright": cfg.remove_copyright,
                "cache_dir": getattr(cfg, "cache_dir", "scryfall_cache"),
                "upscaled_cache_dir": getattr(cfg, "upscaled_cache_dir", "upscaled_cache")
            })
            return

        if path == "/api/upscale-models":
            cfg = AppConfig()
            models = list_available_models(getattr(cfg, "weights_dir", "weights"))
            self.send_json_response({"models": models})
            return

        if path == "/api/local-decks":
            query_params = parse_qs(parsed.query)
            target_dir = query_params.get("dir", ["input"])[0]
            decks = scan_local_decks(target_dir)
            self.send_json_response({"decks": decks, "input_dir": target_dir})
            return

        if path == "/api/events":
            self.serve_sse_events()
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b"{}"
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            payload = {}

        if path == "/api/browse-directory":
            chosen_path = None
            initial_dir = payload.get("initial_dir", "input").strip() or "input"
            prompt_title = payload.get("prompt", "Select Decklists Folder:").strip() or "Select Decklists Folder:"
            scan_decks = bool(payload.get("scan_decks", True))
            if not os.path.isabs(initial_dir):
                initial_dir = os.path.abspath(initial_dir)
            if os.path.isfile(initial_dir):
                initial_dir = os.path.dirname(initial_dir)
            if not os.path.isdir(initial_dir):
                initial_dir = os.path.abspath(".")
            os.makedirs(initial_dir, exist_ok=True)

            escaped_dir = initial_dir.replace('"', '\\"')
            escaped_prompt = prompt_title.replace('"', '\\"')

            if sys.platform == "darwin":
                try:
                    script = (
                        f'try\n'
                        f'  set startFolder to POSIX file "{escaped_dir}" as alias\n'
                        f'  POSIX path of (choose folder with prompt "{escaped_prompt}" default location startFolder)\n'
                        f'on error\n'
                        f'  POSIX path of (choose folder with prompt "{escaped_prompt}")\n'
                        f'end try'
                    )
                    cmd = ["osascript", "-e", script]
                    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                    if res.returncode == 0:
                        chosen_path = res.stdout.strip().rstrip("/")
                except Exception as ex:
                    log_event(f"Browse directory error: {ex}")

            if not chosen_path:
                self.send_json_response({"cancelled": True})
                return

            if scan_decks:
                decks = scan_local_decks(chosen_path)
                log_event(f"Selected directory '{chosen_path}' with {len(decks)} decklist(s)")
                self.send_json_response({"path": chosen_path, "decks": decks})
            else:
                log_event(f"Selected folder '{chosen_path}'")
                self.send_json_response({"path": chosen_path})
            return

        if path == "/api/browse-zip":
            chosen_path = None
            initial_dir = payload.get("initial_dir", "input").strip() or "input"
            if not os.path.isabs(initial_dir):
                initial_dir = os.path.abspath(initial_dir)
            if os.path.isfile(initial_dir):
                initial_dir = os.path.dirname(initial_dir)
            if not os.path.isdir(initial_dir):
                initial_dir = os.path.abspath("input")
            os.makedirs(initial_dir, exist_ok=True)

            escaped_dir = initial_dir.replace('"', '\\"')

            if sys.platform == "darwin":
                try:
                    script = (
                        f'try\n'
                        f'  set startFolder to POSIX file "{escaped_dir}" as alias\n'
                        f'  POSIX path of (choose file of type {{"zip"}} with prompt "Select Server Export ZIP:" default location startFolder)\n'
                        f'on error\n'
                        f'  POSIX path of (choose file of type {{"zip"}} with prompt "Select Server Export ZIP:")\n'
                        f'end try'
                    )
                    cmd = ["osascript", "-e", script]
                    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                    if res.returncode == 0:
                        chosen_path = res.stdout.strip()
                except Exception as ex:
                    log_event(f"Browse ZIP error: {ex}")

            if not chosen_path:
                self.send_json_response({"cancelled": True})
                return

            self.send_json_response({"path": chosen_path})
            return

        if path == "/api/import-local-zip":
            zip_path = payload.get("zip_path", "").strip()
            target_dir = payload.get("target_dir", "input").strip() or "input"
            if not zip_path or not os.path.exists(zip_path):
                self.send_json_response({"error": f"ZIP file not found: {zip_path}"}, status=400)
                return

            try:
                os.makedirs(target_dir, exist_ok=True)
                with zipfile.ZipFile(zip_path, "r") as zf:
                    for member in zf.namelist():
                        if member.startswith("/") or ".." in member:
                            continue
                        if member.startswith("decks/"):
                            dest = os.path.join(target_dir, member[len("decks/"):])
                        else:
                            dest = os.path.join(target_dir, member)

                        if member.endswith("/"):
                            os.makedirs(dest, exist_ok=True)
                        else:
                            os.makedirs(os.path.dirname(dest), exist_ok=True)
                            with zf.open(member) as src, open(dest, "wb") as dst:
                                dst.write(src.read())

                decks = scan_local_decks(target_dir)
                log_event(f"Successfully unpacked ZIP '{os.path.basename(zip_path)}' into '{target_dir}' ({len(decks)} decks)")
                self.send_json_response({"success": True, "target_dir": target_dir, "decks": decks})
            except Exception as ex:
                log_event(f"Failed to import ZIP: {ex}")
                self.send_json_response({"error": str(ex)}, status=500)
            return

        if path == "/api/scan-directory":
            target_dir = payload.get("directory", "input").strip() or "input"
            decks = scan_local_decks(target_dir)
            log_event(f"Scanned folder '{target_dir}': found {len(decks)} decklist(s)")
            self.send_json_response({"success": True, "directory": target_dir, "decks": decks})
            return

        if path == "/api/fetch-online-decks":
            portal_url = payload.get("portal_url", "https://proxy-list-helper.onrender.com").rstrip("/")
            admin_pwd = payload.get("admin_password", "").strip()
            req_url = f"{portal_url}/admin/api/decks"
            req = urllib.request.Request(req_url, headers={
                "X-Admin-Password": admin_pwd,
                "Authorization": f"Bearer {admin_pwd}",
                "Cookie": f"admin_session={admin_pwd}",
                "User-Agent": "MTG-Proxy-Generator-GUI/2.1"
            })
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    self.send_json_response(data)
            except urllib.error.HTTPError as he:
                msg = f"HTTP {he.code}: {he.reason}"
                if he.code == 403:
                    msg = "HTTP 403: Forbidden - Invalid Admin Password. Check the ADMIN_PASSWORD environment variable set on your Render dashboard."
                self.send_json_response({"error": msg}, status=he.code)
            except Exception as ex:
                self.send_json_response({"error": str(ex)}, status=500)
            return

        if path == "/api/import-online-decks":
            portal_url = payload.get("portal_url", "https://proxy-list-helper.onrender.com").rstrip("/")
            admin_pwd = payload.get("admin_password", "").strip()
            deck_ids = payload.get("deck_ids", None)
            target_dir = payload.get("target_dir", "input").strip() or "input"

            req_url = f"{portal_url}/admin/api/export-decks-zip"
            post_body = json.dumps({"deck_ids": deck_ids}).encode("utf-8")
            req = urllib.request.Request(req_url, data=post_body, headers={
                "Content-Type": "application/json",
                "X-Admin-Password": admin_pwd,
                "Authorization": f"Bearer {admin_pwd}",
                "Cookie": f"admin_session={admin_pwd}",
                "User-Agent": "MTG-Proxy-Generator-GUI/2.1"
            })
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    zip_data = resp.read()
                    zip_path = os.path.join(target_dir, "_imported_run.zip")
                    os.makedirs(target_dir, exist_ok=True)
                    with open(zip_path, "wb") as fz:
                        fz.write(zip_data)

                    # Extract into target directory
                    with zipfile.ZipFile(zip_path, "r") as zf:
                        for member in zf.namelist():
                            if member.startswith("/") or ".." in member:
                                continue
                            if member.startswith("decks/"):
                                target = os.path.join(target_dir, member[len("decks/"):])
                            else:
                                target = os.path.join(target_dir, member)

                            if member.endswith("/"):
                                os.makedirs(target, exist_ok=True)
                            else:
                                os.makedirs(os.path.dirname(target), exist_ok=True)
                                with zf.open(member) as src, open(target, "wb") as dst:
                                    dst.write(src.read())

                    if os.path.exists(zip_path):
                        os.remove(zip_path)

                decks = scan_local_decks(target_dir)
                log_event(f"Successfully imported {len(decks)} decklists from cloud portal into '{target_dir}'")
                self.send_json_response({"success": True, "target_dir": target_dir, "decks": decks})
            except urllib.error.HTTPError as he:
                msg = f"HTTP {he.code}: {he.reason}"
                if he.code == 403:
                    msg = "HTTP 403: Forbidden - Invalid Admin Password. Check the ADMIN_PASSWORD environment variable set on your Render dashboard."
                self.send_json_response({"error": msg}, status=he.code)
            except Exception as ex:
                self.send_json_response({"error": str(ex)}, status=500)
            return

        if path == "/api/start-run":
            with _STATE_LOCK:
                if _RUN_STATE["job_status"] == "running":
                    self.send_json_response({"error": "A print run is already in progress!"}, status=400)
                    return

            cfg = AppConfig()
            cfg.mode = payload.get("mode", cfg.mode)
            cfg.upscale_model = payload.get("upscale_model", getattr(cfg, "upscale_model", "4x-UltraSharp"))
            cfg.upscale_factor = int(payload.get("upscale_factor", cfg.upscale_factor))
            cfg.device = payload.get("device", cfg.device)
            cfg.cut_width_mm = float(payload.get("cut_width_mm", cfg.cut_width_mm))
            cfg.cut_height_mm = float(payload.get("cut_height_mm", cfg.cut_height_mm))
            cfg.bleed_margin_mm = float(payload.get("bleed_margin_mm", cfg.bleed_margin_mm))
            cfg.bleed_mode = payload.get("bleed_mode", cfg.bleed_mode)
            cfg.break_by = payload.get("break_by", cfg.break_by)
            cfg.two_divider_cards = bool(payload.get("two_divider_cards", True))
            cfg.custom_back_check_ratio = bool(payload.get("custom_back_check_ratio", True))
            cfg.custom_back_upscale = bool(payload.get("custom_back_upscale", True))
            cfg.custom_back_add_bleed = bool(payload.get("custom_back_add_bleed", True))
            cfg.enable_suborders = bool(payload.get("enable_suborders", False))
            cfg.cards_per_sheet = int(payload.get("cards_per_sheet", 18))
            cfg.target_suborder_cards = int(payload.get("target_suborder_cards", 1440))
            cfg.remove_copyright = bool(payload.get("remove_copyright", True))
            cfg.reset_bleed_cache = bool(payload.get("reset_bleed_cache", False))
            cfg.weights_dir = payload.get("weights_dir", getattr(cfg, "weights_dir", "weights"))
            if "input_dir" in payload and payload["input_dir"]:
                cfg.input_dir = payload["input_dir"]
            if "cache_dir" in payload and payload["cache_dir"]:
                cfg.set_cache_base_dir(payload["cache_dir"])
                log_event(f"Storage: Active cache directory is '{cfg.cache_dir}' (upscaled: '{cfg.upscaled_cache_dir}')")

            selected_deck_paths = payload.get("selected_deck_paths", None)
            custom_back_map = payload.get("custom_back_map", None)

            thread = threading.Thread(target=run_worker_task, args=(cfg, selected_deck_paths, custom_back_map), daemon=True)
            thread.start()

            self.send_json_response({"success": True, "message": "Print run started."})
            return

        if path == "/api/cache-info":
            cache_path = payload.get("cache_dir", "scryfall_cache").strip() or "scryfall_cache"
            temp_cfg = AppConfig()
            temp_cfg.set_cache_base_dir(cache_path)

            def get_dir_size_and_count(d):
                if not os.path.exists(d):
                    return 0, 0
                total_bytes = 0
                count = 0
                for root, _, files in os.walk(d):
                    for f in files:
                        p = os.path.join(root, f)
                        try:
                            total_bytes += os.path.getsize(p)
                            count += 1
                        except OSError:
                            pass
                return total_bytes, count

            raw_bytes, raw_count = get_dir_size_and_count(temp_cfg.cache_dir)
            up_bytes, up_count = get_dir_size_and_count(temp_cfg.upscaled_cache_dir)
            total_bytes = raw_bytes + up_bytes
            total_count = raw_count + up_count

            target_drive = temp_cfg.cache_dir if os.path.exists(temp_cfg.cache_dir) else os.path.abspath(".")
            try:
                disk = shutil.disk_usage(target_drive)
                free_gb = disk.free / (1024 ** 3)
                total_disk_gb = disk.total / (1024 ** 3)
            except Exception:
                free_gb = 0.0
                total_disk_gb = 0.0

            self.send_json_response({
                "cache_dir": temp_cfg.cache_dir,
                "upscaled_cache_dir": temp_cfg.upscaled_cache_dir,
                "total_mb": round(total_bytes / (1024 * 1024), 1),
                "total_gb": round(total_bytes / (1024 ** 3), 2),
                "raw_mb": round(raw_bytes / (1024 * 1024), 1),
                "upscaled_mb": round(up_bytes / (1024 * 1024), 1),
                "file_count": total_count,
                "free_gb": round(free_gb, 1),
                "total_disk_gb": round(total_disk_gb, 1)
            })
            return

        if path == "/api/open-cache-folder":
            cache_path = payload.get("cache_dir", "scryfall_cache").strip() or "scryfall_cache"
            temp_cfg = AppConfig()
            temp_cfg.set_cache_base_dir(cache_path)
            target = temp_cfg.cache_dir if os.path.exists(temp_cfg.cache_dir) else os.path.abspath(".")
            try:
                if sys.platform == "darwin":
                    subprocess.run(["open", target], check=False)
                elif sys.platform.startswith("win"):
                    os.startfile(target)
                else:
                    subprocess.run(["xdg-open", target], check=False)
                self.send_json_response({"success": True, "path": target})
            except Exception as ex:
                self.send_json_response({"error": str(ex)}, status=500)
            return

        if path == "/api/open-folder":
            output_dir = os.path.abspath("output")
            os.makedirs(output_dir, exist_ok=True)
            try:
                if sys.platform == "darwin":
                    subprocess.run(["open", output_dir], check=False)
                elif sys.platform.startswith("win"):
                    os.startfile(output_dir)
                else:
                    subprocess.run(["xdg-open", output_dir], check=False)
                self.send_json_response({"success": True, "path": output_dir})
            except Exception as ex:
                self.send_json_response({"error": str(ex)}, status=500)
            return

        if path == "/api/cancel":
            with _STATE_LOCK:
                if _RUN_STATE["job_status"] == "running":
                    _RUN_STATE["cancel_requested"] = True
                    log_event("Cancel requested by user...")
            self.send_json_response({"success": True})
            return

        self.send_response(404)
        self.end_headers()

    def serve_sse_events(self):
        """Streams Server-Sent Events to the client for live UI reactivity."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        last_idx = 0
        try:
            while True:
                with _STATE_LOCK:
                    state_copy = dict(_RUN_STATE)
                    all_logs = state_copy["logs"]
                    new_logs = all_logs[last_idx:]
                    last_idx = len(all_logs)

                event_data = {
                    "job_status": state_copy["job_status"],
                    "stage": state_copy["stage"],
                    "progress": state_copy["progress"],
                    "new_logs": new_logs,
                    "results": state_copy["results"],
                    "error": state_copy["error"]
                }
                msg = f"data: {json.dumps(event_data, default=str)}\n\n".encode("utf-8")
                self.wfile.write(msg)
                self.wfile.flush()

                time.sleep(0.5)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def serve_html_ui(self):
        """Serves the rich neo-brutalist generator interface."""
        html_path = os.path.join(os.path.dirname(__file__), "generator.html")
        if not os.path.exists(html_path):
            content = "<h1>generator.html not found</h1>".encode("utf-8")
        else:
            with open(html_path, "rb") as f:
                content = f.read()

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

def run_server(config: Optional[AppConfig] = None, port: int = PORT, open_browser: bool = True):
    """Starts the local web server and opens the browser."""
    server_address = ("", port)
    try:
        httpd = ThreadingHTTPServer(server_address, GeneratorRequestHandler)
    except OSError:
        port = port + 1
        server_address = ("", port)
        httpd = ThreadingHTTPServer(server_address, GeneratorRequestHandler)

    url = f"http://localhost:{port}"
    print(f"\n=======================================================")
    print(f"🚀 MTG Proxy Generator Web GUI running at: {url}")
    print(f"   Press Ctrl+C in console to stop the local server.")
    print(f"=======================================================\n")

    if open_browser:
        def _open():
            time.sleep(0.6)
            try:
                if sys.platform == "darwin":
                    subprocess.run(["open", url], check=False)
                elif sys.platform.startswith("win"):
                    os.startfile(url)
                else:
                    subprocess.run(["xdg-open", url], check=False)
            except Exception:
                pass
        threading.Thread(target=_open, daemon=True).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Generator Web GUI server...")
        httpd.server_close()

if __name__ == "__main__":
    run_server()
