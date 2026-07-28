# FDA Ingredient Fragment API

服务提供两个接口：

- `POST /api/v1/fragments/search`：通过 RDKit 子结构匹配查询包含 fragment 的成分。
- `POST /api/v1/fragments/details`：查询成分并关联 Neo4j 信息。

## 启动

在本目录执行：

```powershell
& 'C:\Users\56884\anaconda3\envs\medical\python.exe' -m uvicorn main:app --host 0.0.0.0 --port 8000
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
  "type": "pharmacokinetics",
  "offset": 0,
  "limit": 100
}
```

`type` 可为：

- `pharmacokinetics`：返回 Drug 的药代字段和 `ENZYME_RELATION` 关联的
  `MetabolicEnzyme`。
- `pharmacophore`：返回 `DRUG_INTERACTION`、`HAS_CLINICAL_FEATURE`、
  `TARGETS` 关联节点。

连接信息有默认值，也可通过 `.env.example` 所列环境变量覆盖。环境变量不会由
程序自动读取 `.env` 文件，请在启动进程前设置。

## 日志与耗时

- 日志同时输出到控制台和 `logs/api.log`，单文件 10 MB，保留 5 个历史文件。
- 日志包含请求 ID、MySQL 扫描/匹配数、各阶段耗时、Cypher 和 Neo4j 耗时。
- HTTP 响应头包含 `X-Request-ID` 和 `X-Process-Time-Ms`。
- 详情响应体包含 `response_time_ms`。
- Neo4j 单次查询超时为 30 秒；详情接口默认最多返回 100 条，可使用
  `offset`、`limit` 分页，`limit` 最大为 2000。
