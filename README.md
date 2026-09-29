# API 网关接入层（多版本 + 版本级配额 + 运行时策略 + 统一错误）

基于 FastAPI 的网关接入层示例，版本、配额与限流状态全部承载在本地进程内，
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
- 版本协商先于配额判定，协商失败（未知/停用）不消耗调用方额度。

## 版本级配额

- 维度：按 **(调用方, 版本)** 双维度分桶（`app/ratelimit.py` 的
  `VersionedRateLimiter`）。同一调用方在每个服务版本上各有独立额度：
  - 某版本额度用满后，换到另一个版本仍可正常调用，**两个方向都不互相挤占**；
  - 未声明版本时按默认版本（当前 `v3`）的那套配额策略计算；
  - 不同调用方之间同样互不影响。
- 每个版本可单独配置额度大小与统计窗口（`QuotaPolicy(limit, window_seconds)`），
  典型用法是主推版本（`v3`，当前 10/60s）给更宽的量，
  已弃用版本（`v2`，当前 3/60s）收紧以推动迁移。
- 算法：固定窗口计数。
  - 超限请求立即返回 HTTP 429 + `RATE_LIMITED`，不排队；
  - 正常响应体的 `quota` 字段给出 `limit / used / remaining / window_seconds`；
  - 429 响应体 `error.details` 给出**拒绝依据**（caller、version、已用/额度/剩余）
    以及与**当前窗口一致**的 `reset_after_seconds`，并回写 `Retry-After` 头。
    恢复时间指向当前窗口结束的时刻，不会用"下一次窗口重新开始"的时间误导调用方。
- 协商/判定一致性：`VersionRegistry.negotiate` 返回的快照同时携带该版本状态与配额
  策略，后续配额判定与本次协商看到的是同一份策略，并发调整不会让一次请求看到
  前后不一致的状态。

## 运行时策略调整（无需重启）

- `PATCH /admin/versions/{version}/policy`，请求体字段全部可选、至少传一个：

  ```json
  {"limit": 20, "window_seconds": 30, "status": "deprecated", "sunset": "2027-01-01"}
  ```

  - 只改额度/窗口：**已用计数不归零**，下一个请求起按新策略判定。
    放宽额度不会把已用量当零而多放行；收紧额度不会重复扣或漏扣；
  - 改窗口长度：窗口边界按当前窗口长度对齐推进，窗口内已用量保留；
  - 改 `status`：状态切换从下一个请求生效；一旦调成 `disabled`，
    之后的请求**立即 410**，同时该版本的全部调用方桶被丢弃，
    停用版本不再占用任何额度（重新启用后从 0 开始计）；
  - 未知版本返回 400 `VERSION_UNKNOWN`；非法额度/窗口/状态返回 422。
- `GET /admin/versions/{version}`：查看某版本当前生效的状态、配额策略与
  各调用方当前窗口的用量（`used / remaining / reset_after_seconds`），
  停用版本也可查询。
- 并发正确性：注册表用锁保护策略读写，更新对在途请求以快照方式可见；
  每个配额桶独立持锁，计数与窗口翻转在同一临界区完成，
  运行中改策略与并发请求同时发生时判定结果稳定，不重复计数也不超发。
- 内存回收：空闲超过**自身两个窗口**的调用方桶会被自动清理，
  长期运行、访问调用方很多时内存不会随历史调用方数量无限增长；
  版本停用会立即回收该版本的所有桶。

## 与旧行为的兼容范围

- 版本协商结果完全不变：未声明回落默认版本、未知 400 `VERSION_UNKNOWN`、
  停用 410 `VERSION_DISABLED`、弃用版本正常服务并带弃用提示头；
- 统一错误结构 `{"error": {"code", "message", "request_id"}}` 不变；
  429 在其上**新增**可选的 `error.details` 与 `Retry-After` 响应头；
- 正常响应体里原 `rate_limit` 字段改名为含义更完整的 `quota`
  （新增 `used`、`window_seconds`）；限流维度从"仅按调用方"变为
  **"(调用方, 版本)"**，跨版本不再共享额度（这是本轮刻意的行为变更）；
- 限流器类由 `RateLimiter` 调整为 `VersionedRateLimiter`，
  `check` 需要版本协商快照而非全局单一额度。

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
uv run pytest -q        # 运行全部测试（版本协商 / 版本级配额 / 并发改策略 /
                        # 恢复信息 / 管理端策略 / 异常映射）
uv run pytest -q -s     # 查看单测日志中的请求标识、判定依据与归一化结果
uv run uvicorn main:app --reload   # 启动服务
```

手工验证示例：

```bash
curl -i localhost:8000/api/items                              # 默认版本 v3
curl -i -H 'X-API-Version: v2' localhost:8000/api/items       # 弃用版本提示
curl -i -H 'X-API-Version: v1' localhost:8000/api/items       # 410 已停用
curl -i -H 'X-API-Version: v9' localhost:8000/api/items       # 400 未知版本

# v2 额度只有 3，用满后看 429 依据与 Retry-After
for i in $(seq 4); do curl -s -o /dev/null -w '%{http_code}\n' \
  -H 'X-Caller-ID: demo' -H 'X-API-Version: v2' localhost:8000/api/items; done
curl -i -H 'X-Caller-ID: demo' -H 'X-API-Version: v2' localhost:8000/api/items

# 查询某版本当前策略与用量
curl -s localhost:8000/admin/versions/v2 | python -m json.tool

# 运行时收紧 v3 额度、拉长窗口，无需重启
curl -s -X PATCH localhost:8000/admin/versions/v3/policy \
  -H 'Content-Type: application/json' -d '{"limit":2,"window_seconds":30}'

# 运行时停用某版本：之后请求立即 410 且不占额度
curl -s -X PATCH localhost:8000/admin/versions/v2/policy \
  -H 'Content-Type: application/json' -d '{"status":"disabled"}'

curl -i 'localhost:8000/api/items?fail=business'              # 409 业务异常
curl -i 'localhost:8000/api/items?fail=dependency'            # 502 依赖异常
```
