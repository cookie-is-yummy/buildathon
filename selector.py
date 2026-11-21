"""Blast Radius selector.

For every patch in /data/commits, print one JSON line naming the tests to run and how to
split them over workers. See NOTES.md for the reasoning behind each rule.

The selector never looks a patch up anywhere. It reads the patch, rebuilds the patched
file from the pinned sqlglot source, compares syntax trees, and asks maps recorded at the
pinned commit which tests execute the changed statements, import the changed module or
open the changed file. Whenever it cannot reason about a change it runs every test.
"""

import ast
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("BLAST_DATA", "/data")
MAPS = os.path.join(HERE, "maps")
PINNED = os.path.join(HERE, "pinned")

WORKER_COST_MS = 2000
MAX_WORKERS = 8
# Wall-clock budget for the whole batch. Past it, every remaining commit runs all tests.
TIME_BUDGET_SECONDS = float(os.environ.get("BLAST_BUDGET", "100"))

# A new file with one of these names can change how pytest or Python itself behaves.
GLOBAL_FILE_NAMES = {
    "conftest.py", "pytest.ini", ".pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml",
    "sitecustomize.py", "usercustomize.py", "__init__.py", "pytest.toml", ".pytest.toml",
}


def log(*parts):
    print(*parts, file=sys.stderr)


class RunEverything(Exception):
    """Raised when a change cannot be narrowed safely."""


# --------------------------------------------------------------------------- maps


class Maps:
    """Read-only view of the recordings made at the pinned commit (see tools/)."""

    def __init__(self, test_ids):
        map_ids = json.load(open(os.path.join(MAPS, "tests.json")))
        number_of = {tid: n for n, tid in enumerate(test_ids)}
        # bit position in the maps -> test number in this run's tests.json
        self.number_of_bit = [number_of.get(tid) for tid in map_ids]
        known = set(map_ids)
        # Tests the maps have never seen are added to every non-empty selection.
        self.unknown = [n for n, tid in enumerate(test_ids) if tid not in known]
        files = json.load(open(os.path.join(MAPS, "files.json")))
        self.tracked = set(files["tracked"])
        self.tracked_dirs = set()
        for f in self.tracked:
            while "/" in f:
                f = f.rsplit("/", 1)[0]
                self.tracked_dirs.add(f)
        self._tables = files
        self._lines = {}
        self._attrs = {}
        self._index = None
        self.all_bits = (1 << len(map_ids)) - 1

    def index(self, path):
        """Line ranges of the pinned file's top-level statements and class-body statements."""
        if self._index is None:
            name = os.path.join(MAPS, "index.json")
            self._index = json.load(open(name)) if os.path.exists(name) else {}
        return self._index.get(path)

    def fact(self, name):
        """A yes/no property of the pinned tree, checked when the maps were built."""
        return bool(self._tables.get("facts", {}).get(name))

    def _get(self, table, key):
        return int(self._tables[table].get(key, "0"), 16)

    def importers(self, path):
        """Tests in whose process tree the import system loaded this file."""
        return self._get("imports", path)

    def readers(self, path):
        """Tests during which repository code (sqlglot or the tests) opened this file."""
        return self._get("data", path)

    def infra_readers(self, path):
        """Tests during which something else (pytest reading its config) opened it."""
        return self._get("infra_opens", path)

    def listers(self, directory):
        """Tests during which repository code listed this directory."""
        return self._get("lists", directory or ".")

    def tried_importing(self, module):
        """Tests that tried to import this module name and did not get it."""
        return self._get("failed_imports", module)

    def _file(self, path):
        table = self._lines.get(path)
        if table is None:
            name = os.path.join(MAPS, "lines", path.replace("/", "__") + ".json")
            if os.path.exists(name):
                raw = json.load(open(name))
                table = (raw["runs"], raw["calls"], raw["sets"], {})
            else:
                table = ([], [], [], {})
            self._lines[path] = table
        return table

    def covering(self, path, first, last):
        """Tests that executed any line in first..last of the pinned file."""
        runs, _, sets, cache = self._file(path)
        bits = 0
        seen = set()
        for start, end, set_index in runs:
            if start > last:
                break
            if end >= first and set_index not in seen:
                seen.add(set_index)
                value = cache.get(set_index)
                if value is None:
                    value = cache[set_index] = int(sets[set_index], 16)
                bits |= value
        return bits

    def started(self, path, first, last):
        """Tests that started running a function, lambda or class body whose first line
        (the first decorator, for a decorated function) is in first..last."""
        _, calls, sets, cache = self._file(path)
        bits = 0
        for line, set_index in calls:
            if line > last:
                break
            if line >= first:
                value = cache.get(set_index)
                if value is None:
                    value = cache[set_index] = int(sets[set_index], 16)
                bits |= value
        return bits

    def attribute_readers(self, path, qualname, name):
        """(tests, used_by_hooks) for class attribute `name` assigned in the body of class
        `qualname`: the tests that read it (through the class, a subclass or an instance) or
        anything computed from it, and whether class-creation hooks read it. None when the
        attribute was not instrumented."""
        value = self._attributes(path).get(qualname, {}).get(name)
        if value is None:
            return None
        if value.startswith("!"):
            return int(value[1:], 16), True
        return int(value, 16), False

    def _attributes(self, path):
        table = self._attrs.get(path)
        if table is None:
            name = os.path.join(MAPS, "attrs", path.replace("/", "__") + ".json")
            table = json.load(open(name)) if os.path.exists(name) else {}
            self._attrs[path] = table
        return table

    def numbers(self, bits):
        out = []
        table = self.number_of_bit
        while bits:
            low = bits & -bits
            number = table[low.bit_length() - 1]
            if number is not None:
                out.append(number)
            bits ^= low
        return out


# --------------------------------------------------------------------------- patch parsing


class FileChange:
    __slots__ = ("old", "new", "status", "binary", "hunks", "mode_only")

    def __init__(self):
        self.old = self.new = None
        self.status = "M"
        self.binary = False
        self.hunks = []
        self.mode_only = False


def _strip_prefix(path):
    path = path.strip()
    if path.startswith('"'):
        raise RunEverything("quoted path")
    if path == "/dev/null":
        return None
    if path.startswith(("a/", "b/")):
        return path[2:]
    raise RunEverything("path without a/ or b/ prefix")


def parse_patch(text):
    """Parse a git unified diff into FileChange objects. Anything unexpected raises."""
    lines = text.split("\n")
    changes = []
    current = None
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("diff --git "):
            current = FileChange()
            changes.append(current)
            rest = line[len("diff --git "):]
            # "a/x b/x": split in the middle when both halves are the same path
            if '"' in rest:
                raise RunEverything("quoted path")
            half = (len(rest) - 1) // 2
            if rest[half] == " " and rest[:half][2:] == rest[half + 1:][2:]:
                current.old = current.new = rest[:half][2:]
            i += 1
            continue
        if line.startswith("--- ") and (current is None or current.hunks or current.binary):
            # a plain `diff -u` section without a "diff --git" line
            current = FileChange()
            changes.append(current)
        if current is None:
            if line.strip() == "" or not line.startswith(("+++", "@@")):
                i += 1  # leading commit message or mail headers
                continue
            raise RunEverything("hunk before any file header")
        if line == "-- " and (current.hunks or current.binary):
            break  # the signature that `git format-patch` appends
        if line.startswith("@@"):
            try:
                header = line.split("@@")[1].strip().split(" ")
                old_start, _, old_len = header[0][1:].partition(",")
                new_start, _, new_len = header[1][1:].partition(",")
                old_start, new_start = int(old_start), int(new_start)
                old_len = int(old_len) if old_len else 1
                new_len = int(new_len) if new_len else 1
            except (IndexError, ValueError):
                raise RunEverything("bad hunk header")
            body = []
            i += 1
            seen_old = seen_new = 0
            while i < n and (seen_old < old_len or seen_new < new_len):
                row = lines[i]
                tag = row[:1]
                if tag == " " or row == "":
                    seen_old += 1
                    seen_new += 1
                    body.append((" ", row[1:]))
                elif tag == "-":
                    seen_old += 1
                    body.append(("-", row[1:]))
                elif tag == "+":
                    seen_new += 1
                    body.append(("+", row[1:]))
                elif tag == "\\":
                    body.append(("\\", row))
                else:
                    raise RunEverything("bad hunk line")
                i += 1
            if i < n and lines[i].startswith("\\"):
                body.append(("\\", lines[i]))
                i += 1
            current.hunks.append((old_start, old_len, new_start, new_len, body))
            continue
        if line.startswith("--- "):
            current.old = _strip_prefix(line[4:])
            if current.old is None:
                current.status = "A"
        elif line.startswith("+++ "):
            current.new = _strip_prefix(line[4:])
            if current.new is None:
                current.status = "D"
        elif line.startswith("new file mode"):
            current.status = "A"
        elif line.startswith("deleted file mode"):
            current.status = "D"
        elif line.startswith("rename from "):
            current.status = "R"
            current.old = line[len("rename from "):].strip()
        elif line.startswith("rename to "):
            current.status = "R"
            current.new = line[len("rename to "):].strip()
        elif line.startswith(("copy from ", "copy to ")):
            raise RunEverything("copy")
        elif line.startswith(("GIT binary patch", "Binary files ")):
            current.binary = True
            i += 1
            while i < n and not lines[i].startswith("diff --git "):
                i += 1
            continue
        elif line.startswith(("old mode", "new mode")):
            current.mode_only = True
        elif line.startswith(("index ", "similarity index", "dissimilarity index")) or line.strip() == "":
            pass
        else:
            raise RunEverything("unknown patch line")
        i += 1
    if not changes:
        raise RunEverything("no file changes found")
    seen_paths = set()
    for change in changes:
        for path in {change.old, change.new} - {None}:
            if path in seen_paths:
                raise RunEverything("a file appears twice in the patch")
            seen_paths.add(path)
    for change in changes:
        if change.status == "A":
            change.old = None
        if change.status == "D":
            change.new = None
        if change.old is None and change.new is None:
            raise RunEverything("file change without a path")
        if change.hunks or change.binary or change.status != "M":
            change.mode_only = False
    return changes


def apply_hunks(old_text, hunks):
    """Rebuild the new file. Returns (new_text, old_blocks, new_blocks).

    Blocks are (first, last) 1-based inclusive line ranges that changed. An insertion has an
    empty old block (first = last + 1) and a deletion an empty new block.
    """
    old_lines = old_text.split("\n")
    out = []
    old_blocks, new_blocks = [], []
    position = 0  # index into old_lines
    old_unterminated = new_unterminated = False
    for old_start, old_len, new_start, new_len, body in hunks:
        start = old_start - 1 if old_len else old_start
        if start < position:
            raise RunEverything("overlapping hunks")
        out.extend(old_lines[position:start])
        position = start
        block_old = block_new = None
        previous = None
        for tag, text in body:
            if tag == " ":
                if position >= len(old_lines) or old_lines[position] != text:
                    raise RunEverything("context does not match the pinned source")
                out.append(text)
                position += 1
                block_old = block_new = None
            elif tag == "-":
                if position >= len(old_lines) or old_lines[position] != text:
                    raise RunEverything("removed line does not match the pinned source")
                if block_old is None:
                    block_old = [position + 1, position]
                    block_new = [len(out) + 1, len(out)]
                    old_blocks.append(block_old)
                    new_blocks.append(block_new)
                block_old[1] = position + 1
                position += 1
            elif tag == "+":
                if block_old is None:
                    block_old = [position + 1, position]
                    block_new = [len(out) + 1, len(out)]
                    old_blocks.append(block_old)
                    new_blocks.append(block_new)
                out.append(text)
                block_new[1] = len(out)
            else:  # "\ No newline at end of file" refers to the line just before it
                if previous in ("-", " "):
                    old_unterminated = True
                if previous in ("+", " "):
                    new_unterminated = True
            previous = tag
    out.extend(old_lines[position:])
    new_text = "\n".join(out)
    if new_unterminated and not old_unterminated and new_text.endswith("\n"):
        new_text = new_text[:-1]
    elif old_unterminated and not new_unterminated:
        new_text += "\n"
    return new_text, old_blocks, new_blocks


# --------------------------------------------------------------------------- syntax tree comparison

_BODY_FIELDS = ("body", "orelse", "finalbody")


def _dump(node):
    return ast.dump(node, annotate_fields=False, include_attributes=False)


def _touches(node, blocks):
    first, last = node.lineno, node.end_lineno
    decorators = getattr(node, "decorator_list", None)
    if decorators:
        first = min(first, decorators[0].lineno)
    for block_first, block_last in blocks:
        if block_first <= block_last:
            if first <= block_last and last >= block_first:
                return True
        elif first < block_first <= last:  # insertion strictly inside the node
            return True
    return False


def _header_dump(node):
    """Everything of a compound statement except its nested statement lists."""
    parts = [type(node).__name__]
    for name, value in ast.iter_fields(node):
        if name in _BODY_FIELDS and isinstance(value, list):
            continue
        if name == "handlers":
            parts.append(repr([(h.type and _dump(h.type), h.name) for h in value]))
        elif name == "cases":
            parts.append(repr([(_dump(c.pattern), c.guard and _dump(c.guard)) for c in value]))
        elif isinstance(value, list):
            parts.append(repr([_dump(v) if isinstance(v, ast.AST) else v for v in value]))
        elif isinstance(value, ast.AST):
            parts.append(_dump(value))
        else:
            parts.append(repr(value))
    return "|".join(parts)


def _header_last_line(node):
    """Last line of the part of a compound statement that precedes its first nested block."""
    body = getattr(node, "body", None)
    if body and isinstance(body, list) and body[0].lineno > node.lineno:
        return body[0].lineno - 1
    return node.lineno


_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _scope_signature(function):
    """Facts about a function that a one-statement edit can change for the whole function:
    which names each scope binds, and which scopes are generators or declare globals."""
    out = []

    def visit(scope):
        bound, flags, nested = set(), set(), []
        stack = list(ast.iter_child_nodes(scope))
        while stack:
            node = stack.pop()
            kind = type(node)
            if kind in _SCOPES:
                if kind is not ast.Lambda:
                    bound.add(node.name)
                nested.append(node)
                continue
            if kind is ast.Name:
                if not isinstance(node.ctx, ast.Load):
                    bound.add(node.id)
            elif kind in (ast.Yield, ast.YieldFrom):
                flags.add("yield")
            elif kind is ast.Await:
                flags.add("await")
            elif kind in (ast.Global, ast.Nonlocal):
                flags.add((kind.__name__, tuple(sorted(node.names))))
            elif kind is ast.arg:
                bound.add(node.arg)
            elif kind is ast.alias:
                bound.add(node.asname or node.name.split(".")[0])
            elif kind is ast.ExceptHandler and node.name:
                bound.add(node.name)
            elif kind in (ast.MatchAs, ast.MatchStar) and node.name:
                bound.add(node.name)
            elif kind is ast.MatchMapping and node.rest:
                bound.add(node.rest)
            stack.extend(ast.iter_child_nodes(node))
        out.append((type(scope).__name__, getattr(scope, "name", ""), frozenset(bound), frozenset(flags)))
        for inner in sorted(nested, key=lambda n: (n.lineno, n.col_offset)):
            visit(inner)

    visit(function)
    return out


class Diff:
    """What a Python file change amounts to, in terms of the pinned file's lines."""

    def __init__(self):
        self.whole_module = False  # statements added or removed at module or class level
        self.statements = []  # (first, last) line ranges of statements whose own code changed
        self.functions = []  # (first, last) whole-definition ranges of functions changed structurally
        self.lambdas = []  # lines of module/class level lambdas whose body alone changed
        # class attributes whose assigned value changed:
        # (class qualname, attribute, class node, (first, last) lines of the assignment)
        self.attributes = []
        self.docstrings = 0  # class or module docstrings whose text changed
        self.changed_top = []  # new-side module/class level statements that may differ
        self.reasons = []


_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)
_NESTING = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.If, ast.For, ast.AsyncFor, ast.While,
            ast.With, ast.AsyncWith, ast.Try, ast.Match) + ((ast.TryStar,) if hasattr(ast, "TryStar") else ())
_COMPOUND = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try, ast.Match) + (
    (ast.TryStar,) if hasattr(ast, "TryStar") else ()
)


def _function_body_range(function):
    """The whole definition, decorators included. Used with Maps.started(): a test is
    affected by a structural change to a function only if it started running the function
    or something defined inside it."""
    return _def_first_line(function), function.end_lineno


def _only_lambda_bodies_differ(a, b, found):
    """True when two expressions differ only inside the bodies of lambdas whose parameters
    are unchanged. The differing lambdas' first lines are appended to `found`."""
    if type(a) is not type(b):
        return False
    if isinstance(a, ast.Lambda):
        if _dump(a.args) != _dump(b.args):
            return False
        if _dump(a.body) != _dump(b.body):
            found.append(a.lineno)
        return True
    for (name, x), (_, y) in zip(ast.iter_fields(a), ast.iter_fields(b)):
        if isinstance(x, list):
            if not isinstance(y, list) or len(x) != len(y):
                return False
            for p, q in zip(x, y):
                if isinstance(p, ast.AST):
                    if not isinstance(q, ast.AST) or not _only_lambda_bodies_differ(p, q, found):
                        return False
                elif p != q:
                    return False
        elif isinstance(x, ast.AST):
            if not isinstance(y, ast.AST) or not _only_lambda_bodies_differ(x, y, found):
                return False
        elif x != y or type(x) is not type(y):
            return False
    return True


def _def_first_line(node):
    return min([node.lineno] + [d.lineno for d in node.decorator_list])


def _def_header(node):
    if isinstance(node, ast.ClassDef):
        return repr((node.name, [_dump(b) for b in node.bases], [_dump(k) for k in node.keywords],
                     [_dump(d) for d in node.decorator_list], [_dump(p) for p in getattr(node, "type_params", [])]))
    return repr((type(node).__name__, node.name, _dump(node.args), [_dump(d) for d in node.decorator_list],
                 node.returns and _dump(node.returns), [_dump(p) for p in getattr(node, "type_params", [])]))


def _only_constant_defaults_differ(a, b):
    """True when two function headers differ only in default values that are plain
    constants on both sides (evaluating them at import cannot fail or have effects)."""
    if (a.name != b.name or type(a) is not type(b)
            or [_dump(d) for d in a.decorator_list] != [_dump(d) for d in b.decorator_list]
            or (a.returns and _dump(a.returns)) != (b.returns and _dump(b.returns))
            or getattr(a, "type_params", []) or getattr(b, "type_params", [])):
        return False
    x, y = a.args, b.args
    for field in ("posonlyargs", "args", "kwonlyargs"):
        if [_dump(n) for n in getattr(x, field)] != [_dump(n) for n in getattr(y, field)]:
            return False
    if (x.vararg and _dump(x.vararg)) != (y.vararg and _dump(y.vararg)):
        return False
    if (x.kwarg and _dump(x.kwarg)) != (y.kwarg and _dump(y.kwarg)):
        return False
    old_defaults, new_defaults = x.defaults + x.kw_defaults, y.defaults + y.kw_defaults
    if len(x.defaults) != len(y.defaults) or len(x.kw_defaults) != len(y.kw_defaults):
        return False
    for p, q in zip(old_defaults, new_defaults):
        if (p is None) != (q is None):
            return False
        if p is not None and _dump(p) != _dump(q):
            if not (isinstance(p, ast.Constant) and isinstance(q, ast.Constant)):
                return False
    return True


def _runs(statement):
    """False for statements that execute nothing (and so never show up in a recording)."""
    if isinstance(statement, (ast.Pass, ast.Global, ast.Nonlocal)):
        return False
    if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant):
        return False
    return not (isinstance(statement, ast.AnnAssign) and statement.value is None)


def _statement_range(statement):
    first = statement.lineno
    for decorator in getattr(statement, "decorator_list", ()):
        first = min(first, decorator.lineno)
    return first, statement.end_lineno


def _diff_reshaped_body(old_body, new_body, diff):
    """Statements were added to, removed from or replaced in a block inside a function.

    Line the two blocks up on their unchanged statements. A test's run can first differ
    from before only where it meets a removed or replaced statement (it executed that
    statement before, so the statement's lines name it) or where it reaches an inserted
    one (it then went on to execute the next old statement that runs anything, or, at the
    end of the block, had executed the previous one). Returns False if no such anchor
    exists; the caller then falls back to everyone who ran the function.
    """
    import difflib

    old_dumps = [_dump(x) for x in old_body]
    new_dumps = [_dump(x) for x in new_body]
    ranges = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old_dumps, new_dumps, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        for statement in old_body[i1:i2]:
            ranges.append(_statement_range(statement))
        if j2 > j1 and any(_runs(x) for x in new_body[j1:j2]):
            anchor = next((x for x in old_body[i1:] if _runs(x)), None)
            if anchor is None:
                anchor = next((x for x in reversed(old_body[:i1]) if _runs(x)), None)
            if anchor is None:
                return False
            ranges.append(_statement_range(anchor))
    diff.statements.extend(ranges)
    diff.reasons.append("statements added or removed inside a function")
    return True


def _diff_bodies(old_body, new_body, function, diff, old_blocks, new_blocks, pairs, owner=None):
    if len(old_body) != len(new_body) or any(type(a) is not type(b) for a, b in zip(old_body, new_body)):
        if function is None:
            diff.whole_module = True
            diff.reasons.append("statements added or removed at module or class level")
        elif not _diff_reshaped_body(old_body, new_body, diff):
            diff.functions.append(_function_body_range(function))
            diff.reasons.append("statements added or removed in %s" % function.name)
        return
    for a, b in zip(old_body, new_body):
        if not _touches(a, old_blocks) and not _touches(b, new_blocks):
            continue
        # Definitions and compound statements are compared piece by piece below; dumping a
        # whole class to compare it would cost more than everything else put together.
        if not isinstance(a, _NESTING) and _dump(a) == _dump(b):
            continue
        if function is None:
            diff.changed_top.append(b)
        _diff_statement(a, b, function, diff, old_blocks, new_blocks, pairs, owner)


def _diff_statement(a, b, function, diff, old_blocks, new_blocks, pairs, owner=None):
    if isinstance(a, _DEFS) or isinstance(a, ast.ClassDef):
        if _def_header(a) != _def_header(b):
            if function is None and isinstance(a, _DEFS) and _only_constant_defaults_differ(a, b):
                # A default value is only looked at when the function is called.
                diff.functions.append(_function_body_range(a))
                diff.reasons.append("default argument of %s changed" % a.name)
            elif function is None:
                # The def or class statement runs when its module (or class body) does:
                # whoever executes the header line depends on it.
                diff.statements.append((_def_first_line(a), _header_last_line(a)))
                diff.reasons.append("header of %s changed" % a.name)
                if isinstance(a, _DEFS):
                    diff.functions.append(_function_body_range(a))
            else:
                diff.functions.append(_function_body_range(function))
                return
        if isinstance(a, ast.ClassDef):
            inner = None
            if function is None:
                # owner: (qualified name, node) of the class whose body we are in, when
                # that class sits at module level or directly inside such classes
                inner = ((owner[0] + "." if owner else "") + a.name, a) if owner is not False else False
            _diff_bodies(a.body, b.body, function, diff, old_blocks, new_blocks, pairs, inner)
            return
        outer = function
        if outer is None:
            outer = a
            pairs.append((a, b))
        _diff_bodies(a.body, b.body, outer, diff, old_blocks, new_blocks, pairs)
        return
    if isinstance(a, _COMPOUND):
        # inside `if`/`try` blocks of a class body we no longer track which class we are in
        nested_owner = False if owner else owner
        if _header_dump(a) != _header_dump(b):
            diff.statements.append((a.lineno, _header_last_line(a)))
        for field in _BODY_FIELDS:
            old_part, new_part = getattr(a, field, None), getattr(b, field, None)
            if isinstance(old_part, list) and (old_part or new_part):
                if not old_part and function is not None:
                    # a new `else`/`finally` block: whoever runs the statement's header
                    diff.statements.append((a.lineno, _header_last_line(a)))
                    continue
                _diff_bodies(old_part, new_part or [], function, diff, old_blocks, new_blocks, pairs, nested_owner)
        for field in ("handlers", "cases"):
            old_part, new_part = getattr(a, field, None), getattr(b, field, None)
            if old_part is None:
                continue
            if len(old_part) != len(new_part):
                if function is None:
                    diff.whole_module = True
                else:
                    diff.functions.append(_function_body_range(function))
                return
            for x, y in zip(old_part, new_part):
                _diff_bodies(x.body, y.body, function, diff, old_blocks, new_blocks, pairs, nested_owner)
        return
    # A simple statement. Inside a function, only tests that execute it can notice. At
    # module or class level the same holds: it runs when the module is imported, and the
    # recordings include import time, so its lines list every test that imports the module.
    if _is_string_statement(a) and _is_string_statement(b):
        # A bare string is either a docstring or does nothing at all. Inside a function it
        # is never executed. At class or module level it only sets __doc__.
        if function is None and a.value.value.strip() and b.value.value.strip():
            diff.docstrings += 1
            diff.reasons.append("docstring at line %d" % a.lineno)
            return
    if function is None:
        found = []
        if _only_lambda_bodies_differ(a, b, found) and found:
            # e.g. one entry of a class-level table of lambdas: the table is built the same
            # way at import; only tests that call that lambda run the changed code.
            diff.lambdas.extend(found)
            diff.reasons.append("body of a module or class level lambda at line %d" % found[0])
            return
        target = _assigned_name(a)
        if owner and target and target == _assigned_name(b) and _dump(getattr(a, "annotation", None) or a.targets[0]) == _dump(getattr(b, "annotation", None) or b.targets[0]):
            # Only a change that cannot make the class body itself fail at import is
            # narrowed to the attribute's readers: constants replaced by constants of the
            # same type, or entries removed from a literal.
            if _safe_value_change(a.value, b.value, False):
                diff.attributes.append((owner[0], target, False, (a.lineno, a.end_lineno)))
                diff.reasons.append("class attribute %s.%s" % (owner[0], target))
                return
            if _safe_value_change(a.value, b.value, True):
                diff.attributes.append((owner[0], target, True, (a.lineno, a.end_lineno)))
                diff.reasons.append("class attribute %s.%s (entries removed)" % (owner[0], target))
                return
        diff.reasons.append("module or class level statement at line %d" % a.lineno)
    diff.statements.append((a.lineno, a.end_lineno))


def _subsequence(new, old):
    position = 0
    for item in new:
        while position < len(old) and old[position] != item:
            position += 1
        if position == len(old):
            return False
        position += 1
    return True


def _safe_value_change(a, b, allow_removal):
    """True when value expression `b` differs from `a` only by constants replaced with
    constants of the same type inside literals (and, if allowed, by entries removed from a
    literal). Evaluating such a value at import cannot raise where the old one did not."""
    if type(a) is not type(b):
        return False
    if isinstance(a, ast.Constant):
        return type(a.value) is type(b.value)
    if isinstance(a, ast.UnaryOp):
        return (type(a.op) is type(b.op) and isinstance(a.operand, ast.Constant) and isinstance(b.operand, ast.Constant)
                and type(a.operand.value) is type(b.operand.value) and type(a.operand.value) in (int, float))
    if isinstance(a, (ast.List, ast.Tuple, ast.Set)):
        if len(a.elts) == len(b.elts):
            return all(_safe_value_change(x, y, allow_removal) for x, y in zip(a.elts, b.elts))
        return allow_removal and len(b.elts) < len(a.elts) and _subsequence(
            [_dump(x) for x in b.elts], [_dump(x) for x in a.elts])
    if isinstance(a, ast.Dict):
        if len(a.keys) == len(b.keys):
            for k1, k2, v1, v2 in zip(a.keys, b.keys, a.values, b.values):
                if (k1 is None) != (k2 is None):
                    return False
                if k1 is not None and not _safe_value_change(k1, k2, allow_removal):
                    return False
                if not _safe_value_change(v1, v2, allow_removal):
                    return False
            return True
        return allow_removal and len(b.keys) < len(a.keys) and _subsequence(
            [(k and _dump(k), _dump(v)) for k, v in zip(b.keys, b.values)],
            [(k and _dump(k), _dump(v)) for k, v in zip(a.keys, a.values)])
    return _dump(a) == _dump(b)


def _is_string_statement(statement):
    return (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str))


def _assigned_name(statement):
    """The name a plain `NAME = value` or `NAME: type = value` statement assigns."""
    if isinstance(statement, ast.Assign) and len(statement.targets) == 1 and isinstance(statement.targets[0], ast.Name):
        return statement.targets[0].id
    if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name) and statement.value is not None:
        return statement.target.id
    return None


def _names_read_at_class_time(statement):
    """Names a class-body statement reads while the class body runs (function and lambda
    bodies run later and are skipped; their defaults and decorators are not)."""
    names = set()
    stack = [statement]
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            stack.extend(node.decorator_list)
            stack.extend(node.args.defaults)
            stack.extend(d for d in node.args.kw_defaults if d is not None)
            continue
        if isinstance(node, ast.Lambda):
            stack.extend(node.args.defaults)
            stack.extend(d for d in node.args.kw_defaults if d is not None)
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            names.add(node.id)
        stack.extend(ast.iter_child_nodes(node))
    return names


def _find_chunk(index, old_blocks):
    """Find the one top-level statement, or the one statement of a top-level class body,
    that strictly contains every changed block. Returns (first, last, class name or None)
    or None when the change is not confined that way."""
    if not index or not old_blocks:
        return None

    def inside(first, last):
        for block_first, block_last in old_blocks:
            if block_first <= block_last:
                if not (first <= block_first and block_last <= last):
                    return False
            elif not (first < block_first <= last):  # an insertion must be strictly inside
                return False
        return True

    for first, last, name, children in index:
        if first > old_blocks[0][0]:
            break
        if not inside(first, last):
            continue
        if children:
            for child_first, child_last in children:
                if inside(child_first, child_last):
                    return child_first, child_last, name
        return first, last, None
    return None


def _first_indent(lines, first, last):
    """Leading whitespace of the first line of code in lines first..last."""
    for line in lines[first - 1:last]:
        stripped = line.lstrip()
        if stripped and not stripped.startswith("#"):
            return line[:len(line) - len(stripped)]
    return None


def _parse_chunk(lines, first, last, wrapped):
    """Parse lines first..last of a file on their own. A class-body statement is indented,
    so it is parsed inside a one-line `if` wrapper. Returns the statements, with line
    numbers as in the whole file, or None if the lines do not form whole statements."""
    text = "\n".join(lines[first - 1:last]) + "\n"
    if wrapped:
        tree = ast.parse("if 1:\n" + text)
        if len(tree.body) != 1 or not isinstance(tree.body[0], ast.If) or tree.body[0].orelse:
            return None
        statements = tree.body[0].body
        offset = first - 2
    else:
        tree = ast.parse(text)
        statements = tree.body
        offset = first - 1
    if offset:
        for statement in statements:
            ast.increment_lineno(statement, offset)
    return statements


def diff_python(old_text, new_text, old_blocks, new_blocks, index=None):
    """Compare the pinned and patched source. Returns a Diff, or None when the patched file
    is not valid Python.

    Parsing a 10,000-line module twice costs far more than the rest of the analysis, so
    when every changed line sits strictly inside one top-level statement (or one statement
    of a top-level class), only that statement is parsed, from the old and the new text.
    This is safe: the text before and after it is unchanged, so if the new statement parses
    on its own into exactly one statement, the rest of the file parses as before. If it
    does not, we fall back to parsing the whole file.
    """
    diff = Diff()
    pairs = []
    new_tree = None
    chunk = _find_chunk(index, old_blocks)
    old_statements = new_statements = None
    owner = None
    if chunk is not None:
        first, last, class_name = chunk
        growth = sum((n[1] - n[0]) - (o[1] - o[0]) for o, n in zip(old_blocks, new_blocks))
        try:
            old_statements = _parse_chunk(old_text.split("\n"), first, last, class_name is not None)
        except (SyntaxError, ValueError):
            old_statements = None
        if old_statements is not None and len(old_statements) == 1:
            try:
                new_lines = new_text.split("\n")
                new_statements = _parse_chunk(new_lines, first, last + growth, class_name is not None)
                # The wrapper would accept a statement indented differently from its
                # siblings; the real file would not. Then let the whole-file parse decide.
                if _first_indent(new_lines, first, last + growth) != _first_indent(old_text.split("\n"), first, last):
                    new_statements = None
            except (SyntaxError, ValueError, RecursionError, MemoryError):
                new_statements = None
            if class_name is not None:
                owner = (class_name, None)
    if old_statements is None or new_statements is None or len(old_statements) != 1 or len(new_statements) != 1:
        old_statements = ast.parse(old_text).body
        try:
            new_tree = ast.parse(new_text)
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            return None
        new_statements = new_tree.body
        owner = None
    _diff_bodies(old_statements, new_statements, None, diff, old_blocks, new_blocks, pairs, owner)
    # The compiler also rejects things the parser accepts (`return` outside a function, a
    # name both global and assigned before, ...). Those errors are local to a function or to
    # one module/class level statement, so only the statements that changed are compiled;
    # compiling a whole large module costs ten times more than the rest of the analysis.
    try:
        if diff.whole_module or any(
            isinstance(n, ast.ImportFrom) and n.module == "__future__" for n in diff.changed_top
        ):
            compile(new_tree if new_tree is not None else ast.parse(new_text), "<patched>", "exec", dont_inherit=True)
        else:
            smallest = [n for n in diff.changed_top if not isinstance(n, ast.ClassDef)]
            compile(ast.Module(body=smallest, type_ignores=[]), "<patched>", "exec", dont_inherit=True)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    if diff.statements:
        # A statement edit that changes which names are local, or turns the function into a
        # generator, affects every path through the function, not only the edited statement.
        for old_function, new_function in pairs:
            first, last = old_function.lineno, old_function.end_lineno
            if not any(first <= s[0] <= last for s in diff.statements):
                continue
            if _scope_signature(old_function) != _scope_signature(new_function):
                diff.functions.append(_function_body_range(old_function))
                diff.reasons.append("scope of %s changed" % old_function.name)
    return diff


# --------------------------------------------------------------------------- selection


_CODING = re.compile(r"coding[:=]")


def read_pinned(path):
    with open(os.path.join(PINNED, path), encoding="utf-8", newline="") as handle:
        return handle.read()


def select_python(change, maps, notes):
    path = change.old
    if change.binary:
        raise RunEverything("binary change to a Python file")
    old_text = read_pinned(path)
    new_text, old_blocks, new_blocks = apply_hunks(old_text, change.hunks)
    if "\r" in old_text or "\r" in new_text or "\f" in new_text:
        raise RunEverything("unusual line endings")
    if _CODING.search("\n".join(new_text.split("\n", 2)[:2])):
        raise RunEverything("encoding declaration")
    # Tests during which repository code read this file as data see any change to its text.
    bits = maps.readers(path)
    diff = diff_python(old_text, new_text, old_blocks, new_blocks, maps.index(path))
    if diff is None:
        notes.append("%s no longer compiles: every test importing it" % path)
        return bits | maps.importers(path)
    if diff.whole_module:
        notes.append("%s whole-module change (%s)" % (path, "; ".join(diff.reasons[:2])))
        return bits | maps.importers(path)
    if diff.docstrings and not maps.fact("docstrings_unused"):
        return bits | maps.importers(path)
    for first, last in diff.functions:
        bits |= maps.started(path, first, last)
    for first, last in diff.statements:
        bits |= maps.covering(path, first, last)
    for line in diff.lambdas:
        bits |= maps.started(path, line, line)
    for qualname, name, entries_removed, lines in diff.attributes:
        found = maps.attribute_readers(path, qualname, name)
        if found is None or (entries_removed and found[1]):
            # Not instrumented, or a hook that runs when classes are created reads it and
            # might need an entry that was removed: everyone who runs the assignment.
            bits |= maps.covering(path, lines[0], lines[1])
        else:
            bits |= found[0]
    notes.append("%s: %d statement(s), %d function(s), %d lambda(s), %d attribute(s) %s" % (
        path, len(diff.statements), len(diff.functions), len(diff.lambdas), len(diff.attributes),
        "; ".join(diff.reasons[:2])))
    return bits


def _directory(path):
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _listers_above(path, maps):
    """Tests whose code listed the file's directory or one above it (recursive walks)."""
    bits = 0
    directory = _directory(path)
    while True:
        bits |= maps.listers(directory)
        if not directory:
            return bits
        directory = _directory(directory)


def _module_names(path):
    """Import names a file at this path would answer to."""
    stem, dot, extension = path.rpartition(".")
    names = []
    if extension == "py":
        names.append(stem.replace("/", "."))
    elif extension in ("so", "pyd"):
        directory, _, base = path.rpartition("/")
        names.append((directory + "/" if directory else "").replace("/", ".") + base.split(".")[0])
    return names


def select_added(change, maps, notes):
    path = change.new
    name = path.rsplit("/", 1)[-1]
    if name in GLOBAL_FILE_NAMES or name.endswith(".pth"):
        raise RunEverything("new %s" % name)
    if "/" not in path:
        if name.endswith((".py", ".so", ".pyd", ".cfg", ".ini", ".toml")):
            # The repository root is on the import path: a new top-level module can shadow
            # any import, and a new config file can reconfigure pytest.
            raise RunEverything("new top-level file %s" % name)
    elif path.split("/", 1)[0] not in maps.tracked_dirs:
        raise RunEverything("new top-level directory")
    bits = _listers_above(path, maps)
    for module in _module_names(path):
        bits |= maps.tried_importing(module)
    if path.endswith(".py") and path.startswith("tests/"):
        # pytest may collect it. A new test module cannot fail an existing test unless it
        # does something at import time, so look at what its top level does.
        text = "\n".join(t for tag, t in (row for hunk in change.hunks for row in hunk[4]) if tag == "+")
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            tree = None
        if tree is not None:
            for node in tree.body:
                if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.ClassDef)):
                    continue
                if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Constant):
                    continue
                if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                    continue
                raise RunEverything("new test module with top-level code")
    notes.append("new file %s" % path)
    return bits


def select_removed(path, maps, notes):
    if path not in maps.tracked:
        raise RunEverything("removed file is not in the pinned tree")
    notes.append("removed %s" % path)
    return (maps.importers(path) | maps.readers(path) | maps.infra_readers(path)
            | maps.covering(path, 1, 10 ** 9) | _listers_above(path, maps))


def _ini_is_pytest_neutral(text):
    """True when an ini-style file parses the way pytest's reader (iniconfig) expects and
    has no pytest section. pytest aborts on a file it cannot parse."""
    sections = set()
    names = None
    for raw in text.split("\n"):
        line = raw.rstrip()
        if not line or line.lstrip()[:1] in ("#", ";"):
            continue
        if line[0] == "[":
            if not line.endswith("]") or "pytest" in line:
                return False
            if line in sections:
                return False
            sections.add(line)
            names = set()
        elif line[0] in (" ", "\t"):
            if names is None or not names:
                return False  # continuation of nothing
        else:
            match = re.match(r"([^=:\s][^=:]*?)\s*[=:]", line)
            if match is None or names is None or match.group(1) in names:
                return False
            names.add(match.group(1))
    return True


def select_config(change, maps, notes):
    """pyproject.toml and setup.cfg are read by pytest itself on every run. A change that
    leaves them parseable and without pytest settings cannot alter how tests are run."""
    path = change.old
    if change.binary:
        raise RunEverything("binary change to %s" % path)
    old_text = read_pinned(path)
    new_text, _, _ = apply_hunks(old_text, change.hunks)
    if path == "pyproject.toml":
        import tomllib

        try:
            old_data, new_data = tomllib.loads(old_text), tomllib.loads(new_text)
        except (tomllib.TOMLDecodeError, RecursionError):
            raise RunEverything("pyproject.toml no longer parses")
        if old_data.get("tool", {}).get("pytest") != new_data.get("tool", {}).get("pytest") or "pytest" in new_data.get("tool", {}):
            raise RunEverything("pytest settings in pyproject.toml")
    elif not _ini_is_pytest_neutral(new_text):
        raise RunEverything("%s may affect pytest" % path)
    notes.append("%s: no effect on pytest configuration" % path)
    return maps.readers(path) | maps.importers(path)


PYTEST_CONFIG_FILES = ("pyproject.toml", "setup.cfg")


def select_change(change, maps, notes):
    if change.status == "A":
        if change.new in maps.tracked:
            raise RunEverything("added file already exists")
        return select_added(change, maps, notes)
    if change.status == "D":
        return select_removed(change.old, maps, notes)
    if change.status == "R":
        if (change.hunks or change.binary) and change.new.startswith("tests/") and change.new.endswith(".py"):
            # we only see the changed lines, not the whole new test module
            raise RunEverything("renamed test module with content change")
        if change.new in maps.tracked:
            raise RunEverything("rename onto an existing file")
        return select_removed(change.old, maps, notes) | select_added(change, maps, notes)
    path = change.old
    if path != change.new:
        raise RunEverything("path mismatch")
    if path not in maps.tracked:
        raise RunEverything("modified file is not in the pinned tree")
    if change.mode_only:
        notes.append("%s: mode change only" % path)
        return 0
    if path.endswith(".py"):
        return select_python(change, maps, notes)
    if path in PYTEST_CONFIG_FILES:
        return select_config(change, maps, notes)
    notes.append("%s: tests that open it" % path)
    return maps.readers(path) | maps.infra_readers(path) | maps.importers(path)


def select(patch_text, maps, notes):
    bits = 0
    for change in parse_patch(patch_text):
        bits |= select_change(change, maps, notes)
    return bits


# --------------------------------------------------------------------------- splitting


def split(numbers, duration):
    """Split tests over 1..8 workers, minimising largest load + 2 s per worker (in ms)."""
    if not numbers:
        return []
    order = sorted(numbers, key=lambda n: -duration[n])
    total = sum(duration[n] for n in order)
    longest = duration[order[0]]
    best_cost, best = None, None
    for k in range(1, min(MAX_WORKERS, len(order)) + 1):
        lower_bound = max(longest, -(-total // k)) + WORKER_COST_MS * k
        if best_cost is not None and lower_bound >= best_cost:
            continue
        loads = [0] * k
        workers = [[] for _ in range(k)]
        for n in order:
            j = loads.index(min(loads))
            loads[j] += duration[n]
            workers[j].append(n)
        cost = max(loads) + WORKER_COST_MS * k
        if best_cost is None or cost < best_cost:
            best_cost, best = cost, workers
    return best


# --------------------------------------------------------------------------- main


def main():
    started = time.monotonic()
    out = sys.stdout
    tests = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
    duration = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
    everything = [t["number"] for t in tests]
    run_all = split(everything, duration)
    commits_dir = os.path.join(DATA, "commits")
    commit_ids = sorted(name[:-6] for name in os.listdir(commits_dir) if name.endswith(".patch"))

    maps = None
    try:
        maps = Maps([t["id"] for t in tests])
    except Exception as error:  # without maps the only safe answer is everything
        log("maps failed to load:", repr(error))

    for commit in commit_ids:
        workers = run_all
        notes = []
        try:
            if maps is None:
                raise RunEverything("no maps")
            if time.monotonic() - started > TIME_BUDGET_SECONDS:
                raise RunEverything("time budget")
            with open(os.path.join(commits_dir, commit + ".patch"), encoding="utf-8", newline="") as handle:
                patch_text = handle.read()
            bits = select(patch_text, maps, notes)
            if bits == maps.all_bits:
                numbers = everything
            else:
                numbers = maps.numbers(bits)
                if numbers and maps.unknown:
                    numbers = sorted(set(numbers) | set(maps.unknown))
            workers = split(numbers, duration) if len(numbers) < len(everything) else run_all
            log(commit, len(numbers), "tests;", " | ".join(notes))
        except RunEverything as reason:
            log(commit, "ALL:", reason)
        except BaseException as error:  # never let one commit cost the whole run
            workers = run_all
            log(commit, "ALL after error:", repr(error))
        out.write(json.dumps({"commit": commit, "workers": workers}, separators=(",", ":")) + "\n")
        out.flush()
    log("done in %.2fs" % (time.monotonic() - started))


if __name__ == "__main__":
    main()
