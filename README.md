# API 网关接入层（多版本 + 限流 + 统一错误）

基于 FastAPI 的网关接入层示例，版本与限流状态全部承载在本地进程内，
保证不同版本、配额与异常场景下的对外行为一致且可预期。

## 版本选择与弃用规则

- 通过请求头 `X-API-Version` 声明版本；未声明时稳定回落到默认版本（当前为 `v3`），
  响应体中 `used_default=true` 可观测。
- 版本状态机（`app/versioning.py`）：
  | 状态 | 对外行为 |
  | --- | --- |
  | `active` | 正常服务 |
  | `deprecated` | 仍正常服务，响应携带 `X-API-Deprecated: true`、`Sunset` 头及响应体 `deprecation` 提示 |
  | `disabled` | 拒绝，HTTP 410 + `VERSION_DISABLED` |
  | 未注册版本 | 拒绝，HTTP 400 + `VERSION_UNKNOWN` |
- 版本协商先于限流判定，协商失败不消耗调用方额度。

## 限流维度与参数语义

- 维度：按调用方（请求头 `X-Caller-ID`，缺省为 `anonymous`）分桶，
  同一调用方跨版本共享同一额度，调用方之间互不影响。
- 算法：固定窗口计数（`app/ratelimit.py`）。
  - `limit`：窗口内最大放行请求数（当前 5）；
  - `window_seconds`：窗口长度（当前 60s）；
  - 超限请求立即返回 HTTP 429 + `RATE_LIMITED`，不排队；
  - 响应体 `rate_limit.remaining` 与判定结果中的 `reset_at` 可观测剩余额度与窗口重置时间。
- 并发正确性：每个桶独立持锁，计数与窗口翻转在同一临界区完成，
  不会重复计数或计数泄漏；空闲超过两个窗口的桶被自动清理。

## 错误分类

统一错误结构：

```json
{"error": {"code": "...", "message": "...", "request_id": "..."}}
```

| code | HTTP | 来源 |
| --- | --- | --- |
| `VERSION_UNKNOWN` | 400 | 请求了未注册的版本 |
| `VERSION_DISABLED` | 410 | 请求了已停用版本 |
| `RATE_LIMITED` | 429 | 调用方超出限流额度 |
| `VALIDATION_FAILED` | 422 | 请求参数校验失败 |
| `BUSINESS_RULE_VIOLATION` | 409 | 业务规则冲突 |
| `DEPENDENCY_FAILURE` | 502 | 底层依赖调用失败 |
| `INTERNAL_ERROR` | 500 | 未预期的内部异常 |

原始异常与堆栈只记录在服务端日志，不出现在响应中。

## 请求链路标识

- 请求头 `X-Request-ID` 可传入关联标识；缺省时网关生成 UUID。
- 标识写入响应头 `X-Request-ID` 与错误体 `error.request_id`，
  正常与异常路径均贯穿日志（`request_id=...`）。

## 本地验证

```bash
uv sync                 # 安装依赖
uv run pytest -q        # 运行全部测试（版本协商 / 限流并发 / 异常映射）
uv run pytest -q -s     # 查看单测日志中的请求标识、判定依据与归一化结果
uv run uvicorn main:app --reload   # 启动服务
```

手工验证示例：

```bash
curl -i localhost:8000/api/items                              # 默认版本 v3
curl -i -H 'X-API-Version: v2' localhost:8000/api/items       # 弃用版本提示
curl -i -H 'X-API-Version: v1' localhost:8000/api/items       # 410 已停用
curl -i -H 'X-API-Version: v9' localhost:8000/api/items       # 400 未知版本
for i in $(seq 7); do curl -s -o /dev/null -w '%{http_code}\n' \
  -H 'X-Caller-ID: demo' localhost:8000/api/items; done       # 5 次后 429
curl -i 'localhost:8000/api/items?fail=business'              # 409 业务异常
curl -i 'localhost:8000/api/items?fail=dependency'            # 502 依赖异常
```
