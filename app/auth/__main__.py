"""Run cloud authentication checks.

Usage:
    python -m app.auth            # all providers
    python -m app.auth aws        # AWS only
    python -m app.auth azure      # Azure only
"""

import sys

from rich.markup import escape

from app.auth import aws_auth, azure_auth
from app.auth.common import console

PROVIDERS = {"aws": aws_auth.main, "azure": azure_auth.main}


def run(selected: list[str]) -> int:
    """Run the selected provider checks and return the worst exit code."""
    unknown = [name for name in selected if name not in PROVIDERS]
    if unknown:
        console.print(
            f"[red]Unknown provider(s): {escape(', '.join(unknown))}. "
            f"Choose from: {', '.join(PROVIDERS)}[/]"
        )
        return 2

    exit_code = 0
    for name in selected or PROVIDERS:
        console.rule(f"[bold]{name.upper()}")
        exit_code = max(exit_code, PROVIDERS[name]())
    return exit_code


raise SystemExit(run(sys.argv[1:]))