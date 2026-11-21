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
- which modules it **tries to import and does not find**,
- which **class attributes** it reads,
- for every **comparison** it evaluates, how the two operands relate (same object,
  equal, less),
- for every **`and`/`or` expression** it evaluates, which operands were reached and
  which of them were true.

For one slow test there is one more step, after the lookup: the image repeats the quick,
deterministic part of that test on the patched code and compares the outcome with the
pinned one (see "Replaying the slow executor test").

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

### Comparison operators (`is` / `==` / `!=` / `<` / `<=` ...)

Executing a changed line is necessary for a test to fail, but far from sufficient. Take
`if x is None:` rewritten as `if x == None:`. A test notices only if, at some moment while
it runs, the two operators give **different answers for the two values actually being
compared**. If they never do, the test's run is step for step the same as before, so it
cannot change its result.

What the operands look like is a fact about the pinned commit, and can be recorded before
any patch exists. A third recording rewrites every two-operand comparison in sqlglot,
while the module is being imported, into a call that returns exactly what the comparison
returns and also notes **how the two operands relate**: are they the same object, are
they equal, is one less than the other. Per test and per comparison the map keeps the set
of distinct outcomes seen (usually one or two). It says nothing about patches.

When a patch changes nothing but the operator of comparisons, the selector takes the tests
that execute the statement (the line map, as before) and removes those for which, in
**every** recorded outcome, the old and the new operator give the same answer. Everything
else stays: tests where some outcome differs, tests that execute the statement without
having been seen evaluating the comparison, and any comparison the map does not know.

Details that keep this honest:

- Unknown counts as different. If the recorder could not ask an operator (see next
  point), or asking raised, or the comparison did not return `True`/`False`, the test
  stays selected.
- **The recorder must not disturb the test it watches.** Asking `==` of two arbitrary
  objects runs their `__eq__`, and that can change things: comparing two sqlglot
  expressions computes and *caches* their hash, so the program later skips work it
  would normally do, and the recording no longer shows what the test really does. (We
  got this wrong at first; see "Mistakes" below.) So the recorder asks only questions
  that cannot change anything:
  - plain values (`None`, numbers, strings, bytes, enum members that are numbers, short
    tuples of those): every operator;
  - objects with no `__eq__` of their own: `==` is the same as `is`, nothing is called;
  - built-in containers: `==` only against a plain value or an object of the previous
    kind (that never looks inside the container);
  - objects whose class defines `__eq__` in Python: only `== None` (sqlglot has four
    such methods; all four return at once for `None`);
  - identity always, since it runs no code.
  Everything else stays unknown. A swap of `is` to `==` between two expression objects,
  or of `==` to `<` on anything but plain values, is therefore never narrowed.
- Code that runs only because the recorder asked something (an `__eq__` asked about
  `None`) is recorded like the test's own code. A recording may therefore hold more than
  the test does on its own, never less; more only ever keeps a test selected.
- A swap **to** `is`/`is not` next to a literal makes Python print a `SyntaxWarning` when
  the module is compiled. We cannot rule out that a test treats warnings as errors, so
  that case is not narrowed.
- Chained comparisons (`a < b < c`) are not recorded and not narrowed.
- The map is the union of independent runs: every test alone in a fresh process (child
  processes included), plus the whole suite in normal and in reversed order, and all of
  that done twice (Python randomises string hashing per process, which can change the
  order in which a set is walked; the second pass changed two entries). A test whose own run failed under the recorder is
  never narrowed.
- The recording was made with the same Python version as the image, so the positions
  (line, column) of comparisons in the syntax tree match what the selector computes.

On the sample this moves three commits from "run the 21 s test" to "run nothing"
(`is` -> `==` on an enum member, `is None` -> `== None`, `<` -> `<=`): the tests execute
the line, the operands never distinguish the two operators, and indeed nothing fails.

### `and` / `or`

The same idea, for short-circuit expressions. Python evaluates `a and b` left to right
and stops as soon as the answer is known, so many tests that execute the line never look
at `b`. The comparison recording also notes, for every `and`/`or` expression (nested
ones are flattened into one list of operands) and every test, each distinct **outcome**:
which operands were evaluated and which of those were true. Again a fact about the
pinned commit only.

Two rules use it. Both start from the tests that execute the statement and only remove
tests that were *seen* evaluating the expression; a test that executes the statement but
was never seen evaluating the expression stays selected, and so does everyone when the
map does not know the expression.

1. **`and` swapped with `or`** (operands untouched). For each outcome a test produced,
   the selector replays the new expression on the recorded operand values. The test is
   removed only if, for every one of its outcomes:
   - the new expression needs no operand the old one did not evaluate (we do not know
     that operand's value, so we cannot say what would happen);
   - the result is the same: the same operand's value, or, where only the truth of the
     expression is used (an `if`, `while`, `not`, a condition), the same truth;
   - every operand the new expression no longer evaluates is free of side effects. That
     is judged from the source, strictly: names, constants, attribute reads, constant
     subscripts, comparisons, arithmetic, eight built-ins (`isinstance`, `issubclass`,
     `len`, `getattr`, `hasattr`, `type`, `callable`, `id`) and methods only strings
     have. An attribute whose name is that of a decorated function (a property, say)
     anywhere in sqlglot with a body that does more than return such an expression
     counts as having side effects (`maps/bool/_properties.json` lists them: 192 of 206
     names). Any other call, any comprehension or generator expression (walking an
     iterator uses it up), `any`/`all`, `str()` and f-strings: the test stays.
   On most swaps this removes few tests, because a swap usually does change the result.
2. **A change confined to a later operand** (`a and b` where only `b` differs; any
   operand but the first). A test that never reached that operand cannot notice. Tests
   that did reach it stay. Not applied when the old or new operand contains `:=`,
   `yield` or `await`, which change the function around them just by being there.

An evaluation that did not finish (an exception inside an operand, a generator that was
left suspended) is recorded as "abandoned" and keeps the test selected under both rules.

To record truth the recorder asks `bool()` of every operand, including the last one,
which Python itself does not ask. That runs `__bool__`/`__len__`; sqlglot has four such
methods and each only returns a stored value. An operand whose truth cannot be asked
(it raises) makes the evaluation "abandoned".

No sample commit benefits from these two rules. The sample has three `and`/`or` swaps;
the tests that execute them nearly all fail (8 of 10, 37 of 37, 10 of 10) and all stay
selected. The value is on other patches of these kinds: over every possible one in
sqlglot the rules cut the cost by roughly 3% (`and` -> `or`), 15% (`or` -> `and`) and
25-40% (later operand).

### Replaying the slow executor test (replay.py)

Where the time goes: three tests take 57 of the suite's 103 seconds. The one that matters
most is `tests/test_executor.py::TestExecutor::test_execute_tpcds` (21.5 s). It runs 57
TPC-DS queries through the optimizer and sqlglot's own Python executor and compares the
rows with DuckDB. It therefore executes a large part of the optimizer, planner and
executor, and gets selected for about one code change in six. Over every possible
single-token change in sqlglot, commits that select this test carry two thirds of the
total cost, and for about half of them the test does not actually fail. The rules above
cannot tell which half: the test does execute the changed statement.

But most of those 21 seconds are not sqlglot. The test starts worker processes, loads CSV
tables and runs DuckDB. What it does *with sqlglot* is three calls per query:

    parent    parse_one(sql)                   -> the expression, pickled to a worker
    worker    mp_execute(expression, meta)     -> optimize, plan, execute over the tables
    parent    transpile(sql, write="duckdb")   -> the SQL text DuckDB is given

and these are quick: 17 CPU-seconds for all queries together, 0.2 s for a typical one.
They are also the only way sqlglot code can reach the verdict. So for this one test the
selector can do better than reason about it: it can **repeat those calls on the patched
code** and look at what they return.

`replay.py` does exactly that, in the image, for chosen queries: same query text, same
arguments, the tables built the way the test builds them, the same pickling in between.
It takes a fingerprint of everything the calls return (the object graphs are walked
completely, without calling any method). The fingerprints of the pinned code are computed
when the image is built. The selector drops the test only when **all** of this holds:

1. Everything in the patch that the test executes is a statement inside a function, or a
   function changed as a whole (a default argument, a name that becomes local), in a
   file under `sqlglot/` (no class-level or module-level code, no test file, no fixture).
   For a function changed as a whole, every line of its body that the test executes
   counts as changed.
2. The test executes each of those statements inside the three calls and nowhere else.
   A recording of the real test, in all its processes, says for every line whether it
   ran inside or outside the calls (`tools/site_replay`, three runs, identical).
3. Which queries execute the statement does not depend on what ran earlier in the
   process. Every query was recorded alone in a fresh process and as part of one long
   run; a line where the two differ (a table built on first use, a cache) is left out.
4. The replay of every query that executes one of the statements gives the pinned
   fingerprint.

Then each of the test's steps receives the same values as at the pinned commit, and the
steps that are not replayed (comparing with DuckDB) run the same code on the same input.
The test ends as before: it passes. Any difference, exception or time-out keeps it.

Two refinements:

- **Up to the plan.** Executing the plan over the data is the expensive step. For a
  query in which the patched statements run only before that step, comparing what goes
  *into* it (the plan and the tables) is enough, at a third of the cost. The recording
  keeps the two apart per line and per query. If the plan does differ, the query is
  executed after all and its rows decide: an optimizer change often gives another plan
  with the same result.
- **Hash seeds.** One query is parsed with its modifiers in an order that follows the
  iteration order of a set, so its fingerprint depends on Python's hash seed. Baselines
  exist for six seeds; the replay runs once per distinct pinned behaviour of the queries
  involved (normally once, twice if that query is among them), with the seed fixed.

It is only attempted when it pays: the estimated CPU (recorded per query) must be at most
0.6 of what dropping the test would save, one replay may not use more than that either,
and all replays of a run share a 45-second budget. The CPU seconds are part of the score, so they are in the results table.

The replay needs sqlglot to behave as it does where the tests run. sqlglot imports two
third-party packages if present (`dateutil` for date arithmetic, `mypy_extensions`); the
image installs both at the versions of `requirements.lock`. If the baseline cannot be
built, the selector works as before, without this step.

What this argument assumes, plainly: that the three calls are deterministic given their
arguments, and that they leave nothing behind in the process that later steps read
without going through the changed statement. Point 3 checks the second assumption as far
as coverage can show it. It is checked end to end by running the real test on patched
trees where the replay said "same" (see the left-out-tests check).

### Running cheap candidates in the image (verify.py)

After the rules above, the selected tests are *candidates*: each executes something the
patch changed. Many of them still pass. Handing a candidate out costs at least the 2
seconds of a worker; running it here costs its CPU time, which for most sqlglot tests is
a few milliseconds. So when the candidates are cheap, the selector runs them on the
patched code, in the image, and hands out only the ones that do not pass.

- **Allowed, and what "fails" means.** The kit's README says: "Running sqlglot's tests
  while you decide is allowed, and you pay for the time", and: "'Fails' means pytest
  reports a failure, an error or a timeout for the test, or the test's file cannot be
  imported. A test with subtests is one test."
- **What runs.** sqlglot's tests are plain `unittest` cases (its own `make test` is
  `python -m unittest`). `verify.py` loads the candidates and runs them with unittest in
  one process, in suite order. "Not passed" means failed, errored, a failing subtest, an
  unexpected success, skipped (a skip is no evidence of passing), a test file that
  cannot be imported, or a class that could not be set up. A run that exceeds its time limit tells us nothing and keeps every candidate.
- **On what.** A copy of the pinned tree in `/tmp`, made of symbolic links, in which the
  patched files are real files. Tests that start further Python processes (the optimizer
  tests use a process pool, one dialect test runs `python -c`) get the patched code
  there too. Only patches that modify text files we hold are handled; anything else
  (added, deleted, renamed, binary) keeps the selection as it is.
- **Which tests.** Only tests in `maps/verify.json`: tests that pass in this image at the
  pinned commit when run alone in a fresh process (twice) and when run with the rest of
  their file. That is 1,557 of the 1,620. The executor tests are not among them (they
  need the TPC-H tables, which are not shipped); for the slow one there is the replay.
  A test that is not in the map is never run here and stays selected.
- **Kept small.** The optimizer tests hand every fixture case to a process pool. A real
  pool starts one worker per CPU of the host, each importing DuckDB and pandas; here the
  pool is replaced by a stand-in that runs each call in the same process, with the same
  pickling of arguments and results. DuckDB gets two threads.
- **When.** The map holds each test's CPU seconds. Candidates are run when the estimate
  is at most VERIFY_WORTH times what the commit would cost less if they all passed, and
  all such runs share a 50-second budget. A patch that changes no Python file (a fixture
  edit) is not run: the tests that read a changed fixture are expected to fail.
- **Stopping early.** After a failure the run checks whether going on can still pay:
  with the failures so far, how much less can the commit still cost, against the CPU the
  remaining candidates need. If not, it stops; candidates that were not run stay
  selected. So a patch that breaks most of its candidates costs a fraction of a second.
- **Afterwards.** Tests that passed here are the first to be put into spare room on the
  workers (step 4), so where room exists they are run by the scorer anyway.

What this rests on: a test that passes here on the patched code also passes where the
organisers ran it. The image has the same Python and the same versions of every package
sqlglot and its tests import (`requirements.lock`). It differs in what else ran in the
process before the test, and in Python's hash seed. A test whose result depends on
either would be at risk; the map leaves out tests that behave differently alone and in
their file at the pinned commit, which is the part of this we can observe.

It was checked against the organisers' own answers: forced on for every sample commit,
the tests reported as not passing are exactly the organisers' failing tests, for every
test in the map (0 missed, 0 extra). And against pytest on two sets of 60 random
single-token changes each (see the evaluation sets): forced on, 98 commits were run here,
several with more than a thousand candidates; no test that fails under pytest passed
here, and no test that passes under pytest was reported as failing.

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
  `.pth`, or any new top-level Python or config file: run everything, because the
  repository root is on the import path and such files can change pytest or shadow a
  module (in our test set a new top-level `json.py` broke all 1620 tests). A new
  directory can only become a "namespace package", which never shadows a real module, so
  it matters only to tests that tried to import that name. A new test module selects
  nothing (its tests are not in the list we choose from) as long as importing it only
  defines things: plain imports, functions without decorators, test classes whose base
  is `TestCase` or `Validator`, and constants. Anything else at its top level (a
  decorator, a call, a class with another base, which might register itself somewhere)
  could change state other tests see, so we run everything.
- **Renamed**: a deletion plus an addition.

### Files nobody uses

A file that no test opens, imports or lists (README, docs, CI config, benchmarks) selects
nothing. The evidence is the recording of every file opened during the whole suite.

3. **Split** the selected tests over k = 1..8 workers (longest test first onto the least
   loaded worker) and keep the k with the smallest "largest load + 2 s x k", in whole
   milliseconds as SCORING.md defines.
4. **Fill spare room.** A commit costs its busiest worker. Once the split is fixed, the
   other workers can take more tests for free as long as none becomes busier than the
   busiest. That room is filled, first with the tests the narrowing rules above removed
   (comparison and `and`/`or` outcomes, replay), then with tests that import a changed
   Python file. It never adds a worker and never raises the largest load, so the score
   cannot change; if one of the narrowing rules is ever wrong about a cheap test, the
   test may still be run. It is no substitute for the rules being right: a commit that
   selects nothing has no room, and the slow test never fits. The evaluation runs with
   this switched off (`BLAST_FILL=0`), so that it measures the rules alone.

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
   the changed statement, so it is selected; this needs no special handling. A helper
   program that runs into its own time limit decides nothing: the tests stay selected.
7. **A test that passes in the image and fails where the organisers ran it.** The step
   that runs candidates here trusts the image: same Python, same package versions, but
   a fresh process with only the candidates in it and its own hash seed. A test whose
   result under a patch depends on the hash seed, or on what other tests ran before it,
   could differ. We found no such case in about a hundred commits compared with pytest,
   and none in the sample answers; it cannot be ruled out.

## Mistakes made along the way

- **The comparison recorder changed what it was watching.** Its first version asked `==`
  between any two objects to learn how they relate. Between two sqlglot expressions that
  computes their hash and stores it on the object. Later code then found the hash ready
  and skipped computing it, so the recording showed fewer tests running the hash code
  than really do. Nothing in the sample or in our synthetic sets noticed. It was caught
  by the check that runs every test a rule leaves out (next section): for an `and` -> `or`
  swap inside `Expression.__hash__`, 47 of 94 left-out tests failed. The recorder was
  rewritten to ask only harmless questions (see "Comparison operators"), everything was
  recorded again, and the checks were repeated from scratch on the new maps.
- The same first version also made the recording extremely slow (the question re-entered
  `Expression.__eq__` until Python's recursion limit, on every comparison of expressions).

- **A skipped test looked like a passing one.** The first version of the program that
  runs candidates in the image counted a skipped test as passed. A test that is skipped
  here might run, and fail, elsewhere. A hand-made patch that adds a skip decorator
  showed it. Skips now keep the test selected.
- **Ground truth that said "everything fails".** See the note on the ground-truth tools
  under "Local evaluation sets": two apparent misses were the tool's, not the selector's.

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

| + comparison-operator rule (maps of observed operand outcomes) | 117.425 | 0 | 0.12 | all five sets below: 0 misses; plus the left-out-tests check below |

| + `and`/`or` swap rule and later-operand rule | 117.425 | 0 | 0.10 | all five sets: 0 misses (mixed set 1531.1 s -> 1504.8 s); left-out-tests checks below |

| + replay of the slow executor test; spare room on workers filled | 117.425 | 0 | 1.03 | mixed set 1504.8 s -> 1448.3 s, 0 misses; the other sets unchanged, 0 misses; replay check below |

| + replay extended (whole functions; a different plan with the same rows); cheap candidates run in the image | 112.960 | 0 | 2.8 | see "Through the image" below: all seven sets 0 misses |

The board score of the step before the replay was 193.3 s against 117.4 s on the sample: the
board's commits happen to select the slow executor test more often. Over every possible
single-token change (one file drawn at random, then one place in it), the expected cost
of a code commit was 7.2 s, two thirds of it from commits that select that test. That is
what the replay step is aimed at. On the sample it costs 0.9 s of CPU and saves nothing
(the one commit it looks at really fails the test; the replay says "differs" after
0.7 s).

By kind on the sample at the last step: code 89.3 s (16 commits; the best possible is
79.1 s), comment 0.0 s (3), fixture 17.9 s (8; best possible 17.9), multi 10.2 s (3; best
possible 10.2). "Best possible" means selecting exactly the failing tests. Before the
comparison rule the code commits cost 142.1 s. The `and`/`or` rules change nothing on
the sample.

### Local evaluation sets (never shipped, never read by the selector)

Synthetic patches are generated against the pinned checkout by `tools/gen_patches.py`
(operator and constant flips in function bodies and at class/module level, removed table
entries, removed or dedented statements, shadowing assignments, renamed functions, changed
defaults, removed decorators and imports, syntax errors, comments, docstrings, whitespace,
fixture edits, test edits, deleted/renamed/new files, config files, multi-file mixes). The
real tests are then run on each patch to learn which fail.

| Set | How ground truth was obtained | Patches | With failures | Misses |
| --- | --- | --- | --- | --- |
| Mixed kinds | full suite (`tools/gt_loop.sh`) | 155 | 101 | 0 |
| Class-attribute and lambda rules | full suite | 26 | 19 | 0 |
| Dialect-specific modules | only the tests that import the patched module (`tools/fast_gt.py`): no other test can be affected | 287 | 191 | 0 |
| Dialect, executor and optimizer modules, and test files | importers only | 182 | 139 | 0 |
| Hand-made unusual kinds (`tools/gen_special.py`): test methods added, removed, renamed or skipped; `setUp` added; shared test helpers edited or deleted; new test files, modules, directories, `conftest.py`; binary fixtures changed or deleted; renamed fixtures, test files and modules; mode-only changes; files outside the package; `pyproject.toml`; package `__init__.py` | full suite | 42 | 20 | 0 |

In the third set the selector chose 25,242 tests in total where "every importer" would be
94,215. One patch there turned a loop infinite and exhausted memory, killing the test
process; the 133 importers we did not select were then run separately and all passed.

In the mixed set, two full-suite runs did not finish inside the 20-minute limit (one
patch makes the tokenizer loop forever, the other makes almost every test fail slowly).
For those two, every test the selector had left out was run separately under the patch:
1597 of 1597 and 274 of 274 passed.

The ground-truth method was checked against the organisers' answers: running the full
suite on three sample patches reproduced their failing sets exactly.

Two more sets were made to look like the organisers' code commits: one single-token
change per commit (`and`/`or`, comparison operator, `True`/`False`, number + 1, `not`
removed), the file drawn at random from `sqlglot/`, then the place. In the first the
kind of change follows from what the file contains; in the second the kinds are drawn
in the proportions of the sample. 60 commits each. Ground truth: pytest on the tests
the rules select (or the whole suite when they select none), one commit left out
because the patched code never finishes. 44 and 48 of them break at least one test.

One correction to the ground-truth tools came out of this: when a test file cannot be
imported, pytest given test ids stops the whole session, and the tool then counted every
test as failing. The kit defines "fails" per test (a test whose own file cannot be
imported fails; the others are judged on their own), so the tool now gives pytest whole
files with `--continue-on-collection-errors` and keeps the wanted tests in a plugin.

#### Through the image, all steps on, spare-room filling off

Simulated seconds, and next to them the CPU seconds the two helper programs used (they
are part of the score). "Rules only" is the selection before replay and candidate runs.

| Set | Commits | Rules only | Now: simulated + helper CPU | Misses |
| --- | --- | --- | --- | --- |
| Sample | 30 | 117.4 | 113.0 + 2.8 | 0 |
| Organiser-like, first | 59 | 450.7 | 344.2 + 36.2 | 0 |
| Organiser-like, second | 60 | 418.8 | 365.2 + 30.7 | 0 |
| Mixed kinds | 155 | 1504.8 | 1421.8 + 31.7 | 0 |
| Class-attribute and lambda rules | 26 | 282.1 | 241.9 + 12.1 | 0 |
| Hand-made unusual kinds | 42 | 473.5 | 471.0 + 2.0 | 0 |

What is left on the two organiser-like sets is mostly out of reach for a sound rule: a
few commits change code that runs when a core module is imported (a metaclass, a base
class). Every test executes it, a handful fail, and the two slowest tests cannot be
decided for less CPU than they cost to hand out.

### The left-out-tests check (for the comparison and `and`/`or` rules)

The sets above mostly tell us about patches that make tests fail. These rules are about
tests we *leave out*, so they get a direct check: `tools/gen_swaps.py` draws random
single-edit patches of the kind a rule handles from the **whole** of sqlglot (not from
the sample), keeps those where the rule leaves out at least one test that executes the
statement, and `tools/swap_gt.py` runs exactly those left-out tests on the patched tree.
Every one of them must pass. A single failure means the rule or the recording is wrong.

| Rule | Patches | Left-out tests run | Failed |
| --- | --- | --- | --- |
| Comparison operator swapped, first recording | 400 | 31,173 | 0 |
| Comparison operator swapped, shipped maps, a fresh draw of patches | 400 | 43,713 | 0 |
| `and` <-> `or` (200 each way) | 400 | 39,876 | 0 |
| Later operand: the operand replaced by an expression that raises as soon as it is evaluated | 400 | 51,721 | 0 |
| Replay of the slow executor test said "same": the real test run on the patched tree | 67 | 67 | 0 |
| Candidates run in the image, forced on for every commit of the two organiser-like sets, against pytest | 98 | every candidate | 0 passed here but failing there, 0 the other way |

For the later-operand rule the replacement is the harshest possible change: any left-out
test that evaluates the operand even once crashes.

The replay row comes from a different draw: 140 random single-token changes (`and`/`or`,
comparison, `True`/`False`, number + 1, `not` removed) on lines the replay map covers, one
file at random and then one place in it. Run through the image: 117 replays attempted,
66 "same" (4.6 s each on average), 51 "differs" (1.0 s on average, because a difference
ends a replay early), the rest judged not worth the CPU. Every "same" was then run for
real (45 CPU-seconds each), 67 patches over the versions of the rule: all pass. That
cannot show a failure rate below a few percent, so this check is weaker than the three
above; what carries the rule is the argument in "Replaying the slow executor test".

History, so the numbers are not read as more than they are. The comparison check was
run with the first recording. After the recorder was rewritten (see "Mistakes"), the
same 400 patches were looked at again with the new maps: on 392 the same tests are left
out, on 6 fewer, and on 2 one more test each; those two were run and pass. Then a fresh
draw of 400 patches was checked against the shipped maps (second row). The `and`/`or`
check that caught the mistake was thrown away and both `and`/`or` checks were generated
and run afresh on the new maps. Merging the second recording pass changed what is left
out on one of the 800 patches (fewer tests). Later the side-effect judgement was made
stricter (comprehensions, `any`/`all`, `str()` no longer count as harmless); that leaves
out fewer tests on 12 of the 400 `and`/`or` patches and no additional test anywhere, so
the results above still cover everything the shipped rules leave out.

What this does and does not show: with 0 failures in 400 patches per rule, a rule that
went wrong on more than about 1 patch in 130 would very likely have been caught. It does
not prove zero. One miss costs 300 s. A narrowed patch saves on average about 2 s
(`and` -> `or`), 6 s (`or` -> `and`) or 5 s (later operand), so these rules pay only if
they go wrong on fewer than about 1 narrowed patch in 50 to 150. The check supports
that, without much to spare. The comparison rule saves more per narrowed patch (about
12 s for `is not` -> `!=`, the one swap measured this way), so its margin is wider.
