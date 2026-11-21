"""Comparison recorder, loaded by EVERY Python process started while tests run.

Every comparison `a OP b` in the repository's Python files (OP one of
is, is not, ==, !=, <, <=, >, >=; chained comparisons are left alone) is rewritten, when
the file is imported, into a call that returns exactly what the comparison returns and
also notes, for each OTHER operator of that list, whether it would have returned something
different for these two operands. From that we learn, for every comparison and every test:
  - did the test evaluate this comparison at all, and
  - for which replacement operators the outcome differed at least once.
A test that evaluated a comparison and never saw a difference for operator X behaves
identically if the comparison is rewritten to use X.

Two modes:
  BR_CMP=<prefix>      one test per process tree: writes <prefix>.<pid>.json at exit
  BR_CMP_SEQ=<prefix>  whole suite in one process: br_cmp_plugin.py switches the current
                       test and writes <prefix>.json at the end
"""
import os
import sys

_OUT = os.environ.get("BR_CMP")
_SEQ = os.environ.get("BR_CMP_SEQ")
if _OUT or _SEQ:
    import ast
    import atexit
    import builtins
    import importlib.machinery
    import json
    import operator
    import signal

    _ROOT = os.environ.get("BR_ROOT") or os.getcwd()
    _PREFIX = _ROOT + "/"
    _OPS = (ast.Is, ast.IsNot, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE)
    _FUNCS = (operator.is_, operator.is_not, operator.eq, operator.ne, operator.lt, operator.le, operator.gt, operator.ge)
    _N = len(_OPS)
    # values whose comparison methods are known to be free of side effects: for these
    # every alternative operator is evaluated; for anything else only identity tests and
    # the nearest alternative are (the others are then counted as "differs")
    _PLAIN = (int, float, str, bytes, bool, type(None), tuple, frozenset)
    _NEAR = {0: (2,), 1: (3,), 2: (0, 3), 3: (1, 2), 4: (5,), 5: (4,), 6: (7,), 7: (6,)}

    _sites = []      # index -> (file, line, column, operator index)
    _context = [0]   # bumped when the current test changes (suite mode)
    _state = []      # index -> [context stamp, alternatives still agreeing]
    _executed = {}   # context key -> set of site indexes
    _differs = {}    # context key -> set of (site index, alternative)
    _key = [None if _SEQ else "process"]

    def _compare(a, b, index):
        state = _state[index]
        op = _sites[index][3]
        if state[0] != _context[0]:
            state[0] = _context[0]
            state[1] = [alt for alt in range(_N) if alt != op]
            key = _key[0]
            if key is not None:
                _executed.setdefault(key, set()).add(index)
        try:
            result = _FUNCS[op](a, b)
        except BaseException:
            _all_differ(index, state)
            raise
        remaining = state[1]
        if remaining:
            plain = type(a) in _PLAIN and type(b) in _PLAIN
            near = _NEAR[op]
            keep = []
            for alt in remaining:
                if alt < 2 or plain or alt in near:
                    try:
                        other = _FUNCS[alt](a, b)
                        same = other is result and (result is True or result is False)
                    except BaseException:
                        same = False
                else:
                    same = False
                if same:
                    keep.append(alt)
                else:
                    key = _key[0]
                    if key is not None:
                        _differs.setdefault(key, set()).add((index, alt))
            state[1] = keep
        return result

    def _all_differ(index, state):
        key = _key[0]
        if key is not None:
            for alt in state[1]:
                _differs.setdefault(key, set()).add((index, alt))
        state[1] = []

    builtins.__br_compare__ = _compare

    class _Rewrite(ast.NodeTransformer):
        def __init__(self, rel):
            self.rel = rel

        def visit_Compare(self, node):
            self.generic_visit(node)
            if len(node.ops) != 1 or type(node.ops[0]) not in _OPS:
                return node
            index = len(_sites)
            _sites.append((self.rel, node.lineno, node.col_offset, _OPS.index(type(node.ops[0]))))
            _state.append([-1, []])
            call = ast.Call(
                func=ast.Name(id="__br_compare__", ctx=ast.Load()),
                args=[node.left, node.comparators[0], ast.Constant(value=index)],
                keywords=[],
            )
            return ast.copy_location(call, node)

    class _Loader(importlib.machinery.SourceFileLoader):
        def get_code(self, fullname):
            path = self.get_filename(fullname)
            if not path.startswith(_PREFIX):
                return super().get_code(fullname)
            return self.source_to_code(self.get_data(path), path)

        def source_to_code(self, data, path, *, _optimize=-1):
            if not str(path).startswith(_PREFIX):
                return super().source_to_code(data, path, _optimize=_optimize)
            tree = ast.parse(data, filename=path)
            tree = _Rewrite(path[len(_PREFIX):]).visit(tree)
            ast.fix_missing_locations(tree)
            return compile(tree, path, "exec", dont_inherit=True, optimize=_optimize)

    _hook = importlib.machinery.FileFinder.path_hook(
        (importlib.machinery.ExtensionFileLoader, importlib.machinery.EXTENSION_SUFFIXES),
        (_Loader, importlib.machinery.SOURCE_SUFFIXES),
        (importlib.machinery.SourcelessFileLoader, importlib.machinery.BYTECODE_SUFFIXES),
    )
    for _i, _h in enumerate(sys.path_hooks):
        if "FileFinder" in repr(_h):
            sys.path_hooks[_i] = _hook
            break
    else:
        sys.path_hooks.append(_hook)
    sys.path_importer_cache.clear()

    def _switch(key):
        """Suite mode: comparisons from now on belong to test `key` (None: to nobody)."""
        _key[0] = key
        _context[0] += 1

    def _snapshot():
        return {
            "sites": _sites,
            "executed": {k: sorted(v) for k, v in _executed.items()},
            "differs": {k: sorted(v) for k, v in _differs.items()},
            "argv": sys.argv[:3],
        }

    sys._br_cmp = (_switch, _snapshot)

    if _OUT:
        _done = []

        def _dump(*_):
            if _done:
                return
            _done.append(1)
            try:
                with open("%s.%d.json" % (_OUT, os.getpid()), "w") as fh:
                    json.dump(_snapshot(), fh)
            except Exception as error:
                print("comparison recorder dump failed:", repr(error), file=sys.stderr)

        atexit.register(_dump)

        def _term(signum, frame):
            _dump()
            os._exit(143)

        try:
            signal.signal(signal.SIGTERM, _term)
        except Exception:
            pass
