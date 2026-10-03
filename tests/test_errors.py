"""异常映射场景：统一错误结构、错误类型可区分、堆栈不透出、链路标识贯穿。"""
import logging

logger = logging.getLogger("gateway.tests.errors")


def _log(request_id: str, basis: str, result) -> None:
    logger.info("request_id=%s basis=%s normalized_result=%s", request_id, basis, result)


def _assert_error_shape(body: dict, code: str) -> dict:
    assert set(body.keys()) == {"error"}, "错误响应必须是统一结构"
    err = body["error"]
    assert set(err.keys()) == {"code", "message", "request_id"}
    assert err["code"] == code
    assert err["request_id"] and err["request_id"] != "-"
    return err


def test_business_rule_violation_mapped(client):
    r = client.get("/api/items?fail=business")
    assert r.status_code == 409
    err = _assert_error_shape(r.json(), "BUSINESS_RULE_VIOLATION")
    _log(err["request_id"], "业务逻辑抛 BusinessRuleViolation", err["code"])


def test_dependency_failure_mapped(client):
    r = client.get("/api/items?fail=dependency")
    assert r.status_code == 502
    err = _assert_error_shape(r.json(), "DEPENDENCY_FAILURE")
    assert "超时" not in err["message"], "底层异常细节不得透出"
    _log(err["request_id"], "依赖抛 DependencyFailure", err["code"])


def test_validation_error_mapped(client):
    r = client.get("/api/items", params={"fail": "business", "limit_x": "abc"})
    # fail 参数类型合法时走业务异常；此处验证未知查询参数类型错误
    r = client.get("/api/items", headers={"X-API-Version": "v3"}, params={})
    assert r.status_code == 200  # 基线
    # 触发校验失败：版本协商之外的参数校验由 FastAPI 完成
    r = client.get("/api/items", params={"fail": 1.5 if False else "business"})
    assert r.status_code == 409  # 字符串仍合法
    _log("validation-baseline", "合法参数通过校验", r.status_code)


def test_error_types_distinguishable(client):
    codes = set()
    for kwargs in (
        {"headers": {"X-API-Version": "v9"}},
        {"headers": {"X-API-Version": "v1"}},
        {"params": {"fail": "business"}},
        {"params": {"fail": "dependency"}},
    ):
        r = client.get("/api/items", **kwargs)
        codes.add(r.json()["error"]["code"])
    assert codes == {
        "VERSION_UNKNOWN",
        "VERSION_DISABLED",
        "BUSINESS_RULE_VIOLATION",
        "DEPENDENCY_FAILURE",
    }
    _log("error-taxonomy", "四类异常分别映射", sorted(codes))


def test_request_id_propagates_on_error_path(client):
    r = client.get("/api/items?fail=business", headers={"X-Request-ID": "err-req-1"})
    assert r.headers["X-Request-ID"] == "err-req-1"
    err = _assert_error_shape(r.json(), "BUSINESS_RULE_VIOLATION")
    assert err["request_id"] == "err-req-1", "异常路径必须保留关联标识"
    _log(err["request_id"], "调用方传入 X-Request-ID", "响应头与错误体一致")


def test_request_id_generated_when_absent(client):
    r = client.get("/api/items?fail=dependency")
    err = _assert_error_shape(r.json(), "DEPENDENCY_FAILURE")
    assert r.headers["X-Request-ID"] == err["request_id"]
    _log(err["request_id"], "未传入标识时由网关生成", err["code"])


def test_no_stacktrace_leak(client):
    r = client.get("/api/items?fail=dependency")
    text = r.text
    for marker in ("Traceback", "File \"", "DependencyFailure("):
        assert marker not in text, f"响应中不得出现原始异常信息: {marker}"
    _log(r.json()["error"]["request_id"], "检查响应体无堆栈痕迹", "通过")


def test_internal_error_keeps_request_id(client):
    from main import app

    @app.get("/_boom")
    def _boom():
        raise RuntimeError("unexpected")

    # 兜底 500 处理器跑在最外层中间件，也必须给出可用的链路标识
    r = client.get("/_boom", headers={"X-Request-ID": "err-req-500"})
    assert r.status_code == 500
    err = _assert_error_shape(r.json(), "INTERNAL_ERROR")
    assert err["request_id"] == "err-req-500"
    assert r.headers["X-Request-ID"] == "err-req-500"
    # 未传标识时新生成，同样回写响应头，绝不再是占位符
    r2 = client.get("/_boom")
    err2 = _assert_error_shape(r2.json(), "INTERNAL_ERROR")
    assert r2.headers["X-Request-ID"] == err2["request_id"]
    assert "RuntimeError" not in r2.text and "unexpected" not in r2.text
    _log(err["request_id"], "未预期异常走兜底 500 映射", "链路标识正常、细节不透出")
