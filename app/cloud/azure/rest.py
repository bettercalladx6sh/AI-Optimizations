"""Read-only Azure REST calls: Resource Manager, Azure Monitor metrics, Log Analytics and the
public Retail Prices API.

Calls use explicit API versions, so responses keep the same shape whatever Azure SDK version
is installed. Every request goes through the shared backoff (app/cloud/backoff.py): throttling
(429), server errors (5xx) and connection errors are retried with jitter, honouring Retry-After;
other errors fail at once with fix hints. Only GET requests are sent, except the Log Analytics
query endpoint, which takes its (read-only) query as a POST body.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

import requests

from app.auth import azure_auth
from app.auth.common import AuthError
from app.cloud.backoff import retry

ARM_ENDPOINT = "https://management.azure.com"
LOG_ANALYTICS_ENDPOINT = "https://api.loganalytics.io/v1"
TIMEOUT = (10, 120)                  # seconds: connect, read

_session = requests.Session()


class AzureApiError(AuthError):
    """Raised when an Azure API call fails (permissions, bad input or network)."""


class _HttpError(Exception):
    """An HTTP error response, kept for the retry decision and the final error message."""

    def __init__(self, status: int, code: str, message: str, retry_after: float | None) -> None:
        super().__init__(f"HTTP {status} {code}: {message}")
        self.status, self.code, self.message, self.retry_after = status, code, message, retry_after


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    return isinstance(exc, _HttpError) and (exc.status == 429 or exc.status >= 500)


def _describe(exc: BaseException) -> str:
    return f"HTTP {exc.status}" if isinstance(exc, _HttpError) else type(exc).__name__


def _wait_hint(exc: BaseException) -> float | None:
    return exc.retry_after if isinstance(exc, _HttpError) else None


def _error(response: requests.Response) -> _HttpError:
    try:
        body = response.json()
    except ValueError:
        body = {}
    error = body.get("error") if isinstance(body, dict) else None
    error = error if isinstance(error, dict) else {}
    message = error.get("message") or response.reason or "request failed"
    retry_after = response.headers.get("Retry-After")
    try:
        wait = float(retry_after) if retry_after else None
    except ValueError:
        wait = None
    return _HttpError(response.status_code, error.get("code", ""), message.splitlines()[0], wait)


def _send(method: str, url: str, scope: str | None, params: Mapping[str, Any] | None,
          body: Any | None) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if scope:
        headers["Authorization"] = f"Bearer {azure_auth.access_token(scope)}"
    response = _session.request(method, url, params=params, json=body, headers=headers, timeout=TIMEOUT)
    if response.status_code >= 400:
        raise _error(response)
    return response.json()


def request(
    method: str,
    url: str,
    action: str,
    scope: str | None = azure_auth.ARM_SCOPE,
    params: Mapping[str, Any] | None = None,
    body: Any | None = None,
    permission: str = "the Reader role on the subscription",
) -> dict[str, Any]:
    """One JSON request with backoff; errors become AzureApiError with fix hints.

    `action` names the call in messages (e.g. 'Microsoft.Compute/virtualMachines/read'),
    `permission` what the identity needs when Azure answers 401/403.
    """
    try:
        return retry(_send, method, url, scope, params, body, retryable=_retryable, describe=_describe,
                     wait_hint=_wait_hint, label=action)
    except _HttpError as exc:
        hints: tuple[str, ...] = ()
        if exc.status in (401, 403):
            hints = (f"Grant the identity {permission} ({action})",)
        elif exc.status == 404:
            hints = ("Check AZURE_SUBSCRIPTION_ID and the resource names",)
        elif exc.status == 429:
            hints = ("Azure is throttling requests; wait a few minutes and run again",)
        raise AzureApiError(f"{action}: {exc}", hints) from exc
    except requests.RequestException as exc:
        raise AzureApiError(f"{action}: {type(exc).__name__}: {exc}", ("Check the network connection",)) from exc


def arm_get(path: str, api_version: str, action: str, params: Mapping[str, Any] | None = None,
            permission: str = "the Reader role on the subscription") -> dict[str, Any]:
    """GET one Resource Manager resource, e.g. arm_get('/subscriptions/<id>', '2022-12-01', ...)."""
    return request("GET", f"{ARM_ENDPOINT}{path}", action,
                   params={"api-version": api_version, **(params or {})}, permission=permission)


def arm_list(path: str, api_version: str, action: str, params: Mapping[str, Any] | None = None,
             permission: str = "the Reader role on the subscription") -> Iterator[dict[str, Any]]:
    """Every item of a Resource Manager list, following nextLink pages."""
    page = arm_get(path, api_version, action, params, permission)
    while True:
        yield from page.get("value", [])
        next_link = page.get("nextLink")
        if not next_link:
            return
        page = request("GET", next_link, action, permission=permission)


def subscription_path() -> str:
    return f"/subscriptions/{azure_auth.load_env()['AZURE_SUBSCRIPTION_ID']}"


def log_analytics_query(workspace_id: str, query: str, timespan: str) -> list[dict[str, Any]]:
    """Run a read-only KQL query; return the rows of the primary table as dicts."""
    result = request(
        "POST", f"{LOG_ANALYTICS_ENDPOINT}/workspaces/{workspace_id}/query", "Log Analytics query",
        scope=azure_auth.LOG_ANALYTICS_SCOPE, body={"query": query, "timespan": timespan},
        permission="the Log Analytics Reader role on the workspace AZURE_LOG_ANALYTICS_WORKSPACE_ID",
    )
    tables = result.get("tables") or []
    if not tables:
        return []
    columns = [column["name"] for column in tables[0]["columns"]]
    return [dict(zip(columns, row)) for row in tables[0]["rows"]]


def public_get(url: str, action: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """GET a public, unauthenticated endpoint (the Azure Retail Prices API)."""
    return request("GET", url, action, scope=None, params=params)
