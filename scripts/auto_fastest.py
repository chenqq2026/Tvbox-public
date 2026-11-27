#!/usr/bin/env python3
"""国内网络侧：多通道测速 + 防抖自动切换最快通道。

设计红线（与 CI 侧 monitor_sources.py 分工）：
- 本机（国内网络）测速才对电视盒有意义；CI 海外机房测国内加速通道会系统性误判。
- **地址栏固定不变**：用户填进盒子的订阅入口（README 推荐地址 + config.json 的 urls）
  永远指向 ghfast.top，绝不随测速改动——盒子缓存反复失效比慢 0.3s 更糟，
  而且地址栏拉的是几十 KB 小文件，速度差异可忽略。真正的瓶颈是 1.7MB 的 jar。
- 只切**配置内部 spider.jar 的通道**：改各 config 的 spider 字段 + CI 生成脚本的
  SELF_PROXY + 监控脚本的校验常量，让盒子每次拉到的配置里 jar 都走当前最优通道。
- 不做单次决策：新通道必须【连续 STREAK 天快 ≥ GAIN%】才切换，避免抖动。
- 首次运行只建立基线，不切换。
- 每次测速/决策全程留痕 speed_state.json + speed_report.json，可追溯可回滚。
- 只切「jar 通道前缀」，绝不改站点/解析器/直播内容，也不改地址栏；jar 的 md5 不动。

用法：
  python scripts/auto_fastest.py              # 测速+防抖判断，达标则切换+推送
  python scripts/auto_fastest.py --dry-run    # 只测速+判断，不切换不推送（验证用）
"""
import datetime as dt
import json
import os
import ssl
import statistics
import subprocess
import sys
import urllib.request

# ---- 通道定义（前缀，指向本仓库 raw）----
RAW = "https://raw.githubusercontent.com/chenqq2026/Tvbox-public/main/"
CHANNELS = {
    "ghfast":   "https://ghfast.top/" + RAW,
    "gh-proxy": "https://gh-proxy.com/" + RAW,
    "raw":      RAW,
}
# 测速目标：jar 是真瓶颈（1.7MB），config_all 代表小文件。两者足以定通道。
# weight 用于综合评分，jar 权重最高。
PROBES = [("jar/spider.jar", 0.8), ("config_all.json", 0.2)]
SAMPLES = 2
STREAK = 3          # 连续 3 天推荐同一新通道才切换
GAIN = 0.20         # 且需快 ≥20%
STATE = "speed_state.json"
REPORT = "speed_report.json"
UA = {"User-Agent": "okhttp/3.12.0"}
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

# 关键：测速必须走真实网络，绝不能用本机代理！
# 实测同一 ghfast jar：带代理 8.5s vs 直连 2.0s，代理会二次中转把通道快慢完全颠倒，
# 据此决策会把用户切到慢通道。这里显式禁用所有代理。
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def measure(url, timeout=20, n=600000):
    req = urllib.request.Request(url, headers=UA)
    with opener.open(req, timeout=timeout) as r:
        b = r.read(n)
    return len(b)


def time_one(url):
    """用 curl 直连完整下载到临时文件计时——最贴近 TVBox 真实拉取行为。

    不用 urllib：一是本机 urllib 的 TLS/代理行为与 curl 不一致（实测 ghfast
    urllib 17s vs curl 2s），二是 -o /dev/null 在 Windows 下 curl 的 size 统计
    会变 0。必须写到真实临时文件。禁用代理避免二次中转扭曲通道快慢。
    """
    tmp = "_speed_tmp.bin"
    try:
        p = subprocess.run(
            ["curl", "-sL", "--noproxy", "*", "-m", "20",
             "-o", tmp, "-w", "%{http_code} %{time_total} %{size_download}", url],
            capture_output=True, text=True, timeout=25)
        parts = p.stdout.split()
        if len(parts) == 3 and parts[0] == "200" and int(parts[2]) > 1000:
            return float(parts[1])
        return None
    except Exception:
        return None
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass


def channel_score(ch):
    """返回该通道的综合耗时(秒, 越低越好)与各 target 明细"""
    base = CHANNELS[ch]
    total = 0.0
    detail = {}
    for path, w in PROBES:
        # raw 通道只测 1 次（一直最慢，仅作基线对照，不值得多次采样拖时间）
        n = 1 if ch == "raw" else SAMPLES
        ts = [t for _ in range(n) if (t := time_one(base + path)) is not None]
        if not ts:
            return None, {path: "全部失败"}
        med = statistics.median(ts)
        detail[path] = {"median_s": round(med, 3), "samples": [round(x, 3) for x in ts],
                        "weight": w}
        total += med * w
    return round(total, 3), detail


def now8():
    return (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
            + dt.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


def load_state():
    try:
        return json.load(open(STATE, encoding="utf-8"))
    except Exception:
        return {"current": "ghfast", "streak": 0, "candidate": None, "history": []}


def save_state(s):
    json.dump(s, open(STATE, "w", encoding="utf-8", newline="\n"),
              ensure_ascii=False, indent=2)


# 地址栏固定前缀：用户填进盒子的订阅入口，永不随测速改变。
# 只改配置内部 spider.jar 的通道，故替换时必须锚定到 "…/jar/" 这一段，
# 绝不能用裸前缀全局替换——那会把 config.json 的 urls 和 README 的地址栏一起改掉。
ADDRESS_PREFIX = CHANNELS["ghfast"]          # 仅供文档/断言，switch 不碰
JAR_SUFFIX = "jar/"                          # spider.jar / spare_spider.jar 共同路径


def _jar_prefix(ch):
    """配置内 spider.jar 地址用的完整前缀（到 main/ 为止）。"""
    return CHANNELS[ch]


def switch_channel(new):
    """只切配置内部 spider.jar 通道 + 生成/监控脚本，地址栏与站点一律不动。

    旧实现用裸前缀 old 对全文 replace，有两个 bug：
      1) config.json 的 urls、README 地址栏被一起改（违背"地址栏固定"承诺）；
      2) 生成脚本里是短常量 SELF_PROXY="https://ghfast.top/"，与完整 old 匹配不上，
         导致 CI 每天 05:17 重建又把 jar 写回旧通道，切换活不过一天。
    这里分别用【锚定 jar 路径】和【正则匹配短常量】修正。
    """
    import re
    old = CHANNELS.get(STATE_CURRENT, CHANNELS["ghfast"])
    tgt = CHANNELS[new]
    host = tgt.split("/")[2] if "://" in tgt else new
    changed = []

    # 1) 配置产物：只替换 spider.jar 地址的前缀（锚定 ".../jar/"），
    #    config.json（纯地址栏 urls）不在此列，README 更不碰。
    old_jar = old + JAR_SUFFIX
    tgt_jar = tgt + JAR_SUFFIX
    for fn in ["config_all.json", "config_lite.json", "spare_juhe.json",
               "pruned_config.json"]:
        s = open(fn, encoding="utf-8").read()
        if old_jar in s:
            open(fn, "w", encoding="utf-8", newline="\n").write(s.replace(old_jar, tgt_jar))
            changed.append(fn)

    # 2) 生成脚本里的 SELF_PROXY 短常量（CI 每日重建据此写 jar 地址）
    #    正则精确匹配引号内域名前缀，不受 RAW 路径拼法影响。
    short_prefix = "https://" + host + "/"
    for fn in ["scripts/update_daily.py", "scripts/prune_sites.py"]:
        s = open(fn, encoding="utf-8").read()
        s2 = re.sub(r'(SELF_PROXY\s*=\s*")https://[^"]+(")',
                    lambda m: m.group(1) + short_prefix + m.group(2), s, count=1)
        if s2 != s:
            open(fn, "w", encoding="utf-8", newline="\n").write(s2)
            changed.append(fn)

    # 3) 监控脚本的通道校验常量（CI 侧据此判断 jar 是否被写回旧通道）
    fn = "scripts/monitor_sources.py"
    s = open(fn, encoding="utf-8").read()
    s2 = re.sub(r'GHFAST\s*=\s*"[^"]+"', f'GHFAST = "{host}"', s, count=1)
    if s2 != s:
        open(fn, "w", encoding="utf-8", newline="\n").write(s2)
        changed.append(fn)

    # 4) 地址栏（README.md / config.json）刻意不动——固定 ghfast.top。
    return changed


def git_push(msg):
    env = dict(os.environ)
    cmds = [
        ["git", "add", "-A"],
        ["git", "commit", "-m", msg],
        ["git", "push", "origin", "main"],
    ]
    for c in cmds:
        p = subprocess.run(c, capture_output=True, text=True, env=env)
        if p.returncode != 0:
            return False, f"{' '.join(c)}: {p.stderr.strip()[:200]}"
    return True, "ok"


def main():
    global STATE_CURRENT
    dry = "--dry-run" in sys.argv
    st = load_state()
    STATE_CURRENT = st["current"]

    print(f"=== 国内侧多通道测速 {now8()} ===")
    scores = {}
    details = {}
    for ch in CHANNELS:
        sc, det = channel_score(ch)
        scores[ch] = sc
        details[ch] = det
        if sc is None:
            print(f"  {ch:9s} FAIL {det}")
        else:
            print(f"  {ch:9s} 综合 {sc:6.3f}s  {det}")

    ok = {k: v for k, v in scores.items() if v is not None}
    if not ok:
        print("  所有通道测速失败，本次不决策")
        return
    best = min(ok, key=ok.get)
    cur = ok.get(STATE_CURRENT)
    gain = (cur - ok[best]) / cur if cur and best != STATE_CURRENT else 0.0

    print(f"\n  当前通道={STATE_CURRENT} 最快通道={best} 提升={gain*100:.1f}%")

    # 防抖决策
    if best == STATE_CURRENT:
        st["streak"] = 0
        st["candidate"] = None
        decision = "保持（最快即当前）"
    elif gain < GAIN:
        st["streak"] = 0
        st["candidate"] = None
        decision = f"保持（提升 {gain*100:.1f}% < {GAIN*100:.0f}% 门槛）"
    else:
        if st.get("candidate") == best:
            st["streak"] = st.get("streak", 0) + 1
        else:
            st["streak"] = 1
            st["candidate"] = best
        if st["streak"] >= STREAK:
            decision = f"切换 → {best}（连续 {st['streak']} 天快 {gain*100:.1f}%）"
        else:
            decision = f"观察中 → {best}（第 {st['streak']}/{STREAK} 天，暂不切）"

    print(f"  决策: {decision}" + ("  [DRY-RUN]" if dry else ""))

    report = {
        "checked_at": now8(), "scores": scores, "details": details,
        "current": STATE_CURRENT, "best": best, "gain": round(gain, 4),
        "streak": st.get("streak", 0), "candidate": st.get("candidate"),
        "decision": decision, "dry_run": dry,
    }
    json.dump(report, open(REPORT, "w", encoding="utf-8", newline="\n"),
              ensure_ascii=False, indent=2)

    do_switch = (decision.startswith("切换") and not dry)
    if do_switch:
        changed = switch_channel(best)
        print(f"  已切换文件: {changed}")
        okp, msg = git_push(f"perf(jar): 国内实测连续{STREAK}天最优 jar 通道切至 {best}（地址栏固定不变）")
        print(f"  推送: {msg}")
        if okp:
            st["current"] = best
            st["streak"] = 0
            st["candidate"] = None

    st.setdefault("history", []).append({
        "date": now8(), "best": best, "gain": round(gain, 4),
        "decision": decision, "scores": scores,
    })
    st["history"] = st["history"][-30:]   # 保留最近 30 次
    save_state(st)
    print(f"  留痕: {STATE} / {REPORT}")


if __name__ == "__main__":
    main()
