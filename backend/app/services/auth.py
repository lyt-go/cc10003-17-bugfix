"""访问控制：发布角色（analyst / reviewer / release_manager）与业务归属。

约定：
- analyst         只读调用方，只能查看，不能改动归因规则或服务契约；
- reviewer        可以登记草稿、提交评审、登记规则升版，不能发布/切换/回滚；
- release_manager 发布角色，可以发布、切换、回滚、下线、调整消费方状态，
                  但仅限自己业务归属内的对象。

角色与业务归属通过请求头携带：X-Operator、X-Role、X-Biz-Line。
不带头的调用按只读 analyst 处理，保证默认安全。
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import unquote

from fastapi import Header, HTTPException

ROLE_ANALYST = "analyst"
ROLE_REVIEWER = "reviewer"
ROLE_RELEASE_MANAGER = "release_manager"
ROLES = {ROLE_ANALYST, ROLE_REVIEWER, ROLE_RELEASE_MANAGER}

ROLE_LABELS = {
    ROLE_ANALYST: "只读分析师",
    ROLE_REVIEWER: "评审人",
    ROLE_RELEASE_MANAGER: "发布经理",
}


@dataclass(frozen=True)
class Actor:
    """一次调用背后的操作人。"""

    name: str
    role: str
    biz_line: str

    @property
    def can_edit_draft(self) -> bool:
        return self.role in {ROLE_REVIEWER, ROLE_RELEASE_MANAGER}

    @property
    def can_release(self) -> bool:
        return self.role == ROLE_RELEASE_MANAGER


def _header_text(value: str | None) -> str:
    """HTTP 头只允许 ASCII：中文业务归属按 UTF-8 百分号编码传输，这里还原。"""
    if not value:
        return ""
    text = value.strip()
    if "%" in text:
        try:
            text = unquote(text, encoding="utf-8")
        except (UnicodeDecodeError, ValueError):
            pass
    return text.strip()


def get_actor(
    x_operator: str | None = Header(default=None),
    x_role: str | None = Header(default=None),
    x_biz_line: str | None = Header(default=None),
) -> Actor:
    """从请求头解析操作人；缺省按只读 analyst 处理，未知角色直接拒绝。"""
    role = _header_text(x_role).lower() or ROLE_ANALYST
    if role not in ROLES:
        raise HTTPException(
            status_code=403,
            detail=f"未知角色「{role}」，仅支持 analyst、reviewer、release_manager",
        )
    return Actor(
        name=_header_text(x_operator) or "anonymous",
        role=role,
        biz_line=_header_text(x_biz_line),
    )


def require_edit_draft(actor: Actor, target: str) -> None:
    """登记/升版/提交评审：reviewer 与 release_manager 可做，analyst 只读。"""
    if not actor.can_edit_draft:
        raise HTTPException(
            status_code=403,
            detail=f"角色 {actor.role}（{ROLE_LABELS.get(actor.role, actor.role)}）只能查看，不能改动{target}",
        )


def require_release(actor: Actor, biz_line: str | None, action: str) -> None:
    """发布/切换/回滚/下线：仅 release_manager，且业务归属必须匹配。"""
    if not actor.can_release:
        raise HTTPException(
            status_code=403,
            detail=f"角色 {actor.role} 无权执行「{action}」，仅 release_manager 可操作",
        )
    target = (biz_line or "").strip()
    if target and actor.biz_line != target:
        raise HTTPException(
            status_code=403,
            detail=(
                f"业务归属不匹配：操作人属于「{actor.biz_line or '未指定'}」，"
                f"{action}对象属于「{target}」"
            ),
        )
