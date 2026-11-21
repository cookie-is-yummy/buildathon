"""Build maps/cmp/*.json: per comparison of the pinned sqlglot, which tests evaluated it
and in which tests each other comparison operator would have given a different result.

Input: the files written by tools/site_cmp/sitecustomize.py (tools/record_cmp.sh):
<number>.<pid>.json and <number>.rc for every test run alone, and forward.json /
reverse.json for the whole suite in one process. The map holds the union over all runs.
A test whose own run failed under the recorder is left out of every set, so nothing is
ever narrowed for it.

Operators are numbered: 0 is, 1 is not, 2 ==, 3 !=, 4 <, 5 <=, 6 >, 7 >=.
Output per file: {"sets": [hex bitset, ...],
                  "sites": [[line, column, operator, evaluated, [differs per operator]]]}
where `evaluated` and the entries of `differs` are positions in "sets" (null for the
comparison's own operator).

usage: build_cmp_maps.py <tests.json> <entry repo> <recording dir>
"""
import glob
import json
import os
import shutil
import sys

TESTS, ENTRY, REC = sys.argv[1:4]
OUT = os.path.join(ENTRY, "maps", "cmp")
ids = [t["id"] for t in json.load(open(TESTS))["tests"]]
number = {tid: n for n, tid in enumerate(ids)}

evaluated = {}  # (file, line, column, operator) -> bits
differs = {}    # (file, line, column, operator, alternative) -> bits
skipped = []


def merge(rec, key, bit):
    sites = [tuple(site) for site in rec["sites"]]
    for index in rec["executed"].get(key, ()):
        evaluated[sites[index]] = evaluated.get(sites[index], 0) | bit
    for index, alternative in rec["differs"].get(key, ()):
        site = sites[index] + (alternative,)
        differs[site] = differs.get(site, 0) | bit


for n in range(len(ids)):
    rc_file = os.path.join(REC, f"{n}.rc")
    parts = glob.glob(os.path.join(REC, f"{n}.*.json"))
    if not os.path.exists(rc_file) or open(rc_file).read().strip() != "0" or not parts:
        skipped.append(n)
        continue
    for part in parts:
        merge(json.load(open(part)), "process", 1 << n)

usable = ((1 << len(ids)) - 1)
for n in skipped:
    usable &= ~(1 << n)

for name in ("forward", "reverse"):
    suite = os.path.join(REC, name + ".json")
    if not os.path.exists(suite):
        if os.environ.get("ALLOW_PARTIAL"):
            continue
        raise SystemExit(f"missing {suite}")
    rec = json.load(open(suite))
    for key in set(rec["executed"]) | set(rec["differs"]):
        if key in number:
            merge(rec, key, 1 << number[key])
    print(f"{name}: exit {rec['exit']}")

by_file = {}
for (path, line, column, operator), bits in evaluated.items():
    bits &= usable
    if bits:
        by_file.setdefault(path, []).append((line, column, operator, bits))

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

    rows = []
    for line, column, operator, bits in sorted(sites):
        alternatives = [
            None if alt == operator else ref(differs.get((path, line, column, operator, alt), 0))
            for alt in range(8)
        ]
        rows.append([line, column, operator, ref(bits), alternatives])
    out = os.path.join(OUT, path.replace("/", "__") + ".json")
    json.dump({"sets": sets, "sites": rows}, open(out, "w"), separators=(",", ":"))
    total += os.path.getsize(out)
    count += len(rows)
print(f"{count} comparisons in {len(by_file)} files, {total // 1024} KB; {len(skipped)} tests without a recording: {skipped[:20]}")
