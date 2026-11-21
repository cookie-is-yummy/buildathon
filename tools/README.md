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
| `site_attrs/sitecustomize.py`, `record_attrs.sh` | Which tests read which class attribute, and which entries of class-level dictionaries they look up. |
| `site_cmp/`, `record_cmp.sh` | What each test observes when it evaluates a comparison (how the operands relate) or an `and`/`or` (which operands were evaluated and true). Every test alone, plus the whole suite in both orders. |
| `site_replay/`, `record_phase.sh` | For the slow executor test: which lines it executes inside its three sqlglot calls and which outside (all processes). |
| `record_verify.py` | Run inside the entry image: which tests pass there at the pinned commit (alone and with their file) and their CPU seconds, for `verify.py`. |
| `record_replay.py` | Run inside the entry image: which lines each TPC-DS query executes when `replay.py` repeats those calls, alone and after other queries, and the CPU each query takes. |
| `build_maps.py`, `build_attr_maps.py`, `build_key_maps.py`, `build_cmp_maps.py`, `build_replay_maps.py` | Turn the recordings into `maps/` and copy the pinned sources into `pinned/`. |

## Checking the selector

| File | What it does |
| --- | --- |
| `evaluate.py` | Runs the selector on a data directory and scores it with the kit's `score.py`, per kind. |
| `gen_patches.py` | Generates synthetic patches of many kinds against a pinned checkout. |
| `gen_special.py` | Hand-written patches of kinds the random generator does not make (test methods added or removed, shared helpers, new directories, binary fixtures, renames, file modes). |
| `gt_loop.sh`, `gt_plugin.py`, `gt_answers.py` | Ground truth: runs the full sqlglot suite on each synthetic patch. |
| `fast_gt.py`, `fast_eval.py` | Faster ground truth for modules few tests import: only the importers are run. |
| `gen_swaps.py`, `swap_gt.py` | For the comparison and `and`/`or` rules: makes single-operator patches, and runs exactly the tests the rule leaves out on each patched tree. Every one must pass. |
| `gen_keys.py` | For the per-entry map of class-level dictionaries: replaces an entry's value by something unusable and lists the tests the map leaves out; `swap_gt.py` runs them. Every one must pass. |
| `difftest.py` | Checks that parsing only the touched statement gives the same analysis as parsing the whole file. |
| `check_apply.py` | Checks the selector's patch applier against `git apply`. |

The evaluation scripts expect a scratch directory (default `/home/user/br-work`, or
`$BR_WORK`) holding a clone of sqlglot at the pinned commit.
