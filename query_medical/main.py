from contextlib import asynccontextmanager
from datetime import datetime
import gzip
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import shutil
from time import perf_counter
import uuid

import mysql.connector
from fastapi import FastAPI, HTTPException, Request
from neo4j.exceptions import Neo4jError

from models import (
    FragmentDetailRequest,
    FragmentDetailResponse,
    FragmentRequest,
    FragmentSearchResponse,
)
from services import (
    InvalidSmilesError,
    enrich_matches,
    search_ingredient_smiles,
    standardize_smiles,
)

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_ARCHIVE_DIR = LOG_DIR / "archive"
LOG_ARCHIVE_DIR.mkdir(exist_ok=True)
logger = logging.getLogger("fragment_api")
logger.setLevel(logging.INFO)


class ArchivingRotatingFileHandler(RotatingFileHandler):
    """按大小轮转并压缩归档，永久保留历史日志。"""

    def __init__(self, filename, archive_dir: Path, **kwargs):
        self.archive_dir = archive_dir
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        # backupCount 由自定义 doRollover 接管，不执行历史日志删除。
        super().__init__(filename, backupCount=0, **kwargs)

    def doRollover(self):
        if self.stream:
            self.stream.close()
            self.stream = None

        source = Path(self.baseFilename)
        if source.exists() and source.stat().st_size > 0:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            archive = self.archive_dir / f"{source.stem}_{timestamp}.log.gz"
            try:
                with source.open("rb") as source_file, gzip.open(
                    archive, "wb"
                ) as archive_file:
                    shutil.copyfileobj(source_file, archive_file)
                # 仅在归档成功后清空活动日志；历史内容已保存在 gzip 中。
                source.unlink()
            except Exception:
                if archive.exists():
                    archive.unlink()
                self.stream = self._open()
                raise

        if not self.delay:
            self.stream = self._open()


if not logger.handlers:
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    file_handler = ArchivingRotatingFileHandler(
        LOG_DIR / "api.log",
        archive_dir=LOG_ARCHIVE_DIR,
        maxBytes=10 * 1024 * 1024,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)

SENSITIVE_KEYS = {
    "password",
    "passwd",
    "token",
    "access_token",
    "refresh_token",
    "authorization",
    "secret",
    "api_key",
}
MAX_REQUEST_PARAMS_LENGTH = 4096


def _mask_sensitive(value):
    """递归脱敏请求参数中的密码、令牌等字段。"""
    if isinstance(value, dict):
        return {
            key: "***" if key.lower() in SENSITIVE_KEYS else _mask_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_mask_sensitive(item) for item in value]
    return value


async def _request_params(request: Request) -> str:
    params = {
        "query": dict(request.query_params),
        "path": dict(request.path_params),
    }
    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        body = await request.body()
        if body:
            try:
                params["body"] = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                params["body"] = "<invalid-json>"
    serialized = json.dumps(
        _mask_sensitive(params), ensure_ascii=False, separators=(",", ":")
    )
    if len(serialized) > MAX_REQUEST_PARAMS_LENGTH:
        return serialized[:MAX_REQUEST_PARAMS_LENGTH] + "...<truncated>"
    return serialized


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield


app = FastAPI(
    title="FDA Ingredient Fragment API",
    version="1.0.0",
    description=(
        "通过 RDKit 子结构匹配检索包含指定标准化 fragment 的 FDA 成分，"
        "并可关联 Neo4j 中的药代动力学或药效团信息。"
    ),
    lifespan=lifespan,
)

@app.middleware("http")
async def request_timing(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    started = perf_counter()
    params = await _request_params(request)
    client_ip = request.client.host if request.client else "-"
    logger.info(
        "request_start request_id=%s client_ip=%s method=%s path=%s params=%s",
        request_id,
        client_ip,
        request.method,
        request.url.path,
        params,
    )
    try:
        response = await call_next(request)
    except Exception:
        logger.exception(
            "request_error request_id=%s method=%s path=%s params=%s elapsed_ms=%.2f",
            request_id,
            request.method,
            request.url.path,
            params,
            (perf_counter() - started) * 1000,
        )
        raise
    elapsed_ms = (perf_counter() - started) * 1000
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Process-Time-Ms"] = f"{elapsed_ms:.2f}"
    logger.info(
        "request_end request_id=%s status=%d elapsed_ms=%.2f",
        request_id,
        response.status_code,
        elapsed_ms,
    )
    return response


def _find(fragment: str):
    started = perf_counter()
    try:
        standardized, mol = standardize_smiles(fragment)
        logger.info(
            "fragment_standardized input=%s standardized=%s elapsed_ms=%.2f",
            fragment,
            standardized,
            (perf_counter() - started) * 1000,
        )
        matches = search_ingredient_smiles(mol)
        return standardized, matches
    except InvalidSmilesError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except mysql.connector.Error as exc:
        raise HTTPException(status_code=503, detail=f"MySQL 查询失败: {exc}") from exc


@app.post(
    "/api/v1/fragments/search",
    response_model=FragmentSearchResponse,
    tags=["Fragment"],
    summary="按 fragment 查询活性成分",
)
def search_fragment(request: FragmentRequest):
    standardized, matches = _find(request.fragment)
    return {
        "fragment": standardized,
        "count": len(matches),
        "matches": matches,
    }


@app.post(
    "/api/v1/fragments/details",
    response_model=FragmentDetailResponse,
    tags=["Fragment"],
    summary="按 fragment 查询活性成分及 Neo4j 药物信息",
)
def fragment_details(request: FragmentDetailRequest):
    started = perf_counter()
    standardized, matches = _find(request.fragment)
    page = matches[request.offset : request.offset + request.limit]
    logger.info(
        "details_page total_matches=%d offset=%d limit=%d page_count=%d type=%s",
        len(matches),
        request.offset,
        request.limit,
        len(page),
        request.type.value,
    )
    try:
        results = enrich_matches(page, request.type.value, standardized)
    except Neo4jError as exc:
        raise HTTPException(status_code=503, detail=f"Neo4j 查询失败: {exc}") from exc
    return {
        "fragment": standardized,
        "type": request.type,
        "total_matches": len(matches),
        "offset": request.offset,
        "limit": request.limit,
        "count": len(results),
        "response_time_ms": round((perf_counter() - started) * 1000, 2),
        "results": results,
    }
