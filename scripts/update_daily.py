#!/usr/bin/env python3
"""每日自动更新：
1. 拉取 qist/tvbox 最新 jsm.json -> 修正 spider 为绝对路径 -> jsm_fixed.json
2. 拉取 vbskycn/iptv 最新 iptv4.m3u -> 逐频道验证 -> live_verified.m3u
仅当内容变化时才写文件（配合 workflow 的 git diff 判断是否提交）。
"""
import concurrent.futures as cf
import hashlib
import json
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
UA = {"User-Agent": "okhttp/3.12.0"}
UA_HDR = UA


def get(url, timeout=25, n=400000):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read(n)


def fetch_first(urls, timeout=25, n=400000):
    """多镜像依次尝试，返回第一个成功的内容"""
    last = None
    for u in urls:
        try:
            return get(u, timeout, n)
        except Exception as e:
            last = e
    raise RuntimeError(f"全部镜像失败: {last}")


# ---------- 1. qist jsm.json ----------
GAO_PROXY = "https://gh-proxy.com/"
RAW_QIST = "https://raw.githubusercontent.com/qist/tvbox/master/"
JAR_MD5_RE = re.compile(r"md5;([0-9a-f]{32})")

print("== 1. qist jsm.json ==")
raw = fetch_first([RAW_QIST + "jsm.json", GAO_PROXY + RAW_QIST + "jsm.json"])
jsm = json.loads(raw.decode("utf-8"))

jar_urls = [RAW_QIST + "jar/spider.jar", GAO_PROXY + RAW_QIST + "jar/spider.jar"]
jar = fetch_first(jar_urls, timeout=40)
md5 = hashlib.md5(jar).hexdigest()
jsm["spider"] = GAO_PROXY + RAW_QIST + "jar/spider.jar;md5;" + md5
# 修复站内相对路径
for s in jsm.get("sites", []):
    for f in ("api", "ext"):
        v = str(s.get(f) or "")
        if v.startswith("./"):
            s[f] = "https://qist.wyfc.qzz.io/" + v[2:]
json.dump(jsm, open("jsm_fixed.json", "w", encoding="utf-8"),
          ensure_ascii=False, separators=(",", ":"))
print(f"jsm_fixed.json: sites={len(jsm.get('sites', []))} jar_md5={md5[:12]}")

# ---------- 2. 直播逐频道验证 ----------
print("== 2. 直播源验证 ==")
IPTV_URLS = [
    "https://raw.githubusercontent.com/vbskycn/iptv/master/tv/iptv4.m3u",
    GAO_PROXY + "https://raw.githubusercontent.com/vbskycn/iptv/master/tv/iptv4.m3u",
    "https://ghproxy.net/https://raw.githubusercontent.com/vbskycn/iptv/master/tv/iptv4.m3u",
]
txt = fetch_first(IPTV_URLS, timeout=40).decode("utf-8", "ignore")

channels, name = [], None
for line in txt.splitlines():
    line = line.strip()
    if line.startswith("#EXTINF"):
        m = re.search(r",(.+)$", line)
        name = m.group(1).strip() if m else None
    elif line and not line.startswith("#") and name:
        channels.append((name, line))
        name = None
# 去重（按 URL）
seen, uniq = set(), []
for c, u in channels:
    if u not in seen:
        seen.add(u)
        uniq.append((c, u))
print(f"拉到 {len(uniq)} 个频道，开始逐个验证...")


def playable(u, attempt=2):
    """失败重试一次，降低网络抖动造成的误判"""
    for i in range(attempt):
        try:
            b = get(u, timeout=10 if i else 7, n=600)
            if b[:7] == b"#EXTM3U" or b[:4] == b"\x47\x40\x00\x10":
                return True
        except Exception:
            pass
    return False


verified = []
with cf.ThreadPoolExecutor(30) as ex:
    for (c, u), ok in zip(uniq, ex.map(playable, [u for _, u in uniq])):
        if ok:
            verified.append((c, u))
print(f"验证通过 {len(verified)}/{len(uniq)}")


def grp(n):
    n = n.replace(" ", "")
    if re.match(r"^(CCTV|CGTN|BRTV)", n):
        return "央视"
    if "卫视" in n:
        return "卫视"
    return "其他"


# ---------- 2b. 带宽测速 ----------
print("== 2b. 测速 ==")


def segs_of(txt, base):
    out = []
    for line in txt.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(urllib.parse.urljoin(base, line))
    for m in re.findall(r'URI="([^"]+)"', txt):
        out.append(urllib.parse.urljoin(base, m))
    return out


def measure(u):
    """返回 kbps（取首个分片前 256KB 计时）；失败返回 0"""
    try:
        txt = get(u, timeout=8, n=12000).decode("utf-8", "ignore")
    except Exception:
        return 0
    if not txt.startswith("#EXTM3U"):
        return 0
    base = u
    if "#EXT-X-STREAM-INF" in txt:
        subs = segs_of(txt, u)
        if not subs:
            return 0
        try:
            base = subs[0]
            txt = get(base, timeout=8, n=12000).decode("utf-8", "ignore")
        except Exception:
            return 0
    segs = [s for s in segs_of(txt, base) if not s.lower().endswith(".m3u8")]
    if not segs:
        return 0
    t1, got = time.time(), 0
    try:
        with urllib.request.urlopen(urllib.request.Request(segs[0], headers=UA_HDR),
                                    timeout=10, context=ctx) as r:
            while got < 262144:
                chunk = r.read(65536)
                if not chunk:
                    break
                got += len(chunk)
    except Exception:
        return 0
    dt = time.time() - t1
    if dt <= 0 or got < 8192:
        return 0
    return round(got * 8 / dt / 1000)


speed = {}
ts = time.time()
with cf.ThreadPoolExecutor(20) as ex:
    for (c, u), k in zip(verified, ex.map(lambda p: measure(p[1]), verified)):
        speed[u] = k
known = [v for v in speed.values() if v > 0]
print(f"测速完成 {len(known)}/{len(verified)} 条，用时 {time.time()-ts:.0f}s")
if known:
    known.sort()
    print(f"带宽中位数 {known[len(known)//2]} kbps | 最低 {known[0]} | 最高 {known[-1]}")

# 按频道归组，每个频道保留最快 2 条线路；同频道内慢于 150kbps 的线不保留
by_ch = {}
for c, u in verified:
    by_ch.setdefault(c.replace(" ", ""), []).append((c, u, speed.get(u, 0)))

usable_ok = len(known) >= len(verified) * 0.4   # 测速成功率过低则不参与筛选
final = []
for key, items in by_ch.items():
    if usable_ok:
        items.sort(key=lambda x: -x[2])
        best = items[0][2]
        keep = [it for it in items[:2] if it[2] >= max(150, best * 0.3)]
        final.extend(keep or [items[0]])
    else:
        final.extend(items[:1])
print(f"频道数 {len(by_ch)} → 输出线路 {len(final)} 条（每频道最多2条）")

# 保险：若本次结果比现有文件少 20% 以上，判定为网络抖动，保留旧文件不覆盖
prev = 0
try:
    prev = open("live_verified.m3u", encoding="utf-8").read().count("#EXTINF")
except FileNotFoundError:
    pass
if prev and len(final) < prev * 0.8:
    print(f"警告：本次仅 {len(final)} 条，远少于现有 {prev} 条，"
          f"判定为网络抖动，保留旧 live_verified.m3u 不覆盖")
else:
    order = {"央视": 0, "卫视": 1, "其他": 2}
    final.sort(key=lambda x: (order[grp(x[0])], x[0], -x[2]))
    lines = ["#EXTM3U"]
    seen_name = {}
    for c, u, k in final:
        n = seen_name.get(c, 0)
        seen_name[c] = n + 1
        label = c if n == 0 else f"{c}▪{n+1}"
        if 0 < k < 500:          # 带宽过低，标出来避免误点
            label += "（慢）"
        lines.append(f'#EXTINF:-1 tvg-name="{c}" group-title="{grp(c)}",{label}')
        lines.append(u)
    open("live_verified.m3u", "w", encoding="utf-8").write("\n".join(lines) + "\n")
    json.dump([[c, u, k] for c, u, k in final],
              open("live_speed.json", "w", encoding="utf-8"),
              ensure_ascii=False, separators=(",", ":"))
    print(f"live_verified.m3u 已生成（{len(final)} 条线路 / {len(by_ch)} 个频道，上次 {prev}）")

# ---------- 3. EPG 节目单源探测 ----------
print("== 3. EPG 探测 ==")
EPG_CANDS = [
    "https://epg.cdn.loc.cc/?ch={name}&date={date}",
    "https://epg.112114.xyz/?ch={name}&date={date}",
    "https://diyp.112114.xyz/?ch={name}&date={date}",
]
epg_url = ""
for cand in EPG_CANDS:
    try:
        probe = cand.replace("{name}", "CCTV1").replace("{date}", "20261103")
        d = json.loads(get(probe, timeout=15, n=60000).decode("utf-8"))
        if d.get("epg_data"):
            epg_url = cand
            print(f"EPG 可用: {cand}（CCTV1 {len(d['epg_data'])} 条节目）")
            break
    except Exception as e:
        print(f"EPG 不可用: {cand.split('/')[2]} ({type(e).__name__})")
if not epg_url:
    print("警告：所有 EPG 源都不可用，配置中将不带节目单")

LOGO = "https://logo.wyfc.qzz.io/{name}.png"

# ---------- 4. 合并成单仓配置 config_all.json ----------
print("== 4. 合并单仓配置 ==")
LIVE_URL = GAO_PROXY + "https://raw.githubusercontent.com/chenqq2026/Tvbox-public/main/live_verified.m3u"

# 自持的 8 个直连 CMS（基线，不依赖任何第三方）
try:
    direct = json.load(open("direct_cms.json", encoding="utf-8"))
    direct_sites = direct.get("sites", [])
    direct_parses = direct.get("parses", [])
except FileNotFoundError:
    direct_sites, direct_parses = [], []

merged_sites, used_keys, used_names = [], set(), set()

# 自持直连站优先（最稳，放前面）
for s in direct_sites:
    s = dict(s)
    s["key"] = "cms_" + str(s.get("key") or s.get("name"))[:24]
    if s["key"] in used_keys:
        continue
    used_keys.add(s["key"])
    used_names.add(s.get("name"))
    merged_sites.append(s)

# qist 站点追加，key/name 冲突时加后缀
for s in jsm.get("sites", []):
    s = dict(s)
    k = str(s.get("key") or s.get("name") or "")
    if not k:
        continue
    if k in used_keys:
        k = k + "_q"
    s["key"] = k
    used_keys.add(k)
    merged_sites.append(s)

# 解析器合并（按 name 去重，自持优先）
merged_parses, seen_p = [], set()
for p in direct_parses + jsm.get("parses", []):
    n = str(p.get("name") or "")
    if n and n not in seen_p:
        seen_p.add(n)
        merged_parses.append(p)

# 直播：验证过的 405 频道放第一位，qist 原有直播做备用
merged_lives = [{"name": "自建·已验证频道", "type": 0, "url": LIVE_URL,
                 "playerType": 2, "logo": LOGO}]
for l in jsm.get("lives", []):
    if str(l.get("url") or "").startswith("http"):
        merged_lives.append(l)

if epg_url:
    for l in merged_lives:
        l.setdefault("epg", epg_url)
        l.setdefault("logo", LOGO)

cfg_all = {
    "spider": jsm["spider"],
    "wallpaper": jsm.get("wallpaper", ""),
    "sites": merged_sites,
    "lives": merged_lives,
    "parses": merged_parses,
    "flags": jsm.get("flags", []),
    "doh": jsm.get("doh", []),
    "rules": jsm.get("rules", []),
    "ads": jsm.get("ads", []),
    "danmaku": jsm.get("danmaku", {}),
}
json.dump(cfg_all, open("config_all.json", "w", encoding="utf-8"),
          ensure_ascii=False, separators=(",", ":"))
print(f"config_all.json: sites={len(merged_sites)} "
      f"(自持{len(direct_sites)}+qist{len(jsm.get('sites', []))}) "
      f"parses={len(merged_parses)} lives={len(merged_lives)}")
print("DONE")
