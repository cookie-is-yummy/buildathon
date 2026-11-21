#!/bin/bash
# usage: gt_loop.sh <syn dir>   (host side) -- ground truth for every patch lacking a result
W=/home/user/br-work; SYN=$1; mkdir -p $SYN/gt
G=${GT_REPO:-sqlglot_gt}; [ -d $W/$G ] || cp -r $W/sqlglot $W/$G
for p in $SYN/commits/*.patch; do
  id=$(basename $p .patch); [ -f $SYN/gt/$id.json ] && continue
  git -C $W/$G reset -q --hard HEAD; git -C $W/$G clean -fdq
  if ! git -C $W/$G apply $p 2> $SYN/gt/$id.apply; then echo "APPLYFAIL $id"; echo '{"applyfail": true}' > $SYN/gt/$id.json; continue; fi
  docker run --rm --cpus ${GT_CPUS:-4} --network none -v $W/$G:/src:ro -v $W/tools:/tools:ro -v $SYN/gt:/out br-lab bash -c "cp -r /src /work && cd /work && rm -rf .git && PYTHONPATH=/work:/tools SKIP_INTEGRATION=0 GT_OUT=/out/$id.json timeout 1200 python -m pytest tests -p no:cacheprovider -p gt_plugin -q -n ${GT_CPUS:-4} --timeout=240 > /out/$id.log 2>&1; [ -f /out/$id.json ] || echo '{\"crash\": true}' > /out/$id.json"
  echo "done $id $(tail -1 $SYN/gt/$id.log | cut -c1-100)"
done
git -C $W/$G reset -q --hard HEAD; git -C $W/$G clean -fdq
echo GT_LOOP_DONE
