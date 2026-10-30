"""Route the OpenAI and Anthropic SDKs' calls to OpenPort's swapped client while an eval case runs.

The eval harness can also swap the module attribute a call gets its client from, but only when the client is
reachable at import. Code that builds its client in a function, keeps it on `self`, or gets it from a factory
can't be swapped that way. Intercepting the SDK's own resource methods can: while a case runs, every
`chat.completions.create(...)` or `messages.create(...)`, sync or async, on any client goes to the swapped
client. Outside a case the patch does nothing, and calls from OpenPort's own Token Factory client pass straight
through. A client the app's code hands to a thread it starts itself isn't routed (the route lives in the case's
context), but its call can't reach the closed provider either: that is refused for the whole process while a case runs.
"""

import functools
import importlib
import inspect
import threading
import weakref
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlsplit

ROUTE: ContextVar[object | None] = ContextVar("openport_route", default=None)
PASSTHROUGH: "weakref.WeakSet" = weakref.WeakSet()  # OpenPort's own clients: never routed
# For plain HTTP calls: the eval proxy's URL for the running case (…/case/<id>/v1). A request to an LLM endpoint
# goes there instead, so httpx and requests calls are tested like SDK calls. The proxy forwards from its own
# thread, where this isn't set, so its requests to Token Factory go out as they are.
HTTP_BASE: ContextVar[str | None] = ContextVar("openport_http_base", default=None)
_http_installed = False
# While any case runs, nothing in this process may reach the closed providers themselves: a call OpenPort can't route
# (the Responses API, a beta or streaming method, a client used from the app's own thread) would otherwise be answered
# by GPT or Claude and graded as Nemotron, on the user's real key. Such a call fails instead, and stays where it is. It's
# checked on every request the HTTP clients send, redirects included, in the httpx2 the SDKs use and in httpx.
PROVIDER_DOMAINS = ("openai.com", "anthropic.com", "openai.azure.com")
OWN: ContextVar[bool] = ContextVar("openport_own_request", default=False)  # OpenPort's swapped client forwarding a call
_cases_running = 0
_cases_lock = threading.Lock()


class ProviderBlocked(RuntimeError):
    """An eval case tried to call a closed model's own API."""


def _refuse_providers(url: str) -> None:
    if not _cases_running or OWN.get():
        return
    host = (urlsplit(str(url)).hostname or "").lower().rstrip(".")
    if any(host == domain or host.endswith("." + domain) for domain in PROVIDER_DOMAINS):
        raise ProviderBlocked(f"A test case tried to reach {host}: OpenPort routes only the calls it can send to Token Factory, "
                              f"so this one isn't tested on Nemotron")


@contextmanager
def _case_running():
    global _cases_running
    with _cases_lock:
        _cases_running += 1
    try:
        yield
    finally:
        with _cases_lock:
            _cases_running -= 1


def _guarded(original, is_async: bool):
    """A client's send of one request (each redirect is one more), refused when it's for a closed provider."""
    if is_async:
        @functools.wraps(original)
        async def method(self, request, *args, **kwargs):
            _refuse_providers(request.url)
            return await original(self, request, *args, **kwargs)
    else:
        @functools.wraps(original)
        def method(self, request, *args, **kwargs):
            _refuse_providers(request.url)
            return original(self, request, *args, **kwargs)
    method.__openport_original__ = original
    return method
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
            token, own = ROUTE.set(None), OWN.set(True)  # the swapped client calls the real SDK in turn
            try:
                result = _resolve(target, path)(*args, **kwargs)
                return await result if inspect.isawaitable(result) else result
            finally:
                OWN.reset(own)
                ROUTE.reset(token)
    else:
        @functools.wraps(original)
        def method(self, *args, **kwargs):
            target = target_for(self)
            if target is None:
                return original(self, *args, **kwargs)
            token, own = ROUTE.set(None), OWN.set(True)
            try:
                return _resolve(target, path)(*args, **kwargs)
            finally:
                OWN.reset(own)
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


def redirected(url: str) -> str | None:
    """Where a request to `url` goes during a case: the eval proxy, when it's an LLM endpoint; else None."""
    from ..scan.findings import http_endpoint

    base = HTTP_BASE.get()
    if base is None or url.startswith(base.rsplit("/case/", 1)[0]):
        return None
    api = http_endpoint(url)
    if api == "chat.completions" and "/chat/completions" in url:
        return f"{base}/chat/completions"
    if api == "messages":
        return f"{base}/messages"
    return None


def install_http() -> None:
    """Patch the HTTP clients' send once, so a case's requests to LLM endpoints reach the eval proxy, and none reaches
    a closed provider."""
    global _http_installed
    with _lock:
        if _http_installed:
            return
        for module_name in ("httpx", "httpx2"):  # httpx2 is what the OpenAI and Anthropic SDKs send with
            try:
                module = importlib.import_module(module_name)
            except ImportError:
                continue
            for cls in (module.Client, module.AsyncClient):
                original = cls.send

                def send(self, request, *args, _original=original, _url=module.URL, **kwargs):
                    target = redirected(str(request.url))
                    if target is not None:
                        request.url = _url(target)
                    return _original(self, request, *args, **kwargs)

                cls.send = send
                # Each request the client sends, redirects included, is checked against the closed providers.
                single = cls.__dict__.get("_send_single_request")
                if single is not None:
                    cls._send_single_request = _guarded(single, is_async=cls is module.AsyncClient)
        try:
            import requests

            original_send = requests.Session.send

            def session_send(self, request, **kwargs):
                target = redirected(request.url)
                if target is not None:
                    request.url = target
                else:
                    _refuse_providers(request.url)
                return original_send(self, request, **kwargs)

            requests.Session.send = session_send
        except ImportError:
            pass
        try:
            import aiohttp

            original_request = aiohttp.ClientSession._request

            def session_request(self, method, str_or_url, *args, **kwargs):
                target = redirected(str(str_or_url))
                if target is None:
                    _refuse_providers(str(str_or_url))
                return original_request(self, method, target or str_or_url, *args, **kwargs)

            aiohttp.ClientSession._request = session_request
        except ImportError:
            pass
        _http_installed = True


@contextmanager
def http_routed(base: str):
    """While inside, requests to LLM endpoints go to the eval proxy at `base` (…/case/<id>/v1)."""
    install_http()
    token = HTTP_BASE.set(base)
    try:
        with _case_running():
            yield
    finally:
        HTTP_BASE.reset(token)


def _wrapped(client: object | None) -> list[object]:
    """A client and every client it wraps. Tuning hands the runner a wrapper (VariantClient) whose `client` is
    the real SDK client; that one's calls are OpenPort's own too. Marking only the wrapper sent the inner call
    back to the swapped client: for code on Anthropic's SDK, whose shim never resets the route, every case
    failed before reaching a model (Oct 28)."""
    chain: list[object] = []
    while client is not None and all(client is not seen for seen in chain):
        chain.append(client)
        client = getattr(client, "client", None)
    return chain


@contextmanager
def routed(swapped: object, passthrough: object | None = None):
    """While inside, the SDKs' calls go to `swapped`; `passthrough` (the client `swapped` calls), and any client it
    wraps, goes to Token Factory."""
    install()
    install_http()  # a call the SDK patch doesn't cover still goes out over HTTP, where the providers are refused
    for client in _wrapped(passthrough):
        try:
            PASSTHROUGH.add(client)
        except TypeError:  # not weakly referenceable: it can't be one of the SDK's clients either
            pass
    token = ROUTE.set(swapped)
    try:
        with _case_running():
            yield
    finally:
        ROUTE.reset(token)
