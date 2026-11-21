"""Build maps/cmp/*.json and maps/bool/*.json from the recordings made by
tools/site_cmp/sitecustomize.py (tools/record_cmp.sh): what every test observed, at the
pinned commit, each time it evaluated a comparison or an and/or expression.

Input per recording directory: <number>.<pid>.json and <number>.rc for every test run
alone, and forward.json / reverse.json for the whole suite in one process. Several
recording directories (independent runs, each with its own random hash seeds) are merged.
A test whose own run failed under the recorder in any of them is left out of every set,
so nothing is ever narrowed for it.

maps/cmp/<file>.json: {"sets": [hex bitset of tests, ...],
                       "sites": [[line, column, operator, [[known, true, tests], ...]]]}
One [known, true, tests] entry per distinct outcome: bit i of `known` says the answer of
operator i for the two operands is known, bit i of `true` that it is True; `tests` is a
position in "sets". Operators: 0 is, 1 is not, 2 ==, 3 !=, 4 <, 5 <=, 6 >, 7 >=.
known = 0: the comparison raised or did not return True/False.

maps/bool/<file>.json: {"sets": [...], "sites": [[line, column, number of operands,
[[operands evaluated, operands true, tests], ...]]]}; the two masks have one bit per
operand (left to right, nested and/or flattened); operands evaluated = -1 stands for an
evaluation that did not finish. maps/bool/_properties.json lists the names of properties
in the pinned tree whose body does more than return a side-effect-free expression.

usage: build_cmp_maps.py <tests.json> <entry repo> <recording dir> [<recording dir> ...]
"""
import ast
import glob
import json
import os
import shutil
import sys

TESTS, ENTRY = sys.argv[1:3]
RECS = sys.argv[3:]
OUT = os.path.join(ENTRY, "maps", "cmp")
OUT_BOOL = os.path.join(ENTRY, "maps", "bool")
ids = [t["id"] for t in json.load(open(TESTS))["tests"]]
number = {tid: n for n, tid in enumerate(ids)}

compared = {}   # (file, line, column, operator) -> {outcome: bits}
outcomes = {}   # (file, line, column, operands) -> {(evaluated mask, true mask): bits}
skipped = set()


def merge(rec, key, bit):
    sites = [tuple(site) for site in rec["sites"]]
    for index, outcome in rec["outcomes"].get(key, ()):
        seen = compared.setdefault(sites[index], {})
        seen[outcome] = seen.get(outcome, 0) | bit
    bsites = [tuple(site) for site in rec["bsites"]]
    for index, operands, true in rec["patterns"].get(key, ()):
        seen = outcomes.setdefault(bsites[index], {})
        seen[(operands, true)] = seen.get((operands, true), 0) | bit


for REC in RECS:
    for n in range(len(ids)):
        rc_file = os.path.join(REC, f"{n}.rc")
        parts = glob.glob(os.path.join(REC, f"{n}.*.json"))
        if not os.path.exists(rc_file) or open(rc_file).read().strip() != "0" or not parts:
            skipped.add(n)
            continue
        for part in parts:
            merge(json.load(open(part)), "process", 1 << n)
    for name in ("forward", "reverse"):
        suite = os.path.join(REC, name + ".json")
        if not os.path.exists(suite):
            if os.environ.get("ALLOW_PARTIAL"):
                continue
            raise SystemExit(f"missing {suite}")
        rec = json.load(open(suite))
        for key in set(rec["outcomes"]) | set(rec["patterns"]):
            if key in number:
                merge(rec, key, 1 << number[key])
        print(f"{REC} {name}: exit {rec['exit']}")

skipped = sorted(skipped)
usable = ((1 << len(ids)) - 1)
for n in skipped:
    usable &= ~(1 << n)

by_file = {}
for (path, line, column, operator), seen in compared.items():
    rows = [(outcome >> 8, outcome & 255, bits & usable) for outcome, bits in sorted(seen.items()) if bits & usable]
    if rows:
        by_file.setdefault(path, []).append((line, column, operator, rows))

if os.path.isdir(OUT):
    shutil.rmtree(OUT)
os.makedirs(OUT)
total = count = 0
for path, sites in sorted(by_file.items()):
    sets, positions = [], {}

    def ref(bits):
        if bits not in positions:
            positions[bits] = len(sets)
            sets.append("%x" % bits)
        return positions[bits]

    rows = [[line, column, operator, [[k, t, ref(bits)] for k, t, bits in seen]] for line, column, operator, seen in sorted(sites)]
    out = os.path.join(OUT, path.replace("/", "__") + ".json")
    json.dump({"sets": sets, "sites": rows}, open(out, "w"), separators=(",", ":"))
    total += os.path.getsize(out)
    count += len(rows)
print(f"{count} comparisons in {len(by_file)} files, {total // 1024} KB; {len(skipped)} tests without a recording: {skipped[:20]}")

# ---- and/or expressions
by_file = {}
for (path, line, column, operands), seen in outcomes.items():
    rows = [(evaluated_mask, true, bits & usable) for (evaluated_mask, true), bits in sorted(seen.items()) if bits & usable]
    if rows:
        by_file.setdefault(path, []).append((line, column, operands, rows))
if os.path.isdir(OUT_BOOL):
    shutil.rmtree(OUT_BOOL)
if by_file:
    os.makedirs(OUT_BOOL)
    total = count = 0
    for path, sites in sorted(by_file.items()):
        sets, positions = [], {}

        def ref(bits):
            if bits not in positions:
                positions[bits] = len(sets)
                sets.append("%x" % bits)
            return positions[bits]

        rows = [[line, column, operands, [[e, t, ref(bits)] for e, t, bits in seen]] for line, column, operands, seen in sorted(sites)]
        out = os.path.join(OUT_BOOL, path.replace("/", "__") + ".json")
        json.dump({"sets": sets, "sites": rows}, open(out, "w"), separators=(",", ":"))
        total += os.path.getsize(out)
        count += len(rows)

    # Properties: reading `x.name` runs code when `name` is a property. The selector treats
    # an attribute read as free of side effects unless the name is listed here.
    sys.path.insert(0, ENTRY)
    import selector

    properties = {}  # name -> [function nodes]
    for folder, _, names in os.walk(os.path.join(ENTRY, "pinned")):
        for name in names:
            if not name.endswith(".py"):
                continue
            tree = ast.parse(open(os.path.join(folder, name), encoding="utf-8").read())
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.decorator_list:
                    properties.setdefault(node.name, []).append(node)

    def simple(function, impure):
        if isinstance(function, ast.AsyncFunctionDef):
            return False
        if [ast.dump(d) for d in function.decorator_list] != [ast.dump(ast.Name(id="property", ctx=ast.Load()))]:
            return False  # any other decorator (cached_property, setters, ...) is not looked into
        body = function.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]
        return len(body) == 1 and isinstance(body[0], ast.Return) and selector._pure(body[0].value, impure)

    impure = set(properties)
    while True:
        cleared = {name for name in impure if all(simple(f, impure) for f in properties[name])}
        if not cleared:
            break
        impure -= cleared
    json.dump({"impure": sorted(impure)}, open(os.path.join(OUT_BOOL, "_properties.json"), "w"))
    print(f"{count} and/or expressions in {len(by_file)} files, {total // 1024} KB; "
          f"{len(impure)} of {len(properties)} decorated function names are not simple properties")
