"""Hand-written patches of kinds the random generator does not make (EVALUATION ONLY).

Test classes gaining or losing methods, shared test helpers, new files and directories,
binary fixtures, renames, file modes, files outside the package. Ground truth comes from
running the real suite on each (tools/gt_loop.sh), never from this list.

usage: gen_special.py <scratch checkout of pinned sqlglot> <output dir>
"""
import gzip, json, os, subprocess, sys

REPO, OUT = sys.argv[1:3]
os.makedirs(os.path.join(OUT, "commits"), exist_ok=True)


def git(*a):
    return subprocess.run(["git", "-C", REPO, *a], capture_output=True, check=True).stdout


def path(f):
    return os.path.join(REPO, f)


def read(f):
    return open(path(f), encoding="utf-8").read()


def write(f, text):
    os.makedirs(os.path.dirname(path(f)), exist_ok=True)
    open(path(f), "w", encoding="utf-8").write(text)


def sub(f, old, new, count=1):
    text = read(f)
    assert old in text, (f, old)
    write(f, text.replace(old, new, count))


def drop_method(f, name):
    lines = read(f).split("\n")
    start = next(i for i, l in enumerate(lines) if l.startswith("    def %s(" % name))
    end = next((i for i in range(start + 1, len(lines)) if lines[i].strip() and not lines[i].startswith("     ")), len(lines))
    write(f, "\n".join(lines[:start] + lines[end:]))


def regzip(f):
    data = gzip.decompress(open(path(f), "rb").read())
    rows = data.split(b"\n")
    rows[1], rows[2] = rows[2], rows[1]
    open(path(f), "wb").write(gzip.compress(b"\n".join(rows), mtime=0))


CASES = [
    ("test_method_removed", lambda: drop_method("tests/test_serde.py", "test_meta")),
    ("test_method_removed", lambda: drop_method("tests/dialects/test_dax.py", "test_filter")),
    ("test_method_added", lambda: sub("tests/test_time.py", "    def test_subsecond_precision(self):", "    def test_brand_new(self):\n        self.assertEqual(1, 2)\n\n    def test_subsecond_precision(self):")),
    ("test_method_renamed", lambda: sub("tests/test_serde.py", "def test_recursion(self)", "def test_deep_recursion(self)")),
    ("test_setup_added", lambda: sub("tests/test_time.py", "class TestTime(unittest.TestCase):\n", "class TestTime(unittest.TestCase):\n    def setUp(self):\n        raise RuntimeError('no')\n\n")),
    ("test_class_attr_added", lambda: sub("tests/dialects/test_dax.py", "class TestDAX(Validator):\n", "class TestDAX(Validator):\n    maxDiff = 5\n")),
    ("test_class_attr_changed", lambda: sub("tests/dialects/test_dax.py", 'dialect = "dax"', 'dialect = "tsql"')),
    ("test_skip_decorator", lambda: sub("tests/test_serde.py", "    def test_meta(self):", "    @unittest.skip('x')\n    def test_meta(self):")),
    ("test_module_level", lambda: sub("tests/test_time.py", "\nclass TestTime(", "\nimport nonexistent_module_xyz\n\nclass TestTime(")),
    ("test_base_class", lambda: sub("tests/dialects/test_dialect.py", "    def validate_identity(", "    def setUp(self):\n        raise RuntimeError('no')\n\n    def validate_identity(")),
    ("helper_function", lambda: sub("tests/helpers.py", "            if i + 1 < size:", "            if i + 3 < size:")),
    ("helper_function", lambda: sub("tests/helpers.py", 'return "\\n".join([line for line in s.splitlines() if line and not line.startswith("--")])', 'return "\\n".join([line for line in s.splitlines() if line and not line.startswith("-")])')),
    ("helper_constant", lambda: sub("tests/helpers.py", 'os.path.join(FILE_DIR, "fixtures")', 'os.path.join(FILE_DIR, "fixture")')),
    ("helper_deleted", lambda: os.remove(path("tests/helpers.py"))),
    ("new_test_file", lambda: write("tests/test_zz_new.py", "import unittest\n\n\nclass TestNew(unittest.TestCase):\n    def test_new(self):\n        self.assertEqual(1, 2)\n")),
    ("new_test_file", lambda: write("tests/dialects/test_zz_new.py", "from tests.dialects.test_dialect import Validator\n\n\nclass TestNew(Validator):\n    dialect = 'duckdb'\n\n    def test_new(self):\n        self.validate_identity('SELECT 1')\n")),
    ("new_conftest", lambda: write("tests/conftest.py", "import pytest\n\n\n@pytest.fixture(autouse=True)\ndef _boom():\n    raise RuntimeError('no')\n")),
    ("new_package_dir", lambda: write("sqlglot/extras/tools.py", "VALUE = 1\n")),
    ("new_package_dir", lambda: write("json/tools.py", "VALUE = 1\n")),
    ("new_module", lambda: write("sqlglot/dialects/zzdialect.py", "raise RuntimeError('no')\n")),
    ("new_module", lambda: write("sqlglot/optimizer/zz_rule.py", "raise RuntimeError('no')\n")),
    ("new_module", lambda: write("sqlglot/typing/zz.py", "raise RuntimeError('no')\n")),
    ("new_fixture_dir", lambda: write("tests/fixtures/optimizer/tpc-h/extra.sql", "SELECT 1;\nSELECT 1;\n")),
    ("new_fixture_dir", lambda: write("tests/fixtures/jsonpath/extra.json", "[]\n")),
    ("binary_fixture", lambda: regzip("tests/fixtures/optimizer/tpc-h/nation.csv.gz")),
    ("binary_fixture", lambda: regzip("tests/fixtures/optimizer/tpc-ds/item.csv.gz")),
    ("binary_fixture_deleted", lambda: os.remove(path("tests/fixtures/optimizer/tpc-h/region.csv.gz"))),
    ("fixture_renamed", lambda: os.rename(path("tests/fixtures/optimizer/tpc-ds/tpc-ds.sql"), path("tests/fixtures/optimizer/tpc-ds/tpcds.sql"))),
    ("fixture_renamed", lambda: os.rename(path("tests/fixtures/pretty.sql"), path("tests/fixtures/pretty2.sql"))),
    ("test_file_renamed", lambda: os.rename(path("tests/test_serde.py"), path("tests/test_serde_renamed.py"))),
    ("module_renamed", lambda: os.rename(path("sqlglot/serde.py"), path("sqlglot/serde2.py"))),
    ("mode_only", lambda: os.chmod(path("sqlglot/parser.py"), 0o755)),
    ("mode_only", lambda: os.chmod(path("tests/test_serde.py"), 0o755)),
    ("outside_package", lambda: sub("Makefile", "install", "instal")),
    ("outside_package", lambda: sub(".github/workflows/python-package.yml", "python", "pythons")),
    ("outside_package", lambda: sub("sqlglotc/setup.py", "import", "import os\nimport")),
    ("outside_package", lambda: sub("README.md", "SQL", "sql")),
    ("outside_package", lambda: sub("tests/gen_fixtures.py", "import", "import os\nimport")),
    ("outside_package", lambda: sub("MANIFEST.in", "include", "exclude")),
    ("pyproject", lambda: sub("pyproject.toml", "[build-system]", "[tool.pytest.ini_options]\naddopts = \"-k nothing_matches_this\"\n\n[build-system]")),
    ("pyproject", lambda: sub("pyproject.toml", "name = \"sqlglot\"", "name = \"sqlglot2\"")),
    ("package_init", lambda: sub("sqlglot/__init__.py", "import typing as t", "import typing as t\nimport nonexistent_module_xyz")),
    ("package_init", lambda: sub("sqlglot/dialects/__init__.py", '"DuckDB"', '"DuckDb"')),
    ("tests_init", lambda: write("tests/__init__.py", read("tests/__init__.py") + "raise RuntimeError('no')\n")),
]

meta = {}
for number, (kind, edit) in enumerate(CASES):
    git("reset", "-q", "--hard", "HEAD"); git("clean", "-fdq")
    try:
        edit()
    except Exception as error:
        print("skipped", number, kind, repr(error)[:100])
        continue
    git("add", "-A")
    diff = git("diff", "--cached", "--binary", "-M", "--no-color", "HEAD")
    if not diff.strip():
        print("empty", number, kind)
        continue
    cid = "x%02d" % number
    open(os.path.join(OUT, "commits", cid + ".patch"), "wb").write(diff)
    meta[cid] = {"kind": kind, "desc": diff.decode("utf-8", "replace").split("\n")[0][11:90]}
git("reset", "-q", "--hard", "HEAD"); git("clean", "-fdq")
json.dump(meta, open(os.path.join(OUT, "meta.json"), "w"), indent=1)
print(len(meta), "patches")
