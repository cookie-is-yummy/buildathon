"""Which lines does each TPC-DS query execute inside the three calls that replay.py
repeats, and how much CPU does the query take? Run inside the ENTRY image (the
environment replay.py runs in) on the pinned copy:

    docker run --rm -v $PWD/tools:/tools:ro -v $PWD/rec:/out <entry image> \
        python -B -s /tools/record_replay.py /app/pinned /out/replay.json

Every query is recorded twice per hash seed: alone in a fresh process, and as part of
one process that runs all queries in order. A line that a query executes when it is
alone but not when others ran before it belongs to something done once per process (a
table built on first use, a cache). Such lines are reported as "once": which query
executes them depends on what ran earlier, so the map must not rely on them.

Lines are kept apart by where they run: in the step that executes a plan over the data
("run"), or anywhere else inside the three calls ("plan").

Writes {"plan": {"<query>": {"<file>": [lines]}}, "run": {...}   (alone, all seeds together)
        "once": {"<file>": [lines]},
        "seconds": {"plan": {"<query>": cpu}, "full": {"<query>": cpu}},
        "alone_equals_together": bool}                 (fingerprints agree)
"""
import json
import os
import subprocess
import sys
import time

SEEDS = ("0", "1", "2")


def record(root, wanted):
    """Run the wanted queries in this process; returns per-query lines and fingerprints."""
    sys.path.insert(0, "/app")
    import replay

    prefix = root + "/"
    mon = sys.monitoring
    tool = 4
    mon.use_tool_id(tool, "blast-radius-replay")
    inside = [0]   # 0 outside the calls, 1 inside, 2 inside and executing a plan
    current = [None]
    lines = {}

    def on_line(code, line):
        name = code.co_filename
        if inside[0] > 0 and name.startswith(prefix):
            lines[current[0]][inside[0] - 1].setdefault(name[len(prefix):], set()).add(line)
        return mon.DISABLE

    mon.register_callback(tool, mon.events.LINE, on_line)
    mon.set_events(tool, mon.events.LINE)

    def call(function, *args, **kwargs):
        inside[0] = 1
        mon.restart_events()
        try:
            return function(*args, **kwargs)
        finally:
            inside[0] = 0
            mon.restart_events()

    def run(function):
        inside[0] = 2
        mon.restart_events()
        try:
            return function()
        finally:
            inside[0] = 1
            mon.restart_events()

    def apart(function, *args):
        saved, inside[0] = inside[0], 0
        mon.restart_events()
        try:
            return function(*args)
        finally:
            inside[0] = saved
            mon.restart_events()

    replay._call = call
    replay._run = run
    replay._apart = apart
    pairs, schema, tables = replay.prepare(root, {})
    prints = {}
    for number in (range(len(pairs)) if wanted == "all" else [int(wanted)]):
        meta, sql, _ = pairs[number]
        current[0] = str(number)
        lines[current[0]] = ({}, {})
        prints[str(number)] = replay.replay_one(meta, sql, schema, tables, True)
    return {"plan": {q: {f: sorted(v) for f, v in both[0].items()} for q, both in lines.items()},
            "run": {q: {f: sorted(v) for f, v in both[1].items()} for q, both in lines.items()}, "prints": prints}


def timing(root):
    sys.path.insert(0, "/app")
    import replay

    pairs, schema, tables = replay.prepare(root, {})
    seconds = {"plan": {}, "full": {}}
    for kind in ("full", "plan"):
        for number, (meta, sql, _) in enumerate(pairs):
            start = time.process_time()
            replay.replay_one(meta, sql, schema, tables, kind == "full")
            seconds[kind][str(number)] = round(time.process_time() - start, 3)
    return seconds


def child(mode, root, wanted, seed):
    return subprocess.Popen([sys.executable, "-B", "-s", os.path.abspath(__file__), mode, root, wanted],
                            env=dict(os.environ, PYTHONHASHSEED=seed), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


def main():
    if sys.argv[1] == "record":
        return print(json.dumps(record(sys.argv[2], sys.argv[3])))
    if sys.argv[1] == "timing":
        return print(json.dumps(timing(sys.argv[2])))
    root, out = sys.argv[1].rstrip("/"), sys.argv[2]
    seconds = json.loads(child("timing", root, "-", "0").communicate()[0])
    together = {seed: json.loads(child("record", root, "all", seed).communicate()[0]) for seed in SEEDS}
    alone = {}
    jobs = [(seed, query) for seed in SEEDS for query in sorted(seconds["full"], key=int)]
    running = []
    while jobs or running:
        while jobs and len(running) < (os.cpu_count() or 2):
            seed, query = jobs.pop()
            running.append((seed, query, child("record", root, query, seed)))
        seed, query, process = running.pop(0)
        alone[(seed, query)] = json.loads(process.communicate()[0])
    merged = {"plan": {}, "run": {}}
    once, agree = {}, True
    for (seed, query), got in alone.items():
        agree = agree and got["prints"][query] == together[seed]["prints"][query]
        for part in ("plan", "run"):
            files = merged[part].setdefault(query, {})
            seen_together = together[seed][part][query]
            for path in set(got[part][query]) | set(seen_together):
                here, there = set(got[part][query].get(path, ())), set(seen_together.get(path, ()))
                files.setdefault(path, set()).update(here)
                if here != there:
                    # executed when alone but not after others, or the other way round
                    once.setdefault(path, set()).update(here ^ there)
    json.dump({"plan": {q: {f: sorted(v) for f, v in files.items() if v} for q, files in merged["plan"].items()},
               "run": {q: {f: sorted(v) for f, v in files.items() if v} for q, files in merged["run"].items()},
               "once": {f: sorted(v) for f, v in once.items()}, "seconds": seconds,
               "alone_equals_together": agree}, open(out, "w"))
    print("%d queries, %d seeds; cpu for all: %.1f s executed, %.1f s up to the plan; %d lines depend on what ran before; "
          "fingerprints alone = together: %s" % (len(merged["plan"]), len(SEEDS), sum(seconds["full"].values()),
                                                 sum(seconds["plan"].values()), sum(len(v) for v in once.values()), agree))


if __name__ == "__main__":
    main()
