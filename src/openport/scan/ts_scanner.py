"""Find OpenAI and Anthropic SDK calls in TypeScript and JavaScript, using tree-sitter.

The scanner only parses files. It never runs the code it reads.
"""

import re
from functools import cache

import tree_sitter_typescript as tsts
from tree_sitter import Language, Node, Parser

from .findings import ANTHROPIC_CLIENTS, CALL_SUFFIXES, OPENAI_CLIENTS, CallSiteFinding, features_for_api, infer_output

SDK_PACKAGES = {"openai": "openai", "@anthropic-ai/sdk": "anthropic"}
_FUNCTION_TYPES = {"function_declaration", "method_definition", "arrow_function", "function_expression", "generator_function_declaration"}


@cache
def _parser(tsx: bool) -> Parser:
    return Parser(Language(tsts.language_tsx() if tsx else tsts.language_typescript()))


def _text(node: Node | None) -> str:
    return node.text.decode("utf-8", errors="replace") if node is not None else ""


def _unquote(text: str) -> str:
    return text.strip().strip("'\"`")


def _walk(node: Node):
    yield node
    for child in node.named_children:
        yield from _walk(child)


class _FileScanner:
    def __init__(self, root: Node, relative_path: str, in_tests: bool):
        self.root = root
        self.path = relative_path
        self.in_tests = in_tests
        self.imported_sdks: set[str] = set()
        self.declarations: dict[str, Node] = {}  # const/let NAME = value (last one wins)
        self.clients: dict[str, tuple[str, bool]] = {}  # variable or this.field -> (sdk, has baseURL)
        self._index()

    def _index(self) -> None:
        for node in _walk(self.root):
            if node.type == "import_statement":
                source = _unquote(_text(node.child_by_field_name("source")))
                if source in SDK_PACKAGES:
                    self.imported_sdks.add(SDK_PACKAGES[source])
            elif node.type == "call_expression" and _text(node.child_by_field_name("function")) == "require":
                source = _unquote(_text(node.child_by_field_name("arguments")).strip("()"))
                if source in SDK_PACKAGES:
                    self.imported_sdks.add(SDK_PACKAGES[source])
            elif node.type == "variable_declarator":
                name, value = node.child_by_field_name("name"), node.child_by_field_name("value")
                if name is not None and value is not None and name.type == "identifier":
                    self.declarations[_text(name)] = value
                    self._note_client(_text(name), value)
            elif node.type == "assignment_expression":
                left, value = node.child_by_field_name("left"), node.child_by_field_name("right")
                if left is not None and value is not None:
                    self._note_client(_text(left), value)
            elif node.type == "public_field_definition":  # class field: `client = new OpenAI()`
                name, value = node.child_by_field_name("name"), node.child_by_field_name("value")
                if name is not None and value is not None:
                    self._note_client(f"this.{_text(name)}", value)

    def _note_client(self, name: str, value: Node) -> None:
        if value.type == "await_expression" and value.named_children:
            value = value.named_children[0]
        if value.type != "new_expression":
            return
        constructor = _text(value.child_by_field_name("constructor")).split(".")[-1]
        sdk = "openai" if constructor in OPENAI_CLIENTS else "anthropic" if constructor in ANTHROPIC_CLIENTS else None
        if sdk:
            self.clients[name] = (sdk, "baseURL" in _text(value.child_by_field_name("arguments")))

    def scan(self) -> list[CallSiteFinding]:
        findings = []
        for node in _walk(self.root):
            if node.type == "call_expression":
                finding = self._finding(node)
                if finding:
                    findings.append(finding)
        return findings

    def _finding(self, call: Node) -> CallSiteFinding | None:
        function = call.child_by_field_name("function")
        if function is None or function.type != "member_expression":
            return None
        parts = [p for p in re.sub(r"\s+|\?|!", "", _text(function)).split(".") if p]
        match = next(((sdk, api, s) for s, sdk, api in CALL_SUFFIXES if len(parts) > len(s) and tuple(parts[-len(s):]) == s), None)
        if match is None:
            return None
        sdk, api, suffix = match
        receiver = ".".join(parts[: -len(suffix)])

        args = call.child_by_field_name("arguments")
        first = args.named_children[0] if args is not None and args.named_children else None
        dynamic = False
        if first is not None and first.type == "identifier" and _text(first) in self.declarations:
            first = self.declarations[_text(first)]
            dynamic = first.type != "object"
        elif first is not None and first.type != "object":
            dynamic = True
        properties = self._properties(first) if first is not None and first.type == "object" else {}
        if first is not None and first.type == "object" and any(c.type == "spread_element" for c in first.named_children):
            dynamic = True

        # Is this really an LLM SDK call? `client.messages.create(...)` is also Twilio's API.
        client_sdk, base_url = self.clients.get(receiver, (None, False))
        evidence = "model" in properties and (sdk == "openai" or "max_tokens" in properties)
        if client_sdk is not None:
            if client_sdk != sdk:
                return None
        elif receiver in self.declarations:
            if not evidence:  # some other object declared in this file, like a Twilio client
                return None
        elif not (sdk in self.imported_sdks or evidence):
            return None

        resolved = [self.declarations[_text(v)] for v in properties.values() if v.type == "identifier" and _text(v) in self.declarations]
        searchable = " ".join([_text(first)] + [_text(n) for n in resolved])

        features = features_for_api(sdk, api)
        if "tools" in properties or "functions" in properties:
            features.append("tools")
        structured_kind = "json_schema" if api.endswith("parse") else None
        if "response_format" in properties:
            structured_kind = "json_object" if "json_object" in _text(properties["response_format"]) else "json_schema"
        if "output_config" in properties or ("text" in properties and api.startswith("responses")):
            structured_kind = structured_kind or "json_schema"
        if structured_kind == "json_object":
            features.append("json_mode")
        elif structured_kind and "structured_output" not in features:
            features.append("structured_output")
        if {"thinking", "reasoning_effort", "reasoning"} & properties.keys():
            features.append("thinking")
        if _text(properties.get("stream")) == "true" and "streaming" not in features and "anthropic_streaming" not in features:
            features.append("anthropic_streaming" if sdk == "anthropic" else "streaming")
        if re.search(r"image_url|input_image|type\s*:\s*['\"]image['\"]", searchable):
            features.append("vision")
        if "cache_control" in searchable:
            features.append("prompt_caching")
        if self._ends_with_assistant(properties.get("messages")):
            features.append("prefill" if sdk == "anthropic" else "ends_on_assistant")
        if dynamic:
            features.append("dynamic_kwargs")
        if base_url:
            features.append("base_url_configurable")

        model, model_expr = None, None
        model_node = properties.get("model")
        if model_node is not None:
            if model_node.type == "string":
                model = _unquote(_text(model_node))
            else:
                model_expr = _text(model_node)
                declared = self.declarations.get(model_expr)
                if declared is not None and declared.type == "string":
                    model = _unquote(_text(declared))

        enclosing = self._enclosing_function(call)
        parses_json = enclosing is not None and _parses_json(enclosing, call)
        kind = "attribute" if receiver.startswith("this.") else "module_global" if receiver in self.clients else "other"

        return CallSiteFinding(
            file=self.path,
            line=call.start_point[0] + 1,
            language="typescript",
            function=self._function_name(call),
            sdk=sdk,
            api=api,
            receiver=receiver,
            receiver_kind=kind,
            model=model,
            model_expr=model_expr,
            output=infer_output(api, features, structured_kind, parses_json),
            features=features,
            in_tests=self.in_tests,
        )

    @staticmethod
    def _properties(obj: Node) -> dict[str, Node]:
        properties = {}
        for child in obj.named_children:
            if child.type == "pair":
                properties[_unquote(_text(child.child_by_field_name("key")))] = child.child_by_field_name("value")
            elif child.type == "shorthand_property_identifier":
                properties[_text(child)] = child
        return properties

    def _ends_with_assistant(self, messages: Node | None) -> bool:
        if messages is not None and messages.type in ("identifier", "shorthand_property_identifier"):
            messages = self.declarations.get(_text(messages))
        if messages is None or messages.type != "array" or not messages.named_children:
            return False
        last = messages.named_children[-1]
        return last.type == "object" and re.search(r"role\s*:\s*['\"]assistant['\"]", _text(last)) is not None

    @staticmethod
    def _enclosing_function(node: Node) -> Node | None:
        while node is not None and node.type not in _FUNCTION_TYPES:
            node = node.parent
        return node

    def _function_name(self, node: Node) -> str:
        names = []
        current = node.parent
        while current is not None:
            name = None
            if current.type in ("function_declaration", "generator_function_declaration", "method_definition", "class_declaration"):
                name = _text(current.child_by_field_name("name"))
            elif current.type in ("arrow_function", "function_expression") and current.parent is not None:
                holder = current.parent
                if holder.type == "variable_declarator":
                    name = _text(holder.child_by_field_name("name"))
                elif holder.type == "pair":
                    name = _unquote(_text(holder.child_by_field_name("key")))
            if name:
                names.insert(0, name)
            current = current.parent
        return ".".join(names) if names else "<module>"


_LLM_CALL = re.compile(r"\.(?:chat\.completions|messages|responses|completions)\.(?:create|parse|stream)\s*\(")
_NAME_TYPES = ("identifier", "shorthand_property_identifier", "shorthand_property_identifier_pattern")


def _contains(root: Node, target: Node) -> bool:
    return root.start_byte <= target.start_byte and target.end_byte <= root.end_byte


def _identifiers(node: Node | None) -> set[str]:
    return {_text(n) for n in _walk(node) if n.type in _NAME_TYPES} if node is not None else set()


def _parses_json(function: Node, call: Node) -> bool:
    """Does this call's result reach JSON.parse, directly or through variables in the same function?

    Data flow stops at other LLM calls, so a later call's JSON parsing isn't credited to this one.
    """
    flows: list[tuple[Node, set[str]]] = []  # (value, names it's assigned to)
    for node in _walk(function):
        if node.type == "variable_declarator":
            value = node.child_by_field_name("value")
            if value is not None:
                flows.append((value, _identifiers(node.child_by_field_name("name"))))
        elif node.type in ("assignment_expression", "augmented_assignment_expression"):
            value = node.child_by_field_name("right")
            if value is not None:
                flows.append((value, _identifiers(node.child_by_field_name("left"))))
        elif node.type == "for_in_statement":
            value = node.child_by_field_name("right")
            if value is not None:
                flows.append((value, _identifiers(node.child_by_field_name("left"))))

    parsers = [
        n for n in _walk(function)
        if n.type == "call_expression" and _text(n.child_by_field_name("function")).replace(" ", "") == "JSON.parse"
    ]
    if any(_contains(parser, call) for parser in parsers):
        return True

    tracked: set[str] = set().union(*(names for value, names in flows if _contains(value, call)))
    changed = bool(tracked)
    while changed:
        changed = False
        for value, names in flows:
            if names <= tracked or (not _contains(value, call) and _LLM_CALL.search(_text(value))):
                continue
            if _identifiers(value) & tracked:
                tracked |= names
                changed = True
    return any(_identifiers(parser.child_by_field_name("arguments")) & tracked for parser in parsers)


def scan_typescript(source: bytes, relative_path: str, in_tests: bool) -> list[CallSiteFinding]:
    tsx = relative_path.endswith((".tsx", ".jsx"))
    tree = _parser(tsx).parse(source)
    return _FileScanner(tree.root_node, relative_path, in_tests).scan()
