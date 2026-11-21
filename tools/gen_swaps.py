"""Comparison-operator patches for checking the comparison rule (EVALUATION ONLY).

For every two-operand comparison in the pinned sqlglot and every other operator, the
selector is asked twice: with the comparison recordings and without. Where the recordings
let it drop tests, the patch is a candidate; a sample of candidates is written out together
with the tests that were dropped. tools/swap_gt.py then runs exactly those tests on the
patched code: every one of them must pass.

usage: gen_swaps.py <pinned checkout> <tests.json> <output dir> <how many> [seed]
"""
import ast
import difflib
import json
import os
import random
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import selector as S

REPO, TESTS, OUT, COUNT = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
rng = random.Random(int(sys.argv[5]) if len(sys.argv) > 5 else 1)
tests = json.load(open(TESTS))["tests"]
maps = S.Maps([t["id"] for t in tests])
blind = S.Maps([t["id"] for t in tests])
blind.comparison = lambda *a: None
TOKENS = ["is", "is not", "==", "!=", "<", "<=", ">", ">="]
PATTERN = re.compile(r"is\s+not|is|==|!=|<=|>=|<|>")

files = [f for f in subprocess.run(["git", "-C", REPO, "ls-files", "sqlglot"], capture_output=True, text=True).stdout.split() if f.endswith(".py")]
candidates = []
for path in files:
    text = open(os.path.join(REPO, path), encoding="utf-8").read()
    lines = text.split("\n")
    for node in ast.walk(ast.parse(text)):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1 or type(node.ops[0]) not in S._COMPARISONS:
            continue
        left, right = node.left, node.comparators[0]
        if left.end_lineno != right.lineno:
            continue
        raw = lines[right.lineno - 1].encode("utf-8")
        between = raw[left.end_col_offset:right.col_offset].decode("utf-8")
        match = PATTERN.search(between)
        if not match or PATTERN.search(between, match.end()):
            continue
        old = S._COMPARISONS.index(type(node.ops[0]))
        for new in range(8):
            if new == old:
                continue
            changed = raw[:left.end_col_offset].decode("utf-8") + between[:match.start()] + TOKENS[new] + between[match.end():] + raw[right.col_offset:].decode("utf-8")
            new_lines = lines[:right.lineno - 1] + [changed] + lines[right.lineno:]
            patch = "diff --git a/%s b/%s\n" % (path, path) + "\n".join(
                difflib.unified_diff(lines, new_lines, "a/" + path, "b/" + path, lineterm="", n=3)) + "\n"
            try:
                with_map = set(maps.numbers(S.select(patch, maps, [])))
                without = set(blind.numbers(S.select(patch, blind, [])))
            except S.RunEverything:
                continue
            assert with_map <= without, (path, node.lineno)
            dropped = sorted(without - with_map)
            if dropped:
                candidates.append((path, node.lineno, old, new, patch, "\n".join(new_lines), dropped, len(without)))

print(len(candidates), "candidate patches where the recordings drop tests")
by_pair = {}
for c in candidates:
    by_pair.setdefault((c[2], c[3]), []).append(c)
for pair, group in sorted(by_pair.items()):
    print("  %s -> %s: %d" % (TOKENS[pair[0]], TOKENS[pair[1]], len(group)))
    rng.shuffle(group)
chosen = []
while len(chosen) < COUNT and any(by_pair.values()):
    for pair in sorted(by_pair):
        if by_pair[pair] and len(chosen) < COUNT:
            chosen.append(by_pair[pair].pop())

for sub in ("commits", "files"):
    os.makedirs(os.path.join(OUT, sub), exist_ok=True)
meta = {}
for number, (path, line, old, new, patch, new_text, dropped, before) in enumerate(chosen):
    cid = "w%04d" % number
    open(os.path.join(OUT, "commits", cid + ".patch"), "w", encoding="utf-8").write(patch)
    open(os.path.join(OUT, "files", cid + ".py"), "w", encoding="utf-8").write(new_text)
    meta[cid] = {
        "path": path, "line": line, "kind": "%s -> %s" % (TOKENS[old], TOKENS[new]),
        "dropped": [tests[n]["id"] for n in dropped], "selected_before": before,
        "seconds": round(sum(tests[n]["seconds"] for n in dropped), 3),
    }
json.dump(meta, open(os.path.join(OUT, "meta.json"), "w"), indent=1)
print(len(meta), "patches written;", round(sum(m["seconds"] for m in meta.values())), "test seconds to run")
