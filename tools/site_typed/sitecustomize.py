"""Recorder for typed dictionaries: which tests look at what `total=` decides.

Loaded into every Python process started while one test runs; active when BR_TYPED is set.

The header `class X(TypedDict, total=...)` hands `total` to typing's metaclass, which uses
it for exactly three things: the class attributes `__total__`, `__required_keys__` and
`__optional_keys__` (a subclass computes its own three from those of its bases while it
is created). Nothing else about the class depends on it. So a test depends on the flag
only if something reads one of those attributes, or reads the class's whole `__dict__`.

The metaclass gets a `__getattribute__` that notes such reads, with the place they come
from. Reads made by the metaclass itself while it creates a class are not noted: they
only feed the three attributes of the new class, which are watched in turn.

Written to $BR_TYPED.<pid>.json at exit:
  classes  ["file::Qualname", ...] typed dictionaries defined at the top of a module of
           the repository, as far as this process imported them
  reads    [[class, attribute, reader file, reader line], ...] for every typed dictionary,
           also those defined elsewhere ("(module)::Qualname")
"""
import os
import sys

_OUT = os.environ.get("BR_TYPED")
if _OUT:
    import atexit
    import json
    import signal
    import typing

    _ROOT = (os.environ.get("BR_ROOT") or os.getcwd()).rstrip("/") + "/"
    _META = typing._TypedDictMeta
    _WATCHED = frozenset(("__total__", "__required_keys__", "__optional_keys__", "__dict__"))
    _TYPING = typing.__file__
    _plain = type.__getattribute__
    _reads = set()

    def _name(cls):
        # "file::Qualname" for a class of the repository, "(module)::Qualname" for any other
        name = _plain(cls, "__module__")
        filename = getattr(sys.modules.get(name), "__file__", None) or ""
        where = filename[len(_ROOT):] if filename.startswith(_ROOT) else "(%s)" % name
        return "%s::%s" % (where, _plain(cls, "__qualname__"))

    def __getattribute__(cls, attribute):
        if attribute in _WATCHED:
            frame = sys._getframe(1)
            code = frame.f_code
            if not (code.co_filename == _TYPING and code.co_name == "__new__"):
                _reads.add((_name(cls), attribute, code.co_filename, frame.f_lineno))
        return _plain(cls, attribute)

    _META.__getattribute__ = __getattribute__

    _done = []

    def _dump(*_):
        if _done:
            return
        _done.append(1)
        try:
            classes = set()
            for module in list(sys.modules.values()):
                filename = getattr(module, "__file__", None) or ""
                if not filename.startswith(_ROOT):
                    continue
                for value in list(vars(module).values()):
                    if type(value) is _META and _plain(value, "__module__") == module.__name__:
                        classes.add(_name(value))
            with open("%s.%d.json" % (_OUT, os.getpid()), "w") as fh:
                json.dump({"classes": sorted(classes), "reads": sorted(_reads)}, fh)
        except Exception as error:
            print("typed recorder dump failed:", repr(error), file=sys.stderr)

    atexit.register(_dump)

    def _term(signum, frame):
        _dump()
        os._exit(143)

    try:
        signal.signal(signal.SIGTERM, _term)
    except Exception:
        pass
