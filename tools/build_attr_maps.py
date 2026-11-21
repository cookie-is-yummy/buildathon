"""Build maps/attrs/*.json: which tests can see a change to which class attribute.

Input: the files written by tools/site_attrs/sitecustomize.py while every test ran alone
(<number>.<pid>.json, <number>.rc).

For every attribute assigned in a class body of the pinned sqlglot ("file::Class::ATTR")
the output holds ONE set of tests: those that read the attribute itself, or anything
computed from it:
  - attributes that `__init_subclass__` or a metaclass computed while a class was being
    created, after reading it (e.g. `required_args` from `arg_types`);
  - for the few attributes a sqlglot metaclass may fetch by name from the class dictionary
    (the recorder cannot watch those during creation): every attribute the metaclass
    computed for that class;
  - attributes assigned later in the same class body by a statement that reads its name
    (e.g. `B = {**A, ...}`).
An attribute whose value is used during class creation in a way we cannot follow is left
out; the selector then falls back to every test that imports the module.

usage: build_attr_maps.py <pinned sqlglot checkout> <tests.json> <entry repo> <recording dir>
"""
import ast
import glob
import json
import os
import shutil
import sys

REPO, TESTS, ENTRY, REC = sys.argv[1:5]
OUT = os.path.join(ENTRY, "maps", "attrs")
ids = [t["id"] for t in json.load(open(TESTS))["tests"]]

reads = {}  # key -> bits
wrapped, prewrapped, late = set(), set(), set()
computed = {}  # class id -> names set during creation
creation = {}  # class id -> {(key read, frozenset(names computed earlier))}
for number in range(len(ids)):
    rc_file = os.path.join(REC, f"{number}.rc")
    if not os.path.exists(rc_file) or open(rc_file).read().strip() != "0":
        if os.environ.get("ALLOW_PARTIAL"):
            continue
        raise SystemExit(f"test {number} {ids[number]} has no passing attribute recording")
    parts = glob.glob(os.path.join(REC, f"{number}.*.json"))
    if not parts:
        raise SystemExit(f"test {number} has no attribute recording files")
    bit = 1 << number
    for part in parts:
        rec = json.load(open(part))
        wrapped.update(rec["wrapped"])
        prewrapped.update(rec["prewrapped"])
        late.update(rec.get("late", ()))
        for key in rec["reads"]:
            reads[key] = reads.get(key, 0) | bit
        for class_id, names in rec["computed"].items():
            computed.setdefault(class_id, set()).update(names)
        for class_id, events in rec["creation"].items():
            table = creation.setdefault(class_id, set())
            for key, earlier in events:
                table.add((key, frozenset(earlier)))

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from selector import _assigned_name, _names_read_at_class_time  # the same static rules

class_bodies = {}  # class id -> ClassDef


def index_classes(path):
    def visit(body, prefix):
        for node in body:
            if isinstance(node, ast.ClassDef):
                class_bodies[path + "::" + prefix + node.name] = node
                visit(node.body, prefix + node.name + ".")
            elif isinstance(node, (ast.If, ast.Try)):
                for field in ("body", "orelse", "finalbody"):
                    visit(getattr(node, field, []), prefix)

    visit(ast.parse(open(os.path.join(REPO, path)).read()).body, "")


for path in sorted({key.split("::")[0] for key in wrapped}):
    index_classes(path)

# what each computed attribute depends on
depends = {}  # key that was read -> set of computed keys derived from it
for class_id, names in computed.items():
    events = creation.get(class_id, ())
    for name in names:
        target = class_id + "::" + name
        for key, earlier in events:
            if name not in earlier:
                depends.setdefault(key, set()).add(target)
    if class_id in class_bodies:
        # Body attributes the recorder could not watch while the class was being created
        # (the metaclass may fetch them by name from the class dictionary): assume every
        # computed attribute of the class depends on them.
        for statement in class_bodies[class_id].body:
            own = _assigned_name(statement)
            if own and (class_id not in prewrapped or class_id + "::" + own in late):
                depends.setdefault(class_id + "::" + own, set()).update(class_id + "::" + n for n in names)

hook_reads = {key for events in creation.values() for key, _ in events}
by_file = {}
untracked = 0
for class_id, node in class_bodies.items():
    path, qual = class_id.split("::")
    for statement in node.body:
        name = _assigned_name(statement)
        if not name or class_id + "::" + name not in wrapped:
            continue
        closure, pending, ok = set(), [class_id + "::" + name], True
        while pending and ok:
            key = pending.pop()
            if key in closure:
                continue
            closure.add(key)
            pending.extend(depends.get(key, ()))
            owner, _, attr = key.rpartition("::")
            body = class_bodies.get(owner)
            if body is None:
                continue
            for other in body.body:
                if isinstance(other, ast.ClassDef):
                    uses = any(attr in _names_read_at_class_time(d) for d in other.decorator_list + other.bases)
                else:
                    uses = attr in _names_read_at_class_time(other)
                if not uses:
                    continue
                target = _assigned_name(other)
                if target is None or owner + "::" + target not in wrapped:
                    ok = False  # read at class-creation time by something we cannot follow
                    break
                pending.append(owner + "::" + target)
        if not ok:
            untracked += 1
            continue
        bits = 0
        for key in closure:
            bits |= reads.get(key, 0)
        # "!" marks an attribute that class-creation hooks read (or may read: the body of a
        # class with a sqlglot metaclass is invisible to the recorder while it is created)
        hooked = class_id not in prewrapped or any(key in hook_reads or key in late for key in closure)
        by_file.setdefault(path, {}).setdefault(qual, {})[name] = ("!" if hooked else "") + "%x" % bits

if os.path.isdir(OUT):
    shutil.rmtree(OUT)
os.makedirs(OUT)
total = count = 0
for path, classes in sorted(by_file.items()):
    out = os.path.join(OUT, path.replace("/", "__") + ".json")
    json.dump(classes, open(out, "w"), separators=(",", ":"))
    total += os.path.getsize(out)
    count += sum(len(v) for v in classes.values())
print(f"{count} class attributes mapped in {len(by_file)} files ({untracked} left untracked), {total // 1024} KB")
