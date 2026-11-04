# Tvbox-public

TVBox / FongMi 影视 / OK影视 的配置源。**每天北京时间 05:17 由 GitHub Actions 自动重建**，不需要手动维护。

---

## 一、导入地址

### 主地址（单仓，推荐）

```
https://gh-proxy.com/https://raw.githubusercontent.com/chenqq2026/Tvbox-public/main/config_all.json
```

### 备用（gh-proxy 抽风时换这个，内容完全相同）

```
https://ghproxy.net/https://raw.githubusercontent.com/chenqq2026/Tvbox-public/main/config_all.json
```

> 为什么不推荐 jsDelivr：它 CDN 缓存 12 小时、客户端本地缓存 7 天。配置每天更新，
> 用 jsDelivr 会导致「明明更新了，盒子上还是旧的」，很容易误判成自动更新失效。

---

## 二、里面有什么

| 组成 | 内容 | 维护方式 |
|---|---|---|
| 站点 | 约 165 个：8 个实测直连 CMS（置顶）+ 157 个 spider 站 | 每天随 qist 上游更新；直连站每天探活 |
| spider 引擎 | `jar/spider.jar`，**镜像在本仓库**，带 md5 校验 | 每天随 qist 更新，上游删库也不影响 |
| 直播 | 约 480 条线路 / 320+ 个频道（CCTV1–17 每台 3 条 + 卫视全量），数字随上游浮动 | 双源合并，逐个验证 + 逐条测速 + 上游撤台复验 |
| 节目单 | EPG：`epg.cdn.loc.cc`（JSON 格式） | 每天探测，源挂了自动换下一个 |
| 台标 | `logo.wyfc.qzz.io` | 静态 |
| 解析器 | 27 个（去重后），实测可用的排前面 | 每天实测排序 |

**健康状态实时看 [`health.json`](health.json)**：jar 版本、站点数、解析器可用数、直播带宽中位数、EPG 状态，都是每次运行自动写入的。

直播是**双源合并**：主源 `vbskycn/iptv`（地方台全，但已不含 CCTV-1/2/3/4/6/7/9/10/11/14），
补源 `Guovin/iptv-api`（央视 1–17 每台多线路）。少任何一个都会缺台——这是实测出来的。

---

## 三、自动更新每天做什么

`.github/workflows/daily-update.yml`，每天 UTC 21:17（北京 05:17，qist bot 更新之后）：

1. **拉 qist 最新 `jsm.json`** → 修相对路径 → `jsm_fixed.json`
2. **拉 `spider.jar`** → 校验（ZIP 头 + 体积 + 与 qist 声明的 md5 一致）→ 镜像到 `jar/spider.jar`
3. **直播**：主源 vbskycn + 补源 iptv-api（只取央视/卫视）→ 逐频道请求验证（失败补测一轮）→ 逐条下载分片实测带宽 → 央视/卫视留 3 条、其余留 2 条 → 低于 500kbps 标「（慢）」→ 上一版里「上游已撤但复验仍能播」的线路补回来
4. **直连 CMS 探活**：8 个自持站当天返回不了片单的临时剔除
5. **解析器**：实测排序，把挂掉的排到后面
6. **EPG**：探测候选源，选可用的写入配置
7. **备用聚合镜像**：拉 `Lightconer/tvbox-ysc-config` 的单仓聚合 → 校验站数 → 剔掉它夹带的假直播源 → 连它的 spider jar 一起镜像 → 写 `spare_juhe.json`（任何一步失败都保留旧文件，不覆盖）
8. **写 `health.json`** + 有变化才提交

`.github/workflows/weekly-prune.yml`，每周一 05:47：对备用配置做站点健康巡检，
用 DNS 判定识别「域名已不存在」和「被 DNS 污染」的站点。**默认只报告不写文件**——
老 gao 配置里大量 `csp_` 内置源无法提取主域名，自动写入会把它退回未剔除版本。

**四道防抖保险**（防止一次网络抖动把成品写坏）：
- 拿不到合法 jar（体积/ZIP 头/md5 校验不过）→ 直接中止，保留线上旧版本
- **线路继承**：本轮连不上、但上次实测通过的线路仍然保留（CI 跑在海外机房，够不到国内源不等于线路已死。没有这层兜底时，每天在 CI 里跑会把好线一天天切少）
- **来源可信度判定**：上游拉到了、验证通过率 ≥ 50% → 条数变少按「上游真实精简」放行；只有来源可疑时才拦下，且低于 100 条绝不覆盖
- CCTV1/2/5/13、主要卫视若被漏判 → 从上游补一条进去，核心频道永不为空

> 这两种网络环境都不完美：Actions 在海外机房，到国内 IPTV 源的可达性和带宽都低于家宽；
> 本机则常被代理挡住大流量分片。所以 `health.json` 里的 `median_kbps` 只作**相对**参考
> （用来排序、标「（慢）」），不代表你家里的实际速度。同理，`live.measured_now` 和
> `inherited_kbps` 会告诉你这次有多少是实测、多少是沿用上次结果。

---

## 四、文件说明

| 文件 | 用途 |
|---|---|
| `config_all.json` | **日常使用**。单仓合并版：直连 CMS + spider 站 + 直播 + EPG |
| `pruned_config.json` | 备用。老 gao 配置剔除死域名后的干净版（约 252 站） |
| `direct_cms.json` | 应急最小配置。只有 8 个直连 CMS + 直播，7.9KB，不依赖任何 spider |
| `config.json` | 多仓入口（3 条线路）。**部分壳不支持，导入会提示解析失败** |
| `live_verified.m3u` | 验证 + 测速后的直播清单，可单独给任何 IPTV 播放器用 |
| `live_speed.json` | 每条线路的实测带宽，排查卡顿时看这个 |
| `health.json` | 最近一次运行的健康快照 |
| `jsm_fixed.json` | qist 今日快照（spider 已绝对化） |
| `jar/spider.jar` | spider 引擎镜像，配置里指向这里而不是 qist 上游 |
| `dropped_sites.txt` | 每周剔站清单（站点 key / 名称 / 判定原因） |
| `spare_juhe.json` | **备用聚合配置**（517 站），主配置出问题时临时顶替用，每天自动镜像 |
| `jar/spare_spider.jar` | `spare_juhe.json` 专用的 spider 引擎（1MB），跟着备用配置一起更新 |

---

## 五、常见问题

**Q：提示「解析配置失败」**
1. 先确认用的是 `config_all.json`（单仓）。`config.json` 是多仓格式，部分壳不认。
2. 再重试 2 次——gh-proxy 偶发瞬断，TVBox 拉取那一刻断了就会报这个。
3. 还不行换备用域名（把 `gh-proxy.com` 换成 `ghproxy.net`）。

**Q：能进配置，但点开站点没反应 / 一直转圈**
大概率是 spider 引擎没加载。到 `health.json` 看 `jar.declared_match` 是否为 `true`；
再看播放器设置里的「spider 参数」是否为空。本仓库的 spider 指向自己仓库的 jar，不依赖 qist。
如果只有部分站这样，那是站点本身挂了（spider 站的生命周期普遍只有几个月）。

**Q：某个频道很卡**
看 `live_verified.m3u` 里该频道名有没有「（慢）」标记，或者查 `live_speed.json` 里它的 kbps。
同一频道通常有 2 条线路（`CCTV1`、`CCTV1▪2`），换另一条试。

**Q：没有节目单（EPG）**
先看 `health.json` 的 `epg` 字段是不是「无可用源」。公共 EPG 服务生命周期很短，
四个候选（112114 / fanmingming / epg.pw / 51zmt）目前基本全挂，现用的是 `epg.cdn.loc.cc`。
彻底解决只能自建（iptv-api 支持输出 EPG）。

**Q：为什么站点列表比网上那些「几千个站」的配置少这么多**
那些配置里大部分站早就死了，点开一个崩一个。这里的站点都做过域名存活判定，
但**域名活着不等于站点能用**——spider 站依赖对方网站结构，改版即失效，这是生态本身的限制。

---

## 六、说明

- 本仓库只存**配置和索引**，不存储、不分发任何视频内容。所有内容来自第三方公开站点。
- 源可用性受地区、运营商、时间影响，无法保证长期稳定。
- 仅用于个人学习与技术研究，请勿用于商业用途；内容版权归各原始站点所有。

---

## 七、备用订阅（别人维护的，2026-11-04 实测）

主配置哪天整体出问题（源站大面积失效、jar 拉不到），遥控器里临时换一个顶上用。
**下面每个地址都是当天亲手拉过、解析成功的**，没通过的单独列在最后，省得你再一个个试。

### 🟢 第一梯队：本仓库镜像（推荐，地址固定不会变）

有个仓库 `Lightconer/tvbox-ysc-config` 每天用 Actions 把肥猫、饭太硬、王二小、老刘备、
小马、欧歌、4K小盒子等十几个源抓下来合并成一个单仓文件——**等于把肥猫和饭太硬的镜像搬过来了**。
本仓库每天自动把它镜像一份，好处是地址固定、干净：

```
https://gh-proxy.com/https://raw.githubusercontent.com/chenqq2026/Tvbox-public/main/spare_juhe.json
```

| 项 | 值 |
|---|---|
| 站点 | 517 个（含 `肥猫`、`csp_FeiMaoUC` 等） |
| 直播 | 17 条（上游 21 条，已剔除其夹带的 EPG 测试源） |
| spider | **已镜像到本仓库** `jar/spare_spider.jar`，带 md5 校验 |
| 更新 | 每天随主配置一起同步；上游挂了保留最后一次可用的 |

镜像时做的事：剔掉它夹带的 EPG 测试直播源（21→17 条）、清空它的第三方壁纸和桌面图标
（`深色壁纸.xxooo.cf` / `w.junce.dpdns.org`，换域名就变叉）、把 `warningText` 改成标注本仓库只做镜像。

> 它的 `hosts`（5 条）和 `proxy`（17 条）字段**原样保留**——那些是把咪咕直播、海外源
> 的域名重定向到可用 CDN 的规则，删了会大面积播放失败。知情的点：这意味着配置会在
> 播放器层面改写部分域名的解析，介意就别用备用源，主配置没有这些东西。

> 上游原本的 `spider` 指向一个**改名成 `.png` 的 jar**，挂在第三方 CDN（`ss1.eisite.cn`）上，
> 那个 CDN 一挂五百多个站全废。镜像时连 jar 一起搬过来了，所以现在只依赖本仓库自己。

上游原始地址（本仓库镜像的来源，它自己挂了不影响你）：

```
https://gh-proxy.com/https://raw.githubusercontent.com/Lightconer/tvbox-ysc-config/main/output/%E5%8D%95%E4%BB%93%E8%81%9A%E5%90%88.json
https://gh-proxy.com/https://raw.githubusercontent.com/rtian001/tvbox/main/output/%E5%8D%95%E4%BB%93%E8%81%9A%E5%90%88.json
```

### 🟡 第二梯队：国内原站（直连，站数少但不用套代理）

| 名称 | 地址 | 实测 |
|---|---|---|
| 老刘备 | `https://raw.liucn.cc/box/m.json` | 单仓 234 站 / 3 直播（文件以 `//` 注释开头，TVBox 能认） |
| 王二小 | `https://9280.kstore.space/wex.json` | 单仓 63 站 / 2 直播 |
| 小马 | `https://szyyds.cn/tv/x.json` | 单仓 62 站 / 10 直播 |
| 小盒子 | `http://xhztv.top/xhz` | 单仓 54 站 / 1 直播 |

### 🔵 多仓格式（**你的壳不支持，导入会报「解析配置失败」**，仅记录）

| 名称 | 地址 | 实测 |
|---|---|---|
| 挺好分享 | `http://ztha.top/TVBox/GYCK.json` | 多仓 32 线路 |
| rtian001 多仓 | `https://gh-proxy.com/https://raw.githubusercontent.com/rtian001/tvbox/main/output/%E5%A4%9A%E4%BB%93%E8%AE%A2%E9%98%85.json` | 多仓 9 线路 |

### ❌ 当天实测拉不到的（别再浪费时间试）

肥猫（`肥猫.net/tv` 404、`肥猫.com` 502、`我不是.肥猫.live` 404）、
饭太硬（主域超时、`饭太硬.net/tv` 返回的是一张**口令图片**、`饭太硬.top/tv` 返回防爬 HTML）、
欧歌 `tv.nxog.top/m/`（连接重置）、巧技 `cdn.qiaoji8.com`、传说、Ray、七星、天微、
4K小盒子、拾光、环宇轩、道长。

这些站靠域名轮换 + 图片口令防封，今天通明天就换；想追最新地址得去它们的 Telegram 频道。
**这也正是本仓库不把它们的站点合并进主配置的原因之一**——合并进来等于给自己埋一个随时会炸的依赖。

### 用之前要知道的三件事

1. **站多 ≠ 站能用。** 517 站的大聚合里，大量站点配的是别人 jar 的 `csp_` 扩展，
   和你的 jar 不匹配就点不开。本仓库的 165 站是每天探活筛过的，能开的比例高得多。
2. **别指望自动更新。** 这些地址是别人在维护，失效是常态，用之前先确认能打开。
3. **换回主配置很简单**：设置 → 配置地址 → 粘回第一节那个 `config_all.json` 地址即可。
