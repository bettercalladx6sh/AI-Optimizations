"""Azure authentication: the single entry point for every Azure connection in the app.

Credentials are tried in order; the first that works is reused for the whole run:
    1. Federated managed identity  the user-assigned managed identity (AZURE_UAMI_CLIENT_ID) signs
                                   in as the app registration (AZURE_APP_CLIENT_ID) in the target
                                   tenant (AZURE_TARGET_TENANT_ID), with no secret: cross-tenant
                                   access from an Azure host to a customer subscription
    2. Service principal           AZURE_CLIENT_ID + AZURE_CLIENT_SECRET in .env (CI, servers)
    3. Azure CLI                   `az login` session (developer machines)
    4. Managed identity            the host's managed identity itself (same-tenant access)

The subscription (AZURE_SUBSCRIPTION_ID) lives in the target tenant: AZURE_TARGET_TENANT_ID,
or AZURE_TENANT_ID when no separate target tenant is set. AZURE_TENANT_ID is the home tenant
of the app registration and the managed identity.

Every Azure call is read-only (Reader, Monitoring Reader and Log Analytics Reader roles).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, TypeVar

from azure.core.credentials import AccessToken, TokenCredential
from azure.core.exceptions import ClientAuthenticationError, HttpResponseError
from azure.identity import (
    AzureCliCredential,
    ClientAssertionCredential,
    ClientSecretCredential,
    CredentialUnavailableError,
    ManagedIdentityCredential,
)
from azure.mgmt.resource.resources import ResourceManagementClient
from azure.mgmt.resource.subscriptions import SubscriptionClient

from app.auth.common import ENV_PATH, AuthError, mask, read_env, report_failure, status
from app.cloud.display import field

# The Azure SDK logs every failed attempt; status lines report errors instead.
logging.getLogger("azure").setLevel(logging.ERROR)

REQUIRED_VARS = ("AZURE_SUBSCRIPTION_ID",)
OPTIONAL_VARS = {
    "AZURE_TENANT_ID": "",
    "AZURE_TARGET_TENANT_ID": "",
    "AZURE_APP_CLIENT_ID": "",
    "AZURE_UAMI_CLIENT_ID": "",
    "AZURE_CLIENT_ID": "",
    "AZURE_CLIENT_SECRET": "",
    "AZURE_LOG_ANALYTICS_WORKSPACE_ID": "",
}

ARM_SCOPE = "https://management.azure.com/.default"
LOG_ANALYTICS_SCOPE = "https://api.loganalytics.io/.default"
TOKEN_EXCHANGE_SCOPE = "api://AzureADTokenExchange/.default"
CLI_TIMEOUT = 30
TOKEN_REFRESH_MARGIN = 300          # seconds: renew a cached token this long before it expires

AUTH_HINTS = (
    "Azure host: attach the managed identity AZURE_UAMI_CLIENT_ID and add it as a federated "
    "credential of the app AZURE_APP_CLIENT_ID",
    "Cross-tenant: the app must be multi-tenant and consented in AZURE_TARGET_TENANT_ID",
    "Developer machine: install Azure CLI and run `az login --tenant <target tenant>` (az must be on PATH)",
    "CI / server: set AZURE_CLIENT_ID and AZURE_CLIENT_SECRET",
)
SUBSCRIPTION_HINTS = (
    "Is AZURE_SUBSCRIPTION_ID correct?",
    "Does the identity have the Reader role on the subscription?",
    "Is the subscription in AZURE_TARGET_TENANT_ID (or AZURE_TENANT_ID)?",
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


@dataclass(frozen=True)
class _Option:
    """One way to authenticate; `probe` checks that it is usable on this machine first."""

    source: str
    credential: TokenCredential
    probe: Callable[[], Any] | None = None


def load_env() -> dict[str, str]:
    """Read and validate the Azure settings from .env."""
    return read_env(REQUIRED_VARS, AzureAuthError, optional=OPTIONAL_VARS)


def target_tenant(env: dict[str, str]) -> str:
    """The tenant that holds the subscription."""
    return env["AZURE_TARGET_TENANT_ID"] or env["AZURE_TENANT_ID"]


def _error_message(exc: Exception) -> str:
    """First line of an error; Azure SDK messages span many lines."""
    text = str(getattr(exc, "message", None) or exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__


def _credential_chain(env: dict[str, str]) -> list[_Option]:
    """Build the ordered list of credentials to try."""
    tenant = target_tenant(env)
    uami = env["AZURE_UAMI_CLIENT_ID"] or None
    chain: list[_Option] = []

    if env["AZURE_APP_CLIENT_ID"] and uami:
        if not tenant:
            raise AzureAuthError("AZURE_TARGET_TENANT_ID (or AZURE_TENANT_ID) is required for the federated identity")
        identity = ManagedIdentityCredential(client_id=uami)

        def assertion() -> str:
            return identity.get_token(TOKEN_EXCHANGE_SCOPE).token

        chain.append(_Option(
            "federated managed identity",
            ClientAssertionCredential(tenant, env["AZURE_APP_CLIENT_ID"], assertion),
            probe=assertion,
        ))

    if env["AZURE_CLIENT_ID"] and env["AZURE_CLIENT_SECRET"]:
        if not tenant:
            raise AzureAuthError("AZURE_TENANT_ID is required for a service principal")
        chain.append(_Option(
            "service principal",
            ClientSecretCredential(tenant, env["AZURE_CLIENT_ID"], env["AZURE_CLIENT_SECRET"]),
        ))

    chain.append(_Option("Azure CLI", AzureCliCredential(tenant_id=tenant or None, process_timeout=CLI_TIMEOUT)))
    chain.append(_Option("managed identity", ManagedIdentityCredential(client_id=uami)))
    return chain


@lru_cache(maxsize=1)
def _connect() -> tuple[str, TokenCredential]:
    """Return the first working credential and its source (resolved once, then reused)."""
    unavailable: list[str] = []

    for option in _credential_chain(load_env()):
        try:
            if option.probe:
                option.probe()
            option.credential.get_token(ARM_SCOPE)
            return option.source, option.credential
        except CredentialUnavailableError as exc:
            unavailable.append(f"{option.source}: {_error_message(exc)}")
        except ClientAuthenticationError as exc:
            raise AzureAuthError(f"{option.source} rejected: {_error_message(exc)}", AUTH_HINTS) from exc

    raise AzureAuthError("no Azure credential available", (*unavailable, *AUTH_HINTS))


def get_credential() -> TokenCredential:
    """Return the shared Azure credential."""
    return _connect()[1]


def credential_source() -> str:
    """Return which method authenticated, e.g. 'Azure CLI'."""
    return _connect()[0]


_tokens: dict[str, AccessToken] = {}


def access_token(scope: str = ARM_SCOPE) -> str:
    """A bearer token for `scope`, cached until shortly before it expires.

    Used for the REST calls (Resource Manager, Azure Monitor, Log Analytics); some credentials
    (e.g. the Azure CLI) start a process for every token, so tokens are reused.
    """
    token = _tokens.get(scope)
    if token is None or token.expires_on - TOKEN_REFRESH_MARGIN <= time.time():
        try:
            token = get_credential().get_token(scope)
        except ClientAuthenticationError as exc:
            raise AzureAuthError(f"no token for {scope}: {_error_message(exc)}", AUTH_HINTS) from exc
        _tokens[scope] = token
    return token.token


def get_client(client_class: type[T], with_subscription: bool = True, **kwargs: Any) -> T:
    """Return any Azure SDK client, e.g. get_client(ResourceManagementClient)."""
    if with_subscription:
        return client_class(get_credential(), load_env()["AZURE_SUBSCRIPTION_ID"], **kwargs)
    return client_class(get_credential(), **kwargs)


@lru_cache(maxsize=1)
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
    tenant = target_tenant(env)
    if tenant and identity.tenant_id and identity.tenant_id.lower() != tenant.lower():
        raise AzureAuthError("subscription does not belong to the target tenant", SUBSCRIPTION_HINTS)

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


def _print_resource_groups(groups: list[tuple[str, str]]) -> None:
    """One plain line per resource group: name, then its location."""
    for name, location in groups:
        field(name, location, "dim")


def authenticate() -> AzureIdentity:
    """Run the full authentication check and print the status of each step."""
    try:
        env = load_env()
    except AzureAuthError as exc:
        report_failure("Verifying .env", exc)
        raise
    status(True, f"Verifying .env ({ENV_PATH})")

    status(
        True,
        f"Subscription [{mask(env['AZURE_SUBSCRIPTION_ID'])}]  "
        f"Tenant [{_masked_or(target_tenant(env), 'auto')}]  "
        f"App [{_masked_or(env['AZURE_APP_CLIENT_ID'] or env['AZURE_CLIENT_ID'], 'none')}]  "
        f"Managed identity [{_masked_or(env['AZURE_UAMI_CLIENT_ID'], 'none')}]",
    )

    try:
        source = credential_source()
    except AzureAuthError as exc:
        report_failure("Credential", exc)
        raise
    status(True, f"Credential: {source}")

    try:
        identity = get_identity()
    except AzureAuthError as exc:
        report_failure("Subscription access", exc)
        raise
    status(True, f"Name: {identity.name} ({identity.state})")

    try:
        groups = list_resource_groups()
    except AzureAuthError as exc:
        report_failure("Resource groups", exc)
        raise
    status(True, f"Resource groups: {len(groups)} found")
    if groups:
        _print_resource_groups(groups)

    workspace = env["AZURE_LOG_ANALYTICS_WORKSPACE_ID"]
    status(True, f"Log Analytics workspace [{_masked_or(workspace, 'not set · memory from platform metrics')}]")
    status(True, "Authentication successful")
    return identity


def main() -> int:
    """CLI entry point: exit code 0 on success, 1 on failure."""
    try:
        authenticate()
    except AzureAuthError:
        return 1
    return 0
