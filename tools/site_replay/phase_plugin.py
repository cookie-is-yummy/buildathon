"""pytest plugin for the phase recorder: wraps the test module's calls in the pytest
process (see sitecustomize.py in this directory)."""
import sys


def pytest_collection_finish(session):
    module = sys.modules.get("tests.test_executor")
    wrap = getattr(sys, "_br_phase_wrap", None)
    if module is not None and wrap is not None:
        wrap(module)
