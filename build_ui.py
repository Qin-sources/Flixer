"""
Flixer 骨架前端（阶段 1）
验证链路：搜索 → 详情 → 剧集列表
视觉先做对信息层级，Netflix 皮肤留到阶段 3。
"""
HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Flixer</title>
<style>
  :root{
    --bg:#0b0b0f; --panel:#16161d; --panel2:#1d1d26; --line:#2a2a36;
    --fg:#e8e8ef; --dim:#8f8fa3; --accent:#e50914; --gold:#f5c518; --good:#2ecc71;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:15px/1.6 "Segoe UI","Microsoft YaHei",system-ui,sans-serif}
  header{position:sticky;top:0;z-index:20;background:rgba(11,11,15,.92);
         backdrop-filter:blur(12px);border-bottom:1px solid var(--line);padding:14px 22px;
         display:flex;gap:14px;align-items:center}
  .logo{font-weight:800;font-size:21px;color:var(--accent);letter-spacing:-.5px}
  .search{flex:1;max-width:620px;display:flex;gap:8px}
  .search input{flex:1;background:var(--panel);border:1px solid var(--line);color:var(--fg);
                padding:11px 15px;border-radius:9px;font-size:15px;outline:none}
  .search input:focus{border-color:var(--accent)}
  .search button{background:var(--accent);border:0;color:#fff;padding:0 20px;border-radius:9px;
                 font-size:15px;font-weight:600;cursor:pointer}
  .search button:disabled{opacity:.5;cursor:default}
  main{padding:22px;max-width:1320px;margin:0 auto}
  .hint{color:var(--dim);font-size:14px}
  .bar{display:flex;gap:16px;flex-wrap:wrap;align-items:center;color:var(--dim);
       font-size:13px;margin:16px 0}
  .bar b{color:var(--fg);font-weight:600}
  .chip{background:var(--panel2);border:1px solid var(--line);border-radius:20px;
        padding:3px 11px;font-size:12px}

  /* 详情头 */
  .hero{position:relative;border-radius:14px;overflow:hidden;margin-bottom:22px;
        border:1px solid var(--line);background:var(--panel)}
  .hero-bg{position:absolute;inset:0;background-size:cover;background-position:center;
           filter:blur(2px) brightness(.32);transform:scale(1.06)}
  .hero-in{position:relative;display:flex;gap:22px;padding:22px}
  .poster{width:150px;flex:none;border-radius:10px;overflow:hidden;background:#000;
          box-shadow:0 8px 28px rgba(0,0,0,.6)}
  .poster img{width:100%;display:block}
  .meta h1{margin:0 0 6px;font-size:27px;letter-spacing:-.5px}
  .meta .sub{color:var(--dim);font-size:14px;margin-bottom:10px}
  .rate{color:var(--gold);font-weight:700}
  .ov{color:#c3c3d1;font-size:14px;max-width:760px;margin-top:10px}
  .cast{color:var(--dim);font-size:13px;margin-top:8px}

  /* 资源表 */
  .sect-title{font-size:17px;font-weight:700;margin:26px 0 12px;
              display:flex;align-items:center;gap:10px}
  .sect-title small{color:var(--dim);font-weight:400;font-size:13px}
  .res{display:flex;flex-direction:column;gap:8px}
  .row{display:grid;grid-template-columns:64px 1fr auto;gap:14px;align-items:center;
       background:var(--panel);border:1px solid var(--line);border-radius:10px;
       padding:12px 15px;transition:.15s}
  .row:hover{border-color:#3d3d4d;background:var(--panel2);transform:translateX(2px)}
  .score{font-weight:800;font-size:15px;color:var(--good);text-align:center}
  .score.mid{color:var(--gold)} .score.low{color:var(--dim)}
  .t1{font-size:14px;margin-bottom:4px;word-break:break-all}
  .t2{display:flex;gap:7px;flex-wrap:wrap;align-items:center}
  .tag{font-size:11px;padding:2px 8px;border-radius:5px;background:#23232e;
       color:#b9b9c9;border:1px solid var(--line)}
  .tag.quark{background:#152a45;color:#7ec4ff;border-color:#1f4a75}
  .tag.aliyun{background:#2a1f45;color:#b98cff;border-color:#432f6b}
  .tag.baidu{background:#1f2a45;color:#8cb8ff;border-color:#2f3f6b}
  .tag.xunlei{background:#1f3a45;color:#6fd8e8;border-color:#2f5566}
  .tag.good{background:#173a24;color:#6ee7a0;border-color:#1f5c39}
  .tag.bad{background:#3a1717;color:#ff8a8a;border-color:#5c1f1f}
  .btn{background:var(--accent);border:0;color:#fff;padding:8px 16px;border-radius:8px;
       font-size:13px;font-weight:600;cursor:pointer;white-space:nowrap}
  .btn.ghost{background:var(--panel2);border:1px solid var(--line);color:var(--fg)}

  .skel{height:66px;border-radius:10px;background:linear-gradient(90deg,#16161d 25%,#1f1f28 50%,#16161d 75%);
        background-size:200% 100%;animation:sh 1.2s infinite}
  @keyframes sh{0%{background-position:200% 0}100%{background-position:-200% 0}}
  .err{color:#ff8a8a;background:#2a1414;border:1px solid #5c1f1f;padding:12px 16px;
       border-radius:9px;margin:12px 0}
  .empty{color:var(--dim);padding:40px;text-align:center}
</style>
</head>
<body>
<header>
  <div class="logo">FLIXER</div>
  <div class="search">
    <input id="q" placeholder="搜片名，例如：奥本海默 / 庆余年 / 甄嬛传" autocomplete="off">
    <button id="go">搜索</button>
  </div>
  <div class="chip" id="health">…</div>
</header>
<main id="main">
  <div class="empty">输入片名开始。当前为「骨架验证」阶段：搜索 → 详情 → 剧集列表。</div>
</main>

<script>
const $ = s => document.querySelector(s);
const esc = s => (s||'').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function fmtGb(v){ return v ? (v >= 1 ? v.toFixed(1)+' GB' : (v*1024).toFixed(0)+' MB') : ''; }
function scoreCls(s){ return s >= 90 ? '' : (s >= 60 ? 'mid' : 'low'); }

(async () => {
  try {
    const j = await (await fetch('/api/health')).json();
    $('#health').textContent = '缓存 ' + j.data.cache.entries + ' 条';
  } catch(e){ $('#health').textContent = '离线'; }
})();

$('#go').onclick = doSearch;
$('#q').onkeydown = e => { if (e.key === 'Enter') doSearch(); };

async function doSearch(){
  const q = $('#q').value.trim();
  if (!q) return;
  $('#go').disabled = true;
  const main = $('#main');
  main.innerHTML = '<div class="bar"><b>正在搜索「' + esc(q) + '」…</b>' +
    '<span class="chip">首次约 8~20 秒（实时验活），之后走缓存毫秒级</span></div>' +
    Array.from({length:6}, () => '<div class="skel" style="margin-bottom:8px"></div>').join('');

  try {
    const r = await fetch('/api/search?q=' + encodeURIComponent(q));
    const j = await r.json();
    if (!j.ok) { main.innerHTML = '<div class="err">' + esc(j.error) + '</div>'; return; }
    render(j.data);
  } catch(e){
    main.innerHTML = '<div class="err">请求失败：' + esc(e.message) + '</div>';
  } finally { $('#go').disabled = false; }
}

function render(d){
  const t = d.tmdb || {};
  const st = d.stats || {};
  const res = d.resources || [];

  const hero = `
  <div class="hero">
    ${t.backdrop ? `<div class="hero-bg" style="background-image:url('${esc(t.backdrop)}')"></div>` : ''}
    <div class="hero-in">
      <div class="poster">${t.poster ? `<img src="${esc(t.poster)}" alt="">` : ''}</div>
      <div class="meta">
        <h1>${esc(t.title)}</h1>
        <div class="sub">
          ${esc(t.original_title || '')}
          ${t.year ? ' · ' + t.year : ''}
          ${t.media_type === 'tv' ? ' · 剧集' : ' · 电影'}
          ${t.vote_average ? ` · <span class="rate">★ ${t.vote_average.toFixed(1)}</span>` : ''}
          ${d.is_tv ? '' : ''}
        </div>
        <div class="ov">${esc(t.overview || '')}</div>
        ${t.reasons && t.reasons.length ? `<div class="cast">裁决依据：${t.reasons.join(' / ')}</div>` : ''}
      </div>
    </div>
  </div>`;

  const bar = `
  <div class="bar">
    <span>命中 <b>${res.length}</b> 条 ≥1080P</span>
    <span>合并去重 <b>${st.merged||0}</b></span>
    <span>showpaw 原始 <b>${st.showpaw_total_found||'?'}</b></span>
    ${d.cached ? '<span class="chip">来自缓存</span>' : `<span class="chip">耗时 ${d.elapsed_ms} ms</span>`}
  </div>`;

  const rows = res.map(r => {
    const q = r.quality || {};
    const tags = [];
    if (r.pan_type) tags.push(`<span class="tag ${esc(r.pan_type)}">${esc(r.pan_type)}</span>`);
    if (q.resolution) tags.push(`<span class="tag good">${esc(q.resolution)}</span>`);
    if (q.source) tags.push(`<span class="tag">${esc(q.source)}</span>`);
    if (q.hdr && q.hdr.length) tags.push(`<span class="tag good">${esc(q.hdr.join('/'))}</span>`);
    if (q.subtitle) tags.push(`<span class="tag good">${
      {embedded_bilingual:'内封双语',embedded_cn:'内嵌中文',external:'外挂字幕',none:'无字幕'}[q.subtitle] || q.subtitle}</span>`);
    if (q.size_gb) tags.push(`<span class="tag">${fmtGb(q.size_gb)}</span>`);
    if (r.is_alive === true) tags.push('<span class="tag good">存活</span>');
    if (r.is_alive === false) tags.push('<span class="tag bad">已失效</span>');

    return `<div class="row">
      <div class="score ${scoreCls(r.score)}">${Math.round(r.score)}</div>
      <div>
        <div class="t1">${esc(r.title)}</div>
        <div class="t2">${tags.join('')}</div>
      </div>
      <button class="btn ghost" onclick="probe('${esc(r.pan_url)}')">看剧集</button>
    </div>`;
  }).join('') || '<div class="empty">没有 ≥1080P 的资源</div>';

  $('#main').innerHTML = hero + bar +
    `<div class="sect-title">片源 <small>按 2K 屏加权排序（1080P 高码 > 4K REMUX）</small></div>
     <div class="res">${rows}</div>`;
}

async function probe(url){
  const main = $('#main');
  const old = main.innerHTML;
  main.innerHTML = '<div class="bar"><b>正在读取分享目录（免登录）…</b></div>' +
    Array.from({length:4}, () => '<div class="skel" style="margin-bottom:8px;height:44px"></div>').join('');
  try {
    const r = await fetch('/api/share?url=' + encodeURIComponent(url));
    const j = await r.json();
    if (!j.ok){
      main.innerHTML = old + '<div class="err">读取失败：' + esc(j.error || '') + '</div>';
      return;
    }
    const d = j.data;
    let html = '<div class="sect-title">分享内容 <small>' + esc(d.share_title||'') +
               ' · 共 ' + d.total_videos + ' 个视频</small></div>';
    if (d.is_series){
      for (const [s, eps] of Object.entries(d.seasons)){
        html += `<div class="sect-title">第 ${s || '?'} 季 <small>${eps.length} 集</small></div><div class="res">`;
        html += eps.map(e => `<div class="row"><div class="score low">${e.episode||'-'}</div>
          <div><div class="t1">${esc(e.name)}</div>
          <div class="t2">${e.quality?`<span class="tag good">${esc(e.quality)}</span>`:''}
          ${e.size_gb?`<span class="tag">${fmtGb(e.size_gb)}</span>`:''}</div></div>
          <button class="btn ghost">播放</button></div>`).join('');
        html += '</div>';
      }
    } else {
      html += '<div class="res">' + (d.movies||[]).map(e => `<div class="row">
        <div class="score low">·</div>
        <div><div class="t1">${esc(e.name)}</div>
        <div class="t2">${e.quality?`<span class="tag good">${esc(e.quality)}</span>`:''}
        ${e.size_gb?`<span class="tag">${fmtGb(e.size_gb)}</span>`:''}</div></div>
        <button class="btn ghost">播放</button></div>`).join('') + '</div>';
    }
    main.innerHTML = old + html;
    main.scrollIntoView({behavior:'smooth', block:'start'});
  } catch(e){
    main.innerHTML = old + '<div class="err">' + esc(e.message) + '</div>';
  }
}
window.probe = probe;
</script>
</body>
</html>
"""

if __name__ == "__main__":
    import os
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    os.makedirs(p, exist_ok=True)
    with open(os.path.join(p, "index.html"), "w", encoding="utf-8") as f:
        f.write(HTML)
    print("wrote", os.path.join(p, "index.html"))
