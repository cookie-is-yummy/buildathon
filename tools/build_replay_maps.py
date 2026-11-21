"""Build maps/replay.json from the phase recording of test_execute_tpcds
(tools/record_phase.sh, real test) and the per-query recording of replay.py
(tools/record_replay.py).

A line gets into the map only if the real test executes it inside the three replayed
calls and never outside them (in any of the recorded runs), replay.py executes it too,
and which queries execute it does not depend on what ran earlier in the process. For
each such line the map holds the set of queries whose replay executes it, and the set
of queries that execute it while a plan is being executed over the data.

usage: build_replay_maps.py <phase dir> <replay.json> <entry repo>
"""
import glob
import json
import os
import sys

PHASE, REPLAY, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
TEST = "tests/test_executor.py::TestExecutor::test_execute_tpcds"

outside, inside, calls = set(), set(), {}
runs = sorted({name.split(".")[1] for name in os.listdir(PHASE) if name.endswith(".json")})
for run in runs:
    if open(os.path.join(PHASE, "tpcds.%s.rc" % run)).read().strip() != "0":
        raise SystemExit("run %s of the test failed under the recorder" % run)
    for name in glob.glob(os.path.join(PHASE, "tpcds.%s.*.json" % run)):
        data = json.load(open(name))
        for path, lines in data["outside"].items():
            outside.update((path, line) for line in lines)
        for path, lines in data["inside"].items():
            inside.update((path, line) for line in lines)
        for key, count in data["calls"].items():
            calls[key] = calls.get(key, 0) + count
replay = json.load(open(REPLAY))
queries, running = {}, {}  # line -> queries executing it inside the calls / while a plan is executed
for part, target in (("plan", queries), ("run", queries), ("run", running)):
    for query, files in replay[part].items():
        for path, lines in files.items():
            for line in lines:
                target[(path, line)] = target.get((path, line), 0) | 1 << int(query)

once = {(path, line) for path, lines in replay["once"].items() for line in lines}
if not replay["alone_equals_together"]:
    raise SystemExit("a query's fingerprint depends on what ran before it")
only = inside - outside - once
print("replay: %d lines are executed depending on what ran before (left out)" % len(once))
print("real test, %d runs: %d lines inside the calls, %d of them never outside" % (len(runs), len(inside), len(only)))
for key in sorted(calls):
    print("   %5d x %s" % (calls[key], key))
print("replay: %d lines" % len(queries))
print("inside in the real test but not executed by the replay: %d" % len(inside - set(queries)))
print("executed by the replay but not inside in the real test: %d" % len(set(queries) - inside))
for title, extra in (("test only", inside - set(queries)), ("replay only", set(queries) - inside)):
    by_file = {}
    for path, line in extra:
        by_file.setdefault(path, []).append(line)
    for path in sorted(by_file):
        print("   %s: %s %s" % (title, path, sorted(by_file[path])[:12]))
files = {}
for path, line in sorted(only & set(queries)):
    files.setdefault(path, []).append([line, "%x" % queries[(path, line)], "%x" % running.get((path, line), 0)])
count = len(replay["seconds"]["full"])
seconds = {kind: [replay["seconds"][kind][str(q)] for q in range(count)] for kind in ("plan", "full")}
json.dump({"test": TEST, "seconds": seconds, "files": files}, open(os.path.join(OUT, "maps", "replay.json"), "w"),
          separators=(",", ":"))
print("%d lines in %d files written (%d of them never run while a plan is executed); cpu for all queries: %.1f s executed, %.1f s up to the plan" % (
    sum(len(v) for v in files.values()), len(files), sum(1 for v in files.values() for row in v if row[2] == "0"),
    sum(seconds["full"]), sum(seconds["plan"])))
