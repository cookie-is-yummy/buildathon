"""Fast ground truth for patches to modules that few tests import (EVALUATION ONLY).

A test whose process never imports a module cannot be affected by a change to it. So for
a patch to such a module it is enough to run the tests that import it, which takes seconds.
Runs inside the lab container: /work is a writable copy of the pinned checkout, /entry the
entry repository (for its patch applier and maps), /syn the patch set.

usage: fast_gt.py <shard> <shards>
"""
import glob, json, os, subprocess, sys
sys.path.insert(0, "/entry")
os.environ["BLAST_DATA"] = "/syn"
import selector as S

shard, shards = int(sys.argv[1]), int(sys.argv[2])
tests = json.load(open("/syn/tests.json"))["tests"]
ids = [t["id"] for t in tests]
dur = [t["seconds"] for t in tests]
maps = S.Maps(ids)
out_dir = "/syn/fast"
os.makedirs(out_dir, exist_ok=True)
work = "/work%d" % shard
subprocess.run(["cp", "-r", "/src", work], check=True)
env = dict(os.environ, PYTHONPATH=work + ":/tools", SKIP_INTEGRATION="0")
for i, p in enumerate(sorted(glob.glob("/syn/commits/*.patch"))):
    if i % shards != shard:
        continue
    cid = os.path.basename(p)[:-6]
    result_file = os.path.join(out_dir, cid + ".json")
    if os.path.exists(result_file):
        continue
    try:
        changes = S.parse_patch(open(p, encoding="utf-8", newline="").read())
        if len(changes) != 1 or changes[0].status != "M" or not changes[0].old.endswith(".py"):
            raise ValueError("not a single Python file modification")
        c = changes[0]
        old = S.read_pinned(c.old)
        new, _, _ = S.apply_hunks(old, c.hunks)
        bits = maps.importers(c.old) | maps.readers(c.old) | maps.covering(c.old, 1, 10**9)
        numbers = maps.numbers(bits)
        if sum(dur[n] for n in numbers) > 30 or 1490 in numbers or 1106 in numbers:
            raise ValueError("too many importers")
    except Exception as error:
        json.dump({"skipped": str(error)}, open(result_file, "w"))
        continue
    target = os.path.join(work, c.old)
    open(target, "w", encoding="utf-8", newline="").write(new)
    gt_out = "/tmp/gt%d.json" % shard
    if os.path.exists(gt_out):
        os.remove(gt_out)
    failed, status = None, "ok"
    if numbers:
        try:
            subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-p", "gt_plugin", "-q",
                            "--timeout=120", *[ids[n] for n in numbers]],
                           cwd=work, env=dict(env, GT_OUT=gt_out), capture_output=True, timeout=900)
            r = json.load(open(gt_out))
            passed = set(r["passed"])
            failed = [n for n in numbers if ids[n] not in passed or ids[n] in r["failed"]]
        except Exception as error:
            status = "crash: %r" % (error,)
            failed = numbers
    else:
        failed = []
    open(target, "w", encoding="utf-8", newline="").write(old)
    json.dump({"ran": numbers, "failed": failed, "status": status}, open(result_file, "w"))
