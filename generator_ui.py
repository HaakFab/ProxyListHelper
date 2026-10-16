#!/usr/bin/env python3
"""
Launcher script for MTG Moxfield Proxy Pre-Press Web GUI.
Usage:
    python3 generator_ui.py
"""
import sys
from generator_server import run_server

if __name__ == "__main__":
    port = 8008
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        port = int(sys.argv[1])
    run_server(port=port)
