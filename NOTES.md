# How this entry works

Plain-language notes for explaining the entry. Kept current with the code.

## The problem in one paragraph

Each commit is one patch to sqlglot at a fixed commit. We must name the tests to run and
split them over at most 8 workers. A commit costs the load of its busiest worker plus 2 s
per worker; a commit where we left out a failing test costs 300 s more; our own CPU time
is added. Running everything costs about 36.8 s per commit (the slowest test alone is
28.8 s, so 4 workers is the best split). One miss therefore costs as much as running
everything on 8 commits. **So the rule is: never leave out a test unless there is a sound
reason it cannot fail, and when unsure, run everything.**

## The idea

A test can only change its result if the patch changes something the test *uses*. So
before the competition we recorded, at the pinned commit, what every test uses:

- which **lines** of which Python files it executes,
- which **functions, lambdas and class bodies** it starts running,
- which modules it **imports**,
- which files it **opens** and which directories it **lists**,
- which modules it **tries to import and does not find**.

These recordings ("maps") depend only on the pinned commit. They are the same whatever
patches arrive. At run time the selector works out *what a patch changes* and looks up
*who uses that*.

## How the maps were recorded (tools/)

Every one of the 1620 tests was run **alone, in a fresh process**, in the organisers'
image with the package versions from `requirements.lock` and `SKIP_INTEGRATION=0`.

Two details matter and are easy to get wrong:

1. **Worker processes.** `tests/test_optimizer.py` and `tests/test_executor.py` hand most
   of their work to a pool of *spawned* Python processes, and one dialect test starts
   `python -c ...`. A recorder inside the test process alone sees almost none of the
   optimizer code those tests run. Our recorder is a `sitecustomize.py` that loads into
   **every** Python process started while the test runs, and each process writes its own
   recording; they are merged per test.
2. **Import time counts.** The recording starts before pytest imports anything, so code
   that runs when a module is imported (class bodies, module constants, the `def` lines
   themselves) is recorded for every test that imports that module. sqlglot loads its
   dialect modules lazily, so "who imports `generators/sqlite.py`" is a real, narrow set.

Why alone and not in one big run: in one big run the first test to need something fills a
cache and later tests reuse it without executing the code. Each of those later tests still
depends on that code. Alone, every test executes it itself and the dependency is visible.

The opposite also happens: in a big run a later test can take a "cache hit" path that it
never takes alone. So the whole suite was also recorded in one process, twice (normal and
reversed order), with a marker per test, and the maps hold the **union** of everything.
(It added only a few dozen line entries. In reversed order four tests fail at the pinned
commit because they depend on state left by earlier tests; that told us the suite is not
fully order-independent, which is one of the remaining risks listed below.)

Opened files and listed directories come from Python's audit hook (`sys.addaudithook`),
which reports every `open()`, `os.listdir()`, `os.scandir()`, `glob`. Each event is
classed by what is on the call stack: the import system, repository code (sqlglot or the
tests), or neither (pytest reading its own configuration).

## What the selector does with a patch (selector.py)

1. **Parse the patch** (a git unified diff, possibly several files; additions, deletions,
   renames, mode changes, binary files). Anything it does not recognise: run everything.
2. For each changed file, decide which tests can notice:

### A Python file was modified

Rebuild the new file by applying the hunks to the pinned text (if the context lines do not
match the pinned text: run everything). Then:

- **The new file does not compile** (syntax error, bad indentation, `return` outside a
  function, null bytes): every test that imports the module, directly or through other
  modules, fails at import. Select all of them.
- **The syntax trees are identical** (only comments, blank lines or formatting changed):
  nothing can behave differently, *except* for a test that reads the file's text. Select
  the tests during which repository code opened this file as data (there are a few, e.g.
  the test that runs `sqlglotc/setup.py`). A changed encoding declaration is treated as
  unknown: run everything.
- **Otherwise** walk the old and new trees side by side, top down, and find the smallest
  statements that differ:
  - *A statement inside a function body changed* (same number and kinds of statements
    around it). Only a test that **executes that statement** can notice. Look up the
    statement's lines in the line map. For an `if`/`for`/`while`/`with`/`try` whose
    header changed, the header lines are used.
    We use the whole statement, not just the edited line, because an edit such as
    `and` -> `or` changes which later parts of the same condition get evaluated.
  - *Statements were added, removed or replaced inside a function.* The old and new
    blocks are lined up on their unchanged statements. A test's run can first differ only
    where it meets a removed or replaced statement (it executed that statement before) or
    where it reaches an inserted one (it then went on to execute the next old statement).
    So the removed statements' lines and the neighbours of insertions are looked up.
  - *The edit changes the function as a whole*: a name that becomes local, a `yield`
    appearing (the function becomes a generator), `global` declarations. Then any path
    through the function may change: select every test that **started running** that
    function or anything defined inside it.
  - *A `def` or `class` header changed* (name, parameters, decorators, base classes): the
    header runs at import, so everyone who imports the module is selected, plus everyone
    who ran the function. (Constant defaults are the exception, see below.)
  - *A statement at module or class level changed* (a constant, a class attribute, a
    mapping table). It runs at import time, and since the recordings include import time,
    its lines list exactly the tests that import the module. For core modules that is
    every test; for a dialect module it is the tests that use that dialect.
    One refinement: if the only difference is inside the **body of a lambda** in such a
    statement (sqlglot has large class-level tables of lambdas), building the table is
    unchanged and only tests that **call that lambda** are selected.
  - *Statements were added or removed at module or class level*: every test importing the
    module.
  - Code that no test executes (for example under `if TYPE_CHECKING:`) selects nothing.

### Class attributes (the big tables)

A lot of sqlglot is data in class bodies: `arg_types` of ~1000 expression classes, and the
`TRANSFORMS`, `FUNCTIONS`, `TYPE_MAPPING`, `KEYWORDS` tables and feature flags of every
dialect. A change there runs at import, so the line map can only say "everyone who imports
the module", which for core modules is every test.

So a second recording asks a sharper question: **which tests read which class attribute?**
During that recording every data attribute in a sqlglot class body is replaced by a small
descriptor object that notes each read and returns the real value. Reading the attribute
through an instance, the class or a subclass all go through the descriptor of the class
that *defines* it, which is exactly the dependency we need.

Two complications, both handled when the map is built:

- **Values computed from attributes.** When a class is created, `__init_subclass__` hooks
  and metaclasses compute more attributes (`required_args` from `arg_types`; tries and
  inverse mappings for dialects). The recorder watches class creation: a computed
  attribute depends on every attribute read before it was set. Tests reading the computed
  attribute are added to the readers of what it was computed from. For classes with one of
  sqlglot's own metaclasses the recorder cannot see reads of the class's own body, so
  every computed attribute of such a class is assumed to depend on every body attribute.
- **Later statements in the same class body** that use the name (`B = {**A, ...}`):
  readers of `B` are added to readers of `A`. If the name is used at class-creation time in
  any way we cannot follow, the attribute is left out of the map.

The selector uses this map only when the edit **cannot make the class body fail at
import**: a constant replaced by a constant of the same type inside a literal, or (when no
creation hook reads the attribute) entries removed from a literal. Anything else, for
example a changed argument of a call such as `re.compile(...)`, could raise while the
module is imported and fail every importer, so it falls back to all importers.

### Tests that inspect code instead of running it

`tests/test_typing.py` reads the source of every `sqlglot/typing/*.py` module and compares
the *bytecode* of the functions they register, without calling them. Line coverage cannot
see that. The recording of opened files can: the test opens those files as data, so it is
selected for any change to them (this is also why a comment in those files still selects
one test: we cannot know the reader ignores comments). It compares against functions in
the package's `__init__.py` too, which it never opens, so the rule is: **a test that opens
some module of a package directory as data is selected for any code change in that
directory.** A search of the pinned tests found no other test that inspects code objects.

### Function defaults

If only constant default values of a function's parameters change, the header still runs
at import but cannot fail, and a default is only looked at when the function is called:
select the tests that started the function.

### Docstrings

pytest does not collect sqlglot's doctests (`tests/test_docs.py` uses unittest's
`load_tests`, which pytest ignores; it contributes no test to `tests.json`). A docstring
inside a function is a statement no test executes, so it selects nothing. For class and
module docstrings the map build scans the pinned tree for code that tests execute and that
touches `__doc__`: the only one copies a class's docstring back onto itself. So a
docstring whose text changes (non-empty before and after) selects nothing. Adding or
removing a docstring is a statement added or removed and selects the importers.

### A test file, test helper or fixture was modified

Test files are Python files and go through the same analysis: editing the body of one test
method selects that test; editing shared helper code selects whoever executes it; editing
the class or module level of a test file selects every test that imports it (all tests of
the file, and for `tests/dialects/test_dialect.py` every dialect test, because they all
import its `Validator`).

One refinement for test classes: a **new method named `test_*`** is a new test that pytest
collects; no existing test can notice it, so it selects nothing. A **removed** method
selects whoever ran it (for a test method, the test that no longer exists). A new method
with any other name could override a helper or `setUp`, so it selects the whole file.

A non-Python file (`.sql` fixtures, `.csv.gz` data): the tests during which the file was
opened.

### pyproject.toml and setup.cfg

pytest reads both on every run to look for its settings. If the new file still parses and
has no pytest section, pytest behaves the same, and only tests whose own code opened the
file are selected. If it no longer parses or gains pytest settings: run everything.

### New, deleted and renamed files

- **Deleted**: tests that import it, open it, execute it, or list its directory.
- **New**: tests whose code lists that directory or one above it, and tests that tried to
  import a module of that name and did not find it (sqlglot tries `sqlglot._version` and
  `sqlglotrs`, for example). A new `conftest.py`, `pytest.ini`, `tox.ini`, `__init__.py`,
  `.pth`, any new top-level Python or config file, or a new top-level directory: run
  everything, because the repository root is on the import path and such files can change
  pytest or shadow a module. A new test module selects nothing unless its top level does
  more than import and define things.
- **Renamed**: a deletion plus an addition.

### Files nobody uses

A file that no test opens, imports or lists (README, docs, CI config, benchmarks) selects
nothing. The evidence is the recording of every file opened during the whole suite.

3. **Split** the selected tests over k = 1..8 workers (longest test first onto the least
   loaded worker) and keep the k with the smallest "largest load + 2 s x k", in whole
   milliseconds as SCORING.md defines.

## Safety nets

- Every commit is wrapped: any exception, and any patch the parser does not fully
  understand, falls back to running every test for that commit.
- A wall-clock budget: past it, remaining commits run everything.
- A test in `tests.json` that the maps do not know is added to every non-empty selection.
- Only JSON lines go to stdout; all diagnostics go to stderr.

## What is still risky

Listed from most to least worrying.

1. **Tests that depend on each other.** The suite is not fully order-independent: run in
   reverse, four tests fail at the pinned commit because an earlier test left state behind
   (one test empties the dialect registry). If a patch changes what test A leaves behind,
   test B can fail without executing a single changed line. No coverage-based selector can
   see that. We do not know the order or the number of pytest workers the organisers used.
2. **The slow tests dominate the cost, not the misses.** `test_execute_tpcds` (21.5 s)
   executes a large share of the optimizer and executor, so it is selected for roughly a
   quarter of code changes in core modules, and each of those commits costs about 23-25 s.
   It really does fail on about half of them in the sample. Verifying it locally costs 46
   CPU seconds (it runs 57 queries in a process pool), more than selecting it.
3. **Class attributes read behind the recorder's back.** A read through `cls.__dict__` or
   `vars(cls)` does not go through the descriptor. sqlglot does this in two places, both
   handled (the dialect metaclass, covered by the "computed attributes" rule, and a
   presence-only check in `tokens.py`). The narrowing is also limited to edits that cannot
   fail at import.
4. **Empty selections.** We return nothing for: comment/format-only Python changes
   (identical syntax tree), changes to code no test executes, function docstrings, class
   and module docstrings (text changed, not added/removed), files no test opens, mode-only
   changes, new files nothing lists or tries to import. Each rests on a recording of the
   whole suite, but a wrong "nothing" costs 300 s.
5. **New kinds of patch in the final.** Anything the parser does not recognise runs
   everything (36.8 s), so surprises cost time, not misses, unless they slip through one
   of the rules above in a way we did not think of.
6. **Timeouts.** A patch that makes a test hang counts as a failure. Such a test executes
   the changed statement, so it is selected; this needs no special handling.

## Results log

| Step | Sample score (s) | Misses | CPU (s) | Synthetic set |
| --- | --- | --- | --- | --- |
| Starter kit (everything, 3 workers) | 1210.470 | 0 | ~0.05 | - |
| v1: statement-level line maps + import/open/list maps | 214.401 | 0 | 0.34 | - |
| + whole-suite recordings merged, docstring and default rules | 214.401 | 0 | 0.24 | - |
| + parse only the touched statement (same answers, less CPU) | 214.401 | 0 | 0.11 | 630/630 identical to whole-file parse |
| + class-attribute reader maps | 178.749 | 0 | 0.16 (in Docker) | 7 of 155 ground truths so far: 0 misses |
| + dialect classes instrumented before their metaclass runs | 170.202 | 0 | 0.11 | 20 of 155 ground truths: 0 misses |

| + statement-level handling of added/removed statements, package-inspection rule, parser hardening | 170.202 | 0 | 0.12 (in Docker) | see below |

By kind on the sample at the last step: code 142.1 s (16 commits; the best possible is
79.1 s), comment 0.0 s (3), fixture 17.9 s (8; best possible 17.9), multi 10.2 s (3; best
possible 10.2). "Best possible" means selecting exactly the failing tests.

### Local evaluation sets (never shipped, never read by the selector)

Synthetic patches are generated against the pinned checkout by `tools/gen_patches.py`
(operator and constant flips in function bodies and at class/module level, removed table
entries, removed or dedented statements, shadowing assignments, renamed functions, changed
defaults, removed decorators and imports, syntax errors, comments, docstrings, whitespace,
fixture edits, test edits, deleted/renamed/new files, config files, multi-file mixes). The
real tests are then run on each patch to learn which fail.

| Set | How ground truth was obtained | Patches | With failures | Misses |
| --- | --- | --- | --- | --- |
| Mixed kinds | full suite (`tools/gt_loop.sh`) | 36 of 155 so far | 22 | 0 |
| Class-attribute and lambda rules | full suite | 26 | 19 | 0 |
| Dialect-specific modules | only the tests that import the patched module (`tools/fast_gt.py`): no other test can be affected | 287 | 191 | 0 |

In the third set the selector chose 25,242 tests in total where "every importer" would be
94,215. One patch there turned a loop infinite and exhausted memory, killing the test
process; the 133 importers we did not select were then run separately and all passed.

The ground-truth method was checked against the organisers' answers: running the full
suite on three sample patches reproduced their failing sets exactly.
