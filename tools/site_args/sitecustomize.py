"""Recorder for the argument tables of expressions: which tests can notice whether an
argument is marked required.

Loaded into every Python process started while one test runs; active when BR_ARGS is set.

Every expression class has a table `arg_types = {"this": True, "expressions": False, ...}`.
sqlglot looks at the True/False values in one place: when a class is created,
`Expr.__init_subclass__` computes

    cls.required_args = {k for k, v in cls.arg_types.items() if v}

and `required_args` in turn is read in one place, the loop of `Expression.error_messages`
(the method of the same name on the base class `Expr` only raises NotImplementedError):

    for k in self.required_args:
        v = self.args.get(k)
        if v is None or (isinstance(v, list) and not v):
            ... one more message ...

So whether argument k of a table is marked required matters to a test only if, in some
call of that loop for an expression using the table, the argument is missing (None or an
empty list): then a message appears or disappears. One more case is kept: a call that
finds two or more required arguments missing, because the order of the messages follows
the order of the set, which may change when the set gains or loses a member.

The recorder puts a descriptor in place of every class's `required_args`. A read from
the loop notes, for the expression at hand, every argument of its table that is missing
at that moment, required or not. A read from anywhere else is noted as "read elsewhere":
such a test sees the whole set. (tools/build_arg_maps.py checks that the two pieces of
source quoted above are what the pinned file says.)

Written to $BR_ARGS.<pid>.json at exit:
  missing    {"file::Class": [argument, ...]}  arguments seen missing in the loop, by the
             class whose body holds the table the expression uses
  several    ["file::Class", ...]  tables for which two or more required arguments were
             missing in one call
  elsewhere  ["file::Class", ...]  tables whose `required_args` was read outside the loop
  tables     {"file::Class": "file::Class"}  every expression class -> the class whose
             body holds its table
  unwatched  classes whose `required_args` could not be replaced
"""
import os
import sys

_OUT = os.environ.get("BR_ARGS")
if _OUT:
    import atexit
    import builtins
    import json
    import signal

    _ROOT = (os.environ.get("BR_ROOT") or os.getcwd()).rstrip("/") + "/"
    _CORE = _ROOT + "sqlglot/expressions/core.py"
    _missing = {}
    _several = set()
    _elsewhere = set()
    _table_of = {}
    _base = []
    _setattr = type.__setattr__
    _build_class = builtins.__build_class__

    def _class_name(cls):
        filename = getattr(sys.modules.get(cls.__module__), "__file__", None) or ""
        where = filename[len(_ROOT):] if filename.startswith(_ROOT) else "(%s)" % cls.__module__
        return "%s::%s" % (where, cls.__qualname__)

    def _table(cls):
        """The class in whose body the `arg_types` that `cls` uses is written."""
        found = _table_of.get(cls)
        if found is None:
            for klass in cls.__mro__:
                if "arg_types" in vars(klass):
                    found = _class_name(klass)
                    break
            else:
                found = "?"
            _table_of[cls] = found
        return found

    class _Required:
        def __init__(self, value):
            self.value = value

        def __get__(self, obj, objtype=None):
            code = sys._getframe(1).f_code
            if obj is not None and code.co_name == "error_messages" and code.co_filename == _CORE:
                cls = type(obj)
                table = _table(cls)
                seen = _missing.get(table)
                if seen is None:
                    seen = _missing[table] = set()
                args = obj.args
                required = 0
                for k in type.__getattribute__(cls, "arg_types"):
                    v = args.get(k)
                    if v is None or (isinstance(v, list) and not v):
                        seen.add(k)
                        if k in self.value:
                            required += 1
                if required >= 2:
                    _several.add(table)
            else:
                _elsewhere.add(_table(objtype if objtype is not None else type(obj)))
            return self.value

    def _watch(cls):
        value = vars(cls).get("required_args")
        if isinstance(value, set):
            _setattr(cls, "required_args", _Required(value))

    def _recording_build_class(func, name, *bases, **kwargs):
        cls = _build_class(func, name, *bases, **kwargs)
        try:
            if not _base:
                module = sys.modules.get(getattr(cls, "__module__", None))
                if name == "Expr" and getattr(module, "__file__", None) == _CORE:
                    _base.append(cls)
                    _watch(cls)
            elif isinstance(cls, type) and issubclass(cls, _base[0]):
                _watch(cls)
        except Exception as error:
            print("args recorder:", repr(error), file=sys.stderr)
        return cls

    builtins.__build_class__ = _recording_build_class

    _done = []

    def _dump(*_):
        if _done:
            return
        _done.append(1)
        try:
            tables, unwatched = {}, []
            stack = list(_base)
            seen = set()
            while stack:
                cls = stack.pop()
                if cls in seen:
                    continue
                seen.add(cls)
                tables[_class_name(cls)] = _table(cls)
                if "required_args" in vars(cls) and not isinstance(vars(cls)["required_args"], _Required):
                    unwatched.append(_class_name(cls))
                stack.extend(type.__subclasses__(cls))
            with open("%s.%d.json" % (_OUT, os.getpid()), "w") as fh:
                json.dump({"missing": {k: sorted(v) for k, v in _missing.items()}, "several": sorted(_several),
                           "elsewhere": sorted(_elsewhere), "tables": tables, "unwatched": sorted(unwatched)}, fh)
        except Exception as error:
            print("args recorder dump failed:", repr(error), file=sys.stderr)

    atexit.register(_dump)

    def _term(signum, frame):
        _dump()
        os._exit(143)

    try:
        signal.signal(signal.SIGTERM, _term)
    except Exception:
        pass
