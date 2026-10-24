"""Which eval case is running on this thread, so a client wrapper can tie a model's reply to its case."""

from contextvars import ContextVar

CURRENT_CASE: ContextVar[str | None] = ContextVar("openport_current_case", default=None)
