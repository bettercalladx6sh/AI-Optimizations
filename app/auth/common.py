"""Shared helpers for all cloud authentication modules."""

from __future__ import annotations

from pathlib import Path

from dotenv import dotenv_values
from rich.console import Console
from rich.markup import escape

console = Console()

ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


class AuthError(Exception):
    """Base error for any cloud authentication failure."""


def status(ok: bool, message: str) -> None:
    """Print a single status line with a green tick or red cross."""
    mark = "[bold green]✔[/]" if ok else "[bold red]✘[/]"
    console.print(f"{mark} {escape(message)}")


def mask(value: str, head: int = 4, tail: int = 4) -> str:
    """Hide a secret, keeping only a few leading and trailing characters."""
    if len(value) <= head + tail:
        return "*" * len(value)
    return f"{value[:head]}{'*' * 8}{value[-tail:]}"


def read_env(
    required: tuple[str, ...],
    error: type[AuthError] = AuthError,
    env_path: Path = ENV_PATH,
) -> dict[str, str]:
    """Read and validate the required variables from the .env file."""
    if not env_path.is_file():
        raise error(f".env not found at {env_path}")

    values = dotenv_values(env_path)
    missing = [var for var in required if not values.get(var)]
    if missing:
        raise error(f"missing in .env: {', '.join(missing)}")

    return {var: values[var] for var in required}