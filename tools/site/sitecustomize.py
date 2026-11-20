"""Recorder loaded by EVERY Python process started while one test runs (the test process,
spawned multiprocessing workers, `python -c` children). Active only when BR_REC is set.

Per process it records, relative to the repository root (the working directory):
  lines        executed lines per file (coverage.py)
  calls        first line of every code object that started running (functions, lambdas,
               class bodies, modules) per file
  imports      files opened by the import system
  data         files opened while repository code was on the call stack
  infra_opens  files opened by anything else (pytest reading its config, linecache, ...)
  lists / implists / infra_lists   the same three classes for directory listings
  failed_imports  modules some code tried to import that did not end up imported
and writes them to $BR_REC.<pid>.json at exit.
"""
import os
import sys

_OUT = os.environ.get("BR_REC")
if _OUT:
    import atexit
    import json
    import signal

    _ROOT = os.environ.get("BR_ROOT") or os.getcwd()
    _PREFIX = _ROOT + "/"
    _sets = {k: set() for k in ("imports", "data", "infra_opens", "lists", "implists", "infra_lists", "tried")}
    _calls = {}

    def _rel(p):
        try:
            if isinstance(p, bytes):
                p = p.decode()
            if not isinstance(p, str):
                return None
            p = os.path.abspath(p)
            if p == _ROOT:
                return "."
            if p.startswith(_PREFIX):
                return p[len(_PREFIX):]
        except Exception:
            pass
        return None

    def _classify():
        """'imp' when the import system is on the stack, 'repo' when repository code is,
        else 'infra'."""
        f = sys._getframe(2)
        repo = False
        depth = 0
        while f is not None and depth < 60:
            name = f.f_code.co_filename
            if name.startswith("<frozen importlib"):
                return "imp"
            if name.startswith(_PREFIX):
                repo = True
            f = f.f_back
            depth += 1
        return "repo" if repo else "infra"

    def _hook(event, args):
        try:
            if event == "open":
                r = _rel(args[0])
                if r is None:
                    return
                kind = _classify()
                _sets["imports" if kind == "imp" else "data" if kind == "repo" else "infra_opens"].add(r)
            elif event in ("os.listdir", "os.scandir", "os.walk", "glob.glob", "glob.glob/2"):
                a = args[0] if args else None
                r = _rel("." if a is None else a)
                if r is None:
                    return
                kind = _classify()
                _sets["implists" if kind == "imp" else "lists" if kind == "repo" else "infra_lists"].add(r)
            elif event == "import":
                _sets["tried"].add(args[0])
        except Exception:
            pass

    sys.addaudithook(_hook)

    import coverage

    _cov = coverage.Coverage(data_file=None, include=[_PREFIX + "*"], config_file=False)
    _cov.start()

    _mon = sys.monitoring
    _TOOL = 3
    try:
        _mon.use_tool_id(_TOOL, "blast-radius-calls")

        def _py_start(code, offset):
            name = code.co_filename
            if name.startswith(_PREFIX):
                _calls.setdefault(name[len(_PREFIX):], set()).add(code.co_firstlineno)
            return _mon.DISABLE

        _mon.register_callback(_TOOL, _mon.events.PY_START, _py_start)
        _mon.set_events(_TOOL, _mon.events.PY_START)
    except Exception as error:
        print("calls monitor unavailable:", error, file=sys.stderr)

    _done = []

    def _dump(*_):
        if _done:
            return
        _done.append(1)
        try:
            _cov.stop()
            data = _cov.get_data()
            lines = {}
            for f in data.measured_files():
                r = _rel(f)
                if r:
                    lines[r] = sorted(data.lines(f))
            out = {k: sorted(v) for k, v in _sets.items() if k != "tried"}
            out["failed_imports"] = sorted(m for m in _sets["tried"] if m not in sys.modules)
            out["lines"] = lines
            out["calls"] = {f: sorted(v) for f, v in _calls.items()}
            out["argv"] = sys.argv[:3]
            with open("%s.%d.json" % (_OUT, os.getpid()), "w") as fh:
                json.dump(out, fh)
        except Exception as error:
            print("recorder dump failed:", repr(error), file=sys.stderr)

    atexit.register(_dump)

    def _term(signum, frame):
        _dump()
        os._exit(143)

    try:
        signal.signal(signal.SIGTERM, _term)
    except Exception:
        pass
