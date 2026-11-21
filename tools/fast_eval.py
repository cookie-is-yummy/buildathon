"""Compare the selector with fast ground truth (EVALUATION ONLY). usage: fast_eval.py <syn dir>"""
import collections, glob, json, os, subprocess, sys
syn = sys.argv[1]
meta = json.load(open(syn + "/meta.json"))
tests = json.load(open(syn + "/tests.json"))["tests"]
dur = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
r = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "selector.py")], capture_output=True, env=dict(os.environ, BLAST_DATA=syn))
log = {l.split(" ", 1)[0]: l.split(" ", 1)[1] for l in r.stderr.decode().split("\n") if " " in l}
sel = {}
for line in r.stdout.decode().split("\n"):
    if line:
        row = json.loads(line)
        ws = [w for w in row["workers"] if w]
        sel[row["commit"]] = ({n for w in ws for n in w}, (max(sum(dur[n] for n in w) for w in ws) + 2000 * len(ws)) if ws else 0)
stats = collections.defaultdict(lambda: [0, 0, 0, 0, 0, 0])  # n, with failures, misses, cost, importer-all cost, oracle
skipped = 0
for f in sorted(glob.glob(syn + "/fast/*.json")):
    cid = os.path.basename(f)[:-5]
    g = json.load(open(f))
    if "skipped" in g:
        skipped += 1
        continue
    chosen, cost = sel[cid]
    failed = set(g["failed"])
    k = stats[meta[cid]["kind"]]
    k[0] += 1
    k[1] += bool(failed)
    crashed = g["status"] != "ok"  # the test process died (e.g. out of memory): no per-test verdicts
    miss = not failed <= chosen and not crashed
    k[2] += miss
    k[3] += cost
    k[4] += len(chosen)
    k[5] += len(g["ran"])
    if miss or g["status"] != "ok":
        print("MISS" if miss else "NOTE", cid, meta[cid]["kind"], g["status"][:60], "failed", len(failed), "missed", sorted(failed - chosen)[:8], "|", meta[cid]["desc"][:80], "|", log.get(cid, "")[:160])
print("%-12s %5s %6s %6s %9s %9s %10s" % ("kind", "n", "fails", "MISS", "cost s", "selected", "importers"))
tot = [0] * 6
for kind, k in sorted(stats.items()):
    print("%-12s %5d %6d %6d %9.1f %9d %10d" % (kind, k[0], k[1], k[2], k[3] / 1000, k[4], k[5]))
    tot = [a + b for a, b in zip(tot, k)]
print("%-12s %5d %6d %6d %9.1f %9d %10d   (skipped %d)" % ("TOTAL", tot[0], tot[1], tot[2], tot[3] / 1000, tot[4], tot[5], skipped))
