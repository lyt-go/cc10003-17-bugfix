"""数据服务业务规则：发布冻结契约、版本切换、消费方读取链路。

对应 SVC-ATTR-09 缺陷的修复口径：
- 发布即冻结：发布时把归因规则、触点权重、返回字段整体快照进「契约」，之后任何
  规则升版都不会改变已发布版本的响应；
- 规则升版只能生成新的服务版本，旧版本连同旧契约一起保留，历史响应可回放；
- 重复发布同一服务编号且契约内容一致时幂等返回现有版本，不产生重复契约；
- 同一服务编号的发布/切换/回滚在锁内串行执行，契约先完整构建再一次性提交，
  任一字段校验失败整次回退，不留半成品；
- 消费方停止或规则下线时，读取链路返回明确的不可用原因，绝不回退输出上一轮归因值。
"""
from __future__ import annotations

import copy
import hashlib
import json
import threading
from datetime import datetime, timezone
from typing import Any

from app.services.attribution import AttributionService, rule_version_no
from app.store import store

MODULE = "delivery"
CONSUMER_MODULE = "delivery_consumer"
RELEASE_MODULE = "delivery_release"
REQUIRED_FIELDS = ["服务编号", "数据集名称", "查询参数定义"]
ENTRY_FIELDS = [
    "服务编号",
    "数据集名称",
    "查询参数定义",
    "返回字段",
    "速率限制",
    "分页最大条数",
    "消费方应用",
    "业务归属",
    "规则编号",
]
STATUS_ORDER = ["草稿", "服务评审中", "已发布", "已下线"]
ACTION_RULES = {"提交服务评审": "服务评审中", "发布服务": "已发布", "下线服务": "已下线"}
NEGATIVE_ACTIONS = ["下线服务"]

# 契约内容字段：签名与冻结都以这组字段为准，缺一即视为契约不完整。
CONTRACT_FIELDS = [
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
]

# 同一服务编号的发布/切换/回滚必须串行，避免并发切换产生多份契约。
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(service_no: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(service_no, threading.Lock())


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _version_no(row: dict[str, Any]) -> int:
    try:
        return int(row.get("版本号", 0))
    except (TypeError, ValueError):
        return 0


def _contract_signature(contract: dict[str, Any]) -> str:
    """契约内容指纹：只对内容字段签名，冻结人/冻结时间等元数据不参与。"""
    payload = {field: contract.get(field) for field in CONTRACT_FIELDS}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class DeliveryService:
    def __init__(self) -> None:
        self._attribution = AttributionService()

    # ------------------------------------------------------------------ 查询
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("服务编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def versions_of(self, service_no: str) -> list[dict[str, Any]]:
        rows = [row for row in store.rows(MODULE) if row.get("服务编号") == service_no]
        return sorted(rows, key=_version_no)

    def active_version(self, service_no: str) -> dict[str, Any] | None:
        versions = self.versions_of(service_no)
        for row in versions:
            if row.get("is_active"):
                return row
        published = [row for row in versions if row.get("status") == "已发布"]
        return published[-1] if published else None

    def biz_line_of(self, service_no: str) -> str | None:
        for row in self.versions_of(service_no):
            if row.get("业务归属"):
                return str(row["业务归属"])
        return None

    # ------------------------------------------------------------------ 登记
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        # 全量保留提交字段：返回字段、版本号、消费方应用不能再在登记时丢失。
        for field in ENTRY_FIELDS:
            entry[field] = copy.deepcopy(values.get(field))
        entry["版本号"] = values.get("版本号") or 1
        entry["is_active"] = False
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    # ------------------------------------------------------------------ 发布
    def publish(
        self,
        *,
        service_no: str,
        actor_name: str,
        overrides: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str, str | None]:
        """发布服务版本：冻结契约、幂等去重、失败整次回退。

        返回 (entry, message, error_kind)；error_kind 为 validation/conflict/None。
        """
        overrides = overrides or {}
        with _lock_for(service_no):
            versions = self.versions_of(service_no)
            base = self.active_version(service_no) or (versions[-1] if versions else None)

            rule_no = str(overrides.get("规则编号") or (base or {}).get("规则编号") or "").strip()
            if not rule_no:
                return None, f"服务 {service_no} 缺少绑定的归因规则编号，本次发布已回退", "validation"
            rule = self._attribution.latest_published(rule_no)
            if rule is None:
                return None, f"归因规则 {rule_no} 不存在或尚未发布，本次发布已回退", "conflict"

            contract, error = self._build_contract(rule, base, overrides, actor_name)
            if error:
                return None, error, "validation"
            # 数据集名称不完全来自归因规则，单独校验，避免发布出缺字段的服务版本。
            dataset_name = overrides.get("数据集名称") or (base or {}).get("数据集名称")
            if not str(dataset_name or "").strip():
                return None, f"契约字段「数据集名称」缺失或为空，任一字段写入失败即整次回退，本次发布未生效", "validation"
            signature = _contract_signature(contract)

            # 幂等：当前生效版本的契约签名一致，说明是重复发布，直接返回现有契约。
            if (
                base is not None
                and base.get("is_active")
                and base.get("status") == "已发布"
                and _contract_signature(base.get("契约") or {}) == signature
            ):
                return base, f"服务 {service_no} 重复发布内容一致，沿用已冻结的 v{_version_no(base)} 契约", None

            version_no = max((_version_no(row) for row in versions), default=0) + 1
            entry = {"id": max((int(row.get("id", 0)) for row in store.rows(MODULE)), default=0) + 1}
            for field in ENTRY_FIELDS:
                entry[field] = copy.deepcopy(contract.get(field, (base or {}).get(field)))
            entry["数据集名称"] = copy.deepcopy(dataset_name)
            entry["服务编号"] = service_no
            entry["版本号"] = version_no
            entry["契约"] = contract
            entry["契约签名"] = signature
            entry["is_active"] = True
            entry["status"] = "已发布"
            entry["pending"] = False
            entry["abnormal"] = False

            # 提交点：契约已完整构建并通过校验，这里只做不会失败的整体切换。
            for row in versions:
                row["is_active"] = False
            store.rows(MODULE).append(entry)
            self._log_release(service_no, entry, actor_name, action="发布服务")
            return (
                entry,
                f"服务 {service_no} 已发布 v{version_no}，契约冻结到规则 {rule_no} v{contract['规则版本']}",
                None,
            )

    def _build_contract(
        self,
        rule: dict[str, Any],
        base: dict[str, Any] | None,
        overrides: dict[str, Any],
        actor_name: str,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """把规则版本与服务字段冻结成一份完整契约；任一字段无效则整体放弃。"""
        base = base or {}

        def pick(field: str) -> Any:
            if field in overrides and overrides[field] not in (None, ""):
                return copy.deepcopy(overrides[field])
            if field in base and base[field] not in (None, ""):
                return copy.deepcopy(base[field])
            return copy.deepcopy(rule.get(field))

        contract: dict[str, Any] = {
            "规则编号": rule.get("规则编号"),
            "规则版本": rule_version_no(rule),
            "归因类型": rule.get("归因类型"),
            "回溯窗口天": rule.get("回溯窗口天"),
            "触点权重": copy.deepcopy(rule.get("触点权重")),
            "首次触点权重": copy.deepcopy(rule.get("首次触点权重")),
            "末次触点权重": copy.deepcopy(rule.get("末次触点权重")),
            "中间触点衰减": copy.deepcopy(rule.get("中间触点衰减")),
            "返回字段": pick("返回字段"),
            "查询参数定义": pick("查询参数定义"),
            "消费方应用": pick("消费方应用"),
            "业务归属": pick("业务归属"),
        }
        for field in CONTRACT_FIELDS:
            value = contract.get(field)
            empty = value is None or value == "" or value == [] or value == {}
            if empty:
                return None, f"契约字段「{field}」缺失或为空，任一字段写入失败即整次回退，本次发布未生效"
        contract["冻结人"] = actor_name
        contract["冻结时间"] = _utcnow()
        return contract, None

    # -------------------------------------------------------------- 版本切换
    def switch_version(
        self, *, service_no: str, target_version: int, actor_name: str, action: str = "切换版本"
    ) -> tuple[dict[str, Any] | None, str]:
        with _lock_for(service_no):
            versions = self.versions_of(service_no)
            if not versions:
                return None, f"数据服务 {service_no} 不存在"
            target = next((row for row in versions if _version_no(row) == int(target_version)), None)
            if target is None:
                return None, f"服务 {service_no} 不存在版本 v{target_version}"
            if target.get("status") == "已下线":
                return None, f"服务 {service_no} v{target_version} 已下线，不能切换"
            if target.get("status") != "已发布" or not target.get("契约"):
                return None, f"服务 {service_no} v{target_version} 缺少冻结契约，不能切换"
            for row in versions:
                row["is_active"] = row is target
            self._log_release(service_no, target, actor_name, action=action)
            return (
                target,
                f"服务 {service_no} 已{action}到 v{_version_no(target)}（规则版本 v{target['契约']['规则版本']}）",
            )

    def rollback(
        self, *, service_no: str, actor_name: str, target_version: int | None = None
    ) -> tuple[dict[str, Any] | None, str]:
        """回滚到历史版本：默认回到当前生效版本之前的最近一个可用版本。"""
        active = self.active_version(service_no)
        if active is None:
            return None, f"数据服务 {service_no} 不存在或没有已发布版本"
        if target_version is None:
            candidates = [
                row
                for row in self.versions_of(service_no)
                if _version_no(row) < _version_no(active)
                and row.get("status") == "已发布"
                and row.get("契约")
            ]
            if not candidates:
                return None, f"服务 {service_no} 没有可回滚的历史版本"
            target_version = _version_no(candidates[-1])
        return self.switch_version(
            service_no=service_no, target_version=target_version, actor_name=actor_name, action="回滚"
        )

    # -------------------------------------------------------------- 消费方状态
    def set_consumer_status(
        self, *, service_no: str, consumer_app: str, status: str
    ) -> tuple[dict[str, Any], str]:
        rows = store.rows(CONSUMER_MODULE)
        for row in rows:
            if row.get("服务编号") == service_no and row.get("消费方应用") == consumer_app:
                row["状态"] = status
                return row, f"消费方 {consumer_app} 已置为「{status}」"
        row = {"id": len(rows) + 1, "服务编号": service_no, "消费方应用": consumer_app, "状态": status}
        rows.append(row)
        return row, f"消费方 {consumer_app} 已置为「{status}」"

    def consumer_status(self, service_no: str, consumer_app: str) -> str:
        for row in store.rows(CONSUMER_MODULE):
            if row.get("服务编号") == service_no and row.get("消费方应用") == consumer_app:
                return str(row.get("状态") or "正常")
        return "正常"

    # ------------------------------------------------------------------ 读取
    def serve(
        self, *, service_no: str, consumer_app: str, version_no: int | None = None
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """消费方读取归因结果。

        返回 (payload, error)；error 含 reason/message/status_code，绝不夹带旧归因值。
        """
        versions = self.versions_of(service_no)
        if not versions:
            return None, {
                "reason": "SERVICE_NOT_FOUND",
                "message": f"数据服务 {service_no} 不存在",
                "status_code": 404,
            }
        if version_no is not None:
            entry = next((row for row in versions if _version_no(row) == int(version_no)), None)
            if entry is None:
                return None, {
                    "reason": "VERSION_NOT_FOUND",
                    "message": f"服务 {service_no} 不存在版本 v{version_no}",
                    "status_code": 404,
                }
        else:
            entry = self.active_version(service_no)
        if entry is None or not entry.get("契约"):
            return None, {
                "reason": "CONTRACT_MISSING",
                "message": f"服务 {service_no} 尚未发布冻结契约，暂不可用",
                "status_code": 409,
            }
        if entry.get("status") == "已下线":
            return None, {
                "reason": "SERVICE_OFFLINE",
                "message": f"服务 {service_no} v{_version_no(entry)} 已下线，停止输出归因结果",
                "status_code": 409,
            }
        contract = entry["契约"]
        if consumer_app != contract.get("消费方应用"):
            return None, {
                "reason": "CONSUMER_FORBIDDEN",
                "message": f"消费方 {consumer_app} 不在服务 {service_no} 的授权范围内",
                "status_code": 403,
            }
        if self.consumer_status(service_no, consumer_app) == "已停止":
            return None, {
                "reason": "CONSUMER_STOPPED",
                "message": f"消费方 {consumer_app} 已停止，服务 {service_no} 不再输出归因结果",
                "status_code": 409,
            }
        rule = self._attribution.find_version(contract["规则编号"], contract["规则版本"])
        if rule is None or rule.get("status") == "已下线":
            return None, {
                "reason": "RULE_OFFLINE",
                "message": f"归因规则 {contract['规则编号']} v{contract['规则版本']} 已下线，服务 {service_no} 不可用",
                "status_code": 409,
            }
        payload = {
            "服务编号": service_no,
            "服务版本号": _version_no(entry),
            "规则编号": contract["规则编号"],
            "规则版本": contract["规则版本"],
            "消费方应用": consumer_app,
            "返回字段": copy.deepcopy(contract["返回字段"]),
            "归因结果": {
                "归因类型": copy.deepcopy(contract["归因类型"]),
                "回溯窗口天": copy.deepcopy(contract["回溯窗口天"]),
                "触点权重": copy.deepcopy(contract["触点权重"]),
                "首次触点权重": copy.deepcopy(contract["首次触点权重"]),
                "末次触点权重": copy.deepcopy(contract["末次触点权重"]),
                "中间触点衰减": copy.deepcopy(contract["中间触点衰减"]),
            },
            "冻结人": contract.get("冻结人"),
            "冻结时间": contract.get("冻结时间"),
        }
        return payload, None

    # ------------------------------------------------------------------ 发布记录
    def _log_release(self, service_no: str, entry: dict[str, Any], actor_name: str, *, action: str) -> None:
        rows = store.rows(RELEASE_MODULE)
        contract = entry.get("契约") or {}
        rows.append(
            {
                "id": len(rows) + 1,
                "服务编号": service_no,
                "版本号": _version_no(entry),
                "规则编号": contract.get("规则编号"),
                "规则版本": contract.get("规则版本"),
                "契约签名": entry.get("契约签名") or _contract_signature(contract),
                "动作": action,
                "操作人": actor_name,
                "时间": _utcnow(),
            }
        )

    def release_log(self, service_no: str) -> list[dict[str, Any]]:
        rows = [row for row in store.rows(RELEASE_MODULE) if row.get("服务编号") == service_no]
        return sorted(rows, key=lambda row: int(row.get("id", 0)), reverse=True)

    # ------------------------------------------------------------------ 动作
    def run_action(
        self, entry_id: int, action: str, *, actor_name: str = "system"
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"服务版本 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据服务可执行范围"
        service_no = str(entry.get("服务编号") or "")
        if action == "发布服务":
            # 发布必须走冻结契约链路，不能再只翻转状态。
            published, message, _ = self.publish(service_no=service_no, actor_name=actor_name)
            return published, message
        if action == "下线服务":
            with _lock_for(service_no):
                for row in self.versions_of(service_no):
                    if _version_no(row) == _version_no(entry):
                        row["status"] = "已下线"
                        row["pending"] = False
                        row["abnormal"] = True
                        # 保留 is_active 标记：读取链路据此返回 SERVICE_OFFLINE，
                        # 而不是悄悄回退到更早的版本输出旧归因值。
                return entry, "服务版本已下线服务"
        target = ACTION_RULES[action]
        entry["status"] = target
        entry["pending"] = True
        entry["abnormal"] = False
        return entry, f"服务版本已{action}"
