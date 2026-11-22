#!/bin/bash
# Each test alone, with the argument-table recorder (tools/site_args).
# Inside the lab container: /src pinned checkout, /tools, /data/tests.json, /out.
# Output: /out/args/<n>.<pid>.json, <n>.rc, <n>.log
cp -r /src /work && cd /work && rm -rf .git
export PYTHONPATH=/work:/tools/site_args SKIP_INTEGRATION=0 PYTHONDONTWRITEBYTECODE=1 BR_ROOT=/work
mkdir -p /out/args
python - <<'PY' > /tmp/list.txt
import json
for t in json.load(open('/data/tests.json'))['tests']:
    print(t['number'], t['id'])
PY
one() {
  BR_ARGS=/out/args/$1 python -m pytest -p no:cacheprovider -q "$2" > /out/args/$1.log 2>&1
  echo $? > /out/args/$1.rc
}
export -f one
cat /tmp/list.txt | xargs -P ${ISO_PAR:-4} -L 1 bash -c 'one "$0" "$1"'
echo ARGS_DONE
