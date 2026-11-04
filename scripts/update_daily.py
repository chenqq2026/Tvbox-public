#!/usr/bin/env python3
"""每日自动更新：
1. 拉取 qist/tvbox 最新 jsm.json -> 修正 spider 为绝对路径 -> jsm_fixed.json
2. 拉取 vbskycn/iptv 最新 iptv4.m3u -> 逐频道验证 -> live_verified.m3u
仅当内容变化时才写文件（配合 workflow 的 git diff 判断是否提交）。
"""
import concurrent.futures as cf
import hashlib
import json
import os
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
GH_RAW_SELF = "https://raw.githubusercontent.com/chenqq2026/Tvbox-public/main/"
JAR_MD5_RE = re.compile(r"md5;([0-9a-f]{32})")
JAR_MIN = 500 * 1024          # 合法 spider.jar 至少 500KB，小于则判为错误页
JAR_MAX = 30 * 1024 * 1024

print("== 1. qist jsm.json ==")
raw = fetch_first([GAO_PROXY + RAW_QIST + "jsm.json",
                   "https://qist.wyfc.qzz.io/jsm.json",
                   RAW_QIST + "jsm.json"])
jsm = json.loads(raw.decode("utf-8"))

# 1b. spider.jar：多镜像 + 内容校验 + 镜像进本仓库
#     旧版脚本只算 md5 不校验内容，抓到 GitHub 错误页也会把错 md5 写进配置，
#     导致 TVBox 校验通过但 jar 解析失败 -> 所有 spider 站打不开。
declared_md5 = ""
_m = JAR_MD5_RE.search(str(jsm.get("spider") or ""))
if _m:
    declared_md5 = _m.group(1)
jar, jar_src = None, ""
for u in [GAO_PROXY + RAW_QIST + "jar/spider.jar",
          "https://qist.wyfc.qzz.io/jar/spider.jar",
          RAW_QIST + "jar/spider.jar"]:
    try:
        b = get(u, timeout=60, n=JAR_MAX)
    except Exception as e:
        print(f"  jar 拉取失败 {u[:58]} ({type(e).__name__})")
        continue
    if not (JAR_MIN <= len(b) <= JAR_MAX):
        print(f"  jar 体积异常 {len(b)}B，判为错误页，跳过 {u[:58]}")
        continue
    if not b.startswith(b"PK\x03\x04"):
        print(f"  jar 非 ZIP 头 {b[:6]!r}，跳过 {u[:58]}")
        continue
    # 若 qist 在 jsm.json 里声明了 md5，则必须与声明一致才算拿到正确版本，
    # 否则说明命中了代理 CDN 缓存，换下一个镜像
    calc = hashlib.md5(b).hexdigest()
    if declared_md5 and calc != declared_md5:
        print(f"  jar md5 与 qist 声明不符（命中缓存），换镜像 "
              f"{calc[:8]}!={declared_md5[:8]}")
        continue
    jar, jar_src = b, u
    break

if jar is None:
    if declared_md5:
        # 所有镜像都对不上声明值时，退回按 qist 声明的 md5 + 本次拉到的合法 jar
        for u in [GAO_PROXY + RAW_QIST + "jar/spider.jar",
                  "https://qist.wyfc.qzz.io/jar/spider.jar"]:
            try:
                b = get(u, timeout=60, n=JAR_MAX)
                if JAR_MIN <= len(b) <= JAR_MAX and b.startswith(b"PK\x03\x04"):
                    jar, jar_src = b, u
                    print(f"  退回使用 {u[:58]}（md5 与声明不符，但内容为合法 jar）")
                    break
            except Exception:
                continue
    if jar is None:
        raise SystemExit("所有镜像都拿不到合法 spider.jar，中止本次更新（保留线上旧版本）")

md5 = hashlib.md5(jar).hexdigest()
os.makedirs("jar", exist_ok=True)
old_jar = b""
if os.path.exists("jar/spider.jar"):
    old_jar = open("jar/spider.jar", "rb").read()
jar_changed = (hashlib.md5(old_jar).hexdigest() != md5) if old_jar else True
if jar_changed:
    open("jar/spider.jar", "wb").write(jar)
print(f"  jar {len(jar) / 1048576:.2f}MB md5={md5} 来源={jar_src[:46]} "
      f"声明一致={declared_md5 == md5} 更新={jar_changed}")
# spider 指向本仓库镜像（不再依赖 qist 上游，qist 删库也不影响）
jsm["spider"] = GAO_PROXY + GH_RAW_SELF + "jar/spider.jar;md5;" + md5
# 修复站内相对路径
for s in jsm.get("sites", []):
    for f in ("api", "ext"):
        v = str(s.get(f) or "")
        if v.startswith("./"):
            s[f] = "https://qist.wyfc.qzz.io/" + v[2:]
json.dump(jsm, open("jsm_fixed.json", "w", encoding="utf-8", newline="\n"),
          ensure_ascii=False, separators=(",", ":"))
print(f"jsm_fixed.json: sites={len(jsm.get('sites', []))} jar_md5={md5[:12]}")

# ---------- 2. 直播逐频道验证 ----------
print("== 2. 直播源验证 ==")
# 主源 vbskycn：频道量大、地方台全，但（2026-11 实测）已不含 CCTV-1/2/3/4/6/7/9/10/11/14
IPTV_URLS = [
    "https://raw.githubusercontent.com/vbskycn/iptv/master/tv/iptv4.m3u",
    GAO_PROXY + "https://raw.githubusercontent.com/vbskycn/iptv/master/tv/iptv4.m3u",
    "https://ghproxy.net/https://raw.githubusercontent.com/vbskycn/iptv/master/tv/iptv4.m3u",
]
# 补源 Guovin/iptv-api 自动生成结果（每 6 小时更新）：央视 1-17 每台多线路，正好补缺口
FILL_URLS = [
    GAO_PROXY + "https://raw.githubusercontent.com/Guovin/iptv-api/gd/output/result.m3u",
    "https://raw.githubusercontent.com/Guovin/iptv-api/gd/output/result.m3u",
    "https://ghproxy.net/https://raw.githubusercontent.com/Guovin/iptv-api/gd/output/result.m3u",
]


def norm_chn(n):
    """CCTV-1 / CCTV 1 / cctv1 统一成 CCTV1：便于分组、去重和 EPG 匹配"""
    n = re.sub(r"\s+", "", str(n).strip())
    return re.sub(r"^cctv[-\s]?", "CCTV", n, flags=re.I)


def parse_m3u(txt):
    out, nm = [], None
    for line in txt.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF"):
            m = re.search(r",(.+)$", line)
            nm = m.group(1).strip() if m else None
        elif line and not line.startswith("#") and nm:
            out.append((norm_chn(nm), line))
            nm = None
    return out


channels = parse_m3u(fetch_first(IPTV_URLS, timeout=40).decode("utf-8", "ignore"))
print(f"  主源 vbskycn: {len(channels)} 条")

# 补源只取央视数字频道与卫视，避免把验证量翻四倍
try:
    fill = parse_m3u(fetch_first(FILL_URLS, timeout=45).decode("utf-8", "ignore"))
    fill_keep = [(c, u) for c, u in fill
                 if re.match(r"^CCTV\d+\+?$", c) or "卫视" in c]
    print(f"  补源 iptv-api: {len(fill)} 条 → 央视/卫视 {len(fill_keep)} 条")
    channels += fill_keep
except Exception as e:
    print(f"  补源拉取失败（不影响主流程）: {type(e).__name__}")

# 去重（按 URL）
seen, uniq = set(), []
for c, u in channels:
    if u not in seen:
        seen.add(u)
        uniq.append((c, u))
print(f"拉到 {len(uniq)} 个频道，开始逐个验证...")

# 上次实测通过的线路（live_speed.json 的 url→kbps），用于本次网络够不着时兜底。
# CI 跑在海外机房，到国内 IPTV 源的可达性远低于家宽：本次连不上 ≠ 线路已死。
# 没有这层兜底时，每天在 CI 里跑会把好线一天天切少（实测 534→404 条、432→302 频道）。
prev_speed = {}
try:
    for _c, _u, _k in json.load(open("live_speed.json", encoding="utf-8")):
        prev_speed[_u] = _k
except Exception:
    pass


def playable(u, attempt=2):
    """失败重试一次，降低网络抖动造成的误判"""
    for i in range(attempt):
        try:
            b = get(u, timeout=12 if i else 9, n=600)
            if b[:7] == b"#EXTM3U" or b[:4] == b"\x47\x40\x00\x10":
                return True
        except Exception:
            pass
    return False


# 首轮并发 14（原 30 过高，会被运营商限速误判成“已死”），失败项再降并发补测一轮
verified, failed = [], []
with cf.ThreadPoolExecutor(14) as ex:
    for (c, u), ok in zip(uniq, ex.map(playable, [u for _, u in uniq])):
        (verified if ok else failed).append((c, u))
print(f"首轮验证通过 {len(verified)}/{len(uniq)}")
if failed:
    with cf.ThreadPoolExecutor(6) as ex:
        for (c, u), ok in zip(failed, ex.map(playable, [u for _, u in failed])):
            if ok:
                verified.append((c, u))
    print(f"二次补测后 {len(verified)}/{len(uniq)}")

# 兜底：本轮两测都不通、但上次实测通过的线路仍然保留（标记为未复验）。
# 判据收紧一半——只认上次测到的 url，且必须仍在上游 m3u 里（上游撤了就不留）。
_vset = {u for _c, u in verified}
rescued = [(c, u) for c, u in failed if u not in _vset and u in prev_speed]
if rescued:
    verified.extend(rescued)
    print(f"沿用上次实测通过 {len(rescued)} 条（本次不可达，按可用保留，不当作死线）")
print(f"最终进入测速 {len(verified)}/{len(uniq)}")


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
inherited = measured = 0
ts = time.time()
with cf.ThreadPoolExecutor(12) as ex:
    for (c, u), k in zip(verified, ex.map(lambda p: measure(p[1]), verified)):
        if k > 0:
            measured += 1
        elif prev_speed.get(u, 0) > 0:
            k = prev_speed[u]      # 本次测不出，沿用上次实测值（多为 CI 海外网络够不着）
            inherited += 1
        speed[u] = k
known = [v for v in speed.values() if v > 0]
print(f"测速完成 {len(known)}/{len(verified)} 条"
      f"（本次实测 {measured}，沿用上次 {inherited}），用时 {time.time()-ts:.0f}s")
if known:
    known.sort()
    print(f"带宽中位数 {known[len(known)//2]} kbps | 最低 {known[0]} | 最高 {known[-1]}")

# 按频道归组，每个频道保留最快 2 条线路
# k == 0 表示“测速失败”而非“带宽为0”，不能当成慢线路剔除，否则会误杀可用线路
by_ch = {}
for c, u in verified:
    by_ch.setdefault(c.replace(" ", ""), []).append((c, u, speed.get(u, 0)))

usable_ok = len(known) >= len(verified) * 0.4   # 测速成功率过低则不参与筛选
final = []
for key, items in by_ch.items():
    # 央视与卫视留 3 条（最常看、也最容易单线挂掉），其余留 2 条
    lim = 3 if (re.match(r"^CCTV\d+\+?$", key) or "卫视" in key) else 2
    if usable_ok:
        items.sort(key=lambda x: -x[2])
        best = items[0][2]
        keep = [it for it in items[:lim]
                if it[2] == 0 or it[2] >= max(150, best * 0.3)]
        final.extend(keep or [items[0]])
    else:
        final.extend(items[:1])
print(f"频道数 {len(by_ch)} → 输出线路 {len(final)} 条（央视/卫视最多3条，其余2条）")

# 关键频道兜底：验证阶段受网络抖动影响，CCTV1/5/13 这类台一旦被漏判就"突然没了"。
# 缺哪个就从上游未通过验证的线路里补一条（仍标记为不确定，排在有测速数据的后面）。
MUST_HAVE = ["CCTV1", "CCTV2", "CCTV5", "CCTV5+", "CCTV6", "CCTV13",
             "湖南卫视", "浙江卫视", "东方卫视", "江苏卫视", "北京卫视"]
have = {c.replace(" ", "") for c, _u, _k in final}
missing = [m for m in MUST_HAVE if m not in have]
restored = []
for m in missing:
    for c, u in uniq:
        if c.replace(" ", "") == m:
            final.append((c, u, 0))
            restored.append(m)
            break
if restored:
    print(f"关键频道兜底补入: {restored}")


# 上游会周期性精简（实测 vbskycn 从 404 频道降到 300），撤掉的不等于死掉。
# 把上一版 m3u 里「上游已不再提供、但此刻仍能播」的线路复验一遍留下来，
# 频道数就不会因为上游瘦身而莫名少一大截（实测 432 → 327）。
def clean_name(n):
    return re.sub(r"（慢）$", "", re.sub(r"▪\d+$", "", n)).strip()


try:
    old_m3u = parse_m3u(open("live_verified.m3u", encoding="utf-8").read())
except FileNotFoundError:
    old_m3u = []
new_urls = {u for _c, u, _k in final}
carry = [(clean_name(c), u) for c, u in old_m3u
         if u not in new_urls and clean_name(c)]
if carry:
    with cf.ThreadPoolExecutor(10) as ex:
        carry_ok = [((c, u), ok) for (c, u), ok in
                    zip(carry, ex.map(lambda p: playable(p[1], attempt=1), carry)) if ok]
    cnt = {}
    for c, _u, _k in final:
        cnt[c.replace(" ", "")] = cnt.get(c.replace(" ", ""), 0) + 1
    kept_carry = 0
    for (c, u) in [p for p, ok in carry_ok]:
        key = c.replace(" ", "")
        lim = 3 if (re.match(r"^CCTV\d+\+?$", key) or "卫视" in key) else 2
        if cnt.get(key, 0) < lim:
            final.append((c, u, prev_speed.get(u, 0)))
            cnt[key] = cnt.get(key, 0) + 1
            kept_carry += 1
    print(f"上游已撤但复验可播 {len(carry_ok)}/{len(carry)} 条 → 补回 {kept_carry} 条")

# 保险：只有「来源不可信」时才拦下本次结果。
# 早期版本用「比上次少 20% 就保留旧文件」，结果上游真的精简频道时（实测 404→300 频道）
# 会被永久冻住，线上文件一天天变旧、里面的死线也一直挂着。改为先判断来源可信度：
# 上游拉到了、验证通过率够高 → 条数变少是上游真实变化，放行覆盖。
src_ok = len(channels) > 200 and len(uniq) > 0 and len(verified) >= len(uniq) * 0.5
prev = 0
live_kept_prev = False
try:
    prev = open("live_verified.m3u", encoding="utf-8").read().count("#EXTINF")
except FileNotFoundError:
    pass
keep_prev = len(final) < 100 or (prev and len(final) < prev * 0.8 and not src_ok)
if keep_prev:
    live_kept_prev = True
    print(f"警告：本次仅 {len(final)} 条（来源{'可信' if src_ok else '不可信'}，"
          f"验证 {len(verified)}/{len(uniq)}，现有 {prev} 条），保留旧 live_verified.m3u 不覆盖")
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
        # tvg-name 去掉空格：EPG 服务按频道名查询，「CCTV 1」查不到、「CCTV1」能查到
        lines.append(f'#EXTINF:-1 tvg-name="{c.replace(chr(32), "")}" '
                     f'group-title="{grp(c)}",{label}')
        lines.append(u)
    open("live_verified.m3u", "w", encoding="utf-8", newline="\n").write("\n".join(lines) + "\n")
    json.dump([[c, u, k] for c, u, k in final],
              open("live_speed.json", "w", encoding="utf-8", newline="\n"),
              ensure_ascii=False, separators=(",", ":"))
    print(f"live_verified.m3u 已生成（{len(final)} 条线路 / "
          f"{len({c.replace(' ', '') for c, _u, _k in final})} 个频道，上次 {prev}）")

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

# ---------- 3b. 解析器实测排序（只排序，不删除） ----------
# 解析器多而重复是"点开没反应"的常见原因：壳会按顺序尝试，排在前面的是早就挂掉的
# 公共 jx 接口时，一次点击要等好几轮超时。这里每天实测一次，把活的排前面。
print("== 3b. 解析器实测 ==")
PARSER_TEST = "https://vip.ffzy-play.com/share/20240101/1234/index.m3u8"


def parse_ok(item):
    name, url = item
    if not url.startswith("http"):
        return name, None
    tpl = url if ("url=" in url or "v=" in url) else url + ("&url=" if "?" in url else "?url=")
    try:
        b = get(tpl + urllib.parse.quote(PARSER_TEST, safe=""), timeout=9, n=80000)
        low = b.decode("utf-8", "ignore").lower()
        if any(x in low for x in ("域名过期", "已过期", "已停止", "not found", "已暂停")):
            return name, False
        return name, any(x in low for x in (".m3u8", "video", "player", "iframe", "var url"))
    except Exception:
        return name, False


def build_parses(parses_in):
    """按 url 去重，保序；再用实测结果把可用的稳定排在前面。"""
    uniq, seen_u = [], set()
    for p in parses_in:
        u = str(p.get("url") or "")
        key = u or ("__agg__" + str(p.get("name")))
        if key in seen_u:
            continue
        seen_u.add(key)
        uniq.append(p)
    cand = [(p.get("name"), str(p.get("url") or "")) for p in uniq
            if str(p.get("url") or "").startswith("http")]
    res = {}
    if cand:
        with cf.ThreadPoolExecutor(12) as ex:
            for name, ok in ex.map(parse_ok, cand):
                res[name] = ok
    ok_n = sum(1 for v in res.values() if v)
    print(f"  解析器 {len(parses_in)} -> 去重后 {len(uniq)} | 实测可用 {ok_n}/{len(res)}")
    alive = [p for p in uniq if res.get(p.get("name")) is True]
    unknown = [p for p in uniq if res.get(p.get("name")) is None]
    dead = [p for p in uniq if res.get(p.get("name")) is False]
    return alive + unknown + dead, {"total": len(uniq), "alive": ok_n,
                                    "tested": len(res)}

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

# 4a. 直连 CMS 每日探活：这 8 个站是「最稳的保底」，但也是手工挑的，
#     死了还留在配置里就会「点开就崩」。返回不了片单的当天剔除。
#     注意：不能用 json.loads 判断整包——CMS 返回的列表动辄几 MB，
#     read(n) 截断后必然解析失败，会把活站全判死（这坑踩过一次）。
def cms_alive(s):
    api = str(s.get("api") or "")
    if not api.startswith("http"):
        return True  # 非直连站不在这里判定
    base = api.split("?")[0].rstrip("/")
    u = base + ("&ac=videolist&pg=1" if "?" in base else "?ac=videolist&pg=1")
    for _ in range(2):
        try:
            head = get(u, timeout=12, n=6000).decode("utf-8", "ignore")
            if any(k in head for k in ('"code"', '"list"', '"class"', "<rss", "<list", "<?xml")):
                return True
        except Exception:
            time.sleep(0.5)
    return False


cms_dead = []
if direct_sites:
    with cf.ThreadPoolExecutor(8) as ex:
        cms_ok = list(ex.map(cms_alive, direct_sites))
    cms_dead = [str(s.get("name")) for s, ok in zip(direct_sites, cms_ok) if not ok]
    if cms_dead:
        print(f"  直连CMS探活失败(今日剔除): {cms_dead}")
    direct_sites = [s for s, ok in zip(direct_sites, cms_ok) if ok]
    print(f"  直连CMS探活: {len(direct_sites)} 可用 / {len(cms_dead)} 剔除")

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

# 解析器合并：去重 + 实测可用的排前（自持优先）
merged_parses, parse_stat = build_parses(direct_parses + jsm.get("parses", []))

# 直播：验证过的频道放第一位，qist 原有直播做备用（拉不到频道的直接剔）
merged_lives = [{"name": "自建·已验证频道", "type": 0, "url": LIVE_URL,
                 "playerType": 2, "logo": LOGO}]


def live_has_channels(u):
    """够用才留：少于 20 台没实用价值；多于 800 条基本是 EPG 测试源
    （实测 epg.pw/test_channels.m3u 塞了 1204 条「仅供测试 epg 准确性」的假台）"""
    try:
        t = get(u, timeout=15, n=400000).decode("utf-8", "ignore")
    except Exception:
        return False
    n = len(re.findall(r"^#EXTINF|^[^#\n]+,", t, re.M))
    return 20 <= n <= 800


# 明确不是给人看直播的源：EPG 测试频道、占位测试文件
BAD_LIVE = re.compile(r"epg\.pw|test_?channels|/test\.m3u|测试频道", re.I)
qist_lives = [l for l in jsm.get("lives", [])
              if str(l.get("url") or "").startswith("http")
              and not BAD_LIVE.search(str(l.get("url")) + " " + str(l.get("name") or ""))]
if qist_lives:
    with cf.ThreadPoolExecutor(6) as ex:
        keep = list(ex.map(lambda l: live_has_channels(str(l["url"])), qist_lives))
    dropped = [l.get("name") for l, k in zip(qist_lives, keep) if not k]
    if dropped:
        print(f"  剔除无频道直播源: {dropped}")
    qist_lives = [l for l, k in zip(qist_lives, keep) if k]
for l in qist_lives:
    l = dict(l)
    if not str(l.get("name") or "").startswith("备用"):
        l["name"] = "备用·" + str(l.get("name") or "qist")
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
json.dump(cfg_all, open("config_all.json", "w", encoding="utf-8", newline="\n"),
          ensure_ascii=False, separators=(",", ":"))
print(f"config_all.json: sites={len(merged_sites)} "
      f"(自持{len(direct_sites)}+qist{len(jsm.get('sites', []))}) "
      f"parses={len(merged_parses)} lives={len(merged_lives)}")

# ---------- 4b. 备用聚合镜像 spare_juhe.json ----------
# 主配置挂了时临时顶替用。上游是 Lightconer/tvbox-ysc-config 每日把肥猫/饭太硬/王二小
# 等十几个源合并成的单仓文件。镜像到本仓库有两个目的：
#   1. 地址变成干净的英文短路径（原路径含 http://单仓聚合.json 的中文编码，难记）
#   2. 把它的 spider 也镜像下来（对方用改名成 .png 的 jar 挂第三方 CDN，
#      那个 CDN 挂则 500+ 站全废），改写成本仓库 jar，彻底自持。
# 任何一步失败都不覆盖旧文件——宁可留着上一次能用的。
print("== 4b. 备用聚合镜像 ==")
SPARE_SLUG = urllib.parse.quote("单仓聚合.json")
SPARE_SRC = [
    GAO_PROXY + "https://raw.githubusercontent.com/Lightconer/tvbox-ysc-config/main/output/" + SPARE_SLUG,
    "https://ghproxy.net/https://raw.githubusercontent.com/Lightconer/tvbox-ysc-config/main/output/" + SPARE_SLUG,
    "https://raw.githubusercontent.com/Lightconer/tvbox-ysc-config/main/output/" + SPARE_SLUG,
]
SPARE_JAR = "jar/spare_spider.jar"
spare_stat = {"ok": False}
try:
    spare = json.loads(fetch_first(SPARE_SRC, timeout=45).decode("utf-8", "ignore").lstrip("\ufeff"))
    n_sites = len(spare.get("sites", []))
    if n_sites < 300:
        raise ValueError(f"只有 {n_sites} 站，疑似上游降级，保留旧文件")
    # 顺手剔掉上游夹带的假直播源（epg.pw 那种测试频道）
    spare["lives"] = [l for l in spare.get("lives", [])
                      if str(l.get("url") or "").startswith("http")
                      and not BAD_LIVE.search(str(l.get("url")) + " " + str(l.get("name") or ""))]
    # 壁纸和桌面图标是对方自己的第三方域名（换域名就变叉），清掉用播放器默认的。
    # hosts / proxy 保留：它们是让海外源和咪咕直播能播的关键，动了会大面积失效。
    spare["wallpaper"] = ""
    spare["logo"] = ""
    spare["warningText"] = "本配置由公开源自动聚合（含肥猫等），本仓库仅做镜像，仅供学习交流，请勿商用"
    # 镜像 spider jar
    jar_mirror = False
    raw_spider = str(spare.get("spider") or "")
    m = re.match(r"^(https?://[^;]+)", raw_spider)
    if m:
        try:
            jb = get(m.group(1), timeout=45, n=32 * 1024 * 1024)
            import io as _io
            import zipfile as _zip
            ok_zip = jb[:2] == b"PK" and 300 * 1024 < len(jb) < 30 * 1048576
            if ok_zip:
                _zip.ZipFile(_io.BytesIO(jb)).namelist()   # 能正常打开才算数
                open(SPARE_JAR, "wb").write(jb)
                spare["spider"] = (GAO_PROXY + GH_RAW_SELF + "jar/spare_spider.jar;md5;"
                                   + hashlib.md5(jb).hexdigest())
                jar_mirror = True
        except Exception as e:
            print(f"  jar 镜像失败，沿用对方 spider（依赖其 CDN）: {type(e).__name__}")
    json.dump(spare, open("spare_juhe.json", "w", encoding="utf-8", newline="\n"),
              ensure_ascii=False, separators=(",", ":"))
    spare_stat = {"ok": True, "sites": n_sites, "lives": len(spare["lives"]),
                  "jar_mirrored": jar_mirror}
    print(f"spare_juhe.json 已更新：{n_sites} 站 / {len(spare['lives'])} 直播 / "
          f"jar {'已镜像' if jar_mirror else '仍指上游'}")
except Exception as e:
    print(f"备用聚合更新失败，保留旧 spare_juhe.json: {type(e).__name__} {e}")

# ---------- 5. health.json 健康快照 ----------
print("== 5. 健康快照 ==")
_kb = sorted([k for _c, _u, k in final if k > 0])
health = {
    "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
    "jar": {"md5": md5, "size_mb": round(len(jar) / 1048576, 2),
            "declared_match": declared_md5 == md5, "changed": jar_changed},
    "sites": {
        "total": len(merged_sites),
        "cms": sum(1 for s in merged_sites if str(s.get("type")) == "1"),
        "spider": sum(1 for s in merged_sites if str(s.get("type")) == "3"),
        "cms_dead_today": cms_dead,
    },
    "parses": parse_stat,
    "live": {
        # lines 是真正生效的那份 m3u 的条数；computed_lines 是本次算出来的（诊断用）
        "lines": prev if live_kept_prev else len(final),
        "computed_lines": len(final),
        "stale": live_kept_prev,
        "channels": len({c.replace(" ", "") for c, _u, _k in final}),
        "measured_now": measured,
        "inherited_kbps": inherited,
        "median_kbps": _kb[len(_kb) // 2] if _kb else 0,
        "p10_kbps": _kb[len(_kb) // 10] if _kb else 0,
        "slow_tagged": sum(1 for _c, _u, k in final if 0 < k < 500),
    },
    "epg": epg_url or "无可用源",
    "spare": spare_stat,
}
json.dump(health, open("health.json", "w", encoding="utf-8", newline="\n"),
          ensure_ascii=False, indent=2)
print(json.dumps(health, ensure_ascii=False, indent=2))
print("DONE")
