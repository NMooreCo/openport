"""Run a TypeScript or JavaScript call site's eval cases: the app's code runs unchanged, under Node.

The app's OpenAI SDK reaches a local stand-in for the OpenAI API (`EvalProxy`) through OPENAI_BASE_URL.
Each request goes through the same swapped client the Python harness uses (model swap, the variant
being tuned, usage metering), then on to Token Factory. The Node process never holds a Token Factory
key, and its environment is stripped of anything that looks like a secret. The URL carries the case
ID (/case/<id>/v1), so each reply can be tied to its case.
"""

import inspect
import json
import os
import re
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from openai import APIStatusError
from openai.resources.chat.completions import Completions

from .context import CURRENT_CASE

RUNNER = Path(__file__).with_name("node_runner.mjs")
NODE_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
TS_SUFFIXES = (".ts", ".tsx", ".mts", ".cts")
MARKER = "OPENPORT_RESULT "
ERROR_MARKER = "OPENPORT_ERROR "
TIMEOUT = 300  # seconds per case
_KNOWN = set(inspect.signature(Completions.create).parameters) - {"self", "extra_headers", "extra_query", "extra_body", "timeout"}
_PATH = re.compile(r"^/case/([^/]+)/v1/chat/completions/?$")
_SECRET = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", re.IGNORECASE)


class NodeCaseError(Exception):
    """The app's code failed for this case, or didn't print a result."""


class EvalProxy:
    """A local stand-in for the OpenAI chat completions API that forwards through `client`."""

    def __init__(self, client: Any):
        self.client = client
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 (the http.server API)
                match = _PATH.match(self.path)
                if match is None:
                    return self._reply(404, {"error": {"message": f"OpenPort's eval proxy only serves chat completions, not {self.path}"}})
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if body.get("stream"):
                    return self._reply(400, {"error": {"message": "Streaming isn't supported in OpenPort evals"}})
                token = CURRENT_CASE.set(unquote(match[1]))
                try:
                    response = proxy.forward(body)
                except APIStatusError as error:  # Token Factory's own error, passed through as is
                    return self._reply(error.status_code, error.body if isinstance(error.body, dict) else {"error": {"message": str(error)}})
                except Exception as error:  # noqa: BLE001 (the app sees an API error, as it would in production)
                    return self._reply(500, {"error": {"message": f"{type(error).__name__}: {error}"}})
                finally:
                    CURRENT_CASE.reset(token)
                self._reply(200, json.loads(response.model_dump_json()))

            def _reply(self, status: int, payload: dict):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):  # keep the eval output clean
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def forward(self, body: dict):
        """Send an OpenAI-style request body through the client; options the Python SDK doesn't name go in extra_body."""
        known = {key: value for key, value in body.items() if key in _KNOWN}
        extra = {key: value for key, value in body.items() if key not in _KNOWN}
        if extra:
            known["extra_body"] = extra
        return self.client.chat.completions.create(**known)

    def base_url(self, case_id: str) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/case/{quote(case_id, safe='')}/v1"

    def __enter__(self) -> "EvalProxy":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()


def is_node_site(file: str) -> bool:
    return file.lower().endswith(NODE_SUFFIXES)


def node_command(root: Path, module: str, export: str, args: list[str] | None) -> list[str]:
    """The Node command for one case. TypeScript loads through the project's own tsx, else Node's type stripping."""
    node = shutil.which("node")
    if node is None:
        raise NodeCaseError("Node.js isn't installed; it's needed to run this TypeScript/JavaScript call site")
    loader: list[str] = []
    if module.lower().endswith(TS_SUFFIXES):
        loader = ["--import", "tsx"] if (root / "node_modules" / "tsx").is_dir() else ["--experimental-strip-types", "--no-warnings"]
    return [node, *loader, str(RUNNER), module, export, *([json.dumps(args)] if args else [])]


def clean_env(base_url: str) -> dict[str, str]:
    """The app's environment: no secrets, and its OpenAI SDK pointed at the local proxy."""
    env = {name: value for name, value in os.environ.items() if not _SECRET.search(name)}
    return env | {"OPENAI_BASE_URL": base_url, "OPENAI_API_KEY": "openport-placeholder"}


def run_node_case(root: Path, module: str, export: str, args: list[str] | None, case_input: dict, base_url: str, run=subprocess.run) -> Any:
    """Call the site's export with one case's input, under Node. Returns its output, parsed from JSON."""
    try:
        process = run(
            node_command(root, module, export, args), cwd=root, env=clean_env(base_url), input=json.dumps(case_input),
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired as error:
        raise NodeCaseError(f"timed out after {TIMEOUT}s") from error
    lines = (process.stdout or "").splitlines()
    result = next((line for line in reversed(lines) if line.startswith(MARKER)), None)
    if result is not None:
        return json.loads(result[len(MARKER):])
    failure = next((line for line in reversed(lines) if line.startswith(ERROR_MARKER)), None)
    if failure is not None:  # the app threw: its error message is the stack's first line
        raise NodeCaseError(json.loads(failure[len(ERROR_MARKER):]).strip().splitlines()[0][:300])
    output = [line for line in (process.stderr or process.stdout or "").splitlines() if line.strip()]
    raise NodeCaseError(next((line for line in output if "Error" in line), output[-1] if output else f"node exited with {process.returncode}")[:300])
