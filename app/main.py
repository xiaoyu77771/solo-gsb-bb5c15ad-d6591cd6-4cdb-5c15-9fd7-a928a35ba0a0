"""FastAPI 应用入口。

接口一览：
- POST   /namespaces/{namespace}/names                              注册显示名（201 / 409 / 422）
- POST   /namespaces/{namespace}/names/batch                        批量注册（201 / 409 / 422，单事务）
- POST   /namespaces/{namespace}/names/swap                         原子交换两条已注册名称（200 / 404 / 409 / 422，单事务）
- PATCH  /namespaces/{namespace}/names/{id}                         原子改名（200 / 404 / 409 / 422，单事务）
- DELETE /namespaces/{namespace}/names/{id}                         删除名称并生成释放冷却（200 / 404 / 409 / 422，单事务）
- GET    /namespaces/{namespace}/cooldowns                          列出命名空间内有效释放冷却记录
- POST   /namespaces/{namespace}/reservations                       预占名称（201 / 409 / 422）
- POST   /namespaces/{namespace}/reservations/{token}/claim         确认预占并注册（201 / 404 / 409 / 410）
- POST   /namespaces/{source}/merge                                 合并命名空间（200 / 409 / 422，单事务）
- PUT    /namespaces/{namespace}/script-policy                      配置命名空间混脚本策略（200 / 409 / 422，单事务）
- GET    /namespaces/{namespace}/script-policy                      读取命名空间混脚本策略（200 / 404）
- POST   /namespaces/{namespace}/check                              只分析不落库（200）
- GET    /namespaces/{namespace}/names                              列出命名空间内已注册名称
- GET    /health                                                    健康检查
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import asynccontextmanager
import sqlite3

from fastapi import Depends, FastAPI, HTTPException, status

from . import database, service
from .analysis import normalize_name
from .schemas import (
    AnalysisResult,
    BatchNameRequest,
    BatchRegisterResponse,
    ClaimedNameRecord,
    CooldownRecord,
    MergeRequest,
    MergeResult,
    NameRecord,
    NameRequest,
    ReleaseNameRequest,
    RenameRequest,
    ReservationRecord,
    ReservationRequest,
    ScriptPolicy,
    ScriptPolicyRequest,
    SwapNamesRequest,
    SwapNamesResponse,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时自动初始化数据库（建库建表，幂等，含向新表的自动迁移）
    database.init_db()
    yield


app = FastAPI(
    title="Unicode Display Name Anti-Spoofing API",
    version="1.6.0",
    description=(
        "命名空间内的显示名防仿冒：NFKC+casefold 规范名查重、"
        "confusables 同形字符判定；混脚本拦截支持命名空间级策略"
        "（PUT/GET script-policy，未配置时默认 Latin/Cyrillic/Greek "
        "任意两种混用即违规）；支持预占后确认注册（reservations / claim），"
        "未过期预占与已注册名称一样参与全部冲突判定；支持删除名称并设置"
        "释放冷却（DELETE names/{id}），未过期冷却记录与已注册名称、有效"
        "预占同等参与全部冲突与混脚本判定（source=released），可用 "
        "GET cooldowns 列出；支持把源命名空间的名称、预占与冷却记录整体"
        "合并到目标命名空间（merge，以目标策略检查两侧数据，冲突或违规时"
        "不修改任何数据）；支持凭 expected_revision 对已注册名称做原子改名"
        "（PATCH），改名在单个写锁事务内完成版本校验与冲突复查；支持同"
        "命名空间内两条已注册名称的原子交换（names/swap），单事务内校验"
        "双方归属与版本后成对互换名称并各递增 revision。"
    ),
    lifespan=lifespan,
)


def get_db() -> Iterator[sqlite3.Connection]:
    conn = database.get_connection()
    try:
        yield conn
    finally:
        conn.close()


def _mixed_script_error(analysis: dict) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={
            "error": "mixed_script_violation",
            "message": (
                "名称同时使用了所属命名空间混脚本策略所阻断的脚本组合，"
                "存在仿冒风险，已拒绝"
            ),
            "restricted_scripts_used": analysis["restricted_scripts_used"],
            "blocked_script_pairs": analysis["blocked_script_pairs"],
            "violating_script_pairs": analysis["violating_script_pairs"],
            "script_details": analysis["script_details"],
        },
    )


def _conflict_error(normalized_name: str, conflicts: list[dict], message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "error": "name_conflict",
            "message": message,
            "normalized_name": normalized_name,
            "conflicts": conflicts,
        },
    )


CONFLICT_MESSAGE = "规范名相同或与命名空间内已有名称（含未过期预占、释放冷却）同形"


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post(
    "/namespaces/{namespace}/names",
    status_code=status.HTTP_201_CREATED,
    response_model=NameRecord,
    responses={
        409: {"description": "规范名相同或与已有名称/有效预占/释放冷却同形（confusable）"},
        422: {"description": "混用 Latin/Cyrillic/Greek 中两种及以上脚本"},
    },
)
def register_name(namespace: str, payload: NameRequest, conn=Depends(get_db)) -> dict:
    analysis = service.analyze_name(conn, namespace, payload.name)

    if analysis["mixed_script_violation"]:
        raise _mixed_script_error(analysis)

    if analysis["conflicts"]:
        raise _conflict_error(
            analysis["normalized_name"], analysis["conflicts"], CONFLICT_MESSAGE
        )

    try:
        return service.register_name(
            conn, namespace, payload.name, analysis["normalized_name"]
        )
    except service.MixedScriptNameError as exc:
        # 并发策略改严：只读分析通过、写事务内复查命中新策略
        raise _mixed_script_error(exc.analysis)
    except service.NameConflictError as exc:
        # 并发下唯一约束/事务复查兜底：优先用事务内复查结果，否则重新查出冲突
        conflicts = exc.conflicts or service.find_conflicts(
            conn, namespace, analysis["normalized_name"]
        )
        raise _conflict_error(
            analysis["normalized_name"], conflicts, CONFLICT_MESSAGE
        )


@app.patch(
    "/namespaces/{namespace}/names/{name_id}",
    response_model=NameRecord,
    responses={
        404: {"description": "记录不存在，或不属于该命名空间"},
        409: {"description": "expected_revision 已过期（stale_revision），或新名称与其他记录/有效预占/释放冷却冲突"},
        422: {"description": "混用 Latin/Cyrillic/Greek 中两种及以上脚本"},
    },
)
def rename_registered_name(
    namespace: str, name_id: int, payload: RenameRequest, conn=Depends(get_db)
) -> dict:
    """原子改名：在单个 BEGIN IMMEDIATE 事务内完成乐观锁校验与改名。

    事务内按序校验：记录存在且命名空间匹配（否则 404）-> revision 与
    expected_revision 一致（否则 409 stale_revision，附 current_revision）
    -> 新名称不混脚本（否则 422）-> 新规范名与同空间其他已注册名称
    （排除当前记录）、全部有效预占及未过期冷却不冲突（否则 409，沿用注册
    明细结构）。成功后保留 id / created_at，更新原名与规范名，revision + 1。
    """
    try:
        return service.rename_name(
            conn, namespace, name_id, payload.name, payload.expected_revision
        )
    except service.NameNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "name_not_found",
                "message": "已注册名称不存在，或不属于该命名空间",
            },
        )
    except service.StaleRevisionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "stale_revision",
                "message": "名称记录已被其他请求修改，请重新读取最新版本后重试",
                "current_revision": exc.current_revision,
            },
        )
    except service.MixedScriptNameError as exc:
        raise _mixed_script_error(exc.analysis)
    except service.NameConflictError as exc:
        normalized_name = normalize_name(payload.name)
        # 并发下锁/唯一约束兜底：优先用事务内复查结果，否则回滚后重新查出冲突
        conflicts = exc.conflicts or service.find_conflicts(
            conn, namespace, normalized_name, exclude_name_id=name_id
        )
        raise _conflict_error(normalized_name, conflicts, CONFLICT_MESSAGE)


@app.delete(
    "/namespaces/{namespace}/names/{name_id}",
    response_model=CooldownRecord,
    responses={
        200: {"description": "名称已删除，并生成未过期的释放冷却记录"},
        404: {"description": "记录不存在，或不属于该命名空间"},
        409: {"description": "expected_revision 已过期（stale_revision），或锁竞争"},
        422: {"description": "cooldown_seconds 超出 60–86400 范围，或 expected_revision 非正整数"},
    },
)
def release_registered_name(
    namespace: str, name_id: int, payload: ReleaseNameRequest, conn=Depends(get_db)
) -> dict:
    """删除（释放）名称：单个 BEGIN IMMEDIATE 事务内校验归属与版本后删除并冷却。

    事务内按序校验：记录存在且命名空间匹配（否则 404）-> revision 与
    expected_revision 一致（否则 409 stale_revision，附 current_revision）。
    通过后删除 display_names 行并写入 released_name_cooldowns（原名 /
    规范名原样保留，cooldown_seconds 为 60–86400 秒，expires_at 为 UTC
    过期时刻）。冷却到期前，该规范名与同形名在单条 / 批量注册、预占、
    改名、check、合并及脚本策略更新中与已注册名称、有效预占同等被拦截，
    冲突明细以 source="released" 标识；过期记录在后续写操作前惰性清理。
    """
    try:
        return service.release_name(
            conn,
            namespace,
            name_id,
            payload.expected_revision,
            payload.cooldown_seconds,
        )
    except service.NameNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "name_not_found",
                "message": "已注册名称不存在，或不属于该命名空间",
            },
        )
    except service.StaleRevisionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "stale_revision",
                "message": "名称记录已被其他请求修改，请重新读取最新版本后重试",
                "current_revision": exc.current_revision,
            },
        )
    except service.NameConflictError:
        # 锁竞争：事务已回滚，数据未变，客户端重读后重试
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "release_conflict",
                "message": "删除时发生并发竞争，事务已回滚（名称未删除），请重试",
            },
        )


@app.post(
    "/namespaces/{namespace}/names/swap",
    response_model=SwapNamesResponse,
    responses={
        404: {"description": "任一记录不存在，或不属于该命名空间"},
        409: {"description": "任一方 expected_revision 已过期（stale_revision，附双方当前 revision），或锁竞争"},
        422: {"description": "records 不是恰好两条，或两条记录的 id 相同"},
    },
)
def swap_registered_names(
    namespace: str, payload: SwapNamesRequest, conn=Depends(get_db)
) -> dict:
    """原子交换：在单个 BEGIN IMMEDIATE 事务内校验双方归属与版本后成对互换名称。

    事务内按序校验：两条记录都存在且属于该命名空间（任一不满足 -> 404，
    附全部缺失 id）-> 双方 revision 都等于各自 expected_revision（任一
    过期 -> 409 stale_revision，明细同时给出双方当前 revision，客户端
    重读后可整体重试）。通过后在同一事务内借助未提交的随机临时名腾出
    唯一约束槽位，三步成对互换 original_name 与 normalized_name，双方
    revision 各 + 1；id / created_at 保持不变。临时名只存在于未提交事务
    中（写锁 + 快照隔离使任何并发请求都观察不到），任何失败都整体回滚，
    不存在部分更新。
    """
    entries = [(item.id, item.expected_revision) for item in payload.records]
    try:
        records = service.swap_names(conn, namespace, entries)
    except service.SwapRecordNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "name_not_found",
                "message": "待交换的名称记录不存在，或不属于该命名空间",
                "missing_ids": exc.missing_ids,
            },
        )
    except service.SwapStaleRevisionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "stale_revision",
                "message": (
                    "名称记录已被其他请求修改（双方中至少一方版本过期），"
                    "请重新读取双方最新版本后重试"
                ),
                "records": exc.records,
            },
        )
    except service.NameConflictError:
        # 锁竞争 / 临时名随机碰撞（概率可忽略）：事务已整体回滚，重读双方当前版本
        records_detail = []
        for rid, expected in entries:
            row = conn.execute(
                "SELECT revision FROM display_names WHERE id = ? AND namespace = ?",
                (rid, namespace),
            ).fetchone()
            records_detail.append(
                {
                    "id": rid,
                    "expected_revision": expected,
                    "current_revision": row["revision"] if row else None,
                }
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "swap_conflict",
                "message": "交换时发生并发竞争，事务已整体回滚（无部分更新），请重试",
                "records": records_detail,
            },
        )

    items = [
        {
            "index": index,
            "id": record["id"],
            "expected_revision": expected,
            **record,
        }
        for index, ((_, expected), record) in enumerate(zip(entries, records, strict=True))
    ]
    return {"namespace": namespace, "items": items}


@app.post(
    "/namespaces/{namespace}/names/batch",
    status_code=status.HTTP_201_CREATED,
    response_model=BatchRegisterResponse,
    responses={
        409: {"description": "并发竞争使事务判定失效：已回滚，返回重新分析后的冲突明细"},
        422: {"description": "批次中存在混脚本或库内/预占/批内冲突：汇总明细，未写入任何记录"},
    },
)
def register_names_batch(
    namespace: str, payload: BatchNameRequest, conn=Depends(get_db)
) -> dict:
    # 整批与库内已有名称、有效预占及未过期冷却联合分析：冲突 + 批内两两冲突
    analyses = service.analyze_batch(conn, namespace, payload.names)

    failures = [
        item
        for item in analyses
        if item["mixed_script_violation"] or item["conflicts"] or item["batch_conflicts"]
    ]
    if failures:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "error": "batch_validation_failed",
                "message": (
                    f"批次中 {len(failures)}/{len(analyses)} 个名称存在混脚本或冲突问题，"
                    "未写入任何记录"
                ),
                "items": failures,
            },
        )

    try:
        records = service.insert_batch(conn, namespace, payload.names)
    except service.BatchMixedScriptError as exc:
        # 并发策略改严：批次只读分析通过、写事务内复查命中新策略 -> 409
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "mixed_script_violation",
                "message": "并发的策略修改使批次判定失效，事务已回滚；以下为事务内复查命中混脚本策略的项",
                "items": exc.items,
            },
        )
    except service.NameConflictError:
        # 并发竞争：事务已回滚，重新分析后返回 409 冲突明细
        refreshed = service.analyze_batch(conn, namespace, payload.names)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "name_conflict",
                "message": "并发写入使批次判定失效，事务已回滚；以下为重新分析后的冲突明细",
                "items": [
                    item
                    for item in refreshed
                    if item["conflicts"] or item["batch_conflicts"]
                    or item["mixed_script_violation"]
                ],
            },
        )

    return {
        "namespace": namespace,
        "items": [
            {"index": index, **record} for index, record in enumerate(records)
        ],
    }


@app.post(
    "/namespaces/{namespace}/reservations",
    status_code=status.HTTP_201_CREATED,
    response_model=ReservationRecord,
    responses={
        409: {"description": "规范名相同或与已有名称/有效预占/释放冷却同形（confusable）"},
        422: {"description": "混脚本违规，或 ttl_seconds 超出 30–600 范围"},
    },
)
def reserve_name(
    namespace: str, payload: ReservationRequest, conn=Depends(get_db)
) -> dict:
    """预占名称：复用规范化 / 混脚本 / 同形判定，返回不可猜测 token。

    未过期、未消费的预占与已注册名称、释放冷却一样阻止后续单条注册、
    批量注册及新预占；预占记录在确认（claim）后才真正写入 display_names。
    """
    analysis = service.analyze_name(conn, namespace, payload.name)

    if analysis["mixed_script_violation"]:
        raise _mixed_script_error(analysis)

    if analysis["conflicts"]:
        raise _conflict_error(
            analysis["normalized_name"], analysis["conflicts"], CONFLICT_MESSAGE
        )

    try:
        return service.create_reservation(
            conn, namespace, payload.name, payload.ttl_seconds
        )
    except service.MixedScriptNameError as exc:
        # 并发策略改严：只读分析通过、写事务内复查命中新策略
        raise _mixed_script_error(exc.analysis)
    except service.NameConflictError as exc:
        # 并发：取写锁后复查发现冲突（含其他请求刚刚创建的有效预占）
        conflicts = exc.conflicts or service.find_conflicts(
            conn, namespace, analysis["normalized_name"]
        )
        raise _conflict_error(
            analysis["normalized_name"], conflicts, CONFLICT_MESSAGE
        )


@app.post(
    "/namespaces/{namespace}/reservations/{token}/claim",
    status_code=status.HTTP_201_CREATED,
    response_model=ClaimedNameRecord,
    responses={
        404: {"description": "token 不存在或不属于该命名空间"},
        409: {"description": "预占已被消费（重复/并发确认），或当前与已有名称/其他有效预占冲突"},
        410: {"description": "预占已过期（记录被惰性清理）"},
    },
)
def claim_reservation(namespace: str, token: str, conn=Depends(get_db)) -> dict:
    """在单个 BEGIN IMMEDIATE 事务中校验并确认预占：

    命名空间匹配 -> 有效期（过期 410）-> 消费状态（重复确认 409）
    -> 当前冲突（409，不写入）；全部通过才写 display_names 并标记已消费。
    """
    try:
        return service.claim_reservation(conn, namespace, token)
    except service.ReservationNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "reservation_not_found",
                "message": "预占不存在，或不属于该命名空间",
            },
        )
    except service.ReservationExpiredError:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={
                "error": "reservation_expired",
                "message": "预占已过期且未确认，已失效",
            },
        )
    except service.ReservationConsumedError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "reservation_already_claimed",
                "message": "该预占已确认注册，不能重复消费",
            },
        )
    except service.NameConflictError as exc:
        # 事务内复查发现冲突：未写入任何记录。回滚后重新查询保证返回最新明细
        reservation = conn.execute(
            "SELECT normalized_name FROM name_reservations WHERE token = ?",
            (token,),
        ).fetchone()
        normalized_name = reservation["normalized_name"] if reservation else ""
        conflicts = exc.conflicts or service.find_conflicts(
            conn, namespace, normalized_name
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "name_conflict",
                "message": "确认时名称与已注册名称、其他有效预占或释放冷却冲突，未写入",
                "normalized_name": normalized_name,
                "conflicts": conflicts or [],
            },
        )


@app.post(
    "/namespaces/{source}/merge",
    response_model=MergeResult,
    responses={
        409: {
            "description": "源与目标存在规范名/同形冲突（含有效预占与释放冷却），或并发写入使合并判定失效；均未修改数据"
        },
        422: {"description": "目标命名空间空白或与源命名空间相同"},
    },
)
def merge_namespace(source: str, payload: MergeRequest, conn=Depends(get_db)) -> dict:
    """合并命名空间：把 source 的全部已注册名称与预占记录迁入 target。

    在单个 BEGIN IMMEDIATE 写锁事务中，先对两侧的已注册名称、有效预占与
    未过期冷却做跨空间规范名 / 同形复查：任一冲突 -> 409 并返回双方记录，
    不修改任何数据；无冲突则在同一事务内一并迁移名称、预占与冷却并返回
    迁移数量。并发写入导致复查或唯一约束失败时整体回滚，重新检测后返回 409。
    """
    target = payload.target_namespace
    if target == source:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "error": "invalid_merge_target",
                "message": "目标命名空间与源相同，无需合并",
            },
        )

    try:
        return service.merge_namespaces(conn, source, target)
    except service.MergeConflictError as exc:
        if exc.conflicts or exc.script_violations:
            # 事务内复查发现冲突或脚本违规：直接返回明细，未修改任何数据
            conflicts = exc.conflicts
            script_violations = exc.script_violations
            if conflicts and script_violations:
                message = (
                    "源与目标命名空间存在规范名/同形冲突（含有效预占与释放冷却），"
                    "且源/目标数据存在违反目标命名空间混脚本策略的记录，"
                    "未修改任何数据"
                )
            elif conflicts:
                message = "源与目标命名空间存在规范名/同形冲突（含有效预占与释放冷却），未修改任何数据"
            else:
                message = (
                    "源/目标命名空间存在违反目标命名空间混脚本策略的已注册名称、"
                    "有效预占或释放冷却，未修改任何数据"
                )
        else:
            # 并发写入导致唯一约束/锁失败：已整体回滚，重新检测后返回明细
            conflicts = service.find_merge_conflicts(conn, source, target)
            script_violations = service.find_merge_script_violations(conn, source, target)
            message = "并发写入使合并判定失效，事务已回滚；以下为重新检测后的冲突明细"
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "merge_conflict",
                "message": message,
                "source_namespace": source,
                "target_namespace": target,
                "conflicts": conflicts,
                "script_violations": script_violations,
            },
        )


@app.post(
    "/namespaces/{namespace}/check",
    response_model=AnalysisResult,
)
def check_name(namespace: str, payload: NameRequest, conn=Depends(get_db)) -> dict:
    """只分析不落库：返回规范名、脚本分析与潜在冲突（含未过期预占）。"""
    return service.analyze_name(conn, namespace, payload.name)


@app.get(
    "/namespaces/{namespace}/names",
    response_model=list[NameRecord],
)
def list_names(namespace: str, conn=Depends(get_db)) -> list[dict]:
    return service.list_names(conn, namespace)


@app.get(
    "/namespaces/{namespace}/cooldowns",
    response_model=list[CooldownRecord],
)
def list_cooldowns(namespace: str, conn=Depends(get_db)) -> list[dict]:
    """列出命名空间内当前有效（未过期）的释放冷却记录，按 id 升序。

    只读接口：过期记录不返回，也不在此删除；它们在后续写操作（注册 /
    批量 / 预占 / 确认 / 改名 / 删除 / 合并 / 策略 PUT）的写锁事务开始时
    被惰性清理。
    """
    return service.list_cooldowns(conn, namespace)


@app.get(
    "/namespaces/{namespace}/script-policy",
    response_model=ScriptPolicy,
    responses={
        404: {"description": "该命名空间尚未配置混脚本策略（未配置时沿用内建默认策略）"},
    },
)
def get_script_policy(namespace: str, conn=Depends(get_db)) -> dict:
    """读取命名空间混脚本策略。

    未配置策略的命名空间沿用内建默认策略（Latin/Cyrillic/Greek 任意两种
    混用即违规），但本接口对未配置空间返回 404，以便客户端区分“显式
    配置”与“默认行为”，并据此以 expected_revision=0 发起首次 PUT。
    """
    try:
        return service.get_script_policy_or_404(conn, namespace)
    except service.ScriptPolicyNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "script_policy_not_found",
                "message": (
                    "该命名空间尚未配置混脚本策略，当前沿用内建默认策略"
                    "（Latin/Cyrillic/Greek 任意两种混用即违规）；"
                    "首次配置请以 expected_revision=0 调用 PUT"
                ),
            },
        )


@app.put(
    "/namespaces/{namespace}/script-policy",
    response_model=ScriptPolicy,
    responses={
        409: {
            "description":
                "expected_revision 已过期（stale_revision，附 current_revision，"
                "未配置时为 0），或新策略会使现存已注册名称/有效预占/释放冷却违规"
                "（script_policy_conflict，附逐条违规明细），策略不更新"
        },
        422: {"description": "blocked_script_pairs 含未知脚本名 / 同名对 / 项长度不为 2 / 超过上限"},
    },
)
def put_script_policy(
    namespace: str, payload: ScriptPolicyRequest, conn=Depends(get_db)
) -> dict:
    """配置命名空间混脚本策略：单个 BEGIN IMMEDIATE 事务内校验版本与影响面。

    请求体中的 blocked_script_pairs 无序且去重（服务端按字典序规范化）。
    事务内先校验 expected_revision：命名空间未配置时当前版本按 0 计，
    首次配置必须传 0；之后传上次 GET/PUT 返回的 revision，不一致 ->
    409 stale_revision（附 current_revision）。版本通过后，用新策略扫描
    该空间全部已注册名称、有效预占与未过期冷却（过期记录先惰性清理）：
    任一现存名称命中新策略中的阻断脚本对 -> 409 script_policy_conflict，
    逐条给出记录（kind 区分 registered / reservation / released）、命中
    的脚本对与码点明细，策略不更新。全部通过才写入：首次配置 revision=1，
    之后每次 PUT 成功 revision 递增。配置为空列表表示该空间不再拦截任何
    混脚本组合。
    """
    pairs = [tuple(pair) for pair in payload.blocked_script_pairs]
    try:
        return service.put_script_policy(
            conn, namespace, pairs, payload.expected_revision
        )
    except service.StalePolicyRevisionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "stale_revision",
                "message": (
                    "策略版本已过期（命名空间未配置时当前版本为 0），"
                    "请重新读取最新版本后重试"
                ),
                "current_revision": exc.current_revision,
            },
        )
    except service.ScriptPolicyConflictError as exc:
        if not exc.violations:
            # 锁竞争兜底：回滚后重读版本，提示客户端重试
            row = conn.execute(
                "SELECT revision FROM namespace_script_policies WHERE namespace = ?",
                (namespace,),
            ).fetchone()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "script_policy_conflict",
                    "message": "更新策略时发生并发竞争，事务已回滚，请重新读取最新版本后重试",
                    "current_revision": row["revision"] if row else 0,
                    "violations": [],
                },
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "script_policy_conflict",
                "message": (
                    "新策略会使命名空间内现存的已注册名称、有效预占或释放冷却"
                    "违规，策略未更新；请先改名 / 删除（等待冷却或预占过期）后重试"
                ),
                "violations": exc.violations,
            },
        )
