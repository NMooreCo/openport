"""Append tuned instructions to a system prompt where it's written: a string literal in code, or a prompt file."""

import re
from pathlib import Path

from ..scan import SKIP_DIRS
from .edits import Edit, newline_style

PROMPT_FILE_SUFFIXES = (".md", ".txt", ".prompt", ".j2", ".jinja", ".jinja2", ".tmpl")
MIN_PROMPT_CHARS = 40  # shorter text can't identify a prompt file reliably


def _indent_lines(text: str, indent: bytes, nl: bytes) -> bytes:
    return nl.join(indent + line.encode() if line else b"" for line in text.split("\n"))


def _content_indent(source: bytes, start: int, body_end: int) -> bytes:
    """Indentation of the last non-empty line inside a multi-line literal (kept so dedent() still works)."""
    lines = source[start:body_end].split(b"\n")[1:]
    for line in reversed(lines):
        if line.strip():
            return line[: len(line) - len(line.lstrip(b" \t"))]
    return b""


def _multiline_insertion(source: bytes, start: int, body_end: int, text: str) -> Edit:
    """Insert `text` as new lines just before a multi-line literal's closing delimiter."""
    nl = newline_style(source)
    indent = _content_indent(source, start, body_end)
    block = _indent_lines(text, indent, nl)
    trailing = re.search(rb"\n([ \t]*)$", source[start:body_end])
    if trailing:  # closing delimiter on its own line: add a blank line, then the text
        position = body_end - len(trailing.group(1))
        return Edit(position, position, nl + block + nl)
    return Edit(body_end, body_end, nl + nl + block)


def python_literal_insertion(source: bytes, start: int, end: int, addendum: str, template: bool) -> Edit | None:
    """Append `addendum` to the Python string literal at source[start:end]. None if it can't be done safely."""
    literal = source[start:end]
    prefix = re.match(rb"[A-Za-z]*", literal).group(0).lower()
    raw = b"r" in prefix
    if template:  # an f-string or a .format() template: literal braces must be doubled
        addendum = addendum.replace("{", "{{").replace("}", "}}")
    for quote in ('"""', "'''"):
        if literal.endswith(quote.encode()) and len(literal) >= 6:
            if raw and ("\\" in addendum or quote in addendum):
                return None
            text = addendum if raw else addendum.replace("\\", "\\\\").replace(quote, ("\\" + quote[0]) * 3)
            return _multiline_insertion(source, start, end - 3, text)
    quote = literal[-1:].decode()
    if quote not in ('"', "'"):
        return None
    if raw:
        if any(c in addendum for c in ("\\", quote, "\n")):
            return None
        return Edit(end - 1, end - 1, (" " + addendum).encode())
    text = addendum.replace("\\", "\\\\").replace(quote, "\\" + quote).replace("\n", "\\n")
    return Edit(end - 1, end - 1, ("\\n\\n" + text).encode())


def ts_literal_insertion(source: bytes, start: int, end: int, addendum: str) -> Edit | None:
    """Append `addendum` to a TypeScript string or template literal at source[start:end]."""
    literal = source[start:end]
    if literal.endswith(b"`"):
        text = addendum.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")
        return _multiline_insertion(source, start, end - 1, text)
    quote = literal[-1:].decode()
    if quote not in ('"', "'"):
        return None
    text = addendum.replace("\\", "\\\\").replace(quote, "\\" + quote).replace("\n", "\\n")
    return Edit(end - 1, end - 1, ("\\n\\n" + text).encode())


def find_prompt_file(root: Path, prompt: str) -> tuple[str, int, bytes] | None:
    """(relative path, byte offset just past the prompt, file bytes) for a prompt stored verbatim in a prompt file."""
    target = prompt.strip().replace("\r\n", "\n")
    if len(target) < MIN_PROMPT_CHARS:
        return None
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if path.suffix.lower() not in PROMPT_FILE_SUFFIXES or not path.is_file():
            continue
        if any(part in SKIP_DIRS or part.startswith(".") for part in relative.parts[:-1]):
            continue
        raw = path.read_bytes()
        text = raw.decode("utf-8", errors="replace")
        for candidate in (target, target.replace("\n", "\r\n")):
            index = text.find(candidate)
            if index != -1:
                return relative.as_posix(), len(text[: index + len(candidate)].encode("utf-8")), raw
    return None


def prompt_file_insertion(raw: bytes, offset: int, addendum: str) -> Edit:
    nl = newline_style(raw)
    return Edit(offset, offset, nl + nl + addendum.replace("\n", nl.decode()).encode())
