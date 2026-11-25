"""The engine's session ids: the engine generates them, the pipeline reuses them.

How a session gets its id (the engine's rule, ``openai_compat.py``):

  * a call that carries a stage and NO ``pea_session`` is the first call of a
    new session. The engine generates a code (``SES-321``) and returns it in
    the reply.
  * a call that carries a ``pea_session`` is a continuation. The engine takes
    the id as already assigned and does not generate another.

So the pipeline does not make session ids up. For each chat session in a run:

  1. its first call sends no session code;
  2. the reply names the code the engine generated;
  3. every later call for THAT session sends that code, and only that session.

The next run (or the next chat session) starts again with no code and gets its
own. Re-running the same chat session therefore never merges into an older one.

The local ``SES-007``-style code in ``short_codes.py`` is a display and file-name
counter. It is never sent to the engine.

The first call per session is serialized. With several workers, two first
calls in flight would both send no code and the engine would open two sessions;
the second waits instead and sends the code the first brought back.
"""
from __future__ import annotations

import contextlib
import re
import threading
from typing import Callable, Iterator

# The shape the engine generates (``soma_counter.assign_session``).
_ENGINE_CODE = re.compile(r"^SES-\d+$")


class EngineSessions:
    """Maps a chat session (or build unit) to the id the engine generated for it."""

    def __init__(self) -> None:
        self._codes: dict[str, str] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def get(self, key: str) -> str | None:
        return self._codes.get(key)

    def _lock_for(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    @contextlib.contextmanager
    def scope(self, key: str) -> Iterator[str | None]:
        """Yield the code to send for ``key``, or None for its first call.

        None tells the engine to generate one. While that first call is in
        flight the lock is held, so a second call for the same session waits
        and then sends the code the first one brought back. Call ``remember``
        before leaving the ``with`` block so the waiting call can see it.
        """
        code = self._codes.get(key)
        if code:
            yield code
            return
        with self._lock_for(key):
            yield self._codes.get(key)

    def remember(self, key: str, sent: str | None, received: str | None) -> str | None:
        """Record the code the engine generated. Returns it when newly learned.

        Only a first call (nothing sent) can teach us a code, and only if the
        reply carries one in the engine's own shape. A code is never replaced:
        one chat session has one engine session for the life of the run.
        """
        if sent or key in self._codes:
            return None
        if not received or not _ENGINE_CODE.match(str(received)):
            return None
        self._codes[key] = str(received)
        return self._codes[key]


def learn(sessions: EngineSessions, key: str, sent: str | None,
          received: str | None, say: Callable[[str], None]) -> None:
    """Record what the engine generated and tell the operator, once.

    ``say`` receives one plain line. Says nothing for continuation calls.
    """
    learned = sessions.remember(key, sent, received)
    if learned:
        say(f"engine generated session {learned} for {key}; "
            f"every call in this session now sends it")
    elif not sent and not received:
        say(f"WARNING: the engine returned no session id for {key}; "
            f"the next call will ask for a new one")
