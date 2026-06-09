#!/usr/bin/env python3
"""
Download xterm.js + addon-fit into static/vendor/xterm/ so the terminal works
fully offline (no CDN). Run once on a machine that has internet access; after
that the bundle is served locally and the classroom can stay air-gapped.

Usage:
    python scripts/vendor_xterm.py
"""
from __future__ import annotations

import os
import sys
import urllib.request

XTERM_VERSION = "5.5.0"
FIT_VERSION = "0.10.0"

FILES = {
    "xterm.min.js":     f"https://cdn.jsdelivr.net/npm/@xterm/xterm@{XTERM_VERSION}/lib/xterm.js",
    "xterm.min.css":    f"https://cdn.jsdelivr.net/npm/@xterm/xterm@{XTERM_VERSION}/css/xterm.css",
    "addon-fit.min.js": f"https://cdn.jsdelivr.net/npm/@xterm/addon-fit@{FIT_VERSION}/lib/addon-fit.js",
}

TARGET_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "static", "vendor", "xterm",
)


def main() -> int:
    os.makedirs(TARGET_DIR, exist_ok=True)
    for filename, url in FILES.items():
        dest = os.path.join(TARGET_DIR, filename)
        print(f"→ {filename}  ←  {url}")
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                data = resp.read()
            with open(dest, "wb") as f:
                f.write(data)
            print(f"  saved {len(data):,} bytes")
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR: {exc}", file=sys.stderr)
            return 1
    print("\nГотово. Терминал теперь работает полностью офлайн.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
