"""Run, for each patch made by gen_swaps.py, the tests the comparison rule dropped
(EVALUATION ONLY). Inside the lab container: /src pinned checkout (ro), /tools, /syn the
patch set (rw). Writes /syn/result/<id>.json. Every dropped test must pass.

usage: swap_gt.py <shard> <shards>
"""
import json
import os
import shutil
import subprocess
import sys

shard, shards = int(sys.argv[1]), int(sys.argv[2])
meta = json.load(open("/syn/meta.json"))
work = "/work%d" % shard
subprocess.run(["cp", "-r", "/src", work], check=True)
os.makedirs("/syn/result", exist_ok=True)
env = dict(os.environ, PYTHONPATH=work + ":/tools", SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1")
for i, cid in enumerate(sorted(meta)):
    if i % shards != shard:
        continue
    out = "/syn/result/%s.json" % cid
    if os.path.exists(out):
        continue
    info = meta[cid]
    target = os.path.join(work, info["path"])
    shutil.copyfile("/syn/files/%s.py" % cid, target)
    args = "/tmp/args%d.txt" % shard
    open(args, "w").write("\n".join(info["dropped"]) + "\n")
    result = "/tmp/gt%d.json" % shard
    if os.path.exists(result):
        os.remove(result)
    try:
        run = subprocess.run(
            [sys.executable, "-m", "pytest", "@" + args, "-p", "no:cacheprovider", "-p", "gt_plugin", "-q", "--timeout=300"],
            cwd=work, env=dict(env, GT_OUT=result), capture_output=True, text=True, timeout=1500)
        tail = run.stdout.strip().split("\n")[-1][:200]
    except subprocess.TimeoutExpired:
        tail = "TIMEOUT"
    verdict = {"tail": tail}
    if os.path.exists(result):
        got = json.load(open(result))
        passed = set(got["passed"])
        verdict["not_passed"] = [t for t in info["dropped"] if t not in passed]
    else:
        verdict["crash"] = True
    json.dump(verdict, open(out, "w"))
    shutil.copyfile(os.path.join("/src", info["path"]), target)
    print(cid, info["kind"], len(info["dropped"]), tail, flush=True)
