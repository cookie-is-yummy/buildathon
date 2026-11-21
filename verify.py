"""Run chosen tests of the pinned sqlglot suite on a (patched) tree and report which of
them do not pass.

The selector uses this for cheap tests it would otherwise have to hand out: a test that
executes a changed statement often still passes. Running it here costs its CPU time once;
handing it out costs at least the 2 seconds of a worker. So for small candidate sets the
selector runs them and hands out only the ones that fail.

The tests are ordinary unittest test cases (sqlglot's own `make test` runs them with
`python -m unittest`), so they are run with unittest: one process, in the order given,
set-up and tear-down of classes as unittest does it. A test counts as "not passed" if it
fails, errors, has a failing subtest, passes although expected to fail, is skipped (a
skip says nothing about what the test would do), or if its module or class cannot be
set up.

The tree is a real directory (see the selector's Tree), so that tests which start other
Python processes run the patched code there as well.

Two things are kept from growing with the machine, because this program's CPU seconds
count and its processes are limited:
- A test module's `ProcessPoolExecutor` (the optimizer tests hand every fixture case to
  one) is replaced by a stand-in that runs each call in this process, with the same
  pickling of arguments and results as a real pool. A real pool starts one worker per
  CPU of the host, and every worker imports DuckDB and pandas.
- DuckDB connections get two threads instead of one per CPU.

Stopping early. The selector passes a plan: what each candidate would cost to hand out
and to run here, what the commit costs if all candidates stay selected, and the tests
that stay selected in any case. After a failure the run checks whether going on can
still pay: if even with every remaining candidate passing, the commit would cost only
a little less than with all of them kept, less than the CPU the remaining runs need,
it stops. Candidates that were not run stay selected.

usage: verify.py <tree root> <file with one test id per line> <result file> [<plan.json>]
Writes {"not_passed": [ids], "seconds": {id: cpu of each test that ran}} to the result
file. A candidate in neither list was not run.
"""
import importlib
import json
import os
import sys
import time
import unittest


class InlinePool:
    """concurrent.futures.ProcessPoolExecutor without the processes: a submitted call is
    pickled, unpickled and run at once, and its result (or exception) goes through pickle
    like a worker's would."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **_):
        self._initializer, self._initargs, self._ready = initializer, initargs, False

    @staticmethod
    def _copy(value):
        import pickle
        from multiprocessing.reduction import ForkingPickler

        return pickle.loads(bytes(ForkingPickler.dumps(value)))

    def submit(self, fn, /, *args, **kwargs):
        from concurrent.futures import Future

        future = Future()
        future.set_running_or_notify_cancel()
        try:
            if not self._ready:
                self._ready = True
                if self._initializer is not None:
                    self._initializer(*self._copy(self._initargs))
            fn, args, kwargs = self._copy((fn, args, kwargs))
            future.set_result(self._copy(fn(*args, **kwargs)))
        except Exception as error:
            future.set_exception(error)
        return future

    def map(self, fn, *iterables, timeout=None, chunksize=1):
        futures = [self.submit(fn, *args) for args in zip(*iterables)]
        return (future.result() for future in futures)

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def tame(module):
    """See the module docstring: no worker processes, few DuckDB threads."""
    if hasattr(module, "ProcessPoolExecutor"):
        module.ProcessPoolExecutor = InlinePool
    duckdb = sys.modules.get("duckdb")
    if duckdb is not None and not hasattr(duckdb, "_blast_connect"):
        duckdb._blast_connect = real = duckdb.connect

        def connect(*args, **kwargs):
            config = dict(kwargs.pop("config", None) or {})
            config.setdefault("threads", 2)
            return real(*args, config=config, **kwargs)

        duckdb.connect = connect


class Result(unittest.TestResult):
    def __init__(self, check=None):
        super().__init__()
        self.bad = set()      # unittest ids of tests that did not pass
        self.broken = set()   # classes / modules whose set-up or tear-down failed
        self.started = {}
        self.seconds = {}
        self.check = check    # called after a test that did not pass; True: stop here

    @staticmethod
    def _owner(test):
        return getattr(test, "test_case", test)  # a subtest reports for its test

    def startTest(self, test):
        super().startTest(test)
        self.started[test.id()] = time.process_time()

    def stopTest(self, test):
        super().stopTest(test)
        self.seconds[test.id()] = time.process_time() - self.started.pop(test.id(), time.process_time())
        if self.check is not None and test.id() in self.bad and self.check(self):
            self.stop()

    def _mark(self, test):
        test = self._owner(test)
        if isinstance(test, unittest.TestCase):
            self.bad.add(test.id())
        else:
            # unittest's stand-in for a failed setUpClass / setUpModule / tearDown...
            self.broken.add(str(test))

    def addError(self, test, err):
        super().addError(test, err)
        self._mark(test)

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._mark(test)

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self._mark(test)

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._mark(test)

    def addSkip(self, test, reason):
        # A test that is skipped here has told us nothing about what it does elsewhere.
        super().addSkip(test, reason)
        self._mark(test)


def stopper(plan, cases):
    """The early-stop rule (see the module docstring) as a function of the result so far."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from selector import WORKER_COST_MS, split

    duration = {index: milliseconds for index, milliseconds in enumerate(plan["rest"])}
    rest = list(duration)
    number = {}
    for test_id, case in cases:
        number[case.id()] = len(duration)
        duration[number[case.id()]] = plan["duration"][test_id]
    cpu = {case.id(): plan["cpu"][test_id] for test_id, case in cases}
    state = {"checked": 0}

    def cost(numbers):
        workers = split(numbers, duration)
        return (max(sum(duration[n] for n in w) for w in workers) + WORKER_COST_MS * len(workers)) / 1000.0 if workers else 0.0

    def check(result):
        bad = [name for name in result.bad if name in number]
        if len(bad) < state["checked"] * 1.2:  # not at every single failure of a long run
            return False
        state["checked"] = len(bad)
        remaining = sum(seconds for name, seconds in cpu.items() if name not in result.seconds)
        best = cost(rest + [number[name] for name in bad])
        return plan["worth"] * (plan["all"] - best) < remaining

    return check


def main(argv):
    root, wanted, out = os.path.abspath(argv[1]), argv[2], argv[3]
    plan = json.load(open(argv[4])) if len(argv) > 4 else None
    os.chdir(root)
    sys.path.insert(0, root)
    sys.dont_write_bytecode = True
    ids = [line.strip() for line in open(wanted) if line.strip()]
    not_passed = set()
    cases = []  # (test id, TestCase)
    for test_id in ids:
        try:
            path, klass, method = test_id.split("::")
            module = importlib.import_module(path[:-3].replace("/", "."))
            tame(module)
            cases.append((test_id, getattr(module, klass)(method)))
        except BaseException:
            not_passed.add(test_id)  # cannot even be loaded
    result = Result(stopper(plan, cases) if plan is not None else None)
    suite = unittest.TestSuite(case for _, case in cases)
    try:
        suite.run(result)
    except BaseException:
        not_passed.update(test_id for test_id, _ in cases)
    if result.broken:
        # we do not try to tell which tests a broken class or module takes with it
        not_passed.update(test_id for test_id, _ in cases)
    seconds = {}
    for test_id, case in cases:
        if case.id() in result.bad:
            not_passed.add(test_id)
        if case.id() in result.seconds:
            seconds[test_id] = round(result.seconds[case.id()], 4)
    with open(out, "w") as handle:
        json.dump({"not_passed": sorted(not_passed), "seconds": seconds}, handle)


if __name__ == "__main__":
    main(sys.argv)
    sys.stdout.flush()
    os._exit(0)  # do not wait for anything a test left behind
