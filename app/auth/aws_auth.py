"""AWS authentication: the single entry point for every AWS connection in the app."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import boto3
from botocore.client import BaseClient
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import dotenv_values
from rich.console import Console
from rich.markup import escape

console = Console()

ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
REQUIRED_VARS = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_DEFAULT_REGION")


class AWSAuthError(Exception):
    """Raised when AWS credentials are missing, invalid or unreachable."""


@dataclass(frozen=True)
class AWSIdentity:
    """Caller identity returned by AWS STS."""

    account_id: str
    arn: str
    user_id: str

    @property
    def name(self) -> str:
        """Readable identity name, e.g. 'alice', 'root' or 'MyRole/session'."""
        resource = self.arn.split(":", 5)[-1]
        return resource.split("/", 1)[-1]


def _status(ok: bool, message: str) -> None:
    """Print a single status line with a green tick or red cross."""
    mark = "[bold green]✔[/]" if ok else "[bold red]✘[/]"
    console.print(f"{mark} {escape(message)}")


def mask(value: str, head: int = 4, tail: int = 4) -> str:
    """Hide a secret, keeping only a few leading and trailing characters."""
    if len(value) <= head + tail:
        return "*" * len(value)
    return f"{value[:head]}{'*' * 8}{value[-tail:]}"


def load_env(env_path: Path = ENV_PATH) -> dict[str, str]:
    """Read and validate the required AWS settings from the .env file."""
    if not env_path.is_file():
        raise AWSAuthError(f".env not found at {env_path}")

    values = dotenv_values(env_path)
    missing = [var for var in REQUIRED_VARS if not values.get(var)]
    if missing:
        raise AWSAuthError(f"missing in .env: {', '.join(missing)}")

    return {var: values[var] for var in REQUIRED_VARS}


@lru_cache(maxsize=1)
def get_session() -> boto3.Session:
    """Return a shared boto3 session built from .env (created once, then reused)."""
    env = load_env()
    return boto3.Session(
        aws_access_key_id=env["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=env["AWS_SECRET_ACCESS_KEY"],
        region_name=env["AWS_DEFAULT_REGION"],
    )


def get_client(service: str, region: str | None = None) -> BaseClient:
    """Return a boto3 client for any AWS service, e.g. get_client('cloudwatch')."""
    return get_session().client(service, region_name=region)


def get_identity() -> AWSIdentity:
    """Confirm the credentials with AWS STS and return the caller identity."""
    try:
        response = get_client("sts").get_caller_identity()
    except ClientError as exc:
        raise AWSAuthError(exc.response["Error"]["Message"]) from exc
    except BotoCoreError as exc:
        raise AWSAuthError(str(exc)) from exc

    return AWSIdentity(
        account_id=response["Account"],
        arn=response["Arn"],
        user_id=response["UserId"],
    )


def authenticate() -> AWSIdentity:
    """Run the full authentication check and print the status of each step."""
    try:
        env = load_env()
    except AWSAuthError as exc:
        _status(False, f"Verifying .env: {exc}")
        _status(False, "Authentication failed")
        raise
    _status(True, f"Verifying .env ({ENV_PATH})")

    _status(
        True,
        f"Access key [{mask(env['AWS_ACCESS_KEY_ID'])}]  "
        f"Secret key [{mask(env['AWS_SECRET_ACCESS_KEY'], head=0)}]  "
        f"Region [{env['AWS_DEFAULT_REGION']}]",
    )

    try:
        identity = get_identity()
    except AWSAuthError as exc:
        _status(False, "Name: unavailable")
        _status(False, f"Authentication failed: {exc}")
        raise
    _status(True, f"Name: {identity.name} (account {identity.account_id})")
    _status(True, "Authentication successful")

    return identity


def main() -> int:
    """CLI entry point: exit code 0 on success, 1 on failure."""
    try:
        authenticate()
    except AWSAuthError:
        return 1
    return 0


