from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class QueryType(str, Enum):
    pharmacokinetics = "pharmacokinetics"
    pharmacophore = "pharmacophore"


class FragmentRequest(BaseModel):
    fragment: str = Field(
        min_length=1,
        examples=["c1ccccc1"],
        description="标准化 fragment SMILES；服务端仍会再次规范化并校验。",
    )


class FragmentDetailRequest(FragmentRequest):
    type: QueryType = Field(description="Neo4j 信息类型。")
    offset: int = Field(default=0, ge=0, description="从第几个 MySQL 匹配结果开始。")
    limit: int = Field(
        default=100,
        ge=1,
        le=2000,
        description="本次最多关联查询的药物数，避免小 fragment 产生超大响应。",
    )


class IngredientMatch(BaseModel):
    active_ingredient: str | None
    smiles: str


class FragmentSearchResponse(BaseModel):
    fragment: str
    count: int
    matches: list[IngredientMatch]


class DrugDetail(BaseModel):
    active_ingredient: str | None
    smiles: str
    drug_found: bool
    data: dict[str, Any]


class FragmentDetailResponse(BaseModel):
    fragment: str
    type: QueryType
    total_matches: int
    offset: int
    limit: int
    count: int
    response_time_ms: float
    results: list[DrugDetail]
