"""Add the argument tables of expressions to maps/keys/*.json.

build_key_maps.py leaves out every dictionary whose values are looked at while a class
is being created, and `arg_types` is one: `Expr.__init_subclass__` turns it into
`required_args`. This script follows that one step further, with a recording of its own
(tools/site_args, tools/record_args.sh; read the top of site_args/sitecustomize.py first).

For the entry of argument k in the table written in the body of class C, the map gets
the tests that can notice a change of the entry's value:

  - tests during which the loop of `Expr.error_messages` met an expression using that
    table with argument k missing (None or an empty list);
  - tests during which that loop found two or more required arguments missing at once
    for such an expression (the order of the messages may change);
  - tests during which `required_args` of a class using the table was read anywhere
    else;
  - tests that looked up the entry's value in the table itself, or did something that
    shows every value, outside class creation (from the recordings of
    tools/site_attrs, the same ones build_key_maps.py uses).

A table is left out (a change to it is then treated as before) when
  - a class that does not use the table as its own read it while being created (its
    values were copied somewhere we do not follow);
  - the attribute recorder could not watch it during class creation;
  - another statement of the class body uses the name `arg_types`;
  - a key is not a plain string.

Before anything is written the script checks that the pinned source still says what
the argument rests on: the one line of `__init_subclass__`, the three lines of the loop,
and that `required_args` is mentioned nowhere else under sqlglot/.

Run it after build_key_maps.py (which rewrites maps/keys from scratch).

usage: build_arg_maps.py <pinned sqlglot checkout> <tests.json> <entry repo> <args recording dir>
                         <attribute recording dir> [<attribute recording dir> ...]
"""
import ast
import glob
import json
import os
import sys

REPO, TESTS, ENTRY, ARGS = sys.argv[1:5]
ATTRS = sys.argv[5:]
OUT = os.path.join(ENTRY, "maps", "keys")
NAME = "arg_types"
ids = [t["id"] for t in json.load(open(TESTS))["tests"]]

sys.path.insert(0, ENTRY)
from selector import _assigned_name, _names_read_at_class_time  # the same static rules

# ------------------------------------------------------------------ the source we rely on
CORE = "sqlglot/expressions/core.py"
core_text = open(os.path.join(REPO, CORE), encoding="utf-8").read()
HOOK = "        cls.required_args = {k for k, v in cls.arg_types.items() if v}\n"
LOOP = ("        for k in self.required_args:\n"
        "            v = self.args.get(k)\n"
        "            if v is None or (isinstance(v, list) and not v):\n")
if core_text.count(HOOK) != 1 or core_text.count(LOOP) != 1:
    raise SystemExit("the pinned %s does not compute or use required_args the way this map assumes" % CORE)
core_tree = ast.parse(core_text)
expr = next(n for n in core_tree.body if isinstance(n, ast.ClassDef) and n.name == "Expr")
hook = next(n for n in expr.body if isinstance(n, ast.FunctionDef) and n.name == "__init_subclass__")
# `Expr.error_messages` only raises NotImplementedError; the loop is in `Expression`'s
checks = [n for c in core_tree.body if isinstance(c, ast.ClassDef) for n in c.body
          if isinstance(n, ast.FunctionDef) and n.name == "error_messages"]
check = next(n for n in checks if n.lineno <= core_text[:core_text.index(LOOP)].count("\n") + 1 <= n.end_lineno)
hook_line = core_text[:core_text.index(HOOK)].count("\n") + 1
loop_line = core_text[:core_text.index(LOOP)].count("\n") + 1
if not (hook.lineno <= hook_line <= hook.end_lineno and check.lineno <= loop_line <= check.end_lineno):
    raise SystemExit("the two pieces of source are not where they are expected")
mentions = []
for root, _, names in os.walk(os.path.join(REPO, "sqlglot")):
    for name in names:
        if name.endswith(".py"):
            path = os.path.join(root, name)
            text = open(path, encoding="utf-8").read()
            if "required_args" in text:
                for number, line in enumerate(text.split("\n"), 1):
                    if "required_args" in line:
                        mentions.append((os.path.relpath(path, REPO), number))
declared = [n.lineno for n in expr.body if isinstance(n, ast.AnnAssign) and getattr(n.target, "id", None) == "required_args"]
if len(declared) != 1 or sorted(mentions) != sorted([(CORE, declared[0]), (CORE, hook_line), (CORE, loop_line)]):
    raise SystemExit("required_args is mentioned somewhere unexpected: %s" % mentions)
init_hooks = []
for root, _, names in os.walk(os.path.join(REPO, "sqlglot", "expressions")):
    for name in names:
        if name.endswith(".py"):
            text = open(os.path.join(root, name), encoding="utf-8").read()
            if "__init_subclass__" in text or "metaclass" in text:
                init_hooks.append(os.path.relpath(os.path.join(root, name), REPO))
if init_hooks != [CORE] or core_text.count("def __init_subclass__") != 1 or "metaclass" in core_text:
    raise SystemExit("another class-creation hook among the expressions: %s" % init_hooks)

# ------------------------------------------------------------------ the two recordings
missing, several, elsewhere = {}, {}, {}  # table class -> {argument: bits} / bits / bits
table_of = {}                             # expression class -> table class
for number in range(len(ids)):
    rc_file = os.path.join(ARGS, "%d.rc" % number)
    parts = glob.glob(os.path.join(ARGS, "%d.*.json" % number))
    if not os.path.exists(rc_file) or open(rc_file).read().strip() != "0" or not parts:
        raise SystemExit("test %d %s has no passing recording in %s" % (number, ids[number], ARGS))
    bit = 1 << number
    for part in parts:
        data = json.load(open(part))
        if data["unwatched"]:
            raise SystemExit("classes not watched in %s: %s" % (part, data["unwatched"][:5]))
        for cls, table in data["tables"].items():
            if table_of.setdefault(cls, table) != table:
                raise SystemExit("%s uses two different tables" % cls)
        for table, arguments in data["missing"].items():
            seen = missing.setdefault(table, {})
            for argument in arguments:
                seen[argument] = seen.get(argument, 0) | bit
        for table in data["several"]:
            several[table] = several.get(table, 0) | bit
        for table in data["elsewhere"]:
            elsewhere[table] = elsewhere.get(table, 0) | bit

keyed, reads = {}, {}
creators = {}  # attribute key -> classes during whose creation it was read
prewrapped, late = set(), set()
for rec in ATTRS:
    for number in range(len(ids)):
        rc_file = os.path.join(rec, "%d.rc" % number)
        parts = glob.glob(os.path.join(rec, "%d.*.json" % number))
        if not os.path.exists(rc_file) or open(rc_file).read().strip() != "0" or not parts:
            raise SystemExit("test %d %s has no passing recording in %s" % (number, ids[number], rec))
        bit = 1 << number
        for part in parts:
            data = json.load(open(part))
            prewrapped.update(data["prewrapped"])
            late.update(data.get("late", ()))
            for key, entries in data["keyed"].items():
                if key.endswith("::" + NAME):
                    known = keyed.setdefault(key, {})
                    for position, name in entries.items():
                        if known.setdefault(position, name) != name:
                            raise SystemExit("%s %s: two names for one entry" % (key, position))
            for key, names in data["keyreads"].items():
                if key.endswith("::" + NAME):
                    for name in names:
                        reads[(key, name)] = reads.get((key, name), 0) | bit
            for cls, seen in data["creation"].items():
                for key, _ in seen:
                    if key.endswith("::" + NAME):
                        creators.setdefault(key, set()).add(cls)


def class_node(path, qualname):
    node = ast.parse(open(os.path.join(REPO, path), encoding="utf-8").read())
    for part in qualname.split("."):
        node = next((n for n in node.body if isinstance(n, ast.ClassDef) and n.name == part), None)
        if node is None:
            return None
    return node


stats = {"tables": 0, "entries": 0, "entries no test can notice": 0,
         "left out: read while another class was created": 0, "left out: not watched during class creation": 0,
         "left out: used in the class body": 0, "left out: a key that is not a string": 0,
         "left out: not an expression class": 0}
by_file = {}
for key in sorted(keyed):
    path, qualname, _ = key.split("::")
    owner = path + "::" + qualname
    if not keyed[key]:
        continue
    if table_of.get(owner) != owner:
        stats["left out: not an expression class"] += 1
        continue
    if any(table_of.get(cls) != owner for cls in creators.get(key, ())):
        stats["left out: read while another class was created"] += 1
        continue
    if owner not in prewrapped or key in late:
        stats["left out: not watched during class creation"] += 1
        continue
    node = class_node(path, qualname)
    if node is None:
        continue
    assignments = [st for st in node.body if _assigned_name(st) == NAME]
    used = len(assignments) != 1 or not isinstance(getattr(assignments[0], "value", None), ast.Dict)
    for statement in node.body:
        if statement in assignments:
            continue
        if isinstance(statement, ast.ClassDef):
            if any(NAME in _names_read_at_class_time(d) for d in statement.decorator_list + statement.bases):
                used = True
        elif NAME in _names_read_at_class_time(statement):
            used = True
    if used:
        stats["left out: used in the class body"] += 1
        continue
    if any(not name.startswith("s:") for name in keyed[key].values()):
        stats["left out: a key that is not a string"] += 1
        continue
    always = several.get(owner, 0) | elsewhere.get(owner, 0) | reads.get((key, "*"), 0)
    entries = {}
    for position, name in keyed[key].items():
        entries[position] = missing.get(owner, {}).get(name[2:], 0) | reads.get((key, name), 0) | always
        stats["entries no test can notice"] += not entries[position]
    by_file.setdefault(path, {}).setdefault(qualname, {})[NAME] = entries
    stats["tables"] += 1
    stats["entries"] += len(entries)

total = 0
for path, classes in sorted(by_file.items()):
    out = os.path.join(OUT, path.replace("/", "__") + ".json")
    table = json.load(open(out)) if os.path.exists(out) else {"sets": [], "tables": {}}
    for names in table["tables"].values():
        names.pop(NAME, None)
    # renumber the sets: the ones still used, then the new ones
    index = {}
    tables = {}
    for qualname, names in table["tables"].items():
        for name, entries in names.items():
            tables.setdefault(qualname, {})[name] = {
                position: index.setdefault(int(table["sets"][i], 16), len(index)) for position, i in entries.items()}
    for qualname, names in classes.items():
        for name, entries in names.items():
            tables.setdefault(qualname, {})[name] = {
                position: index.setdefault(bits, len(index)) for position, bits in entries.items()}
    sets = ["%x" % bits for bits, _ in sorted(index.items(), key=lambda kv: kv[1])]
    json.dump({"sets": sets, "tables": tables}, open(out, "w"), separators=(",", ":"))
    total += os.path.getsize(out)
print("; ".join("%s: %d" % kv for kv in stats.items()), "; %d files, %d KB" % (len(by_file), total // 1024))
print("tables with two or more required arguments missing at once: %d; read elsewhere: %d" % (len(several), len(elsewhere)))
