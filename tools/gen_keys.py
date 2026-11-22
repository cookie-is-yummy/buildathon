"""Patches for checking the per-key map of class-level dictionaries (EVALUATION ONLY).

For entries the map covers, the entry's value in the pinned source is replaced by
something a test cannot use without noticing: a function that raises when called (where
the value was a call or a lambda), `None` (a name), another string or number (a
constant), a tuple with its first constant changed. The tests the map leaves out for that
entry (everyone who runs the assignment, minus the entry's readers) are written next to
the patch; tools/swap_gt.py runs exactly those on the patched code. Every one must pass.

usage: gen_keys.py <pinned checkout> <tests.json> <output dir> <how many> [seed]

KEYS_NAME=<attribute> limits the patches to dictionaries assigned to that attribute
(for example arg_types). KEYS_AGAINST=attribute writes down, instead of everyone who runs
the assignment, only the tests the rule for whole attributes would select minus the
entry's readers: far fewer tests per patch, so many more patches can be checked.
"""
import ast
import difflib
import json
import os
import random
import sys
import warnings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import selector as S

warnings.simplefilter("ignore")
REPO, TESTS, OUT, COUNT = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
rng = random.Random(int(sys.argv[5]) if len(sys.argv) > 5 else 1)
tests = json.load(open(TESTS))["tests"]
ids = [t["id"] for t in tests]
seconds = {t["number"]: t["seconds"] for t in tests}
maps = S.Maps(ids)
RAISES = '(lambda *a, **k: (_ for _ in ()).throw(BaseException("poisoned entry used")))'


def poison(node, text):
    if isinstance(node, (ast.Call, ast.Lambda)):
        return RAISES
    if isinstance(node, (ast.Name, ast.Attribute)):
        return "None"
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            return repr("\x01poisoned " + node.value)
        if isinstance(node.value, bool):
            return repr(not node.value)
        if isinstance(node.value, (int, float)):
            return repr(node.value + 977)
        return None
    if isinstance(node, (ast.Tuple, ast.List)) and node.elts:
        for position, item in enumerate(node.elts):
            new = poison(item, text) if isinstance(item, ast.Constant) else None
            if new is not None:
                parts = [ast.get_source_segment(text, e) for e in node.elts]
                parts[position] = new
                inner = ", ".join(parts) + ("," if len(parts) == 1 and isinstance(node, ast.Tuple) else "")
                return ("(%s)" if isinstance(node, ast.Tuple) else "[%s]") % inner
    return None


def offset(lines, line, column):
    start = sum(len(t) + 1 for t in lines[:line - 1])
    return start + len(lines[line - 1].encode("utf-8")[:column].decode("utf-8"))


candidates = []
directory = os.path.join(S.MAPS, "keys")
for file_name in sorted(os.listdir(directory)):
    path = file_name[:-5].replace("__", "/")
    table = json.load(open(os.path.join(directory, file_name)))
    for qualname, names in table["tables"].items():
        for name, entries in names.items():
            if os.environ.get("KEYS_NAME") and name != os.environ["KEYS_NAME"]:
                continue
            for position in entries:
                candidates.append((path, qualname, name, position))
print(len(candidates), "entries in the map")
rng.shuffle(candidates)
for sub in ("commits", "files"):
    os.makedirs(os.path.join(OUT, sub), exist_ok=True)
meta = {}
trees = {}
skipped = same = 0
for path, qualname, name, position in candidates:
    if len(meta) >= COUNT:
        break
    if path not in trees:
        text = open(os.path.join(REPO, path), encoding="utf-8").read()
        trees[path] = (text, ast.parse(text))
    text, tree = trees[path]
    node = tree
    for part in qualname.split("."):
        node = next(n for n in node.body if isinstance(n, ast.ClassDef) and n.name == part)
    statement = [st for st in node.body if S._assigned_name(st) == name][-1]
    line, column = (int(x) for x in position.split(":"))
    value = next(v for k, v in zip(statement.value.keys, statement.value.values)
                 if k is not None and (k.lineno, k.col_offset) == (line, column))
    new = poison(value, text)
    if new is None:
        skipped += 1
        continue
    lines = text.split("\n")
    start, end = offset(lines, value.lineno, value.col_offset), offset(lines, value.end_lineno, value.end_col_offset)
    patched = text[:start] + new + text[end:]
    try:
        ast.parse(patched)
    except SyntaxError:
        skipped += 1
        continue
    everyone = maps.covering(path, statement.lineno, statement.end_lineno)
    if os.environ.get("KEYS_AGAINST") == "attribute":
        whole = maps.attribute_readers(path, qualname, name)
        if whole is not None:
            everyone = whole[0]
    readers = maps.key_readers(path, qualname, name, [(line, column)])
    dropped = maps.numbers(everyone & ~readers)
    if not dropped:
        same += 1
        continue
    cid = "k%04d" % len(meta)
    diff = "".join(difflib.unified_diff(text.splitlines(keepends=True), patched.splitlines(keepends=True), "a/" + path, "b/" + path, n=3))
    open(os.path.join(OUT, "commits", cid + ".patch"), "w").write("diff --git a/%s b/%s\n%s" % (path, path, diff))
    open(os.path.join(OUT, "files", cid + ".py"), "w").write(patched)
    meta[cid] = {"path": path, "line": line, "kind": "%s %s" % (name, type(value).__name__), "class": qualname,
                 "readers": len(maps.numbers(readers)), "dropped": [ids[n] for n in dropped],
                 "seconds": round(sum(seconds[n] for n in dropped), 2)}
json.dump(meta, open(os.path.join(OUT, "meta.json"), "w"), indent=1)
print("%d patches written; %d entries with a value we cannot poison, %d where nothing is left out; %.0f test seconds to run" % (
    len(meta), skipped, same, sum(m["seconds"] for m in meta.values())))
