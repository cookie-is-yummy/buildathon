"""pytest plugin: record failing node ids (failure/error/timeout/subtest failure/collection error)."""
import json, os
_failed = set(); _collect_failed = set(); _passed = set()
def pytest_runtest_logreport(report):
    if report.failed:
        _failed.add(report.nodeid)
    elif report.when == "call" and report.passed:
        _passed.add(report.nodeid)
def pytest_collectreport(report):
    if report.failed:
        _collect_failed.add(report.nodeid)
def pytest_sessionfinish(session, exitstatus):
    out = os.environ.get("GT_OUT")
    if out and not hasattr(session.config, "workerinput"):
        json.dump({"failed": sorted(_failed), "collect_failed": sorted(_collect_failed),
                   "passed": sorted(_passed - _failed), "exit": int(exitstatus)}, open(out, "w"))
