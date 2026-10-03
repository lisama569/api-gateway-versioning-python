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

- `PATCH /admin/versions/{name}`，请求体字段均可选，**但至少要提供一个**：
  - `limit`：新窗口额度，必须为正整数（`> 0`）；
  - `window_seconds`：新窗口长度，必须为正数（`> 0`）；
  - `status`：`active` / `deprecated` / `disabled`。
- **参数校验与失败语义**：
  - `limit` / `window_seconds` 传 `0`、负数、非数字，`status` 传枚举外的值，
    请求体为空对象、字段名写错（未识别字段）、`null` 或根本不是合法 JSON，
    一律返回 **HTTP 422 + `VALIDATION_FAILED`**（属于调用方参数错误，
    不会走到 500，也不回传任何内部异常文本/堆栈）；
  - **失败原子性**：所有参数校验与读取都发生在任何状态变更之前。
    一次请求里同时要求停用又把额度写成非法值时，请求失败，但该版本的
    状态、额度、窗口与已累计用量与未调用时完全一致，不会“报错却改了一半”。
- 合法请求从**下一个请求**起按新策略判定，调整前后的请求合并计算也不会出错：
  - 改额度/窗口不清零任何桶：收紧额度时已用量保留（可能立即超限，不多放行），
    放宽时只补足差额（不漏放行）；窗口拉长/缩短都只在“新窗口完整走完”后才翻窗，
    不会因为换策略就把已用量当零；
  - 计数与窗口翻转在桶锁的同一临界区完成，且每个请求先抓取一份不可变策略快照再判定，
    并发请求正打进来时调整，每个请求只按一套策略判定，不重扣、不漏扣。
- **停用与重新启用的额度口径**：
  - 版本一旦停用，从下一个请求起在版本协商阶段立即被 410 拒绝，
    **彻底不再占用任何额度**：该版本全部调用方的累计用量与内存统计桶立即清空；
  - 停用期间可以继续调整额度/窗口（只换策略、不产生任何桶）；
  - 重新启用后**从干净状态开始计数**，停用前的旧用量不会带回来，
    调用方不会因为历史用量一上来就被限流。
  - 切换顺序保证：停用先切状态再换策略再清桶；启用先让策略就位、最后放开状态，
    切换瞬间不会出现“带着旧状态/旧桶判定”的请求。

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
| `INTERNAL_ERROR` | 500 | 未预期的内部异常 |

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

## 本地验证

```bash
uv sync                 # 安装依赖
uv run pytest -q        # 全部测试；默认开启 INFO 实时日志，
                        # 每个用例开始/结束都会打印业务域与通过情况，
                        # 覆盖：版本协商 / 调用方限流 / 版本级配额（隔离·
                        # 运行时策略·停用清理·并发不重不漏）/ 管理端调整
                        # （非法值·空体·错字段·失败原子性·停用再启用）/
                        # 异常映射（含 500 路径链路标识）
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

# 非法参数：422（不是 500），错误体与响应头都带本次请求标识
curl -i -X PATCH localhost:8000/admin/versions/v3 \
  -H 'Content-Type: application/json' -H 'X-Request-ID: my-trace-1' \
  -d '{"limit":0}'
# 空请求体 / 写错字段名同样 422，不会静默成功
curl -s -o /dev/null -w '%{http_code}\n' -X PATCH localhost:8000/admin/versions/v3 \
  -H 'Content-Type: application/json' -d '{}'
curl -s -o /dev/null -w '%{http_code}\n' -X PATCH localhost:8000/admin/versions/v3 \
  -H 'Content-Type: application/json' -d '{"limt":3}'
# 同时停用 + 非法值：422，且 v3 仍是 active（失败不改任何状态）
curl -s -X PATCH localhost:8000/admin/versions/v3 \
  -H 'Content-Type: application/json' -d '{"status":"disabled","limit":-5}'
curl -s localhost:8000/admin/versions/v3 | python -m json.tool

# 运行时停用 v2：之后立即 410、用量被清空；再启用后从干净的 2 个额度开始
curl -s -X PATCH localhost:8000/admin/versions/v2 \
  -H 'Content-Type: application/json' -d '{"status":"disabled"}'
curl -s localhost:8000/admin/versions/v2 | python -m json.tool   # callers 为空
curl -i -H 'X-API-Version: v2' localhost:8000/api/items          # 410
curl -s -X PATCH localhost:8000/admin/versions/v2 \
  -H 'Content-Type: application/json' -d '{"status":"deprecated"}'

# 查看某版本当前策略与各调用方用量
curl -s localhost:8000/admin/versions/v2 | python -m json.tool
```
