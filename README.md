# Blast Radius entry

An entry for the RWX "Blast Radius" track. For each patch to
[sqlglot](https://github.com/tobymao/sqlglot) at commit
`eb11aec4be8cb2122afe713a4816662c3f0fb2fa` it prints which tests to run and how to split
them over workers.

**How it decides is explained in plain language in [NOTES.md](NOTES.md).** In one
paragraph: the selector rebuilds the patched file from the pinned source, compares the
syntax trees to find exactly which statements changed, and asks maps recorded at the
pinned commit which tests execute those statements, import that module or open that file.
When it cannot reason about a change, it runs every test.

There is no table from patches, or from kinds of edit, to tests. The maps describe the
pinned commit only: which test executes which line, imports which module, opens which
file. They were recorded before any patch was looked at and are the same whatever the
patches are.

## Layout

| Path | What it is |
| --- | --- |
| `selector.py`, `run.py` | The program the image runs. Standard library only. |
| `maps/` | Recordings of the pinned commit, as plain JSON (see `tools/build_maps.py`). |
| `pinned/` | The Python files (and two config files) of sqlglot at the pinned commit. The selector needs their text to rebuild a patched file. |
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
python3 tools/build_maps.py sqlglot sample/tests.json . rec/iso2 rec/seq/forward.json rec/seq/reverse.json
python3 tools/build_attr_maps.py sqlglot sample/tests.json . rec/attrs
```

The recordings run each test under the labelling image and package versions
(`requirements.lock`), with a recorder loaded into every Python process the test starts.
See [tools/README.md](tools/README.md).

## Try it

```sh
make sample                                   # build, run under the scorer's limits, score
python3 tools/evaluate.py sample sample/answers.json   # same without Docker, with a per-kind table
```

## Open-source code used

- **sqlglot** (MIT, © Toby Mao). `pinned/` holds unmodified copies of its Python source
  files, `pyproject.toml` and `setup.cfg` at the pinned commit, with its licence in
  `pinned/LICENSE`. The maps in `maps/` were recorded by running its test suite.
- **coverage.py** (Apache 2.0, © Ned Batchelder and contributors) recorded the executed
  lines. It is used only by `tools/`, never in the image.
- **pytest** (MIT) ran the tests while recording. Used only by `tools/`.
- The starter kit files (`score.py`, `SCORING.md`, `Makefile`, `sample/`,
  `requirements.lock`) come from the organisers.

The image itself uses only the Python standard library.

## Licence

MIT, see [LICENSE](LICENSE).
