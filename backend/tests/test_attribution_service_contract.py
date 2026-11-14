"""SVC-ATTR-09 回归验收：发布冻结契约、响应回放、并发发布、回滚与鉴权。

覆盖链路：发布入口（/api/delivery/publish）→ 契约冻结 → 响应读取（/api/delivery/{no}/serve）
→ 发布记录追溯（/api/delivery/{no}/releases）。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.delivery import DeliveryService, _contract_signature

SERVICE = "SVC-ATTR-09"
RULE = "ATTR-0009"
CONSUMER = "growth-app"


def headers(role: str = "release_manager", biz_line: str = "增长业务线") -> dict[str, str]:
    """中文头值按 UTF-8 百分号编码，与浏览器端传输方式一致。"""
    return {"X-Operator": quote("联调发布人"), "X-Role": role, "X-Biz-Line": quote(biz_line)}


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def rule_id(client: TestClient) -> int:
    items = client.get("/api/attribution").json()["items"]
    return next(row["id"] for row in items if row["规则编号"] == RULE)


def upgrade_and_publish_rule(client: TestClient, weights: dict[str, float]) -> None:
    """把归因规则升到新版本并发布；旧版本 v1 必须保持冻结。"""
    entry_id = rule_id(client)
    response = client.post(
        f"/api/attribution/{entry_id}/upgrade",
        json={"values": {"触点权重": weights, "末次触点权重": weights["末次触点"], "首次触点权重": weights["首次触点"]}},
        headers=headers("reviewer"),
    )
    assert response.status_code == 200, response.text
    new_id = response.json()["entry"]["id"]
    response = client.post(
        f"/api/attribution/{new_id}/actions",
        json={"values": {"action": "发布规则"}},
        headers=headers(),
    )
    assert response.json()["ok"] is True, response.text


def publish(client: TestClient, **values) -> dict:
    payload = {"服务编号": SERVICE, **values}
    response = client.post("/api/delivery/publish", json={"values": payload}, headers=headers())
    return response


def serve(client: TestClient, version_no: int | None = None) -> dict:
    params = {"consumer_app": CONSUMER}
    if version_no is not None:
        params["version_no"] = version_no
    return client.get(f"/api/delivery/{SERVICE}/serve", params=params).json()


# ---------------------------------------------------------------- 旧响应保留
def test_published_contract_does_not_follow_rule_upgrade(client: TestClient) -> None:
    before = serve(client)
    assert before["规则版本"] == 1
    assert before["归因结果"]["触点权重"] == {"首次触点": 0.4, "末次触点": 0.4, "中间触点": 0.2}

    # 规则升版后，已发布服务的响应必须还是当时冻结的触点权重。
    upgrade_and_publish_rule(client, {"首次触点": 0.1, "末次触点": 0.1, "中间触点": 0.8})
    after = serve(client)
    assert after["规则版本"] == 1
    assert after["归因结果"]["触点权重"] == before["归因结果"]["触点权重"]
    assert after["服务版本号"] == before["服务版本号"]


def test_rule_upgrade_generates_new_service_version_and_keeps_history(client: TestClient) -> None:
    upgrade_and_publish_rule(client, {"首次触点": 0.1, "末次触点": 0.1, "中间触点": 0.8})

    response = publish(client, 规则编号=RULE)
    assert response.status_code == 200, response.text
    entry = response.json()["entry"]
    assert entry["版本号"] == 2
    assert entry["契约"]["规则版本"] == 2

    latest = serve(client)
    assert latest["服务版本号"] == 2
    assert latest["规则版本"] == 2
    assert latest["归因结果"]["触点权重"] == {"首次触点": 0.1, "末次触点": 0.1, "中间触点": 0.8}

    # 旧版本仍可按版本号回放，且回放内容与当时一致。
    old = serve(client, version_no=1)
    assert old["规则版本"] == 1
    assert old["归因结果"]["触点权重"] == {"首次触点": 0.4, "末次触点": 0.4, "中间触点": 0.2}


def test_response_and_release_log_trace_back_to_rule_version(client: TestClient) -> None:
    upgrade_and_publish_rule(client, {"首次触点": 0.1, "末次触点": 0.1, "中间触点": 0.8})
    assert publish(client, 规则编号=RULE).status_code == 200

    payload = serve(client)
    releases = client.get(f"/api/delivery/{SERVICE}/releases").json()["items"]
    matched = [
        row
        for row in releases
        if row["版本号"] == payload["服务版本号"] and row["规则版本"] == payload["规则版本"]
    ]
    assert matched, releases
    assert matched[0]["契约签名"]


# ---------------------------------------------------------------- 幂等与并发
def test_repeated_publish_is_idempotent(client: TestClient) -> None:
    first = publish(client, 规则编号=RULE)
    assert first.status_code == 200
    version = first.json()["entry"]["版本号"]

    second = publish(client, 规则编号=RULE)
    assert second.status_code == 200
    assert second.json()["entry"]["版本号"] == version
    assert "重复发布" in second.json()["message"]

    contracts = [row for row in client.get("/api/delivery").json()["items"] if row["服务编号"] == SERVICE]
    assert len(contracts) == 1


def test_concurrent_publish_yields_single_complete_contract(client: TestClient) -> None:
    service = DeliveryService()

    def do_publish(_: int):
        return service.publish(service_no=SERVICE, actor_name="并发发布人", overrides={})

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(do_publish, range(16)))

    entries = [entry for entry, _, _ in results]
    assert all(entry is not None for entry in entries)
    # 全部重复发布同一个内容，最终只应存在一份契约。
    versions = service.versions_of(SERVICE)
    assert len(versions) == 1
    contract = versions[0]["契约"]
    for field in [
        "规则编号",
        "规则版本",
        "归因类型",
        "回溯窗口天",
        "触点权重",
        "返回字段",
        "查询参数定义",
        "消费方应用",
        "业务归属",
    ]:
        assert contract.get(field) not in (None, "", [], {}), field


def test_concurrent_switch_keeps_single_active_contract(client: TestClient) -> None:
    service = DeliveryService()
    upgrade_and_publish_rule(client, {"首次触点": 0.1, "末次触点": 0.1, "中间触点": 0.8})
    published = publish(client, 规则编号=RULE)
    assert published.status_code == 200
    assert len(service.versions_of(SERVICE)) == 2

    def do_switch(target: int):
        return service.switch_version(service_no=SERVICE, target_version=target, actor_name="并发切换人")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(do_switch, [1, 2] * 8))

    assert all(entry is not None for entry, _ in results)
    actives = [row for row in service.versions_of(SERVICE) if row.get("is_active")]
    assert len(actives) == 1
    contract = actives[0]["契约"]
    for field in [
        "规则编号",
        "规则版本",
        "归因类型",
        "回溯窗口天",
        "触点权重",
        "首次触点权重",
        "末次触点权重",
        "中间触点衰减",
        "返回字段",
        "查询参数定义",
        "消费方应用",
        "业务归属",
    ]:
        assert contract.get(field) not in (None, "", [], {}), field
    # 读取链路跟随最终生效版本，且两处规则版本一致。
    payload = serve(client)
    assert payload["服务版本号"] == actives[0]["版本号"]
    assert payload["规则版本"] == contract["规则版本"]


def test_failed_contract_write_rolls_back_entirely(client: TestClient) -> None:
    before = serve(client)
    versions_before = len(client.get("/api/delivery").json()["items"])

    # 返回字段为空会让整份契约校验失败。
    response = publish(client, 规则编号=RULE, 返回字段=[])
    assert response.status_code == 400
    assert "整次回退" in response.json()["detail"]

    assert len(client.get("/api/delivery").json()["items"]) == versions_before
    after = serve(client)
    assert after["服务版本号"] == before["服务版本号"]
    assert after["归因结果"] == before["归因结果"]


# ---------------------------------------------------------------- 回滚
def test_rollback_realigns_attribution_result_with_rule_version(client: TestClient) -> None:
    upgrade_and_publish_rule(client, {"首次触点": 0.1, "末次触点": 0.1, "中间触点": 0.8})
    assert publish(client, 规则编号=RULE).status_code == 200

    response = client.post(
        f"/api/delivery/{SERVICE}/rollback",
        json={"values": {"版本号": 1}},
        headers=headers(),
    )
    assert response.status_code == 200, response.text

    rolled = serve(client)
    assert rolled["服务版本号"] == 1
    assert rolled["规则版本"] == 1
    assert rolled["归因结果"]["触点权重"] == {"首次触点": 0.4, "末次触点": 0.4, "中间触点": 0.2}

    history = client.get(f"/api/delivery/{SERVICE}/releases").json()["items"]
    assert any(row["动作"] == "回滚" and row["版本号"] == 1 for row in history)


# ---------------------------------------------------------------- 不可用原因
def test_stopped_consumer_gets_explicit_unavailable_reason(client: TestClient) -> None:
    assert serve(client)["归因结果"]

    response = client.post(
        f"/api/delivery/{SERVICE}/consumer",
        json={"values": {"消费方应用": CONSUMER, "状态": "已停止"}},
        headers=headers(),
    )
    assert response.status_code == 200

    stopped = client.get(f"/api/delivery/{SERVICE}/serve", params={"consumer_app": CONSUMER})
    assert stopped.status_code == 409
    detail = stopped.json()["detail"]
    assert detail["reason"] == "CONSUMER_STOPPED"
    assert detail["message"]
    assert "归因结果" not in stopped.json()


def test_offline_rule_does_not_serve_previous_attribution(client: TestClient) -> None:
    entry_id = rule_id(client)
    response = client.post(
        f"/api/attribution/{entry_id}/actions",
        json={"values": {"action": "下线规则"}},
        headers=headers(),
    )
    assert response.status_code == 200

    offline = client.get(f"/api/delivery/{SERVICE}/serve", params={"consumer_app": CONSUMER})
    assert offline.status_code == 409
    detail = offline.json()["detail"]
    assert detail["reason"] == "RULE_OFFLINE"
    assert "归因结果" not in offline.json()


def test_unknown_consumer_is_rejected(client: TestClient) -> None:
    response = client.get(f"/api/delivery/{SERVICE}/serve", params={"consumer_app": "other-app"})
    assert response.status_code == 403
    assert response.json()["detail"]["reason"] == "CONSUMER_FORBIDDEN"


# ---------------------------------------------------------------- 权限
@pytest.mark.parametrize("role", ["analyst", "reviewer"])
def test_non_release_manager_cannot_publish_or_switch(client: TestClient, role: str) -> None:
    publish_response = client.post(
        "/api/delivery/publish",
        json={"values": {"服务编号": SERVICE, "规则编号": RULE}},
        headers=headers(role),
    )
    assert publish_response.status_code == 403

    switch_response = client.post(
        f"/api/delivery/{SERVICE}/switch",
        json={"values": {"版本号": 1}},
        headers=headers(role),
    )
    assert switch_response.status_code == 403

    assert serve(client)["服务版本号"] == 1


def test_analyst_cannot_modify_rules_but_can_read(client: TestClient) -> None:
    entry_id = rule_id(client)
    response = client.post(
        f"/api/attribution/{entry_id}/upgrade",
        json={"values": {"触点权重": {"首次触点": 1.0}}},
        headers=headers("analyst"),
    )
    assert response.status_code == 403
    assert client.get("/api/attribution").status_code == 200


def test_release_manager_with_wrong_biz_line_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/delivery/publish",
        json={"values": {"服务编号": SERVICE, "规则编号": RULE}},
        headers=headers(biz_line="风控业务线"),
    )
    assert response.status_code == 403
    assert "业务归属不匹配" in response.json()["detail"]


def test_unknown_role_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/delivery/publish",
        json={"values": {"服务编号": SERVICE}},
        headers={"X-Role": "admin"},
    )
    assert response.status_code == 403


# ---------------------------------------------------------------- 字段对齐
def test_list_fields_align_with_detail_and_contract(client: TestClient) -> None:
    items = client.get("/api/delivery", params={"keyword": SERVICE}).json()["items"]
    assert len(items) == 1
    row = items[0]
    detail = client.get(f"/api/delivery/{row['id']}").json()
    for field in ("服务编号", "数据集名称", "规则编号", "版本号", "返回字段", "查询参数定义", "消费方应用", "业务归属"):
        assert row[field] == detail[field]
    assert row["规则版本"] == detail["契约"]["规则版本"]

    attribution = client.get("/api/attribution", params={"keyword": RULE}).json()["items"][0]
    assert attribution["规则版本"] == 1
    assert attribution["触点权重"]


def test_contract_signature_ignores_freeze_metadata() -> None:
    contract = {
        "规则编号": RULE,
        "规则版本": 1,
        "归因类型": "位置归因",
        "回溯窗口天": 30,
        "触点权重": {"首次触点": 0.4},
        "首次触点权重": 0.4,
        "末次触点权重": 0.4,
        "中间触点衰减": 0.2,
        "返回字段": ["规则编号"],
        "查询参数定义": "date_range",
        "消费方应用": CONSUMER,
        "业务归属": "增长业务线",
    }
    assert _contract_signature(contract) == _contract_signature({**contract, "冻结人": "a", "冻结时间": "b"})
