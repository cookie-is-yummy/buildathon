"""Third recorder: the whole suite in ONE process, with one coverage context per test.

Run alone, every test fills every cache itself. In a whole-suite run a later test can take
a cache-hit path that it never takes alone. This recording adds those lines. It is merged
(as a union) with the per-test recordings. Active when BR_SEQ is set; only the pytest
process itself is recorded (see br_seq_plugin.py).
"""
import os
import sys

if os.environ.get("BR_SEQ") and "pytest" in " ".join(sys.argv[:1]) or os.environ.get("BR_SEQ") and sys.argv[:1] == ["-m"]:
    import coverage

    _ROOT = os.environ.get("BR_ROOT") or os.getcwd()
    cov = coverage.Coverage(data_file=os.environ["BR_SEQ"] + ".cov", include=[_ROOT + "/*"], config_file=False)
    cov.start()
    sys._br_seq_cov = cov
