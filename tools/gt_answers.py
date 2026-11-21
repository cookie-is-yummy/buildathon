"""Turn ground-truth pytest results for synthetic patches into an answers.json (evaluation only)."""
import json, os, sys, glob
syn, tests_file = sys.argv[1], sys.argv[2]
tests = json.load(open(tests_file))["tests"]
meta = json.load(open(os.path.join(syn, "meta.json")))
out = {}
for cid, m in meta.items():
    f = os.path.join(syn, "gt", cid + ".json")
    if not os.path.exists(f):
        continue
    r = json.load(open(f))
    if r.get("applyfail"):
        continue
    if r.get("crash"):
        failing = [t["number"] for t in tests]
        strict = failing
    else:
        passed = set(r["passed"]); failed = set(r["failed"]); cf = set(r["collect_failed"])
        strict = [t["number"] for t in tests if t["id"] in failed or t["file"] in cf]
        failing = [t["number"] for t in tests if t["id"] not in passed or t["id"] in failed]
    out[cid] = {"failing": failing, "strict": strict, "kind": m["kind"], "desc": m["desc"]}
data = os.path.join(syn, "data"); os.makedirs(os.path.join(data, "commits"), exist_ok=True)
for f in glob.glob(os.path.join(data, "commits", "*")): os.remove(f)
for cid in out:
    os.link(os.path.join(syn, "commits", cid + ".patch"), os.path.join(data, "commits", cid + ".patch"))
if not os.path.exists(os.path.join(data, "tests.json")):
    os.link(tests_file, os.path.join(data, "tests.json")) if False else __import__("shutil").copyfile(tests_file, os.path.join(data, "tests.json"))
json.dump({"commits": out}, open(os.path.join(syn, "answers.json"), "w"))
print(len(out), "answers;", sum(1 for v in out.values() if v["failing"]), "with failures;", sum(1 for v in out.values() if v["failing"] != v["strict"]), "where not-passed differs from failed")
