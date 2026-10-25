"""Find OpenAI and Anthropic SDK calls in Python source, using the standard `ast` module, and plain HTTP calls
(requests, httpx, aiohttp) to the same endpoints.

The scanner only parses files. It never imports or runs the code it reads.
"""

import ast

from .findings import (
    ANTHROPIC_CLIENTS,
    CALL_SUFFIXES,
    OPENAI_CLIENTS,
    CallSiteFinding,
    features_for_api,
    http_endpoint,
    http_features,
    infer_output,
)

SDK_MODULES = {"openai": "openai", "anthropic": "anthropic"}
HTTP_METHODS = {"post": 0, "request": 1, "stream": 1}  # method -> where its URL is among the positional arguments


def _constructor_sdk(node: ast.AST) -> str | None:
    """'openai' / 'anthropic' if the node is a client constructor call like OpenAI() or anthropic.Anthropic()."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
    if name in OPENAI_CLIENTS:
        return "openai"
    if name in ANTHROPIC_CLIENTS:
        return "anthropic"
    return None


def _match_suffix(func: ast.AST) -> tuple[str, str, ast.AST] | None:
    """For `x.chat.completions.create`, return ("openai", "chat.completions", <x>)."""
    names = []
    node = func
    while isinstance(node, ast.Attribute):
        names.insert(0, node.attr)
        node = node.value
    for suffix, sdk, api in CALL_SUFFIXES:
        if tuple(names[-len(suffix):]) == suffix and len(names) >= len(suffix):
            receiver = func
            for _ in suffix:
                receiver = receiver.value
            return sdk, api, receiver
    return None


def _module_name(relative_path: str) -> str:
    parts = relative_path[: -len(".py")].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if parts and parts[0] == "src":
        parts = parts[1:]
    return ".".join(parts)


class _FileScanner(ast.NodeVisitor):
    def __init__(self, tree: ast.Module, relative_path: str, in_tests: bool):
        self.path = relative_path
        self.in_tests = in_tests
        self.findings: list[CallSiteFinding] = []
        self.scope: list[ast.AST] = []  # enclosing classes and functions

        self.imported_sdks: set[str] = set()
        self.imported_names: set[str] = set()
        self.constants: dict[str, ast.AST] = {}  # module-level NAME = value
        self.module_clients: dict[str, tuple[str, bool]] = {}  # name -> (sdk, has base_url)
        for statement in tree.body:
            self._index_module_statement(statement)
        self.local_clients: dict[int, dict[str, tuple[str, bool]]] = {}  # id(function) -> name -> (sdk, base_url)
        # `@app.post("/v1/chat/completions")` serves the endpoint rather than calling it.
        self.decorators = {id(d) for node in ast.walk(tree) if hasattr(node, "decorator_list") for d in node.decorator_list}

    # --- module-level indexing -------------------------------------------------------------------

    def _index_module_statement(self, statement: ast.stmt) -> None:
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                root = alias.name.split(".")[0]
                if root in SDK_MODULES:
                    self.imported_sdks.add(root)
        elif isinstance(statement, ast.ImportFrom) and statement.module:
            root = statement.module.split(".")[0]
            if root in SDK_MODULES:
                self.imported_sdks.add(root)
            else:
                self.imported_names.update(alias.asname or alias.name for alias in statement.names)
        elif isinstance(statement, (ast.Assign, ast.AnnAssign)) and statement.value is not None:
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    self.constants[target.id] = statement.value
                    sdk = _constructor_sdk(statement.value)
                    if sdk:
                        self.module_clients[target.id] = (sdk, _has_kwarg(statement.value, "base_url"))
        elif isinstance(statement, (ast.If, ast.Try)):
            for child in statement.body:
                self._index_module_statement(child)

    # --- scope tracking --------------------------------------------------------------------------

    def _visit_scope(self, node: ast.AST) -> None:
        self.scope.append(node)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            clients = {}
            for inner in ast.walk(node):
                if isinstance(inner, ast.Assign) and len(inner.targets) == 1 and isinstance(inner.targets[0], ast.Name):
                    sdk = _constructor_sdk(inner.value)
                    if sdk:
                        clients[inner.targets[0].id] = (sdk, _has_kwarg(inner.value, "base_url"))
            self.local_clients[id(node)] = clients
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _visit_scope

    def _function_name(self) -> str:
        names = [node.name for node in self.scope if hasattr(node, "name")]
        return ".".join(names) if names else "<module>"

    def _current_function(self) -> ast.AST | None:
        for node in reversed(self.scope):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return node
        return None

    # --- calls -----------------------------------------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        match = _match_suffix(node.func)
        if match:
            sdk, api, receiver = match
            finding = self._finding(node, sdk, api, receiver)
        else:
            finding = self._http_finding(node)
        if finding:
            self.findings.append(finding)
        self.generic_visit(node)

    def _assigned(self, names: set[str]) -> list[ast.AST]:
        """The values assigned to these names: module constants, and assignments in the enclosing function."""
        values = [self.constants[name] for name in names if name in self.constants]
        function = self._current_function()
        if function is not None:
            values += [node.value for node in ast.walk(function)
                       if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in node.targets)]
        return values

    def _http_finding(self, call: ast.Call) -> CallSiteFinding | None:
        """`requests.post(url, json=payload)`, `client.post(...)`, `session.request("POST", url, ...)`, where the URL
        (or what it's built from) names an LLM endpoint such as /chat/completions or api.anthropic.com."""
        func = call.func
        if not isinstance(func, ast.Attribute) or func.attr not in HTTP_METHODS or id(call) in self.decorators:
            return None
        kwargs = {kw.arg: kw.value for kw in call.keywords if kw.arg}
        position = HTTP_METHODS[func.attr]
        if position:  # request("POST", url) and stream("POST", url)
            method = kwargs.get("method") or (call.args[0] if call.args else None)
            if not (isinstance(method, ast.Constant) and str(method.value).upper() == "POST"):
                return None
        url = kwargs.get("url") or (call.args[position] if len(call.args) > position else None)
        if url is None:
            return None
        names = {n.id for n in ast.walk(url) if isinstance(n, ast.Name)}
        api = http_endpoint(" ".join(ast.unparse(value) for value in [url, *self._assigned(names)]))
        if api is None:
            return None

        payload = kwargs.get("json")
        body = kwargs.get("data") or kwargs.get("content")
        if payload is None and isinstance(body, ast.Call) and body.args and ast.unparse(body.func).endswith("dumps"):
            payload = body.args[0]  # data=json.dumps({...})
        if isinstance(payload, ast.Name):
            payload = next(iter(reversed(self._assigned({payload.id}))), payload)
        keys = {k.value: v for k, v in zip(payload.keys, payload.values) if isinstance(k, ast.Constant)} if isinstance(payload, ast.Dict) else {}

        features = http_features(api)
        searchable = [payload] if payload is not None else []
        if "tools" in keys or "functions" in keys:
            features.append("tools")
        structured_kind = None
        if "response_format" in keys:
            structured_kind = "json_object" if "json_object" in ast.unparse(keys["response_format"]) else "json_schema"
            features.append("json_mode" if structured_kind == "json_object" else "structured_output")
        if {"thinking", "reasoning_effort", "reasoning"} & keys.keys():
            features.append("thinking")
        if _is_true(keys.get("stream")):
            features.append("streaming")
        if _mentions(searchable, {"image_url", "input_image"}) or _has_type(searchable, "image"):
            features.append("vision")

        model, model_expr = None, None
        model_node = keys.get("model")
        if isinstance(model_node, ast.Constant) and isinstance(model_node.value, str):
            model = model_node.value
        elif model_node is not None:
            model_expr = ast.unparse(model_node)
            resolved = self.constants.get(model_node.id) if isinstance(model_node, ast.Name) else None
            if isinstance(resolved, ast.Constant) and isinstance(resolved.value, str):
                model = resolved.value

        function = self._current_function()
        return CallSiteFinding(
            file=self.path,
            line=call.lineno,
            language="python",
            function=self._function_name(),
            sdk="http",
            api=api,
            receiver=ast.unparse(func.value),
            receiver_kind="http",
            model=model,
            model_expr=model_expr,
            output=infer_output(api, features, structured_kind, function is not None and _parses_json(function, call)),
            features=features,
            in_tests=self.in_tests,
            module=_module_name(self.path),
        )

    def _receiver_kind(self, receiver: ast.AST) -> tuple[str, str | None, bool]:
        """(kind, sdk the receiver was built with, whether that client sets a base_url)."""
        if isinstance(receiver, ast.Name):
            function = self._current_function()
            local = self.local_clients.get(id(function), {}) if function is not None else {}
            if receiver.id in local:
                return ("local", *local[receiver.id])
            if receiver.id in self.module_clients:
                return ("module_global", *self.module_clients[receiver.id])
            if receiver.id in self.imported_names:
                return "imported", None, False
            return "other", None, False
        if isinstance(receiver, ast.Attribute):
            return "attribute", None, False
        if isinstance(receiver, ast.Call):
            return "factory", _constructor_sdk(receiver), _has_kwarg(receiver, "base_url")
        return "other", None, False

    def _finding(self, call: ast.Call, sdk: str, api: str, receiver: ast.AST) -> CallSiteFinding | None:
        kwargs = {kw.arg: kw.value for kw in call.keywords if kw.arg}
        kind, client_sdk, base_url = self._receiver_kind(receiver)

        # Is this really an LLM SDK call? `client.messages.create(...)` is also Twilio's API.
        evidence = "model" in kwargs and (sdk == "openai" or "max_tokens" in kwargs)
        if client_sdk is not None:
            if client_sdk != sdk:
                return None
        elif isinstance(receiver, ast.Name) and receiver.id in self.constants:
            if not evidence:  # some other module-level object, like a Twilio client
                return None
        elif not (sdk in self.imported_sdks or evidence):
            return None

        features = features_for_api(sdk, api)
        # Look through variables the call uses too (`content = [{"type": "image_url", ...}]` before the call).
        function = self._current_function()
        names = {n.id for arg in [*call.args, *kwargs.values()] for n in ast.walk(arg) if isinstance(n, ast.Name)}
        resolved = [self.constants[name] for name in names if name in self.constants]
        if function is not None:
            resolved += [
                node.value
                for node in ast.walk(function)
                if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in node.targets)
            ]
        searchable = [call, *resolved]

        if "tools" in kwargs or "functions" in kwargs:
            features.append("tools")
        structured_kind = "json_schema" if api.endswith("parse") else None
        response_format = kwargs.get("response_format")
        if response_format is not None:
            text = ast.unparse(response_format)
            structured_kind = "json_object" if "json_object" in text else "json_schema"
        if "output_config" in kwargs or "output_format" in kwargs or "text" in kwargs and api.startswith("responses"):
            structured_kind = structured_kind or "json_schema"
        if structured_kind == "json_object":
            features.append("json_mode")
        elif structured_kind and "structured_output" not in features:
            features.append("structured_output")
        if "thinking" in kwargs or "reasoning_effort" in kwargs or "reasoning" in kwargs:
            features.append("thinking")
        if _is_true(kwargs.get("stream")) and "streaming" not in features and "anthropic_streaming" not in features:
            features.append("anthropic_streaming" if sdk == "anthropic" else "streaming")
        if _mentions(searchable, {"image_url", "input_image"}) or _has_type(searchable, "image"):
            features.append("vision")
        if _mentions(searchable, {"cache_control"}):
            features.append("prompt_caching")
        if _ends_with_assistant(kwargs.get("messages"), self.constants):
            features.append("prefill" if sdk == "anthropic" else "ends_on_assistant")
        if any(kw.arg is None for kw in call.keywords) or (call.args and not kwargs):
            features.append("dynamic_kwargs")
        if base_url:
            features.append("base_url_configurable")

        model, model_expr = None, None
        model_node = kwargs.get("model")
        if isinstance(model_node, ast.Constant) and isinstance(model_node.value, str):
            model = model_node.value
        elif model_node is not None:
            model_expr = ast.unparse(model_node)
            if isinstance(model_node, ast.Name):
                value = self.constants.get(model_node.id)
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    model = value.value

        parses_json = function is not None and _parses_json(function, call)

        return CallSiteFinding(
            file=self.path,
            line=call.lineno,
            language="python",
            function=self._function_name(),
            sdk=sdk,
            api=api,
            receiver=ast.unparse(receiver),
            receiver_kind=kind,
            model=model,
            model_expr=model_expr,
            output=infer_output(api, features, structured_kind, parses_json),
            features=features,
            in_tests=self.in_tests,
            module=_module_name(self.path),
        )


def _has_kwarg(call: ast.Call, name: str) -> bool:
    return any(kw.arg == name for kw in call.keywords)


def _is_true(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _mentions(nodes: list[ast.AST], words: set[str]) -> bool:
    return any(isinstance(n, ast.Constant) and n.value in words for root in nodes for n in ast.walk(root))


def _has_type(nodes: list[ast.AST], value: str) -> bool:
    """Is there a dict literal like {"type": "image", ...} anywhere in these nodes?"""
    for root in nodes:
        for n in ast.walk(root):
            if isinstance(n, ast.Dict):
                for key, val in zip(n.keys, n.values):
                    if isinstance(key, ast.Constant) and key.value == "type" and isinstance(val, ast.Constant) and val.value == value:
                        return True
    return False


def _ends_with_assistant(messages: ast.AST | None, constants: dict[str, ast.AST]) -> bool:
    if isinstance(messages, ast.Name):
        messages = constants.get(messages.id)
    if not isinstance(messages, ast.List) or not messages.elts or not isinstance(messages.elts[-1], ast.Dict):
        return False
    last = messages.elts[-1]
    return any(
        isinstance(k, ast.Constant) and k.value == "role" and isinstance(v, ast.Constant) and v.value == "assistant"
        for k, v in zip(last.keys, last.values)
    )


def _is_json_parser(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        name = func.id
        if name == "loads":  # from json import loads
            return True
    elif isinstance(func, ast.Attribute):
        name = func.attr
        if name == "loads":
            return isinstance(func.value, ast.Name) and func.value.id in ("json", "orjson", "ujson", "simplejson")
    else:
        return False
    lowered = name.lower()
    return name in ("model_validate_json", "parse_raw") or (
        "json" in lowered and any(verb in lowered for verb in ("load", "parse", "extract", "decode", "read"))
    )


def _names_in(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _contains(root: ast.AST, target: ast.AST) -> bool:
    return any(n is target for n in ast.walk(root))


def _parses_json(function: ast.AST, call: ast.Call) -> bool:
    """Does this call's result reach a JSON parser, directly or through variables in the same function?

    Data flow stops at other LLM calls, so a later call's JSON parsing isn't credited to this one.
    """
    flows: list[tuple[ast.AST, set[str]]] = []  # (value, names it's assigned to)
    for node in ast.walk(function):
        if isinstance(node, ast.Assign):
            flows.append((node.value, set().union(*(_names_in(t) for t in node.targets))))
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and node.value is not None:
            flows.append((node.value, _names_in(node.target)))
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            flows.append((node.iter, _names_in(node.target)))

    parsers = [n for n in ast.walk(function) if isinstance(n, ast.Call) and _is_json_parser(n)]
    if any(_contains(arg, call) for parser in parsers for arg in parser.args):
        return True

    tracked = set().union(*(targets for value, targets in flows if _contains(value, call)))
    other_llm_calls = [
        value for value, _ in flows
        if not _contains(value, call)
        and any(isinstance(n, ast.Call) and _match_suffix(n.func) for n in ast.walk(value))
    ]
    changed = bool(tracked)
    while changed:
        changed = False
        for value, targets in flows:
            if value in other_llm_calls or targets <= tracked:
                continue
            if _names_in(value) & tracked:
                tracked |= targets
                changed = True
    return any(_names_in(arg) & tracked for parser in parsers for arg in parser.args)


def scan_python(source: str, relative_path: str, in_tests: bool) -> list[CallSiteFinding]:
    tree = ast.parse(source)
    scanner = _FileScanner(tree, relative_path, in_tests)
    scanner.visit(tree)
    return scanner.findings
