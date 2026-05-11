"""MCP (Model Context Protocol) client for Ouroboros.

This is a *client-only* layer: Ouroboros connects to one or more external
MCP servers, lists their tools/resources, and exposes the tools through
the existing :class:`ouroboros.tools.registry.ToolRegistry` so the
LLM can invoke them like any other tool. Ouroboros does NOT (yet) expose
its own tools as an MCP server.

Design choices
--------------

* **Settings-driven**: the active server list is read from
  ``MCP_SERVERS`` in settings.json. Each entry is a dict with ``id``,
  ``name``, ``enabled``, ``transport``, ``url``, ``auth_header``,
  ``auth_token``, and ``allowed_tools``. The schema is documented in
  :func:`normalize_server_config`.
* **Hot-reloadable**: :func:`reconfigure` is called whenever
  ``/api/settings`` saves a new payload; it rebuilds the server table
  without restarting the process. No long-lived sessions are kept open
  between calls — every ``call_tool`` opens a fresh transport, runs the
  request, and closes it. This trades latency for simplicity and makes
  the client robust against transient errors.
* **Provider-safe tool names**: the LLM sees tools as
  ``mcp_<serverSlug>__<toolSlug>`` clamped to the OpenAI/Anthropic 64-char
  limit. Slugs are sanitized to alnum/underscore and truncated with a
  short SHA1 suffix when they would overflow.
* **Secret hygiene**: ``auth_token`` is never returned via the status
  payload. The HTTP layer in :mod:`ouroboros.mcp_api` reuses the same
  masking convention as the rest of ``settings.json`` secrets.
* **Network safety**: a small deny-list refuses obvious cloud-metadata
  endpoints to harden against SSRF-style abuse from prompt-injected MCP
  configs.
* **SDK optional**: the Python ``mcp`` package is an optional runtime
  dependency. When it is missing, the manager remains importable, all
  configuration plumbing keeps working, and probe/call calls return a
  structured error explaining the missing dependency. This mirrors the
  ``a2a-sdk`` ``try/except ImportError`` pattern in ``a2a_server.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import re
import threading
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SDK availability — optional dependency, mirroring the A2A pattern.
# ---------------------------------------------------------------------------

try:  # pragma: no cover - import guard exercised by tests via monkeypatch
    from mcp import ClientSession  # type: ignore
    from mcp.client.streamable_http import streamablehttp_client  # type: ignore
    from mcp.client.sse import sse_client  # type: ignore

    _MCP_SDK_AVAILABLE = True
    _MCP_SDK_IMPORT_ERROR: Optional[str] = None
except Exception as _import_exc:  # pragma: no cover - defensive
    ClientSession = None  # type: ignore[assignment]
    streamablehttp_client = None  # type: ignore[assignment]
    sse_client = None  # type: ignore[assignment]
    _MCP_SDK_AVAILABLE = False
    _MCP_SDK_IMPORT_ERROR = f"{type(_import_exc).__name__}: {_import_exc}"


SUPPORTED_TRANSPORTS = ("streamable_http", "sse")
TOOL_NAME_PREFIX = "mcp_"
_TOOL_NAME_PATTERN = re.compile(r"^mcp_[A-Za-z0-9_]+__[A-Za-z0-9_]+$")
_MAX_TOOL_NAME_LEN = 64
_MAX_SERVER_SLUG = 24
_MAX_TOOL_SLUG = 32

# Hard-coded deny list for obvious SSRF-style targets that are extremely
# unlikely to be a legitimate MCP endpoint. We do NOT block private LAN
# ranges by default — many users run MCP servers on localhost or on a
# trusted home network.
_DENIED_HOSTS = frozenset(
    {
        "169.254.169.254",  # AWS / Azure / OCI metadata
        "100.100.100.200",  # Alibaba metadata
        "metadata.google.internal",
        "metadata",
    }
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MCPServerConfig:
    """Validated, normalized MCP server config."""

    id: str
    name: str
    enabled: bool
    transport: str
    url: str
    auth_header: str
    auth_token: str
    allowed_tools: List[str]

    def has_auth(self) -> bool:
        return bool(self.auth_token.strip())

    def sanitized_id(self) -> str:
        return self.id


@dataclass
class MCPTool:
    """A discovered tool from an MCP server, normalized for the registry."""

    server_id: str
    raw_name: str
    prefixed_name: str
    description: str
    schema: Dict[str, Any]


@dataclass
class MCPServerRuntime:
    """Mutable per-server runtime state (discovered tools + last status)."""

    config: MCPServerConfig
    tools: List[MCPTool] = field(default_factory=list)
    last_error: str = ""
    last_refreshed: str = ""
    last_attempted: str = ""


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _slugify(value: str, *, max_len: int) -> str:
    """Return a provider-safe lowercase alnum/underscore slug.

    Empty / non-string inputs return ``""``. Long inputs are truncated
    deterministically with a short SHA1 suffix so two distinct tools cannot
    collide just because their first N characters match.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    # Replace any non-alnum/underscore character with ``_``, lowercase the
    # result, collapse repeated underscores, and trim leading/trailing ``_``.
    safe = re.sub(r"[^A-Za-z0-9_]", "_", text)
    safe = re.sub(r"_+", "_", safe).strip("_").lower()
    if not safe:
        return ""
    if len(safe) <= max_len:
        return safe
    digest = hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:6]
    keep = max_len - len(digest) - 1
    if keep <= 0:
        return digest
    return f"{safe[:keep]}_{digest}"


def canonical_server_id(value: str) -> str:
    """Return the canonical persisted/runtime MCP server id.

    The Settings UI accepts friendly input (spaces, uppercase, punctuation),
    but every downstream surface (settings.json, /api/settings, /api/mcp/*,
    MCPManager) must agree on a single id. Persisting the canonical id avoids
    a split where the manager reports ``github_server`` but the UI still calls
    refresh/test with ``GitHub Server!``.
    """
    return _slugify(value, max_len=_MAX_SERVER_SLUG)


def make_tool_name(server_id: str, tool_name: str) -> str:
    """Return the provider-safe ``mcp_<server>__<tool>`` string."""
    server_slug = canonical_server_id(server_id)
    tool_slug = _slugify(tool_name, max_len=_MAX_TOOL_SLUG)
    if not server_slug or not tool_slug:
        return ""
    candidate = f"{TOOL_NAME_PREFIX}{server_slug}__{tool_slug}"
    if len(candidate) > _MAX_TOOL_NAME_LEN:
        # ``_slugify`` already capped each side; if we still overflow, hash
        # the whole tail and rebuild.
        digest = hashlib.sha1(candidate.encode("utf-8")).hexdigest()[:6]
        candidate = f"{TOOL_NAME_PREFIX}{server_slug}__{digest}"
    return candidate


def parse_tool_name(name: str) -> Optional[Dict[str, str]]:
    """Reverse :func:`make_tool_name` into ``{server_slug, tool_slug}``.

    Returns ``None`` for non-MCP names or invalid shapes.
    """
    text = str(name or "")
    if not text.startswith(TOOL_NAME_PREFIX):
        return None
    if not _TOOL_NAME_PATTERN.match(text):
        return None
    body = text[len(TOOL_NAME_PREFIX):]
    if "__" not in body:
        return None
    server, tool = body.split("__", 1)
    return {"server_slug": server, "tool_slug": tool}


def is_mcp_tool_name(name: str) -> bool:
    """Return True when ``name`` looks like a manager-issued MCP tool name."""
    return parse_tool_name(name) is not None


def _validate_url(url: str) -> str:
    """Return a normalized URL or raise ``ValueError``.

    Accepts only ``http``/``https`` schemes and refuses obvious SSRF
    targets (``169.254.169.254``, cloud-metadata hostnames). Private LAN
    addresses are allowed — many users run MCP servers on loopback.
    """
    text = str(url or "").strip()
    if not text:
        raise ValueError("url is required")
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(
            "MCP server url must use http:// or https:// (got "
            f"{parsed.scheme or 'no scheme'!r})"
        )
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise ValueError("MCP server url is missing a hostname")
    if host in _DENIED_HOSTS:
        raise ValueError(f"MCP server hostname {host!r} is on the deny list")
    # Also block obvious link-local IPs even when not in the literal deny
    # list (e.g. someone supplies 169.254.169.254 with port).
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        addr = None
    if addr is not None and addr.is_link_local:
        raise ValueError(
            f"MCP server hostname {host!r} is a link-local address"
        )
    return text


def _coerce_str_list(value: Any) -> List[str]:
    if value in (None, "", [], ()):
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def normalize_server_config(raw: Dict[str, Any]) -> Optional[MCPServerConfig]:
    """Validate and normalize a single ``MCP_SERVERS`` entry.

    Returns ``None`` for unsalvageable entries; the caller (typically
    :func:`reconfigure`) logs a warning. Returns a frozen
    :class:`MCPServerConfig` otherwise.

    The schema (deliberately small for v1):

    .. code-block:: yaml

        id: "github"            # required, sanitized into a slug
        name: "GitHub MCP"      # optional pretty name
        enabled: true           # default false
        transport: streamable_http   # one of SUPPORTED_TRANSPORTS
        url: "https://..."      # required for HTTP transports
        auth_header: "Authorization"   # default "Authorization"
        auth_token: "Bearer xxx"        # optional, masked on the wire
        allowed_tools: ["search", ...]  # empty list = allow every discovered tool
    """
    if not isinstance(raw, dict):
        return None

    raw_id = raw.get("id") or raw.get("slug") or raw.get("name")
    server_slug = canonical_server_id(raw_id)
    if not server_slug:
        return None

    transport = str(raw.get("transport") or "streamable_http").strip().lower()
    if transport not in SUPPORTED_TRANSPORTS:
        return None

    try:
        url = _validate_url(raw.get("url") or "")
    except ValueError:
        return None

    name = str(raw.get("name") or raw.get("label") or server_slug).strip() or server_slug
    enabled_raw = raw.get("enabled", False)
    if isinstance(enabled_raw, bool):
        enabled = enabled_raw
    else:
        enabled = str(enabled_raw or "").strip().lower() in {"1", "true", "yes", "on"}

    auth_header = str(raw.get("auth_header") or "Authorization").strip() or "Authorization"
    auth_token = str(raw.get("auth_token") or "").strip()

    allowed_tools = _coerce_str_list(raw.get("allowed_tools"))

    return MCPServerConfig(
        id=server_slug,
        name=name,
        enabled=enabled,
        transport=transport,
        url=url,
        auth_header=auth_header,
        auth_token=auth_token,
        allowed_tools=allowed_tools,
    )


def parse_servers(raw_list: Any) -> List[MCPServerConfig]:
    """Normalize a raw ``MCP_SERVERS`` list. Drops invalid entries silently."""
    if not isinstance(raw_list, list):
        return []
    out: List[MCPServerConfig] = []
    seen: set = set()
    for entry in raw_list:
        cfg = normalize_server_config(entry)
        if cfg is None:
            continue
        if cfg.id in seen:
            # Same id appearing twice would create duplicate tool prefixes
            # and confuse the registry; keep the first occurrence only.
            continue
        seen.add(cfg.id)
        out.append(cfg)
    return out


def redact_servers_for_status(configs: List[MCPServerConfig]) -> List[Dict[str, Any]]:
    """Return a list of dicts safe to send to the UI (auth tokens masked)."""
    out: List[Dict[str, Any]] = []
    for cfg in configs:
        out.append(
            {
                "id": cfg.id,
                "name": cfg.name,
                "enabled": cfg.enabled,
                "transport": cfg.transport,
                "url": cfg.url,
                "auth_header": cfg.auth_header,
                "auth_token": _mask_token(cfg.auth_token),
                "auth_configured": cfg.has_auth(),
                "allowed_tools": list(cfg.allowed_tools),
            }
        )
    return out


def _mask_token(value: str) -> str:
    text = str(value or "")
    if not text:
        return ""
    return text[:4] + "..." if len(text) > 4 else "***"


# ---------------------------------------------------------------------------
# Async transport — the only place that imports the optional ``mcp`` SDK.
# ---------------------------------------------------------------------------


async def _list_tools_async(cfg: MCPServerConfig, *, timeout_sec: int) -> List[Dict[str, Any]]:
    """Connect to ``cfg`` and return the raw discovered tools list.

    Returns ``[]`` when the SDK is not available so callers see a clean
    "no tools" outcome with a populated ``last_error``. Errors are
    re-raised so the caller can surface them in the status payload.
    """
    if not _MCP_SDK_AVAILABLE:
        raise RuntimeError(
            "MCP client SDK not installed. Add `mcp>=1.6` to the runtime."
        )
    headers = {}
    if cfg.has_auth():
        headers[cfg.auth_header] = cfg.auth_token

    async def _do_with_session(session_factory) -> List[Dict[str, Any]]:
        async with session_factory as transport_ctx:
            # Both transports yield ``(read, write[, ...])``; we only need
            # the first two streams.
            streams = transport_ctx
            if isinstance(streams, tuple):
                read, write = streams[0], streams[1]
            else:
                read, write = streams.read, streams.write  # pragma: no cover
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                tools_raw: List[Dict[str, Any]] = []
                for tool in result.tools or []:
                    tools_raw.append(
                        {
                            "name": getattr(tool, "name", ""),
                            "description": getattr(tool, "description", "") or "",
                            "input_schema": getattr(tool, "inputSchema", {}) or {},
                        }
                    )
                return tools_raw

    if cfg.transport == "streamable_http":
        factory = streamablehttp_client(cfg.url, headers=headers)
    elif cfg.transport == "sse":
        factory = sse_client(cfg.url, headers=headers)
    else:  # pragma: no cover - guarded by parse_servers
        raise RuntimeError(f"Unsupported transport: {cfg.transport!r}")

    return await asyncio.wait_for(_do_with_session(factory), timeout=timeout_sec)


async def _call_tool_async(
    cfg: MCPServerConfig, tool_name: str, arguments: Dict[str, Any], *, timeout_sec: int
) -> str:
    """Open a fresh session, call one tool, and return a stringified result."""
    if not _MCP_SDK_AVAILABLE:
        raise RuntimeError(
            "MCP client SDK not installed. Add `mcp>=1.6` to the runtime."
        )
    headers = {}
    if cfg.has_auth():
        headers[cfg.auth_header] = cfg.auth_token

    async def _do() -> str:
        if cfg.transport == "streamable_http":
            factory = streamablehttp_client(cfg.url, headers=headers)
        else:
            factory = sse_client(cfg.url, headers=headers)
        async with factory as transport_ctx:
            streams = transport_ctx
            if isinstance(streams, tuple):
                read, write = streams[0], streams[1]
            else:
                read, write = streams.read, streams.write  # pragma: no cover
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)
                return _stringify_call_result(result)

    return await asyncio.wait_for(_do(), timeout=timeout_sec)


def _stringify_call_result(result: Any) -> str:
    """Convert an ``mcp.types.CallToolResult`` into a model-friendly string.

    The SDK exposes results as a list of content parts (``TextContent``,
    ``ImageContent``, etc.) plus an ``isError`` flag. We concatenate text
    parts and JSON-encode anything else so the model gets a faithful
    representation without us hallucinating fields.
    """
    parts: List[str] = []
    is_error = bool(getattr(result, "isError", False) or getattr(result, "is_error", False))
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if isinstance(text, str) and text:
            parts.append(text)
            continue
        # Best-effort JSON dump for non-text parts.
        try:
            parts.append(json.dumps(_serialize_content_part(item), ensure_ascii=False))
        except Exception:
            parts.append(repr(item))
    if not parts and getattr(result, "structuredContent", None):
        try:
            parts.append(json.dumps(result.structuredContent, ensure_ascii=False))
        except Exception:
            parts.append(repr(result.structuredContent))
    body = "\n\n".join(parts).strip() or "(empty result)"
    if is_error:
        return f"⚠️ MCP_TOOL_ERROR: {body}"
    return body


def _serialize_content_part(item: Any) -> Dict[str, Any]:
    """Best-effort conversion of an MCP content part into a JSON-safe dict."""
    out: Dict[str, Any] = {}
    for attr in ("type", "uri", "mimeType", "data", "annotations"):
        value = getattr(item, attr, None)
        if value is not None and not callable(value):
            out[attr] = value
    return out


# ---------------------------------------------------------------------------
# Sync wrapper — survives both "no event loop" and "running event loop" cases.
# ---------------------------------------------------------------------------


def _run_async(coro_factory: Callable[[], Awaitable[Any]]) -> Any:
    """Run an async coroutine from a synchronous caller.

    The caller passes a *factory* (``lambda: coro()``) instead of a coroutine
    object so that, when we end up needing a fresh coroutine on the second
    branch, we don't reuse a closed one.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_factory())

    # We're on a thread that already has a running loop — do a sub-thread
    # ``asyncio.run`` so we don't have to await it back into the parent loop.
    holder: Dict[str, Any] = {}

    def _runner() -> None:
        try:
            holder["value"] = asyncio.run(coro_factory())
        except BaseException as exc:
            holder["error"] = exc

    thread = threading.Thread(target=_runner, name="mcp-sync-runner", daemon=True)
    thread.start()
    thread.join()
    if "error" in holder:
        raise holder["error"]
    return holder.get("value")


# ---------------------------------------------------------------------------
# Manager — module-global singleton.
# ---------------------------------------------------------------------------


class MCPManager:
    """Process-wide registry of configured MCP servers + discovered tools."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._enabled = False
        self._tool_timeout_sec = 60
        self._servers: Dict[str, MCPServerRuntime] = {}
        # Hook used by tests to inject a fake transport. Production callers
        # rely on the real ``_list_tools_async`` / ``_call_tool_async``.
        self._async_list_tools: Callable[[MCPServerConfig, int], Awaitable[List[Dict[str, Any]]]] = (
            lambda cfg, timeout: _list_tools_async(cfg, timeout_sec=timeout)
        )
        self._async_call_tool: Callable[
            [MCPServerConfig, str, Dict[str, Any], int], Awaitable[str]
        ] = (
            lambda cfg, name, args, timeout: _call_tool_async(
                cfg, name, args, timeout_sec=timeout
            )
        )

    # -- configuration ------------------------------------------------------

    def reconfigure(self, settings: Dict[str, Any]) -> None:
        """Rebuild the server table from a freshly loaded settings dict.

        Preserves previously discovered tools for servers whose config did
        not change so an in-flight task continues to see the same tool
        schemas without an immediate re-discover round-trip.
        """
        with self._lock:
            self._enabled = bool(settings.get("MCP_ENABLED"))
            try:
                self._tool_timeout_sec = max(1, int(settings.get("MCP_TOOL_TIMEOUT_SEC") or 60))
            except (TypeError, ValueError):
                self._tool_timeout_sec = 60
            new_configs = parse_servers(settings.get("MCP_SERVERS"))
            new_servers: Dict[str, MCPServerRuntime] = {}
            for cfg in new_configs:
                old = self._servers.get(cfg.id)
                if old is not None and old.config == cfg:
                    new_servers[cfg.id] = old
                else:
                    new_servers[cfg.id] = MCPServerRuntime(config=cfg)
            self._servers = new_servers

    # -- introspection ------------------------------------------------------

    def is_enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def server_ids(self) -> List[str]:
        with self._lock:
            return list(self._servers.keys())

    def server_count(self) -> int:
        with self._lock:
            return len(self._servers)

    def tool_timeout_sec(self) -> int:
        with self._lock:
            return self._tool_timeout_sec

    def list_tools_for_registry(self) -> List[Dict[str, Any]]:
        """Return enabled-server tool descriptors in ToolRegistry shape."""
        with self._lock:
            if not self._enabled:
                return []
            results: List[Dict[str, Any]] = []
            for runtime in self._servers.values():
                cfg = runtime.config
                if not cfg.enabled:
                    continue
                allowed = set(cfg.allowed_tools)
                for tool in runtime.tools:
                    if allowed and tool.raw_name not in allowed:
                        continue
                    results.append(
                        {
                            "name": tool.prefixed_name,
                            "description": tool.description,
                            "schema": tool.schema,
                            "server_id": tool.server_id,
                            "raw_name": tool.raw_name,
                        }
                    )
            return results

    def get_tool(self, prefixed_name: str) -> Optional[Dict[str, Any]]:
        for tool in self.list_tools_for_registry():
            if tool["name"] == prefixed_name:
                return tool
        return None

    def status_payload(self) -> Dict[str, Any]:
        """Return a redacted status snapshot for ``/api/mcp/status``."""
        with self._lock:
            servers: List[Dict[str, Any]] = []
            for runtime in self._servers.values():
                cfg = runtime.config
                servers.append(
                    {
                        "id": cfg.id,
                        "name": cfg.name,
                        "enabled": cfg.enabled,
                        "transport": cfg.transport,
                        "url": cfg.url,
                        "auth_header": cfg.auth_header,
                        "auth_configured": cfg.has_auth(),
                        "allowed_tools": list(cfg.allowed_tools),
                        "tool_count": len(runtime.tools),
                        "tools": [
                            {
                                "name": tool.raw_name,
                                "prefixed_name": tool.prefixed_name,
                                "description": tool.description,
                            }
                            for tool in runtime.tools
                        ],
                        "last_error": runtime.last_error,
                        "last_refreshed": runtime.last_refreshed,
                        "last_attempted": runtime.last_attempted,
                    }
                )
            return {
                "enabled": self._enabled,
                "sdk_available": _MCP_SDK_AVAILABLE,
                "sdk_error": _MCP_SDK_IMPORT_ERROR or "",
                "tool_timeout_sec": self._tool_timeout_sec,
                "servers": servers,
            }

    # -- discovery ----------------------------------------------------------

    def refresh_server(self, server_id: str) -> Dict[str, Any]:
        """Re-list tools for one server. Returns a per-server status dict."""
        with self._lock:
            runtime = self._servers.get(server_id)
            if runtime is None:
                return {
                    "ok": False,
                    "error": f"unknown server id: {server_id!r}",
                }
            cfg = runtime.config
            timeout = self._tool_timeout_sec

        attempted_at = datetime.now(timezone.utc).isoformat()
        try:
            tools_raw = _run_async(lambda: self._async_list_tools(cfg, timeout))
        except BaseException as exc:  # noqa: BLE001 - surface any failure
            with self._lock:
                target = self._servers.get(server_id)
                if target is not None:
                    target.last_error = f"{type(exc).__name__}: {exc}"
                    target.last_attempted = attempted_at
                    target.tools = []
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        normalized = [
            MCPTool(
                server_id=cfg.id,
                raw_name=str(item.get("name") or "").strip(),
                prefixed_name=make_tool_name(cfg.id, item.get("name") or ""),
                description=str(item.get("description") or "")[:1024],
                schema=_normalize_input_schema(item.get("input_schema")),
            )
            for item in tools_raw
            if str(item.get("name") or "").strip()
        ]
        normalized = [tool for tool in normalized if tool.prefixed_name]
        # Drop duplicates by prefixed_name (e.g. tools whose names collapse
        # to the same slug after sanitisation).
        seen: set = set()
        deduped: List[MCPTool] = []
        for tool in normalized:
            if tool.prefixed_name in seen:
                continue
            seen.add(tool.prefixed_name)
            deduped.append(tool)

        finished_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            target = self._servers.get(server_id)
            if target is not None:
                target.tools = deduped
                target.last_error = ""
                target.last_attempted = attempted_at
                target.last_refreshed = finished_at
        return {
            "ok": True,
            "server_id": cfg.id,
            "tool_count": len(deduped),
            "tools": [
                {
                    "name": tool.raw_name,
                    "prefixed_name": tool.prefixed_name,
                    "description": tool.description,
                }
                for tool in deduped
            ],
        }

    def refresh_all(self) -> Dict[str, Any]:
        """Refresh every enabled server. Returns per-server outcomes."""
        outcomes: Dict[str, Any] = {}
        with self._lock:
            ids = [cfg_id for cfg_id, rt in self._servers.items() if rt.config.enabled]
        for server_id in ids:
            outcomes[server_id] = self.refresh_server(server_id)
        return {"refreshed": outcomes}

    def test_server(self, raw_config: Dict[str, Any]) -> Dict[str, Any]:
        """Probe a candidate config without persisting it.

        Used by the UI ``Test connection`` action so the user can confirm
        their URL/auth before saving the new config to ``settings.json``.
        """
        cfg = normalize_server_config(raw_config)
        if cfg is None:
            return {
                "ok": False,
                "error": "Invalid MCP server config (missing id/url, unsupported transport, or denied URL).",
            }
        timeout = self._tool_timeout_sec
        try:
            tools_raw = _run_async(lambda: self._async_list_tools(cfg, timeout))
        except BaseException as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {
            "ok": True,
            "server_id": cfg.id,
            "tool_count": len(tools_raw),
            "tools": [
                {
                    "name": str(t.get("name") or ""),
                    "description": str(t.get("description") or "")[:512],
                }
                for t in tools_raw
            ],
        }

    # -- dispatch -----------------------------------------------------------

    def call_tool(self, prefixed_name: str, arguments: Dict[str, Any]) -> str:
        """Synchronously invoke an MCP tool. Returns a model-friendly string.

        Errors are returned as ``"⚠️ MCP_TOOL_ERROR: ..."`` strings rather
        than raised so the caller (``ToolRegistry.execute``) can surface
        them through the standard tool result channel.
        """
        if not self.is_enabled():
            return "⚠️ MCP_DISABLED: enable MCP in Settings → Integrations to use this tool."
        with self._lock:
            tool_descriptor = None
            for runtime in self._servers.values():
                cfg = runtime.config
                if not cfg.enabled:
                    continue
                allowed = set(cfg.allowed_tools)
                for tool in runtime.tools:
                    if tool.prefixed_name == prefixed_name:
                        if allowed and tool.raw_name not in allowed:
                            return (
                                f"⚠️ MCP_TOOL_DISALLOWED: {tool.raw_name!r} is not on the "
                                f"allowed_tools list for server {cfg.id!r}."
                            )
                        tool_descriptor = (cfg, tool)
                        break
                if tool_descriptor:
                    break
            if not tool_descriptor:
                return (
                    f"⚠️ MCP_TOOL_NOT_FOUND: {prefixed_name!r}. Refresh the server in "
                    "Settings → Integrations or check the allowed_tools allowlist."
                )
            cfg, tool = tool_descriptor
            timeout = self._tool_timeout_sec
        try:
            text = _run_async(
                lambda: self._async_call_tool(cfg, tool.raw_name, arguments or {}, timeout)
            )
        except asyncio.TimeoutError:
            return f"⚠️ MCP_TOOL_TIMEOUT: server {cfg.id!r} did not respond in {timeout}s"
        except BaseException as exc:  # noqa: BLE001 - any failure is reported
            return f"⚠️ MCP_TOOL_ERROR: {type(exc).__name__}: {exc}"
        return text


def _normalize_input_schema(value: Any) -> Dict[str, Any]:
    """Coerce an MCP-server-supplied input_schema into an OpenAI-style schema.

    OpenAI/Anthropic tool schemas expect a JSON Schema object with at least
    ``type: object`` and a ``properties`` map. MCP servers usually return
    that shape directly, but a defensive normaliser keeps a malformed
    server from crashing the registry.
    """
    if not isinstance(value, dict):
        return {"type": "object", "properties": {}}
    out = dict(value)
    if out.get("type") != "object":
        out["type"] = "object"
    if "properties" not in out or not isinstance(out["properties"], dict):
        out["properties"] = {}
    return out


# ---------------------------------------------------------------------------
# Module-level singleton + convenience helpers.
# ---------------------------------------------------------------------------


_manager_lock = threading.Lock()
_manager: Optional[MCPManager] = None


def get_manager() -> MCPManager:
    """Return the process-global :class:`MCPManager`, creating it on first use."""
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = MCPManager()
        return _manager


def reset_manager_for_tests() -> None:
    """Test-only helper to drop the module-level singleton."""
    global _manager
    with _manager_lock:
        _manager = None


def reconfigure_from_settings(settings: Dict[str, Any]) -> None:
    """Reconfigure the global manager from a settings dict.

    Public helper so :mod:`server` can call this on every settings save
    without importing the MCPManager class directly.
    """
    get_manager().reconfigure(settings)


def call_mcp_tool(name: str, arguments: Dict[str, Any]) -> str:
    """Synchronous tool-call helper for :class:`ToolRegistry`."""
    return get_manager().call_tool(name, arguments or {})
