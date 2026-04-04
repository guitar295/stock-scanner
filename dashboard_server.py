"""
=============================================================================
DASHBOARD SERVER — Tích hợp vào scanner bot
Thêm vào cuối BƯỚC 1 (IMPORT):
    from dashboard_server import start_dashboard

Thêm vào BƯỚC 9 (sau khi khởi động listener):
    start_dashboard(
        alerted_today_ref  = lambda: alerted_today,
        history_cache_ref  = lambda: history_cache,
        cache_lock_ref     = cache_lock,
        fetch_heatmap_fn   = fetch_heatmap_data,
        signal_emoji_ref   = SIGNAL_EMOJI,
        signal_rank_ref    = SIGNAL_RANK,
        port               = 8080,
    )

Truy cập: http://YOUR_VPS_IP:8080
=============================================================================
"""

from flask import Flask, jsonify, Response
import threading
import json
import time
from datetime import datetime
import pytz

TZ_VN = pytz.timezone('Asia/Ho_Chi_Minh')
app   = Flask(__name__)

# ── Tham chiếu đến dữ liệu sống trong scanner ──────────────────────────────
_get_alerted_today  = None
_get_history_cache  = None
_cache_lock         = None
_fetch_heatmap_fn   = None
_signal_emoji       = {}
_signal_rank        = {}

# ── Cache heatmap (tránh gọi API mỗi lần refresh) ─────────────────────────
_heatmap_cache      = {"data": {}, "ts": "", "updated_at": 0}
_heatmap_lock       = threading.Lock()
HEATMAP_TTL_SEC     = 120   # làm mới heatmap mỗi 2 phút

# =============================================================================
# API ENDPOINTS
# =============================================================================

@app.route("/api/signals")
def api_signals():
    """Trả về danh sách tín hiệu hôm nay."""
    alerted = _get_alerted_today() if _get_alerted_today else {}
    result  = []
    for sym, sig in alerted.items():
        result.append({
            "symbol":  sym,
            "signal":  sig,
            "emoji":   _signal_emoji.get(sig, "📌"),
            "rank":    _signal_rank.get(sig, 0),
        })
    result.sort(key=lambda x: x["rank"], reverse=True)
    return jsonify({
        "signals":    result,
        "count":      len(result),
        "updated_at": datetime.now(TZ_VN).strftime("%H:%M:%S"),
    })


@app.route("/api/heatmap")
def api_heatmap():
    """Trả về data heatmap, có cache TTL 2 phút."""
    now = time.time()
    with _heatmap_lock:
        age = now - _heatmap_cache["updated_at"]
        if age > HEATMAP_TTL_SEC and _fetch_heatmap_fn:
            try:
                data, ts_str = _fetch_heatmap_fn()
                _heatmap_cache["data"]       = data
                _heatmap_cache["ts"]         = ts_str
                _heatmap_cache["updated_at"] = time.time()
            except Exception as e:
                print(f"  [Dashboard] ❌ Fetch heatmap lỗi: {e}")
        return jsonify({
            "data":       _heatmap_cache["data"],
            "timestamp":  _heatmap_cache["ts"],
            "cached_age": int(time.time() - _heatmap_cache["updated_at"]),
        })


@app.route("/api/cache_info")
def api_cache_info():
    """Thông tin cache lịch sử."""
    cache = _get_history_cache() if _get_history_cache else {}
    info  = []
    with _cache_lock:
        for sym, df in list(cache.items())[:10]:   # chỉ lấy 10 mã đầu để demo
            if df is not None and len(df) > 0:
                info.append({
                    "symbol":    sym,
                    "rows":      len(df),
                    "last_date": str(df.index[-1].date()),
                })
    return jsonify({
        "total_symbols": len(cache),
        "sample":        info,
        "updated_at":    datetime.now(TZ_VN).strftime("%H:%M:%S"),
    })


@app.route("/api/status")
def api_status():
    cache  = _get_history_cache() if _get_history_cache else {}
    return jsonify({
        "status":        "running",
        "cache_symbols": len(cache),
        "server_time":   datetime.now(TZ_VN).strftime("%H:%M:%S %d/%m/%Y"),
    })


@app.route("/")
def index():
    """Trả về trang dashboard HTML."""
    return Response(DASHBOARD_HTML, mimetype="text/html")


# =============================================================================
# KHỞI ĐỘNG
# =============================================================================

def start_dashboard(
    alerted_today_ref,
    history_cache_ref,
    cache_lock_ref,
    fetch_heatmap_fn,
    signal_emoji_ref,
    signal_rank_ref,
    port=8080,
):
    global _get_alerted_today, _get_history_cache, _cache_lock
    global _fetch_heatmap_fn, _signal_emoji, _signal_rank

    _get_alerted_today = alerted_today_ref
    _get_history_cache = history_cache_ref
    _cache_lock        = cache_lock_ref
    _fetch_heatmap_fn  = fetch_heatmap_fn
    _signal_emoji      = signal_emoji_ref
    _signal_rank       = signal_rank_ref

    def _run():
        import logging
        log = logging.getLogger('werkzeug')
        log.setLevel(logging.ERROR)   # tắt log mỗi request
        app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    print(f"🌐 Dashboard khởi động tại http://0.0.0.0:{port}")


# =============================================================================
# HTML DASHBOARD
# =============================================================================

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Scanner Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Syne:wght@400;700;800&display=swap" rel="stylesheet">
<style>
:root {
  --bg:       #080c12;
  --surface:  #0d1520;
  --border:   #1a2535;
  --accent:   #00d4ff;
  --accent2:  #00ff88;
  --text:     #c8d8e8;
  --muted:    #4a6070;
  --up:       #00e676;
  --dn:       #ff5252;
  --warn:     #ffd740;
  --font-mono:'JetBrains Mono', monospace;
  --font-ui:  'Syne', sans-serif;
}

* { margin:0; padding:0; box-sizing:border-box; }

body {
  background: var(--bg);
  color: var(--text);
  font-family: var(--font-mono);
  font-size: 13px;
  min-height: 100vh;
  overflow-x: hidden;
}

/* ── HEADER ── */
header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 24px;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
  position: sticky; top: 0; z-index: 100;
}
header h1 {
  font-family: var(--font-ui);
  font-size: 18px;
  font-weight: 800;
  letter-spacing: 2px;
  color: var(--accent);
  text-transform: uppercase;
}
.header-right { display:flex; gap:16px; align-items:center; }
#clock { color: var(--muted); font-size:12px; }
.dot-live {
  width:8px; height:8px; border-radius:50%;
  background: var(--accent2);
  box-shadow: 0 0 8px var(--accent2);
  animation: pulse 2s ease-in-out infinite;
}
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }

/* ── LAYOUT ── */
.container { padding:20px 24px; display:flex; flex-direction:column; gap:20px; }

/* ── STATS ROW ── */
.stats-row { display:grid; grid-template-columns: repeat(4, 1fr); gap:12px; }
.stat-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius:8px;
  padding:14px 18px;
  display:flex; flex-direction:column; gap:4px;
}
.stat-label { font-size:10px; color:var(--muted); text-transform:uppercase; letter-spacing:1px; }
.stat-value { font-size:28px; font-weight:700; font-family:var(--font-ui); }
.stat-value.green { color: var(--up); }
.stat-value.blue  { color: var(--accent); }
.stat-value.gold  { color: var(--warn); }
.stat-value.teal  { color: var(--accent2); }

/* ── MAIN GRID ── */
.main-grid { display:grid; grid-template-columns: 1fr 1fr; gap:20px; }

/* ── PANEL ── */
.panel {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius:8px;
  overflow:hidden;
}
.panel-header {
  padding:10px 16px;
  border-bottom:1px solid var(--border);
  display:flex; align-items:center; justify-content:space-between;
}
.panel-title {
  font-family: var(--font-ui);
  font-size:12px; font-weight:700;
  text-transform:uppercase; letter-spacing:1.5px;
  color: var(--accent);
}
.panel-meta { font-size:10px; color:var(--muted); }
.panel-body { padding:12px 16px; }

/* ── SIGNAL TABLE ── */
.signal-list { display:flex; flex-direction:column; gap:4px; }
.signal-row {
  display:grid;
  grid-template-columns: 32px 72px 1fr 90px;
  align-items:center;
  padding:8px 10px;
  border-radius:5px;
  border: 1px solid var(--border);
  transition: border-color 0.15s, background 0.15s;
  cursor:default;
  animation: fadeIn 0.3s ease;
}
@keyframes fadeIn { from{opacity:0;transform:translateY(4px)} to{opacity:1;transform:none} }
.signal-row:hover { background: rgba(0,212,255,0.04); border-color: rgba(0,212,255,0.2); }

.sig-emoji  { font-size:14px; text-align:center; }
.sig-sym    { font-weight:700; color:#fff; font-size:13px; }
.sig-type   { font-size:11px; color:var(--muted); }
.sig-badge  {
  font-size:10px; font-weight:700; padding:3px 8px;
  border-radius:4px; text-align:center; letter-spacing:0.5px;
}
.badge-BREAKOUT     { background:rgba(0,230,118,0.15); color:var(--up);   border:1px solid rgba(0,230,118,0.3); }
.badge-POCKET       { background:rgba(255,215,64,0.15); color:var(--warn); border:1px solid rgba(255,215,64,0.3); }
.badge-PREBREAK     { background:rgba(179,136,255,0.15);color:#b388ff;    border:1px solid rgba(179,136,255,0.3); }
.badge-BOTTOMBREAKP { background:rgba(0,212,255,0.15);  color:var(--accent);border:1px solid rgba(0,212,255,0.3); }
.badge-BOTTOMFISH   { background:rgba(255,152,0,0.15);  color:#ff9800;    border:1px solid rgba(255,152,0,0.3); }
.badge-MA_CROSS     { background:rgba(200,216,232,0.1); color:var(--text); border:1px solid rgba(200,216,232,0.2); }

.empty-state {
  text-align:center; padding:40px 20px;
  color:var(--muted); font-size:12px;
}
.empty-state .big { font-size:32px; margin-bottom:8px; }

/* ── HEATMAP TABLE ── */
.hmap-scroll { max-height:380px; overflow-y:auto; }
.hmap-scroll::-webkit-scrollbar { width:4px; }
.hmap-scroll::-webkit-scrollbar-thumb { background:var(--border); border-radius:2px; }

.hmap-table { width:100%; border-collapse:collapse; }
.hmap-table th {
  position:sticky; top:0;
  background:var(--surface);
  font-size:10px; color:var(--muted);
  text-transform:uppercase; letter-spacing:1px;
  padding:6px 8px; text-align:left;
  border-bottom:1px solid var(--border);
  font-family:var(--font-ui);
}
.hmap-table td {
  padding:5px 8px;
  border-bottom:1px solid rgba(26,37,53,0.5);
  font-size:12px;
}
.hmap-table tr:hover td { background:rgba(0,212,255,0.03); }
.pct-cell { font-weight:700; text-align:right; font-size:12px; }
.pct-pos { color: var(--up); }
.pct-neg { color: var(--dn); }
.pct-zero{ color: var(--muted); }

/* ── CACHE INFO ── */
.cache-grid { display:grid; grid-template-columns:1fr 1fr; gap:8px; }
.cache-item {
  display:flex; justify-content:space-between;
  padding:5px 8px;
  border-radius:4px;
  background: rgba(0,0,0,0.2);
  font-size:11px;
}
.cache-sym  { color:var(--accent); font-weight:700; }
.cache-date { color:var(--muted); }

/* ── FOOTER ── */
footer {
  text-align:center;
  padding:12px;
  color:var(--muted);
  font-size:10px;
  border-top:1px solid var(--border);
  margin-top:8px;
}

/* ── RESPONSIVE ── */
@media(max-width:768px) {
  .stats-row { grid-template-columns:1fr 1fr; }
  .main-grid { grid-template-columns:1fr; }
}

/* ── REFRESH INDICATOR ── */
.refresh-bar {
  height:2px;
  background: linear-gradient(90deg, var(--accent), var(--accent2));
  width:0%; transition:width 30s linear;
}
.refresh-bar.running { width:100%; }
</style>
</head>
<body>

<div class="refresh-bar" id="rbar"></div>

<header>
  <h1>⚡ Scanner Dashboard</h1>
  <div class="header-right">
    <div class="dot-live"></div>
    <span id="clock">--:--:--</span>
  </div>
</header>

<div class="container">

  <!-- STATS ROW -->
  <div class="stats-row">
    <div class="stat-card">
      <span class="stat-label">Tín hiệu hôm nay</span>
      <span class="stat-value green" id="stat-signals">—</span>
    </div>
    <div class="stat-card">
      <span class="stat-label">Mã trong cache</span>
      <span class="stat-value blue"  id="stat-cache">—</span>
    </div>
    <div class="stat-card">
      <span class="stat-label">BREAKOUT / PIVOT</span>
      <span class="stat-value gold"  id="stat-top">—</span>
    </div>
    <div class="stat-card">
      <span class="stat-label">Cập nhật lúc</span>
      <span class="stat-value teal" style="font-size:16px;" id="stat-time">—</span>
    </div>
  </div>

  <!-- MAIN GRID -->
  <div class="main-grid">

    <!-- SIGNAL PANEL -->
    <div class="panel">
      <div class="panel-header">
        <span class="panel-title">Tín hiệu hôm nay</span>
        <span class="panel-meta" id="sig-meta">cập nhật mỗi 30s</span>
      </div>
      <div class="panel-body">
        <div class="signal-list" id="signal-list">
          <div class="empty-state">
            <div class="big">📡</div>
            <div>Đang tải dữ liệu...</div>
          </div>
        </div>
      </div>
    </div>

    <!-- HEATMAP PANEL -->
    <div class="panel">
      <div class="panel-header">
        <span class="panel-title">Heatmap ngành</span>
        <span class="panel-meta" id="hmap-ts">cập nhật mỗi 2 phút</span>
      </div>
      <div class="panel-body">
        <div class="hmap-scroll">
          <table class="hmap-table">
            <thead>
              <tr>
                <th>Mã</th>
                <th>Giá</th>
                <th style="text-align:right">% Thay đổi</th>
              </tr>
            </thead>
            <tbody id="hmap-body">
              <tr><td colspan="3" class="empty-state">Đang tải...</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

  </div>

  <!-- CACHE INFO -->
  <div class="panel">
    <div class="panel-header">
      <span class="panel-title">Cache lịch sử (10 mã mẫu)</span>
      <span class="panel-meta" id="cache-meta">—</span>
    </div>
    <div class="panel-body">
      <div class="cache-grid" id="cache-grid">
        <div class="cache-item"><span class="cache-date">Đang tải...</span></div>
      </div>
    </div>
  </div>

</div>

<footer>Scanner Bot Dashboard • Tự động làm mới mỗi 30 giây • Dữ liệu realtime từ VPS</footer>

<script>
// ── CLOCK ────────────────────────────────────────────────────────────────
function updateClock() {
  const now = new Date();
  document.getElementById('clock').textContent =
    now.toLocaleTimeString('vi-VN', {hour12:false}) + ' ' +
    now.toLocaleDateString('vi-VN');
}
setInterval(updateClock, 1000);
updateClock();

// ── BADGE CLASS ──────────────────────────────────────────────────────────
function badgeClass(sig) {
  const m = {
    'BREAKOUT':     'badge-BREAKOUT',
    'POCKET PIVOT': 'badge-POCKET',
    'PRE-BREAK':    'badge-PREBREAK',
    'BOTTOMBREAKP': 'badge-BOTTOMBREAKP',
    'BOTTOMFISH':   'badge-BOTTOMFISH',
    'MA_CROSS':     'badge-MA_CROSS',
  };
  return m[sig] || 'badge-MA_CROSS';
}

// ── FETCH SIGNALS ────────────────────────────────────────────────────────
async function fetchSignals() {
  try {
    const r = await fetch('/api/signals');
    const j = await r.json();
    const list = document.getElementById('signal-list');
    document.getElementById('stat-signals').textContent = j.count;
    document.getElementById('stat-time').textContent    = j.updated_at;
    document.getElementById('sig-meta').textContent     = 'cập nhật ' + j.updated_at;

    const top = j.signals.filter(s =>
      s.signal === 'BREAKOUT' || s.signal === 'POCKET PIVOT'
    ).length;
    document.getElementById('stat-top').textContent = top || '—';

    if (j.signals.length === 0) {
      list.innerHTML = `<div class="empty-state">
        <div class="big">💤</div>
        <div>Chưa có tín hiệu nào hôm nay</div>
      </div>`;
      return;
    }

    list.innerHTML = j.signals.map(s => `
      <div class="signal-row">
        <span class="sig-emoji">${s.emoji}</span>
        <span class="sig-sym">${s.symbol}</span>
        <span class="sig-type">${s.signal}</span>
        <span class="sig-badge ${badgeClass(s.signal)}">${s.signal.replace('POCKET PIVOT','PIVOT').replace('PRE-BREAK','PRE')}</span>
      </div>
    `).join('');
  } catch(e) {
    console.error('fetchSignals:', e);
  }
}

// ── FETCH HEATMAP ────────────────────────────────────────────────────────
async function fetchHeatmap() {
  try {
    const r = await fetch('/api/heatmap');
    const j = await r.json();
    document.getElementById('hmap-ts').textContent = j.timestamp || '';

    const entries = Object.entries(j.data || {})
      .sort((a, b) => b[1].pct - a[1].pct)
      .slice(0, 60);

    const body = document.getElementById('hmap-body');
    if (entries.length === 0) {
      body.innerHTML = '<tr><td colspan="3" style="text-align:center;padding:20px;color:#4a6070">Chưa có dữ liệu</td></tr>';
      return;
    }

    body.innerHTML = entries.map(([sym, d]) => {
      const pct   = (d.pct || 0);
      const cls   = pct > 0.05 ? 'pct-pos' : pct < -0.05 ? 'pct-neg' : 'pct-zero';
      const arrow = pct > 0.05 ? '▲' : pct < -0.05 ? '▼' : '—';
      return `<tr>
        <td style="color:#fff;font-weight:700">${sym}</td>
        <td>${d.price ? d.price.toFixed(2) : '—'}</td>
        <td class="pct-cell ${cls}">${arrow} ${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%</td>
      </tr>`;
    }).join('');
  } catch(e) {
    console.error('fetchHeatmap:', e);
  }
}

// ── FETCH CACHE INFO ─────────────────────────────────────────────────────
async function fetchCacheInfo() {
  try {
    const r = await fetch('/api/cache_info');
    const j = await r.json();
    document.getElementById('stat-cache').textContent  = j.total_symbols;
    document.getElementById('cache-meta').textContent  = `${j.total_symbols} mã • ${j.updated_at}`;
    const grid = document.getElementById('cache-grid');
    if (!j.sample || j.sample.length === 0) {
      grid.innerHTML = '<div class="cache-item"><span class="cache-date">Chưa có dữ liệu</span></div>';
      return;
    }
    grid.innerHTML = j.sample.map(c => `
      <div class="cache-item">
        <span class="cache-sym">${c.symbol}</span>
        <span class="cache-date">${c.rows} nến • ${c.last_date}</span>
      </div>
    `).join('');
  } catch(e) {
    console.error('fetchCacheInfo:', e);
  }
}

// ── REFRESH BAR ──────────────────────────────────────────────────────────
function startRefreshBar() {
  const bar = document.getElementById('rbar');
  bar.style.transition = 'none';
  bar.style.width = '0%';
  setTimeout(() => {
    bar.style.transition = 'width 30s linear';
    bar.style.width = '100%';
  }, 50);
}

// ── MAIN LOOP ────────────────────────────────────────────────────────────
async function refresh() {
  startRefreshBar();
  await Promise.all([fetchSignals(), fetchCacheInfo()]);
}

async function refreshAll() {
  startRefreshBar();
  await Promise.all([fetchSignals(), fetchHeatmap(), fetchCacheInfo()]);
}

// Khởi động
refreshAll();
setInterval(refresh,     30_000);   // tín hiệu + cache: mỗi 30s
setInterval(fetchHeatmap, 120_000); // heatmap: mỗi 2 phút
</script>
</body>
</html>
"""
