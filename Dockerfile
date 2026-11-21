# The scorer builds this on linux/amd64 and runs it with no network.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
# The selector itself uses only the standard library. replay.py runs parts of the pinned
# sqlglot; these are the two third-party packages sqlglot imports when they are there (as
# they are where the tests run), at the versions of requirements.lock.
RUN pip install --no-cache-dir --no-deps python-dateutil==4.0.0 mypy_extensions==1.2.0 || true
COPY selector.py run.py replay.py /app/
COPY maps /app/maps
COPY pinned /app/pinned
# Bytecode for the selector and the pinned sources, then the fingerprints replay.py
# compares against (computed from the pinned sources only). If that step cannot run, the
# file is not written and the selector simply does without it.
RUN chmod -R a+rX /app \
 && python -m compileall -q --invalidation-mode unchecked-hash /app/selector.py /app/replay.py \
 && python -m compileall -q /app/pinned/sqlglot /app/pinned/tests/__init__.py /app/pinned/tests/helpers.py \
 && (python -B -s /app/replay.py baseline /app/pinned /app/baseline.json || rm -f /app/baseline.json) \
 && chmod -R a+rX /app
# -I: ignore the environment and user site; -S: skip site-packages (stdlib only)
CMD ["python", "-I", "-S", "/app/run.py"]
