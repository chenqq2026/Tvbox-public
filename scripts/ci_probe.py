#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CI 侧搜索实测探路 —— 只诊断，不写任何配置。

目的：量化 GitHub Actions（海外机房）对国内采集站的可达性，判断能不能把
build_lite.py 的搜索实测放进 CI，实现「零手动」的每日全自动维护。

口径保证：直接 import build_lite 的常量与函数（KW / collect_candidates /
one_search / fetch / UA / 判定逻辑），不复制代码，避免「零转录」被破坏导致
本机与 CI 数据不可比。基线是仓库里 lite_probe.json 的本机实测结果。

双门槛（与门，任一不过就终止，不落地全自动）：
  ① 请求成功率 ≥ 0.60  —— 整体网络可用性
  ② 活站召回率 ≥ 0.80  —— 本机 10 个达标站里，CI 仍能判为达标(hit≥4)的比例
     这条是防误杀核心：CI 把活站判死，比「测不到」危险得多。

输出：ci_probe_result.json（结构化证据，提交留档）
      ci_probe_summary.md（仅用于 GitHub Step Summary）
"""
import concurrent.futures as cf
import datetime as dt
import json
import os
from collections import defaultdict

# 零转录：复用本机守门工具的同一套常量 / 请求 / 判定实现
from build_lite import (KW, collect_candidates, one_search,
                        load_json, write_json, stamp)

CFG = "config_all.json"
EVIDENCE = "lite_probe.json"
RESULT = "ci_probe_result.json"
SUMMARY = "ci_probe_summary.md"
MIN_HIT = 4
GATE_REQUEST_RATE = 0.60
GATE_RECALL = 0.80


def main():
    cfg = load_json(CFG)
    ev = load_json(EVIDENCE) or {}
    if not isinstance(cfg, dict) or not cfg.get("sites"):
        raise RuntimeError("config_all.json 不可读")
    if not ev.get("keep_sites"):
        raise RuntimeError("lite_probe.json 缺本机基线，无法算召回率")

    cands = collect_candidates(cfg)
    names = {c["norm"]: c["name"] for c in cands}
    local_keep = {s["norm"] for s in ev["keep_sites"]}
    local_rows = {s["norm"]: s for s in ev.get("site_rows", [])}
    local_zero = {n for n, s in local_rows.items() if s.get("hit", 0) == 0}

    jobs = [(c, kw) for c in cands for kw in KW]
    print(f"CI 探路：{len(cands)} 个去重直连 CMS × {len(KW)} 关键词 = {len(jobs)} 次搜索", flush=True)
    with cf.ThreadPoolExecutor(16) as ex:
        res = list(ex.map(one_search, jobs))

    hits, items, times, errs, req_errors = (defaultdict(int), defaultdict(int),
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
            req_errors[err] += 1

    req_ok = sum(1 for _n, _k, _c, t, _e in res if t > 0)
    rate = req_ok / len(jobs) if jobs else 0.0
    rows = []
    for c in cands:
        m = sorted(times[c["norm"]])
        rows.append({**c, "hit": hits[c["norm"]], "items": items[c["norm"]],
                     "ttfb_median": m[len(m) // 2] if m else 0,
                     "errors": errs[c["norm"]],
                     "local_hit": local_rows.get(c["norm"], {}).get("hit")})
    rows.sort(key=lambda x: (-x["hit"], -x["items"]))

    ci_keep = {r["norm"] for r in rows if r["hit"] >= MIN_HIT}
    kept = local_keep & ci_keep
    false_kill = local_keep - ci_keep
    extra_keep = ci_keep - local_keep
    recall = len(kept) / len(local_keep) if local_keep else 0.0
    all_ms = [t for _n, _k, _c, t, _e in res if t]
    ttfb_med = sorted(all_ms)[len(all_ms) // 2] if all_ms else 0
    zero_in_ci = {names[n]: hits[n] for n in local_zero if n in names}

    gates = {
        "request_success_rate>=0.60": rate >= GATE_REQUEST_RATE,
        "live_site_recall>=0.80": recall >= GATE_RECALL,
    }
    passed = all(gates.values())

    out = {
        "probed_at": stamp(),
        "runner": os.environ.get("CI_PROBE_RUNNER", "GitHub Actions ubuntu-latest (overseas)"),
        "purpose": "探路：判断能否把搜索实测放进 CI 实现零手动维护",
        "method": "与 build_lite.py 零转录复用；基线 = lite_probe.json 本机实测",
        "keywords": KW,
        "candidates": len(cands),
        "requests_total": len(jobs),
        "requests_ok": req_ok,
        "request_success_rate": round(rate, 4),
        "request_errors": dict(req_errors),
        "ttfb_median_ms": ttfb_med,
        "local_keep_sites": len(local_keep),
        "ci_keep_sites": len(ci_keep),
        "recall": round(recall, 4),
        "false_kill_sites": [names.get(n, n) for n in sorted(false_kill)],
        "extra_keep_sites": [names.get(n, n) for n in sorted(extra_keep)],
        "local_zero_hit_in_ci": zero_in_ci,
        "site_rows": rows,
        "gates": gates,
        "passed": passed,
        "conclusion": ("双门槛通过，可落地 CI 全自动证据链"
                       if passed else "门槛未过，按纪律终止，不落地全自动"),
    }
    write_json(RESULT, out)

    lines = [
        "## CI 侧搜索实测探路结论", "",
        "| 项 | 值 |", "|---|---|",
        f"| 运行环境 | {out['runner']} |",
        f"| 请求成功 | {req_ok}/{len(jobs)}（{rate:.1%}） |",
        f"| TTFB 中位 | {ttfb_med} ms |",
        f"| 本机活站 / CI 判活 | {len(local_keep)} / {len(ci_keep)} |",
        f"| 活站召回率 | {recall:.1%} |",
        f"| 被 CI 误杀的活站 | {len(false_kill)} 个 |",
        f"| 失败类型 | {dict(req_errors) or '无'} |",
        "", "### 门槛（与门）", "",
    ]
    for k, v in gates.items():
        lines.append(f"- {'🟢 PASS' if v else '🔴 FAIL'}  {k}")
    lines += ["", f"**结论：{out['conclusion']}**", "",
              "### 各站明细（CI hit / 本机 hit）", "",
              "| 站 | CI | 本机 | TTFB |", "|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['name']} | {r['hit']}/{len(KW)} | "
                     f"{r['local_hit'] if r['local_hit'] is not None else '-'}/8 | "
                     f"{r['ttfb_median']}ms |")
    open(SUMMARY, "w", encoding="utf-8", newline="\n").write("\n".join(lines) + "\n")

    print(f"\n请求成功 {req_ok}/{len(jobs)} = {rate:.1%}，失败类型 {dict(req_errors) or '无'}")
    print(f"本机活站 {len(local_keep)} / CI 判活 {len(ci_keep)} / 召回 {recall:.1%}")
    print(f"误杀: {[names.get(n, n) for n in sorted(false_kill)]}")
    print(f"本机0命中站在CI: {zero_in_ci}")
    for k, v in gates.items():
        print(f"  {'🟢 PASS' if v else '🔴 FAIL'}  {k}")
    print(f"结论: {out['conclusion']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
