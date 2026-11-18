"""Cliente HTTP async NiFi: GET + POST /access/token + POST/DELETE de consultas read-only."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

import httpx

from nifi_mcp.auth import AuthManager, sanitize_for_log
from nifi_mcp.exceptions import DebugEndpointDenied, NiFiClientError
from nifi_mcp.models import DiscoveryAttempt, GlobalConfig, ServerConfig

logger = logging.getLogger(__name__)

TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}
USABLE_STATUS = range(200, 400)
AUTH_CHALLENGE_STATUS = {401, 403}

ALLOWED_DEBUG_PREFIXES = (
    "/flow/about",
    "/flow/current-user",
    "/flow/cluster/summary",
    "/controller/cluster",
    "/flow/process-groups/",
    "/flow/processors/",
    "/flow/connections/",
    "/flow/bulletin-board",
    "/flow/search-results",
    "/flow/parameter-contexts",
    "/flow/controller/",
    "/authentication/configuration",
    "/access/config",
    "/counters",
    "/resources",
)

# POST/DELETE permitidos solo para consultas read-only (provenance y listado de cola).
_READ_ONLY_POST = (
    re.compile(r"^/provenance/?$"),
    re.compile(r"^/flowfile-queues/[^/]+/listing-requests/?$"),
)
_READ_ONLY_DELETE = (
    re.compile(r"^/provenance/[^/]+/?$"),
    re.compile(r"^/flowfile-queues/[^/]+/listing-requests/[^/]+/?$"),
)

DENIED_DEBUG_FRAGMENTS = (
    "/content",
    "/flowfiles/",
    "/flowfile-queues/",
    "/state",
    "/run-status",
    "/config",
    "/references",
    "/descriptors",
    "/revision",
    "/download",
    "/upload",
    "/nar/",
    "/assets/",
    "/parameter-contexts/",
    "/access/token",
    "/access/logout",
    "/access/login",
    "..",
    "\\",
    "\x00",
)


def _looks_like_nifi(resp: httpx.Response) -> bool:
    ct = (resp.headers.get("content-type") or "").lower()
    if "json" in ct:
        return True
    body = resp.text[:500].lower()
    return "nifi" in body or "application/json" in ct


def _normalize_rel_path(path: str) -> str:
    rel = path.strip()
    if not rel.startswith("/"):
        rel = "/" + rel
    return rel.rstrip("/") or "/"


def _validate_read_only_write(method: str, path: str) -> str:
    rel = _normalize_rel_path(path)
    patterns = _READ_ONLY_POST if method == "POST" else _READ_ONLY_DELETE
    if not any(p.match(rel) for p in patterns):
        raise NiFiClientError(f"Método no permitido: {method} {rel}")
    return rel


def validate_debug_endpoint(endpoint: str) -> str:
    ep = endpoint.strip()
    if not ep:
        raise DebugEndpointDenied("Endpoint no permitido")
    if ep.startswith(("http://", "https://")):
        raise DebugEndpointDenied("Endpoint no permitido")
    if any(fragment in ep for fragment in DENIED_DEBUG_FRAGMENTS):
        raise DebugEndpointDenied("Endpoint no permitido")
    if not ep.startswith("/"):
        ep = "/" + ep
    lower = ep.lower()
    if not any(lower.startswith(prefix.lower()) for prefix in ALLOWED_DEBUG_PREFIXES):
        raise DebugEndpointDenied("Endpoint no permitido")
    if "?" in ep:
        raise DebugEndpointDenied("Endpoint no permitido")
    return ep


class NiFiClient:
    def __init__(
        self,
        server: ServerConfig,
        global_cfg: GlobalConfig,
        auth_manager: AuthManager,
    ) -> None:
        self.server = server
        self.global_cfg = global_cfg
        self.auth = auth_manager
        self._base_url: str | None = server.base_url
        self._discovery_attempts: list[DiscoveryAttempt] = []
        self._client: httpx.AsyncClient | None = None
        self._authenticated = False

    @property
    def base_url(self) -> str | None:
        return self._base_url

    @property
    def authenticated(self) -> bool:
        return self._authenticated

    @property
    def discovery_attempts(self) -> list[DiscoveryAttempt]:
        return list(self._discovery_attempts)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            verify = self.auth.httpx_verify(self.server)
            self._client = httpx.AsyncClient(verify=verify, follow_redirects=True)
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def discover_base_url(self) -> str:
        if self._base_url:
            return self._base_url
        client = await self._get_client()
        timeout = self.server.effective_timeout(self.global_cfg)
        candidate: str | None = None
        candidate_attempt: DiscoveryAttempt | None = None

        for url in self.server.discovery_urls():
            probe_url = f"{url.rstrip('/')}/flow/about"
            attempt = DiscoveryAttempt(url=url, ok=False)
            started = time.perf_counter()
            try:
                resp = await client.get(probe_url, timeout=timeout)
                attempt.latency_ms = round((time.perf_counter() - started) * 1000, 1)
                attempt.status_code = resp.status_code
                attempt.nifi_candidate = _looks_like_nifi(resp)

                if resp.status_code in USABLE_STATUS and attempt.nifi_candidate:
                    attempt.ok = True
                    self._base_url = url.rstrip("/")
                    self._discovery_attempts.append(attempt)
                    return self._base_url

                if resp.status_code in AUTH_CHALLENGE_STATUS and attempt.nifi_candidate:
                    attempt.ok = True
                    attempt.warning = "auth_required"
                    candidate = url.rstrip("/")
                    candidate_attempt = attempt
                    self._discovery_attempts.append(attempt)
                    continue

                if resp.status_code in AUTH_CHALLENGE_STATUS and not attempt.nifi_candidate:
                    attempt.warning = "possible_non_nifi_endpoint"
                attempt.error = f"HTTP {resp.status_code}"
            except httpx.HTTPError as exc:
                attempt.latency_ms = round((time.perf_counter() - started) * 1000, 1)
                attempt.error = sanitize_for_log(exc)
            self._discovery_attempts.append(attempt)

        if candidate and candidate_attempt:
            self._base_url = candidate
            try:
                await self._auth_headers()
                self._authenticated = True
                candidate_attempt.authenticated = True
                retry = await client.get(
                    f"{candidate}/flow/about",
                    headers=await self._auth_headers(),
                    timeout=timeout,
                )
                if retry.status_code in USABLE_STATUS:
                    return self._base_url
                candidate_attempt.warning = "auth_failed_after_discovery"
            except Exception as exc:
                candidate_attempt.warning = sanitize_for_log(exc)
            return self._base_url

        msgs = "; ".join(
            f"{a.url}: {a.error or a.status_code}" for a in self._discovery_attempts
        )
        raise NiFiClientError(
            f"No se pudo descubrir base_url para {self.server.id} ({msgs})",
            server_id=self.server.id,
        )

    async def _auth_headers(self) -> dict[str, str]:
        base = self._base_url or await self.discover_base_url()
        client = await self._get_client()
        headers = await self.auth.ensure_auth_headers(
            client, self.server, base, self.global_cfg
        )
        self._authenticated = bool(headers.get("Authorization"))
        return headers

    async def get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        refresh: bool = False,
    ) -> Any:
        del refresh
        base = self._base_url or await self.discover_base_url()
        rel = path if path.startswith("/") else f"/{path}"
        url = f"{base.rstrip('/')}{rel}"
        return await self._request_json("GET", url, params=params)

    async def post_json_read_only(self, path: str, body: dict[str, Any]) -> Any:
        """POST allowlisted (provenance query, queue listing request)."""
        rel = _validate_read_only_write("POST", path)
        base = self._base_url or await self.discover_base_url()
        url = f"{base.rstrip('/')}{rel}"
        return await self._request_json("POST", url, json_body=body)

    async def delete_json_read_only(self, path: str) -> Any:
        """DELETE allowlisted (limpiar query/listing request; no borra flowfiles)."""
        rel = _validate_read_only_write("DELETE", path)
        base = self._base_url or await self.discover_base_url()
        url = f"{base.rstrip('/')}{rel}"
        return await self._request_json("DELETE", url)

    async def debug_get_json(self, endpoint: str, *, params: dict[str, Any] | None = None) -> Any:
        rel = validate_debug_endpoint(endpoint)
        return await self.get_json(rel, params=params)

    async def get_json_optional(self, path: str) -> dict[str, Any] | None:
        try:
            data = await self.get_json(path)
            return data if isinstance(data, dict) else {}
        except NiFiClientError as exc:
            if exc.status_code in (401, 403, 404):
                return None
            raise

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        if method not in ("GET", "POST", "DELETE"):
            raise NiFiClientError(f"Método no permitido: {method}", server_id=self.server.id)

        client = await self._get_client()
        headers = await self._auth_headers()
        if json_body is not None:
            headers = {**headers, "Content-Type": "application/json"}
        retries = self.server.effective_retries(self.global_cfg)
        timeout = self.server.effective_timeout(self.global_cfg)
        last_exc: Exception | None = None

        for attempt in range(retries + 1):
            try:
                resp = await client.request(
                    method,
                    url,
                    params=params,
                    data=data,
                    json=json_body,
                    headers=headers,
                    timeout=timeout,
                )
                if resp.status_code in TRANSIENT_STATUS and attempt < retries:
                    await asyncio.sleep(self.global_cfg.retry_backoff_seconds * (attempt + 1))
                    continue
                if resp.status_code == 401:
                    self.auth.clear_token(self.server.id)
                    headers = await self._auth_headers()
                    if attempt < retries:
                        continue
                if resp.status_code >= 400:
                    raise NiFiClientError(
                        f"HTTP {resp.status_code} en {self.server.id}",
                        server_id=self.server.id,
                        status_code=resp.status_code,
                    )
                if not resp.content:
                    return {}
                try:
                    return resp.json()
                except json.JSONDecodeError as exc:
                    raise NiFiClientError(
                        f"JSON inválido desde {self.server.id}",
                        server_id=self.server.id,
                    ) from exc
            except NiFiClientError:
                raise
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt < retries:
                    await asyncio.sleep(self.global_cfg.retry_backoff_seconds * (attempt + 1))
                    continue
                raise NiFiClientError(
                    f"Error de red en {self.server.id}: {sanitize_for_log(exc)}",
                    server_id=self.server.id,
                ) from exc
        if last_exc:
            raise NiFiClientError(
                f"Error de red en {self.server.id}: {sanitize_for_log(last_exc)}",
                server_id=self.server.id,
            )
        raise NiFiClientError("Solicitud fallida", server_id=self.server.id)

    async def get_about(self) -> dict[str, Any]:
        data = await self.get_json("/flow/about")
        return data if isinstance(data, dict) else {}

    async def get_auth_configuration(self) -> dict[str, Any] | None:
        data = await self.get_json_optional("/authentication/configuration")
        if data is not None:
            return data
        return await self.get_json_optional("/access/config")

    async def get_current_user(self) -> dict[str, Any] | None:
        return await self.get_json_optional("/flow/current-user")

    async def get_cluster_summary(self) -> dict[str, Any] | None:
        data = await self.get_json_optional("/flow/cluster/summary")
        if data is not None:
            return data
        return await self.get_json_optional("/controller/cluster")
