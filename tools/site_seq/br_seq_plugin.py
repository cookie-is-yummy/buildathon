"""pytest plugin for the whole-suite recording: switches the coverage context per test,
records which code objects start in each test, and writes line -> tests bitsets."""
import json
import os
import sys

_ROOT = os.environ.get("BR_ROOT") or os.getcwd()
_PREFIX = _ROOT + "/"
_cov = getattr(sys, "_br_seq_cov", None)
_calls = {}  # (file, line) -> set of node ids
_current = [None]
_mon = sys.monitoring
_TOOL = 3


def _py_start(code, offset):
    try:
        name = code.co_filename
        if name.startswith(_PREFIX) and _current[0] is not None:
            _calls.setdefault((name[len(_PREFIX):], code.co_firstlineno), set()).add(_current[0])
        return _mon.DISABLE
    except Exception:  # interpreter shutdown
        return None


def pytest_configure(config):
    _mon.use_tool_id(_TOOL, "blast-radius-seq")
    _mon.register_callback(_TOOL, _mon.events.PY_START, _py_start)
    _mon.set_events(_TOOL, _mon.events.PY_START)


def pytest_collection_modifyitems(items):
    if os.environ.get("BR_SEQ_REVERSE"):
        items.reverse()


def pytest_runtest_logstart(nodeid, location):
    _current[0] = nodeid
    _mon.restart_events()
    _cov.switch_context(nodeid)


def pytest_runtest_logfinish(nodeid, location):
    _current[0] = None
    _cov.switch_context("")


def pytest_sessionfinish(session, exitstatus):
    _cov.stop()
    ids = [t["id"] for t in json.load(open(os.environ["BR_TESTS"]))["tests"]]
    number = {tid: n for n, tid in enumerate(ids)}
    data = _cov.get_data()
    lines = {}
    for f in data.measured_files():
        if not f.startswith(_PREFIX):
            continue
        table = {}
        for line, contexts in data.contexts_by_lineno(f).items():
            bits = 0
            for context in contexts:
                n = number.get(context)
                if n is not None:
                    bits |= 1 << n
            if bits:
                table[line] = "%x" % bits
        lines[f[len(_PREFIX):]] = table
    calls = {}
    for (f, line), nodes in _calls.items():
        bits = 0
        for node in nodes:
            n = number.get(node)
            if n is not None:
                bits |= 1 << n
        calls.setdefault(f, {})[line] = "%x" % bits
    json.dump({"lines": lines, "calls": calls, "exit": int(exitstatus)}, open(os.environ["BR_SEQ"] + ".json", "w"))
