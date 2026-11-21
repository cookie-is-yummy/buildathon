# Blast Radius entry

An entry for the RWX "Blast Radius" track. For each patch to
[sqlglot](https://github.com/tobymao/sqlglot) at commit
`eb11aec4be8cb2122afe713a4816662c3f0fb2fa` it prints which tests to run and how to split
them over workers.

**How it decides is explained in plain language in [NOTES.md](NOTES.md).** In one
paragraph: the selector rebuilds the patched file from the pinned source, compares the
syntax trees to find exactly which statements changed, and asks maps recorded at the
pinned commit which tests execute those statements, import that module or open that file.
When it cannot reason about a change, it runs every test. For the slowest integration
test it goes one step further: it repeats that test's sqlglot calls on the patched code
(`replay.py`) and keeps the test unless the outcome is identical to the pinned one. And
when the remaining candidates are cheap, it runs them on the patched code in the image
(`verify.py`) and prints only the ones that do not pass.

There is no table from patches, or from kinds of edit, to tests. The maps describe the
pinned commit only: which test executes which line, imports which module, opens which
file, reads which class attribute, and what it observed when it evaluated a comparison
(how the two operands related) or an `and`/`or` expression (which operands it reached and
which were true). They were recorded by running the unmodified test suite and are the
same whatever the patches are.

## Layout

| Path | What it is |
| --- | --- |
| `selector.py`, `run.py` | The program the image runs. Standard library only. |
| `replay.py` | Started by the selector for some commits: repeats the sqlglot calls of the slow executor test on the patched code and compares fingerprints of what they return. Runs the pinned sqlglot. |
| `maps/` | Recordings of the pinned commit, as plain JSON (see `tools/build_maps.py`). |
| `verify.py` | Started by the selector for some commits: runs candidate tests of the pinned suite with unittest on a patched copy of the tree. |
| `pinned.tar.xz` | Unmodified files of sqlglot at the pinned commit, packed to keep the repository small: its Python files and two config files (the selector needs their text to rebuild a patched file) and the test fixtures except the TPC-H tables (`replay.py` and `verify.py` read them). The image unpacks it to `pinned/`; to work locally, `tar -xJf pinned.tar.xz`. |
| `tools/` | How the maps were made, and the local evaluation harness. Not used at run time. |
| `Dockerfile`, `Makefile`, `score.py`, `SCORING.md`, `sample/` | From the organisers' starter kit. |

## Rebuilding the maps

```sh
git clone https://github.com/tobymao/sqlglot && git -C sqlglot checkout eb11aec4be8cb2122afe713a4816662c3f0fb2fa
docker build -t br-lab -f tools/lab.Dockerfile .
RUN="docker run --rm --network none -v $PWD/sqlglot:/src:ro -v $PWD/tools:/tools:ro -v $PWD/rec:/out -v $PWD/sample/tests.json:/data/tests.json:ro br-lab"
$RUN /tools/record_all.sh      # every test alone: lines, started functions, files
$RUN /tools/record_suite.sh    # whole suite in one process, normal and reversed order
$RUN /tools/record_attrs.sh    # every test alone: class attributes read
$RUN /tools/record_cmp.sh      # comparisons and and/or: what each test observed (run it twice, into rec/ and rec2/)
python3 tools/build_maps.py sqlglot sample/tests.json . rec/iso2 rec/seq/forward.json rec/seq/reverse.json
python3 tools/build_attr_maps.py sqlglot sample/tests.json . rec/attrs
python3 tools/build_cmp_maps.py sample/tests.json . rec/cmp rec2/cmp
$RUN /tools/record_phase.sh    # the slow executor test: which lines run inside its three sqlglot calls
make build                     # the entry image; the next step runs inside it
docker run --rm --network none -v $PWD/tools:/tools:ro -v $PWD/rec:/out blast-radius-entry \
    python -B -s /tools/record_replay.py /app/pinned /out/replay.json
python3 tools/build_replay_maps.py rec/phase rec/replay.json .
docker run --rm --network none -v $PWD/tools:/tools:ro -v $PWD/rec:/out -v $PWD/sample/tests.json:/data/tests.json:ro \
    blast-radius-entry python -B -s /tools/record_verify.py /app/pinned /data/tests.json /out/verify.json
cp rec/verify.json maps/verify.json
make build                     # again, now with maps/replay.json and maps/verify.json
```

`build_maps.py` writes the pinned sources to `pinned/`; the tracked archive is made from it
with `tar --sort=name --exclude=__pycache__ -cJf pinned.tar.xz pinned` (after copying
`tests/fixtures/` from the checkout into `pinned/`, without `optimizer/tpc-h/*.csv.gz`).

The recordings run each test under the labelling image and package versions
(`requirements.lock`), with a recorder loaded into every Python process the test starts.
See [tools/README.md](tools/README.md).

## Try it

```sh
make sample                                   # build, run under the scorer's limits, score
python3 tools/evaluate.py sample sample/answers.json   # same without Docker, with a per-kind table
```

## Open-source code used

- **sqlglot** (MIT, © Toby Mao). `pinned.tar.xz` holds unmodified copies of its Python
  source files, `pyproject.toml`, `setup.cfg` and its test fixtures (without the TPC-H
  tables) at the pinned commit, with its licence in `pinned/LICENSE`. The maps in `maps/` were recorded by running its test suite.
- Installed into the image from PyPI, at the versions of `requirements.lock`:
  **python-dateutil** 4.0.0 (Apache 2.0 / BSD) and **mypy_extensions** 1.2.0 (MIT), which
  sqlglot imports when they are present, as they are where the tests run; and **pytz**
  2026.6 (MIT), **tzdata** 2026.6 (Apache 2.0), **numpy** 2.5.5 (BSD), **pandas** 3.1.1
  (BSD) and **duckdb** 2.0.0 (MIT), which sqlglot's test files import. `replay.py` and
  `verify.py` run the pinned sqlglot and its tests; the selector itself uses none of them.
- **coverage.py** (Apache 2.0, © Ned Batchelder and contributors) recorded the executed
  lines. It is used only by `tools/`, never in the image.
- **pytest** (MIT) ran the tests while recording. Used only by `tools/`.
- The starter kit files (`score.py`, `SCORING.md`, `Makefile`, `sample/`,
  `requirements.lock`) come from the organisers.

`selector.py` uses only the Python standard library.

## Licence

MIT, see [LICENSE](LICENSE).
