"""Build maps/typed.json from the recording made by tools/record_typed.sh.

For every typed dictionary defined under sqlglot/: the tests during which something read
one of the attributes that the `total=` flag decides (`__total__`, `__required_keys__`,
`__optional_keys__`) or the class's whole `__dict__`. The map is written only if
every test has a recording of its own run, alone, that passed. Lists hold test ids.

usage: build_typed_map.py <tests.json> <entry repo> <recording dir> [<recording dir> ...]
"""
import glob
import json
import os
import sys

TESTS, OUT, DIRS = sys.argv[1], sys.argv[2], sys.argv[3:]
tests = json.load(open(TESTS))["tests"]
classes = set()
readers = {}  # class -> set of test numbers
places = {}
complete = True
for test in tests:
    number = test["number"]
    for directory in DIRS:
        code = os.path.join(directory, "%d.rc" % number)
        files = glob.glob(os.path.join(directory, "%d.*.json" % number))
        if not os.path.exists(code) or open(code).read().strip() != "0" or not files:
            complete = False
            print("no clean recording of test %d (%s) in %s" % (number, test["id"], directory))
            continue
        for name in files:
            data = json.load(open(name))
            classes.update(data["classes"])
            for cls, attribute, filename, line in data["reads"]:
                readers.setdefault(cls, set()).add(number)
                key = "%s:%d reads %s" % (filename.split("/lib/")[-1], line, attribute)
                places[key] = places.get(key, 0) + 1
if not complete:
    raise SystemExit("recording incomplete: no map written")
name_of = {test["number"]: test["id"] for test in tests}
table = {}
for cls in sorted(classes | set(readers)):
    path, qualname = cls.split("::")
    if cls not in classes:
        # read, but not a module-level class of the repository: kept, so that its readers
        # count for every typed dictionary (it may be a subclass of one of ours)
        path, qualname = "(elsewhere)", cls
    table.setdefault(path, {})[qualname] = sorted(name_of[n] for n in readers.get(cls, ()))
json.dump({"watched": ["__total__", "__required_keys__", "__optional_keys__", "__dict__"], "tests": len(tests), "classes": table},
          open(os.path.join(OUT, "maps", "typed.json"), "w"), indent=1, sort_keys=True)
print("%d typed dictionaries in %d files; %d of them read by some test" % (
    len(classes), len(table), sum(1 for c in classes if readers.get(c))))
for key in sorted(places, key=places.get, reverse=True)[:20]:
    print("   %5d x %s" % (places[key], key))
