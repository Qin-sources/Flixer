<p align="center">
  <a href="README.md">简体中文</a> · <b>English</b>
</p>

# Flixer

**One click from “I want to watch this in 4K” to actually watching it.**

A self-hosted movie & series hub: browse with Douban / TMDB, find sources across netdisk search
engines, and play through **mpv via a direct link**. Real 4K, real Dolby, instant seeking.

> Personal study and research only. **Not sold, no ads, no paid tier.**
> A voluntary tip jar sits at the bottom of this page; it changes nothing about the software.


## Screenshots

| Home | Discover |
|---|---|
| ![Home](docs/shot-home.png) | ![Discover](docs/shot-filter.png) |

| Detail | Sources (auto-verified) |
|---|---|
| ![Detail](docs/shot-detail.png) | ![Sources](docs/shot-search.png) |

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
