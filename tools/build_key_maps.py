"""Build maps/keys/*.json: which tests look at which ENTRY of a class-level dictionary.

Input: the files written by tools/site_attrs/sitecustomize.py while every test ran alone
(one or more recording directories; their union is used).

For a dictionary display assigned in a class body of the pinned sqlglot, and for each
entry written there, the output holds the tests that looked that key up in the dictionary,
or did something with the dictionary that shows every value (values(), items(), a copy,
a comparison, printing it). Asking whether a key is there and walking the keys show no
value and do not count.

A dictionary is left out (the selector then treats a change as before) when
  - a value of it was looked at while some class was being created (a metaclass or an
    `__init_subclass__` hook computing other attributes from it); single entries looked
    at that way are left out on their own;
  - the recorder could not watch it during class creation;
  - another statement of the same class body uses its name in any way other than walking
    its keys (`for key in NAME` in a comprehension): the body works on the plain
    dictionary, where look-ups cannot be seen.

usage: build_key_maps.py <pinned sqlglot checkout> <tests.json> <entry repo> <recording dir> [<recording dir> ...]
"""
import ast
import glob
import json
import os
import shutil
import sys

REPO, TESTS, ENTRY = sys.argv[1:4]
RECS = sys.argv[4:]
OUT = os.path.join(ENTRY, "maps", "keys")
ids = [t["id"] for t in json.load(open(TESTS))["tests"]]

sys.path.insert(0, ENTRY)
from selector import _assigned_name, _names_read_at_class_time  # the same static rules

keyed = {}       # attribute key -> {"line:col": key name}
reads = {}       # (attribute key, key name) -> bits
creation = {}    # attribute key -> key names looked at during class creation
prewrapped, late = set(), set()
for rec in RECS:
    for number in range(len(ids)):
        rc_file = os.path.join(rec, f"{number}.rc")
        if not os.path.exists(rc_file) or open(rc_file).read().strip() != "0":
            raise SystemExit(f"test {number} {ids[number]} has no passing recording in {rec}")
        parts = glob.glob(os.path.join(rec, f"{number}.*.json"))
        if not parts:
            raise SystemExit(f"test {number} has no recording files in {rec}")
        bit = 1 << number
        for part in parts:
            data = json.load(open(part))
            prewrapped.update(data["prewrapped"])
            late.update(data.get("late", ()))
            for key, entries in data["keyed"].items():
                known = keyed.setdefault(key, {})
                for position, name in entries.items():
                    if known.setdefault(position, name) != name:
                        raise SystemExit(f"{key} {position}: two names for one entry")
            for key, names in data["keyreads"].items():
                for name in names:
                    reads[(key, name)] = reads.get((key, name), 0) | bit
            for key, names in data["keycreation"].items():
                creation.setdefault(key, set()).update(names)


def class_node(path, qualname):
    node = ast.parse(open(os.path.join(REPO, path), encoding="utf-8").read())
    for part in qualname.split("."):
        node = next((n for n in node.body if isinstance(n, ast.ClassDef) and n.name == part), None)
        if node is None:
            return None
    return node


def only_walks_keys(statement, name):
    """Every use of `name` while the class body runs is `for ... in name` of a comprehension."""
    walked = {id(g.iter) for n in ast.walk(statement) if isinstance(n, (ast.GeneratorExp, ast.ListComp, ast.SetComp, ast.DictComp))
              for g in n.generators}
    stack = [statement]
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            arguments = node.args
            stack.extend(arguments.defaults + [d for d in arguments.kw_defaults if d is not None])
            stack.extend(getattr(node, "decorator_list", []))
            continue
        if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load) and id(node) not in walked:
            return False
        stack.extend(ast.iter_child_nodes(node))
    return True


by_file = {}
stats = {"dictionaries": 0, "entries": 0, "left out: looked at during class creation": 0,
         "left out: not watched during class creation": 0, "left out: used in the class body": 0,
         "entries left out: looked at during class creation": 0}
for key in sorted(keyed):
    path, qualname, name = key.split("::")
    if not keyed[key]:
        continue
    if "*" in creation.get(key, ()):
        stats["left out: looked at during class creation"] += 1
        continue
    if path + "::" + qualname not in prewrapped or key in late:
        stats["left out: not watched during class creation"] += 1
        continue
    node = class_node(path, qualname)
    if node is None:
        continue
    assignments = [st for st in node.body if _assigned_name(st) == name]
    used = False
    for statement in node.body:
        if statement in assignments:
            continue
        if isinstance(statement, ast.ClassDef):
            if any(name in _names_read_at_class_time(d) for d in statement.decorator_list + statement.bases):
                used = True
        elif name in _names_read_at_class_time(statement) and not only_walks_keys(statement, name):
            used = True
    if used or len(assignments) != 1:
        stats["left out: used in the class body"] += 1
        continue
    everything = reads.get((key, "*"), 0)
    entries = {}
    for position, key_name in keyed[key].items():
        if key_name in creation.get(key, ()):
            stats["entries left out: looked at during class creation"] += 1
            continue
        entries[position] = reads.get((key, key_name), 0) | everything
    if entries:
        by_file.setdefault(path, {}).setdefault(qualname, {})[name] = entries
        stats["dictionaries"] += 1
        stats["entries"] += len(entries)

if os.path.isdir(OUT):
    shutil.rmtree(OUT)
os.makedirs(OUT)
total = 0
for path, classes in sorted(by_file.items()):
    sets, index = [], {}
    tables = {}
    for qualname, names in classes.items():
        for name, entries in names.items():
            tables.setdefault(qualname, {})[name] = {
                position: index.setdefault(bits, len(index)) for position, bits in entries.items()}
    sets = ["%x" % bits for bits, _ in sorted(index.items(), key=lambda kv: kv[1])]
    out = os.path.join(OUT, path.replace("/", "__") + ".json")
    json.dump({"sets": sets, "tables": tables}, open(out, "w"), separators=(",", ":"))
    total += os.path.getsize(out)
print("; ".join("%s: %d" % kv for kv in stats.items()), "; %d files, %d KB" % (len(by_file), total // 1024))
