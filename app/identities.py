"""人员标识登记、历史别名解析与质控复核回避检查。

规则（与本轮需求一致）：
  * 质控复核须防止参与样本保管的人评价自己的案卷：复核提交在事务内以
    案卷封存版本快照中的采集人、有效事件里的移交/签收参与人、核定人
    对照复核人及其（登记在册的）别名；命中任一份项即整单拒绝，
    列出份项与命中角色，不占用复核单号。
  * 人员标识可登记历史别名：别名全局唯一——不得归属多人，也不得转移归属
    （别名行只追加，不更新、不删除、不复用）。
  * 增补别名须携带所见修订号（expected_revision），与当前修订号不一致即拒绝
    （乐观并发控制）；修订号在创建人员时为 1，每次增补别名成功 +1。
  * 未登记姓名按原字符串比较：未在别名册中的名字只与相等的原字符串同一；
    两人互异及第三人资格按本次提交时的别名归属判定（不追溯）。
  * 成功的复核/裁决意见固化提交当时使用的身份归属（人员标识、命中等名），
    供查询；后续增补别名不追溯改变已提交意见，旧意见不伪造身份记录
    （旧库迁移而来的复核行身份列为 NULL，仍以原字符串展示）。
"""
import sqlite3
from dataclasses import dataclass
from typing import Any

from .db import utcnow_iso
from .schemas import AliasAddIn, PersonIn
from .service import DomainError

# 封存快照中参与样本保管、需要回避的角色（仅有效事件；核定人为核定入库操作人）
ROLE_COLLECTOR = "collector"
ROLE_TRANSFER_FROM = "transfer_from"
ROLE_TRANSFER_TO = "transfer_to"
ROLE_RECEIVER = "receiver"
ROLE_APPROVER = "approver"

_ROLE_LABELS = {
    ROLE_COLLECTOR: "采集人",
    ROLE_TRANSFER_FROM: "移交交出人",
    ROLE_TRANSFER_TO: "移交接收人",
    ROLE_RECEIVER: "签收人",
    ROLE_APPROVER: "核定人",
}


# --------------------------------------------------------------------------- 人员登记 / 别名

def _get_person_by_id(conn: sqlite3.Connection, person_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM person WHERE person_id = ?", (person_id,)).fetchone()


def _get_alias(conn: sqlite3.Connection, alias: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM person_alias WHERE alias = ?", (alias,)).fetchone()


def _alias_rows(conn: sqlite3.Connection, person_pk: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM person_alias WHERE person_pk = ? ORDER BY id ASC", (person_pk,)
    ).fetchall()


def _person_view(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    aliases = [
        {"alias": a["alias"], "is_primary": bool(a["is_primary"]), "created_at": a["created_at"]}
        for a in _alias_rows(conn, row["id"])
    ]
    return {
        "person_id": row["person_id"],
        "revision": row["revision"],
        "aliases": aliases,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def register_person(conn: sqlite3.Connection, data: PersonIn) -> tuple[dict[str, Any], bool]:
    """登记人员标识与初始别名。返回 (人员视图, 是否幂等重试)。

    同 person_id 重试：别名集合相同 → 200 幂等；任何不同 → 409 person_conflict。
    任一起始别名已归属他人 → 409 alias_conflict（不创建人员、不占用别名）。
    """
    aliases = list(dict.fromkeys(data.aliases))           # 去重保序
    existing = _get_person_by_id(conn, data.person_id)
    if existing is not None:
        current = [a["alias"] for a in _alias_rows(conn, existing["id"])]
        if set(current) == set(aliases):
            return _person_view(conn, existing), True
        raise DomainError(
            409,
            "person_conflict",
            f"人员标识 {data.person_id} 已登记但提交的别名集合与库存不一致（人员标识不可改、别名不可转移）",
            {"person_id": data.person_id, "existing_aliases": current, "incoming_aliases": aliases},
        )

    # 别名不得归属多人（在插入前统一拦截，失败不留任何人员/别名记录）
    taken = []
    for name in aliases:
        row = _get_alias(conn, name)
        if row is not None:
            owner = conn.execute("SELECT person_id FROM person WHERE id = ?", (row["person_pk"],)).fetchone()
            taken.append({"alias": name, "owner_person_id": owner["person_id"] if owner else None})
    if taken:
        raise DomainError(
            409,
            "alias_conflict",
            "别名不得归属多人：提交的别名中存在已登记归属他人的别名，整单拒绝",
            {"person_id": data.person_id, "aliases": taken},
        )

    created_at = utcnow_iso()
    try:
        pc = conn.execute(
            "INSERT INTO person (person_id, revision, created_at, updated_at) VALUES (?,?,?,?)",
            (data.person_id, 1, created_at, created_at),
        )
    except sqlite3.IntegrityError:
        # 并发下人员标识竞争：重读后按幂等/冲突处理
        existing = _get_person_by_id(conn, data.person_id)
        if existing is not None:
            current = [a["alias"] for a in _alias_rows(conn, existing["id"])]
            if set(current) == set(aliases):
                return _person_view(conn, existing), True
        raise DomainError(409, "person_conflict", f"人员标识 {data.person_id} 并发登记冲突")
    person_pk = pc.lastrowid
    for idx, name in enumerate(aliases):
        try:
            conn.execute(
                "INSERT INTO person_alias (person_pk, alias, is_primary, created_at)"
                " VALUES (?,?,?,?)",
                (person_pk, name, 1 if idx == 0 else 0, created_at),
            )
        except sqlite3.IntegrityError:
            raise DomainError(
                409,
                "alias_conflict",
                f"别名 {name} 并发下已被他人登记（别名不得归属多人），整单回滚",
                {"alias": name},
            )
    row = conn.execute("SELECT * FROM person WHERE id = ?", (person_pk,)).fetchone()
    return _person_view(conn, row), False


def add_alias(conn: sqlite3.Connection, person_id: str, data: AliasAddIn) -> dict[str, Any]:
    """为已登记人员增补历史别名。

    必须携带所见修订号 expected_revision：与当前修订号不一致（过期/并发已改）
    → 409 person_revision_stale；别名已归属（含本人）→ 409 alias_conflict
    （别名不得归属多人或转移归属；重复增补同名别名也以此拒绝，别名册只追加新名字）。
    成功后修订号 +1。
    """
    person = _get_person_by_id(conn, person_id)
    if person is None:
        raise DomainError(404, "person_not_found", f"人员不存在: {person_id}")
    if data.expected_revision != person["revision"]:
        raise DomainError(
            409,
            "person_revision_stale",
            f"增补别名被拒绝：携带的修订号 {data.expected_revision} 已过期，"
            f"人员 {person_id} 当前修订号为 {person['revision']}（请重新读取后重试）",
            {"person_id": person_id,
             "expected_revision": data.expected_revision,
             "current_revision": person["revision"]},
        )

    alias_row = _get_alias(conn, data.alias)
    if alias_row is not None:
        owner = conn.execute("SELECT person_id FROM person WHERE id = ?", (alias_row["person_pk"],)).fetchone()
        same_owner = owner is not None and owner["person_id"] == person_id
        raise DomainError(
            409,
            "alias_conflict",
            f"别名 {data.alias} 已登记归属{('该人员本人（别名不可重复增补）' if same_owner else '其他人员')}，"
            "别名不得归属多人或转移归属",
            {"alias": data.alias, "owner_person_id": owner["person_id"] if owner else None,
             "same_person": same_owner},
        )

    created_at = utcnow_iso()
    try:
        conn.execute(
            "INSERT INTO person_alias (person_pk, alias, is_primary, created_at)"
            " VALUES (?, ?, 0, ?)",
            (person["id"], data.alias, created_at),
        )
    except sqlite3.IntegrityError:
        raise DomainError(
            409,
            "alias_conflict",
            f"别名 {data.alias} 并发下已被登记（别名不得归属多人或转移归属），整单回滚",
            {"alias": data.alias},
        )
    # 条件更新做乐观锁兜底：并发增补仅一次成功，落败方看到新修订号后重试
    cur = conn.execute(
        "UPDATE person SET revision = revision + 1, updated_at = ?"
        " WHERE id = ? AND revision = ?",
        (created_at, person["id"], data.expected_revision),
    )
    if cur.rowcount == 0:
        raise DomainError(
            409,
            "person_revision_stale",
            f"人员 {person_id} 在本事务前已被并发增补别名，修订号过期，请重新读取后重试",
            {"person_id": person_id, "expected_revision": data.expected_revision},
        )
    row = conn.execute("SELECT * FROM person WHERE id = ?", (person["id"],)).fetchone()
    return _person_view(conn, row)


def list_persons(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute("SELECT * FROM person ORDER BY id ASC").fetchall()
    return {"total": len(rows), "items": [_person_view(conn, r) for r in rows]}


def get_person(conn: sqlite3.Connection, person_id: str) -> dict[str, Any]:
    person = _get_person_by_id(conn, person_id)
    if person is None:
        raise DomainError(404, "person_not_found", f"人员不存在: {person_id}")
    return _person_view(conn, person)


# --------------------------------------------------------------------------- 身份解析

@dataclass(frozen=True)
class Identity:
    """一个姓名在当前别名册中的归属解析结果（按提交时的别名归属快照判定）。"""

    name: str
    person_pk: int | None = None
    person_id: str | None = None
    matched_alias: str | None = None

    @property
    def registered(self) -> bool:
        return self.person_pk is not None

    def same_person(self, other: "Identity") -> bool:
        """是否同一人：已登记按归属人员判定；未登记姓名按原字符串比较。"""
        if self.registered and other.registered:
            return self.person_pk == other.person_pk
        return self.name == other.name

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "person_id": self.person_id,
            "registered": self.registered,
            "matched_alias": self.matched_alias,
        }


def alias_map(conn: sqlite3.Connection) -> dict[str, Identity]:
    """读取当前全量别名归属（别名 → 身份）。在提交事务内读取，保证与判定同一视图。"""
    mapping: dict[str, Identity] = {}
    rows = conn.execute(
        "SELECT pa.alias AS alias, pa.person_pk AS person_pk, p.person_id AS person_id"
        " FROM person_alias pa JOIN person p ON p.id = pa.person_pk"
    ).fetchall()
    for r in rows:
        mapping[r["alias"]] = Identity(r["alias"], r["person_pk"], r["person_id"], r["alias"])
    return mapping


def resolve_name(name: str, aliases: dict[str, Identity]) -> Identity:
    """按当前别名归属解析姓名；未登记姓名只保留原字符串（person_pk=None）。"""
    return aliases.get(name, Identity(name))


def resolve_identities(conn: sqlite3.Connection, names: list[str]) -> dict[str, Any]:
    """解析接口：每个姓名给出当前归属（未登记按原字符串，registered=false）。"""
    aliases = alias_map(conn)
    items = []
    for name in names:
        ident = resolve_name(name, aliases)
        items.append(ident.as_dict())
    return {"total": len(items), "items": items}


# --------------------------------------------------------------------------- 封存快照中的参与人

def snapshot_participants(snapshot: dict[str, Any]) -> list[dict[str, str]]:
    """提取一份封存版本快照中的样本保管参与人（份项回避检查范围）。

    范围严格按需求：采集人、封存版本的【有效事件】中的移交交出/接收人与签收人、
    核定人。storage 的 custodian（入库保管人，通常为“实验室”）与封签检查不在
    保管交接参与人之列；快照 timeline 只含有效事件，故此处天然不含已作废/替换事件。
    同一姓名/角色可能在多场移交重复出现，调用方按 (角色,事件号,姓名) 去重展示。
    """
    found: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    def add(role: str, name: str | None, ref: str) -> None:
        if not name or not name.strip():
            return
        name = name.strip()
        key = (role, ref, name)
        if key not in seen:
            seen.add(key)
            found.append({"role": role, "role_label": _ROLE_LABELS[role], "name": name, "ref": ref})

    sample = snapshot.get("sample") or {}
    add(ROLE_COLLECTOR, sample.get("collector"), "sample")

    for ev in snapshot.get("timeline") or []:
        payload = ev.get("payload") or {}
        ref = ev.get("event_id") or ""
        etype = ev.get("event_type")
        if etype == "transfer":
            add(ROLE_TRANSFER_FROM, payload.get("from_custodian"), ref)
            add(ROLE_TRANSFER_TO, payload.get("to_custodian"), ref)
        elif etype == "receive":
            add(ROLE_RECEIVER, payload.get("receiver"), ref)

    approval = snapshot.get("approval")
    if approval and snapshot.get("approved_storage_at"):
        add(ROLE_APPROVER, approval.get("approver"), "approval")
    return found


def conflict_of_interest_hits(
    reviewer: Identity, aliases: dict[str, Identity], snapshots: dict[int, dict[str, Any]]
) -> list[dict[str, Any]]:
    """对给定份项快照集合做回避检查，返回命中明细（空=无回避冲突）。

    命中条件（未登记姓名按原字符串比较；已登记按别名册归属，任一别名即视为同一人）：
    复核人（按其当前全部别名归属）等于份项中的任一保管参与人。
    snapshots 以封存清单 item_index 为键。
    """
    if reviewer.registered:
        # 该人员当前全部别名都代表同一人；同时并入提交原字符串做防御性兜底。
        reviewer_names = {
            alias for alias, ident in aliases.items() if ident.person_pk == reviewer.person_pk
        }
        reviewer_names.add(reviewer.name)
    else:
        reviewer_names = {reviewer.name}

    hits: list[dict[str, Any]] = []
    for item_index in sorted(snapshots):
        for p in snapshot_participants(snapshots[item_index]):
            if p["name"] in reviewer_names:
                hits.append({
                    "index": item_index,
                    "sample_code": snapshots[item_index].get("sample", {}).get("sample_code"),
                    "role": p["role"],
                    "role_label": p["role_label"],
                    "name": p["name"],
                    "event_id": None if p["ref"] in ("sample", "approval") else p["ref"],
                })
    return hits
