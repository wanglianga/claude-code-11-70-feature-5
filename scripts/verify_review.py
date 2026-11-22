#!/usr/bin/env python3
"""容量决策复盘验收脚本。

覆盖：
  1. 策略/成本接口：台账模式与成本边界建议；
  2. 三条模拟序列：容量耗尽 / 低风险阻塞 / 策略切换竞争，判定结论与反事实差异；
  3. 顺序规范器：同刻事件按预登记 event_seq 裁定，且与数据库写入顺序不同；
  4. 检查点：状态保全保存当时队列水位与调度游标；
  5. 补传晚到：落入已复盘区间生成新版本，旧版本内容哈希不变；
  6. 证据不足保护：事件链断裂时比较器只给证据不足，并标注当前状态非历史；
  7. 生产台账隔离：模拟数据不出现在分区完整性口径。

用法： python scripts/verify_review.py [--base http://127.0.0.1:8000]
退出码： 0 全部通过；1 存在失败。
"""
import argparse
import sys
from datetime import datetime, timedelta, timezone

import requests

PASSED, FAILED = [], []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"[{tag}] {name}" + (f" —— {detail}" if detail else ""), flush=True)
    (PASSED if cond else FAILED).append(name)
    return cond


class Client:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.s = requests.Session()

    def login(self, username="admin", password="admin123"):
        r = self.s.post(f"{self.base}/api/auth/login", json={"username": username, "password": password}, timeout=30)
        r.raise_for_status()
        self.s.headers["Authorization"] = f"Bearer {r.json()['token']}"

    def get(self, path, **kw):
        r = self.s.get(f"{self.base}/api{path}", timeout=120, **kw)
        r.raise_for_status()
        return r.json()

    def post(self, path, body=None, **kw):
        r = self.s.post(f"{self.base}/api{path}", json=body or {}, timeout=300, **kw)
        r.raise_for_status()
        return r.json()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    args = ap.parse_args()
    c = Client(args.base)
    print(f"== 目标: {args.base} ==", flush=True)
    c.login()
    check("登录获取令牌", True)

    # ---------- 1. 策略与成本 ----------
    print("\n-- 台账模式与成本边界 --", flush=True)
    cfg = c.get("/review/config")
    check("默认台账模式为 checkpoint（低成本）", cfg["ledger_mode"]["mode"] == "checkpoint",
          cfg["ledger_mode"]["label"])
    check("提供 journal 与 checkpoint 成本估算",
          "journal" in cfg["cost"] and "checkpoint" in cfg["cost"])
    rec_journal = c.post("/review/strategy", {"response_target_sec": 0.2, "peak_event_rate": 500, "retention_days": 365})
    check("严格响应+高事件量建议 journal（直接解释）", rec_journal["recommended_mode"] == "journal",
          rec_journal["label"])
    rec_cp = c.post("/review/strategy", {"response_target_sec": 5, "peak_event_rate": 30, "retention_days": 365})
    check("常规响应+长保留期建议 checkpoint", rec_cp["recommended_mode"] == "checkpoint", rec_cp["label"])

    # ---------- 2. 三条模拟序列 ----------
    print("\n-- 三条模拟序列 --", flush=True)
    cap = c.post("/review/scenarios/capacity/build", {}, params={"force": True})
    capr = cap["latest_review"]
    check("容量耗尽序列判定为 capacity_exhausted", capr["diagnosis"]["verdict"] == "capacity_exhausted",
          capr["diagnosis"]["summary"])
    check("容量耗尽反事实给出证据充分的比较", capr["counterfactual"]["status"] == "ok")
    check("容量耗尽记录当时积压水位", (capr["diagnosis"]["signals"].get("anchor_backlog") or 0) >= 1,
          f"backlog={capr['diagnosis']['signals'].get('anchor_backlog')}")

    low = c.post("/review/scenarios/low_risk/build", {}, params={"force": True})
    lowr = low["latest_review"]
    check("低风险阻塞序列判定为 low_risk_blocked", lowr["diagnosis"]["verdict"] == "low_risk_blocked",
          lowr["diagnosis"]["summary"])
    cmp_low = next((x for x in lowr["counterfactual"]["comparisons"] if x["name"] == "若低风险任务未占用通道"), None)
    check("低风险未占用通道 → 目标等待缩短", cmp_low is not None and (cmp_low.get("wait_delta_ms") or 0) < 0,
          f"delta={cmp_low and cmp_low.get('wait_delta_ms')}ms")

    race = c.post("/review/scenarios/policy_race/build", {}, params={"force": True})
    racer = race["latest_review"]
    check("策略切换竞争序列判定为 policy_switch_race", racer["diagnosis"]["verdict"] == "policy_switch_race",
          racer["diagnosis"]["summary"])
    cmp_pol = next((x for x in racer["counterfactual"]["comparisons"] if x["name"] == "沿用上一策略"), None)
    check("沿用上一策略给出非零等待差异", cmp_pol is not None and cmp_pol.get("wait_delta_ms") not in (None, 0),
          f"delta={cmp_pol and cmp_pol.get('wait_delta_ms')}ms")

    # ---------- 3. 顺序规范器 ----------
    print("\n-- 顺序规范器（同刻按预登记序号） --", flush=True)
    oc = race["detail"]["ordering_check"]
    check("同刻事件的数据库写入顺序与预登记序号顺序不同", oc["differs"] is True,
          f"db={oc['by_db_write_order']}")
    check("裁定顺序以 event_seq 升序", [x[1] for x in oc["by_registered_seq"]] ==
          sorted(x[1] for x in oc["by_registered_seq"]))
    # 数据库写入顺序下 first=批次完成；规范器应 first=策略生效（seq 更小）
    check("规范器不依赖写入先后（策略生效先于批次完成裁定）",
          oc["by_registered_seq"][0][2] == "policy_effective" and oc["by_db_write_order"][0][2] == "batch_complete")

    # ---------- 4. 检查点（状态保全） ----------
    print("\n-- 状态保全（检查点） --", flush=True)
    anchor = capr["timeline"]["anchor"]
    check("复盘锚定到区间起点检查点", anchor.get("checkpoint_id") is not None,
          f"checkpoint={anchor.get('checkpoint_id')}")
    check("检查点保存分区队列水位与游标", bool(anchor.get("partitions")) and
          all("backlog" in v for v in anchor["partitions"].values()))

    # ---------- 5. 补传晚到 → 新版本 ----------
    print("\n-- 补传晚到 → 新版本 + 差异说明 --", flush=True)
    old_v = race["review_version"]
    old_hist = {h["version"]: h for h in race.get("history", [])}
    old_hash = (old_hist.get(old_v) or {}).get("content_hash")
    late = c.post("/review/scenarios/policy_race/late-event")
    check("补传晚到生成新复盘版本", late["review_version"] == old_v + 1,
          f"v{old_v} -> v{late['review_version']}")
    new_hist = {h["version"]: h for h in late.get("history", [])}
    if old_hash:
        check("旧版本内容哈希保持不变（已签发结论不改写）",
              new_hist.get(old_v, {}).get("content_hash") == old_hash, old_hash)
    else:
        check("旧版本仍在历史中（已签发结论不改写）", old_v in new_hist)
    check("新版本带差异说明", bool(late.get("diff") and late["diff"].get("changes")),
          "; ".join(late["diff"]["changes"]) if late.get("diff") else "")
    check("新版本指向被替代版本",
          new_hist.get(late["review_version"], {}).get("supersedes_version") == old_v)

    # ---------- 6. 证据不足保护 ----------
    print("\n-- 证据不足保护 --", flush=True)
    ins = c.post("/review/scenarios/insufficient/build", {}, params={"force": True})
    insr = ins["latest_review"]
    check("事件链断裂 → 结论为 insufficient", insr["diagnosis"]["verdict"] == "insufficient",
          insr["diagnosis"]["summary"])
    check("比较器只给证据不足", insr["counterfactual"]["status"] == "insufficient",
          "；".join(insr["counterfactual"].get("reasons", [])))
    check("证据不足时标注当前队列状态非历史答案",
          bool(insr["counterfactual"].get("current_queue_state_not_historical")) and
          "并非当时答案" in insr["counterfactual"].get("evidence_note", ""))

    # ---------- 7. 生产台账隔离 ----------
    print("\n-- 生产台账隔离 --", flush=True)
    integ = c.get("/partitions/integrity")
    check("模拟序列不进入分区完整性口径", integ["consistent"] is True)

    # ---------- 8. 生产告警复盘 ----------
    print("\n-- 生产告警复盘 --", flush=True)
    # 先注入一条确定性高风险超标读数并等待告警，保证在全新库上也可验证
    import time
    import uuid

    sess = f"verify-rev-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    ing = requests.post(
        f"{args.base}/api/ingest/readings",
        json={"gateway_code": "GW-VERIFY", "session_uuid": sess,
              "readings": [{"point_code": "WW-COD-01", "source_seq": 1, "ts": now, "value": 150.0}]},
        headers={"X-Ingest-Token": "ingest-dev-token"}, timeout=60,
    )
    ing.raise_for_status()
    new_log_id = ing.json()["results"][0]["log_id"]
    alerts = []
    for _ in range(30):
        alerts = c.get("/review/live/alerts")
        if any(a["receive_log_id"] == new_log_id for a in alerts):
            break
        time.sleep(1)
    check("可列出生产告警供复盘", any(a["receive_log_id"] == new_log_id for a in alerts),
          f"{len(alerts)} 条")
    target = next((a for a in alerts if a["receive_log_id"] == new_log_id), alerts[0] if alerts else None)
    if target:
        rr = c.post(f"/review/live/alerts/{target['alert_id']}")
        check("告警复盘返回时间线与证据索引",
              rr["result"]["timeline"]["events"] is not None and len(rr["result"]["evidence_index"]) >= 1,
              f"events={len(rr['result']['timeline']['events'])}")

    print(f"\n== 结果: {len(PASSED)} 通过, {len(FAILED)} 失败 ==", flush=True)
    if FAILED:
        print("失败项:", *FAILED, sep="\n  - ", flush=True)
        sys.exit(1)
    print("ALL REVIEW CHECKS PASSED", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
