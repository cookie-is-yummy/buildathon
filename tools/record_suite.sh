#!/bin/bash
# Whole-suite single-process recordings: default order and reversed order.
cp -r /src /work && cd /work && rm -rf .git
export PYTHONPATH=/work:/tools/site_seq SKIP_INTEGRATION=0 PYTHONDONTWRITEBYTECODE=1 BR_ROOT=/work BR_TESTS=/data/tests.json
mkdir -p /out/seq
(BR_SEQ=/out/seq/forward python -m pytest tests -p no:cacheprovider -p br_seq_plugin -q > /out/seq/forward.log 2>&1; echo $? > /out/seq/forward.rc) &
(BR_SEQ=/out/seq/reverse BR_SEQ_REVERSE=1 python -m pytest tests -p no:cacheprovider -p br_seq_plugin -q > /out/seq/reverse.log 2>&1; echo $? > /out/seq/reverse.rc) &
wait
echo SEQ_DONE
