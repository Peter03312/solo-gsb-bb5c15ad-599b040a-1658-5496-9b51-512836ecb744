"""端到端自测：人员标识/历史别名登记 + 质控复核回避（本轮新增）。

覆盖：
  * 人员登记：起始别名、person_id 幂等（同别名集合 200）/异内容冲突 409；
    别名归属多人拒绝（alias_conflict）、空别名字段校验；
  * 增补别名：携带所见修订号成功且 revision +1；过期修订号 person_revision_stale；
    别名归属他人 / 本人重复增补 / 转移归属 → alias_conflict；人员不存在 404；
  * 解析：已登记别名解析到 person_id；未登记姓名 registered=false；
  * 回避检查：封存快照中的采集人 / 有效事件移交交出接收人 / 签收人 / 核定人
    对照复核人及其别名；命中任一份项 → 422 review_conflict_of_interest，
    列出份项(index/sample_code/role/name)、整单不留记录、不占用复核单号；
    未命中（仅封签检查人/入库保管人身份）可正常提交；未登记姓名按原字符串比较；
    第三人裁决仅按分歧份项（非分歧份命中不拦，分歧份命中即拦）；
  * 两人互异 / 第三人资格：按本次提交时的别名归属判定——后增补别名可识别同一人；
  * 身份固化：成功意见记录提交当时身份归属；后续增补别名不追溯改变已提交意见，
    旧意见不伪造身份记录（person_id 仍为旧归属 / 未登记为 null）。

运行：python -m tests.test_persons
"""
import os
import tempfile
import threading

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test_persons.db")
os.environ["SEED_ON_START"] = "false"

from fastapi.testclient import TestClient  # noqa: E402

from app.db import db_session  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)
client.__enter__()  # 触发 lifespan（建表/启动钩子）
API = "/api/v1"
PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {detail}")


def err(r):
    return r.json().get("error", {})


def db_scalar(sql, args=()):
    with db_session() as conn:
        return conn.execute(sql, args).fetchone()[0]


# ---------------------------------------------------------------- 工具：造链与案卷

def valid_chain(code, collector, to_custodian, receiver=None, approver=None,
                storage_custodian="实验室", approve=True):
    """登记并补齐 移交→签收→检查→入库（可指定核定人；版本 0..4，核定后 5）。

    有效链要求签收人=移交接收人，故 receiver 默认等于 to_custodian。
    """
    seal = f"SEAL-{code}"
    receiver = receiver or to_custodian
    r = client.post(f"{API}/samples", json={
        "sample_code": code, "collected_at": "2026-09-30T01:00:00Z",
        "collector": collector, "seal_no": seal})
    assert r.status_code == 201, r.text
    for payload in [
        {"event_id": f"{code}-T1", "event_type": "transfer", "sample_code": code,
         "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": f"TR-{code}",
         "from_custodian": collector, "to_custodian": to_custodian},
        {"event_id": f"{code}-R1", "event_type": "receive", "sample_code": code,
         "occurred_at": "2026-09-30T04:00:00Z", "transfer_no": f"TR-{code}",
         "receiver": receiver, "seal_no": seal},
        {"event_id": f"{code}-C1", "event_type": "seal_check", "sample_code": code,
         "occurred_at": "2026-09-30T04:30:00Z", "checked_seal_no": seal, "seal_intact": True},
        {"event_id": f"{code}-S1", "event_type": "storage", "sample_code": code,
         "occurred_at": "2026-09-30T05:00:00Z", "custodian": storage_custodian},
    ]:
        rr = client.post(f"{API}/events", json=payload)
        assert rr.status_code == 201, rr.text
    if approve or approver is not None:
        rr = client.post(f"{API}/samples/{code}/approve",
                         json={"approver": approver or "默认主管", "note": "准予入库"})
        assert rr.status_code == 200, rr.text
    return seal


def make_dossier(did, codes):
    r = client.post(f"{API}/dossiers", json={
        "dossier_id": did,
        "items": [{"sample_code": c} for c in sorted(codes)]})
    assert r.status_code == 201, r.text
    return r.json()["dossier"]["digest"]


def submit(did, review_no, reviewer, digest, items):
    return client.post(f"{API}/dossiers/{did}/reviews", json={
        "review_no": review_no, "reviewer": reviewer,
        "expected_digest": digest, "items": items})


def op(code, passed=True, reason=None):
    return {"sample_code": code, "passed": passed, "reason": reason}


def all_pass(codes):
    return [op(c, True) for c in sorted(codes)]


def progress(did):
    return client.get(f"{API}/dossiers/{did}/reviews").json()


# ---------------------------------------------------------------- 1. 人员登记

print("1) 人员标识登记：起始别名、修订号、幂等/冲突")
r = client.post(f"{API}/persons", json={"person_id": "EMP-001", "aliases": ["王采集", "王工"]})
check("登记 201，revision=1", r.status_code == 201 and r.json()["person"]["revision"] == 1, r.text)
check("首个别名 is_primary=true", r.json()["person"]["aliases"][0]["is_primary"] is True, r.text)
check("起始别名自动去重",
      client.post(f"{API}/persons", json={"person_id": "EMP-DUP", "aliases": ["甲", "甲", "甲一"]}).status_code == 201)

r = client.post(f"{API}/persons", json={"person_id": "EMP-001", "aliases": ["王工", "王采集"]})
check("同 person_id 同别名集合（换序）重试 200 幂等",
      r.status_code == 200 and r.json()["idempotent"] is True, r.text)
r = client.post(f"{API}/persons", json={"person_id": "EMP-001", "aliases": ["王采集"]})
check("同 person_id 异别名集合 409 person_conflict",
      r.status_code == 409 and err(r)["code"] == "person_conflict", r.text)

r = client.post(f"{API}/persons", json={"person_id": "EMP-002", "aliases": ["王采集"]})
check("别名归属多人 409 alias_conflict（不创建人员）",
      r.status_code == 409 and err(r)["code"] == "alias_conflict"
      and err(r)["details"]["aliases"][0]["owner_person_id"] == "EMP-001", r.text)
check("冲突后人员不存在、别名未被占用给新人",
      client.get(f"{API}/persons/EMP-002").status_code == 404)

r = client.post(f"{API}/persons", json={"person_id": "EMP-X", "aliases": []})
check("空别名列表 422（字段校验）", r.status_code == 422, r.text)
r = client.post(f"{API}/persons", json={"person_id": "EMP-X", "aliases": ["  "]})
check("空白别名 422（字段校验）", r.status_code == 422, r.text)

# 其他参与人
for pid, aliases in [
    ("EMP-003", ["李运输"]), ("EMP-004", ["周签收"]), ("EMP-005", ["赵主管"]),
    ("EMP-100", ["质控甲", "QC-A"]), ("EMP-200", ["质控乙"]), ("EMP-300", ["质控丙"]),
]:
    assert client.post(f"{API}/persons", json={"person_id": pid, "aliases": aliases}).status_code == 201

r = client.get(f"{API}/persons")
check("人员列表", r.status_code == 200 and r.json()["total"] >= 7, r.text)
r = client.get(f"{API}/persons/EMP-001")
check("单人查询返回别名册", r.status_code == 200 and
      {a["alias"] for a in r.json()["person"]["aliases"]} == {"王采集", "王工"}, r.text)
check("不存在人员 404 person_not_found",
      client.get(f"{API}/persons/NOBODY").status_code == 404)

# ---------------------------------------------------------------- 2. 增补别名 + 修订号

print("2) 增补历史别名：乐观修订号、别名不可转移/复用")
r = client.post(f"{API}/persons/EMP-002/aliases",
                json={"alias": "王采集", "expected_revision": 1})
check("不存在人员增补 404", r.status_code == 404 and err(r)["code"] == "person_not_found", r.text)

r = client.post(f"{API}/persons/EMP-001/aliases",
                json={"alias": "王采集旧名", "expected_revision": 99})
check("过期修订号 409 person_revision_stale（详情给两侧修订号）",
      r.status_code == 409 and err(r)["code"] == "person_revision_stale"
      and err(r)["details"]["current_revision"] == 1
      and err(r)["details"]["expected_revision"] == 99, r.text)

r = client.post(f"{API}/persons/EMP-001/aliases",
                json={"alias": "王采集", "expected_revision": 1})
check("别名本人重复增补 409 alias_conflict",
      r.status_code == 409 and err(r)["code"] == "alias_conflict"
      and err(r)["details"]["same_person"] is True, r.text)

r = client.post(f"{API}/persons/EMP-003/aliases",
                json={"alias": "王工", "expected_revision": 1})
check("别名已归属他人 409 alias_conflict（不得转移归属）",
      r.status_code == 409 and err(r)["code"] == "alias_conflict"
      and err(r)["details"]["owner_person_id"] == "EMP-001", r.text)

r = client.post(f"{API}/persons/EMP-001/aliases",
                json={"alias": "王采集旧名", "expected_revision": 1})
check("正确修订号增补成功 200，revision=2",
      r.status_code == 200 and r.json()["person"]["revision"] == 2, r.text)
check("新别名出现在别名册（追加、保留旧别名）",
      {a["alias"] for a in r.json()["person"]["aliases"]}
      == {"王采集", "王工", "王采集旧名"}, r.text)

r = client.post(f"{API}/persons/EMP-001/aliases",
                json={"alias": "更早名字", "expected_revision": 1})
check("用过的修订号再次增补 → 409 person_revision_stale",
      r.status_code == 409 and err(r)["code"] == "person_revision_stale", r.text)

# ---------------------------------------------------------------- 3. 身份解析

print("3) 身份解析：别名命中 / 未登记按原字符串")
r = client.get(f"{API}/identities/resolve", params=[("names", "王采集旧名"), ("names", "谁也不是")])
items = {it["name"]: it for it in r.json()["items"]}
check("已登记别名解析到 person_id 与命中等名",
      items["王采集旧名"]["person_id"] == "EMP-001"
      and items["王采集旧名"]["matched_alias"] == "王采集旧名"
      and items["王采集旧名"]["registered"] is True, r.text)
check("未登记姓名 registered=false / person_id=null",
      items["谁也不是"]["registered"] is False and items["谁也不是"]["person_id"] is None, r.text)

# ---------------------------------------------------------------- 4. 回避：采集人命中

print("4) 回避检查：采集人（含别名）命中整单拒绝、不留记录、不占单号")
valid_chain("COI-1", "王采集", "李运输")
valid_chain("COI-2", "赵采集", "钱接收")
DIG = make_dossier("DOS-COI-1", ["COI-1", "COI-2"])

r = submit("DOS-COI-1", "REV-C1", "王采集旧名", DIG, all_pass(["COI-1", "COI-2"]))
check("采集人别名命中 → 422 review_conflict_of_interest",
      r.status_code == 422 and err(r)["code"] == "review_conflict_of_interest", r.text)
hits = err(r)["details"]["items"]
check("命中明细列出份项 index/sample_code/role/name（采集人=移交交出人，collector+transfer_from 两角色）",
      len(hits) == 2 and hits[0]["index"] == 0 and hits[0]["sample_code"] == "COI-1"
      and {(h["role"], h["name"], h["event_id"]) for h in hits}
      == {("collector", "王采集", None), ("transfer_from", "王采集", "COI-1-T1")}, r.text)
check("命中详情含复核人身份与检查范围",
      err(r)["details"]["reviewer"]["person_id"] == "EMP-001"
      and err(r)["details"]["scope"] == "all_items", r.text)
check("回避拒绝不留复核案",
      db_scalar("SELECT COUNT(*) FROM review_case rc JOIN dossier d ON rc.dossier_pk=d.id"
                " WHERE d.dossier_id='DOS-COI-1'") == 0)

# 不占用复核单号：换一个无利害关系人、用相同复核单号可成功
r = submit("DOS-COI-1", "REV-C1", "质控甲", DIG, all_pass(["COI-1", "COI-2"]))
check("同一复核单号在回避失败后可被合规复核人使用（不占单号）",
      r.status_code == 201, r.text)

# 第二份：采集人本人之外，其另一别名同样命中（整单拒绝，即使只命中一份）
r = submit("DOS-COI-1", "REV-C2", "王工", DIG, all_pass(["COI-1", "COI-2"]))
check("复核人用另一别名（王工）同样命中采集人 → 422",
      r.status_code == 422 and err(r)["code"] == "review_conflict_of_interest", r.text)

# 未登记姓名按原字符串比较：与采集人原字符串相同才命中
r = submit("DOS-COI-1", "REV-C3", "王采集", DIG, all_pass(["COI-1", "COI-2"]))
check("未登记？（王采集已登记 EMP-001）命中 → 422",
      r.status_code == 422 and err(r)["code"] == "review_conflict_of_interest", r.text)
r = submit("DOS-COI-1", "REV-C4", "王采", DIG, all_pass(["COI-1", "COI-2"]))
check("原字符串不相等且未登记别名 → 可提交（seq=2）",
      r.status_code == 201 and r.json()["submission"]["seq"] == 2, r.text)
check("无分歧两份通过 → 阶段 completed", progress("DOS-COI-1")["stage"] == "completed")

# ---------------------------------------------------------------- 5. 回避：移交/签收/核定人

print("5) 回避检查：有效事件移交交出/接收人、签收人、核定人命中")
# COI-3：移交 孙采集A→李运输，签收人 李运输，核定人 赵主管
valid_chain("COI-3", "孙采集A", "李运输", approver="赵主管")
valid_chain("COI-4", "孙采集B", "吴运输", approve=False)
DIG2 = make_dossier("DOS-COI-2", ["COI-3", "COI-4"])

r = submit("DOS-COI-2", "REV-D1", "李运输", DIG2, all_pass(["COI-3", "COI-4"]))
hits = err(r)["details"]["items"]
check("移交接收人/签收人（李运输）命中 transfer_to + receiver，均带 event_id",
      r.status_code == 422 and
      {(h["sample_code"], h["role"], h["name"], h["event_id"]) for h in hits}
      == {("COI-3", "transfer_to", "李运输", "COI-3-T1"),
          ("COI-3", "receiver", "李运输", "COI-3-R1")}, r.text)

r = submit("DOS-COI-2", "REV-D2", "赵主管", DIG2, all_pass(["COI-3", "COI-4"]))
hits = err(r)["details"]["items"]
check("核定人命中 approver",
      r.status_code == 422 and
      {(h["sample_code"], h["role"], h["name"]) for h in hits}
      == {("COI-3", "approver", "赵主管")}, r.text)

# 仅身份是入库保管人“实验室”或封签检查（无人员字段）不构成回避
r = submit("DOS-COI-2", "REV-D4", "实验室", DIG2, all_pass(["COI-3", "COI-4"]))
check("入库保管人名称不在回避范围 → 第一份可提交 201", r.status_code == 201, r.text)
r = submit("DOS-COI-2", "REV-D5", "质控甲", DIG2, all_pass(["COI-3", "COI-4"]))
check("无利害复核人第二份 → 201 完成", r.status_code == 201, r.text)

# ---------------------------------------------------------------- 6. 作废事件不参与回避（只看封存版本有效事件）

print("6) 回避只依据封存版本的有效事件：已作废移交的交出人不命中")
# 新样本：移交-签收链中把唯一的 transfer 作废（封存最新版本），复核人=该交出人
code = "COI-5"
valid_chain(code, "冯采集", "陈运输", approve=False)
# 把陈运输参与的移交与签收一并作废：封存最新版本有效事件中不再有其任何角色
rr = client.post(f"{API}/corrections", json={
    "correction_id": "FIX-VOID-T", "sample_code": code,
    "target_event_id": f"{code}-T1", "action": "void",
    "operator": "质控员", "reason": "移交记录作废"})
assert rr.status_code == 201, rr.text
rr = client.post(f"{API}/corrections", json={
    "correction_id": "FIX-VOID-R", "sample_code": code,
    "target_event_id": f"{code}-R1", "action": "void",
    "operator": "质控员", "reason": "签收随移交一并作废"})
assert rr.status_code == 201, rr.text
make_dossier("DOS-COI-3", [code, "COI-1"])
r = client.get(f"{API}/dossiers/DOS-COI-3")
dig3 = r.json()["dossier"]["digest"]
r = submit("DOS-COI-3", "REV-E1", "陈运输", dig3, all_pass([code, "COI-1"]))
check("作废移交的交出人不在封存有效事件中 → 不命中（但 COI-1 的采集人是王采集？此处应通过）",
      r.status_code == 201, r.text)

# ---------------------------------------------------------------- 7. 第三人裁决仅按分歧份项

print("7) 第三人回避范围：仅按分歧份项；资格按当前别名归属")
# COI-3（孙采集A/李运输/周签收/赵主管）与 COI-4（孙采集B/吴运输）
DIG4 = make_dossier("DOS-COI-4", ["COI-3", "COI-4"])
# 两名无利害复核人：COI-3 通过、COI-4 不通过 vs 都通过 → COI-4 分歧
r = submit("DOS-COI-4", "REV-F1", "质控甲", DIG4,
           [op("COI-3", True), op("COI-4", False, "链有疑问")])
assert r.status_code == 201, r.text
r = submit("DOS-COI-4", "REV-F2", "质控乙", DIG4, all_pass(["COI-3", "COI-4"]))
assert r.status_code == 201, r.text
p = progress("DOS-COI-4")
check("仅 COI-4 分歧待裁决", p["disputed_samples"] == ["COI-4"], str(p["disputed_samples"]))

# 裁决人只参与过 COI-3 的保管（李运输在 COI-3 移交、不在 COI-4）：
# 分歧份项只有 COI-4 → 不命中，裁决可提交
r = submit("DOS-COI-4", "REV-F3", "李运输", DIG4,
           [op("COI-4", False, "裁决维持不通过")])
check("第三人命中的仅是非分歧份项 COI-3 → 不拦截，裁决 201", r.status_code == 201, r.text)

# 再造一个分歧卷：分歧份项为 COI-3，则李运输必须被拦
DIG5 = make_dossier("DOS-COI-5", ["COI-3", "COI-4"])
r = submit("DOS-COI-5", "REV-G1", "质控甲", DIG5,
           [op("COI-3", False, "有问题"), op("COI-4", True)])
assert r.status_code == 201, r.text
r = submit("DOS-COI-5", "REV-G2", "质控乙", DIG5, all_pass(["COI-3", "COI-4"]))
assert r.status_code == 201, r.text
r = submit("DOS-COI-5", "REV-G3", "李运输", DIG5,
           [op("COI-3", False, "裁决不通过")])
check("第三人裁决命中分歧份项 COI-3 的 transfer_to → 422（scope=disputed_items）",
      r.status_code == 422 and err(r)["code"] == "review_conflict_of_interest"
      and err(r)["details"]["scope"] == "disputed_items"
      and {h["sample_code"] for h in err(r)["details"]["items"]} == {"COI-3"}, r.text)
check("回避失败不占复核单号（REV-G3 可由合规第三人使用）",
      submit("DOS-COI-5", "REV-G3", "质控丙", DIG5,
             [op("COI-3", False, "裁决不通过")]).status_code == 201)

# ---------------------------------------------------------------- 8. 两人互异/第三人资格按当前别名归属

print("8) 同一人识别按提交时别名归属：增补别名后被识别为重复复核人")
DIG6 = make_dossier("DOS-COI-6", ["COI-3", "COI-4"])
# EMP-200 质控乙 先以原名提交第一份（故意与第二人产生 COI-4 分歧，使案卷停在待裁决阶段）
r = submit("DOS-COI-6", "REV-H1", "质控乙", DIG6,
           [op("COI-3", True), op("COI-4", False, "链有疑问")])
assert r.status_code == 201, r.text
# 为 EMP-200 增补一个新别名
rev = client.get(f"{API}/persons/EMP-200").json()["person"]["revision"]
r = client.post(f"{API}/persons/EMP-200/aliases",
                json={"alias": "乙的新工号", "expected_revision": rev})
assert r.status_code == 200, r.text
# 用新别名提交第二份 → duplicate_reviewer（资格按当前别名归属）
r = submit("DOS-COI-6", "REV-H2", "乙的新工号", DIG6, all_pass(["COI-3", "COI-4"]))
check("后增补的别名识别为同一复核人 → 409 duplicate_reviewer",
      r.status_code == 409 and err(r)["code"] == "duplicate_reviewer"
      and err(r)["details"]["prior_reviewer"]["person_id"] == "EMP-200", r.text)
# 未登记的新名字（与前序原字符串不等、不归属已登记人员）可作为第二人
r = submit("DOS-COI-6", "REV-H3", "外部专家X", DIG6, all_pass(["COI-3", "COI-4"]))
check("未登记且原字符串不同的姓名视为另一人 → 第二份 201（COI-4 进入分歧）",
      r.status_code == 201 and progress("DOS-COI-6")["disputed_samples"] == ["COI-4"], r.text)

# 第三人资格：前两名任一人用新旧别名裁决都被拦（案卷此时待裁决，先判资格）
r = submit("DOS-COI-6", "REV-H4", "乙的新工号", DIG6, [op("COI-4", True)])
check("第三人是前两名之一（新别名）→ 409 duplicate_reviewer",
      r.status_code == 409 and err(r)["code"] == "duplicate_reviewer", r.text)

# 未登记姓名的同一人：同原字符串第二份直接 409
DIG7 = make_dossier("DOS-COI-7", ["COI-3", "COI-4"])
assert submit("DOS-COI-7", "REV-I1", "路人甲", DIG7, all_pass(["COI-3", "COI-4"])).status_code == 201
r = submit("DOS-COI-7", "REV-I2", "路人甲", DIG7, all_pass(["COI-3", "COI-4"]))
check("未登记姓名原字符串相同 → 409 duplicate_reviewer",
      r.status_code == 409 and err(r)["code"] == "duplicate_reviewer", r.text)

# ---------------------------------------------------------------- 9. 身份固化与不追溯

print("9) 成功意见固化当时身份归属；增补别名不追溯、旧意见不伪造身份")
DIG8 = make_dossier("DOS-COI-8", ["COI-3", "COI-4"])
# 以一个尚未登记的名字提交
r = submit("DOS-COI-8", "REV-J1", "当时未登记的名字", DIG8, all_pass(["COI-3", "COI-4"]))
assert r.status_code == 201, r.text
ident = r.json()["submission"]["reviewer_identity"]
check("提交时未登记 → person_id=null / matched_alias=null（不伪造身份）",
      ident == {"name": "当时未登记的名字", "person_id": None,
                "registered": False, "matched_alias": None}, str(ident))

# 事后把这个名字登记为某人员的别名（新人员，起始别名）
r = client.post(f"{API}/persons", json={"person_id": "EMP-999", "aliases": ["当时未登记的名字"]})
assert r.status_code == 201, r.text
p = progress("DOS-COI-8")
rv1 = p["reviewers"][0]
opinion_person = p["items"][0]["opinions"][0]["reviewer_identity"]
check("事后登记别名不追溯改写已提交意见（仍 registered=false）",
      rv1["reviewer_identity"]["registered"] is False
      and opinion_person["registered"] is False
      and opinion_person["person_id"] is None, str(p))

# 已登记人员提交：固化 person_id 与命中别名；之后增补别名，固化记录不变
r = submit("DOS-COI-8", "REV-J2", "质控丙", DIG8, all_pass(["COI-3", "COI-4"]))
assert r.status_code == 201, r.text
ident2 = r.json()["submission"]["reviewer_identity"]
check("已登记复核人固化 person_id=EMP-300 与命中别名 质控丙",
      ident2["person_id"] == "EMP-300" and ident2["matched_alias"] == "质控丙", str(ident2))
rev = client.get(f"{API}/persons/EMP-300").json()["person"]["revision"]
assert client.post(f"{API}/persons/EMP-300/aliases",
                   json={"alias": "丙的补充别名", "expected_revision": rev}).status_code == 200
p = progress("DOS-COI-8")
rv = next(x for x in p["reviewers"] if x["review_no"] == "REV-J2")
check("后续增补别名不改变已提交意见的身份记录",
      rv["reviewer_identity"]["matched_alias"] == "质控丙"
      and rv["reviewer_identity"]["person_id"] == "EMP-300", str(rv))

# ---------------------------------------------------------------- 10. 复核单号/幂等规则保持

print("10) 幂等与单号规则保持：同单号同内容 200 返回原固化身份；异内容 409")
r = submit("DOS-COI-8", "REV-J2", "质控丙", DIG8, all_pass(["COI-3", "COI-4"]))
check("同复核单号同内容重试 200 幂等", r.status_code == 200 and r.json()["idempotent"] is True, r.text)
check("幂等返回原始身份归属",
      r.json()["submission"]["reviewer_identity"]["person_id"] == "EMP-300", r.text)
r = submit("DOS-COI-8", "REV-J2", "质控丙", DIG8,
           [op("COI-3", False, "改意见"), op("COI-4", True)])
check("同复核单号异内容 409 review_conflict",
      r.status_code == 409 and err(r)["code"] == "review_conflict", r.text)
# 跨案卷占用单号
r = submit("DOS-COI-1", "REV-J2", "质控甲",
           client.get(f"{API}/dossiers/DOS-COI-1").json()["dossier"]["digest"],
           all_pass(["COI-1", "COI-2"]))
check("复核单号跨案卷复用 409", r.status_code == 409 and err(r)["code"] == "review_conflict", r.text)

# ---------------------------------------------------------------- 11. 并发增补别名

print("11) 并发增补别名：同修订号仅一次成功（乐观修订号串行））")
client.post(f"{API}/persons", json={"person_id": "EMP-RACE", "aliases": ["初名"]})
barrier = threading.Barrier(2)
results: list[tuple[str, int, str]] = []


def add_concurrent(alias: str) -> None:
    r = client.post(f"{API}/persons/EMP-RACE/aliases",
                    json={"alias": alias, "expected_revision": 1})
    results.append((alias, r.status_code, err(r).get("code", "")))


t1 = threading.Thread(target=add_concurrent, args=("并发别名A",))
t2 = threading.Thread(target=add_concurrent, args=("并发别名B",))
t1.start(); t2.start(); t1.join(); t2.join()
ok = [x for x in results if x[1] == 200]
stale = [x for x in results if x[1] == 409 and x[2] == "person_revision_stale"]
check("并发同修订号增补：恰好一次成功、一次 person_revision_stale",
      len(ok) == 1 and len(stale) == 1, str(results))
final = client.get(f"{API}/persons/EMP-RACE").json()["person"]
check("落败方别名未写入，revision=2",
      final["revision"] == 2 and len(final["aliases"]) == 2, str(final))

# ---------------------------------------------------------------- 汇总

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
raise SystemExit(1 if FAIL else 0)
