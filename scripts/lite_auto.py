#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CI 每日全自动维护 config_lite.json —— 不再需要手动刷新证据。

探路结论（2026-11-06，ci_probe_result.json）
------------------------------------------
GitHub Actions 海外机房对国内「点播采集站 API」完全可达：请求成功率 83.3%
（120/144）、TTFB 中位 233ms、本机 10 个活站召回 100%、零误杀。
但方向和我原先担心的相反：CI 把本机 0/8 的「非凡」「尤酷」判成了 8/8、7/8。
也就是 CI 会「误纳」——海外可达 ≠ 用户家里能用。

注意这不推翻直播的结论：直播 IPTV 地方台受地域/运营商限制，CI 确实系统性
不可达（频道 347→332→322、本机复验 14/14 可播）。点播采集站是公网 CDN，
两者性质不同，结论不同不矛盾。

双源与门（核心机制）
--------------------
  本机证据 = 硬基线（用户真实网络），CI 证据 = 衰减维护，二者与门：
  · 本机基线 7 天内 → 名单锁定不动，CI 只累计衰减计数并每天重建配置
  · 超过 7 天 → CI 接管演化：连续 3 天 0 命中才淘汰，连续 2 天达标才加回
  · 本机明确判死(hit=0)的站进 locked_out，**CI 永远无权自动加回**
    （这一条专门防上面测出来的「误纳」）
  · 解析器：CI 每天复验，连续 3 天判死才剔除；不自动新增（同理防误纳）
  · 单日整体异常（成功率<50% 或全站 0 命中）→ 观测作废，零写入
  · 活站批量误杀保护：当前名单里 >50% 本次判死 → 判为 CI 抖动，作废
  · 任一门槛不过 → state / 证据 / 配置全部零写入，保留旧版本

用法（由 daily-update.yml 每天调用）
    python scripts/lite_auto.py
"""
import concurrent.futures as cf
import datetime as dt
from collections import defaultdict

# 零转录：与本机守门工具共用同一套常量 / 请求 / 判定 / 重建实现
from build_lite import (KW, HOLDOUT_KW, MIN_HIT, MIN_SITES, MIN_PARSES,
                        MIN_KW_STATIONS, SHRINK_GUARD, CFG_ALL, EVIDENCE,
                        collect_candidates, one_search, one_parse,
                        load_json, write_json, rebuild, now8, stamp)

STATE = "lite_state.json"
DROP_STREAK = 3          # 连续 N 天 0 命中才淘汰
ADD_STREAK = 2           # 连续 N 天达标才加回（locked_out 除外）
PARSE_DROP_STREAK = 3    # 解析器连续 N 天判死才剔除
LOCAL_TTL_DAYS = 7


def days_ago(s):
    try:
        return (now8() - dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")).total_seconds() / 86400
    except Exception:
        return 1e9


def blank_state():
    return {"schema": 1, "updated_at": None, "site_streak": {}, "parse_streak": {},
            "local_baseline": {}, "locked_out": [], "events": [],
            "last_keep_sites": 0, "shrink_streak": 0}


def main():
    cfg = load_json(CFG_ALL)
    ev = load_json(EVIDENCE)
    if not isinstance(cfg, dict) or not cfg.get("sites") or not isinstance(ev, dict) \
            or not ev.get("keep_sites"):
        print("缺主配置或证据 → 保留旧 config_lite.json")
        return 0
    state = load_json(STATE) or blank_state()

    # ---- 本机基线：只要证据来自本机就刷新，本机判定始终是权威 ----
    if "本机" in str(ev.get("source") or ""):
        state["local_baseline"] = {
            "probed_at": ev.get("probed_at"),
            "keep": [s["norm"] for s in ev["keep_sites"]],
            "zero": [s["norm"] for s in ev.get("site_rows", []) if s.get("hit", 0) == 0],
        }
    base = state.get("local_baseline") or {}
    locked_out = set(state.get("locked_out") or []) | set(base.get("zero") or [])
    state["locked_out"] = sorted(locked_out)
    local_fresh = 0 <= days_ago(base.get("probed_at") or "") <= LOCAL_TTL_DAYS
    local_hit_map = {**{n: 8 for n in (base.get("keep") or [])},
                     **{n: 0 for n in (base.get("zero") or [])}}

    # ---- 每日实测（主探测 + 留出集 + 解析器）----
    cands = collect_candidates(cfg)
    jobs = [(c, k) for c in cands for k in KW]
    hold_jobs = [(c, k) for c in cands for k in HOLDOUT_KW]
    print(f"== CI 每日实测：{len(cands)} 站 × {len(KW)} 词 + 留出集 {len(HOLDOUT_KW)} 词 ==")
    with cf.ThreadPoolExecutor(16) as ex:
        res = list(ex.map(one_search, jobs))
        hres = list(ex.map(one_search, hold_jobs))

    parses_in, seen = [], set()
    for p in cfg.get("parses", []):
        u = str(p.get("url") or "")
        if u.startswith("http") and u not in seen:
            seen.add(u)
            parses_in.append(p)
    with cf.ThreadPoolExecutor(12) as ex:
        prs = [x for x in ex.map(one_parse, parses_in) if x]
    pinfo = {p["url"]: p for p in prs}

    hits, items, times, errs, req_err = (defaultdict(int), defaultdict(int),
                                         defaultdict(list), defaultdict(int),
                                         defaultdict(int))
    for norm, kw, n, ttfb, err in res:
        if n > 0:
            hits[norm] += 1
            items[norm] += n
        if ttfb:
            times[norm].append(ttfb)
        if err:
            errs[norm] += 1
            req_err[err] += 1
    hold_hits, hold_cover = defaultdict(int), defaultdict(set)
    for norm, kw, n, _t, _e in hres:
        if n > 0:
            hold_hits[norm] += 1
            hold_cover[kw].add(norm)
    cover = defaultdict(set)
    for norm, kw, n, _t, _e in res:
        if n > 0:
            cover[kw].add(norm)

    req_ok = sum(1 for _n, _k, _c, t, _e in res if t > 0)
    rate = req_ok / len(jobs) if jobs else 0.0
    rows = []
    for c in cands:
        m = sorted(times[c["norm"]])
        rows.append({**c, "hit": hits[c["norm"]], "items": items[c["norm"]],
                     "ttfb_median": m[len(m) // 2] if m else 0,
                     "errors": errs[c["norm"]],
                     "local_hit": local_hit_map.get(c["norm"])})
    rows.sort(key=lambda x: (-x["hit"], -x["items"], x["ttfb_median"] or 99999))

    # ---- 整体作废：CI 抖动 / 代理故障时不留任何痕迹 ----
    if rate < 0.5 or all(hits[c["norm"]] == 0 for c in cands):
        print(f"🔴 整体异常（成功率 {rate:.1%} 或全站 0 命中）→ 本次观测作废，零写入")
        return 0

    # ---- 衰减计数（先算在内存，门槛不过就不落盘）----
    ss = dict(state.get("site_streak") or {})
    for c in cands:
        n = c["norm"]
        cur = dict(ss.get(n) or {"zero": 0, "hit": 0})
        cur["name"] = c["name"]
        if hits[n] >= MIN_HIT:
            cur["hit"] += 1
            cur["zero"] = 0
        else:
            cur["zero"] += 1
            cur["hit"] = 0
        ss[n] = cur

    prev_keep = [s["norm"] for s in ev["keep_sites"]]
    current = set(prev_keep)
    events = []
    if local_fresh:
        keep = set(current)
        print(f"  本机基线仍新鲜（{base.get('probed_at')}），名单锁定，仅累计衰减计数")
    else:
        keep = set(current)
        for n in list(keep):
            if ss.get(n, {}).get("zero", 0) >= DROP_STREAK:
                keep.discard(n)
                events.append({"at": stamp(), "action": "drop",
                               "name": ss[n]["name"], "reason": f"连续{DROP_STREAK}天0命中"})
        for c in cands:
            n = c["norm"]
            if n in keep or n in locked_out:
                continue
            if ss.get(n, {}).get("hit", 0) >= ADD_STREAK:
                keep.add(n)
                events.append({"at": stamp(), "action": "add",
                               "name": c["name"], "reason": f"连续{ADD_STREAK}天达标"})
        if events:
            for e in events:
                print(f"  名单演化: {e['action']} {e['name']} ({e['reason']})")

    # ---- 解析器：只剔除连续判死者，不自动新增（防误纳）----
    ps = dict(state.get("parse_streak") or {})
    keep_parse = []
    for u in [p["url"] for p in ev.get("keep_parses", [])]:
        r = pinfo.get(u)
        cur = dict(ps.get(u) or {"zero": 0, "hit": 0})
        if r and r["verdict"] == "可用":
            cur["hit"] += 1
            cur["zero"] = 0
        else:
            cur["zero"] += 1
            cur["hit"] = 0
        ps[u] = cur
        if cur["zero"] < PARSE_DROP_STREAK:
            keep_parse.append(r or {"name": "未测", "url": u,
                                    "verdict": "未测", "ttfb": 0, "err": ""})
    keep_parse.sort(key=lambda p: (p.get("ttfb") or 99999))

    # ---- 门槛（与门）----
    keep_sites = [r for r in rows if r["norm"] in keep]
    kn = {r["norm"] for r in keep_sites}
    stations = {k: len(cover[k] & kn) for k in KW}
    hold_stations = {k: len(hold_cover[k] & kn) for k in HOLDOUT_KW}
    dead_in_keep = sum(1 for n in current if n in kn and hits[n] == 0)
    shrink = len(keep_sites) < len(prev_keep) * SHRINK_GUARD
    shrink_streak = (int(state.get("shrink_streak") or 0) + 1) if shrink else 0

    checks = {
        "网络整体正常": rate >= 0.5,
        "能出片站点数≥%d" % MIN_SITES: len(keep_sites) >= MIN_SITES,
        "关键词全覆盖(每词≥%d站)" % MIN_KW_STATIONS:
            all(v >= MIN_KW_STATIONS for v in stations.values()),
        "留出集覆盖(每词≥%d站)" % MIN_KW_STATIONS:
            all(v >= MIN_KW_STATIONS for v in hold_stations.values()),
        "可用解析器≥%d" % MIN_PARSES: len(keep_parse) >= MIN_PARSES,
        "活站批量误杀保护": dead_in_keep <= len(current) * 0.5,
        "站点骤降防抖": (not shrink) or shrink_streak >= 2,
    }
    passed = all(checks.values())
    print(f"  请求成功 {req_ok}/{len(jobs)} = {rate:.1%} | 名单 {len(prev_keep)} → "
          f"{len(keep_sites)} 站 / {len(keep_parse)} 解析器 | 本次判死活站 {dead_in_keep} 个")
    for k, v in checks.items():
        print(f"    {'🟢 PASS' if v else '🔴 FAIL'}  {k}")
    # CI 会「误纳」的站：本次判活但被本机基线锁死，单独留痕，绝不进名单
    extra = [r["name"] for r in rows if r["hit"] >= MIN_HIT and r["norm"] in locked_out]
    if extra:
        print(f"  ⚠️ CI 误纳候选（本机基线判死，已拦截）: {extra}")

    if not passed:
        print("🔴 门槛未全过 → state / 证据 / 配置全部零写入，保留旧版本")
        return 0

    # ---- 落盘 ----
    state.update({
        "updated_at": stamp(), "site_streak": ss, "parse_streak": ps,
        "locked_out": sorted(locked_out),
        "events": ((state.get("events") or []) + events)[-30:],
        "last_keep_sites": len(keep_sites), "shrink_streak": shrink_streak,
    })
    new_ev = dict(ev)
    new_ev.update({
        "probed_at": stamp(),
        "source": "ci-auto（GitHub Actions 海外机房）",
        "runner": "GitHub Actions ubuntu-latest",
        "passed": True, "checks": checks,
        "request_ok": req_ok, "request_total": len(jobs),
        "request_errors": dict(req_err),
        "keep_sites": keep_sites, "keep_parses": keep_parse,
        "site_rows": rows, "parse_rows": prs,
        "zero_hit_sites": [r["name"] for r in rows if r["hit"] == 0],
        "keyword_stations": stations,
        "holdout_keywords": HOLDOUT_KW,
        "holdout_stations": hold_stations,
        "holdout_hits_per_site": {r["norm"]: hold_hits[r["norm"]] for r in rows},
        "shrink_streak": shrink_streak, "shrink_prev": len(prev_keep),
        "auto": {
            "local_baseline_at": base.get("probed_at"),
            "local_fresh": local_fresh,
            "locked_out": sorted(locked_out),
            "ci_extra_keep_blocked": extra,
            "drop_streak_days": DROP_STREAK,
            "add_streak_days": ADD_STREAK,
            "events": state["events"][-10:],
            "note": "CI 每天实测但只做衰减淘汰；本机基线判死的站永不自动加回",
        },
    })
    write_json(STATE, state)
    write_json(EVIDENCE, new_ev)
    ok = rebuild(cfg, new_ev)
    if not ok:
        print("  rebuild 未成功，保留旧 config_lite.json（证据与 state 已更新，下次会自愈）")

    h = load_json("health.json")
    if isinstance(h, dict) and isinstance(h.get("lite"), dict):
        h["lite"]["auto"] = {
            "source": new_ev["source"],
            "local_baseline_at": base.get("probed_at"),
            "local_fresh": local_fresh,
            "locked_out": len(locked_out),
            "ci_extra_keep_blocked": len(extra),
            "events_today": len(events),
            "keep_sites": len(keep_sites),
            "keep_parses": len(keep_parse),
        }
        write_json("health.json", h, indent=2)
    print(f"✅ 全自动维护完成：证据刷新于 {new_ev['probed_at']}，零手动")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
