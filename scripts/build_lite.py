#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
config_lite.json 生成与维护（方案 A：精简版，可回滚）

为什么做这个
------------
165 个站点里 142 个是 spider 站，且实测 131 条播放地址只落在 6 个采集 CDN 主域上
（前 2 个占 69%）——绝大多数站点是同一批后端的「门面」。本机 8 关键词实测：
22 个 type=1 直连 CMS 去重后 18 个，其中 9 个完全搜不到片，确定能出片的只有 9 个。
精简版只保留「实测能搜到片」的直连站 + 「实测可用」的解析器，
直播 / EPG / spider jar / flags / doh / rules / ads 与主配置完全相同。

几种模式
--------
probe   （默认，在本机国内网络跑）：真实关键词搜索实测 + 解析器实测，
        全部门槛通过才覆盖 config_lite.json；任一不过则**配置零写入**，保留旧版本。
        本机结果是「硬基线」，优先级高于 CI。
rebuild （兜底）：不做任何网络判定，只读取 7 天内有效证据，从当天最新的
        config_all.json 重建精简版，让它跟上每天的直播 / jar / EPG 更新。
auto    （CI 每天，见 scripts/lite_auto.py）：CI 实测 + 衰减演化 + 双源与门。

CI 侧判定的边界（2026-11-06 探路后修正）
----------------------------------------
原判断是「海外机房测国内采集站不可信」，探路（ci_probe_result.json）证明对
**点播采集站 API** 这个判断是错的：CI 请求成功率 83.3%、TTFB 中位 233ms、
本机 10 个活站召回 100%、零误杀。但方向相反的风险是真的——CI 会「误纳」：
把本机 0/8 的「非凡」「尤酷」判成 8/8、7/8，海外可达 ≠ 用户家里能用。
所以现在 CI 每天实测，但用双源与门：本机证据是硬基线，CI 只有权衰减淘汰，
**无权自动加进本机判死的站**（locked_out）。

注意这不推翻直播的结论：直播 IPTV 地方台受地域/运营商限制，CI 系统性不可达
（频道 347→332→322，本机复验 14/14 可播）。点播是公网 CDN，两者性质不同。

用法
----
    python scripts/build_lite.py              # 本机：权威实测 + 生成
    python scripts/build_lite.py --rebuild   # 兜底：只重建，不联网判定
    python scripts/lite_auto.py              # CI 每天：全自动衰减维护
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import gzip
import json
import os
import ssl
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
# 用浏览器 UA：采集站对 okhttp/3.12 常返回空片单，本机实测口径与 vod_probe.py 一致
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
      "Accept-Encoding": "identity"}

KW = ["流浪地球", "狂飙", "三体", "庆余年", "繁花", "哪吒", "封神", "长安十二时辰"]
# 留出集：探测选站时绝不使用的另一批词，用来验证「精简后内容覆盖没塌」不是
# 在训练词上过拟合。每次刷新证据都会自动跑一遍，结果与门槛一起写进证据文件。
HOLDOUT_KW = ["甄嬛传", "斗罗大陆", "周处除三害", "隐秘的角落",
              "满江红", "奥本海默", "鱿鱼游戏", "山海情"]
PARSER_TEST = "https://vip.ffzy-play.com/share/20240101/1234/index.m3u8"

CFG_ALL = "config_all.json"
CFG_LITE = "config_lite.json"
EVIDENCE = "lite_probe.json"

MIN_HIT = 4               # 8 个关键词里至少命中 4 个，才算「能出片」
MIN_SITES = 5             # 达标站下限
MIN_PARSES = 3            # 可用解析器下限
MIN_KW_STATIONS = 2       # 每个关键词至少要有 2 个达标站能出片（单站挂了不至于没片）
EVIDENCE_TTL_DAYS = 7
SHRINK_GUARD = 0.5        # 达标站骤降超过 50% 时，需连续两次观测才允许覆盖


# ---------- 基础工具 ----------
def now8():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=8)


def stamp():
    return now8().strftime("%Y-%m-%d %H:%M:%S")


def safe(u):
    """中文/空格 URL 必须转码，否则 urllib 直接 UnicodeEncodeError（踩过）。"""
    return urllib.parse.quote(u, safe=":/?&=%#+,;@!$*'()-._~[]")


def fetch(u, timeout=9, n=300000):
    t0 = time.time()
    try:
        req = urllib.request.Request(safe(u), headers=UA)
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            b = r.read(n)
            if r.headers.get("Content-Encoding") == "gzip":
                try:
                    b = gzip.decompress(b)
                except Exception:
                    pass
        return {"text": b.decode("utf-8", "ignore"),
                "ttfb": round((time.time() - t0) * 1000), "err": ""}
    except Exception as e:
        return {"text": "", "ttfb": 0, "err": type(e).__name__}


def load_json(path):
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return None


def write_json(path, obj, indent=None):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=indent,
                  separators=None if indent else (",", ":"))
        f.write("\n")


def norm_api(api):
    """按 API 主路径去重——同一采集站常在配置里挂好几个别名。"""
    return str(api).split("?")[0].rstrip("/").lower()


# ---------- 探测 ----------
def collect_candidates(cfg):
    out, seen = [], set()
    for s in cfg.get("sites", []):
        api = str(s.get("api") or "")
        if str(s.get("type")) != "1" or not api.startswith("http"):
            continue
        k = norm_api(api)
        if k in seen:
            continue
        seen.add(k)
        out.append({"key": str(s.get("key") or ""),
                    "name": str(s.get("name") or "")[:40],
                    "api": api, "norm": k})
    return out


def one_search(job):
    """能不能搜到片，才是「站点可用」的真判据；只看响应头会把摆设站全放过。"""
    c, kw = job
    u = c["norm"] + "?ac=videolist&wd=" + urllib.parse.quote(kw)
    r = fetch(u, timeout=9, n=300000)
    n = 0
    if r["text"]:
        try:
            obj = json.loads(r["text"])
            lst = obj.get("list") or []
            n = len(lst) if isinstance(lst, list) else 0
            if not n and isinstance(obj.get("total"), int):
                n = min(obj["total"], 99)
        except Exception:
            # 部分站返回 XML / 被截断的 JSON，做宽松判定，别把活站误判成死站
            low = r["text"][:200000].lower()
            if "vod_play_url" in low or "<video" in low:
                n = 1
    return c["norm"], kw, n, r["ttfb"], r["err"]


def one_parse(p):
    u = str(p.get("url") or "")
    if not u.startswith("http"):
        return None
    tpl = u if ("url=" in u or "v=" in u) else u + ("&url=" if "?" in u else "?url=")
    r = fetch(tpl + urllib.parse.quote(PARSER_TEST, safe=""), timeout=9, n=80000)
    low = r["text"].lower()
    if r["err"] or not r["ttfb"]:
        v = "超时/失败"
    elif any(x in low for x in ("域名过期", "已过期", "已停止", "not found", "已暂停")):
        v = "已停用"
    elif any(x in low for x in (".m3u8", "video", "player", "iframe", "var url")):
        v = "可用"
    else:
        v = "无有效内容"
    return {"name": str(p.get("name") or ""), "url": u,
            "verdict": v, "ttfb": r["ttfb"], "err": r["err"]}


def evidence_age_days(ev):
    try:
        return (now8() - dt.datetime.strptime(ev["probed_at"], "%Y-%m-%d %H:%M:%S")).total_seconds() / 86400
    except Exception:
        return 1e9


# ---------- 重建（本机 probe 通过后、CI 每天都会调用） ----------
def rebuild(cfg, ev):
    """从最新主配置派生精简版。任何异常都保留旧文件，绝不写空壳。"""
    if not isinstance(ev, dict) or ev.get("passed") is not True:
        print("  证据未达标或缺失 → config_lite.json 保持原样")
        return False
    age = evidence_age_days(ev) * 86400
    if not (-3600 <= age <= EVIDENCE_TTL_DAYS * 86400):
        print(f"  证据已过期（{evidence_age_days(ev):.1f} 天）→ 保留旧配置；"
              f"请在本机重跑 python scripts/build_lite.py")
        return False

    ks = ev.get("keep_sites") or []
    kp = ev.get("keep_parses") or []
    if len(ks) < MIN_SITES or len(kp) < MIN_PARSES:
        print("  证据名单过短 → 保留旧配置")
        return False

    # 用 API 主路径匹配：主配置每天重建，key 可能被上游改动，api 才是稳定标识
    by_norm = {}
    for s in cfg.get("sites", []):
        api = str(s.get("api") or "")
        if api.startswith("http"):
            by_norm.setdefault(norm_api(api), s)
    lite_sites = []
    for k in ks:
        s = by_norm.get(k.get("norm"))
        if s:
            lite_sites.append(dict(s))

    order = {p["url"]: i for i, p in enumerate(kp)}
    lite_parses, seen = [], set()
    for p in cfg.get("parses", []):
        u = str(p.get("url") or "")
        if u in order and u not in seen:
            seen.add(u)
            lite_parses.append(dict(p))
    lite_parses.sort(key=lambda p: order.get(str(p.get("url") or ""), 9999))

    if len(lite_sites) < MIN_SITES or len(lite_parses) < MIN_PARSES:
        print(f"  主配置只匹配到 {len(lite_sites)} 站 / {len(lite_parses)} 解析器 → 保留旧配置")
        return False
    if not cfg.get("lives") or not cfg.get("spider"):
        print("  主配置缺直播或 spider → 保留旧配置")
        return False

    # 按主配置原顺序重建：JSON 键顺序不影响解析，但保持一致便于 diff 人工核对
    out = {}
    for k, v in cfg.items():
        if k == "sites":
            out[k] = lite_sites
        elif k == "parses":
            out[k] = lite_parses
        else:
            out[k] = v
    write_json(CFG_LITE, out)
    print(f"  config_lite.json 已生成：{len(lite_sites)} 站 / {len(lite_parses)} 解析器 / "
          f"{len(out['lives'])} 直播源（其余字段与主配置一致）")

    # 回填 health.lite，让 CI 摘要能看到精简版今天是否重建成功。
    # 本机单独跑时 health 里可能还没有 lite 段，这里自建一份基础结构，
    # CI 里 update_daily.py 会先写标准结构，两边字段兼容。
    h = load_json("health.json")
    if isinstance(h, dict):
        lite = h.get("lite")
        if not isinstance(lite, dict):
            lite = {}
        # 基础字段每次都按当前证据刷新：本机单独跑时 health.lite 可能是上一次的旧值，
        # CI 里 update_daily.py 也会先写一份标准结构，两处用同一份证据，覆盖无害。
        lite.update({
            "enabled": True,
            "probed_at": ev.get("probed_at"),
            "evidence_age_days": round(evidence_age_days(ev), 2),
            "passed": ev.get("passed"),
            "probe_sites": len(ev.get("site_rows") or []),
            "keep_sites": len(ks),
            "keep_parses": len(kp),
            "zero_hit_sites": ev.get("zero_hit_sites") or [],
            "keyword_stations": ev.get("keyword_stations") or {},
            "holdout_stations": ev.get("holdout_stations") or {},
        })
        lite.update({"rebuilt_at": stamp(), "config_sites": len(lite_sites),
                     "config_parses": len(lite_parses), "config_ok": True})
        h["lite"] = lite
        write_json("health.json", h, indent=2)
    return True


# ---------- 本机实测 ----------
def do_probe(cfg):
    cands = collect_candidates(cfg)
    print(f"== 精简版探测：{len(cands)} 个去重直连 CMS × {len(KW)} 关键词 = {len(cands)*len(KW)} 次搜索 ==")
    jobs = [(c, kw) for c in cands for kw in KW]
    holdout_jobs = [(c, kw) for c in cands for kw in HOLDOUT_KW]
    with cf.ThreadPoolExecutor(16) as ex:
        res = list(ex.map(one_search, jobs))
        print(f"== 留出集复验：{len(HOLDOUT_KW)} 个全新关键词 = {len(holdout_jobs)} 次搜索 ==")
        holdout_res = list(ex.map(one_search, holdout_jobs))

    hold_hits, hold_cover = defaultdict(int), defaultdict(set)
    for norm, kw, n, _ms, _err in holdout_res:
        if n > 0:
            hold_hits[norm] += 1
            hold_cover[kw].add(norm)

    per = defaultdict(lambda: {"hit": 0, "items": 0, "ms": [], "errs": 0})
    cover = defaultdict(set)
    for norm, kw, n, ms, err in res:
        d = per[norm]
        if err:
            d["errs"] += 1
        if n > 0:
            d["hit"] += 1
            d["items"] += n
            cover[kw].add(norm)
        if ms:
            d["ms"].append(ms)

    site_rows = []
    for c in cands:
        d = per[c["norm"]]
        ms = sorted(d["ms"])
        site_rows.append({**c, "hit": d["hit"], "items": d["items"],
                          "ttfb_median": ms[len(ms) // 2] if ms else 0,
                          "errors": d["errs"]})
    site_rows.sort(key=lambda s: (-s["hit"], -s["items"], s["ttfb_median"] or 99999))

    parses_in, seen_u = [], set()
    for p in cfg.get("parses", []):
        u = str(p.get("url") or "")
        if not u.startswith("http") or u in seen_u:
            continue
        seen_u.add(u)
        parses_in.append(p)
    print(f"== 解析器实测：{len(parses_in)} 个去重 jx 接口 ==")
    with cf.ThreadPoolExecutor(12) as ex:
        prs = [x for x in ex.map(one_parse, parses_in) if x]

    keep_sites = [s for s in site_rows if s["hit"] >= MIN_HIT]
    keep_parses = sorted([p for p in prs if p["verdict"] == "可用"],
                         key=lambda p: (p["ttfb"] or 99999))
    zero_hit = [s["name"] for s in site_rows if s["hit"] == 0]
    keep_norms = {s["norm"] for s in keep_sites}
    stations = {k: len(v & keep_norms) for k, v in cover.items()}
    hold_stations = {k: len(hold_cover[k] & keep_norms) for k in HOLDOUT_KW}
    dropped_norms = [s["norm"] for s in site_rows if s["hit"] < MIN_HIT]
    req_ok = sum(1 for _n, _k, _c, ms, _e in res if ms > 0)
    errors = defaultdict(int)
    for _n, _k, _c, _ms, e in res:
        if e:
            errors[e] += 1

    # 防抖：本机也可能瞬断。达标站骤降 >50% 时，连续两次观测才允许换名单。
    prev = load_json(EVIDENCE) or {}
    prev_n = len(prev.get("keep_sites") or [])
    streak = int(prev.get("shrink_streak") or 0)
    shrink = prev_n > 0 and len(keep_sites) < prev_n * SHRINK_GUARD
    streak = streak + 1 if shrink else 0

    checks = {
        "网络整体正常": req_ok >= len(jobs) * 0.5 and any(s["hit"] > 0 for s in site_rows),
        "能出片站点数≥%d" % MIN_SITES: len(keep_sites) >= MIN_SITES,
        "关键词全覆盖(每词≥%d站)" % MIN_KW_STATIONS:
            len(stations) == len(KW) and all(v >= MIN_KW_STATIONS for v in stations.values()),
        "留出集覆盖(每词≥%d站)" % MIN_KW_STATIONS:
            len(hold_stations) == len(HOLDOUT_KW) and all(v >= MIN_KW_STATIONS for v in hold_stations.values()),
        "可用解析器≥%d" % MIN_PARSES: len(keep_parses) >= MIN_PARSES,
        "站点骤降防抖": (not shrink) or streak >= 2,
    }
    passed = all(checks.values())

    print("\n== 各站实测（命中数 / 片单累计 / TTFB中位）==")
    for s in site_rows:
        flag = "保留" if s["hit"] >= MIN_HIT else ("0命中·淘汰" if s["hit"] == 0 else "不足·淘汰")
        print(f"  {s['hit']}/{len(KW)} 命中 | {s['items']:>4} 条 | {s['ttfb_median']:>5}ms | "
              f"{flag:<10} {s['name']}")
    vd = defaultdict(int)
    for p in prs:
        vd[p["verdict"]] += 1
    print(f"\n解析器判定: {dict(vd)}")
    print(f"请求成功 {req_ok}/{len(jobs)}，失败类型 {dict(errors) or '无'}")
    print(f"关键词可用站数: {stations}")
    print(f"留出集({len(HOLDOUT_KW)}个新词)每词可用站数: {hold_stations}")
    _kh = [hold_hits[n] for n in keep_norms]
    _dh = [hold_hits[n] for n in dropped_norms]
    print(f"留出集平均命中: 保留站 {sum(_kh)/max(len(_kh),1):.2f}/{len(HOLDOUT_KW)} vs "
          f"淘汰站 {sum(_dh)/max(len(_dh),1):.2f}/{len(HOLDOUT_KW)}")
    print(f"\n== 门槛（与门）==")
    for k, v in checks.items():
        print(f"  {'🟢 PASS' if v else '🔴 FAIL'}  {k}")
    print(f"结论: {'全过' if passed else '未全过'} | 保留 {len(keep_sites)} 站 / "
          f"{len(keep_parses)} 解析器 | 0 命中站 {len(zero_hit)} 个")

    ev = {
        "probed_at": stamp(),
        "source": "本机国内网络（权威基线）",
        "keywords": KW,
        "min_hit": MIN_HIT,
        "passed": passed,
        "checks": checks,
        "request_ok": req_ok,
        "request_total": len(jobs),
        "request_errors": dict(errors),
        "keep_sites": keep_sites,
        "keep_parses": keep_parses,
        "site_rows": site_rows,
        "parse_rows": prs,
        "zero_hit_sites": zero_hit,
        "keyword_stations": stations,
        "holdout_keywords": HOLDOUT_KW,
        "holdout_stations": hold_stations,
        "holdout_hits_per_site": {s["norm"]: hold_hits[s["norm"]] for s in site_rows},
        "shrink_streak": streak,
        "shrink_prev": prev_n,
    }
    # 证据文件始终写：它是观测记录（含失败原因与骤降计数），不是配置。
    write_json(EVIDENCE, ev)

    if passed:
        rebuild(cfg, ev)
    else:
        print("\n配置零写入：config_lite.json 保持原样，主配置 config_all.json 从未改动。")
    return passed


def main():
    ap = argparse.ArgumentParser(description="精简版配置生成与维护")
    ap.add_argument("--rebuild", action="store_true",
                    help="CI 模式：只读证据重建，不做任何网络判定")
    args = ap.parse_args()

    cfg = load_json(CFG_ALL)
    if not isinstance(cfg, dict) or not cfg.get("sites"):
        print("config_all.json 不可读或为空，退出")
        return 1

    if args.rebuild:
        ev = load_json(EVIDENCE)
        if not ev:
            print("lite_probe.json 不存在 → 保留旧 config_lite.json（首次请在本机跑 build_lite.py）")
            return 0
        rebuild(cfg, ev)
        # 证据过期是预期状态，不该让 workflow 失败
        return 0
    do_probe(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
