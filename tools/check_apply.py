"""Check the selector's patch parser/applier against `git apply` on a set of patches."""
import os, subprocess, sys, glob
sys.path.insert(0, "/home/user/Digestr/blast-radius")
import selector as S
repo = "/home/user/br-work/sqlglot_chk"
if not os.path.isdir(repo):
    subprocess.run(["cp", "-r", "/home/user/br-work/sqlglot", repo], check=True)
ok = bad = 0
import collections
kinds = collections.Counter()
for p in sorted(glob.glob(sys.argv[1] + "/*.patch")):
    subprocess.run(["git", "-C", repo, "reset", "-q", "--hard", "HEAD"]); subprocess.run(["git", "-C", repo, "clean", "-fdq"])
    r = subprocess.run(["git", "-C", repo, "apply", p], capture_output=True)
    if r.returncode: print("git apply failed", p, r.stderr[:200]); continue
    try:
        changes = S.parse_patch(open(p, encoding="utf-8", newline="").read())
    except S.RunEverything as e:
        kinds["ALL:" + str(e)] += 1; continue
    except UnicodeDecodeError:
        kinds["undecodable"] += 1; continue
    for c in changes:
        kinds[c.status + ("b" if c.binary else "")] += 1
        if c.status == "M" and not c.binary and c.hunks:
            old = open(os.path.join("/home/user/br-work/sqlglot", c.old), encoding="utf-8", newline="").read()
            try:
                new, ob, nb = S.apply_hunks(old, c.hunks)
            except S.RunEverything as e:
                kinds["applyALL:" + str(e)] += 1; continue
            real = open(os.path.join(repo, c.new), encoding="utf-8", newline="").read()
            if new == real: ok += 1
            else: bad += 1; print("MISMATCH", p, c.old)
            if c.old.endswith(".py"):
                d = S.diff_python(old, new, ob, nb)
                import ast
                try: ast.parse(real); parses = True
                except SyntaxError: parses = False
                same = parses and ast.dump(ast.parse(old)) == ast.dump(ast.parse(real))
                empty = d is not None and not (d.whole_module or d.statements or d.functions or d.lambdas or d.attributes or d.docstrings)
                if (d is None) != (not parses and True) and parses: print("COMPILE-ONLY failure", p)
                if d is not None and empty != same: print("DIFF DISAGREES", p, "empty", empty, "same", same)
subprocess.run(["git", "-C", repo, "reset", "-q", "--hard", "HEAD"]); subprocess.run(["git", "-C", repo, "clean", "-fdq"])
print("ok", ok, "bad", bad, dict(kinds))
