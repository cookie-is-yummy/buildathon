#!/bin/bash
# Each test alone, with the typed-dictionary recorder (tools/site_typed).
# Inside the lab container: /src pinned checkout, /tools, /data/tests.json, /out.
# Output: /out/typed/<n>.<pid>.json, <n>.rc, <n>.log
cp -r /src /work && cd /work && rm -rf .git
export PYTHONPATH=/work:/tools/site_typed SKIP_INTEGRATION=0 PYTHONDONTWRITEBYTECODE=1 BR_ROOT=/work
mkdir -p /out/typed
python - <<'PY' > /tmp/list.txt
import json
for t in json.load(open('/data/tests.json'))['tests']:
    print(t['number'], t['id'])
PY
one() {
  BR_TYPED=/out/typed/$1 python -m pytest -p no:cacheprovider -q "$2" > /out/typed/$1.log 2>&1
  echo $? > /out/typed/$1.rc
}
export -f one
cat /tmp/list.txt | xargs -P ${ISO_PAR:-4} -L 1 bash -c 'one "$0" "$1"'
echo TYPED_DONE
