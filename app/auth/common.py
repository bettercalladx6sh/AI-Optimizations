"""Shared helpers for all cloud authentication modules."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from dotenv import dotenv_values
from rich.console import Console
from rich.markup import escape

console = Console()

ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


class AuthError(Exception):
    """Base error for any cloud authentication failure, with optional fix hints."""

    def __init__(self, message: str, hints: Iterable[str] = ()) -> None:
        super().__init__(message)
        self.hints = tuple(hints)


def status(ok: bool, message: str) -> None:
    """Print a single status line with a green tick or red cross."""
    mark = "[bold green]✔[/]" if ok else "[bold red]✘[/]"
    console.print(f"{mark} {escape(message)}")


def hint(lines: Iterable[str]) -> None:
    """Print dimmed follow-up hints under a failed status line."""
    for line in lines:
        console.print(f"  [dim]→ {escape(line)}[/]")


def mask(value: str, head: int = 4, tail: int = 4) -> str:
    """Hide a secret, keeping only a few leading and trailing characters."""
    if len(value) <= head + tail:
        return "*" * len(value)
    return f"{value[:head]}{'*' * 8}{value[-tail:]}"


def read_env(
    required: tuple[str, ...],
    error: type[AuthError] = AuthError,
    env_path: Path = ENV_PATH,
    optional: dict[str, str] | None = None,
) -> dict[str, str]:
    """Read required variables (and optional ones with defaults) from the .env file."""
    if not env_path.is_file():
        raise error(f".env not found at {env_path}")

    values = dotenv_values(env_path)
    missing = [var for var in required if not (values.get(var) or "").strip()]
    if missing:
        raise error(f"missing in .env: {', '.join(missing)}")

    env = {var: values[var].strip() for var in required}
    for var, default in (optional or {}).items():
        env[var] = (values.get(var) or default).strip()
    return env

def report_failure(step: str, exc: AuthError) -> None:
    """Print a failed step, its fix hints and the final failure line."""
    status(False, f"{step}: {exc}")
    hint(exc.hints)
    status(False, "Authentication failed")