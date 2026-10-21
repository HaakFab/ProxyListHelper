#!/usr/bin/env python3
"""
Launcher script for MTG Moxfield Proxy Pre-Press Web GUI.
Usage:
    python3 generator_ui.py
"""
import os
import sys

# Verify if required pipeline dependencies are available in current Python
def _ensure_runtime_environment():
    try:
        import PIL
        import reportlab
        return
    except ImportError:
        pass

    # Search for configured Python with full dependencies installed
    candidates = [
        "/Users/fabian/miniconda3/bin/python3",
        os.path.expanduser("~/miniconda3/bin/python3"),
        os.path.expanduser("~/anaconda3/bin/python3"),
        "/usr/local/bin/python3",
        "/opt/homebrew/bin/python3"
    ]
    for cand in candidates:
        if os.path.exists(cand) and cand != sys.executable:
            try:
                # Test candidate
                import subprocess
                res = subprocess.run([cand, "-c", "import PIL, reportlab"], capture_output=True)
                if res.returncode == 0:
                    print(f"[PRE-PRESS] Switching Python interpreter to: {cand}")
                    os.execv(cand, [cand] + sys.argv)
            except Exception:
                continue

_ensure_runtime_environment()

from generator_server import run_server

if __name__ == "__main__":
    port = 8008
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        port = int(sys.argv[1])
    run_server(port=port)
