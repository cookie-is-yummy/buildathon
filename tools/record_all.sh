#!/bin/bash
# Inside the lab container: /src (pinned checkout, ro), /tools (ro), /out (rw), /data/tests.json.
# Runs every test alone in a fresh process tree with the recorder active.
cp -r /src /work && cd /work && rm -rf .git
export PYTHONPATH=/work:/tools/site SKIP_INTEGRATION=0 PYTHONDONTWRITEBYTECODE=1 BR_ROOT=/work
mkdir -p /out/iso2
python - <<'PY' > /tmp/list.txt
import json
for t in json.load(open('/data/tests.json'))['tests']:
    print(t['number'], t['id'])
PY
one() {
  BR_REC=/out/iso2/$1 python -m pytest -p no:cacheprovider -q "$2" > /out/iso2/$1.log 2>&1
  echo $? > /out/iso2/$1.rc
}
export -f one
cat /tmp/list.txt | xargs -P ${ISO_PAR:-4} -L 1 bash -c 'one "$0" "$1"'
echo ISO_DONE
