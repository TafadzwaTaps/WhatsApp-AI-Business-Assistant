#!/usr/bin/env python3
"""Regenerate cloudflare/maintenance-worker.js from worker.template.js + static/maintenance.html.

Run after editing static/maintenance.html:   python cloudflare/build_worker.py
"""
import json, os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

with open(os.path.join(ROOT, "static", "maintenance.html"), encoding="utf-8") as fh:
    html = fh.read()
with open(os.path.join(HERE, "worker.template.js"), encoding="utf-8") as fh:
    tpl = fh.read()

out = tpl.replace("__MAINTENANCE_HTML__", json.dumps(html))
with open(os.path.join(HERE, "maintenance-worker.js"), "w", encoding="utf-8") as fh:
    fh.write(out)
print("wrote cloudflare/maintenance-worker.js (%d bytes)" % len(out))
