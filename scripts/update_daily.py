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
import urllib.request

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
UA = {"User-Agent": "okhttp/3.12.0"}


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


def playable(u):
    try:
        b = get(u, timeout=7, n=600)
        return b[:7] == b"#EXTM3U" or b[:4] == b"\x47\x40\x00\x10"
    except Exception:
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


order = {"央视": 0, "卫视": 1, "其他": 2}
lines = ["#EXTM3U"]
for c, u in sorted(verified, key=lambda x: (order[grp(x[0])], x[0])):
    lines.append(f'#EXTINF:-1 tvg-name="{c}" group-title="{grp(c)}",{c}')
    lines.append(u)
open("live_verified.m3u", "w", encoding="utf-8").write("\n".join(lines) + "\n")
print("live_verified.m3u 已生成")
print("DONE")
