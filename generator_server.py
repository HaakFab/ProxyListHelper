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
    """Scans local input directory for deck files and companion cardbacks."""
    results = []
    if not os.path.exists(input_dir):
        os.makedirs(input_dir, exist_ok=True)
        return results

    entries = []
    for item in sorted(os.listdir(input_dir)):
        item_path = os.path.join(input_dir, item)
        if os.path.isfile(item_path) and item.endswith(".txt"):
            entries.append((item_path, None))
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
                entries.append((chosen_txt, custom_back))

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

def run_worker_task(config: AppConfig, selected_deck_paths: Optional[List[str]] = None):
    """Executes the print run pipeline in a background thread."""
    from moxfield_mpc_to_pdf import run_pipeline

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
    log_event(f"Mode: {config.mode} | Scale: x{config.upscale_factor} | Device: {config.device}")
    log_event(f"Suborders: {config.enable_suborders} (Sheet: {config.cards_per_sheet}, Target: {config.target_suborder_cards})")
    log_event(f"Two Divider Cards: {config.two_divider_cards} (Stripes: White/Red/Yellow/Blue)")

    # Prepare specific deck entries if selected
    specific_entries = None
    if selected_deck_paths:
        specific_entries = []
        for path in selected_deck_paths:
            custom_back = None
            parent_dir = os.path.dirname(path)
            if parent_dir and os.path.isdir(parent_dir) and parent_dir != config.input_dir:
                for ext in [".jpg", ".jpeg", ".png", ".webp"]:
                    for b_name in ["back", "cardback", "custom_back"]:
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

    try:
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
        with _STATE_LOCK:
            _RUN_STATE["job_status"] = "error"
            _RUN_STATE["error"] = str(ex)
            _RUN_STATE["stage"] = "error"
        log_event(f"Fatal Exception: {ex}")

class GeneratorRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Suppress noisy standard HTTP logs in console
        return

    def send_json_response(self, data: Any, status: int = 200):
        body = json.dumps(data).encode("utf-8")
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
                "upscale_factor": cfg.upscale_factor,
                "device": cfg.device,
                "cut_width_mm": cfg.cut_width_mm,
                "cut_height_mm": cfg.cut_height_mm,
                "bleed_margin_mm": cfg.bleed_margin_mm,
                "bleed_mode": cfg.bleed_mode,
                "break_by": cfg.break_by,
                "two_divider_cards": getattr(cfg, "two_divider_cards", True),
                "enable_suborders": getattr(cfg, "enable_suborders", False),
                "cards_per_sheet": getattr(cfg, "cards_per_sheet", 18),
                "target_suborder_cards": getattr(cfg, "target_suborder_cards", 1440),
                "remove_copyright": cfg.remove_copyright
            })
            return

        if path == "/api/local-decks":
            decks = scan_local_decks("input")
            self.send_json_response({"decks": decks})
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

        if path == "/api/fetch-online-decks":
            portal_url = payload.get("portal_url", "https://proxy-list-helper.onrender.com").rstrip("/")
            admin_pwd = payload.get("admin_password", "")
            req_url = f"{portal_url}/admin/api/decks"
            req = urllib.request.Request(req_url, headers={
                "X-Admin-Password": admin_pwd,
                "User-Agent": "MTG-Proxy-Generator-GUI/2.1"
            })
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    self.send_json_response(data)
            except urllib.error.HTTPError as he:
                self.send_json_response({"error": f"HTTP {he.code}: {he.reason}"}, status=he.code)
            except Exception as ex:
                self.send_json_response({"error": str(ex)}, status=500)
            return

        if path == "/api/import-online-decks":
            portal_url = payload.get("portal_url", "https://proxy-list-helper.onrender.com").rstrip("/")
            admin_pwd = payload.get("admin_password", "")
            deck_ids = payload.get("deck_ids", None)

            req_url = f"{portal_url}/admin/api/export-decks-zip"
            post_body = json.dumps({"deck_ids": deck_ids}).encode("utf-8")
            req = urllib.request.Request(req_url, data=post_body, headers={
                "Content-Type": "application/json",
                "X-Admin-Password": admin_pwd,
                "User-Agent": "MTG-Proxy-Generator-GUI/2.1"
            })
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    zip_data = resp.read()
                    zip_path = os.path.join("input", "_imported_run.zip")
                    with open(zip_path, "wb") as fz:
                        fz.write(zip_data)

                    # Extract into input directory
                    with zipfile.ZipFile(zip_path, "r") as zf:
                        for member in zf.namelist():
                            if member.startswith("decks/"):
                                # Extract decks/ folder contents directly into input/
                                target = os.path.join("input", member[len("decks/"):])
                                if member.endswith("/"):
                                    os.makedirs(target, exist_ok=True)
                                else:
                                    os.makedirs(os.path.dirname(target), exist_ok=True)
                                    with zf.open(member) as src, open(target, "wb") as dst:
                                        dst.write(src.read())

                    if os.path.exists(zip_path):
                        os.remove(zip_path)

                decks = scan_local_decks("input")
                log_event(f"Successfully imported {len(decks)} decklists from online portal into input/")
                self.send_json_response({"success": True, "decks": decks})
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
            cfg.upscale_factor = int(payload.get("upscale_factor", cfg.upscale_factor))
            cfg.device = payload.get("device", cfg.device)
            cfg.cut_width_mm = float(payload.get("cut_width_mm", cfg.cut_width_mm))
            cfg.cut_height_mm = float(payload.get("cut_height_mm", cfg.cut_height_mm))
            cfg.bleed_margin_mm = float(payload.get("bleed_margin_mm", cfg.bleed_margin_mm))
            cfg.bleed_mode = payload.get("bleed_mode", cfg.bleed_mode)
            cfg.break_by = payload.get("break_by", cfg.break_by)
            cfg.two_divider_cards = bool(payload.get("two_divider_cards", True))
            cfg.enable_suborders = bool(payload.get("enable_suborders", False))
            cfg.cards_per_sheet = int(payload.get("cards_per_sheet", 18))
            cfg.target_suborder_cards = int(payload.get("target_suborder_cards", 1440))
            cfg.remove_copyright = bool(payload.get("remove_copyright", True))
            cfg.reset_bleed_cache = bool(payload.get("reset_bleed_cache", False))

            selected_deck_paths = payload.get("selected_deck_paths", None)

            thread = threading.Thread(target=run_worker_task, args=(cfg, selected_deck_paths), daemon=True)
            thread.start()

            self.send_json_response({"success": True, "message": "Print run started."})
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
                msg = f"data: {json.dumps(event_data)}\n\n".encode("utf-8")
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
