"""The dashboard must render without raising, including before any run has data.

It is the demo surface, so a rendering error is only ever discovered at the worst moment.
These are smoke tests: they execute the real script against the real result files.
"""
import os

from streamlit.testing.v1 import AppTest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "dashboard.py")


def run_app():
    cwd = os.getcwd()
    os.chdir(ROOT)                       # the app reads results/ relative to the repo root
    try:
        at = AppTest.from_file(APP, default_timeout=300)
        at.run()
        return at
    finally:
        os.chdir(cwd)


def test_it_renders_without_raising():
    at = run_app()
    assert not at.exception, [e.value for e in at.exception]


def test_every_panel_is_present_for_a_run_with_data():
    at = run_app()
    assert not at.exception, [e.value for e in at.exception]
    assert [t.label for t in at.tabs] == \
        ["Signals", "Oracle", "Orders & risk", "Swarm vs solo", "History"]
    assert len(at.metric) > 10
    assert at.dataframe, "the order log and decision log should render"


def test_the_run_is_pinned_to_the_submission_account():
    """No run picker: a viewer must not be able to switch to a development run."""
    at = run_app()
    assert not at.selectbox, "the dashboard should expose no run/account selector"
    assert any("results/submission" in c.value for c in at.caption)
