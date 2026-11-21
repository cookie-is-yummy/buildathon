"""Generate varied synthetic patches against the pinned sqlglot checkout.

EVALUATION ONLY. Output goes to a directory outside the entry repository and is never
shipped or read by the selector.

usage: gen_patches.py <repo> <outdir> <seed> <count-scale>
"""
import ast
import json
import os
import random
import subprocess
import sys

REPO, OUT, SEED, SCALE = sys.argv[1], sys.argv[2], int(sys.argv[3]), float(sys.argv[4])
rng = random.Random(SEED)
os.makedirs(os.path.join(OUT, "commits"), exist_ok=True)


def git(*a):
    return subprocess.run(["git", "-C", REPO, *a], capture_output=True, text=True, check=False).stdout


def reset():
    git("reset", "-q", "--hard", "HEAD")
    git("clean", "-fdq")


def files(prefix, suffix):
    out = []
    for f in git("ls-files", prefix).split("\n"):
        if f.endswith(suffix):
            out.append(f)
    return out


SRC = [f for f in files("sqlglot", ".py")]
TESTS = [f for f in files("tests", ".py")]
FIXTURES = [f for f in git("ls-files", "tests/fixtures").split("\n") if f]


def read(f):
    with open(os.path.join(REPO, f), "rb") as fh:
        return fh.read()


def write(f, data):
    p = os.path.join(REPO, f)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as fh:
        fh.write(data)


def offsets(data):
    offs = [0]
    for line in data.split(b"\n"):
        offs.append(offs[-1] + len(line) + 1)
    return offs


def span(offs, node):
    return offs[node.lineno - 1] + node.col_offset, offs[node.end_lineno - 1] + node.end_col_offset


class Scoper(ast.NodeVisitor):
    """Collect mutation candidates tagged with whether they sit in a function body."""

    def __init__(self, data):
        self.data = data
        self.offs = offsets(data)
        self.depth_fn = 0
        self.cands = []  # (scope, start, end, replacement, desc)
        self.stmts = []  # (scope, node)
        self.funcs = []
        self.classes = []

    def add(self, s, e, rep, desc):
        scope = "fn" if self.depth_fn else "top"
        self.cands.append((scope, s, e, rep, desc))

    def visit_FunctionDef(self, node):
        self.funcs.append((self.depth_fn, node))
        for d in node.decorator_list:
            self.visit(d)
        self.visit(node.args)
        self.depth_fn += 1
        for st in node.body:
            self.stmts.append(("fn", node, st))
            self.visit(st)
        self.depth_fn -= 1

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        self.depth_fn += 1
        self.generic_visit(node)
        self.depth_fn -= 1

    def visit_ClassDef(self, node):
        self.classes.append(node)
        self.generic_visit(node)

    def generic_visit(self, node):
        for field in ("body", "orelse", "finalbody"):
            for st in getattr(node, field, []) if isinstance(getattr(node, field, None), list) else []:
                if isinstance(st, ast.stmt) and self.depth_fn:
                    self.stmts.append(("fn", node, st))
        super().generic_visit(node)

    def visit_Compare(self, node):
        if len(node.ops) == 1:
            s = span(self.offs, node.left)[1]
            e = span(self.offs, node.comparators[0])[0]
            op = self.data[s:e]
            swaps = {
                ast.Eq: b"!=", ast.NotEq: b"==", ast.Lt: b"<=", ast.LtE: b"<", ast.Gt: b">=",
                ast.GtE: b">", ast.Is: b"==", ast.IsNot: b"is", ast.In: b"not in", ast.NotIn: b"in",
            }
            rep = swaps.get(type(node.ops[0]))
            if rep and b"\n" not in op and b"#" not in op:
                self.add(s, e, b" " + rep + b" ", "compare")
        self.generic_visit(node)

    def visit_BoolOp(self, node):
        i = rng.randrange(len(node.values) - 1)
        s = span(self.offs, node.values[i])[1]
        e = span(self.offs, node.values[i + 1])[0]
        txt = self.data[s:e]
        old = b"and" if isinstance(node.op, ast.And) else b"or"
        new = b"or" if isinstance(node.op, ast.And) else b"and"
        if txt.count(old) == 1 and b"#" not in txt and b")" not in txt and b"(" not in txt:
            self.add(s, e, txt.replace(old, new), "boolop")
        self.generic_visit(node)

    def visit_Constant(self, node):
        s, e = span(self.offs, node)
        v = node.value
        if v is True:
            self.add(s, e, b"False", "const_bool")
        elif v is False:
            self.add(s, e, b"True", "const_bool")
        elif isinstance(v, int) and not isinstance(v, bool):
            self.add(s, e, str(v + 1).encode(), "const_int")
        elif isinstance(v, str) and 0 < len(v) < 30 and self.data[s:e][:1] in (b'"', b"'") and b"\n" not in self.data[s:e]:
            q = self.data[s:s + 1]
            if self.data[s:s + 3] not in (b'"""', b"'''"):
                choice = rng.random()
                if choice < 0.5:
                    self.add(s, e, q + self.data[s + 1:e - 1] + b"_X" + q, "const_str")
                elif choice < 0.75:
                    self.add(s, e, q + q, "const_str_empty")
                else:
                    self.add(s, e, q + self.data[s + 1:e - 1].swapcase() + q, "const_str_case")

    def visit_UnaryOp(self, node):
        if isinstance(node.op, ast.Not):
            s, _ = span(self.offs, node)
            o, _ = span(self.offs, node.operand)
            if self.data[s:o].strip() == b"not":
                self.add(s, o, b"", "drop_not")
        self.generic_visit(node)

    def visit_Return(self, node):
        if node.value is not None and not (isinstance(node.value, ast.Constant) and node.value.value is None):
            s, e = span(self.offs, node.value)
            self.add(s, e, b"None", "return_none")
        self.generic_visit(node)

    def visit_BinOp(self, node):
        s = span(self.offs, node.left)[1]
        e = span(self.offs, node.right)[0]
        op = self.data[s:e].strip()
        swaps = {b"+": b"-", b"-": b"+", b"*": b"+", b"|": b"&"}
        if op in swaps:
            self.add(s, e, b" " + swaps[op] + b" ", "binop")
        self.generic_visit(node)


def scope_of(f):
    data = read(f)
    sc = Scoper(data)
    sc.visit(ast.parse(data))
    return data, sc


def pick_src(weighted=None):
    if weighted is None:
        weighted = rng.random() < 0.5
    if weighted:
        sizes = [len(read(f)) for f in SRC]
        return rng.choices(SRC, weights=sizes)[0]
    return rng.choice(SRC)


def apply(data, s, e, rep):
    return data[:s] + rep + data[e:]


def lines_of(data):
    return data.split(b"\n")


# ---------------------------------------------------------------- mutators
# each returns a description or None; it edits the working tree directly


def m_code_fn(pool=None):
    f = rng.choice(pool) if pool else pick_src()
    data, sc = scope_of(f)
    c = [x for x in sc.cands if x[0] == "fn"]
    if not c:
        return None
    _, s, e, rep, desc = rng.choice(c)
    write(f, apply(data, s, e, rep))
    return f"{desc} in function body of {f}"


def m_code_top():
    f = pick_src()
    data, sc = scope_of(f)
    c = [x for x in sc.cands if x[0] == "top"]
    if not c:
        return None
    _, s, e, rep, desc = rng.choice(c)
    write(f, apply(data, s, e, rep))
    return f"{desc} at class/module level of {f}"


def m_drop_dict_entry():
    """Remove one line of a multi-line class-level dict/set/tuple literal."""
    f = pick_src()
    data = read(f)
    tree = ast.parse(data)
    spots = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for st in node.body:
                if isinstance(st, (ast.Assign, ast.AnnAssign)) and st.value is not None:
                    v = st.value
                    if isinstance(v, (ast.Dict, ast.Set, ast.Tuple, ast.List)) and v.end_lineno - v.lineno >= 3:
                        elts = v.keys if isinstance(v, ast.Dict) else v.elts
                        vals = v.values if isinstance(v, ast.Dict) else v.elts
                        for k, val in zip(elts, vals):
                            if k is not None and k.lineno == val.end_lineno and k.lineno not in (v.lineno, v.end_lineno):
                                spots.append(k.lineno)
    if not spots:
        return None
    ln = rng.choice(spots)
    ls = lines_of(data)
    if not ls[ln - 1].rstrip().endswith(b","):
        return None
    del ls[ln - 1]
    write(f, b"\n".join(ls))
    return f"drop literal entry line {ln} at class level of {f}"


COMMENTS = [b"# TODO: revisit", b"# Kept for backwards compatibility", b"# Cheap check first", b"# noqa", b"# type: ignore", b"# see issue 1234"]


def m_comment(pool=None):
    f = rng.choice(pool) if pool else pick_src()
    data = read(f)
    ls = lines_of(data)
    i = rng.randrange(len(ls))
    ref = ls[i] if ls[i].strip() else b""
    indent = ref[: len(ref) - len(ref.lstrip())]
    ls.insert(i, indent + rng.choice(COMMENTS))
    write(f, b"\n".join(ls))
    return f"comment line inserted before line {i + 1} of {f}"


def m_trailing_comment():
    f = pick_src()
    data = read(f)
    ls = lines_of(data)
    idx = [i for i, l in enumerate(ls) if l.strip() and not l.rstrip().endswith(b"\\") and b'"""' not in l]
    i = rng.choice(idx)
    ls[i] = ls[i] + b"  " + rng.choice(COMMENTS)
    write(f, b"\n".join(ls))
    return f"trailing comment appended to line {i + 1} of {f}"


def m_fixture():
    sqls = [x for x in FIXTURES if x.endswith(".sql")]
    f = rng.choice(sqls) if rng.random() < 0.8 else rng.choice(FIXTURES)
    data = read(f)
    ls = lines_of(data)
    idx = [i for i, l in enumerate(ls) if l.strip()]
    if not idx:
        return None
    i = rng.choice(idx)
    line = ls[i]
    r = rng.random()
    if r < 0.4:
        toks = line.split(b" ")
        j = rng.randrange(len(toks))
        toks[j] = toks[j] + b"x" if rng.random() < 0.5 else b"NULL " + toks[j]
        ls[i] = b" ".join(toks)
        d = "token edit"
    elif r < 0.6:
        del ls[i]
        d = "line deleted"
    elif r < 0.8:
        ls.insert(i, b"-- extra note")
        d = "sql comment line inserted"
    else:
        ls.insert(i, b"")
        d = "blank line inserted"
    write(f, b"\n".join(ls))
    return f"{d} at line {i + 1} of fixture {f}"


def m_test_edit():
    r = rng.random()
    if r < 0.7:
        return m_code_fn(pool=[t for t in TESTS if "/test_" in t])
    if r < 0.85:
        return m_code_fn(pool=["tests/helpers.py", "tests/dialects/test_dialect.py"])
    return m_comment(pool=TESTS)


def m_syntax_error():
    f = pick_src()
    data = read(f)
    ls = lines_of(data)
    idx = [i for i, l in enumerate(ls) if l.strip() and not l.strip().startswith(b"#")]
    i = rng.choice(idx)
    r = rng.random()
    if r < 0.4:
        ls[i] = ls[i] + b" )"
    elif r < 0.7:
        ls[i] = b" " + ls[i]
    else:
        ls[i] = ls[i].replace(b":", b"", 1) if b":" in ls[i] else ls[i] + b" ("
    write(f, b"\n".join(ls))
    try:
        ast.parse(b"\n".join(ls))
        ok = "still parses"
    except SyntaxError:
        ok = "does not parse"
    return f"break syntax at line {i + 1} of {f} ({ok})"


def m_delete_file():
    r = rng.random()
    f = pick_src(False) if r < 0.5 else rng.choice(FIXTURES) if r < 0.8 else rng.choice(TESTS)
    os.remove(os.path.join(REPO, f))
    return f"delete {f}"


def m_rename_file():
    r = rng.random()
    f = pick_src(False) if r < 0.6 else rng.choice(FIXTURES)
    base, ext = os.path.splitext(f)
    os.rename(os.path.join(REPO, f), os.path.join(REPO, base + "_renamed" + ext))
    return f"rename {f}"


NEW_FILES = [
    ("sqlglot/dialects/newdialect.py", b"from sqlglot.dialects.dialect import Dialect\n\n\nclass NewDialect(Dialect):\n    pass\n"),
    ("sqlglot/optimizer/extra_rule.py", b"def extra_rule(expression):\n    return expression\n"),
    ("sqlglot/helpers_extra.py", b"VALUE = 1\n"),
    ("tests/test_new_feature.py", b"import unittest\n\n\nclass TestNew(unittest.TestCase):\n    def test_new(self):\n        self.assertTrue(True)\n"),
    ("tests/fixtures/optimizer/new_rule.sql", b"SELECT 1;\nSELECT 1;\n"),
    ("docs/notes.md", b"# notes\n"),
    ("conftest.py", b"import pytest\n"),
    ("tests/conftest.py", b"import pytest\n\n\n@pytest.fixture(autouse=True)\ndef _noop():\n    yield\n"),
    ("json.py", b"VALUE = 1\n"),
    ("sqlglot/py.typed2", b""),
    ("pytest.ini", b"[pytest]\naddopts = -p no:warnings\n"),
    ("tests/fixtures/extra.sql", b"SELECT 1\n"),
    ("sqlglot/typing/newdialect.py", b"EXPRESSION_METADATA = {}\n"),
]


def m_new_file():
    f, content = rng.choice(NEW_FILES)
    write(f, content)
    git("add", "-N", f)
    return f"new file {f}"


CONFIG_FILES = ["pyproject.toml", "setup.cfg", "setup.py", "MANIFEST.in", "sqlglotc/setup.py", "sqlglotc/pyproject.toml",
                "Makefile", "README.md", "CHANGELOG.md", ".github/workflows/python-package.yml", "sqlglot/py.typed",
                ".pre-commit-config.yaml", "AGENTS.md", "posts/ast_primer.md", "benchmarks/parse.py"]


def m_config():
    cands = [f for f in CONFIG_FILES if os.path.exists(os.path.join(REPO, f))]
    f = rng.choice(cands)
    data = read(f)
    ls = lines_of(data)
    if f == "sqlglotc/setup.py" and rng.random() < 0.6:
        data2, sc = scope_of(f)
        if sc.cands:
            _, s, e, rep, desc = rng.choice(sc.cands)
            write(f, apply(data2, s, e, rep))
            return f"{desc} in {f}"
    idx = [i for i, l in enumerate(ls) if l.strip()] or [0]
    i = rng.choice(idx)
    r = rng.random()
    if r < 0.4:
        del ls[i]
        d = "line deleted"
    elif r < 0.7:
        ls[i] = ls[i] + b"x"
        d = "line edited"
    else:
        ls.insert(i, b"# note" if not f.endswith(".md") else b"note")
        d = "line inserted"
    write(f, b"\n".join(ls))
    return f"{d} at line {i + 1} of {f}"


def m_docstring():
    for _ in range(20):
        f = pick_src()
        data = read(f)
        tree = ast.parse(data)
        docs = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.Module)) and node.body:
                st = node.body[0]
                if isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant) and isinstance(st.value.value, str):
                    docs.append(st)
        if docs:
            st = rng.choice(docs)
            ls = lines_of(data)
            i = rng.randrange(st.lineno - 1, st.end_lineno)
            ls[i] = ls[i].replace(b"the", b"that", 1) if b"the" in ls[i] else ls[i].rstrip() + b" (edited)" if not ls[i].rstrip().endswith(b'"""') else ls[i].replace(b'"""', b' edited"""', 1) if ls[i].count(b'"""') == 1 and i != st.lineno - 1 else ls[i]
            write(f, b"\n".join(ls))
            return f"docstring edit at line {i + 1} of {f}"
    return None


def m_signature():
    f = pick_src()
    data, sc = scope_of(f)
    if not sc.funcs:
        return None
    offs = sc.offs
    _, fn = rng.choice(sc.funcs)
    r = rng.random()
    ls = lines_of(data)
    if r < 0.35:
        line = ls[fn.lineno - 1]
        key = b"def " + fn.name.encode()
        if key not in line:
            return None
        ls[fn.lineno - 1] = line.replace(key, key + b"_renamed", 1)
        d = f"rename function {fn.name}"
    elif r < 0.6 and fn.decorator_list:
        d0 = fn.decorator_list[0]
        del ls[d0.lineno - 1: d0.end_lineno]
        d = f"remove decorator of {fn.name}"
    elif fn.args.defaults:
        dflt = rng.choice(fn.args.defaults)
        s, e = span(offs, dflt)
        v = dflt.value if isinstance(dflt, ast.Constant) else "x"
        rep = b"True" if v is False else b"False" if v is True else b"None" if v is not None else b"0"
        write(f, apply(data, s, e, rep))
        return f"change default of {fn.name} in {f}"
    else:
        return None
    write(f, b"\n".join(ls))
    return f"{d} in {f}"


def m_import_change():
    f = pick_src()
    data = read(f)
    tree = ast.parse(data)
    imps = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    if not imps:
        return None
    n = rng.choice(imps)
    ls = lines_of(data)
    r = rng.random()
    if r < 0.5:
        del ls[n.lineno - 1: n.end_lineno]
        d = "remove import"
    else:
        seg = ls[n.lineno - 1: n.end_lineno]
        del ls[n.lineno - 1: n.end_lineno]
        last = imps[-1].end_lineno - (n.end_lineno - n.lineno + 1)
        ls[last:last] = seg
        d = "move import to end of import block"
    write(f, b"\n".join(ls))
    return f"{d} at line {n.lineno} of {f}"


def m_whitespace():
    f = pick_src()
    data = read(f)
    ls = lines_of(data)
    r = rng.random()
    if r < 0.5:
        idx = [i for i, l in enumerate(ls) if not l.strip()]
        if not idx:
            return None
        i = rng.choice(idx)
        ls.insert(i, b"")
        d = "blank line inserted"
    else:
        idx = [i for i, l in enumerate(ls) if b" == " in l or b", " in l]
        if not idx:
            return None
        i = rng.choice(idx)
        ls[i] = ls[i].replace(b" == ", b"  ==  ", 1) if b" == " in ls[i] else ls[i].rstrip() + b"  "
        d = "spacing change"
    write(f, b"\n".join(ls))
    return f"{d} at line {i + 1} of {f}"


def m_delete_stmt():
    f = pick_src()
    data, sc = scope_of(f)
    simple = [st for (_, parent, st) in sc.stmts
              if isinstance(st, (ast.Assign, ast.Expr, ast.AugAssign, ast.Return, ast.Raise, ast.Continue, ast.Break))
              and not (isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant))]
    if not simple:
        return None
    st = rng.choice(simple)
    ls = lines_of(data)
    line = ls[st.lineno - 1]
    indent = line[: len(line) - len(line.lstrip())]
    ls[st.lineno - 1: st.end_lineno] = [indent + b"pass"]
    write(f, b"\n".join(ls))
    return f"statement replaced by pass at line {st.lineno} of {f}"


def m_dedent():
    f = pick_src()
    data, sc = scope_of(f)
    ifs = [st for (_, parent, st) in sc.stmts if isinstance(st, ast.If) and not st.orelse and len(st.body) >= 2
           and st.body[-1].lineno == st.body[-1].end_lineno]
    if not ifs:
        return None
    st = rng.choice(ifs)
    last = st.body[-1]
    ls = lines_of(data)
    line = ls[last.lineno - 1]
    if not line.startswith(b"    "):
        return None
    ls[last.lineno - 1] = line[4:]
    write(f, b"\n".join(ls))
    return f"dedent statement out of if at line {last.lineno} of {f}"


def m_new_local():
    """Add a statement that turns a global name into a local one late in a function."""
    f = pick_src()
    data, sc = scope_of(f)
    fns = [fn for (_, fn) in sc.funcs if fn.body and fn.body[-1].lineno > fn.lineno + 3]
    if not fns:
        return None
    fn = rng.choice(fns)
    names = [n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and n.id in ("exp", "t", "seq_get", "isinstance", "len")]
    if not names:
        return None
    last = fn.body[-1]
    ls = lines_of(data)
    line = ls[last.lineno - 1]
    indent = line[: len(line) - len(line.lstrip())]
    ls.insert(last.lineno - 1, indent + rng.choice(names).encode() + b" = None")
    write(f, b"\n".join(ls))
    return f"shadowing assignment before line {last.lineno} of {f}"


def m_multi():
    parts = []
    for m in rng.sample([m_code_fn, m_fixture, m_comment, m_code_top, m_test_edit, m_docstring], rng.choice([2, 2, 3])):
        d = m()
        if d:
            parts.append(d)
    return " + ".join(parts) if len(parts) >= 2 else None


PLAN = [
    ("code", m_code_fn, 40), ("code_top", m_code_top, 12), ("dict_entry", m_drop_dict_entry, 8),
    ("comment", m_comment, 6), ("comment_trailing", m_trailing_comment, 3), ("fixture", m_fixture, 10),
    ("test_edit", m_test_edit, 10), ("multi", m_multi, 8), ("syntax", m_syntax_error, 5),
    ("delete_file", m_delete_file, 5), ("rename_file", m_rename_file, 3), ("new_file", m_new_file, 6),
    ("config", m_config, 7), ("docstring", m_docstring, 4), ("signature", m_signature, 7),
    ("import", m_import_change, 4), ("whitespace", m_whitespace, 3), ("delete_stmt", m_delete_stmt, 7),
    ("dedent", m_dedent, 4), ("new_local", m_new_local, 3),
]

meta = {}
jobs = []
ONLY = os.environ.get("KINDS")
for kind, fn, n in PLAN:
    if ONLY and kind not in ONLY.split(","):
        continue
    jobs += [(kind, fn)] * max(1, round(n * SCALE))
rng.shuffle(jobs)
alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
for kind, fn in jobs:
    for attempt in range(30):
        reset()
        try:
            desc = fn()
        except Exception as error:  # a mutator that cannot apply here just retries elsewhere
            desc = None
        if not desc:
            continue
        diff = subprocess.run(["git", "-C", REPO, "diff", "--no-color", "--binary"], capture_output=True).stdout
        if diff.strip():
            break
    else:
        continue
    cid = "s%d" % SEED + "".join(rng.choice(alphabet) for _ in range(4))
    with open(os.path.join(OUT, "commits", cid + ".patch"), "wb") as fh:
        fh.write(diff)
    meta[cid] = {"kind": kind, "desc": desc}
reset()
json.dump(meta, open(os.path.join(OUT, "meta.json"), "w"), indent=1)
print(len(meta), "patches")
