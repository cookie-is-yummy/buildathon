# tools/

Nothing in this directory runs in the image. It is how the maps were made and how the
selector was checked. **No patches, ground truth or other evaluation data are stored in
this repository**, and the selector reads none.

## Making the maps (from the pinned commit only)

| File | What it does |
| --- | --- |
| `lab.Dockerfile` | The organisers' image plus `requirements.lock` and coverage.py. |
| `site/sitecustomize.py`, `record_all.sh` | Recorder loaded into every Python process a test starts: executed lines, started functions, imported/opened/listed files. One fresh process tree per test. |
| `site_seq/`, `record_suite.sh` | The whole suite in one process (normal and reversed order) with one coverage context per test. |
| `site_attrs/sitecustomize.py`, `record_attrs.sh` | Which tests read which class attribute. |
| `build_maps.py`, `build_attr_maps.py` | Turn the recordings into `maps/` and copy the pinned sources into `pinned/`. |

## Checking the selector

| File | What it does |
| --- | --- |
| `evaluate.py` | Runs the selector on a data directory and scores it with the kit's `score.py`, per kind. |
| `gen_patches.py` | Generates synthetic patches of many kinds against a pinned checkout. |
| `gen_special.py` | Hand-written patches of kinds the random generator does not make (test methods added or removed, shared helpers, new directories, binary fixtures, renames, file modes). |
| `gt_loop.sh`, `gt_plugin.py`, `gt_answers.py` | Ground truth: runs the full sqlglot suite on each synthetic patch. |
| `fast_gt.py`, `fast_eval.py` | Faster ground truth for modules few tests import: only the importers are run. |
| `difftest.py` | Checks that parsing only the touched statement gives the same analysis as parsing the whole file. |
| `check_apply.py` | Checks the selector's patch applier against `git apply`. |

The evaluation scripts expect a scratch directory (default `/home/user/br-work`, or
`$BR_WORK`) holding a clone of sqlglot at the pinned commit.
