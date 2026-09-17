#!/usr/bin/env python3
"""Hook-Helfer für Claude Code im backend-Repo.

Zwei Modi, beide lesen das Hook-JSON von stdin:

    pre    PreToolUse auf Edit|Write — blockt Änderungen an bereits
           existierenden Alembic-Migrationen. Neue Migrationen bleiben
           erlaubt.
    lint   PostToolUse auf Edit|Write — lässt ruff --fix im Container
           backend-dev über die gerade geänderte Datei laufen.

Bewusst fail-open: jeder Fehler (kein Docker, kaputtes JSON, Container
aus) endet still mit Exit 0. Ein Hook, der die Sitzung blockiert, weil
er selbst defekt ist, kostet mehr als er schützt. Das harte Gate für
Lint und Tests ist die CI.

Kein jq: das ist auf den Entwicklungsrechnern nicht überall installiert,
python dagegen zwangsläufig.
"""

import json
import os
import subprocess
import sys

CONTAINER = "backend-dev"
# Alles unterhalb dieses Pfadsegments liegt im Container unter /app.
MOUNT_MARKER = "/backend/"


def hook_input() -> dict:
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


def edited_path(data: dict) -> str:
    """Den Pfad der geänderten Datei aus dem Hook-JSON ziehen.

    PostToolUse liefert ihn in ``tool_response.filePath``, PreToolUse nur
    in ``tool_input.file_path``.
    """
    response = data.get("tool_response") or {}
    tool_input = data.get("tool_input") or {}
    return response.get("filePath") or tool_input.get("file_path") or ""


def as_posix(path: str) -> str:
    """Windows-Backslashes zu Slashes, damit ein Muster beide Seiten trifft."""
    return path.replace("\\", "/")


def block_migration(data: dict) -> None:
    path = edited_path(data)
    if "alembic/versions/" not in as_posix(path):
        return
    # Eine neue Migration darf entstehen — tabu sind nur bestehende.
    if not os.path.isfile(path):
        return
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    # Bewusst ohne Umlaute: die Windows-Konsole laeuft nicht
                    # zwangslaeufig auf UTF-8, und diese Zeile wird gelesen,
                    # wenn gerade etwas schiefgeht.
                    "permissionDecisionReason": (
                        "Bestehende Alembic-Migrationen werden nie bearbeitet: "
                        "eine geaenderte Migration zerlegt jede Datenbank, die "
                        "sie bereits gefahren hat. Stattdessen eine neue "
                        "erzeugen mit: docker exec backend-dev alembic "
                        "revision --autogenerate -m '...'"
                    ),
                }
            }
        )
    )


def ruff_fix(data: dict) -> None:
    path = as_posix(edited_path(data))
    if not path.endswith(".py") or MOUNT_MARKER not in path:
        return
    # ../backend ist als /app gemountet, der Pfad muss übersetzt werden.
    in_container = "/app/" + path.rsplit(MOUNT_MARKER, 1)[1]
    subprocess.run(
        ["docker", "exec", CONTAINER, "poetry", "run", "ruff", "check", "--fix", in_container],
        capture_output=True,
        timeout=45,
    )


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        data = hook_input()
        if mode == "pre":
            block_migration(data)
        elif mode == "lint":
            ruff_fix(data)
    except Exception:
        pass


if __name__ == "__main__":
    main()
