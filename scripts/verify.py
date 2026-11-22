#!/usr/bin/env python3
"""验收脚本：模拟 实时流 / 补传流 / 冲突重传 / 持续写入变慢 四类场景，
核对 接收日志、策略台账、告警延迟、恢复游标、最终分区完整性 的一致性。

用法： python scripts/verify.py [--base http://127.0.0.1:8000]
退出码： 0 = 全部通过； 1 = 存在失败项。
"""
import argparse
import sys
import time
import uuid
from datetime import datetime, timezone

import requests

PASSED, FAILED = [], []


def utcnow_iso():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    line = f"[{tag}] {name}" + (f" —— {detail}" if detail else "")
    print(line, flush=True)
    (PASSED if cond else FAILED).append(name)
    return cond


def poll_until(fn, timeout=60, interval=1.0, desc=""):
    """轮询直到 fn() 返回真值或超时，返回 fn 的最后结果。"""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    return last


class Client:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.s = requests.Session()
        self.token = None

    def login(self, username="admin", password="admin123"):
        r = self.s.post(f"{self.base}/api/auth/login", json={"username": username, "password": password}, timeout=30)
        r.raise_for_status()
        self.token = r.json()["token"]
        self.s.headers["Authorization"] = f"Bearer {self.token}"

    def get(self, path, **kw):
        r = self.s.get(f"{self.base}/api{path}", timeout=60, **kw)
        r.raise_for_status()
        return r.json()

    def post(self, path, body=None, **kw):
        r = self.s.post(f"{self.base}/api{path}", json=body or {}, timeout=300, **kw)
        r.raise_for_status()
        return r.json()

    def patch(self, path, body):
        r = self.s.patch(f"{self.base}/api{path}", json=body, timeout=30)
        r.raise_for_status()
        return r.json()

    def ingest(self, gateway, session, readings):
        """以网关身份直接调用接入 API（X-Ingest-Token）。"""
        r = requests.post(
            f"{self.base}/api/ingest/readings",
            json={"gateway_code": gateway, "session_uuid": session, "readings": readings},
            headers={"X-Ingest-Token": "ingest-dev-token"},
            timeout=300,
        )
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

    # ---------- 0. 设备目录 ----------
    devices = c.get("/devices")
    check("设备目录已种子（>=10 个点位）", len(devices) >= 10, f"{len(devices)} 个点位")
    by_code = {d["point_code"]: d for d in devices}

    # ---------- 1. 实时流 -> 告警延迟 ----------
    print("\n-- 场景一：实时流 --", flush=True)
    c.post("/simulate/realtime", {"count": 40, "exceed_ratio": 0.4})
    # 确定性超标：直接向高风险点位 WW-COD-01 发超标读数（阈值 100）
    rt_session = f"verify-rt-{uuid.uuid4().hex[:8]}"
    now = utcnow_iso()
    res = c.ingest("GW-VERIFY", rt_session, [{"point_code": "WW-COD-01", "source_seq": 1, "ts": now, "value": 150.0}])
    rt_log_id = res["results"][0]["log_id"]
    check("实时读数被确认", res["results"][0]["status"] == "confirmed", f"log #{rt_log_id}")

    def alert_for_rt():
        data = c.get("/alerts", params={"page_size": 50})
        for a in data["items"]:
            if a["receive_log_id"] == rt_log_id:
                return a
        return None

    alert = poll_until(alert_for_rt, timeout=60, desc="等待告警")
    check("高风险超标产生告警", alert is not None)
    if alert:
        check(
            "告警延迟 < 15 分钟",
            alert["latency_ms"] < 15 * 60 * 1000,
            f"latency={alert['latency_ms']}ms",
        )

    # ---------- 2. 冲突重传 ----------
    print("\n-- 场景二：冲突重传 --", flush=True)
    cf = c.post("/simulate/conflict")
    sid = cf["session_uuid"]
    logs = c.get("/logs", params={"session_uuid": sid, "page_size": 10})["items"]
    statuses = {l["status"] for l in logs}
    check("同会话同序列产生 已确认/重传/冲突 三态", statuses == {"confirmed", "duplicate", "conflict"}, f"statuses={statuses}")
    ev = c.get(f"/evidence/{cf['confirmed']['log_id']}")
    check("已确认读数保持先到值（未被覆盖）", ev["receive_log"]["value"] == cf["confirmed_value"],
          f"value={ev['receive_log']['value']}")
    check("冲突证据并列保存", len(ev["conflicts"]) == 1 and ev["conflicts"][0]["value"] == cf["conflict_value"],
          f"conflicts={ev['conflicts']}")

    # ---------- 3. 补传流 -> 恢复游标 ----------
    print("\n-- 场景三：断网恢复补传流 --", flush=True)
    runs_before = c.get("/recovery/runs")
    before_ids = {r["id"] for r in runs_before}
    bf = c.post("/simulate/backfill", {"hours": 1.0, "step_sec": 60})
    check("补传读数全部确认", bf["counts"]["confirmed"] > 0, f"confirmed={bf['counts']['confirmed']}")

    def new_recovery_runs():
        runs = [r for r in c.get("/recovery/runs") if r["id"] not in before_ids]
        return runs if sum(r["recovered_count"] for r in runs) >= bf["counts"]["confirmed"] else None

    runs = poll_until(new_recovery_runs, timeout=120, interval=2)
    check("恢复编排器按分区游标回补积压", runs is not None,
          f"{len(runs) if runs else 0} 次运行" if runs else "超时未见回补")
    if runs:
        check("回补运行覆盖全部分区", {r["partition_key"] for r in runs} >= {"P-HIGH-1", "P-HIGH-2", "P-MED-1", "P-LOW-1"})

    # ---------- 4. 策略升级 -> 台账 ----------
    print("\n-- 场景四：策略升级与台账 --", flush=True)
    ledger_before = c.get("/policies/ledger")
    prev_version = max(p["version"] for p in ledger_before)
    c.post("/policies", {"name": f"验收策略 v{prev_version + 1}", "priority": {"high": 100, "medium": 60, "low": 5},
                         "reserve_high_writes": 60, "activate": True, "note": "验收脚本发布"})
    c.post("/simulate/realtime", {"count": 20, "exceed_ratio": 0.2})
    time.sleep(3)

    def ledger_ok():
        ledger = c.get("/policies/ledger")
        v_prev = next((p for p in ledger if p["version"] == prev_version), None)
        v_new = next((p for p in ledger if p["version"] == prev_version + 1), None)
        if v_prev and v_new and v_prev["batch_count"] > 0 and v_new["batch_count"] > 0:
            return ledger
        return None

    ledger = poll_until(ledger_ok, timeout=60)
    check("旧批次继续引用创建时策略版本", ledger is not None and
          next(p for p in ledger if p["version"] == prev_version)["batch_count"] > 0,
          f"v{prev_version} 仍有历史批次")
    check("新批次使用新策略版本", ledger is not None and
          next(p for p in ledger if p["version"] == prev_version + 1)["batch_count"] > 0,
          f"v{prev_version + 1} 已产生批次")

    # ---------- 5. 设备校准 -> 待核不告警 ----------
    print("\n-- 场景五：设备校准期间读数 --", flush=True)
    dev = by_code["WW-NH3-01"]
    c.patch(f"/devices/{dev['id']}", {"status": "calibrating"})
    try:
        cal_session = f"verify-cal-{uuid.uuid4().hex[:8]}"
        res = c.ingest("GW-VERIFY", cal_session,
                       [{"point_code": "WW-NH3-01", "source_seq": 1, "ts": utcnow_iso(), "value": 50.0}])
        cal_log_id = res["results"][0]["log_id"]

        def cal_conclusion():
            data = c.get("/quality/conclusions", params={"device_point": "WW-NH3-01", "page_size": 50})
            for item in data["items"]:
                if item["receive_log_id"] == cal_log_id:
                    return item
            return None

        concl = poll_until(cal_conclusion, timeout=60)
        check("校准期间读数结论为待核", concl is not None and concl["conclusion"] == "pending_review",
              f"conclusion={concl and concl['conclusion']}")
        check("待核结论未确认", concl is not None and concl["confirmed"] is False)
        alerts = c.get("/alerts", params={"page_size": 200})["items"]
        check("待核结论未进入告警", all(a["receive_log_id"] != cal_log_id for a in alerts))
    finally:
        c.patch(f"/devices/{dev['id']}", {"status": "normal"})

    # ---------- 6. 持续写入变慢 -> 降级与恢复 ----------
    print("\n-- 场景六：持续写入变慢 --", flush=True)
    c.post("/simulate/slow-writes", {"enabled": True, "delay_ms": 1200})
    try:
        st = poll_until(lambda: c.get("/scheduler/state")["degraded"] and c.get("/scheduler/state") or None,
                        timeout=60, interval=2)
        check("写入延迟跨阈值后调度器降级", st is not None and st["degraded"])
        check("低风险衍生计算已暂停", st is not None and st["low_risk_paused"])

        # 降级期间：高风险超标仍应被告警；低风险读数应积压
        dg_session = f"verify-dg-{uuid.uuid4().hex[:8]}"
        res_hi = c.ingest("GW-VERIFY", dg_session,
                          [{"point_code": "WW-COD-01", "source_seq": 1, "ts": utcnow_iso(), "value": 160.0}])
        hi_log_id = res_hi["results"][0]["log_id"]
        res_lo = c.ingest("GW-VERIFY", dg_session,
                          [{"point_code": "TK-LV-03", "source_seq": 2, "ts": utcnow_iso(), "value": 5.0}])
        lo_log_id = res_lo["results"][0]["log_id"]

        def alert_for_hi():
            data = c.get("/alerts", params={"page_size": 100})
            for a in data["items"]:
                if a["receive_log_id"] == hi_log_id:
                    return a
            return None

        hi_alert = poll_until(alert_for_hi, timeout=90, interval=2)
        check("降级期间高风险分区保留写入额度（超标仍告警）", hi_alert is not None,
              f"latency={hi_alert['latency_ms']}ms" if hi_alert else "超时未告警")

        def low_backlog():
            for p in c.get("/partitions"):
                if p["partition_key"] == "P-LOW-1":
                    return p["backlog"] >= 1 and p
            return None

        backlog = poll_until(low_backlog, timeout=30)
        check("降级期间低风险分区产生积压水位", backlog is not None,
              f"P-LOW-1 backlog={backlog['backlog']}" if backlog else "无积压")
    finally:
        c.post("/simulate/slow-writes", {"enabled": False})

    st2 = poll_until(lambda: (not c.get("/scheduler/state")["degraded"]) and c.get("/scheduler/state") or None,
                     timeout=60, interval=2)
    check("写入恢复后调度器退出降级", st2 is not None and not st2["degraded"])

    c.post("/recovery/run", {})

    def all_consistent():
        return c.get("/partitions/integrity")["consistent"]

    check("恢复后最终分区完整性一致", poll_until(all_consistent, timeout=120, interval=2) is True)

    # ---------- 7. 证据链完整性 ----------
    print("\n-- 证据链抽查 --", flush=True)
    ev2 = c.get(f"/evidence/{hi_log_id}")
    chain_ok = all([ev2["receive_log"], ev2["session"], ev2["device"], ev2["batch"], ev2["conclusion"], ev2["alert"]])
    check("高风险读数证据链完整（日志/会话/设备/批次/结论/告警）", chain_ok)
    if ev2["batch"]:
        check("证据链批次引用策略版本", ev2["batch"]["policy_version"] >= 1,
              f"policy_version={ev2['batch']['policy_version']}")

    # ---------- 汇总 ----------
    print(f"\n== 结果: {len(PASSED)} 通过, {len(FAILED)} 失败 ==", flush=True)
    if FAILED:
        print("失败项:", *FAILED, sep="\n  - ", flush=True)
        sys.exit(1)
    print("ALL CHECKS PASSED", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
