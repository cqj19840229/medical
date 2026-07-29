from contextlib import asynccontextmanager
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
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
logger = logging.getLogger("fragment_api")
logger.setLevel(logging.INFO)
if not logger.handlers:
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    file_handler = RotatingFileHandler(
        LOG_DIR / "api.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)


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
    logger.info(
        "request_start request_id=%s method=%s path=%s",
        request_id,
        request.method,
        request.url.path,
    )
    try:
        response = await call_next(request)
    except Exception:
        logger.exception(
            "request_error request_id=%s elapsed_ms=%.2f",
            request_id,
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
