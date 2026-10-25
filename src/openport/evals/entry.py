"""What an eval calls: a module's function, or a method of one of its classes.

A call inside a method (`Assistant.reply`) is tested on an instance of its class built for each case, so
cases running side by side never share an instance's state. The class is built with no arguments unless the
spec gives some (`init:`, keyword arguments as a mapping or positional ones as a list); static and class
methods are called on the class itself.
"""

import inspect
from typing import Any, Callable


def entry_callable(module: Any, name: str, init: dict | list | None = None) -> Callable:
    owner_name, _, method = name.rpartition(".")
    if not owner_name:
        return getattr(module, name)
    owner = module
    for part in owner_name.split("."):
        owner = getattr(owner, part)
    if not inspect.isclass(owner) or isinstance(inspect.getattr_static(owner, method), (staticmethod, classmethod)):
        return getattr(owner, method)
    args, kwargs = (list(init), {}) if isinstance(init, list) else ([], dict(init or {}))

    def call(*call_args, **call_kwargs):
        return getattr(owner(*args, **kwargs), method)(*call_args, **call_kwargs)

    call.__name__ = call.__qualname__ = name
    return call
