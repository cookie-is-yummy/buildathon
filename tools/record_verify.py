"""Which tests can verify.py decide, and what does each cost? Run inside the ENTRY image
on the pinned copy:

    docker run --rm -v $PWD/tools:/tools:ro -v $PWD/rec:/out -v $PWD/sample/tests.json:/data/tests.json:ro \
        <entry image> python -B -s /tools/record_verify.py /app/pinned /data/tests.json /out/verify.json

Every test is run alone in a fresh process, twice, and once more together with the other
tests of its file. A test is "verifiable" only if it passes every time: a test that does
not pass here at the pinned commit (a package the image does not have, a test that needs
another test to run first) can never be decided here.

Writes {"start": cpu of a run before its first test,
        "tests": {id: [cpu after other tests of its file, extra cpu when it is the first]}}.
CPU is that of the whole process tree (some tests start worker processes).
"""
import json
import os
import subprocess
import sys
import tempfile

ROOT, TESTS, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
VERIFY = "/app/verify.py"
ENV = {"SKIP_INTEGRATION": "0", "PYTHONDONTWRITEBYTECODE": "1", "HOME": "/tmp", "PATH": os.environ["PATH"],
       "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}  # as the selector runs it
SKIP_FILES = ("tests/test_executor.py",)  # needs DuckDB, pandas and the CSV tables
work = tempfile.mkdtemp()


def start(name, ids):
    wanted, out = os.path.join(work, name + ".txt"), os.path.join(work, name + ".json")
    open(wanted, "w").write("\n".join(ids) + "\n")
    # /usr/bin/env python under a tiny wrapper that reports the CPU of the whole process tree
    code = ("import json,resource,subprocess,sys;"
            "r=subprocess.run([sys.executable,'-B','-s']+sys.argv[1:],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
            "u=resource.getrusage(resource.RUSAGE_CHILDREN);"
            "open(sys.argv[4]+'.cpu','w').write(str(u.ru_utime+u.ru_stime))")
    return out, subprocess.Popen([sys.executable, "-B", "-s", "-c", code, VERIFY, ROOT, wanted, out], env=ENV, cwd=ROOT,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def finish(job):
    out, process = job
    try:
        process.wait(timeout=900)
        result = json.load(open(out))
        result["cpu"] = float(open(out + ".cpu").read())
        return result
    except Exception:
        process.kill()
        return None


def run_all(jobs):
    """jobs: [(name, ids)] -> {name: result}, a few at a time."""
    results, running, todo = {}, [], list(jobs)
    while todo or running:
        while todo and len(running) < (os.cpu_count() or 2):
            name, ids = todo.pop()
            running.append((name, start(name, ids)))
        name, job = running.pop(0)
        results[name] = finish(job)
    return results


ids = [t["id"] for t in json.load(open(TESTS))["tests"] if t["id"].split("::")[0] not in SKIP_FILES]
# What a run costs before its first test: starting Python, importing sqlglot and a test
# module. Measured with one quick test, whose own time is taken off.
PROBE = next(test for test in ids if test.startswith("tests/test_helper.py"))
base = sum(finish(start("probe", [PROBE]))["cpu"] for _ in range(5)) / 5
good, alone_cpu = set(ids), {}
for attempt in range(2):
    alone = run_all([("a%d_%d" % (attempt, n), [test]) for n, test in enumerate(ids)])
    for n, test in enumerate(ids):
        got = alone["a%d_%d" % (attempt, n)]
        if got is None or got["not_passed"] or test not in got["seconds"]:
            good.discard(test)
        else:
            alone_cpu[test] = max(alone_cpu.get(test, 0.0), got["cpu"] - base)
files = {}
for test in ids:
    files.setdefault(test.split("::")[0], []).append(test)
together = run_all([("f%d" % n, tests) for n, tests in enumerate(files.values())])
table = {}
for n, tests in enumerate(files.values()):
    got = together["f%d" % n]
    for test in tests:
        if got is None or test in got["not_passed"] or test not in got["seconds"]:
            good.discard(test)
        elif test in good:
            # [CPU after others of its file, extra CPU when it is the first]
            after = min(got["seconds"][test], alone_cpu[test])
            table[test] = [round(max(after, 0.0), 4), round(max(alone_cpu[test] - after, 0.0), 4)]
json.dump({"start": round(base, 3), "tests": table}, open(OUT, "w"), separators=(",", ":"))
bad_files = {}
for test in ids:
    if test not in good:
        bad_files[test.split("::")[0]] = bad_files.get(test.split("::")[0], 0) + 1
print("%d of %d tests verifiable; start %.2f s; cpu of all after others %.1f s; not verifiable by file: %s" % (
    len(table), len(ids), base, sum(v[0] for v in table.values()), bad_files))
