"""Shared scaffolding for ContextHub online reverse proxies (weak S2).

Both the MCP tool proxy (``online_proxy``) and the agent-to-agent handoff proxy
(``online_handoff_proxy``) are the same shape: receive an HTTP request, run a
ContextHub gate *before* the real upstream executes, then either forward
(observe / allow / deterministic repair) or synthesize a blocking response
(enforce). This module holds the protocol-agnostic, boundary-agnostic pieces so
neither proxy duplicates them:

- mode constants + validation (``observe`` / ``enforce``)
- ``ForwardResponse`` / ``ProxyOutcome`` value types
- generic verdict/violation extraction from any ``*EnforcementResult``
- transparent upstream relay (urllib) + hop-by-hop header hygiene
- a JSONL decision sink
- a threaded HTTP listener bound to one route (option A: identity by port)
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import inspect
import json
import threading
from pathlib import Path
from typing import Any, Protocol
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest


OBSERVE = "observe"
ENFORCE = "enforce"
_VALID_MODES = frozenset({OBSERVE, ENFORCE})

# Verdict→action strings (from EnforcementInterceptor.apply) that mean the gate
# would have stopped the action in enforce mode.
_BLOCK_ACTIONS = frozenset({"block", "pending", "retry_with_feedback"})

# Hop-by-hop and length headers we must not blindly relay upstream.
_HOP_BY_HOP = frozenset(
    {
        "host",
        "content-length",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "accept-encoding",
    }
)

DecisionSink = Callable[[Any], None]


class HasUpstream(Protocol):
    """Any route that knows its upstream endpoint (tool or handoff)."""

    upstream_endpoint: str


class SupportsHandle(Protocol):
    async def handle(
        self, route: Any, *, headers: Mapping[str, str], body: bytes
    ) -> "ProxyOutcome": ...


@dataclass(frozen=True)
class ForwardResponse:
    """A response to return to the calling agent."""

    status: int
    headers: dict[str, str]
    body: bytes


@dataclass(frozen=True)
class ProxyOutcome:
    """Result of handling one request: the response plus any decision record."""

    response: ForwardResponse
    record: Any | None


def validate_mode(mode: str) -> str:
    normalized = str(mode or "").strip().lower()
    if normalized not in _VALID_MODES:
        raise ValueError(f"mode must be one of {sorted(_VALID_MODES)}, got {mode!r}")
    return normalized


def try_parse_json(body: bytes) -> Any:
    """Parse a JSON body, returning dict / list / scalar / None on failure."""

    if not body:
        return None
    try:
        return json.loads(body)
    except (ValueError, TypeError):
        return None


def would_block_action(action: str) -> bool:
    return action in _BLOCK_ACTIONS


def verdict_value(result: Any) -> str:
    verdict = result.decision.verdict
    return verdict.value if hasattr(verdict, "value") else str(verdict)


def violations_json(result: Any) -> list[dict[str, Any]]:
    return [
        {
            "kind": violation.kind.value if hasattr(violation.kind, "value") else str(violation.kind),
            "message": violation.message,
            "repair_hint": violation.repair_hint,
            "evidence": violation.evidence,
        }
        for violation in result.decision.violations
    ]


class JsonlDecisionSink:
    """Append decision records as JSONL. Thread-safe for the threaded server.

    Records only need a ``to_json()`` method, so both the tool and handoff
    decision records work here.
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def __call__(self, record: Any) -> None:
        line = json.dumps(record.to_json(), ensure_ascii=False, sort_keys=True)
        with self._lock:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")


def relayable_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        str(key): str(value)
        for key, value in headers.items()
        if str(key).lower() not in _HOP_BY_HOP
    }


def without_content_length(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        str(key): str(value)
        for key, value in headers.items()
        if str(key).lower() != "content-length"
    }


def default_urllib_forward(
    route: HasUpstream,
    headers: Mapping[str, str],
    body: bytes,
) -> ForwardResponse:
    """Relay a request to the real upstream service, transparently."""

    endpoint = route.upstream_endpoint
    opener = _opener_for_endpoint(endpoint)
    req = urlrequest.Request(url=endpoint, data=body, headers=dict(headers), method="POST")
    try:
        with opener(req, timeout=300) as resp:
            return ForwardResponse(
                status=getattr(resp, "status", None) or resp.getcode(),
                headers=_response_headers(resp),
                body=resp.read(),
            )
    except urlerror.HTTPError as exc:
        # Relay a genuine upstream error unchanged.
        return ForwardResponse(
            status=exc.code,
            headers=_response_headers(exc),
            body=exc.read() if hasattr(exc, "read") else b"",
        )


def _response_headers(resp: Any) -> dict[str, str]:
    headers = getattr(resp, "headers", None)
    if headers is None and hasattr(resp, "info"):
        headers = resp.info()
    if headers is None:
        return {}
    try:
        return {str(key): str(value) for key, value in headers.items()}
    except AttributeError:
        return {}


def _opener_for_endpoint(endpoint: str) -> Callable[..., Any]:
    if _is_loopback_endpoint(endpoint):
        return urlrequest.build_opener(urlrequest.ProxyHandler({})).open
    return urlrequest.urlopen


def _is_loopback_endpoint(endpoint: str) -> bool:
    try:
        host = urlparse.urlparse(endpoint).hostname
    except ValueError:
        return False
    return (host or "").lower() in {"localhost", "127.0.0.1", "::1"}


class ProxyHTTPServer:
    """One listener (= one port) bound to a single route (option A).

    Identity is fixed by the port, so the upstream auth header never has to be
    reverse-mapped. Each agent×upstream gets its own ``ProxyHTTPServer``. Works
    for any ``proxy`` exposing ``async handle(route, *, headers, body)``.
    """

    def __init__(
        self,
        route: Any,
        proxy: SupportsHandle,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
    ):
        self._route = route
        self._proxy = proxy
        handler = _make_handler(route, proxy)
        self._httpd = ThreadingHTTPServer((host, port), handler)
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        return self._httpd.server_address[0], self._httpd.server_address[1]

    @property
    def url(self) -> str:
        host, port = self.address
        return f"http://{host}:{port}"

    def start(self) -> "ProxyHTTPServer":
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __enter__(self) -> "ProxyHTTPServer":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


def _make_handler(route: Any, proxy: SupportsHandle) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            headers = {key: value for key, value in self.headers.items()}
            outcome = asyncio.run(proxy.handle(route, headers=headers, body=body))
            self._write(outcome.response)

        def _write(self, response: ForwardResponse) -> None:
            self.send_response(response.status)
            for key, value in response.headers.items():
                if key.lower() in _HOP_BY_HOP:
                    continue
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(response.body)))
            self.end_headers()
            self.wfile.write(response.body)

        def log_message(self, *args: object) -> None:  # silence default stderr spam
            return

    return _Handler


async def maybe_await(value: Any) -> Any:
    """Await a value if it is awaitable, else return it (for injected upstreams)."""

    if inspect.isawaitable(value):
        return await value
    return value
