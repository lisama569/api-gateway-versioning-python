# 多版本接口网关接入层（FastAPI / 本地进程状态）

在现有 FastAPI 服务之上实现的轻量网关层：**版本协商**、**按调用方限流**、**统一错误结构与请求链路标识**。
版本表与限流计数均保存在单进程内存中（线程安全），不依赖外部存储，行为确定性强、可复现。

## 目录结构

```
app/
  config.py       # 限流等配置
  models.py       # 版本状态 / 错误码 / 协商结果等领域类型
  versioning.py   # 版本注册表与协商规则
  ratelimit.py    # 固定窗口限流器（线程安全、惰性过期清理）
  context.py      # 基于 contextvars 的每请求上下文（request_id/caller/version）
  logging_conf.py # 日志过滤器：每条日志自动携带 request_id
  errors.py       # 网关异常体系与统一错误映射（不透出原始堆栈）
  gateway.py      # HTTP 中间件：链路标识 → 版本协商 → 限流 → 弃用提示
  routes.py       # 示例业务端点（正常 / 422 / 502 / 500 路径）
  main.py         # create_app() 应用工厂
tests/            # 版本协商、限流并发、异常映射三类测试
main.py           # 兼容入口（uvicorn main:app）
```

## 运行

```bash
uv sync
uv run fastapi run main.py          # 或 uv run uvicorn main:app
```

## 1. 版本协商

通过请求头 **`X-API-Version`** 声明版本（仅作用于 `/api/*` 路径）。
原始值会先归一化：去除首尾空白、去掉大小写不敏感的 `v` 前缀，例如 `" V1 " → "1"`、`"v3" → "3"`。

| 场景 | 判定 | HTTP 状态 | 错误码 |
|---|---|---|---|
| 未带头 / 空白头 | 选择注册表里**唯一的默认版本**（默认 `2`，语义稳定，不由"最新版本"隐式决定） | 200 | — |
| 显式请求 `v3`（active） | 使用 `3` | 200 | — |
| 显式请求 `v2`（deprecated，仍可用） | 正常处理 + 响应携带弃用提示 | 200 | — |
| 显式请求 `v1`（retired，已停用） | 拒绝 | **410** | `VERSION_RETIRED` |
| 请求未注册的版本（如 `v9`） | 拒绝 | **404** | `VERSION_UNKNOWN` |

内置版本表（`app/versioning.py::build_default_registry`）：

| 版本 | 状态 | 说明 |
|---|---|---|
| `1` | retired | 2026-01-01 sunset，后继 `2` |
| `2` | **deprecated（默认）** | 后继 `3`，仍可调用 |
| `3` | active | 当前推荐版本 |

注册表强制**恰好一个默认版本**（构造期校验），保证默认选择语义稳定。

### 弃用可观测性（deprecated 但可用的版本）

响应会携带标准提示头：

- `Deprecation: true`（RFC 9745 草案）
- `Warning: 299 - "API version 2 is deprecated; please upgrade to version 3"`
- `Sunset: <HTTP-date>`（若注册了 sunset 日期）
- `X-API-Deprecated: true`、`X-API-Successor-Version: 3`
- 响应体与所有日志中的解析版本均为最终协商结果（`X-API-Version` 回显头）。

retired 版本不可用，不携带弃用提示头，直接返回 410。

## 2. 限流

- **维度**：按 `(X-Caller-ID, 协商后版本)` 二元组独立计数，调用方之间、版本之间互不影响；
  缺失 `X-Caller-ID` 时归入固定桶 `anonymous`（匿名调用方共享同一配额）。
- **算法**：进程内**固定窗口**。默认参数（`app/config.py`）：`limit=10` 次 / `window_seconds=60` 秒。
- **拒绝而非排队**：窗口内第 `limit+1` 个请求立即返回 **429 `RATE_LIMITED`**，不等待、不入队；
  被拒请求**不消耗计数**（不会因为拒绝流量而延长占用）。
- **原子性**：单次全局锁内完成"检查 + 递增"，并发下不会重复计数或漏计数（见并发测试）。
- **过期清理**：窗口到达边界（`now - started_at >= window_seconds`）即滚动为新窗口；
  每次请求顺带清理其他键的过期窗口，内存只与当前窗口内活跃调用方数量成正比，不泄漏。

成功与超限响应均带：

```
X-RateLimit-Limit: 10
X-RateLimit-Remaining: 9
Retry-After: 60                 # 仅 429 时出现（秒，向上取整）
```

429 响应体 `details` 中包含 `caller / version / limit / retry_after_seconds`。

## 3. 统一错误结构与链路标识

所有错误响应使用同一结构，**原始异常信息与堆栈绝不透出到响应体**，仅以完整级别写入服务端日志：

```json
{
  "error": {
    "code": "RATE_LIMITED",
    "message": "rate limit exceeded",
    "request_id": "79bf72c3e3…",
    "details": { "caller": "alice", "version": "2", "limit": 10, "retry_after_seconds": 60 }
  }
}
```

| 来源 | HTTP | code |
|---|---|---|
| 未知版本 | 404 | `VERSION_UNKNOWN` |
| 已停用版本 | 410 | `VERSION_RETIRED` |
| 限流 | 429 | `RATE_LIMITED` |
| 业务声明的底层依赖失败（`UpstreamError`） | 502 | `UPSTREAM_ERROR` |
| 参数 / 请求体校验失败（FastAPI/Pydantic） | 422 | `VALIDATION_ERROR`（归一化为 `field/message/type` 列表） |
| 未预期业务异常 | 500 | `INTERNAL_ERROR`（统一文案，不含内部细节） |

**请求链路标识**：

- 优先使用入站 `X-Request-ID`（截断到 128 字符），缺失则生成 32 位 hex；
- 每个响应（含 404/410/422/429/502/500）都回显 `X-Request-ID`，并写入错误体 `error.request_id`；
- 基于 `contextvars` 的 `RequestContext` 贯穿中间件、路由与异常处理器，
  每条日志自动带 `[request_id=…]`；异常路径（含 500）同样保留关联标识，
  完整堆栈只出现在服务端日志中。

## 4. 示例

```bash
# 默认版本（2，弃用中）
curl -i http://127.0.0.1:8000/api/ping

# 显式版本
curl -i http://127.0.0.1:8000/api/ping -H 'X-API-Version: v3'

# 调用方维度
curl -i http://127.0.0.1:8000/api/ping -H 'X-Caller-ID: alice'

# 校验错误
curl -i -X POST http://127.0.0.1:8000/api/echo -H 'Content-Type: application/json' -d '{}'

# 底层依赖失败 / 未预期异常
curl -i http://127.0.0.1:8000/api/upstream-fail -H 'X-API-Version: v3'
curl -i http://127.0.0.1:8000/api/boom -H 'X-API-Version: v3'
```

## 5. 本地验证

```bash
uv run pytest                                   # 全部 28 个用例
uv run pytest tests/test_version_negotiation.py # 版本协商
uv run pytest tests/test_rate_limit.py          # 限流边界/隔离/并发竞态
uv run pytest tests/test_error_mapping.py       # 异常映射

# 实时观察单测日志中的 request_id、归一化结果与判定依据：
uv run pytest -s -o log_cli=true --log-cli-level=INFO
```

测试覆盖要点：

- **版本协商**：未声明（默认）、空白头、显式 active/deprecated、unknown(404)、retired(410)、
  默认版本唯一性校验、弃用提示头断言；
- **限流**：配额边界立即拒绝、被拒不计数、窗口在精确边界滚动、调用方/版本隔离、
  过期窗口清理无泄漏；并发用例 500 线程争抢 100 配额（精确放行 100，可稳定复现）、
  双版本各 200 并发各独立占满；HTTP 层 80 并发争抢 20 配额；
- **异常映射**：502/500/422(body/query/非法 JSON)/404/410/429 分类断言、
  错误体不含原始异常与堆栈、关联标识在响应头、错误体与日志中一致贯穿。

> 说明：限流与版本状态为**单进程内存态**，多 worker / 多实例部署时各自独立计数；
> 如需全局一致配额，需要将 `RateLimiter` 替换为 Redis 等共享存储实现（接口保持不变）。
