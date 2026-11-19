"""Executes the dashboard's inventory-status helpers in Node (they are what the owner sees)."""
import os
import shutil
import subprocess

import pytest

HERE = os.path.dirname(__file__)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_dashboard_stock_helpers_behave():
    r = subprocess.run(["node", os.path.join(HERE, "stock_ui_check.js"),
                        os.path.join(HERE, "..", "..", "static", "dashboard.js")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_all_three_status_callers_surface_the_stock_note():
    src = open(os.path.join(HERE, "..", "..", "static", "dashboard.js"), encoding="utf8").read()
    assert src.count("_stockNote(_res)") == 2                      # drawer + list prompt
    assert "_stockEl.textContent = _orderStockStatus(order)" in src
