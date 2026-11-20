#!/bin/bash
# Like record_all.sh but with the class-attribute recorder. Output: /out/attrs/<n>.<pid>.json, <n>.rc
cp -r /src /work && cd /work && rm -rf .git
export PYTHONPATH=/work:/tools/site_attrs SKIP_INTEGRATION=0 PYTHONDONTWRITEBYTECODE=1 BR_ROOT=/work
mkdir -p /out/attrs
python - <<'PY' > /tmp/list.txt
import json
for t in json.load(open('/data/tests.json'))['tests']:
    print(t['number'], t['id'])
PY
one() {
  BR_ATTR=/out/attrs/$1 python -m pytest -p no:cacheprovider -q "$2" > /out/attrs/$1.log 2>&1
  echo $? > /out/attrs/$1.rc
}
export -f one
cat /tmp/list.txt | xargs -P ${ISO_PAR:-4} -L 1 bash -c 'one "$0" "$1"'
echo ATTRS_DONE
