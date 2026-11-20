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

Written to $BR_ATTR.<pid>.json at exit:
  reads     attribute keys ("file::Class.Qualname::ATTR") read outside class creation
  wrapped   every instrumented key
  creation  class id -> list of [key read during its creation, [computed names set earlier]]
  prewrapped  class ids whose body attributes were instrumented before creation hooks ran
  computed  class id -> names that appeared on the class during creation (not in its body)
"""
import os
import sys

_OUT = os.environ.get("BR_ATTR")
if _OUT:
    import abc
    import atexit
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

    class _Tracker:
        __slots__ = ("name", "value", "key")

        def __init__(self, name, value, key):
            self.name, self.value, self.key = name, value, key

        def __get__(self, obj, objtype=None):
            if _creating:
                record = _creating[-1]
                earlier = ()
                if objtype is not None and objtype.__qualname__ == record["qualname"]:
                    body = record["body"]
                    earlier = [n for n, v in objtype.__dict__.items()
                               if not (n.startswith("__") and n.endswith("__")) and (n not in body or body[n] is not v)]
                record["reads"].append([self.key, sorted(earlier)])
            else:
                _reads.add(self.key)
            return self.value

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
        if meta in (type, abc.ABCMeta):
            prewrap = True
        elif meta_file.startswith(_SRC):
            prewrap = False
        else:
            return _build_class(func, name, *bases, **kwargs)  # Enum, NamedTuple, Protocol, ...
        class_id = filename[len(_ROOT) + 1:] + "::" + func.__qualname__
        prefix = class_id + "::"

        def proxy(name_, bases_, namespace, **kw):
            if prewrap:
                for attr, value in list(namespace.items()):
                    if _is_data(attr, value):
                        key = prefix + attr
                        namespace[attr] = _Tracker(attr, value, key)
                        _wrapped.add(key)
            body = dict(namespace)  # the values as the class body left them (wrapped if prewrap)
            record = {"qualname": func.__qualname__, "body": body, "reads": []}
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
                _setattr(cls, attr, _Tracker(attr, value, key))
                _wrapped.add(key)
            if prewrap:
                _prewrapped.add(class_id)
            _computed.setdefault(class_id, set()).update(added)
            _creation.setdefault(class_id, []).extend(record["reads"])
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
                    "creation": _creation, "prewrapped": sorted(_prewrapped),
                    "computed": {k: sorted(v) for k, v in _computed.items()},
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
