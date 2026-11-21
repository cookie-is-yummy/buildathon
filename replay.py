"""Repeat what tests/test_executor.py::TestExecutor::test_execute_tpcds does with sqlglot,
for chosen queries, on a (patched) tree, and say whether anything came out differently.

That test is the second slowest of the suite (21 s). For every query of
tests/fixtures/optimizer/tpc-ds/tpc-ds.sql it does:

    parent process   expression = parse_one(sql)              -> pickled to a worker
    worker process   mp_execute(expression, meta): optimize, plan, execute over the
                     CSV tables                               -> rows pickled back
    parent process   transpile(sql, write="duckdb") -> DuckDB runs that SQL
                     rows compared with DuckDB's rows

Most of the 21 s is process start-up, loading tables and DuckDB. The three sqlglot calls
are quick (0.2 s for a typical query), and they are the only way sqlglot code reaches the
verdict: through the parsed expression, the rows, and the SQL text given to DuckDB.

This program makes exactly those three calls for the chosen queries, with the arguments
the test uses, and takes a fingerprint of everything they return. The fingerprints for
the pinned code are computed when the image is built (baseline). If a patch only touches
statements that the test executes inside those three calls and nowhere else
(tools/site_replay records where the test executes each line), and the fingerprints of
every query that executes one of those statements are unchanged, then every step of the
test gets the same values as before, and the test ends as it did before: it passes.

Any difference, exception, time-out or doubt means "differs", and the test stays selected.

Executing a plan over the data is the expensive step. For a query in which the patched
statements are only executed BEFORE that step (parsing, optimizing, planning), it is
enough to compare what goes into the step: the plan and the tables. If those are
unchanged, the step runs the same code on the same input. Such queries are replayed
"up to the plan"; the others are executed.

The fingerprint walks the returned objects completely (every attribute, in order, with
shared objects noted as shared) without calling any of their methods.

usage:
  replay.py run <root> <expected.json> <seed> <queries> <queries> [<rel path>=<patched file> ...]
        first list: queries to replay up to the plan; second list: queries to execute
        ("-" for none). Prints "same", or "differs <query>" at the first that differs
  replay.py baseline <root> <output.json>
        the pinned fingerprints, for several hash seeds (run at image build)
  replay.py show <root> <plan|full>
        prints {"<query>": "<fingerprint>"} for all queries

<root> holds sqlglot/ and tests/ of the pinned commit; <queries> are 0-based numbers,
comma separated.
"""
import hashlib
import importlib.machinery
import json
import os
import subprocess
import sys

FIXTURE = "optimizer/tpc-ds/tpc-ds.sql"
DATA = "optimizer/tpc-ds/"
SEEDS = (0, 1, 2, 3, 4, 5)
_PLAIN = (bool, int, float, str, bytes, complex)


def _name(tp):
    return "%s.%s" % (tp.__module__, tp.__qualname__)


class _Mark:
    __slots__ = ("text",)

    def __init__(self, text):
        self.text = text


def _shallow(obj):
    """A description of an object from its plain attributes only."""
    tp = type(obj)
    if obj is None or tp in _PLAIN:
        return "%s:%r" % (tp.__name__, obj)
    parts = [_name(tp)]
    state = []
    for klass in tp.__mro__:
        for slot in klass.__dict__.get("__slots__", ()):
            if slot != "_hash" and hasattr(obj, slot):
                state.append((slot, getattr(obj, slot)))
    state.extend(getattr(obj, "__dict__", {}).items())
    for key, value in state:
        if value is None or type(value) in _PLAIN:
            parts.append("%s=%r" % (key, value))
    if isinstance(obj, _PLAIN) or tp.__module__ == "builtins" and tp not in (list, tuple, dict, set, frozenset):
        parts.append(repr(obj))
    return "\x00".join(parts)


def _walk(obj, out, memo):
    """Append a canonical description of everything reachable from obj."""
    stack = [obj]
    while stack:
        obj = stack.pop()
        tp = type(obj)
        if tp is _Mark:
            out.append(obj.text)
            continue
        if obj is None or tp in _PLAIN:
            out.append("%s:%r" % (tp.__name__, obj))
            continue
        seen = memo.get(id(obj))
        if seen is not None:
            out.append("@%s" % seen[0])
            continue
        memo[id(obj)] = (memo["count"], obj)
        memo["count"] += 1
        if tp in (list, tuple):
            out.append("%s%d(" % (tp.__name__, len(obj)))
            stack.append(_Mark(")"))
            stack.extend(reversed(obj))
        elif tp is dict:
            out.append("dict%d(" % len(obj))
            stack.append(_Mark(")"))
            for key, value in reversed(list(obj.items())):
                stack.append(value)
                stack.append(key)
        elif tp in (set, frozenset):
            # A set has no order of its own. Put the elements in an order that does not
            # depend on the process (by their plain attributes; elements that look the
            # same that way, by their full description), then walk them like a list.
            keyed = {}
            for item in obj:
                keyed.setdefault(_shallow(item), []).append(item)
            ordered = []
            for key in sorted(keyed):
                group = keyed[key]
                if len(group) > 1:
                    group.sort(key=lambda item: "\x00".join(_walk(item, [], dict(memo))))
                ordered.extend(group)
            out.append("%s%d(" % (tp.__name__, len(ordered)))
            stack.append(_Mark(")"))
            stack.extend(reversed(ordered))
        elif isinstance(obj, type) or callable(obj) and hasattr(obj, "__qualname__"):
            out.append("<%s.%s>" % (getattr(obj, "__module__", "?"), obj.__qualname__))
        elif tp.__module__ == "builtins" or isinstance(obj, _PLAIN):
            # other built-in values, and subclasses of plain values (enum members)
            out.append("%s:%r" % (_name(tp), obj))
        else:
            out.append(_name(tp) + "{")
            stack.append(_Mark("}"))
            state = []
            for klass in tp.__mro__:
                for slot in klass.__dict__.get("__slots__", ()):
                    if slot in ("__dict__", "__weakref__"):
                        continue
                    try:
                        value = getattr(obj, slot)
                    except AttributeError:
                        state.append((slot, _Mark("<unset>")))
                        continue
                    if slot == "_hash":
                        # a cached hash: its value changes from process to process
                        value = _Mark("<hash>" if value is not None else "<no hash>")
                    state.append((slot, value))
            if hasattr(obj, "__dict__"):
                state.extend(obj.__dict__.items())
            elif not state:
                # nothing to look into (a date, a Decimal, ...): its printed form
                state.append(("repr", repr(obj)))
            for key, value in reversed(state):
                stack.append(value)
                stack.append(_Mark(key + "="))
    return out


def fingerprint(objects, known=()):
    """`known`: (object, label) pairs for objects that are inputs, described by their label."""
    out = []
    memo = {"count": 0}  # id -> (number in order of first visit, the object); labels for inputs
    for obj, label in known:
        memo[id(obj)] = (label, obj)
    for obj in objects:
        _walk(obj, out, memo)
        out.append("|")
    return hashlib.sha1("\x00".join(out).encode("utf-8", "backslashreplace")).hexdigest()


def install(root, patched):
    """Import sqlglot and tests from `root`, with the files in `patched` ({relative path:
    file holding the new text}) in place of the ones on disk."""
    sys.dont_write_bytecode = True
    sources = {os.path.join(root, rel): path for rel, path in patched.items()}

    class Loader(importlib.machinery.SourceFileLoader):
        def get_code(self, fullname):
            path = self.get_filename(fullname)
            if path in sources:
                with open(sources[path], "rb") as handle:
                    return compile(handle.read(), path, "exec", dont_inherit=True)
            return super().get_code(fullname)

    hook = importlib.machinery.FileFinder.path_hook(
        (importlib.machinery.ExtensionFileLoader, importlib.machinery.EXTENSION_SUFFIXES),
        (Loader, importlib.machinery.SOURCE_SUFFIXES),
        (importlib.machinery.SourcelessFileLoader, importlib.machinery.BYTECODE_SUFFIXES),
    )
    for position, existing in enumerate(sys.path_hooks):
        if "FileFinder" in repr(existing):
            sys.path_hooks[position] = hook
            break
    else:
        sys.path_hooks.append(hook)
    sys.path_importer_cache.clear()
    sys.path.insert(0, root)


def _call(function, *args, **kwargs):
    """One of the three calls the test makes. tools/record_replay.py replaces this (and
    the two below) to note which lines each query executes where."""
    return function(*args, **kwargs)


def _run(function):
    """The expensive step: executing a plan over the data."""
    return function()


def _apart(function, *args):
    """Something the test does elsewhere (set-up), done here in the middle of a call."""
    return function(*args)


class Tables(dict):
    """The test's tables, each loaded when first asked for. Built the way
    TestExecutor.setUpClass builds them and, like the worker's copy, unpickled."""

    def __init__(self, root, schema):
        super().__init__()
        self.directory = os.path.join(root, "tests", "fixtures", DATA)
        self.schema = schema

    def __missing__(self, name):
        if name not in self.schema:
            raise KeyError(name)
        table = self[name] = _apart(self.load, name)
        return table

    def load(self, name):
        import ast
        import csv
        import gzip
        import pickle

        from sqlglot.executor.table import Table

        rows = []
        ctypes = []
        with gzip.open(os.path.join(self.directory, name + ".csv.gz"), "rt", newline="") as handle:
            reader = csv.reader(handle, delimiter="|")
            next(reader)
            for row in reader:
                if not ctypes:
                    for value in row:
                        try:
                            ctypes.append(type(ast.literal_eval(value)))
                        except (ValueError, SyntaxError):
                            ctypes.append(str)
                rows.append(tuple(None if (tp is not str and value == "") else tp(value) for tp, value in zip(ctypes, row)))
        return pickle.loads(pickle.dumps(Table(columns=self.schema[name], rows=rows)))


def worker(expression, meta, schema, tables, run):
    """tests/test_executor.py: mp_execute, and inside it sqlglot/executor/__init__.py:
    execute, statement for statement, so that it can stop before the plan is executed.
    (A patch to that function itself is therefore never replayed: its lines are not in
    the map.) Returns the rows, or with run=False what the last step would be given."""
    import sqlglot.executor as executor
    from sqlglot import find_tables

    if not meta.get("execute"):
        return None

    used = {}

    for t in find_tables(expression):
        name = t.name
        used[name] = tables[name]

    # execute(expression, schema=schema, tables=used)
    dialect = None
    tables_ = executor.ensure_tables(used, dialect=dialect)
    if not schema:
        raise ValueError("the test always passes a schema")
    schema = executor.ensure_schema(schema, dialect=dialect)
    if tables_.supported_table_args and tables_.supported_table_args != schema.supported_table_args:
        raise executor.ExecuteError("Tables must support the same table args as schema")
    expression = executor.optimize(expression, schema, leave_tables_isolated=True, dialect=dialect)
    expression.sql(pretty=True)  # the argument of a debug log line
    plan = executor.Plan(expression)
    if not run:
        return tables_, plan
    return _run(lambda: executor.PythonExecutor(tables=tables_).execute(plan))


def replay_one(meta, sql, schema, tables, run):
    """What the test's sqlglot calls return for one query, as a fingerprint. With
    run=False: what they return up to the point where the plan would be executed."""
    import pickle
    from multiprocessing.reduction import ForkingPickler

    from sqlglot import parse_one, transpile

    try:
        parsed = _call(parse_one, sql)
        # the worker gets a pickled copy
        expression = pickle.loads(bytes(ForkingPickler.dumps(parsed)))
        result = _call(worker, expression, meta, schema, tables, run)
        if result is None:
            return fingerprint((parsed, expression))
        # the parent asks DuckDB
        duck = _call(transpile, sql, write="duckdb")
        if run:
            # the parent gets a pickled copy of the rows
            result = pickle.loads(bytes(ForkingPickler.dumps(result)))
            return fingerprint((parsed, expression, result, duck))
        # Two parts. The order of the worker's tables follows the order of a set, which
        # changes with the hash seed; everything else normally does not. Keeping them
        # apart lets the selector see which seeds really behave differently.
        tables_, plan = result
        known = [(table, "table " + name) for name, table in sorted(tables.items())]
        return fingerprint((parsed, expression, plan, duck)) + ":" + fingerprint((tables_,), known)
    except RecursionError:
        raise
    except Exception as error:
        return "raised " + type(error).__name__


def prepare(root, patched):
    """The suite's (meta, sql, expected) triples, the worker's schema and its tables."""
    import pickle

    sys.setrecursionlimit(20000)
    install(root, patched)
    import sqlglot.executor  # noqa: F401  what tests/test_executor.py imports at its top
    import sqlglot.executor.python  # noqa: F401
    import sqlglot.planner  # noqa: F401
    import tests.helpers as helpers

    schema = pickle.loads(pickle.dumps(helpers.TPCDS_SCHEMA))
    return list(helpers.load_sql_fixture_pairs(FIXTURE)), schema, Tables(root, schema)


def baseline(root, out):
    """Fingerprints of every query, both kinds, for several hash seeds; seed 0 twice, to
    find queries whose fingerprint is not even stable for one seed."""
    runs = []
    for seed in SEEDS + (SEEDS[0],):
        for kind in ("plan", "full"):
            runs.append((str(seed), kind, subprocess.Popen(
                [sys.executable, "-B", "-s", os.path.abspath(__file__), "show", root, kind],
                env=dict(os.environ, PYTHONHASHSEED=str(seed)), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)))
    result = {"plan": {}, "full": {}}
    unstable = set()
    for seed, kind, process in runs:
        text = process.communicate()[0]
        if process.returncode:
            raise SystemExit("baseline run failed")
        got = json.loads(text)
        for query, value in got.items():
            if value.startswith("raised") or result[kind].get(seed, got).get(query) != value:
                unstable.add(query)
        result[kind][seed] = got
    result["unstable"] = sorted(unstable, key=int)
    json.dump(result, open(out, "w"))
    print("baseline: %d queries, %d seeds, unstable %s" % (len(result["full"]["0"]), len(SEEDS), result["unstable"]))


def main(argv):
    mode, root = argv[1], argv[2]
    if mode == "baseline":
        import dateutil.relativedelta  # noqa: F401  the test environment has these two;
        import mypy_extensions  # noqa: F401     without them sqlglot takes other paths
        return baseline(root, argv[3])
    if mode == "show":
        pairs, schema, tables = prepare(root, {})
        print(json.dumps({str(number): replay_one(meta, sql, schema, tables, argv[3] == "full")
                          for number, (meta, sql, _) in enumerate(pairs)}))
        return
    expected, seed = json.load(open(argv[3])), argv[4]
    wanted = [[int(n) for n in text.split(",")] if text != "-" else [] for text in argv[5:7]]
    patched = dict(item.split("=", 1) for item in argv[7:])
    pairs, schema, tables = prepare(root, patched)
    for run, numbers in ((False, wanted[0]), (True, wanted[1])):
        for number in numbers:
            meta, sql, _ = pairs[number]
            if replay_one(meta, sql, schema, tables, run) != expected["full" if run else "plan"][seed].get(str(number)):
                print("differs %d" % number)
                return
    print("same")


if __name__ == "__main__":
    main(sys.argv)
