"""Turn the per-test recordings into the compact maps the selector ships with.

Input:  the files written by tools/site/sitecustomize.py while every test ran alone, in a
        fresh process tree, at the pinned sqlglot commit: <number>.<pid>.json (one per
        Python process the test started) and <number>.rc (pytest's exit status).
Output: maps/tests.json   test ids, in recording order (index = bit position in every set)
        maps/files.json   which tests import, open or list which files and directories
        maps/lines/*.json per Python file: executed line -> tests, started code -> tests
        pinned/           the pinned text of every Python file (needed to rebuild a patched
                          file and compare syntax trees)

Nothing here knows about any patch. The maps describe the pinned commit only.

A whole-suite recording (tools/site_seq, one process, one coverage context per test) can be
merged in: run alone, a test fills every cache itself; in a whole-suite run it may take a
cache-hit path instead. The maps hold the union.

usage: build_maps.py <pinned checkout> <tests.json> <entry repo> <recording dir> [<whole-suite .json>...]
"""
import ast
import glob
import json
import os
import shutil
import subprocess
import sys

REPO, TESTS, ENTRY, REC = sys.argv[1:5]
OUT = os.path.join(ENTRY, "maps")
PINNED = os.path.join(ENTRY, "pinned")

tests = json.load(open(TESTS))["tests"]
ids = [t["id"] for t in tests]

lines, calls = {}, {}  # file -> line -> bitset
KINDS = ("imports", "data", "infra_opens", "lists", "implists", "infra_lists", "failed_imports")
tables = {k: {} for k in KINDS}

for number, test_id in enumerate(ids):
    rc_file = os.path.join(REC, f"{number}.rc")
    if not os.path.exists(rc_file) or open(rc_file).read().strip() != "0":
        raise SystemExit(f"test {number} {test_id} has no passing recording")
    parts = glob.glob(os.path.join(REC, f"{number}.*.json"))
    if not parts:
        raise SystemExit(f"test {number} {test_id} has no recording files")
    bit = 1 << number
    for part in parts:
        rec = json.load(open(part))
        for source, target in ((rec["lines"], lines), (rec["calls"], calls)):
            for f, ls in source.items():
                table = target.setdefault(f, {})
                for ln in ls:
                    table[ln] = table.get(ln, 0) | bit
        for kind in KINDS:
            table = tables[kind]
            for key in rec.get(kind, []):
                table[key] = table.get(key, 0) | bit

for suite_file in sys.argv[5:]:
    rec = json.load(open(suite_file))
    if rec["exit"] != 0:
        # In reversed order a few tests fail at the pinned commit (they depend on state left
        # by earlier tests). Their executed lines are still lines those tests can execute,
        # so the recording is still merged: it can only widen a selection.
        print(f"{suite_file}: note, this whole-suite run had failures (exit {rec['exit']})")
    added = 0
    for source, target in ((rec["lines"], lines), (rec["calls"], calls)):
        for f, table in source.items():
            mine = target.setdefault(f, {})
            for ln, bits in table.items():
                ln, bits = int(ln), int(bits, 16)
                before = mine.get(ln, 0)
                if bits & ~before:
                    added += 1
                mine[ln] = before | bits
    print(f"{suite_file}: widened {added} line or call entries")

tracked = [f for f in subprocess.run(["git", "-C", REPO, "ls-files"], capture_output=True, text=True).stdout.split("\n") if f]

if os.path.isdir(OUT):
    shutil.rmtree(OUT)
os.makedirs(os.path.join(OUT, "lines"))


def enc(bits):
    return "%x" % bits


# Modules that every test tries and fails to import for reasons unrelated to the repository
# (Windows-only modules and the like) are kept too: the selector only asks about names that
# a new file in the repository could provide.
# Fact: does any code that the tests execute consume a docstring? The selector treats a
# changed class or module docstring as harmless only if nothing does. The one consumer at
# the pinned commit copies a class's docstring back onto itself (replacing None with ""),
# which a non-empty docstring passes through unchanged.
BENIGN_DOC_USES = {'setattr(cls, "__doc__", getattr(cls, "__doc__", None) or "")'}
doc_sites = []
for f in tracked:
    if not f.endswith(".py"):
        continue
    source = open(os.path.join(REPO, f), encoding="utf-8").read()
    if "__doc__" not in source and "getdoc" not in source and "doctest" not in source:
        continue
    text_lines = source.split("\n")
    for node in ast.walk(ast.parse(source)):
        hit = (
            (isinstance(node, ast.Attribute) and node.attr in ("__doc__", "getdoc"))
            or (isinstance(node, ast.Name) and node.id in ("__doc__", "getdoc", "doctest"))
            or (isinstance(node, ast.Constant) and node.value == "__doc__")
            or (isinstance(node, (ast.Import, ast.ImportFrom)) and "doctest" in ast.dump(node))
        )
        if hit and lines.get(f, {}).get(node.lineno):  # only code some test executes
            doc_sites.append((f, node.lineno, text_lines[node.lineno - 1].strip()))
docstrings_unused = all(text in BENIGN_DOC_USES for _, _, text in doc_sites)
print("docstring consumers executed by tests:", sorted(set(doc_sites)), "-> docstrings_unused =", docstrings_unused)

json.dump(ids, open(os.path.join(OUT, "tests.json"), "w"), indent=0)
files = {"tracked": tracked, "facts": {"docstrings_unused": docstrings_unused}}
for kind in KINDS:
    files[kind] = {k: enc(b) for k, b in sorted(tables[kind].items())}
json.dump(files, open(os.path.join(OUT, "files.json"), "w"), separators=(",", ":"))

# Line ranges of top-level statements (and, for top-level classes, of the statements in the
# class body), so the selector can parse just the statement a patch touches.
index = {}
for f in tracked:
    if not f.endswith(".py"):
        continue
    try:
        tree = ast.parse(open(os.path.join(REPO, f), encoding="utf-8").read())
    except SyntaxError:
        continue

    def span(node):
        first = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
        return [first, node.end_lineno]

    entries = []
    for node in tree.body:
        children = [span(child) for child in node.body] if isinstance(node, ast.ClassDef) else None
        entries.append(span(node) + [node.name if children is not None else None, children])
    index[f] = entries
json.dump(index, open(os.path.join(OUT, "index.json"), "w"), separators=(",", ":"))

total = 0
for f in sorted(set(lines) | set(calls)):
    if not f.endswith(".py"):
        continue
    sets, set_index = [], {}

    def ref(bits):
        j = set_index.get(bits)
        if j is None:
            j = set_index[bits] = len(sets)
            sets.append(enc(bits))
        return j

    runs = []
    prev_line = prev_set = start = None
    table = lines.get(f, {})
    for ln in sorted(table):
        j = ref(table[ln])
        if prev_set == j and ln == prev_line + 1:
            prev_line = ln
            continue
        if prev_set is not None:
            runs.append([start, prev_line, prev_set])
        start, prev_line, prev_set = ln, ln, j
    if prev_set is not None:
        runs.append([start, prev_line, prev_set])
    started = [[ln, ref(bits)] for ln, bits in sorted(calls.get(f, {}).items())]
    out = os.path.join(OUT, "lines", f.replace("/", "__") + ".json")
    json.dump({"sets": sets, "runs": runs, "calls": started}, open(out, "w"), separators=(",", ":"))
    total += os.path.getsize(out)

if os.path.isdir(PINNED):
    shutil.rmtree(PINNED)
for f in tracked:
    if f.endswith(".py") or f in ("pyproject.toml", "setup.cfg"):
        dst = os.path.join(PINNED, f)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(os.path.join(REPO, f), dst)
shutil.copyfile(os.path.join(REPO, "LICENSE"), os.path.join(PINNED, "LICENSE"))
print(f"{len(ids)} tests, {len(lines)} files with line data, {total // 1024} KB of line maps")
