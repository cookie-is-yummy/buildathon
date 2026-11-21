#!/bin/bash
# Comparison recordings (see site_cmp/sitecustomize.py). Inside the lab container:
# /src (pinned checkout, ro), /tools (ro), /out (rw), /data/tests.json.
# Every test alone in a fresh process tree, then the whole suite in one process in normal
# and reversed order. Output: /out/cmp/<n>.<pid>.json, <n>.rc, forward.json, reverse.json
cp -r /src /work && cd /work
export PYTHONPATH=/work:/tools/site_cmp SKIP_INTEGRATION=0 PYTHONDONTWRITEBYTECODE=1 BR_ROOT=/work
mkdir -p /out/cmp
python - <<'PY' > /tmp/list.txt
import json
for t in json.load(open('/data/tests.json'))['tests']:
    print(t['number'], t['id'])
PY
one() {
  [ -f /out/cmp/$1.rc ] && return
  BR_CMP=/out/cmp/$1 python -m pytest -p no:cacheprovider -q "$2" > /out/cmp/$1.log 2>&1
  echo $? > /out/cmp/$1.rc
}
export -f one
if [ -z "$CMP_SKIP_ISO" ]; then cat /tmp/list.txt | xargs -P ${ISO_PAR:-4} -L 1 bash -c 'one "$0" "$1"'; fi
(BR_CMP_SEQ=/out/cmp/forward python -m pytest tests -p no:cacheprovider -p br_cmp_plugin -q > /out/cmp/forward.log 2>&1; echo $? > /out/cmp/forward.rc) &
(BR_CMP_SEQ=/out/cmp/reverse BR_SEQ_REVERSE=1 python -m pytest tests -p no:cacheprovider -p br_cmp_plugin -q > /out/cmp/reverse.log 2>&1; echo $? > /out/cmp/reverse.rc) &
wait
echo CMP_DONE
