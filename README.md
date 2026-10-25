# Flixer

<p align="center">
  <b>简体中文</b> · <a href="README.en.md">English</a>
</p>

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

---

## 界面一览

| 首页（开屏动画后的主视觉） | 精确筛选 |
|---|---|
| ![首页](docs/shot-home.png) | ![筛选](docs/shot-filter.png) |

| 影片详情 | 网盘资源（自动验活） |
|---|---|
| ![详情](docs/shot-detail.png) | ![搜索](docs/shot-search.png) |

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

## 四、已知限制

- **只支持夸克播放**。其他网盘能搜到，但播放链路只接了夸克。
- **DTS / TrueHD 必须走 mpv**，浏览器内核都不支持。
- **多视频轨（防和谐假轨）**浏览器无法切轨，只能靠 mpv 选轨（已自动选分辨率最大的真视频轨）。
- **pansou.top 是第三方站点**，随时可能改接口或失效。
- 4K HEVC 在不兼容的显卡上会先黑几秒（硬解失败 → 自动降级），之后所有片子直接软解。
- 电视剧换集的播放列表是后台逐集解析后追加的，40 集大约 15~30 秒才追全。

---

## 五、支持作者

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

## 六、免责声明

本项目仅用于**个人学习与技术研究**，**绝不商用、绝不盈利**，不存储任何影视资源，
所有内容均来自第三方公开分享链接。请勿将本项目用于任何商业或盈利目的。
使用者需自行确保其行为符合当地法律法规与各网盘服务条款。因使用本工具产生的一切后果由使用者自行承担。

如权利人认为内容侵权，请联系作者，**会立即删除相关内容**。

---

## 七、致谢

- [PanSou](https://github.com/fish2018/pansou) —— 网盘搜索内核
- [mpv](https://mpv.io/) —— 播放器
- [TMDB](https://www.themoviedb.org/) —— 影视元数据
- 豆瓣 —— 榜单与中文资料
- [Scrapling](https://github.com/D4Vinci/Scrapling) —— 反爬抓取兜底

