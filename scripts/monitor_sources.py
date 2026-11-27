#!/usr/bin/env python3
"""订阅源可用性监控（CI 侧运行）。

边界（重要）：CI 在海外机房，测速结果对国内电视盒无参考价值（见直播误杀），
所以这里 **不做任何测速、不做通道切换决策**。只做两件在 CI 里 100% 可靠的事：

1. 产物完整性：4 个对外订阅地址对应的本地文件是否存在、JSON 是否合法、
   关键字段是否齐全（sites / lives / urls）、数量是否跌破安全下限。
2. 通道固化校验（两类分开，2026-11-27 重构）：
   - 地址栏：config.json 的 urls 必须固定指向 ghfast.top（用户盒子填的入口，
     永不随测速变；变了会导致盒子缓存失效）。
   - jar 通道：各配置里的 spider.jar 地址必须指向当前最优通道 JAR_HOST
     （由本机国内测速脚本 auto_fastest.py 写入，CI 只校验不决策）。
   任一类被上游脚本误写，都标记异常并写报告。

产物 monitor_report.json 供人查看，不参与自动提交（避免抖动）。
异常不 fail 整个 workflow——只记录，是否处理由人决定。
"""
import datetime as dt
import json
import os
import sys

ADDRESS_HOST = "ghfast.top"   # 地址栏固定通道：永不改，用户盒子填的就是这个
GHFAST = "gh-proxy.com"       # 当前 jar 通道：由 auto_fastest.py 测速后写入（变量名沿用）
REPORT = "monitor_report.json"

# 4 个对外订阅地址 -> 对应本地文件；size_floor 是内容跌破就判异常的下限
TARGETS = [
    ("主用 config_all", "config_all.json", "sites", 50),
    ("精简 config_lite", "config_lite.json", "sites", 5),
    ("应急 spare_juhe", "spare_juhe.json", "sites", 300),
    ("剔站 pruned", "pruned_config.json", "sites", 100),
    ("直播 live_verified", "live_verified.m3u", "lines", 100),
    ("多仓壳 config", "config.json", "urls", 2),
]

rows = []
problems = []

for label, fn, field, floor in TARGETS:
    row = {"name": label, "file": fn, "field": field, "ok": False}
    if not os.path.exists(fn):
        row["reason"] = "文件缺失"
        problems.append(f"{label}: 文件缺失")
        rows.append(row)
        continue
    try:
        if fn.endswith(".m3u"):
            n = sum(1 for line in open(fn, encoding="utf-8") if line.startswith("#EXTINF"))
        else:
            d = json.load(open(fn, encoding="utf-8"))
            n = len(d.get(field) or []) if isinstance(d, dict) else len(d)
        row["count"] = n
        if n >= floor:
            row["ok"] = True
        else:
            row["reason"] = f"{field}={n} 低于下限 {floor}"
            problems.append(f"{label}: {row['reason']}")
    except Exception as e:
        row["reason"] = f"解析失败 {type(e).__name__}"
        problems.append(f"{label}: {row['reason']}")
    rows.append(row)

# 通道固化校验：地址栏固定 ghfast.top，jar 通道走当前最优 GHFAST，两者分开判。
def _check(urls, host):
    return [u for u in urls if "raw.githubusercontent" in u and host not in u]


chan_rows = []
# (a) 地址栏：config.json 的 urls 是用户填进盒子的订阅入口，必须固定 ADDRESS_HOST
fn = "config.json"
if os.path.exists(fn):
    try:
        d = json.load(open(fn, encoding="utf-8"))
        urls = [u["url"] for u in (d.get("urls") or [])
                if isinstance(u, dict) and isinstance(u.get("url"), str)]
        bad = _check(urls, ADDRESS_HOST)
        chan_rows.append({"file": fn, "kind": "地址栏", "expect": ADDRESS_HOST,
                          "urls": len(urls), "off_channel": len(bad)})
        if bad:
            problems.append(f"{fn}: {len(bad)} 个地址栏入口偏离固定通道 {ADDRESS_HOST}")
    except Exception as e:
        chan_rows.append({"file": fn, "error": type(e).__name__})

# (b) jar 通道：各配置的 spider 字段必须指向当前最优 GHFAST
for fn in ["config_all.json", "config_lite.json", "spare_juhe.json",
           "pruned_config.json"]:
    if not os.path.exists(fn):
        continue
    try:
        d = json.load(open(fn, encoding="utf-8"))
        urls = [d["spider"]] if isinstance(d.get("spider"), str) else []
        bad = _check(urls, GHFAST)
        chan_rows.append({"file": fn, "kind": "jar", "expect": GHFAST,
                          "urls": len(urls), "off_channel": len(bad)})
        if bad:
            problems.append(f"{fn}: {len(bad)} 个 jar 地址未走当前最优通道 {GHFAST}")
    except Exception as e:
        chan_rows.append({"file": fn, "error": type(e).__name__})

stamp = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
         + dt.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
report = {
    "checked_at": stamp,
    "scope": "产物完整性 + 通道固化（CI 不做测速/不切通道）",
    "targets": rows,
    "channels": chan_rows,
    "all_ok": not problems,
    "problems": problems,
}
json.dump(report, open(REPORT, "w", encoding="utf-8", newline="\n"),
          ensure_ascii=False, indent=2)

print("=== 订阅源可用性监控（CI 侧，仅完整性/通道校验）===")
for r in rows:
    flag = "OK" if r["ok"] else "!!"
    detail = f"{r.get('field')}={r.get('count')}" if r["ok"] else r.get("reason", "")
    print(f"  [{flag}] {r['name']:20s} {detail}")
for c in chan_rows:
    print(f"  [通道] {c['file']:20s} {c.get('channel')} (off={c.get('off_channel', 0)})")
if problems:
    print(f"  发现 {len(problems)} 个问题（详见 {REPORT}，不自动处理）")
else:
    print("  全部正常")

# --summary：输出 markdown 表格供 workflow 写进 GITHUB_STEP_SUMMARY
if "--summary" in sys.argv:
    print("")
    print("| 检查项 | 状态 |")
    print("|---|---|")
    for r in rows:
        state = "OK" if r["ok"] else r.get("reason", "异常")
        print(f"| {r['name']} | {state} |")
    for c in chan_rows:
        print(f"| {c['file']} 通道 | {c.get('channel', '?')} (off={c.get('off_channel', 0)}) |")
    print("")
    print("**结论**:", "全部正常" if not problems else f"发现 {len(problems)} 个问题（不自动处理）")
