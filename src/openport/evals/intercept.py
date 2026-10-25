"""Route the OpenAI and Anthropic SDKs' calls to OpenPort's swapped client while an eval case runs.

The eval harness can also swap the module attribute a call gets its client from, but only when the client is
reachable at import. Code that builds its client in a function, keeps it on `self`, or gets it from a factory
can't be swapped that way. Intercepting the SDK's own resource methods can: while a case runs, every
`chat.completions.create(...)` or `messages.create(...)`, sync or async, on any client goes to the swapped
client. Outside a case the patch does nothing, and calls from OpenPort's own Token Factory client pass straight
through. A client the app's code hands to a thread it starts itself isn't routed: the route lives in the case's
context.
"""

import functools
import importlib
import inspect
import threading
import weakref
from contextlib import contextmanager
from contextvars import ContextVar

ROUTE: ContextVar[object | None] = ContextVar("openport_route", default=None)
PASSTHROUGH: "weakref.WeakSet" = weakref.WeakSet()  # OpenPort's own clients: never routed
# (module, class, method, where the same call lives on the swapped client)
TARGETS = [
    ("openai.resources.chat.completions", "Completions", "create", ("chat", "completions", "create")),
    ("openai.resources.chat.completions", "Completions", "parse", ("chat", "completions", "parse")),
    ("openai.resources.chat.completions", "AsyncCompletions", "create", ("chat", "completions", "create")),
    ("openai.resources.chat.completions", "AsyncCompletions", "parse", ("chat", "completions", "parse")),
    ("openai.resources.beta.chat.completions", "Completions", "parse", ("beta", "chat", "completions", "parse")),
    ("openai.resources.beta.chat.completions", "AsyncCompletions", "parse", ("beta", "chat", "completions", "parse")),
    ("anthropic.resources.messages", "Messages", "create", ("messages", "create")),
    ("anthropic.resources.messages", "AsyncMessages", "create", ("messages", "create")),
]
_lock = threading.Lock()
_installed = False


def _resolve(target: object, path: tuple[str, ...]):
    for name in path:
        target = getattr(target, name)
    return target


def _routed(original, path: tuple[str, ...], is_async: bool):
    def target_for(resource) -> object | None:
        target = ROUTE.get()
        return None if target is None or getattr(resource, "_client", None) in PASSTHROUGH else target

    # The SDKs wrap their async methods in a sync decorator, so the class says which they are, not inspect.
    if is_async:
        @functools.wraps(original)
        async def method(self, *args, **kwargs):
            target = target_for(self)
            if target is None:
                return await original(self, *args, **kwargs)
            token = ROUTE.set(None)  # the swapped client calls the real SDK in turn
            try:
                result = _resolve(target, path)(*args, **kwargs)
                return await result if inspect.isawaitable(result) else result
            finally:
                ROUTE.reset(token)
    else:
        @functools.wraps(original)
        def method(self, *args, **kwargs):
            target = target_for(self)
            if target is None:
                return original(self, *args, **kwargs)
            token = ROUTE.set(None)
            try:
                return _resolve(target, path)(*args, **kwargs)
            finally:
                ROUTE.reset(token)
    method.__openport_original__ = original
    return method


def install() -> None:
    """Patch the SDK methods once; an SDK or method this version doesn't have is skipped."""
    global _installed
    with _lock:
        if _installed:
            return
        for module_name, class_name, method_name, path in TARGETS:
            try:
                cls = getattr(importlib.import_module(module_name), class_name)
            except (ImportError, AttributeError):
                continue
            original = cls.__dict__.get(method_name)
            if original is not None and not hasattr(original, "__openport_original__"):
                setattr(cls, method_name, _routed(original, path, is_async=class_name.startswith("Async")))
        _installed = True


@contextmanager
def routed(swapped: object, passthrough: object | None = None):
    """While inside, the SDKs' calls go to `swapped`; `passthrough` (the client `swapped` calls) goes to Token Factory."""
    install()
    if passthrough is not None:
        try:
            PASSTHROUGH.add(passthrough)
        except TypeError:  # not weakly referenceable: it can't be one of the SDK's clients either
            pass
    token = ROUTE.set(swapped)
    try:
        yield
    finally:
        ROUTE.reset(token)
