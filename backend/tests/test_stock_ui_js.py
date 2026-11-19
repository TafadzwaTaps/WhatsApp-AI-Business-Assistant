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


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_dashboard_billing_date_helper_behaves():
    r = subprocess.run(["node", os.path.join(HERE, "billing_ui_check.js"),
                        os.path.join(HERE, "..", "..", "static", "dashboard.js")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_billing_ui_wires_cancel_resume_and_real_states():
    src = open(os.path.join(HERE, "..", "..", "static", "dashboard.js"), encoding="utf8").read()
    assert "async function stripeResume" in src and "'/billing/resume'" in src
    assert "stripe-resume-btn" in src
    assert "cancelled:'var(--text-dim)'" in src                     # the backend spelling is styled
    assert "res.message ||" in src and "res.access_until" in src    # server message and date, not a fixed claim
    assert "Subscription cancelled — access continues until period end." not in src
