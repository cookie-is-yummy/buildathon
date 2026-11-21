"""Phase recorder for tests/test_executor.py::TestExecutor::test_execute_tpcds.
Loaded by EVERY Python process started while the test runs. Active when BR_PHASE is set.

The test hands every query to a worker process and compares what comes back with DuckDB
(see replay.py). sqlglot code runs in three calls made by the test module:

    parse_one(sql)                    parent, in _mp_execute
    mp_execute(expression, meta)      worker
    transpile(sql, write="duckdb")    parent, in cached_execute

This recorder notes, for every line of the repository, whether the test executes it
INSIDE one of those calls or OUTSIDE them (import time, set-up, pickling, comparing).
It also notes how each call was made, so that replay.py, which repeats exactly these
calls, can be checked against the real test.

The worker processes import the test module normally and are caught by an import hook;
the pytest process imports it through pytest's own loader, so there phase_plugin.py
(-p phase_plugin) does the wrapping.

Writes $BR_PHASE.<pid>.json at exit.
"""
import os
import sys

_OUT = os.environ.get("BR_PHASE")
if _OUT:
    import atexit
    import functools
    import importlib.abc
    import json
    import signal

    _ROOT = os.environ.get("BR_ROOT") or os.getcwd()
    _PREFIX = _ROOT + "/"
    _depth = [0]
    _lines = ({}, {})  # outside, inside: file -> set of lines
    _calls = {}        # description of a wrapped call -> how often
    _mon = sys.monitoring
    _TOOL = 4
    _mon.use_tool_id(_TOOL, "blast-radius-phase")

    def _line(code, line):
        name = code.co_filename
        if name.startswith(_PREFIX):
            _lines[1 if _depth[0] else 0].setdefault(name[len(_PREFIX):], set()).add(line)
        return _mon.DISABLE

    _mon.register_callback(_TOOL, _mon.events.LINE, _line)
    _mon.set_events(_TOOL, _mon.events.LINE)

    def _wrap(module, name):
        real = getattr(module, name)

        @functools.wraps(real, updated=())
        def inside(*args, **kwargs):
            key = "%s.%s(%s%s)" % (
                module.__name__, name, ", ".join(type(a).__name__ for a in args),
                "".join(", %s=%r" % (k, v if isinstance(v, (str, bool, type(None))) else type(v).__name__)
                        for k, v in sorted(kwargs.items())))
            _calls[key] = _calls.get(key, 0) + 1
            _depth[0] += 1
            if _depth[0] == 1:
                _mon.restart_events()
            try:
                return real(*args, **kwargs)
            finally:
                _depth[0] -= 1
                if _depth[0] == 0:
                    _mon.restart_events()

        setattr(module, name, inside)

    _TARGETS = {"tests.test_executor": ("parse_one", "mp_execute", "transpile")}
    _wrapped = set()

    def _wrap_all(module):
        if module.__name__ in _TARGETS and module.__name__ not in _wrapped:
            _wrapped.add(module.__name__)
            for name in _TARGETS[module.__name__]:
                _wrap(module, name)

    sys._br_phase_wrap = _wrap_all

    class _Loader(importlib.abc.Loader):
        def __init__(self, real):
            self.real = real

        def create_module(self, spec):
            return self.real.create_module(spec)

        def exec_module(self, module):
            self.real.exec_module(module)
            _wrap_all(module)

        def __getattr__(self, name):
            return getattr(self.real, name)

    class _Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path, target=None):
            if fullname not in _TARGETS:
                return None
            for finder in sys.meta_path:
                if finder is self or not hasattr(finder, "find_spec"):
                    continue
                spec = finder.find_spec(fullname, path, target)
                if spec is not None:
                    spec.loader = _Loader(spec.loader)
                    return spec
            return None

    sys.meta_path.insert(0, _Finder())

    _done = []

    def _dump(*_):
        if _done:
            return
        _done.append(1)
        try:
            with open("%s.%d.json" % (_OUT, os.getpid()), "w") as fh:
                json.dump({
                    "outside": {f: sorted(v) for f, v in _lines[0].items()},
                    "inside": {f: sorted(v) for f, v in _lines[1].items()},
                    "calls": _calls,
                    "argv": sys.argv[:3],
                }, fh)
        except Exception as error:
            print("phase recorder dump failed:", repr(error), file=sys.stderr)

    atexit.register(_dump)

    def _term(signum, frame):
        _dump()
        os._exit(143)

    try:
        signal.signal(signal.SIGTERM, _term)
    except Exception:
        pass
