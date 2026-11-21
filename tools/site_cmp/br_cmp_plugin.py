"""pytest plugin for the whole-suite comparison recording (see sitecustomize.py)."""
import json
import os
import sys

_switch, _snapshot = sys._br_cmp


def pytest_collection_modifyitems(items):
    if os.environ.get("BR_SEQ_REVERSE"):
        items.reverse()


def pytest_runtest_logstart(nodeid, location):
    _switch(nodeid)


def pytest_runtest_logfinish(nodeid, location):
    _switch(None)


def pytest_sessionfinish(session, exitstatus):
    out = _snapshot()
    out["exit"] = int(exitstatus)
    json.dump(out, open(os.environ["BR_CMP_SEQ"] + ".json", "w"))
