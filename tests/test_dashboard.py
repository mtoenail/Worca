"""The dashboard must render without raising, including before any run has data.

It is the demo surface, so a rendering error is only ever discovered at the worst moment.
These are smoke tests: they execute the real script against the real result files.
"""
import os

import pytest

from streamlit.testing.v1 import AppTest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "dashboard.py")


def run_app(select_run=None):
    cwd = os.getcwd()
    os.chdir(ROOT)                       # the app reads results/ relative to the repo root
    try:
        at = AppTest.from_file(APP, default_timeout=300)
        at.run()
        if select_run and select_run in at.selectbox[0].options:
            at.selectbox[0].set_value(select_run)
            at.run()
        return at
    finally:
        os.chdir(cwd)


def test_it_renders_without_raising():
    at = run_app()
    assert not at.exception, [e.value for e in at.exception]


def test_every_panel_is_present_for_a_run_with_data():
    runs = [r for r in run_app().selectbox[0].options if not r.endswith("warmup")]
    if not runs:
        pytest.skip("no completed run to render")
    at = run_app(runs[0])
    assert not at.exception, [e.value for e in at.exception]
    assert [t.label for t in at.tabs] == \
        ["Signals", "Oracle", "Orders & risk", "Swarm vs solo", "History"]
    assert len(at.metric) > 10
    assert at.dataframe, "the order log and decision log should render"


def test_a_stale_signal_is_flagged_not_hidden():
    """The 21.5h gamma_scout:SPY signal in the archived run must be visibly marked."""
    at = run_app("results/dev-overnight")
    if at.selectbox[0].value != "results/dev-overnight":
        pytest.skip("archived run not present")
    assert any("STALE" in e.value for e in at.error)
