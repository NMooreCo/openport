"""Byte-precise source edits, so a port changes only what it must and keeps the original formatting."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Edit:
    start: int
    end: int
    text: bytes


def apply_edits(source: bytes, edits: list[Edit]) -> bytes:
    ordered = sorted(edits, key=lambda e: (e.start, e.end))
    for a, b in zip(ordered, ordered[1:]):
        if b.start < a.end:
            raise ValueError(f"Overlapping edits at bytes {a.start}-{a.end} and {b.start}-{b.end}")
    for edit in reversed(ordered):
        source = source[: edit.start] + edit.text + source[edit.end :]
    return source


def line_starts(source: bytes) -> list[int]:
    starts = [0]
    for index, byte in enumerate(source):
        if byte == 0x0A:  # \n
            starts.append(index + 1)
    return starts


def line_end(source: bytes, offset: int) -> int:
    """Offset just past the newline that ends the line containing `offset`."""
    newline = source.find(b"\n", offset)
    return len(source) if newline == -1 else newline + 1


def before_newline(source: bytes, offset: int) -> int:
    """Offset of the line ending (before any \\r\\n) of the line containing `offset`."""
    end = line_end(source, offset)
    if end > 0 and source[end - 1 : end] == b"\n":
        end -= 1
        if end > 0 and source[end - 1 : end] == b"\r":
            end -= 1
    return end


def indentation(source: bytes, offset: int) -> bytes:
    start = source.rfind(b"\n", 0, offset) + 1
    line = source[start:offset]
    return line[: len(line) - len(line.lstrip(b" \t"))]


def newline_style(source: bytes) -> bytes:
    return b"\r\n" if b"\r\n" in source else b"\n"


def quoted_like(original: bytes, value: str) -> bytes:
    """`value` as a string literal using the same quote character as `original`."""
    quote = original[:1] if original[:1] in (b"'", b'"', b"`") else b'"'
    return quote + value.encode() + quote


def was_comment(source: bytes, value_end: int, original: str, marker: bytes) -> Edit | None:
    """A trailing `# was "gpt-4o"` (or `// was ...`) on the edited line, unless the line already has one."""
    end = before_newline(source, value_end)
    rest = source[value_end:end]
    if marker in rest or rest.rstrip().endswith(b"\\"):
        return None
    return Edit(end, end, b"  " + marker + b' was "' + original.encode() + b'"')
