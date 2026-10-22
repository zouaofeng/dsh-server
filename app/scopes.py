"""Session id rules and per-token session scopes.

With authentication enabled, each token owns a scope and every session id is stored
inside the runtime as ``<scope>--<session_id>``; clients only ever see ``session_id``.
"""

from __future__ import annotations

import hashlib
import re
from typing import Optional

# Session ids become directory names under $DSH_HOME/sessions, so keep them path-safe.
SESSION_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
_SESSION_ID_RE = re.compile(SESSION_ID_PATTERN)
_SCOPED_ID_RE = re.compile(r"^t[0-9a-f]{12}--")


def token_scope(token: str) -> str:
    """Stable, non-reversible scope for a token (the token itself never reaches disk)."""
    return "t" + hashlib.sha256(token.encode()).hexdigest()[:12]


def scoped_session_id(scope: Optional[str], session_id: str) -> str:
    """Session id used inside the runtime."""
    return f"{scope}--{session_id}" if scope else session_id


def public_session_id(scope: Optional[str], runtime_session_id: str) -> Optional[str]:
    """Client-facing id of a runtime session, or ``None`` when it is outside ``scope``.

    Without a scope (authentication disabled) only unscoped sessions are visible.
    """
    if scope:
        prefix = f"{scope}--"
        if not runtime_session_id.startswith(prefix):
            return None
        candidate = runtime_session_id[len(prefix):]
    else:
        if _SCOPED_ID_RE.match(runtime_session_id):
            return None
        candidate = runtime_session_id
    return candidate if _SESSION_ID_RE.match(candidate) else None
