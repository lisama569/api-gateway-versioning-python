# API 网关接入层（多版本 + 版本级配额 + 运行时策略 + 统一错误）

基于 FastAPI 的网关接入层示例，版本、配额与策略状态全部承载在本地进程内，
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
- 版本协商先于配额判定，协商失败（未知/停用）不消耗任何额度。

## 版本级配额

配额在 `app/quotas.py` 的 `VersionQuotaManager` 中实现，维度为 **(版本, 调用方)**，
每个仍在服务的版本可以配置自己的额度与统计窗口（`QuotaPolicy(limit, window_seconds)`）：

| 版本 | 状态 | 初始额度 |
| --- | --- | --- |
| `v2`（弃用，收紧促迁移） | deprecated | 2 次 / 60s |
| `v3`（主推，放宽） | active | 5 次 / 60s |

- **双向隔离**：同一调用方在 A 版本把额度用满后，换到 B 版本仍按 B 版本的桶正常调用，
  两个版本互不挤占；不同调用方之间同样互不影响。
- **未声明版本**：按默认版本（`v3`）的策略判定，与显式声明 `v3` 的请求计入同一个桶。
- **固定窗口**：窗口内计数，超限立即 429，不排队；窗口在“距离窗口起点走过一个完整窗口”后翻转。
- **空闲回收**：某 (版本, 调用方) 桶空闲超过该版本窗口的 2 倍后自动清理；
  服务长期运行、访问过大量调用方，内存也不会随历史调用方数量无限增长。
- 旧的单维限流器 `app/ratelimit.py`（`RateLimiter`）保留为基础设施组件，
  网关请求链路现统一走版本级配额；`main.py` 中 `limiter` 作为 `quota_manager` 的别名保留。

## 运行时策略调整（无需重启）

- `PATCH /admin/versions/{name}`，请求体字段均可选：
  - `limit`：新窗口额度；`window_seconds`：新窗口长度；
  - `status`：`active` / `deprecated` / `disabled`。
- **参数校验与失败语义**：所有校验在请求解析阶段完成——`limit <= 0`、
  `window_seconds <= 0`、空请求体（一个字段都没填）、未知字段名，一律返回
  422 + `VALIDATION_FAILED`，链路标识（`error.request_id` / `X-Request-ID` 头）
  与其它错误路径一致，内部校验细节不透出。校验失败的请求**不会改动任何状态**：
  即使同一次请求里同时带了合法字段（如 `status`）和非法字段（如 `limit: 0`），
  版本状态、额度、窗口、已统计用量也都保持与调用前完全一致，不存在"改了一半"。
- 从**下一个请求**起按新策略判定，调整前后的请求合并计算也不会出错：
  - 改额度/窗口不清零任何桶：收紧额度时已用量保留（可能立即超限，不多放行），
    放宽时只补足差额（不漏放行）；窗口拉长/缩短都只在"新窗口完整走完"后才翻窗，
    不会因为换策略就把已用量当零；
  - 计数与窗口翻转在桶锁的同一临界区完成，且每个请求先抓取一份不可变策略快照再判定，
    并发请求正打进来时调整，每个请求只按一套策略判定，不重扣、不漏扣；
  - 停用版本时先切状态再换策略：切换瞬间之后的请求在版本协商阶段立即被 410 拒绝，
    **不再占用任何额度**。
- **停用/重新启用的额度口径**：版本一旦停用，立即清空该版本全部调用方桶——
  已累计用量彻底释放、不再占用任何额度，历史调用方统计也不滞留内存；
  重新启用后所有调用方从干净状态重新计数（重新计满一个完整额度后才被限流）。

## 策略与用量可观测

- `GET /admin/versions/{name}`：返回版本状态、是否默认、当前生效的 `limit/window_seconds`、
  以及每个调用方的 `used / remaining / reset_in_seconds`；未知版本返回 400。
- 被限流（429）时，错误体 `error.details` 给出拒绝依据，恢复信息与**当前窗口**一致：

  ```json
  {
    "error": {
      "code": "RATE_LIMITED",
      "message": "调用方 demo 在版本 v2 上超出配额",
      "request_id": "...",
      "details": {
        "caller": "demo",
        "version": "v2",
        "limit": 2,
        "remaining": 0,
        "window_seconds": 60.0,
        "retry_after_seconds": 59.872
      }
    }
  }
  ```

  同时响应头携带标准 `Retry-After`（向上取整秒）。`retry_after_seconds` 是
  “当前窗口重置时刻 − 判定时刻”的相对剩余时间，运行中改过窗口也按新窗口计算，
  不会用下一次窗口开始的绝对时间误导调用方。
- 正常响应体的 `rate_limit` 中带有 `version / limit / remaining / window_seconds`。

## 错误分类

统一错误结构：

```json
{"error": {"code": "...", "message": "...", "request_id": "..."}}
```

| code | HTTP | 来源 |
| --- | --- | --- |
| `VERSION_UNKNOWN` | 400 | 请求了未注册的版本（或管理端操作未知版本） |
| `VERSION_DISABLED` | 410 | 请求了已停用版本 |
| `RATE_LIMITED` | 429 | (版本, 调用方) 维度超出配额，含 `details` 与 `Retry-After` |
| `VALIDATION_FAILED` | 422 | 请求参数校验失败 |
| `BUSINESS_RULE_VIOLATION` | 409 | 业务规则冲突 |
| `DEPENDENCY_FAILURE` | 502 | 底层依赖调用失败 |
| `INTERNAL_ERROR` | 500 | 未预期的内部异常（链路标识照常生成/透传，细节不透出） |

原始异常与堆栈只记录在服务端日志，不出现在响应中。

## 请求链路标识

- 请求头 `X-Request-ID` 可传入关联标识；缺省时网关生成 UUID。
- 标识写入响应头 `X-Request-ID` 与错误体 `error.request_id`，
  正常与异常路径均贯穿日志（`request_id=...`）。

## 与旧行为的兼容范围

- 版本协商（未声明回落、未知 400、停用 410、弃用提示）与异常分类完全不变。
- 默认版本 `v3` 仍为 5 次 / 60s，未声明版本的调用体感与旧版一致。
- **行为变化**：限流维度由“仅按调用方（跨版本共享额度）”改为“(版本, 调用方)”：
  同一调用方在不同版本上的额度不再共享，这是本轮有意调整；
  429 响应在原有 `code/message/request_id` 之外**新增**可选 `details` 与 `Retry-After` 头，
  旧调用方读取既有字段不受影响。
- **行为变化**：版本停用即释放全部已统计用量，重新启用后从干净状态重新计数
  （此前重新启用会沿用停用前的旧用量）。

## 本地验证

```bash
uv sync                 # 安装依赖
uv run pytest -q        # 全部测试（版本协商 / 版本级配额隔离 / 运行时策略并发 / 异常映射）
                        # 日志逐条输出 business_area=<业务域> case=<用例> outcome=<结果>，
                        # 以及请求标识、判定依据与恢复信息
uv run uvicorn main:app --reload   # 启动服务
```

手工验证示例：

```bash
# 版本级额度：v2 只有 2 个，打满后 v3 仍有自己的 5 个
for i in $(seq 3); do curl -s -o /dev/null -w '%{http_code}\n' \
  -H 'X-Caller-ID: demo' -H 'X-API-Version: v2' localhost:8000/api/items; done
for i in $(seq 6); do curl -s -o /dev/null -w '%{http_code}\n' \
  -H 'X-Caller-ID: demo' -H 'X-API-Version: v3' localhost:8000/api/items; done

# 429 的拒绝依据与恢复信息
curl -i -H 'X-Caller-ID: demo' -H 'X-API-Version: v2' localhost:8000/api/items

# 运行时收紧 v3 额度，立即生效
curl -s -X PATCH localhost:8000/admin/versions/v3 \
  -H 'Content-Type: application/json' -d '{"limit":1}'

# 非法参数：422 + VALIDATION_FAILED，状态保持不变
curl -i -X PATCH localhost:8000/admin/versions/v3 \
  -H 'Content-Type: application/json' -d '{"limit":0}'
curl -i -X PATCH localhost:8000/admin/versions/v2 \
  -H 'Content-Type: application/json' -d '{"status":"disabled","limit":0}'

# 运行时停用 v2：之后立即 410，且历史用量彻底释放；
# 重新启用后从干净状态重新计数
curl -s -X PATCH localhost:8000/admin/versions/v2 \
  -H 'Content-Type: application/json' -d '{"status":"disabled"}'
curl -i -H 'X-API-Version: v2' localhost:8000/api/items
curl -s -X PATCH localhost:8000/admin/versions/v2 \
  -H 'Content-Type: application/json' -d '{"status":"deprecated"}'

# 查看某版本当前策略与各调用方用量
curl -s localhost:8000/admin/versions/v2 | python -m json.tool
```
