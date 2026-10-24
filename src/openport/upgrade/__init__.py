"""`openport upgrade` (Phase 2): patch vulnerable dependencies and fix what the upgrade breaks, with proof.

Same "prove it, then ship it" engine as the port, aimed at a second kind of PR. This module currently
covers reading lockfiles; advisory lookup, version selection, code fixes and the PR come next.
"""

from .lockfiles import Dependency, read_lockfiles
from .osv import Advisory, find_advisories

__all__ = ["Advisory", "Dependency", "find_advisories", "read_lockfiles"]
