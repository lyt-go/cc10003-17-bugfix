"""归因模型业务规则：状态流转、规则版本与升版冻结口径都收在这里。

关键约束（对应 SVC-ATTR-09 缺陷修复）：
- 每条规则版本一旦发布即冻结，任何改动只能走「规则升版」生成新的规则版本；
- 升版不会改动或下线旧版本，旧版本保持已发布冻结态，供历史服务契约继续追溯；
- 显式「下线规则」只作用于被操作的那一个规则版本；
- 提交字段全量落库，触点权重等字段不能在登记/升版时被静默丢弃。
"""
from __future__ import annotations

import copy
from typing import Any

from app.store import store

MODULE = "attribution"
REQUIRED_FIELDS = ["规则编号", "归因类型", "回溯窗口天"]
RULE_FIELDS = [
    "规则编号",
    "归因类型",
    "回溯窗口天",
    "触点权重",
    "首次触点权重",
    "末次触点权重",
    "中间触点衰减",
    "关联渠道",
    "业务归属",
]
STATUS_ORDER = ["草稿", "规则评审中", "已发布", "已下线"]
ACTION_RULES = {"提交规则评审": "规则评审中", "发布规则": "已发布", "下线规则": "已下线"}
NEGATIVE_ACTIONS = ["下线规则"]
FROZEN_FIELDS = [
    "归因类型",
    "回溯窗口天",
    "触点权重",
    "首次触点权重",
    "末次触点权重",
    "中间触点衰减",
    "关联渠道",
    "业务归属",
]


def rule_version_no(row: dict[str, Any]) -> int:
    try:
        return int(row.get("规则版本", 0) or 0)
    except (TypeError, ValueError):
        return 0


class AttributionService:
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
            rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def find_version(self, rule_no: str, version: int) -> dict[str, Any] | None:
        """按规则编号 + 规则版本精确定位一条规则版本。"""
        for row in store.rows(MODULE):
            if row.get("规则编号") == rule_no and rule_version_no(row) == int(version):
                return row
        return None

    def latest_published(self, rule_no: str) -> dict[str, Any] | None:
        """取规则编号下版本号最大的「已发布」规则版本；服务发布只绑定它。"""
        published = [
            row
            for row in store.rows(MODULE)
            if row.get("规则编号") == rule_no and row.get("status") == "已发布"
        ]
        if not published:
            return None
        return max(published, key=rule_version_no)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        # 全量保留提交字段：触点权重、首次/末次触点权重等不能再被静默丢弃。
        for field in RULE_FIELDS:
            entry[field] = copy.deepcopy(values.get(field))
        entry["规则版本"] = 1
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def upgrade_rule(
        self, entry_id: int, overrides: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any] | None, str]:
        """规则升版：基于一条已发布规则生成新版本行，旧版本原样冻结保留。"""
        source = store.find(MODULE, entry_id)
        if source is None:
            return None, f"归因规则 {entry_id} 不存在或已归档"
        if source.get("status") != "已发布":
            return None, f"规则 {source.get('规则编号')} 当前为「{source.get('status')}」，只有已发布版本才能升版"
        rows = store.rows(MODULE)
        overrides = overrides or {}
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        for field in RULE_FIELDS:
            # 升版字段以旧版本为基线深拷贝，覆盖项只作用于新版本，旧版本不受影响。
            entry[field] = copy.deepcopy(overrides.get(field, source.get(field)))
        entry["规则版本"] = rule_version_no(source) + 1
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, f"规则 {entry['规则编号']} 已升版到 v{entry['规则版本']}，旧版本 v{rule_version_no(source)} 保持冻结"

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"归因规则 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于归因模型可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        if entry.get("status") == "已发布" and target != "已下线":
            return None, (
                f"规则 {entry.get('规则编号')} v{rule_version_no(entry)} 已发布并冻结，"
                "不能回退状态；改动请走「规则升版」生成新版本"
            )
        entry["status"] = target
        entry["pending"] = target in STATUS_ORDER[:2]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"归因规则已{action}"
