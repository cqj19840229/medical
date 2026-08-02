# global_med 多端口统一请求审计代理

该程序让多个 HTTP 端口统一经过同一套审计逻辑，再分别转发到配置的后端服务。它会记录请求时间、来源 IP、入口端口、路径、状态码和 `user_id`，但不会记录密码、Authorization、Cookie 或完整请求体。

## 配置与启动

复制 `config.example.json` 为 `config.json`，按实际情况修改每个入口端口和后端地址。入口端口不能和后端端口相同，否则会形成转发循环。

```powershell
Copy-Item .\config.example.json .\config.json
& 'C:\Users\56884\anaconda3\envs\medical\python.exe' -m pip install -r .\requirements.txt
& 'C:\Users\56884\anaconda3\envs\medical\python.exe' .\gateway.py --config .\config.json
```

客户端需要改为访问代理入口。例如原后端为 `http://服务器IP:8000`，配置入口为 8080 后，客户端访问 `http://服务器IP:8080`。日志默认写入 `logs/access.jsonl`。

`user_id` 按以下优先级获取：`X-User-ID`/`X-Authenticated-User-ID` 请求头、查询参数、`/users/{user_id}/...` 路径、JSON 请求体、表单请求体。

## 安全边界

- 日志中的 `user_id` 只是请求声称的身份，不能单独用于鉴权。最可靠的做法是让认证服务验证登录态/JWT 后写入受信任的 `X-Authenticated-User-ID`，并阻止公网客户端直接访问后端端口。
- 该代理只处理明确发给它的 HTTP 请求，不是网络抓包工具。对于 HTTPS，应在 Nginx/Caddy 等受控入口终止 TLS 后转发给本代理，或为本程序配置受信任证书终止方案；不能读取未解密的 HTTPS 流量。
- 如果入口前还有可信负载均衡器，需要另行配置可信代理列表后才能接受其 `X-Forwarded-For`，当前实现刻意不信任客户端自带的该请求头。
