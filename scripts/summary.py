#!/usr/bin/env python3
"""把 health.json 渲染成 GitHub Step Summary 的 markdown 表格行。"""
import json

try:
    h = json.load(open("health.json", encoding="utf-8"))
except FileNotFoundError:
    print("health.json 不存在（本次运行可能提前中止），无摘要")
    raise SystemExit(0)

jar = h["jar"]
print(f"| spider jar | {jar['size_mb']} MB / md5 `{jar['md5'][:12]}` / 与上游声明一致={jar['declared_match']} / 今日更新={jar['changed']} |")
st = h["sites"]
print(f"| 站点 | {st['total']} 个（直连 CMS {st['cms']} + spider {st['spider']}） |")
dead = st.get("cms_dead_today") or []
if dead:
    print(f"| 今日剔除直连站 | {'、'.join(dead)} |")
ps = h["parses"]
print(f"| 解析器 | {ps['alive']}/{ps['total']} 实测可用 |")
lv = h["live"]
print(f"| 直播 | {lv['channels']} 频道 / {lv['lines']} 线路，带宽中位 {lv['median_kbps']} kbps，慢线标记 {lv['slow_tagged']} |")
vc = h.get("vod_cdn") or {}
if vc.get("probed"):
    share = vc.get("top2_host_share") or 0
    verdict = "达标" if vc.get("passed") else "未达标(零写入)"
    print(f"| 点播 CDN | 基线 {vc.get('baseline_median_kbps')} kbps / 前2主域占比 {share:.0%} / "
          f"新源探测 {verdict}（新主域 {vc.get('new_hosts')} 个）/ 加速站生效 {vc.get('fast_sites_active')} |")
lt = h.get("lite") or {}
if lt.get("enabled"):
    age = lt.get("evidence_age_days")
    age_txt = f"{age:.1f}天前" if isinstance(age, (int, float)) else "未知"
    verdict = "达标" if lt.get("passed") else "未达标(配置零写入)"
    hs = lt.get("holdout_stations") or {}
    hold_txt = (f"留出集 {sum(1 for v in hs.values() if v >= 2)}/{len(hs)} 词覆盖"
                if hs else "留出集 无数据")
    print(f"| 精简版 | 证据 {age_txt} / 能出片站 {lt.get('keep_sites')}/{lt.get('probe_sites')} / "
          f"可用解析器 {lt.get('keep_parses')} / 0命中 {len(lt.get('zero_hit_sites') or [])} 个 / "
          f"{hold_txt} / 门槛 {verdict} / 今日重建={lt.get('config_ok', False)} |")
print(f"| EPG | {h['epg']} |")
print("")
print(f"更新时间（UTC+8）：{h['updated']}")
