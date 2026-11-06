#!/usr/bin/env python3
"""多 CDN 源守门工具（本机运行，不进 CI）。

目的：点播卡顿的主因是播放带宽不足，且 165 个站点背后只落在 6 个源站主域上。
本脚本从多个上游订阅里找「播放地址落在其它主域、且实测更快」的直连 CMS，
只有证据足够硬才允许把站点写进 vod_fast_sites.json 供 update_daily.py 合入主配置。

为什么不放进每日 CI：
  CI 跑在海外机房，对国内采集 CDN 系统性测不准（直播线路误杀就是这么来的）。
  带宽判定必须在国内网络做，CI 只负责读取已验证的结果并做健康记录。

门槛（全部 PASS 才写入，任一 FAIL 则零写入）：
  1. 测通的新主域 >= 3
  2. 两轮独立采样中位带宽均 >= 基线 2 倍
  3. 两轮结论一致（差异 < 30%）
  4. 至少一条线路 >= 3000 kbps（能撑 1080p）

用法：python scripts/expand_cdn.py
"""
import concurrent.futures as cf
import datetime as dt
import json
import os
import random
import re
import socket
import ssl
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
      "Accept-Encoding": "identity"}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

REPORT = "vod_cdn_report.json"
FAST = "vod_fast_sites.json"
GH = "https://gh-proxy.com/"
RAW = GH + "https://raw.githubusercontent.com/"
SPARE_SLUG = urllib.parse.quote("单仓聚合.json")
UPSTREAMS = [
    ("王二小", "https://9280.kstore.space/wex.json"),
    ("小马", "https://szyyds.cn/tv/x.json"),
    ("小盒子", "http://xhztv.top/xhz"),
    ("Lightconer单仓", RAW + "Lightconer/tvbox-ysc-config/main/output/" + SPARE_SLUG),
    ("rtian001单仓", RAW + "rtian001/tvbox/main/output/" + SPARE_SLUG),
]
SAMPLE_PER_HOST = 3
SAMPLE_BYTES = 262144
BASELINE_FALLBACK = 872          # 2026-11-06 本机 40 条实测中位，仅作动态基线失败时兜底
# 历史多次实测见过的采集 CDN 主域。动态基线会随当天片单漂移（同一批站每次
# 返回的影片不同，抽到的主域就不同），这里保留一份稳定全集用于交叉核对，
# 避免把「今天没抽到、明天抽到了」的老域名误当成新源。
KNOWN_HOSTS = {
    "tyyszywvod2.com", "huyall.com", "hhuus.com", "rstu6.com", "lzcdn27.com",
    "tscjzy.com", "baofeng9.com", "dytt-see.com", "jisuzyv.com", "xluuss.com",
    "taopianplay1.com", "bfvvs.com", "tpvod.com", "maowushi.com",
}


def safe_url(u):
    return urllib.parse.quote(u, safe=":/?&=%#+,;@!$*'()-._~[]%")


def main_domain(u):
    """归一化到主域：v14.rstu6.com -> rstu6.com。

    踩过的坑：早期用完整主机名跟归一化后的名字比，把 v14.rstu6.com / rstu6.com
    当成两个不同 CDN，误报 11 个「新域名」，实际只有 7 个。比较必须同尺度。"""
    h = urllib.parse.urlparse(u).netloc.lower().split(":")[0]
    p = h.split(".")
    return ".".join(p[-2:]) if len(p) >= 2 else h


def fetch(url, timeout=12, n=6 * 1024 * 1024):
    try:
        req = urllib.request.Request(safe_url(url), headers=UA)
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            b = r.read(n)
            if r.headers.get("Content-Encoding") == "gzip":
                try:
                    import gzip
                    b = gzip.decompress(b)
                except Exception:
                    pass
        return b.decode("utf-8", "ignore")
    except Exception:
        return ""


def relaxed(txt):
    """TVBox 配置常见 BOM + // 注释 + 尾逗号，直接 json.loads 会误判成坏源。"""
    txt = txt.lstrip("\ufeff").strip()
    lines = [l for l in txt.splitlines() if not l.lstrip().startswith(("//", "#"))]
    txt = re.sub(r",\s*([}\]])", r"\1", "\n".join(lines))
    try:
        return json.loads(txt)
    except Exception:
        return None


def playlist_of(api):
    base = api.split("?")[0].rstrip("/")
    return base + ("&ac=videolist&pg=1" if "?" in base else "?ac=videolist&pg=1")


def play_urls(txt):
    try:
        obj = json.loads(txt)
    except Exception:
        return []
    out = []
    for item in (obj.get("list") or [])[:4]:
        for m in re.findall(r'https?://[^\s"\'$,]+\.m3u8[^\s"\'$,]*',
                            str(item.get("vod_play_url") or "")):
            out.append(m)
    return list(dict.fromkeys(out))[:8]


def first_seg(u):
    txt = fetch(u, timeout=9, n=200000)
    if not txt.startswith("#EXTM3U"):
        return None
    base = u
    if "#EXT-X-STREAM-INF" in txt:
        subs = [urllib.parse.urljoin(u, l.strip()) for l in txt.splitlines()
                if l.strip() and not l.startswith("#")]
        if not subs:
            return None
        base = subs[0]
        txt = fetch(base, timeout=9, n=200000)
    segs = [urllib.parse.urljoin(base, l.strip()) for l in txt.splitlines()
            if l.strip() and not l.startswith("#") and ".m3u8" not in l.lower()]
    segs += [urllib.parse.urljoin(base, m) for m in re.findall(r'URI="([^"]+)"', txt)]
    segs = [s for s in segs if ".m3u8" not in s.lower()]
    return segs[0] if segs else None


def speed(seg):
    """首分片前 256KB 下载带宽 kbps。绝对数值受 TCP 慢启动影响会偏低，
    但基线与候选用同一方法、同一台机器、同一时间窗，相对比较成立。"""
    if not seg:
        return 0
    t0, got = time.time(), 0
    try:
        req = urllib.request.Request(safe_url(seg), headers=UA)
        with urllib.request.urlopen(req, timeout=10, context=ctx) as f:
            while got < SAMPLE_BYTES:
                c = f.read(65536)
                if not c:
                    break
                got += len(c)
    except Exception:
        return 0
    dt = time.time() - t0
    return round(got * 8 / dt / 1000) if dt > 0 and got >= 8192 else 0


def median(vals):
    s = sorted(vals)
    return s[len(s) // 2] if s else 0


def now():
    return (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
            + dt.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


# ---------- 1. 动态基线：当前主配置实际落在哪些源站主域、有多快 ----------
print("== 1. 现有配置基线 ==")
try:
    cfg = json.load(open("config_all.json", encoding="utf-8"))
except Exception:
    cfg = {}
base_apis = [str(s.get("api") or "") for s in cfg.get("sites", [])
             if str(s.get("api") or "").startswith("http")]
base_apis = list(dict.fromkeys(a.split("?")[0].rstrip("/").lower() for a in base_apis))
with cf.ThreadPoolExecutor(12) as ex:
    base_txts = list(ex.map(lambda a: fetch(playlist_of(a), timeout=10, n=600000), base_apis))
base_urls = [u for t in base_txts for u in play_urls(t)]
base_hosts = {main_domain(u) for u in base_urls}
print(f"  直连 API {len(base_apis)} 个 | 抽到播放地址 {len(base_urls)} 条 | 源站主域 {len(base_hosts)} 个")
print(f"  主域分布: {dict(Counter(main_domain(u) for u in base_urls).most_common(8))}")

random.Random(1106).shuffle(base_urls)
with cf.ThreadPoolExecutor(10) as ex:
    base_segs = list(ex.map(first_seg, base_urls[:20]))
with cf.ThreadPoolExecutor(10) as ex:
    base_ks = [k for k in ex.map(speed, base_segs) if k > 0]
baseline = median(base_ks) or BASELINE_FALLBACK
print(f"  基线带宽中位 {baseline} kbps（样本 {len(base_ks)} 条"
      f"{'，用兜底值' if not base_ks else ''}）")

# ---------- 2. 上游候选 ----------
print("\n== 2. 拉取上游订阅 ==")
cfgs = []
if os.path.exists("spare_juhe.json"):
    try:
        cfgs.append(("本仓镜像spare", json.load(open("spare_juhe.json", encoding="utf-8"))))
        print("  本仓镜像spare: 本地")
    except Exception:
        pass
for name, u in UPSTREAMS:
    t0 = time.time()
    txt = fetch(u, timeout=20)
    obj = relaxed(txt) if txt else None
    if isinstance(obj, dict) and "urls" in obj and "sites" not in obj:
        for su in obj["urls"][:3]:
            su = su.get("url") if isinstance(su, dict) else su
            o = relaxed(fetch(str(su), timeout=15))
            if isinstance(o, dict) and o.get("sites"):
                obj = o
                break
    if isinstance(obj, dict) and obj.get("sites"):
        cfgs.append((name, obj))
        print(f"  {name}: {len(obj['sites'])} 站 / {time.time()-t0:.1f}s")
    else:
        print(f"  {name}: 拉不到或解析失败 / {time.time()-t0:.1f}s")

cand, seen = [], set()
for src, c in cfgs:
    for s in c.get("sites", []):
        api = str(s.get("api") or "")
        if not api.startswith("http") or str(s.get("type")) != "1":
            continue
        k = api.split("?")[0].rstrip("/").lower()
        if k in seen:
            continue
        seen.add(k)
        cand.append({"src": src, "site": s, "api": api})
print(f"  候选直连 CMS（去重）: {len(cand)} 个")

with cf.ThreadPoolExecutor(12) as ex:
    ctexts = list(ex.map(lambda c: fetch(playlist_of(c["api"]), timeout=10, n=600000), cand))
for c, t in zip(cand, ctexts):
    c["urls"] = play_urls(t)

# ---------- 3. 只保留「播放地址落在基线主域之外」的站点 ----------
new_by_host = defaultdict(list)
new_sites = []
for c in cand:
    nu = [u for u in c["urls"] if main_domain(u) not in base_hosts and ".m3u8" in u]
    if not nu:
        continue
    c["new_urls"] = nu
    new_sites.append(c)
    for u in nu:
        new_by_host[main_domain(u)].append(u)
print(f"\n== 3. 新主域 {len(new_by_host)} 个 / 能提供新主域的站点 {len(new_sites)} 个 ==")
print("  ", sorted(new_by_host))

ipmap = {}
for h in new_by_host:
    try:
        ipmap[h] = sorted({a[4][0] for a in socket.getaddrinfo(h, None)})
    except Exception:
        ipmap[h] = []
print("  DNS 解析（防同批机器马甲）:",
      {h: (v or "解析失败") for h, v in ipmap.items()})

# ---------- 4. 两轮独立带宽采样 ----------
sample = []
for h, urls in sorted(new_by_host.items()):
    uu = list(urls)
    random.Random(7).shuffle(uu)
    for u in uu[:SAMPLE_PER_HOST]:
        sample.append((h, u))
print(f"\n== 4. 测速样本 {len(sample)} 条 ==")
with cf.ThreadPoolExecutor(10) as ex:
    segs = list(ex.map(lambda x: first_seg(x[1]), sample))


def round_test(tag, order):
    with cf.ThreadPoolExecutor(10) as ex:
        ks = list(ex.map(speed, order))
    good = [k for k in ks if k > 0]
    per = defaultdict(list)
    for (h, _u), k in zip(sample, ks):
        if k > 0:
            per[h].append(k)
    print(f"  {tag}: 成功 {len(good)}/{len(ks)} | 中位 {median(good)} kbps | "
          f"最高 {max(good) if good else 0} kbps")
    return {"good": good, "per": {h: median(v) for h, v in per.items()}, "ks": ks}


r1 = round_test("第1轮", segs)
time.sleep(3)
r2 = round_test("第2轮(反向序独立复测)", list(reversed(segs)))
m1, m2 = median(r1["good"]), median(r2["good"])
hosts_ok = len({h for (h, _u), k in zip(sample, r1["ks"]) if k > 0})

checks = {
    "新主域>=3且测通>=3": hosts_ok >= 3,
    "两轮中位均>=基线2倍": m1 >= baseline * 2 and m2 >= baseline * 2,
    "两轮结论一致(差<30%)": m1 > 0 and m2 > 0 and abs(m1 - m2) / max(m1, m2) < .3,
    "至少1条>=3000kbps": max(r1["good"] + r2["good"] or [0]) >= 3000,
}
passed = all(checks.values())
print("\n== 5. 门槛判定 ==")
for k, v in checks.items():
    print(f"  [{'PASS' if v else 'FAIL'}] {k}")
print("  总判定:", "PASS 允许写入" if passed else "FAIL 零写入（按纪律不硬凑）")

report = {
    "probed_at": now() + " (UTC+8)",
    "network": "本机国内网络；CI 海外机房不可用于带宽判定",
    "baseline": {"median_kbps": baseline, "samples": len(base_ks),
                 "hosts": sorted(base_hosts),
                 "host_distribution": dict(Counter(main_domain(u) for u in base_urls).most_common(10)),
                 "top2_share": (sum(c for _, c in Counter(main_domain(u) for u in base_urls).most_common(2))
                                / max(1, len(base_urls)))},
    "upstreams": [n for n, _ in cfgs],
    "candidate_cms": len(cand),
    "new_hosts": sorted(new_by_host),
    "new_vs_known_hosts": sorted(set(new_by_host) - KNOWN_HOSTS),
    "known_hosts_total": len(KNOWN_HOSTS),
    "baseline_note": "动态基线随当天片单抽样漂移；与 KNOWN_HOSTS 交叉核对，"
                     "带宽门槛为与门硬约束，单项漂移不会导致误纳入",
    "new_host_dns": ipmap,
    "sample_lines": len(sample),
    "round1": {"median_kbps": m1, "ok": len(r1["good"]), "per_host": r1["per"],
               "max_kbps": max(r1["good"] or [0])},
    "round2": {"median_kbps": m2, "ok": len(r2["good"]), "per_host": r2["per"],
               "max_kbps": max(r2["good"] or [0])},
    "checks": checks,
    "passed": passed,
    "verdict": ("达标，已写入 vod_fast_sites.json" if passed else
                "未达标：新主域带宽与现有基线无显著差异，或解析不稳定；不引入任何站点"),
}
json.dump(report, open(REPORT, "w", encoding="utf-8", newline="\n"),
          ensure_ascii=False, indent=2)
print(f"  诊断报告 -> {REPORT}")

if passed:
    sites, seen_k = [], set()
    for c in new_sites:
        s = dict(c["site"])
        k = "fast_" + str(s.get("key") or s.get("name") or "")[:24]
        if k in seen_k:
            continue
        seen_k.add(k)
        s["key"] = k
        s["name"] = "⚡" + str(s.get("name") or "")[:24]
        sites.append(s)
    json.dump({"verified_at": now(), "passed": True,
               "baseline_median_kbps": baseline,
               "measured_median_kbps": [m1, m2],
               "new_hosts": sorted(new_by_host), "sites": sites},
              open(FAST, "w", encoding="utf-8", newline="\n"),
              ensure_ascii=False, indent=2)
    print(f"  ✅ 已写入 {FAST}（{len(sites)} 站），下次每日更新自动合入主配置")
else:
    print(f"  ⛔ 未写 {FAST}，主配置保持零变化")
print("DONE")
