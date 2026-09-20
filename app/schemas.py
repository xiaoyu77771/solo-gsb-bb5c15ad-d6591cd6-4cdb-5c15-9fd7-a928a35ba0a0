"""请求 / 响应模型。"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field, StringConstraints, model_validator

from .analysis import (
    MAX_BLOCKED_PAIRS,
    normalize_blocked_pairs,
)


def _not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("name must not be blank")
    return value


# 单个显示名的统一约束：1–64 字符、非空白（注册 / 检查 / 批量接口共用）
NameStr = Annotated[
    str,
    StringConstraints(min_length=1, max_length=64),
    AfterValidator(_not_blank),
]

# 命名空间的统一约束：1–64 字符、非空白（合并接口的请求体使用）
NamespaceStr = Annotated[
    str,
    StringConstraints(min_length=1, max_length=64),
    AfterValidator(_not_blank),
]


class NameRequest(BaseModel):
    """注册 / 检查请求体。"""

    name: NameStr = Field(examples=["Аdmin"])


class RenameRequest(BaseModel):
    """原子改名请求体：新名称 + 调用方持有的乐观锁版本。"""

    name: NameStr = Field(examples=["Аdmin"])
    expected_revision: int = Field(
        ge=1,
        examples=[1],
        description=(
            "调用方上次读取到的 revision；与当前值一致才执行改名，"
            "成功后 revision 递增"
        ),
    )


# 释放名称的冷却时长边界（与 service 层常量保持一致）
MIN_COOLDOWN_SECONDS = 60
MAX_COOLDOWN_SECONDS = 86400


class ReleaseNameRequest(BaseModel):
    """删除（释放）名称请求体：乐观锁版本 + 冷却时长（60–86400 秒）。"""

    expected_revision: int = Field(
        ge=1,
        examples=[1],
        description=(
            "调用方上次读取到的 revision；与当前值一致才执行删除，"
            "删除成功后原规范名进入冷却"
        ),
    )
    cooldown_seconds: int = Field(
        ge=MIN_COOLDOWN_SECONDS,
        le=MAX_COOLDOWN_SECONDS,
        examples=[3600],
        description=(
            "释放冷却时长（秒），范围 60–86400；到期前该规范名与已注册名称、"
            "有效预占同等参与全部冲突与混脚本判定"
        ),
    )


class BatchNameRequest(BaseModel):
    """批量注册请求体：1–50 个名称，全部通过才落库。"""

    names: list[NameStr] = Field(
        min_length=1,
        max_length=50,
        examples=[["paypal", "Alice"]],
    )


class SwapNameEntry(BaseModel):
    """原子交换中的一方：记录 id 与调用方持有的乐观锁版本。"""

    id: int = Field(ge=1, examples=[1])
    expected_revision: int = Field(
        ge=1,
        examples=[1],
        description="调用方上次读取到的该记录 revision；双方版本都一致才执行交换",
    )


class SwapNamesRequest(BaseModel):
    """原子交换请求体：恰好两条不同的记录，各自携带 expected_revision。"""

    records: list[SwapNameEntry] = Field(
        min_length=2,
        max_length=2,
        examples=[{"id": 1, "expected_revision": 1}],
    )

    @model_validator(mode="after")
    def _require_distinct_ids(self) -> "SwapNamesRequest":
        if self.records[0].id == self.records[1].id:
            raise ValueError("records 必须是两条不同的记录（id 不能相同）")
        return self


class ReservationRequest(BaseModel):
    """预占请求体：名称 + 有效期（30–600 秒）。"""

    name: NameStr = Field(examples=["Аdmin"])
    ttl_seconds: int = Field(
        ge=30,
        le=600,
        examples=[120],
        description="预占有效期（秒），范围 30–600；到期未确认即失效",
    )


class ScriptDetail(BaseModel):
    script: str
    characters: list[str]
    code_points: list[str]


class ScriptPolicyRequest(BaseModel):
    """命名空间混脚本策略 PUT 请求体：阻断脚本对 + 乐观锁版本。

    blocked_script_pairs 无序且去重：输入顺序无关，大小写敏感，重复对只
    保留一次；每一项为恰好两个不同的合法 Unicode 脚本名，空列表表示
    “不拦截任何脚本组合”。
    """

    blocked_script_pairs: list[list[str]] = Field(
        max_length=MAX_BLOCKED_PAIRS,
        examples=[[["Latin", "Cyrillic"], ["Greek", "Latin"]]],
        description=(
            "被阻断的无序脚本对：名称同时出现一对中的两个脚本即违规。"
            "顺序无关、自动去重；空列表表示不拦截任何混脚本组合"
        ),
    )
    expected_revision: int = Field(
        ge=0,
        examples=[0],
        description=(
            "调用方上次读取到的策略 revision；命名空间尚未配置策略时为 0，"
            "首次配置必须传 0，成功后 revision 变为 1，之后每次 PUT 递增"
        ),
    )

    @model_validator(mode="after")
    def _canonicalize_pairs(self) -> "ScriptPolicyRequest":
        # 逐项校验脚本名（未知 / Common / Inherited / Unknown / 同名对 -> 422），
        # 再无序去重并按字典序排列，使服务层只需处理规范对
        normalized = normalize_blocked_pairs(self.blocked_script_pairs)
        object.__setattr__(self, "blocked_script_pairs", [list(pair) for pair in normalized])
        return self


class ScriptPolicy(BaseModel):
    """命名空间混脚本策略（GET 与 PUT 成功的响应）。"""

    namespace: str
    blocked_script_pairs: list[list[str]] = Field(
        description="生效中的阻断脚本对（无序、已去重，按字典序排列）"
    )
    revision: int = Field(description="策略版本号：首次配置为 1，每次成功 PUT 递增")
    created_at: str
    updated_at: str


class ConflictRecord(BaseModel):
    id: int
    original_name: str
    normalized_name: str
    reason: Literal["normalized_match", "confusable"]
    # 冲突来源：已注册名称（默认，向后兼容）、仍有效的预占，或删除后的冷却记录
    source: Literal["registered", "reservation", "released"] = "registered"
    # source 为 reservation / released 时出现：记录过期时刻（UTC ISO 8601）
    expires_at: str | None = None


class AnalysisResult(BaseModel):
    """名称分析结果（检查接口的响应，也是注册前的内部判定结果）。"""

    namespace: str
    original_name: str
    normalized_name: str
    scripts: list[str]
    script_details: list[ScriptDetail]
    restricted_scripts_used: list[str]
    blocked_script_pairs: list[list[str]] = Field(
        description="本次判定生效的命名空间混脚本策略（未配置时为内建默认策略）"
    )
    violating_script_pairs: list[list[str]] = Field(
        description="名称实际命中的阻断脚本对；非空即 mixed_script_violation"
    )
    mixed_script_violation: bool
    conflicts: list[ConflictRecord]


class NameRecord(BaseModel):
    id: int
    namespace: str
    original_name: str
    normalized_name: str
    revision: int
    created_at: str


class ReservationRecord(BaseModel):
    """预占成功响应：不可猜测的确认 token 与 UTC 过期时刻。"""

    id: int
    namespace: str
    original_name: str
    normalized_name: str
    token: str
    ttl_seconds: int
    expires_at: str
    created_at: str


class CooldownRecord(BaseModel):
    """释放冷却记录：删除名称后生成，expires_at 前持续占位。"""

    id: int
    namespace: str
    original_name: str
    normalized_name: str
    cooldown_seconds: int
    expires_at: str
    created_at: str


class ClaimedNameRecord(NameRecord):
    """确认预占成功响应：已注册名称记录 + 消费掉的预占信息。"""

    token: str
    expires_at: str
    consumed_at: str


class BatchItemRecord(NameRecord):
    """批量注册成功的单项记录：落库字段 + 原请求索引。"""

    index: int


class BatchRegisterResponse(BaseModel):
    """批量注册成功响应：items 按原请求顺序返回。"""

    namespace: str
    items: list[BatchItemRecord]


class SwapNameItemRecord(SwapNameEntry):
    """原子交换成功的单项记录：请求中的 id / expected_revision + 交换后的完整记录。"""

    index: int
    namespace: str
    original_name: str
    normalized_name: str
    revision: int
    created_at: str


class SwapNamesResponse(BaseModel):
    """原子交换成功响应：items 按请求顺序返回交换后的两条记录。"""

    namespace: str
    items: list[SwapNameItemRecord]


class MergeRequest(BaseModel):
    """合并请求体：目标命名空间（源命名空间在路径中）。"""

    target_namespace: NamespaceStr = Field(
        examples=["chat-v2"],
        description="目标命名空间：源命名空间的全部名称与预占记录将迁入此处",
    )


class MergeResult(BaseModel):
    """合并成功响应：迁入目标命名空间的记录数量。"""

    source_namespace: str
    target_namespace: str
    merged_names: int = Field(description="迁移的已注册名称数量")
    merged_reservations: int = Field(
        description="迁移的预占记录数量（含已消费的历史记录；过期记录已惰性清理，不迁移）"
    )
    merged_cooldowns: int = Field(
        description="迁移的释放冷却记录数量（未过期记录；过期记录已惰性清理，不迁移）"
    )


class ErrorDetail(BaseModel):
    error: str
    message: str
