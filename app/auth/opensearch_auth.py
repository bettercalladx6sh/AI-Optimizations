"""OpenSearch authentication: the single entry point for every OpenSearch connection in the app.

TLS follows the URL scheme (https:// or http://). Certificate verification is off
by default so self-signed UAT certificates work; set OPENSEARCH_VERIFY_CERTS=true
to enable it.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlparse

import urllib3
from opensearchpy import OpenSearch
from opensearchpy.exceptions import (
    AuthenticationException,
    AuthorizationException,
    ConnectionTimeout,
    NotFoundError,
    RequestError,
    SSLError,
    TransportError,
)
from opensearchpy.exceptions import ConnectionError as OpenSearchConnectionError

from app.auth.common import ENV_PATH, AuthError, read_env, report_failure, status

REQUIRED_VARS = ("OPENSEARCH_URL", "OPENSEARCH_USER", "OPENSEARCH_PASSWORD")
OPTIONAL_VARS = {"OPENSEARCH_VERIFY_CERTS": "false"}

REQUEST_TIMEOUT = 10
MAX_RETRIES = 2
AUTHINFO_PATH = "/_plugins/_security/authinfo"

TRUE_VALUES = {"true", "1", "yes", "on"}
FALSE_VALUES = {"false", "0", "no", "off"}

NETWORK_HINTS = (
    "Check the host and port in OPENSEARCH_URL",
    "Check the firewall / security group allows your IP",
    "Check the URL scheme matches the server (https:// vs http://)",
)

# Checked in order: subclasses (SSLError, ConnectionTimeout) before ConnectionError.
ERROR_HINTS: tuple[tuple[type[TransportError], tuple[str, ...]], ...] = (
    (AuthenticationException, ("Check OPENSEARCH_USER and OPENSEARCH_PASSWORD",)),
    (AuthorizationException, ("The user is authenticated but lacks permission; check its roles",)),
    (SSLError, (
        "Check the URL scheme matches the server (https:// vs http://)",
        "For a self-signed certificate set OPENSEARCH_VERIFY_CERTS=false",
    )),
    (ConnectionTimeout, NETWORK_HINTS),
    (OpenSearchConnectionError, NETWORK_HINTS),
)


class OpenSearchAuthError(AuthError):
    """Raised when OpenSearch is unreachable or rejects the credentials."""


@dataclass(frozen=True)
class OpenSearchSettings:
    """Validated connection settings from .env."""

    url: str
    user: str
    password: str
    use_ssl: bool
    verify_certs: bool


@dataclass(frozen=True)
class OpenSearchIdentity:
    """Login and cluster details confirmed by the server."""

    user: str
    roles: tuple[str, ...]
    cluster: str
    version: str
    distribution: str


def _parse_bool(name: str, value: str) -> bool:
    """Convert a .env flag such as 'true' or 'false' to a bool."""
    normalized = value.strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    raise OpenSearchAuthError(f"{name} must be true or false, got '{value}'")


def load_settings() -> OpenSearchSettings:
    """Read and validate the OpenSearch settings from .env."""
    env = read_env(REQUIRED_VARS, OpenSearchAuthError, optional=OPTIONAL_VARS)
    url = env["OPENSEARCH_URL"].rstrip("/")
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise OpenSearchAuthError(f"OPENSEARCH_URL must look like https://host:port, got '{url}'")
    try:
        parsed.port
    except ValueError:
        raise OpenSearchAuthError(f"port in OPENSEARCH_URL must be a number, got '{url}'") from None

    return OpenSearchSettings(
        url=url,
        user=env["OPENSEARCH_USER"],
        password=env["OPENSEARCH_PASSWORD"],
        use_ssl=parsed.scheme == "https",
        verify_certs=_parse_bool("OPENSEARCH_VERIFY_CERTS", env["OPENSEARCH_VERIFY_CERTS"]),
    )


@lru_cache(maxsize=1)
def get_client() -> OpenSearch:
    """Return a shared OpenSearch client built from .env (created once, then reused)."""
    settings = load_settings()

    if settings.use_ssl and not settings.verify_certs:
        # Certificate checks are off by choice; don't warn on every request.
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    return OpenSearch(
        hosts=[settings.url],
        http_auth=(settings.user, settings.password),
        use_ssl=settings.use_ssl,
        verify_certs=settings.verify_certs,
        ssl_assert_hostname=None if settings.verify_certs else False,
        ssl_show_warn=False,
        timeout=REQUEST_TIMEOUT,
        max_retries=MAX_RETRIES,
        retry_on_timeout=True,
    )


def _first_line(value: object) -> str:
    text = str(value).strip()
    return text.splitlines()[0] if text else ""


def _to_auth_error(exc: TransportError) -> OpenSearchAuthError:
    """Convert an opensearch-py error into OpenSearchAuthError with fix hints."""
    if isinstance(exc, OpenSearchConnectionError):
        message = _first_line(exc.info) or type(exc).__name__
    else:
        message = f"HTTP {exc.status_code}: {_first_line(exc.error)}"

    hints = next((h for cls, h in ERROR_HINTS if isinstance(exc, cls)), ())
    return OpenSearchAuthError(message, hints)


def get_identity() -> OpenSearchIdentity:
    """Confirm the login (security plugin) and read the cluster details."""
    client = get_client()

    try:
        try:
            auth = client.transport.perform_request("GET", AUTHINFO_PATH)
            user = auth.get("user_name", "unknown")
            roles = tuple(auth.get("roles", ()))
        except (NotFoundError, RequestError):
            user, roles = "anonymous (security plugin disabled)", ()
        info = client.info()
    except TransportError as exc:
        raise _to_auth_error(exc) from exc

    version = info.get("version", {})
    return OpenSearchIdentity(
        user=user,
        roles=roles,
        cluster=info.get("cluster_name", "unknown"),
        version=version.get("number", "unknown"),
        distribution=version.get("distribution", "opensearch"),
    )


def _on_off(value: bool) -> str:
    return "on" if value else "off"


def authenticate() -> OpenSearchIdentity:
    """Run the full connection check and print the status of each step."""
    try:
        settings = load_settings()
    except OpenSearchAuthError as exc:
        report_failure("Verifying .env", exc)
        raise
    status(True, f"Verifying .env ({ENV_PATH})")

    status(
        True,
        f"URL [{settings.url}]  User [{settings.user}]  Password [********]  "
        f"TLS [{_on_off(settings.use_ssl)}]  Verify certs [{_on_off(settings.verify_certs)}]",
    )

    try:
        identity = get_identity()
    except OpenSearchAuthError as exc:
        report_failure("Connection", exc)
        raise
    roles = ", ".join(identity.roles) or "none"
    status(
        True,
        f"Name: {identity.user} (roles: {roles}) on cluster '{identity.cluster}' "
        f"- {identity.distribution} {identity.version}",
    )
    status(True, "Authentication successful")

    return identity


def main() -> int:
    """CLI entry point: exit code 0 on success, 1 on failure."""
    try:
        authenticate()
    except OpenSearchAuthError:
        return 1
    return 0