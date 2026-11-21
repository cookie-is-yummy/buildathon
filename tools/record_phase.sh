#!/bin/bash
# Inside the lab container: /src (pinned checkout, ro), /tools (ro), /out (rw).
# Runs test_execute_tpcds alone, several times, with the phase recorder.
cp -r /src /work && cd /work && rm -rf .git
export PYTHONPATH=/work:/tools/site_replay SKIP_INTEGRATION=0 PYTHONDONTWRITEBYTECODE=1 BR_ROOT=/work
mkdir -p /out/phase
for run in 1 2 3; do
  BR_PHASE=/out/phase/tpcds.$run python -m pytest -p no:cacheprovider -p phase_plugin -q \
    "tests/test_executor.py::TestExecutor::test_execute_tpcds" > /out/phase/tpcds.$run.log 2>&1
  echo $? > /out/phase/tpcds.$run.rc
done
echo PHASE_DONE
