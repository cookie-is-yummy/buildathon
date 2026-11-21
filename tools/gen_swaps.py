"""Operator patches for checking the comparison and and/or rules (EVALUATION ONLY).

For every two-operand comparison in the pinned sqlglot and every other comparison operator
(kind "cmp"), or for every single `and`/`or` keyword flipped to the other one (kind
"bool"), the selector is asked twice: with the recordings and without. Where the recordings
let it drop tests, the patch is a candidate; a sample of candidates is written out together
with the tests that were dropped. tools/swap_gt.py then runs exactly those tests on the
patched code: every one of them must pass.

Also prints what the rule saves over ALL such patches (the commit cost with and without).

usage: gen_swaps.py <pinned checkout> <tests.json> <output dir> <how many> <cmp|bool> [seed]
"""
import ast
import difflib
import json
import os
import random
import re
import subprocess
import sys
import warnings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import selector as S

warnings.simplefilter("ignore")
REPO, TESTS, OUT, COUNT, MODE = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), sys.argv[5]
rng = random.Random(int(sys.argv[6]) if len(sys.argv) > 6 else 1)
tests = json.load(open(TESTS))["tests"]
duration = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
maps = S.Maps([t["id"] for t in tests])
blind = S.Maps([t["id"] for t in tests])
blind.comparison = lambda *a: None
blind.outcomes = lambda *a: None
TOKENS = ["is", "is not", "==", "!=", "<", "<=", ">", ">="]
PATTERN = re.compile(r"is\s+not|is|==|!=|<=|>=|<|>")
WORD = re.compile(r"\b(and|or)\b")


def cost(numbers):
    workers = S.split(numbers, duration)
    return (max(sum(duration[n] for n in w) for w in workers) + 2000 * len(workers)) / 1000 if workers else 0.0


def offset(lines, line, column):
    """Character position in the whole text of a (1-based line, utf-8 byte column)."""
    start = sum(len(text) + 1 for text in lines[:line - 1])
    return start + len(lines[line - 1].encode("utf-8")[:column].decode("utf-8"))


def variants(text, lines):
    """(line, label, new text) for every single-operator edit of the chosen kind."""
    for node in ast.walk(ast.parse(text)):
        if MODE == "cmp" and isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in S._COMPARISONS:
            left, right = node.left, node.comparators[0]
            start, end = offset(lines, left.end_lineno, left.end_col_offset), offset(lines, right.lineno, right.col_offset)
            between = text[start:end]
            match = PATTERN.search(between)
            if not match or PATTERN.search(between, match.end()) or "#" in between:
                continue
            old = S._COMPARISONS.index(type(node.ops[0]))
            for new in range(8):
                if new != old:
                    yield (right.lineno, "%s -> %s" % (TOKENS[old], TOKENS[new]),
                           text[:start + match.start()] + TOKENS[new] + text[start + match.end():])
        if MODE == "bool" and isinstance(node, ast.BoolOp):
            word = "and" if isinstance(node.op, ast.And) else "or"
            other = "or" if word == "and" else "and"
            for first, second in zip(node.values, node.values[1:]):
                start, end = offset(lines, first.end_lineno, first.end_col_offset), offset(lines, second.lineno, second.col_offset)
                between = text[start:end]
                found = WORD.findall(between)
                if found != [word] or "#" in between:
                    continue
                match = WORD.search(between)
                yield (second.lineno, "%s -> %s" % (word, other),
                       text[:start + match.start()] + other + text[start + match.end():])


files = [f for f in subprocess.run(["git", "-C", REPO, "ls-files", "sqlglot"], capture_output=True, text=True).stdout.split() if f.endswith(".py")]
candidates = []
totals = {}  # label -> [patches, cost without recordings, cost with, patches narrowed]
for path in files:
    text = open(os.path.join(REPO, path), encoding="utf-8").read()
    lines = text.split("\n")
    for line, label, new_text in variants(text, lines):
        try:
            ast.parse(new_text)
        except SyntaxError:
            continue
        patch = "diff --git a/%s b/%s\n" % (path, path) + "\n".join(
            difflib.unified_diff(lines, new_text.split("\n"), "a/" + path, "b/" + path, lineterm="", n=3)) + "\n"
        try:
            with_map = set(maps.numbers(S.select(patch, maps, [])))
            without = set(blind.numbers(S.select(patch, blind, [])))
        except S.RunEverything:
            continue
        # the recordings may also add tests (an expression reached only through the
        # recorder's own trial comparisons), so `with_map` is not always a subset
        dropped = sorted(without - with_map)
        total = totals.setdefault(label, [0, 0.0, 0.0, 0])
        total[0] += 1
        total[1] += cost(without)
        total[2] += cost(with_map)
        if dropped:
            total[3] += 1
            candidates.append((path, line, label, patch, new_text, dropped, len(without)))

print(len(candidates), "candidate patches where the recordings drop tests")
print("  %-16s %7s %9s %10s %10s" % ("edit", "patches", "narrowed", "avg before", "avg after"))
for label, (count, before, after, narrowed) in sorted(totals.items()):
    print("  %-16s %7d %9d %10.2f %10.2f" % (label, count, narrowed, before / count, after / count))
by_label = {}
for c in candidates:
    by_label.setdefault(c[2], []).append(c)
for group in by_label.values():
    rng.shuffle(group)
chosen = []
while len(chosen) < COUNT and any(by_label.values()):
    for label in sorted(by_label):
        if by_label[label] and len(chosen) < COUNT:
            chosen.append(by_label[label].pop())

for sub in ("commits", "files"):
    os.makedirs(os.path.join(OUT, sub), exist_ok=True)
meta = {}
for number, (path, line, label, patch, new_text, dropped, before) in enumerate(chosen):
    cid = "%s%04d" % ("w" if MODE == "cmp" else "b", number)
    open(os.path.join(OUT, "commits", cid + ".patch"), "w", encoding="utf-8").write(patch)
    open(os.path.join(OUT, "files", cid + ".py"), "w", encoding="utf-8").write(new_text)
    meta[cid] = {
        "path": path, "line": line, "kind": label,
        "dropped": [tests[n]["id"] for n in dropped], "selected_before": before,
        "seconds": round(sum(tests[n]["seconds"] for n in dropped), 3),
    }
json.dump(meta, open(os.path.join(OUT, "meta.json"), "w"), indent=1)
print(len(meta), "patches written;", round(sum(m["seconds"] for m in meta.values())), "test seconds to run")
