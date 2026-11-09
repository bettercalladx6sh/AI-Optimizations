"""Azure authentication: the single entry point for every Azure connection in the app.

Secretless cross-tenant access: a user-assigned managed identity (UAMI) issues a
token that a multi-tenant app registration exchanges, through a federated
identity credential, for an access token in the target tenant.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any, TypeVar

import requests
from azure.core.exceptions import AzureError
from azure.identity import ClientAssertionCredential, ManagedIdentityCredential

from app.auth.common import ENV_PATH, AuthError, mask, read_env, status

REQUIRED_VARS = (
    "AZURE_SUBSCRIPTION_ID",
    "AZURE_TENANT_ID",
    "AZURE_APP_CLIENT_ID",
    "AZURE_UAMI_CLIENT_ID",
    "AZURE_TARGET_TENANT_ID",
)

ARM_ENDPOINT = "https://management.azure.com"
ARM_SCOPE = f"{ARM_ENDPOINT}/.default"
TOKEN_EXCHANGE_SCOPE = "api://AzureADTokenExchange/.default"
SUBSCRIPTION_API_VERSION = "2022-12-01"
REQUEST_TIMEOUT = 30

T = TypeVar("T")


class AzureAuthError(AuthError):
    """Raised when Azure credentials are missing, invalid or unreachable."""


@dataclass(frozen=True)
class AzureIdentity:
    """Target subscription details confirmed by Azure Resource Manager."""

    subscription_id: str
    name: str
    tenant_id: str
    state: str


def load_env() -> dict[str, str]:
    """Read and validate the Azure settings from .env."""
    return read_env(REQUIRED_VARS, AzureAuthError)


@lru_cache(maxsize=1)
def get_credential() -> ClientAssertionCredential:
    """Return a shared credential for the target tenant (created once, then reused)."""
    env = load_env()
    managed_identity = ManagedIdentityCredential(client_id=env["AZURE_UAMI_CLIENT_ID"])

    def _uami_assertion() -> str:
        return managed_identity.get_token(TOKEN_EXCHANGE_SCOPE).token

    return ClientAssertionCredential(
        tenant_id=env["AZURE_TARGET_TENANT_ID"],
        client_id=env["AZURE_APP_CLIENT_ID"],
        func=_uami_assertion,
    )


def get_client(client_class: type[T], with_subscription: bool = True, **kwargs: Any) -> T:
    """Return any Azure SDK client, e.g. get_client(ComputeManagementClient)."""
    if with_subscription:
        return client_class(get_credential(), load_env()["AZURE_SUBSCRIPTION_ID"], **kwargs)
    return client_class(get_credential(), **kwargs)


def _error_message(exc: Exception) -> str:
    """First line of an error; azure-identity messages span many lines."""
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__


def get_identity() -> AzureIdentity:
    """Confirm the credentials by reading the target subscription from Azure."""
    env = load_env()
    subscription_id = env["AZURE_SUBSCRIPTION_ID"]

    try:
        token = get_credential().get_token(ARM_SCOPE).token
        response = requests.get(
            f"{ARM_ENDPOINT}/subscriptions/{subscription_id}",
            params={"api-version": SUBSCRIPTION_API_VERSION},
            headers={"Authorization": f"Bearer {token}"},
            timeout=REQUEST_TIMEOUT,
        )
    except AzureError as exc:
        raise AzureAuthError(_error_message(exc)) from exc
    except requests.RequestException as exc:
        raise AzureAuthError(f"cannot reach Azure: {_error_message(exc)}") from exc

    try:
        body = response.json()
    except ValueError:
        body = {}

    if not response.ok:
        message = body.get("error", {}).get("message") or f"HTTP {response.status_code}"
        raise AzureAuthError(message)

    identity = AzureIdentity(
        subscription_id=subscription_id,
        name=body.get("displayName", subscription_id),
        tenant_id=body.get("tenantId", ""),
        state=body.get("state", "Unknown"),
    )
    if identity.tenant_id.lower() != env["AZURE_TARGET_TENANT_ID"].lower():
        raise AzureAuthError("subscription does not belong to AZURE_TARGET_TENANT_ID")

    return identity


def authenticate() -> AzureIdentity:
    """Run the full authentication check and print the status of each step."""
    try:
        env = load_env()
    except AzureAuthError as exc:
        status(False, f"Verifying .env: {exc}")
        status(False, "Authentication failed")
        raise
    status(True, f"Verifying .env ({ENV_PATH})")

    status(
        True,
        f"Subscription [{mask(env['AZURE_SUBSCRIPTION_ID'])}]  "
        f"Tenant [{mask(env['AZURE_TENANT_ID'])}]  "
        f"Target tenant [{mask(env['AZURE_TARGET_TENANT_ID'])}]",
    )
    status(
        True,
        f"App client [{mask(env['AZURE_APP_CLIENT_ID'])}]  "
        f"UAMI client [{mask(env['AZURE_UAMI_CLIENT_ID'])}]",
    )

    try:
        identity = get_identity()
    except AzureAuthError as exc:
        status(False, "Name: unavailable")
        status(False, f"Authentication failed: {exc}")
        raise
    status(True, f"Name: {identity.name} ({identity.state})")
    status(True, "Authentication successful")

    return identity


def main() -> int:
    """CLI entry point: exit code 0 on success, 1 on failure."""
    try:
        authenticate()
    except AzureAuthError:
        return 1
    return 0