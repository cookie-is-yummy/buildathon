"""Second recorder: which tests READ which class attribute of sqlglot.

Loaded into every Python process started while one test runs; active when BR_ATTR is set.

Every data attribute in the body of a class defined under sqlglot/ is replaced by a
descriptor that notes each read and returns the plain value. A read through an instance,
the class or a subclass all reach the descriptor of the class that DEFINES the attribute,
which is exactly the dependency "this test depends on this assignment in this class body".

Class creation is watched too. `__init_subclass__` hooks and metaclasses compute further
attributes from the body's attributes (for example `required_args` from `arg_types`).
While a class is being created we note which attributes are read and which computed
attributes already existed at that moment; a computed attribute can only depend on reads
that happened before it was set.

Dictionaries in class bodies (the dialects' tables) are watched per key as well. Such a
dictionary is replaced by a dict subclass with the same content that notes which keys a
test looks up, and when a test does something that shows it every value (values(),
items(), a copy, a comparison, printing it). Asking whether a key is there, or walking the
keys, shows no value and is not noted. For each entry written in the class body we also
note where its key stands in the source (line and column), so that the selector can find
the entry a patch touches.

Written to $BR_ATTR.<pid>.json at exit:
  reads     attribute keys ("file::Class.Qualname::ATTR") read outside class creation
  wrapped   every instrumented key
  creation  class id -> list of [key read during its creation, [computed names set earlier]]
  prewrapped  class ids whose body attributes were instrumented before creation hooks ran
  computed  class id -> names that appeared on the class during creation (not in its body)
  late      keys of body attributes that could only be instrumented after class creation
  keyed     attribute key -> {"line:column" of an entry's key in the source: name of the key}
  keyreads  attribute key -> names of the dictionary keys looked up outside class creation
            ("*": every value was shown)
  keycreation  the same for look-ups while some class was being created
"""
import os
import sys

_OUT = os.environ.get("BR_ATTR")
if _OUT:
    import abc
    import atexit
    import enum
    import builtins
    import json
    import signal
    import types

    _ROOT = os.environ.get("BR_ROOT") or os.getcwd()
    _SRC = _ROOT + "/sqlglot/"
    _reads = set()
    _wrapped = set()
    _creation = {}
    _prewrapped = set()
    _computed = {}
    _creating = []
    _SKIP_TYPES = (types.FunctionType, classmethod, staticmethod, property, types.BuiltinFunctionType,
                   types.MethodType, types.MemberDescriptorType, types.GetSetDescriptorType)
    _setattr = type.__setattr__

    def _is_data(name, value):
        if name.startswith("__") and name.endswith("__"):
            return False
        if name == "_abc_impl" or isinstance(value, _SKIP_TYPES) or type(value) is _Tracker:
            return False
        return not (hasattr(type(value), "__get__") or hasattr(type(value), "__set_name__"))

    _keyed = {}        # attribute key -> {"line:col": key name}
    _keyreads = {}     # attribute key -> set of key names ("*": everything)
    _keycreation = {}

    def _key_name(k):
        """A name for a dictionary key that is the same in every process, or None."""
        tp = type(k)
        if tp is str:
            return "s:" + k
        if k is None or tp in (int, bool, float, bytes):
            return "c:%r" % (k,)
        if isinstance(k, type):
            return "t:%s.%s" % (k.__module__, k.__qualname__)
        if isinstance(k, enum.Enum):
            return "e:%s.%s.%s" % (tp.__module__, tp.__qualname__, k.name)
        if tp is tuple:
            parts = [_key_name(item) for item in k]
            return None if None in parts else "(" + ",".join(parts) + ")"
        return None

    class _KeyDict(dict):
        """A class-body dictionary that notes which of its values are looked at."""

        def _note(self, name):
            table = _keycreation if _creating else _keyreads
            table.setdefault(self._br, set()).add(name if name is not None else "*")

        def __getitem__(self, k):
            self._note(_key_name(k))
            return dict.__getitem__(self, k)

        def get(self, k, default=None):
            self._note(_key_name(k))
            return dict.get(self, k, default)

        # Walking the keys shows no value. __iter__ and keys are defined here all the same:
        # with them CPython copies this dictionary (dict(d), {**d}, f(**d), update) through
        # keys() and __getitem__, which we see, instead of reaching into it directly.
        def __iter__(self):
            return dict.__iter__(self)

        def keys(self):
            return dict.keys(self)

    def _shows_everything(name):
        real = getattr(dict, name)

        def method(self, *args, **kwargs):
            self._note("*")
            return real(self, *args, **kwargs)

        method.__name__ = name
        return method

    for _name in ("values", "items", "copy", "__or__", "__ror__", "__ior__", "__eq__", "__ne__", "__repr__",
                  "__reduce_ex__", "__reduce__", "setdefault", "pop", "popitem", "update", "__setitem__",
                  "__delitem__", "clear"):
        setattr(_KeyDict, _name, _shows_everything(_name))
    _KeyDict.__hash__ = None

    _tree_cache = {}

    def _class_node(filename, qualname):
        import ast

        tree = _tree_cache.get(filename)
        if tree is None:
            try:
                with open(filename, encoding="utf-8") as fh:
                    tree = ast.parse(fh.read())
            except Exception:
                tree = False
            _tree_cache[filename] = tree
        node = tree
        for part in qualname.split("."):
            if not node:
                return None
            node = next((n for n in node.body if isinstance(n, ast.ClassDef) and n.name == part), None)
        return node

    def _entries(filename, qualname, attr, value, globals_):
        """{"line:col" of each entry's key in the class body: key name}, for the entries of
        the dictionary display assigned to `attr` whose keys are plain names or constants."""
        import ast

        node = _class_node(filename, qualname)
        found = {}
        if node is None:
            return found
        display = None
        for statement in node.body:
            targets = statement.targets if isinstance(statement, ast.Assign) else (
                [statement.target] if isinstance(statement, ast.AnnAssign) and statement.value is not None else [])
            if any(isinstance(t, ast.Name) and t.id == attr for t in targets):
                display = statement.value if isinstance(statement.value, ast.Dict) else None
        if display is None:
            return found
        for key in display.keys:
            if key is None or not all(isinstance(n, (ast.Name, ast.Attribute, ast.Constant, ast.Tuple, ast.Load))
                                      for n in ast.walk(key)):
                continue
            try:
                k = eval(compile(ast.Expression(key), filename, "eval"), globals_, {})
            except Exception:
                continue
            name = _key_name(k)
            if name is not None and dict.__contains__(value, k):
                found["%d:%d" % (key.lineno, key.col_offset)] = name
        return found

    class _Tracker:
        __slots__ = ("name", "value", "key")

        def __init__(self, name, value, key, where=None):
            if type(value) is dict and where is not None:
                watched = _KeyDict(value)
                watched._br = key
                _keyed[key] = _entries(where[0], where[1], name, value, where[2])
                value = watched
            self.name, self.value, self.key = name, value, key

        def __get__(self, obj, objtype=None):
            if _creating:
                record = _creating[-1]
                earlier = ()
                if objtype is not None and objtype.__qualname__ == record["qualname"]:
                    body = record["body"]
                    earlier = [n for n, v in objtype.__dict__.items()
                               if not (n.startswith("__") and n.endswith("__")) and (n not in body or body[n] is not v)]
                record["reads"].add((self.key, tuple(sorted(earlier))))
            else:
                _reads.add(self.key)
            return self.value

    _late = set()  # attributes instrumented only after their class was created
    _string_cache = {}

    def _strings_in(filename):
        names = _string_cache.get(filename)
        if names is None:
            import ast

            names = set()
            try:
                with open(filename, encoding="utf-8") as fh:
                    for node in ast.walk(ast.parse(fh.read())):
                        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.isidentifier():
                            names.add(node.value)
            except Exception:
                names = None
            _string_cache[filename] = names
        return names if names is not None else _Everything()

    class _Everything:
        def __contains__(self, item):
            return True

    _build_class = builtins.__build_class__

    def _recording_build_class(func, name, *bases, **kwargs):
        filename = func.__code__.co_filename
        if not filename.startswith(_SRC) or "<locals>" in func.__qualname__:
            return _build_class(func, name, *bases, **kwargs)
        meta = kwargs.get("metaclass")
        resolved = types.resolve_bases(bases)
        if meta is None:
            meta = type(resolved[0]) if resolved else type
        if not isinstance(meta, type):
            return _build_class(func, name, *bases, **kwargs)
        meta = types._calculate_meta(meta, resolved)
        meta_file = getattr(sys.modules.get(meta.__module__), "__file__", "") or ""
        by_string = ()
        if meta in (type, abc.ABCMeta):
            prewrap = True
        elif meta_file.startswith(_SRC):
            # A sqlglot metaclass. It may fetch some attributes by name from the class
            # dictionary (which would hand it our descriptor instead of the value), so
            # every name that appears as a string in the metaclass's source file is left
            # alone until the class exists. All other attributes are instrumented first.
            prewrap = True
            by_string = _strings_in(meta_file)
        else:
            return _build_class(func, name, *bases, **kwargs)  # Enum, NamedTuple, Protocol, ...
        class_id = filename[len(_ROOT) + 1:] + "::" + func.__qualname__
        prefix = class_id + "::"
        where = (filename, func.__qualname__, func.__globals__)

        def proxy(name_, bases_, namespace, **kw):
            if prewrap:
                for attr, value in list(namespace.items()):
                    if attr in by_string:
                        _late.add(prefix + attr)
                        continue
                    if _is_data(attr, value):
                        key = prefix + attr
                        namespace[attr] = _Tracker(attr, value, key, where)
                        _wrapped.add(key)
            body = dict(namespace)  # the values as the class body left them (wrapped if prewrap)
            record = {"qualname": func.__qualname__, "body": body, "reads": set()}
            _creating.append(record)
            try:
                cls = meta(name_, bases_, namespace, **kw)
            finally:
                _creating.pop()
            added = []
            for attr, value in list(cls.__dict__.items()):
                if attr.startswith("__") and attr.endswith("__"):
                    continue
                if attr not in body or body[attr] is not value:
                    added.append(attr)  # set or replaced while the class was being created
                if type(value) is _Tracker or not _is_data(attr, value):
                    continue
                key = prefix + attr
                _setattr(cls, attr, _Tracker(attr, value, key, where if attr in body else None))
                _wrapped.add(key)
            if prewrap:
                _prewrapped.add(class_id)
            _computed.setdefault(class_id, set()).update(added)
            _creation.setdefault(class_id, set()).update(record["reads"])
            return cls

        prepare = getattr(meta, "__prepare__", None)
        if prepare is not None:
            proxy.__prepare__ = prepare
        kwargs["metaclass"] = proxy
        return _build_class(func, name, *bases, **kwargs)

    builtins.__build_class__ = _recording_build_class

    _done = []

    def _dump(*_):
        if _done:
            return
        _done.append(1)
        try:
            with open("%s.%d.json" % (_OUT, os.getpid()), "w") as fh:
                json.dump({
                    "reads": sorted(_reads), "wrapped": sorted(_wrapped),
                    "creation": {k: sorted([key, list(earlier)] for key, earlier in v) for k, v in _creation.items()},
                    "prewrapped": sorted(_prewrapped),
                    "computed": {k: sorted(v) for k, v in _computed.items()},
                    "late": sorted(_late),
                    "keyed": _keyed,
                    "keyreads": {k: sorted(v) for k, v in _keyreads.items()},
                    "keycreation": {k: sorted(v) for k, v in _keycreation.items()},
                }, fh)
        except Exception as error:
            print("attr recorder dump failed:", repr(error), file=sys.stderr)

    atexit.register(_dump)

    def _term(signum, frame):
        _dump()
        os._exit(143)

    try:
        signal.signal(signal.SIGTERM, _term)
    except Exception:
        pass
