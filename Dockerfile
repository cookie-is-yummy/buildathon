# The scorer builds this on linux/amd64 and runs it with no network.
# The image needs nothing beyond the Python standard library: the maps and the pinned
# sqlglot sources they were recorded from are plain files in this repository.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
COPY selector.py run.py /app/
COPY maps /app/maps
COPY pinned /app/pinned
RUN chmod -R a+rX /app && python -m compileall -q --invalidation-mode unchecked-hash /app/selector.py
# -I: ignore the environment and user site; -S: skip site-packages (stdlib only)
CMD ["python", "-I", "-S", "/app/run.py"]
