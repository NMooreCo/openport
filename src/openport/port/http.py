"""Moving a plain HTTP call to an OpenAI-style chat endpoint onto Token Factory: what both rewriters share.

A call moves when the code spells out where it goes: its URL (or the base URL it's built from) is a string in
the code, so it can point at Token Factory's chat endpoint. Its key moves too when it's read from an
environment variable, and its model when the request names one. Anything the code gets from elsewhere
(settings, an object's attributes, the caller) is left as it is, with a note saying what to change.
"""

import re

from ..runtime.openport_llm import TOKEN_FACTORY_BASE_URL

TOKEN_FACTORY_V1 = TOKEN_FACTORY_BASE_URL.rstrip("/")  # https://api.tokenfactory.nebius.com/v1
TOKEN_FACTORY_ORIGIN = TOKEN_FACTORY_V1.removesuffix("/v1")
CHAT_URL = f"{TOKEN_FACTORY_V1}/chat/completions"
KEY_VARIABLE, BASE_URL_VARIABLE = "NEBIUS_API_KEY", "NEBIUS_BASE_URL"
BASE_URL_VARIABLES = {"OPENAI_BASE_URL", "OPENAI_API_BASE"}  # the OpenAI SDK's own, renamed as the SDK port renames them

ENDPOINT = "/chat/completions"
HOST = r"https?://(?:[\w.-]+|\[[0-9a-fA-F:]+\])(?::\d+)?"  # a scheme and host written out, with no placeholders in them
URL = re.compile(HOST + r"[^\s\"'`]*")  # a URL in a string literal's source, up to its closing quote
WHOLE = re.compile(HOST + r"[^\s\"'`]*?(?=/chat/completions)")  # a full endpoint URL, up to its path
AZURE = re.compile(r"azure\.com", re.I)  # Azure OpenAI: deployments and its own auth, ported by hand like AzureOpenAI clients
LOCAL = re.compile(r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]|host\.docker\.internal)\b")


def url_edits(literals: list[str]) -> dict[int, str] | None:
    """New source text for the string literals a URL is built from (each given as source, quotes included), so the
    URL becomes Token Factory's chat endpoint: {index: text} for the ones that change.

    None when that can't be done safely: the scheme and host come from something other than a literal in the
    code, or the path isn't OpenAI's (/v1/chat/completions, or /chat/completions after the base URL).
    """
    whole = {i: WHOLE.sub(TOKEN_FACTORY_V1, text) for i, text in enumerate(literals) if WHOLE.search(text)}
    if whole:
        return whole
    ends = [text for text in literals if ENDPOINT in text]
    bases = [i for i, text in enumerate(literals) if URL.search(text)]
    if len(ends) != 1 or not bases:
        return None
    end = ends[0]
    before = re.split(r"[\"'`}]", end[: end.index(ENDPOINT)])[-1]  # what the literal puts between the base URL and the path
    base = {"": TOKEN_FACTORY_V1, "/v1": TOKEN_FACTORY_ORIGIN}.get(before)
    if base is None:
        return None
    return {i: URL.sub(lambda m: base + "/" * m[0].endswith("/"), literals[i]) for i in bases}


def why_kept(literals: list[str], url: str) -> str | None:
    """Why a plain HTTP call stays where it is, given the literals its URL is built from and the URL's own source;
    None when it can move."""
    written = " ".join(literals)
    hosts = sorted({re.match(HOST, url)[0].split("://")[1] for url in URL.findall(written)})
    if AZURE.search(written):
        return "calls Azure OpenAI, with its own deployments and keys: port by hand"
    if LOCAL.search(written):
        return "calls a server on the same machine (the app's own, or a local model), so it's left as it is"
    if len(hosts) > 1:  # a table of providers, say: rewriting each one would leave `gemini` calling Nemotron
        return f"picks its URL from several ({', '.join(hosts)}): add Token Factory to them by hand"
    if url_edits(literals) is None:
        return f"OpenPort can't tell where its URL (`{url}`) points: move it to {CHAT_URL}, with {KEY_VARIABLE} and a Nemotron model, by hand"
    return None
