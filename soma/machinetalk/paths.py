"""Where the pipeline's outside data lives, read from the environment.

Nothing outside the repo is named in code. A machine that keeps its sessions or
its invariants somewhere else (a RunPod box, a laptop) sets one variable; the
defaults are this workstation's layout, so nothing changes here.

    SOMA_SESSIONS_DIR     the chat-log sessions, one directory per session
    SOMA_INVARIANTS_DB    the invariants corpus (SQLite, the `truth` table)
    SOMA_SESSION_VAULT_DB the SearchMonkey vault the session picker reads
    SOMA_TURNKEY_PATH     an extra directory to import the TurnKey SDK from
"""
from __future__ import annotations

import os
from pathlib import Path

_DEFAULT_SESSIONS = "/media/storage/dendritic123/chat-logs/sessions"
_DEFAULT_INVARIANTS = "/media/storage/invariants/invariants.db"
_DEFAULT_VAULT = "/media/storage/graphdb/session_site/vault.db"


def sessions_dir() -> Path:
    return Path(os.environ.get("SOMA_SESSIONS_DIR") or _DEFAULT_SESSIONS)


def invariants_db() -> str:
    return os.environ.get("SOMA_INVARIANTS_DB") or _DEFAULT_INVARIANTS


def session_vault_db() -> Path:
    return Path(os.environ.get("SOMA_SESSION_VAULT_DB") or _DEFAULT_VAULT)


def turnkey_path() -> str | None:
    return os.environ.get("SOMA_TURNKEY_PATH") or None
