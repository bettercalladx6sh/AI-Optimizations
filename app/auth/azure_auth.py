"""Azure authentication: the single entry point for every Azure connection in the app.

Credentials are tried in order; the first that works is reused for the whole run:
    1. Service principal  AZURE_CLIENT_ID + AZURE_CLIENT_SECRET in .env (CI, servers)
    2. Azure CLI          `az login` session (developer machines)
    3. Managed identity   Azure VM / App Service / Container (optional AZURE_UAMI_CLIENT_ID)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, TypeVar

from azure.core.credentials import TokenCredential
from azure.core.exceptions import ClientAuthenticationError, HttpResponseError
from azure.identity import (
    AzureCliCredential,
    ClientSecretCredential,
    CredentialUnavailableError,
    ManagedIdentityCredential,
)
from azure.mgmt.resource.resources import ResourceManagementClient
from azure.mgmt.resource.subscriptions import SubscriptionClient
from rich.table import Table

from app.auth.common import ENV_PATH, AuthError, console, hint, mask, read_env, status

# The Azure SDK logs every failed attempt; status lines report errors instead.
logging.getLogger("azure").setLevel(logging.ERROR)

REQUIRED_VARS = ("AZURE_SUBSCRIPTION_ID",)
OPTIONAL_VARS = {
    "AZURE_TENANT_ID": "",
    "AZURE_CLIENT_ID": "",
    "AZURE_CLIENT_SECRET": "",
    "AZURE_UAMI_CLIENT_ID": "",
}

ARM_SCOPE = "https://management.azure.com/.default"
CLI_TIMEOUT = 30

AUTH_HINTS = (
    "Developer machine: install Azure CLI and run `az login` (az must be on PATH)",
    "CI / server: set AZURE_TENANT_ID, AZURE_CLIENT_ID and AZURE_CLIENT_SECRET",
    "Check that AZURE_TENANT_ID is the subscription's tenant",
)
SUBSCRIPTION_HINTS = (
    "Is AZURE_SUBSCRIPTION_ID correct?",
    "Does the identity have the Reader role on the subscription?",
    "Is the subscription in the selected tenant?",
)
RESOURCE_GROUP_HINTS = ("Does the identity have permission to list resource groups?",)

T = TypeVar("T")


class AzureAuthError(AuthError):
    """Raised when Azure credentials are missing, invalid or lack access."""


@dataclass(frozen=True)
class AzureIdentity:
    """Subscription details confirmed by Azure Resource Manager."""

    subscription_id: str
    name: str
    tenant_id: str
    state: str


def load_env() -> dict[str, str]:
    """Read and validate the Azure settings from .env."""
    return read_env(REQUIRED_VARS, AzureAuthError, optional=OPTIONAL_VARS)


def _error_message(exc: Exception) -> str:
    """First line of an error; Azure SDK messages span many lines."""
    text = str(getattr(exc, "message", None) or exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__


def _credential_chain(env: dict[str, str]) -> list[tuple[str, TokenCredential]]:
    """Build the ordered list of credentials to try."""
    tenant = env["AZURE_TENANT_ID"]
    chain: list[tuple[str, TokenCredential]] = []

    if env["AZURE_CLIENT_ID"] and env["AZURE_CLIENT_SECRET"]:
        if not tenant:
            raise AzureAuthError("AZURE_TENANT_ID is required for a service principal")
        chain.append((
            "service principal",
            ClientSecretCredential(tenant, env["AZURE_CLIENT_ID"], env["AZURE_CLIENT_SECRET"]),
        ))

    chain.append(("Azure CLI", AzureCliCredential(tenant_id=tenant or None, process_timeout=CLI_TIMEOUT)))
    chain.append(("managed identity", ManagedIdentityCredential(client_id=env["AZURE_UAMI_CLIENT_ID"] or None)))
    return chain


@lru_cache(maxsize=1)
def _connect() -> tuple[str, TokenCredential]:
    """Return the first working credential and its source (resolved once, then reused)."""
    unavailable: list[str] = []

    for source, credential in _credential_chain(load_env()):
        try:
            credential.get_token(ARM_SCOPE)
            return source, credential
        except CredentialUnavailableError as exc:
            unavailable.append(f"{source}: {_error_message(exc)}")
        except ClientAuthenticationError as exc:
            raise AzureAuthError(f"{source} rejected: {_error_message(exc)}", AUTH_HINTS) from exc

    raise AzureAuthError("no Azure credential available", (*unavailable, *AUTH_HINTS))


def get_credential() -> TokenCredential:
    """Return the shared Azure credential."""
    return _connect()[1]


def credential_source() -> str:
    """Return which method authenticated, e.g. 'Azure CLI'."""
    return _connect()[0]


def get_client(client_class: type[T], with_subscription: bool = True, **kwargs: Any) -> T:
    """Return any Azure SDK client, e.g. get_client(ComputeManagementClient)."""
    if with_subscription:
        return client_class(get_credential(), load_env()["AZURE_SUBSCRIPTION_ID"], **kwargs)
    return client_class(get_credential(), **kwargs)


def get_identity() -> AzureIdentity:
    """Confirm access to the configured subscription."""
    env = load_env()

    try:
        subscription = get_client(SubscriptionClient, with_subscription=False).subscriptions.get(
            env["AZURE_SUBSCRIPTION_ID"]
        )
    except HttpResponseError as exc:
        raise AzureAuthError(
            f"HTTP {exc.status_code}: {_error_message(exc)}", SUBSCRIPTION_HINTS
        ) from exc

    identity = AzureIdentity(
        subscription_id=subscription.subscription_id,
        name=subscription.display_name,
        tenant_id=subscription.tenant_id or "",
        state=getattr(subscription.state, "value", str(subscription.state)),
    )
    if env["AZURE_TENANT_ID"] and identity.tenant_id.lower() != env["AZURE_TENANT_ID"].lower():
        raise AzureAuthError("subscription does not belong to AZURE_TENANT_ID", SUBSCRIPTION_HINTS)

    return identity


def list_resource_groups() -> list[tuple[str, str]]:
    """Return (name, location) for every resource group in the subscription."""
    try:
        groups = get_client(ResourceManagementClient).resource_groups.list()
        return sorted((group.name, group.location) for group in groups)
    except HttpResponseError as exc:
        raise AzureAuthError(
            f"HTTP {exc.status_code}: {_error_message(exc)}", RESOURCE_GROUP_HINTS
        ) from exc


def _masked_or(value: str, fallback: str) -> str:
    return mask(value) if value else fallback


def _report_failure(step: str, exc: AuthError) -> None:
    status(False, f"{step}: {exc}")
    hint(exc.hints)
    status(False, "Authentication failed")


def _print_resource_groups(groups: list[tuple[str, str]]) -> None:
    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
    table.add_column("Resource group")
    table.add_column("Location", style="dim")
    for name, location in groups:
        table.add_row(name, location)
    console.print(table)


def authenticate() -> AzureIdentity:
    """Run the full authentication check and print the status of each step."""
    try:
        env = load_env()
    except AzureAuthError as exc:
        _report_failure("Verifying .env", exc)
        raise
    status(True, f"Verifying .env ({ENV_PATH})")

    status(
        True,
        f"Subscription [{mask(env['AZURE_SUBSCRIPTION_ID'])}]  "
        f"Tenant [{_masked_or(env['AZURE_TENANT_ID'], 'auto')}]  "
        f"Client [{_masked_or(env['AZURE_CLIENT_ID'], 'none')}]",
    )

    try:
        source = credential_source()
    except AzureAuthError as exc:
        _report_failure("Credential", exc)
        raise
    status(True, f"Credential: {source}")

    try:
        identity = get_identity()
    except AzureAuthError as exc:
        _report_failure("Subscription access", exc)
        raise
    status(True, f"Name: {identity.name} ({identity.state})")

    try:
        groups = list_resource_groups()
    except AzureAuthError as exc:
        _report_failure("Resource groups", exc)
        raise
    status(True, f"Resource groups: {len(groups)} found")
    if groups:
        _print_resource_groups(groups)

    status(True, "Authentication successful")
    return identity


def main() -> int:
    """CLI entry point: exit code 0 on success, 1 on failure."""
    try:
        authenticate()
    except AzureAuthError:
        return 1
    return 0