# FDA Ingredient Fragment API

服务提供两个接口：

- `POST /api/v1/fragments/search`：通过 RDKit 子结构匹配查询包含 fragment 的成分。
- `POST /api/v1/fragments/details`：查询成分并关联 Neo4j 信息。

## 启动

在本目录执行：

```powershell
& 'C:\Users\56884\anaconda3\envs\medical\python.exe' -m uvicorn main:app --host 0.0.0.0 --port 8000 --app-dir C:\medical\github\medical\query_medical
/opt/miniconda3/envs/medical -m uvicorn main:app --host 0.0.0.0 --port 8888
```

Swagger UI：<http://localhost:8000/docs>

OpenAPI JSON：<http://localhost:8000/openapi.json>

## 请求示例

```json
{
  "fragment": "c1ccccc1"
}
```

详情接口：

```json
{
  "fragment": "c1ccccc1",
  "type": "pk",
  "offset": 0,
  "limit": 100
}
```

详情接口也支持多个互不重叠的必选片段：

```json
{
  "fragment": ["c1ccccc1", "Cc1ccccc1"],
  "type": "effect",
  "offset": 0,
  "limit": 100
}
```

多个片段采用 AND 匹配，而且每个片段必须使用不同的分子原子。例如苯环与
甲基取代苯环同时查询时，同一个芳环不能重复满足两个条件，因此结果必须至少
存在两个相应的苯环。

`type` 可为：

- `pk`：返回 Drug 的药代字段和 `ENZYME_RELATION` 关联的
  `MetabolicEnzyme`。
- `effect`：先根据 fragment 找到 Drug，再通过 `Drug -[:TREATS]- Indication`
  查询适应症，通过适应症查询 `HAS_CLINICAL_FEATURE` 临床表征，并直接查询
  Drug 的 `TARGETS`。Drug 使用 `drug_name CONTAINS active_ingredient`（忽略大小写）
  匹配，名称结果均去重。不返回
  `InteractionObject`；`data` 中仅返回名称数组 `indication_name`、
  `feature_name`、`target_name`。
- `safety`：返回 `HAS_ADVERSE_REACTION` 关联的不良反应节点。

连接信息有默认值，也可通过 `.env.example` 所列环境变量覆盖。环境变量不会由
程序自动读取 `.env` 文件，请在启动进程前设置。

## 日志与耗时

- 日志同时输出到控制台和 `logs/api.log`，服务重启后继续追加，不会覆盖。
- 活动日志达到 10 MB 后自动压缩归档至 `logs/archive/api_时间.log.gz`；
  所有归档永久保留，程序不会自动删除历史日志。
- 统一 HTTP 中间件记录所有请求的请求 ID、客户端 IP、方法、路径、查询参数、
  路径参数、JSON 请求体、状态码和总耗时；敏感字段自动脱敏，请求参数最长记录
  4096 字符。
- 日志还包含 MySQL 扫描/匹配数、各阶段耗时、Cypher 和 Neo4j 耗时。
- HTTP 响应头包含 `X-Request-ID` 和 `X-Process-Time-Ms`。
- 详情响应体包含 `response_time_ms`。
- Neo4j 单次查询超时为 30 秒；详情接口默认最多返回 100 条，可使用
  `offset`、`limit` 分页，`limit` 最大为 2000。
