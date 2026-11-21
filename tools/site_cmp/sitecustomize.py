"""Comparison recorder, loaded by EVERY Python process started while tests run.

Every comparison `a OP b` in the repository's Python files (OP one of
is, is not, ==, !=, <, <=, >, >=; chained comparisons are left alone) is rewritten, when
the file is imported, into a call that returns exactly what the comparison returns and
also notes how the two operands relate: the answer each operator of that list gives for
them, where that can be asked without disturbing the test (see _kind: no comparison
method written in Python is ever called, except to ask `== None`, and containers are
never compared with each other). Per test and comparison we keep the set of distinct
outcomes. So we learn, for every comparison and every test, whether the test evaluated it
and what the operands looked like each time.

Every `and`/`or` expression is recorded too. For the whole expression (nested `and`/`or`
flattened; its operands that are not themselves `and`/`or` are its "leaves", numbered left
to right) we note each distinct outcome a test produced: which leaves were evaluated and
which of those were true. An evaluation that did not finish (an exception, a generator
that was suspended in the middle) is noted as "abandoned".

Code that runs because the recorder asked something (an __eq__ asked about None, the
truth of an operand) is recorded like any other code: a recording may hold more than the
test does on its own, never less.

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
    _DUNDERS = ("__eq__", "__ne__", "__lt__", "__le__", "__gt__", "__ge__")
    _SLOT = (type(object.__eq__), type(str.lower))  # how methods written in C look
    _NONE = type(None)

    _sites = []      # index -> (file, line, column, operator index)
    _context = [0]   # bumped when the current test changes (suite mode)
    _state = []      # index -> [context stamp, outcomes already noted in this context]
    _outcomes = {}   # context key -> set of (site index, outcome)
    _key = [None if _SEQ else "process"]
    _kinds = {}      # type -> kind, see _kind
    # True while the recorder asks an object's own __eq__ about None (so that this is not
    # asked again from inside that very __eq__)
    _busy = [False]

    def _kind(tp):
        """How a type compares. The recorder must not change what the test does, so it
        only asks questions whose answer cannot run code that changes anything:
        0  a plain value (None, numbers, strings, bytes and subclasses that keep the
           built-in comparison methods): every operator can be asked;
        1  no __eq__ of its own: == is the same as `is`;
        2  another type whose comparison methods are written in C (list, dict, ...): ==
           can be asked against kinds 0 and 1 (it does not look inside);
        3  a comparison method written in Python: only `== None` is asked."""
        kind = _kinds.get(tp)
        if kind is None:
            if any(type(getattr(tp, name, None)) not in _SLOT for name in _DUNDERS):
                kind = 3
            elif tp is _NONE or issubclass(tp, (int, float, str, bytes)):
                kind = 0
            elif tp.__eq__ is object.__eq__ and tp.__ne__ is object.__ne__:
                kind = 1
            else:
                kind = 2
            _kinds[tp] = kind
        return kind

    def _plain(value):
        kind = _kind(type(value))
        if kind == 2 and type(value) is tuple and len(value) <= 16:
            return all(_kind(type(item)) == 0 for item in value)
        return kind == 0

    def _compare(a, b, index):
        """Return `a OP b` and note the outcome: for each of the eight operators whether
        its answer for these operands is known, and whether it is True. Encoded as
        (known mask << 8) | true mask; 0 means nothing is known (the comparison raised or
        did not return True/False)."""
        op = _sites[index][3]
        try:
            result = _FUNCS[op](a, b)
        except BaseException:
            _note(index, 0)
            raise
        known = true = 0
        if result is True or result is False:
            same = a is b
            answers = {0: same, 1: not same, op: result}
            if _plain(a) and _plain(b):
                for alt in range(_N):
                    if alt not in answers:
                        try:
                            answers[alt] = _FUNCS[alt](a, b)
                        except BaseException:
                            pass
            elif op < 2:
                # `is` / `is not`: what would == / != say?
                ka, kb = _kind(type(a)), _kind(type(b))
                if ka == 1 and kb == 1:
                    answers[2], answers[3] = same, not same
                elif ka < 3 and kb < 3 and not (ka == 2 and kb == 2):
                    try:
                        answers[2] = _FUNCS[2](a, b)
                        answers[3] = _FUNCS[3](a, b)
                    except BaseException:
                        pass
                elif (a is None or b is None) and not _busy[0]:
                    _busy[0] = True
                    try:
                        answers[2] = _FUNCS[2](a, b)
                        answers[3] = _FUNCS[3](a, b)
                    except BaseException:
                        pass
                    finally:
                        _busy[0] = False
            for alt, other in answers.items():
                if other is True:
                    known |= 1 << alt
                    true |= 1 << alt
                elif other is False:
                    known |= 1 << alt
        _note(index, known << 8 | true)
        return result

    def _note(index, outcome):
        state = _state[index]
        if state[0] != _context[0]:
            state[0] = _context[0]
            state[1] = set()
        if outcome not in state[1]:
            state[1].add(outcome)
            key = _key[0]
            if key is not None:
                _outcomes.setdefault(key, set()).add((index, outcome))

    builtins.__br_compare__ = _compare

    _bsites = []     # index -> (file, line, column, number of leaves)
    _bstack = []     # open evaluations: [site index, id of the calling frame, evaluated mask, true mask]
    _patterns = {}   # context key -> set of (site index, evaluated mask, true mask); mask -1: abandoned
    _getframe = sys._getframe

    def _abandon(entry):
        key = _key[0]
        if key is not None:
            _patterns.setdefault(key, set()).add((entry[0], -1, 0))

    def _find(index, frame):
        """The open evaluation of this site by this frame. Whatever was opened after it and
        is still open did not finish normally."""
        position = len(_bstack) - 1
        while position >= 0:
            entry = _bstack[position]
            if entry[0] == index and entry[1] == frame:
                break
            position -= 1
        if position < 0:
            return None
        while len(_bstack) - 1 > position:
            _abandon(_bstack.pop())
        return _bstack[position]

    def _bool_begin(index):
        _bstack.append([index, id(_getframe(1)), 0, 0])
        return index

    def _bool_leaf(value, index, leaf):
        entry = _find(index, id(_getframe(1)))
        if entry is not None and entry[2] != -1:
            try:
                truth = True if value else False
            except BaseException:
                entry[2] = -1
            else:
                bit = 1 << leaf
                if entry[2] & bit:
                    entry[2] = -1
                else:
                    entry[2] |= bit
                    if truth:
                        entry[3] |= bit
        return value

    def _bool_end(index, value):
        entry = _find(index, id(_getframe(1)))
        if entry is not None:
            _bstack.pop()
            key = _key[0]
            if key is not None:
                _patterns.setdefault(key, set()).add((index, entry[2], entry[3] if entry[2] != -1 else 0))
        return value

    def _bool_flush():
        while _bstack:
            _abandon(_bstack.pop())

    builtins.__br_bool_begin__ = _bool_begin
    builtins.__br_bool_leaf__ = _bool_leaf
    builtins.__br_bool_end__ = _bool_end

    class _Rewrite(ast.NodeTransformer):
        def __init__(self, rel):
            self.rel = rel

        def visit_Compare(self, node):
            self.generic_visit(node)
            if len(node.ops) != 1 or type(node.ops[0]) not in _OPS:
                return node
            index = len(_sites)
            _sites.append((self.rel, node.lineno, node.col_offset, _OPS.index(type(node.ops[0]))))
            _state.append([-1, None])
            call = ast.Call(
                func=ast.Name(id="__br_compare__", ctx=ast.Load()),
                args=[node.left, node.comparators[0], ast.Constant(value=index)],
                keywords=[],
            )
            return ast.copy_location(call, node)

        def visit_BoolOp(self, node):
            index = len(_bsites)
            _bsites.append(None)
            count = [0]
            inner = self._bool(node, index, count)
            _bsites[index] = (self.rel, node.lineno, node.col_offset, count[0])
            begin = ast.Call(func=ast.Name(id="__br_bool_begin__", ctx=ast.Load()), args=[ast.Constant(value=index)], keywords=[])
            call = ast.Call(func=ast.Name(id="__br_bool_end__", ctx=ast.Load()), args=[begin, inner], keywords=[])
            return ast.copy_location(call, node)

        def _bool(self, node, index, count):
            values = []
            for value in node.values:
                if isinstance(value, ast.BoolOp):
                    values.append(self._bool(value, index, count))
                else:
                    leaf = count[0]
                    count[0] += 1
                    call = ast.Call(
                        func=ast.Name(id="__br_bool_leaf__", ctx=ast.Load()),
                        args=[self.visit(value), ast.Constant(value=index), ast.Constant(value=leaf)],
                        keywords=[],
                    )
                    values.append(ast.copy_location(call, value))
            return ast.copy_location(ast.BoolOp(op=node.op, values=values), node)

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
        _bool_flush()
        _key[0] = key
        _context[0] += 1

    def _snapshot():
        _bool_flush()
        return {
            "bsites": _bsites,
            "patterns": {k: sorted(v) for k, v in _patterns.items()},
            "sites": _sites,
            "outcomes": {k: sorted(v) for k, v in _outcomes.items()},
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
