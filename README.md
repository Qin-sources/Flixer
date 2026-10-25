# Flixer

一个跑在自己电脑上的影视聚合站：**豆瓣/TMDB 选片 + 网盘搜索找源 + mpv 直接播放**。
不搞盗链聚合站那套，也不做 CMS——只要「点一下就能看 4K」。

> 本项目仅供个人学习与技术研究，**不售卖、不接广告、不做付费版**。
> 如果它帮到了你，页尾有投喂入口（完全自愿，不影响任何功能）。

- 前端：单文件 `static/index.html`（无框架、无构建）
- 后端：Python（Flask + waitress），端口 `7100`
- 播放：本机 **mpv**（网页只做选片与传参，画面全在 mpv 里）
- 片源：`pansou.top`（SSE 接口）+ 本地 PanSou 插件聚合
- 元数据：TMDB → 豆瓣 → OMDb/TVmaze → showpaw

---

## 〇、跑起来需要三样东西（缺一不可）

| 依赖 | 干什么用 | 怎么准备 |
|---|---|---|
| **TMDB API Key** | 拿片名/海报/简介/筛选数据 | 注册 <https://www.themoviedb.org> → 设置 → API → 申请 v3 key（免费），把 32 位 key 存到 `data/tmdb_key.txt` |
| **mpv 播放器** | 真正播放视频（网页只做遥控） | 从 <https://mpv.io/installation/> 下载 Windows 版，把 `mpv.exe` 放到 `bin/mpv/mpv.exe` |
| **夸克网盘 Cookie** | 解析网盘直链 | 浏览器登录 <https://pan.quark.cn> → F12 → Network 任一请求 → 复制 `Cookie`，存到 `data/quark_cookie.json` |

> 三者都在本地，`data/` 目录已被 `.gitignore` 忽略，不会被提交。
> 没有 TMDB key：能搜片、能播放，但没有简介/海报/筛选。
> 没有 mpv：页面会提示「已在 mpv 播放」但实际没有画面。
> 没有夸克 cookie：解析直链会 412，什么也播不了。

---

## 一、普通用户：怎么跑起来

### 1. 你需要准备什么

| 东西 | 用途 | 必需 |
|---|---|---|
| Python 3.10+ | 跑后端 | 是 |
| 夸克网盘账号 | 解析网盘直链（cookie 存本地） | **是**（否则 412，播不了） |
| TMDB API Key（v3，免费申请） | 片名/海报/简介/筛选 | **强烈建议**（否则没有简介） |
| mpv | 播放器（放 `bin/mpv/mpv.exe`） | **是**（否则没有画面） |
| ffmpeg | 探测音视频编码（放 `bin/ffmpeg.exe`） | 是 |
| PanSou（可选） | 本地网盘搜索兜底 | 否 |

### 2. 配置（都放本地，不会上传）

```
data/tmdb_key.txt        # 一行，32 位 TMDB v3 key
                         # 申请：themoviedb.org → 设置 → API → 创建 → 复制 "API Key (v3 auth)"

data/quark_cookie.json   # 夸克 cookie
                         # 获取：登录 pan.quark.cn → F12 → Network → 任意请求
                         #       → Request Headers → 复制整行 Cookie
                         # 内容形如：{"cookie": "__puus=...; __pus=...; ..."}
```

`data/quark_cookie.json` 也可以只写一行纯 cookie 字符串（程序两种都能读）。

`data/` 整个目录已写进 `.gitignore`，**不会**被提交到仓库。

### 3. 启动

```bash
pip install -r requirements.txt
python server.py
```

浏览器打开 `http://127.0.0.1:7100`。

启动时它会自动：
- 拉起本地 PanSou（如果装了）
- 预热首页/榜单（首个访客不吃冷启动）
- 预热 mpv 进程（播放时秒起）

### 4. 怎么用

```
首页/榜单     挑片
搜索          输入片名 → 拿到多网盘资源列表（夸克为主）
筛选          地区 + 门类 + 类型 + 排序，滚到底自动加载
详情页        看简介、片源列表、每行有「链接」按钮（可复制分享链接+提取码）
播放          点「播放」→ mpv 弹出并开始播放
电视剧        点播放先弹选集，选完直接播那一集
换集          mpv 里按 > / n（网页提示条里也有集号按钮）
```

---

## 二、Netflix 风格的仪式感

这不是「功能堆完就交货」的界面。整套 UI 是按 **Netflix 的克制感**做的：

- **开屏动画**：品牌标淡入 → 光晕扩散 → 主视觉升起，进入首页前留一小段空白，让「看电影」有仪式感
- **纯粹体验**：没有推荐弹窗、没有运营位、没有「下载 App 送会员」；一屏之内只有内容
- **界面语言**：深黑底 + 单一红色强调色，卡片圆角、缓慢悬停、克制的动效时长
- **播放即交棒**：点播放后网页退场，画面交给 mpv——不在网页里假装播放器

> 原则：**少即是高级。** 每个动效都要回答「它是否让内容更突出」，否则删掉。

### 它解决的核心矛盾

- **聚合站**给你转码后的 1080p：码率被压、音轨被丢，4K 只是个标签
- **网盘播放**要转存、要会员等级、要忍限速和广告
- **浏览器播放器**解不了杜比、切不了轨，进度条还会闪

Flixer 的答案：**让专业播放器干专业的事。** 夸克直链 → mpv 硬解 → 秒拖。
网页只做它擅长的：找片、筛片、选集、传参。

---

## 三、给 AI / 开发者：仓库地图

### 目录结构

```
server.py                 所有 HTTP 路由（Flask）
services/
  meta2.py                元数据编排：TMDB → 豆瓣 → OMDb/TVmaze → showpaw
  discover.py             精确筛选（TMDB /discover，地区+类型+门类+分页）
  dual.py                 双源搜索（pansou.top + 本地 PanSou）+ 结果打分过滤
  ptsou.py                pansou.top 源：SSE 流 + 加密串解密
  pansou.py               本地 PanSou 进程管理（自动启动、代理、超时）
  quark.py                夸克网盘 API（分享解析、直链、share_state 状态判定）
  play.py                 播放准备（直链、音视频探测、选集）
  mpvctl.py               mpv 进程控制（命名管道 IPC、窗口摆位、选轨、单实例守卫）
  audio.py / probe.py     音视频编码探测
  home.py / browse.py     首页与榜单
  filter.py               旧筛选（豆瓣 tag，已基本被 discover.py 取代）
  subsrc.py / subjob.py   在线字幕（assrt 源）
static/index.html         唯一前端（约 5.5k 行，含所有 CSS/JS）
```

### 关键不变量（改代码前必读）

1. **搜索链路可走代理，播放链路必须直连。**
   播放如果走代理会卡；搜索（尤其 TG/pansou.top）走代理才通。
2. **播放判定「问浏览器」，不按片源硬判。**
   把音轨/视频编码翻成 MIME → `canPlayType`。解不了才交给 mpv。
3. **mpv 启动前必须 `_kill_strays()`。**
   否则会播进残留的不可见实例，表现是「提示已在 mpv 播放但没有窗口」。
4. **mpv 起播先把 `vid` 设成真视频轨再开画面。**
   假轨（防和谐 Notice）会先闪一下白字，所以启动参数带 `--vid=no`，选好轨再打开。
5. **硬解失败要能自动降级。**
   6~9 秒内出帧 <5 → 切 `hwdec=no`，并把「这台机器不行」写进 `data/mpv_hw.json`，下次直接软解。
6. **前端禁止按视图名判断界面。**
   用 DOM 存在性（如 `#fvGrid`）判断当前视图，否则会误重绘、闪屏、跳回顶部。
7. **一切延迟回调都要能被取消。**
   切片时清 `__plTimers`，否则上一部片的定时器会改当前界面（片名/时长来回闪）。

### 常见坑（都真实踩过）

| 现象 | 真实原因 |
|---|---|
| 加载本地页面却拿到旧 JS | 有两个 `server.py` 进程同时在跑 + 浏览器缓存 |
| 搜索一直 0 条 | `Referer` 头里写了中文（urllib3 只接受 latin-1），异常被吞 |
| 死链被标成「需要提取码」 | 库里缓存了旧校验结果，没现场复查（夸克 41011/41012 = 已失效） |
| 首页海报是外国片 | OMDb 用中文片名搜到了同名西片，海报覆盖了豆瓣的 |
| 4K 起播黑屏有声 | 显卡 HEVC 硬解不兼容，需自动降级软解 |
| 进度条来回闪 | CSS 用了错的元素 id（`#prog` vs `#plProg`），底轨溢出 |
| 翻页白屏 | 第二页请求失败时把整个网格 innerHTML 覆盖成错误提示 |

### 验证命令（改完必须跑）

```bash
node --check <(提取 static/index.html 里的内联 script)   # 前端语法
python -m py_compile server.py services/*.py              # 后端语法
curl -s "http://127.0.0.1:7100/api/health"
curl -s "http://127.0.0.1:7100/api/discover?media=电影&region=美国&genre=喜剧"
curl -s "http://127.0.0.1:7100/api/search?q=兰香如故"
```

---

## 三·五、技术栈与依赖项目

**技术栈**：Python（Flask + waitress）· 单文件前端（无框架、无构建）· mpv IPC · SQLite 缓存

| 项目 | 用途 |
|---|---|
| [PanSou](https://github.com/fish2018/pansou) | 网盘搜索内核（本地插件聚合） |
| [mpv](https://mpv.io/) | 播放器（直连、硬解、选轨） |
| [FFmpeg](https://ffmpeg.org/) | 音视频编码探测 |
| [TMDB](https://www.themoviedb.org/) | 影视元数据与筛选 |
| 豆瓣 | 榜单与中文资料 |
| [Scrapling](https://github.com/D4Vinci/Scrapling) | 反爬抓取兜底 |
| [pansou.top](https://pansou.top/) | 第三方搜索源（SSE 接口） |
| [Motion One](https://motion.dev/) | 轻量动效 |

**开发方式**：本项目使用 **[DSH（DeepSeek Harness）](https://github.com/deepseek-ai)** 完成——
需求拆解、代码编写、抓包调试、验收测试全部在 DSH 会话里进行，关键结论都以实测数字为准。

> **版权声明**：本项目不存储、不托管任何影视资源，所有内容均来自第三方公开分享链接。
> 若相关权利人认为本项目侵犯其权益，**请联系作者，会立即删除**。

---

## 四、开发历程（踩坑实录）

这个项目不是一次写成的。下面是关键节点，数字都是当时实测的。

**1. 播放卡顿（已修）**
最初 `/api/stream` 按 8MB 有界段串行拉流，每段都要重连夸克，接缝停 0.4~0.6 秒 → 「开头流畅、几秒后一卡一卡」。
改成生产者线程预取 + 约 6MB 队列后：吞吐 4.44 → 6.16 MB/s，100MB 连读中 >350ms 卡顿 4 次 → 0 次。

**2. 元数据太慢（已修）**
首页冷启动 35.58s → 1.17s，榜单 4.64s → 0.05s。
手段：旧缓存过期先返回（stale-while-revalidate）+ 冷启动只返回榜单本体 + 启动预热。

**3. 播放判定（推翻重做）**
一开始按片源音轨硬判「要不要 mpv」，结果同一部片在 Chrome 和夸克里行为不一致。
后来实测三家浏览器：

```
360安全16.1 / Edge155 / 夸克7.4   都能解 HEVC + Dolby E-AC3 + AC3
Chrome                            解不了 ec-3 / ac-3
DTS / TrueHD                      四家全不支持 → 只能 mpv
```

于是改成「问浏览器能不能解」，播放路径由运行时能力决定。

**4. 从网页播放改成全 mpv**
网页播放器要处理遮罩、进度条、全屏、mpv 窗口对位……每一块都能出 bug（进度条闪、控制条跑出屏幕、窗口叠在一起）。
最后直接删掉播放页：点播放 → mpv 起播 + 一个轻提示。**问题从根源消失**。

**5. mpv 的一串坑（都已修）**

```
两个 mpv 窗口         → 加 _io_lock + 单实例守卫 _kill_strays()
窗口位置偏右下          → SetWindowPos 用逻辑像素，不要再乘 devicePixelRatio
窗口闪                → 摆位容差 10px，贴合即停
播一会自动退出         → 网页心跳（5 秒一次）喂看门狗
先闪假轨再出正片       → 起播 --vid=no，选好正片轨再开画面
黑屏有声              → 硬解不兼容，6 秒无帧自动切软解并记住
```

**6. 搜索源（绕了一大圈）**
本地 PanSou 的插件是**异步**的：接口 3~4 秒就返回（此时 total=0），插件跑完才写缓存。
所以之前「搜索很慢/没结果」其实是**只查了一次**。改成两段式：先查一次，结果少于 3 条时等 4 秒再查一次合并。

后来接 `pansou.top`：抓包发现它是 **SSE 流式推送**，每条结果还带一个加密串，要再 POST `/api/other/save_url` 才拿到真实夸克链接。
接上之后《兰香如故》从 2 条变成 25 条（验活后剩 7 条真正可用的 4K 源）。

**7. 死链误判**
一条分享被标成「需要提取码」，点进去却是无效的。直接问夸克：

```
41011 分享地址已失效
41012 好友已取消了分享
```

而库里存的是更早的 `NEED_PASSCODE`。于是加了 `share_state()` 现场复查，死链不再骗人输密码。

**8. 筛选重做**
原来走豆瓣 tag，做不出「美国 + 喜剧」这种组合——要么混进国产片，要么筛空。
改成 TMDB `/discover`：地区、类型、门类都在服务端组合过滤。实测：

```
美国 + 喜剧    20001 部
韩国 + 悬疑剧   422 部
动漫 + 日本    5551 部
```

---

## 五、已知限制

- **只支持夸克播放**。其他网盘能搜到，但播放链路只接了夸克。
- **DTS / TrueHD 必须走 mpv**，浏览器内核都不支持。
- **多视频轨（防和谐假轨）**浏览器无法切轨，只能靠 mpv 选轨（已自动选分辨率最大的真视频轨）。
- **pansou.top 是第三方站点**，随时可能改接口或失效。
- 4K HEVC 在不兼容的显卡上会先黑几秒（硬解失败 → 自动降级），之后所有片子直接软解。
- 电视剧换集的播放列表是后台逐集解析后追加的，40 集大约 15~30 秒才追全。

---

## 六、支持作者

这个项目花了很久。如果它帮到了你，**欢迎投喂作者**——完全自愿，不影响任何功能，
也不构成任何商业授权。

<table>
<tr>
<td align="center" width="50%">
  <img src="docs/reward-wechat.jpg" width="260" alt="微信收款码"><br>
  <b>微信支付</b>
</td>
<td align="center" width="50%">
  <img src="docs/reward-alipay.jpg" width="260" alt="支付宝收款码"><br>
  <b>支付宝</b>
</td>
</tr>
</table>

不投喂也完全没问题——**点个 Star、提个 Issue、把体验告诉我**，同样有用。

---

## 七、免责声明

本项目仅用于**个人学习与技术研究**，**绝不商用、绝不盈利**，不存储任何影视资源，
所有内容均来自第三方公开分享链接。请勿将本项目用于任何商业或盈利目的。
使用者需自行确保其行为符合当地法律法规与各网盘服务条款。因使用本工具产生的一切后果由使用者自行承担。

如权利人认为内容侵权，请联系作者，**会立即删除相关内容**。

---

## 八、致谢

- [PanSou](https://github.com/fish2018/pansou) —— 网盘搜索内核
- [mpv](https://mpv.io/) —— 播放器
- [TMDB](https://www.themoviedb.org/) —— 影视元数据
- 豆瓣 —— 榜单与中文资料
- [Scrapling](https://github.com/D4Vinci/Scrapling) —— 反爬抓取兜底

---

# Flixer — English

**One click from “I want to watch this in 4K” to actually watching it.**

A self-hosted movie & series hub: browse with Douban / TMDB, find sources across netdisk search
engines, and play through **mpv via a direct link**. Real 4K, real Dolby, instant seeking.

> Personal study and research only. **Not sold, no ads, no paid tier.**
> A voluntary tip jar sits at the bottom of this page; it changes nothing about the software.

## 0. Three things you must have

| Dependency | Purpose | How to get it |
|---|---|---|
| **TMDB API Key** | Titles, posters, overviews, discover filters | Register at <https://www.themoviedb.org> → Settings → API → free v3 key → `data/tmdb_key.txt` |
| **mpv** | The actual player (web UI is only a remote) | Windows build from <https://mpv.io/installation/> → `bin/mpv/mpv.exe` |
| **Quark cookie** | Resolving netdisk direct links | Log in at <https://pan.quark.cn> → F12 → Network → copy `Cookie` → `data/quark_cookie.json` |

## 1. What problem it solves

- **Aggregator sites** hand you transcoded 1080p — bitrate squeezed, audio dropped, "4K" is a label
- **Netdisk players** want you to save the file first, then throttle you anyway
- **Browser players** cannot decode Dolby, cannot switch tracks, and their progress bar flickers

Flixer's answer: **let the professional player do the professional job.**
Quark direct link → mpv hardware decoding → instant seek. The web UI only does what it is good at:
finding, filtering, picking episodes, passing parameters.

## 2. Netflix-grade ritual

A deliberately restrained, Netflix-flavoured interface: an opening brand animation
(logo fade-in → light bloom → hero rise) before the home page, a dark canvas with a single red accent,
slow hover states, and **zero** promo popups. When you press play, the page steps aside — the picture
belongs to mpv, not to a fake in-page player.

> Principle: **less is premium.** Every animation must make the content stand out, or it gets deleted.

## 3. Dirty problems already solved

| Problem | Fix |
|---|---|
| Dead share links | Ask Quark per link (`41011` expired / `41012` revoked) instead of letting you click into nothing |
| Decoy video track | Detect the fake "notice" track and pick the real one before playback starts |
| Access codes | Parsed automatically from URL params and share titles |
| Black screen with audio | GPU HEVC incompatibility → auto software decode, remembered per machine |
| Filters | TMDB discover: "US + Comedy" no longer returns Chinese films |
| Slow search | PanSou plugins are async → two-phase query (partial results first, refill after 4s) |
| Scroll flicker | Fade-in only on first render, not on every page append |

## 4. Stack & credits

Python (Flask + waitress) · single-file frontend · mpv IPC · SQLite cache

Built with [PanSou](https://github.com/fish2018/pansou), [mpv](https://mpv.io/),
[FFmpeg](https://ffmpeg.org/), [TMDB](https://www.themoviedb.org/), Douban,
[Scrapling](https://github.com/D4Vinci/Scrapling), [pansou.top](https://pansou.top/),
[Motion One](https://motion.dev/).

**Developed with [DSH (DeepSeek Harness)](https://github.com/deepseek-ai)** — requirement breakdown,
implementation, packet-level debugging and acceptance testing all happened inside DSH sessions,
backed by measured numbers.

## 5. Known limitations

- Quark only for playback
- **DTS / TrueHD must go through mpv**
- Multi-video-track files need mpv to choose the real track
- pansou.top is third-party and may change or disappear
- 4K HEVC may show a few seconds of black on unsupported GPUs
- Episode playlist is appended in the background (40 episodes ≈ 15–30s)

## 6. Support the author

If this project helped you, **you are welcome to buy the author a coffee**. Completely voluntary —
it unlocks nothing and is not a commercial license.

| WeChat Pay | Alipay |
|---|---|
| <img src="docs/reward-wechat.jpg" width="220"> | <img src="docs/reward-alipay.jpg" width="220"> |

A **Star**, an **Issue**, or honest feedback helps just as much.

## 7. Disclaimer

Personal study and research only. Not sold, no ads, no paid edition. No media is stored or hosted;
everything comes from third-party public share links. You are responsible for complying with
applicable laws and provider terms. If a rights holder believes content infringes,
**contact the author and it will be removed immediately**.
