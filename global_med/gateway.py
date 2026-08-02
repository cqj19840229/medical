"""Multi-port HTTP reverse proxy with request identity audit logging.

This gateway is intended for services you own or administer.  It does not sniff
traffic: clients must send their HTTP requests to one of the configured ports.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import time
from typing import Any
from urllib.parse import parse_qs
import uuid

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
import uvicorn


BASE_DIR = Path(__file__).resolve().parent
HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length",
}
USER_PATH_RE = re.compile(r"(?:^|/)users/([^/?#]+)(?:/|$)", re.IGNORECASE)
logger = logging.getLogger("global_med.gateway")


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        config = json.load(file)
    if not config.get("listeners"):
        raise ValueError("config.listeners must contain at least one listener")
    return config


def configure_logging(config: dict[str, Any]) -> None:
    log_path = BASE_DIR / config.get("audit_log", "logs/access.jsonl")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = RotatingFileHandler(
        log_path,
        maxBytes=int(config.get("log_max_bytes", 10 * 1024 * 1024)),
        backupCount=int(config.get("log_backup_count", 10)),
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)


def _scalar(value: Any) -> str | None:
    if isinstance(value, (str, int)) and str(value).strip():
        return str(value).strip()[:256]
    return None


def extract_user_id(
    headers: dict[str, str], path: str, query: dict[str, str], body: bytes
) -> tuple[str | None, str | None]:
    """Extract a claimed user id and describe where it came from.

    This is audit metadata only. Authorization must rely on a verified session or
    token at the application/authentication layer.
    """
    for name in ("x-user-id", "x-authenticated-user-id"):
        value = _scalar(headers.get(name))
        if value:
            return value, f"header:{name}"
    for name in ("user_id", "userId"):
        value = _scalar(query.get(name))
        if value:
            return value, f"query:{name}"
    match = USER_PATH_RE.search(path)
    if match:
        return match.group(1)[:256], "path"
    content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    try:
        if content_type == "application/json" and body:
            payload = json.loads(body)
            if isinstance(payload, dict):
                for name in ("user_id", "userId"):
                    value = _scalar(payload.get(name))
                    if value:
                        return value, f"json:{name}"
        elif content_type == "application/x-www-form-urlencoded" and body:
            form = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=False)
            for name in ("user_id", "userId"):
                value = _scalar(form.get(name, [None])[0])
                if value:
                    return value, f"form:{name}"
    except (ValueError, TypeError, json.JSONDecodeError):
        pass
    return None, None


def create_app(listener: dict[str, Any], config: dict[str, Any]) -> FastAPI:
    upstream = str(listener["upstream"]).rstrip("/")
    timeout = float(config.get("upstream_timeout_seconds", 60))
    max_inspect = int(config.get("max_inspect_body_bytes", 1024 * 1024))
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)

    @app.on_event("shutdown")
    async def close_client() -> None:
        await client.aclose()

    @app.api_route("/{proxy_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    async def proxy(request: Request, proxy_path: str) -> Response:
        started = time.perf_counter()
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        body = await request.body()
        headers = {key.lower(): value for key, value in request.headers.items()}
        query = dict(request.query_params)
        user_id, user_id_source = extract_user_id(
            headers, request.url.path, query, body[:max_inspect]
        )
        outgoing_headers = {
            key: value for key, value in request.headers.items()
            if key.lower() not in HOP_BY_HOP_HEADERS
        }
        outgoing_headers["x-request-id"] = request_id
        outgoing_headers["x-forwarded-for"] = request.client.host if request.client else "unknown"
        outgoing_headers["x-forwarded-host"] = request.headers.get("host", "")
        target = f"{upstream}/{proxy_path}"
        if request.url.query:
            target += f"?{request.url.query}"
        status_code = 502
        error: str | None = None
        try:
            upstream_response = await client.request(
                request.method, target, headers=outgoing_headers, content=body
            )
            status_code = upstream_response.status_code
            response_headers = {
                key: value for key, value in upstream_response.headers.items()
                if key.lower() not in HOP_BY_HOP_HEADERS
            }
            response: Response = Response(
                content=upstream_response.content,
                status_code=status_code,
                headers=response_headers,
            )
        except httpx.HTTPError as exc:
            error = type(exc).__name__
            response = JSONResponse(
                status_code=502,
                content={"detail": "upstream unavailable", "request_id": request_id},
            )
        audit = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "request_id": request_id,
            "client_ip": request.client.host if request.client else None,
            "listen_port": int(listener["port"]),
            "method": request.method,
            "path": request.url.path,
            "user_id": user_id,
            "user_id_source": user_id_source,
            "status_code": status_code,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "error": error,
        }
        logger.info(json.dumps(audit, ensure_ascii=False, separators=(",", ":")))
        return response

    return app


async def serve(config: dict[str, Any]) -> None:
    servers = []
    for listener in config["listeners"]:
        uvicorn_config = uvicorn.Config(
            create_app(listener, config),
            host=str(listener.get("host", "0.0.0.0")),
            port=int(listener["port"]),
            log_level=str(config.get("log_level", "info")),
            proxy_headers=False,
        )
        servers.append(uvicorn.Server(uvicorn_config).serve())
    await asyncio.gather(*servers)


def main() -> None:
    parser = argparse.ArgumentParser(description="Multi-port HTTP audit reverse proxy")
    parser.add_argument("--config", type=Path, default=BASE_DIR / "config.json")
    args = parser.parse_args()
    config = load_config(args.config.resolve())
    configure_logging(config)
    asyncio.run(serve(config))


if __name__ == "__main__":
    main()
