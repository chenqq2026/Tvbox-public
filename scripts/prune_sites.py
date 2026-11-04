#!/usr/bin/env python3
"""剔除死站点（A方案·DNS 判定版）

为什么不直接用 HTTP 探活：本机流量全走代理，国内影视站普遍屏蔽机房/代理 IP，
HTTP 探测会把大量活站误判成死站；直连又完全不通。因此改用 DNS 判定：

- NXDOMAIN                     → 域名已不存在，真死，剔除
- 解析到 GFW 污染 IP            → 国内无法访问（用户在国内），剔除
- 解析到正常 IP                 → 保留（保守，可能仍需实际验证）
- 临时解析失败(EAI_AGAIN)       → 保留（保守）

输出 pruned_config.json
"""
import concurrent.futures as cf
import hashlib
import ipaddress
import json
import os
import re
import socket
import sys
import urllib.parse

GAO_PROXY = "https://gh-proxy.com/"
SKIP = ("githubusercontent", "gh-proxy", "jsdelivr", "github.com", "gitee.com",
        "ghproxy", "gitmirror", "127.0.0.1", "localhost", "servicewechat",
        "wyfc.qzz.io", "douban.com")

# GFW DNS 污染常见落地 IP（Facebook/Dropbox/Twitter 段等）
POISON_V4 = ("31.13.", "108.160.", "8.7.198.", "46.82.174.", "59.24.3.",
             "203.98.7.", "93.46.8.", "243.185.187.", "37.61.54.", "78.16.49.",
             "159.106.121.", "4.36.66.", "8.105.7.", "10.10.10.10",
             "0.0.0.0", "127.0.0.1")
POISON_V6 = ("2a03:2880", "2001::", "::1")

cfg = json.load(open("verified_config.json", encoding="utf-8"))
sites = cfg["sites"]
os.makedirs("cache_js", exist_ok=True)
print(f"原始站点: {len(sites)}")


def get(url, timeout=20, n=400000):
    q = urllib.parse.quote(url, safe=":/?&=%")
    req = urllib.request.Request(q, headers={"User-Agent": "okhttp/3.12.0"})
    ctx = __import__("ssl").create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = __import__("ssl").CERT_NONE
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read(n)


def js_local(url):
    fn = "cache_js/" + hashlib.md5(url.encode()).hexdigest() + ".js"
    if os.path.exists(fn) and os.path.getsize(fn) > 50:
        return open(fn, encoding="utf-8", errors="ignore").read()
    try:
        b = get(url)
        open(fn, "wb").write(b)
        return b.decode("utf-8", errors="ignore")
    except Exception:
        return ""


HOST_RE = re.compile(r"""host\s*[:=]\s*['"]([^'"]+)['"]""")
URL_RE = re.compile(r"""['"]?(https?://[^\s'"\\<>)\]}]+)""")


def ok_host(u):
    h = urllib.parse.urlparse(u).netloc.lower().split(":")[0]
    if not h or any(x in h for x in SKIP):
        return None
    return h


def rule_file(s):
    api = str(s.get("api") or "")
    ext = str(s.get("ext") or "")
    for v in (ext, api):
        if not v:
            continue
        if v.strip().startswith("{"):
            try:
                j = json.loads(v)
            except Exception:
                continue
            for vv in j.values():
                if isinstance(vv, str) and vv.split("?")[0].lower().endswith(".js"):
                    return vv
            continue
        if v.split("?")[0].lower().endswith(".js"):
            return v
    return None


def primary_host(s):
    if str(s.get("type")) == "1":
        api = str(s.get("api") or "")
        return ok_host(api) if api.startswith("http") else None
    rf = rule_file(s)
    if rf:
        txt = js_local(rf)
        for m in HOST_RE.findall(txt):
            h = ok_host(m)
            if h:
                return h
    ext = str(s.get("ext") or "")
    if ext.strip().startswith("{"):
        try:
            j = json.loads(ext)
            for vv in j.values():
                if isinstance(vv, str) and vv.startswith("http"):
                    h = ok_host(vv)
                    if h:
                        return h
        except Exception:
            pass
    for m in URL_RE.findall(ext + " " + str(s.get("api") or "")):
        h = ok_host(m)
        if h:
            return h
    return None


print("解析各站主域名...")
site_primary = {}
with cf.ThreadPoolExecutor(12) as ex:
    for s, h in zip(sites, ex.map(primary_host, sites)):
        site_primary[s.get("key")] = h
hs = sorted({h for h in site_primary.values() if h})
unknown = [k for k, v in site_primary.items() if not v]
print(f"主域名 {len(hs)} 个 | 无法判定主域名 {len(unknown)} 站（保守保留）")


def is_poison(ip):
    s = str(ip)
    if ":" in s:
        return any(s.lower().startswith(p) for p in POISON_V6)
    return any(s.startswith(p) for p in POISON_V4)


def dns_check(h):
    try:
        ips = {ai[4][0] for ai in socket.getaddrinfo(h, None)}
    except socket.gaierror as e:
        if e.errno in (-2, -5, 11001):
            return h, "NXDOMAIN", []
        return h, "TEMPFAIL", []
    except Exception:
        return h, "TEMPFAIL", []
    real = [ip for ip in ips if not is_poison(ip)]
    if real:
        return h, "OK", real[:2]
    return h, "POISONED", list(ips)[:2]


print("DNS 判定...")
status = {}
with cf.ThreadPoolExecutor(40) as ex:
    for h, st, ips in ex.map(dns_check, hs):
        status[h] = (st, ips)

cnt = {}
for h, (st, _) in status.items():
    cnt[st] = cnt.get(st, 0) + 1
print("域名判定分布:", cnt)

kept, dropped = [], []
for s in sites:
    h = site_primary.get(s.get("key"))
    if h is None:
        kept.append(s)
        continue
    st = status.get(h, ("TEMPFAIL", []))[0]
    if st in ("NXDOMAIN", "POISONED"):
        dropped.append((s, h, st))
    else:
        kept.append(s)

seen, final = set(), []
for s in kept:
    k = s.get("key")
    if k in seen:
        continue
    seen.add(k)
    final.append(s)

LIVE_URL = GAO_PROXY + "https://raw.githubusercontent.com/chenqq2026/Tvbox-public/main/live_verified.m3u"
EPG = "https://epg.cdn.loc.cc/?ch={name}&date={date}"
LOGO = "https://logo.wyfc.qzz.io/{name}.png"

# spider 指向本仓库镜像的 jar（与 update_daily.py 保持一致），不再指向 gao/qist 上游
GH_SELF = GAO_PROXY + "https://raw.githubusercontent.com/chenqq2026/Tvbox-public/main/"
spider_line = cfg["spider"]
if os.path.exists("jar/spider.jar"):
    spider_line = GH_SELF + "jar/spider.jar;md5;" + hashlib.md5(
        open("jar/spider.jar", "rb").read()).hexdigest()

out = {
    "spider": spider_line,
    "wallpaper": cfg.get("wallpaper", ""),
    "sites": final,
    "lives": [{"name": "自建·已验证频道", "type": 0, "url": LIVE_URL,
               "playerType": 2, "epg": EPG, "logo": LOGO}],
    "parses": cfg["parses"],
    "flags": cfg.get("flags", []),
    "doh": cfg.get("doh", []),
    "rules": cfg.get("rules", []),
    "ads": cfg.get("ads", []),
}
json.dump(out, open("pruned_config.json", "w", encoding="utf-8", newline="\n"),
          ensure_ascii=False, separators=(",", ":"))

dead_nx = sum(1 for _, _, st in dropped if st == "NXDOMAIN")
dead_po = sum(1 for _, _, st in dropped if st == "POISONED")
print(f"保留 {len(final)} 站 | 剔除 {len(dropped)} 站 "
      f"(域名不存在 {dead_nx} + DNS污染 {dead_po})")

# ---- 写入保护 ----
# 老 gao 配置里大量站点是 csp_ 内置源（没有 js 规则、ext 也不是域名），
# 这类站无法提取主域名，只能“保守保留”。一旦规则缓存缺失或上游改动，
# 剔除数就会掉到个位数，把已经剔干净的配置“恢复”成未剔版本 —— 属于自动退化。
# 因此：保留数明显多于上次时不覆盖；--report 模式下只报告。
REPORT_ONLY = "--report" in sys.argv
prev_n = 0
try:
    prev_n = len(json.load(open("pruned_config.json", encoding="utf-8"))["sites"])
except Exception:
    pass
degraded = bool(prev_n) and len(final) > prev_n * 1.05

if degraded:
    print(f"警告：本次保留 {len(final)} 站 > 上次 {prev_n} 站 ×1.05，"
          f"判定为剔站失效（规则缓存缺失/上游改动），保留旧 pruned_config.json 不覆盖")
if REPORT_ONLY:
    print("报告模式：不写入 pruned_config.json / dropped_sites.txt")

if not (REPORT_ONLY or degraded):
    json.dump(out, open("pruned_config.json", "w", encoding="utf-8", newline="\n"),
              ensure_ascii=False, separators=(",", ":"))
    print(f"pruned_config.json {os.path.getsize('pruned_config.json')/1024:.1f}KB")
    with open("dropped_sites.txt", "w", encoding="utf-8", newline="\n") as f:
        for s, h, st in sorted(dropped, key=lambda x: (x[2], str(x[1]))):
            f.write(f"{st}\t{s.get('key')}\t{s.get('name')}\t{h}\n")
    print("剔除清单 dropped_sites.txt")
