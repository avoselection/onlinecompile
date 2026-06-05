xterm.js vendor folder
======================

This folder is intentionally (almost) empty.

The terminal uses a 3-tier loading strategy:
  1. Local vendor copy in this folder  (fully offline)
  2. jsDelivr CDN                       (online fallback)
  3. Built-in <pre> terminal in app.js  (always works, no deps)

To make the terminal fully offline, run once with internet access:

    python scripts/vendor_xterm.py

That downloads three files here:
    xterm.min.js
    xterm.min.css
    addon-fit.min.js

After that the classroom machine never needs the internet for the terminal.
If these files are absent, the app silently falls back to CDN, then to the
plain-text terminal — output is never lost either way.
