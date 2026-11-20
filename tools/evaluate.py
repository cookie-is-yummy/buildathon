"""Run the selector on a data directory and score it against known answers.

usage: evaluate.py <data dir with commits/ and tests.json> <answers.json> [--meta meta.json]

Prints the simulated score, misses, the selector's CPU seconds and a per-kind table.
The answers are used for scoring only; the selector never sees them.
"""
import collections
import json
import os
import subprocess
import sys
import resource

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import score  # the kit's scorer

data, answers_file = sys.argv[1], sys.argv[2]
answers = json.load(open(answers_file))["commits"]
tests = json.load(open(os.path.join(data, "tests.json")))["tests"]
env = dict(os.environ, BLAST_DATA=data)
before = resource.getrusage(resource.RUSAGE_CHILDREN)
run = subprocess.run([sys.executable, os.path.join(os.path.dirname(HERE), "selector.py")], capture_output=True, env=env)
after = resource.getrusage(resource.RUSAGE_CHILDREN)
cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
open(os.path.join(os.path.dirname(HERE), "output.jsonl"), "wb").write(run.stdout)
log = {}
for line in run.stderr.decode().split("\n"):
    if " " in line:
        log[line.split(" ", 1)[0]] = line.split(" ", 1)[1]
result = score.evaluate_run(run.stdout, tests, answers)
if not result["ok"]:
    print("REJECTED", result)
    print(run.stderr.decode()[-2000:])
    sys.exit(1)
duration = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
per_kind = collections.defaultdict(lambda: [0, 0, 0, 0])
rows = []
for line in run.stdout.decode().split("\n"):
    if not line:
        continue
    row = json.loads(line)
    workers = [w for w in row["workers"] if w]
    cost = (max(sum(duration[n] for n in w) for w in workers) + 2000 * len(workers)) if workers else 0
    chosen = {n for w in workers for n in w}
    failing = answers[row["commit"]]["failing"]
    miss = any(n not in chosen for n in failing)
    ideal = (2000 + sum(duration[n] for n in failing)) if failing else 0
    kind = answers[row["commit"]].get("kind", "?")
    k = per_kind[kind]
    k[0] += 1; k[1] += cost; k[2] += miss; k[3] += ideal
    rows.append((row["commit"], kind, len(chosen), len(failing), cost, miss))
print("simulated %.3f s   misses %d   selector CPU %.2f s   commits %d" % (
    result["simulated_milliseconds"] / 1000, len(result["missed_commits"]), cpu, len(rows)))
print("%-18s %5s %10s %8s %7s %10s" % ("kind", "n", "cost s", "avg s", "misses", "oracle s"))
for kind, (n, cost, miss, ideal) in sorted(per_kind.items()):
    print("%-18s %5d %10.1f %8.2f %7d %10.1f" % (kind, n, cost / 1000, cost / 1000 / n, miss, ideal / 1000))
if "-v" in sys.argv:
    for commit, kind, chosen, failing, cost, miss in sorted(rows, key=lambda r: -r[4]):
        print("%-8s %-14s sel %4d fail %4d cost %6.1f %s %s" % (commit, kind, chosen, failing, cost / 1000, "MISS" if miss else "", log.get(commit, "")[:150]))
for commit in result["missed_commits"]:
    print("MISS", commit, answers[commit].get("kind"), log.get(commit, "")[:300])
