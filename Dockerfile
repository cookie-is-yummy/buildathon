# The scorer builds this on linux/amd64 and runs it with no network.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
# The selector itself uses only the standard library. replay.py and verify.py run the
# pinned sqlglot and some of its tests; these are the third-party packages that sqlglot
# (the first two) and its test files import, at the versions of requirements.lock.
RUN pip install --no-cache-dir --no-deps python-dateutil==4.0.0 mypy_extensions==1.2.0 pytz==2026.6 \
    tzdata==2026.6 numpy==2.5.5 pandas==3.1.1 duckdb==2.0.0 || true
COPY selector.py run.py replay.py verify.py /app/
COPY maps /app/maps
# pinned.tar.xz: sqlglot at the pinned commit (see README); ADD unpacks it to /app/pinned
ADD pinned.tar.xz /app/
# Bytecode for the selector and the pinned sources, then the fingerprints replay.py
# compares against (computed from the pinned sources only). If that step cannot run, the
# file is not written and the selector simply does without it.
RUN chmod -R a+rX /app \
 && python -m compileall -q --invalidation-mode unchecked-hash /app/selector.py /app/replay.py /app/verify.py \
 && python -m compileall -q /app/pinned/sqlglot /app/pinned/tests \
 && (python -B -s /app/replay.py baseline /app/pinned /app/baseline.json || rm -f /app/baseline.json) \
 && (python -B -s -c "import pytz, numpy, pandas, duckdb" && touch /app/tests-can-run || true) \
 && chmod -R a+rX /app
# -I: ignore the environment and user site; -S: skip site-packages (stdlib only)
CMD ["python", "-I", "-S", "/app/run.py"]
