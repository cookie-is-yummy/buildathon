"""Differential test: chunked parsing must give the same Diff as whole-file parsing."""
import glob, json, os, sys
sys.path.insert(0, "/home/user/Digestr/blast-radius")
import selector as S
index = json.load(open("/home/user/Digestr/blast-radius/maps/index.json"))
def key(d):
    if d is None: return None
    return (d.whole_module, sorted(d.statements), sorted(d.functions), sorted(d.lambdas),
            sorted((a[0], a[1], a[3]) for a in d.attributes), d.docstrings)
n = same = chunked = 0
for directory in sys.argv[1:]:
    for p in sorted(glob.glob(directory + "/*.patch")):
        try:
            changes = S.parse_patch(open(p, encoding="utf-8", newline="").read())
        except Exception:
            continue
        for c in changes:
            if c.status != "M" or c.binary or not c.hunks or not c.old.endswith(".py"):
                continue
            old = S.read_pinned(c.old)
            try:
                new, ob, nb = S.apply_hunks(old, c.hunks)
            except S.RunEverything:
                continue
            n += 1
            full = key(S.diff_python(old, new, ob, nb, None))
            fast = key(S.diff_python(old, new, ob, nb, index.get(c.old)))
            if S._find_chunk(index.get(c.old), ob): chunked += 1
            if full == fast: same += 1
            else: print("DIFFERS", p, c.old, "\n  full", full, "\n  fast", fast)
print("file changes", n, "identical", same, "used chunk", chunked)
