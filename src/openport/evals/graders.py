"""Graders: score one call-site output against its expected result.

Strict checks come first and do most of the work. The LLM judge (Ultra) only scores what rules
can't, like the tone and helpfulness of a free-text reply.
"""

import re
from dataclasses import dataclass
from typing import Any, Callable

# judge(rubric, case_input, output) -> score from 1 to 5
Judge = Callable[[str, dict, str], int]


@dataclass
class Grade:
    passed: bool
    score: float  # fraction of checks passed, 0..1
    failures: list[str]


def _grade(checks: list[tuple[bool, str]]) -> Grade:
    failures = [label for ok, label in checks if not ok]
    score = (len(checks) - len(failures)) / len(checks) if checks else 1.0
    return Grade(passed=not failures, score=score, failures=failures)


def _norm(value: Any) -> Any:
    if isinstance(value, str):
        return " ".join(value.split()).lower()
    return value


def _same(actual: Any, expected: Any) -> bool:
    if actual == "":
        actual = None
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        try:
            return abs(float(actual) - float(expected)) < 0.01
        except (TypeError, ValueError):
            return False
    return _norm(actual) == _norm(expected)


def fields(output: dict, expected: dict, names: list[str]) -> Grade:
    return _grade([(_same(output.get(name), expected.get(name)), f"{name}: got {output.get(name)!r}") for name in names])


def _phone(value: str | None) -> str | None:
    if not value:
        return None
    return ("+" if value.strip().startswith("+") else "") + re.sub(r"\D", "", value)


def pii(output: dict, expected: dict, ticket: str) -> Grade:
    redacted = output.get("redacted_text") or ""
    lowered = redacted.lower()
    checks = [
        (_same(output.get("customer_name"), expected["customer_name"]), f"customer_name: got {output.get('customer_name')!r}"),
        (_same(output.get("email"), expected["email"]), f"email: got {output.get('email')!r}"),
        (_phone(output.get("phone")) == expected["phone"], f"phone: got {output.get('phone')!r}"),
        (_same((output.get("order_id") or None), expected["order_id"]), f"order_id: got {output.get('order_id')!r}"),
    ]
    checks += [(s.lower() not in lowered, f"left {s!r} unredacted") for s in expected["pii_strings"]]
    checks += [(tag in redacted, f"missing {tag}") for tag in expected["tags"]]
    checks += [(s.lower() in lowered, f"removed {s!r}, which should stay") for s in expected["must_keep"]]
    if expected.get("unchanged"):
        checks.append((_norm(redacted) == _norm(ticket), "changed a ticket with no PII"))
    return _grade(checks)


_MARKDOWN_LINE = re.compile(r"^\s*(#{1,6}\s|[-*•]\s|\d+[.)]\s)")
_GREETING = re.compile(r"^\s*(hi|hello|hey|dear)\b", re.IGNORECASE)


def _first_sentence_after_greeting(text: str) -> str:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    if lines and _GREETING.match(lines[0]) and len(lines[0]) < 40:
        lines = lines[1:]
    body = " ".join(lines)
    if _GREETING.match(body):  # greeting on the same line as the body: "Hi Nina, please stop..."
        body = body.split(",", 1)[-1]
    return re.split(r"(?<=[.!?])\s", body.strip(), maxsplit=1)[0]


def text_rules(output: str, expected: dict, config: dict, case_input: dict, judge: Judge | None) -> Grade:
    text = (output or "").strip()
    checks = [(phrase.lower() in text.lower(), f"missing {phrase!r}") for phrase in expected.get("must_include", [])]
    if config.get("ends_with"):
        checks.append((text.endswith(config["ends_with"]), "wrong sign-off"))
    if config.get("max_words"):
        words = len(text.split())
        checks.append((words <= config["max_words"], f"{words} words"))
    if config.get("max_exclamations") is not None:
        checks.append((text.count("!") <= config["max_exclamations"], "too many exclamation marks"))
    if config.get("no_markdown"):
        checks.append((not any(_MARKDOWN_LINE.match(line) for line in text.splitlines()), "uses markdown"))
    for pattern in config.get("forbidden", []):
        match = re.search(pattern, text)
        checks.append((match is None, f"forbidden: {match.group(0)!r}" if match else ""))
    if expected.get("safety_first"):
        checks.append(("stop using" in _first_sentence_after_greeting(text).lower(), "safety warning not first"))
    if config.get("judge") and judge is not None:
        try:
            score = judge(config["judge"]["rubric"], case_input, text)
            label = f"judge scored {score}/5"
        except Exception as error:
            score, label = 0, f"judge failed: {type(error).__name__}"
        checks.append((score >= config["judge"].get("min_score", 4), label))
    return _grade(checks)


def grade(config: dict, output: Any, expected: dict, case_input: dict, judge: Judge | None = None) -> Grade:
    kind = config.get("type")
    if kind == "fields":
        return fields(output, expected, config["fields"])
    if kind == "pii":
        return pii(output, expected, case_input.get("ticket", ""))
    if kind == "text_rules":
        return text_rules(output, expected, config, case_input, judge)
    raise ValueError(f"Unknown grader type {kind!r}")
