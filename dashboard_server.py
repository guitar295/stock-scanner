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
  --bg:      #f0f4f8;
  --surface: #ffffff;
  --border:  #dde3ec;
  --accent:  #1565c0;
  --accent2: #00897b;
  --text:    #1a2535;
  --muted:   #7a8fa6;
  --font-mono:'JetBrains Mono', monospace;
  --font-ui:  'Syne', sans-serif;
}
* { margin:0; padding:0; box-sizing:border-box; }
body {
  background:var(--bg); color:var(--text);
  font-family:var(--font-mono); font-size:13px;
  min-height:100vh; overflow-x:hidden;
}

/* HEADER */
header {
  display:flex; align-items:center; justify-content:space-between;
  padding:14px 24px; border-bottom:1px solid var(--border);
  background:var(--surface); position:sticky; top:0; z-index:100;
  box-shadow:0 1px 4px rgba(0,0,0,0.06);
}
header h1 {
  font-family:var(--font-ui); font-size:18px; font-weight:800;
  letter-spacing:2px; color:var(--accent); text-transform:uppercase;
}
.header-right { display:flex; gap:16px; align-items:center; }
#clock { color:var(--muted); font-size:12px; }
.dot-live {
  width:8px; height:8px; border-radius:50%;
  background:var(--accent2); box-shadow:0 0 8px rgba(0,137,123,0.4);
  animation:pulse 2s ease-in-out infinite;
}
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }

/* LAYOUT */
.container { padding:20px 24px; display:flex; flex-direction:column; gap:20px; }

/* PANEL */
.panel {
  background:var(--surface); border:1px solid var(--border);
  border-radius:8px; overflow:hidden;
  box-shadow:0 1px 3px rgba(0,0,0,0.04);
}
.panel-header {
  padding:10px 16px; border-bottom:1px solid var(--border);
  display:flex; align-items:center; justify-content:space-between;
  background:#f7f9fc;
}
.panel-title {
  font-family:var(--font-ui); font-size:12px; font-weight:700;
  text-transform:uppercase; letter-spacing:1.5px; color:var(--accent);
}
.panel-meta { font-size:10px; color:var(--muted); }
.panel-body { padding:12px 16px; }

/* SIGNAL LIST */
.signal-list { display:flex; flex-direction:column; gap:4px; }
.signal-row {
  display:grid; grid-template-columns:32px 72px 1fr 90px;
  align-items:center; padding:8px 10px;
  border-radius:5px; border:1px solid var(--border);
  cursor:pointer; transition:all 0.15s;
  animation:fadeIn 0.3s ease;
}
@keyframes fadeIn { from{opacity:0;transform:translateY(4px)} to{opacity:1;transform:none} }
.signal-row:hover {
  background:#eef4ff; border-color:rgba(21,101,192,0.3);
  box-shadow:0 2px 8px rgba(21,101,192,0.08);
}
.signal-row:hover .sig-sym { color:var(--accent); }
.sig-emoji { font-size:14px; text-align:center; }
.sig-sym   { font-weight:700; color:var(--text); font-size:13px; transition:color 0.15s; }
.sig-type  { font-size:11px; color:var(--muted); }
.sig-badge {
  font-size:10px; font-weight:700; padding:3px 8px;
  border-radius:4px; text-align:center; letter-spacing:0.5px;
}
.badge-BREAKOUT     { background:#e8f5e9; color:#1b7f4f; border:1px solid #a5d6a7; }
.badge-POCKET       { background:#fff8e1; color:#e65100; border:1px solid #ffcc80; }
.badge-PREBREAK     { background:#ede7f6; color:#5e35b1; border:1px solid #ce93d8; }
.badge-BOTTOMBREAKP { background:#e3f2fd; color:#1565c0; border:1px solid #90caf9; }
.badge-BOTTOMFISH   { background:#fff3e0; color:#e65100; border:1px solid #ffb74d; }
.badge-MA_CROSS     { background:#f5f5f5; color:#546e7a; border:1px solid #cfd8dc; }
.empty-state { text-align:center; padding:40px 20px; color:var(--muted); font-size:12px; }
.empty-state .big { font-size:32px; margin-bottom:8px; }

/* HEATMAP */
.heatmap-outer {
  overflow-x:auto; padding-bottom:6px;
}
.heatmap-outer::-webkit-scrollbar { height:5px; }
.heatmap-outer::-webkit-scrollbar-thumb { background:var(--border); border-radius:3px; }

/* Grid: mỗi cột là 1 flex-col, các cột xếp ngang */
.heatmap-grid {
  display:inline-flex;
  flex-direction:row;
  gap:5px;
  align-items:flex-start;
  min-width:max-content;
}

.hmap-col {
  display:flex; flex-direction:column;
  gap:5px; width:158px; flex-shrink:0;
}

.hmap-group { display:flex; flex-direction:column; gap:2px; }

/* Header nhóm ngành — khớp màu scanner: HMAP_HDR_FILL=(220,228,250) */
.hmap-group-hdr {
  display:flex; align-items:center; justify-content:space-between;
  padding:3px 7px; border-radius:4px;
  background:rgb(220,228,250); border:1px solid rgb(160,180,230);
  font-family:var(--font-ui); font-size:9px; font-weight:700;
  text-transform:uppercase; letter-spacing:0.5px;
  color:rgb(25,55,150);
  white-space:nowrap; overflow:hidden;
}
.hmap-hdr-pct { font-size:9px; font-weight:700; flex-shrink:0; margin-left:4px; }
.hmap-hdr-pct.pos { color:rgb(30,140,40); }
.hmap-hdr-pct.neg { color:rgb(190,30,30); }
.hmap-hdr-pct.zero{ color:rgb(120,120,30); }

/* Cell mã — height khớp scanner HMAP_CELL_H=26 */
.hmap-cell {
  display:flex; align-items:center; justify-content:space-between;
  padding:0 7px; height:24px; border-radius:4px;
  cursor:pointer; border:1px solid rgba(0,0,0,0.07);
  transition:filter 0.12s, transform 0.1s;
  overflow:hidden;
}
.hmap-cell:hover { filter:brightness(0.88); transform:scale(1.025); z-index:1; }
.hmap-cell-left  { display:flex; flex-direction:column; }
.hmap-cell-sym   { font-size:10px; font-weight:700; line-height:1.1; }
.hmap-cell-price { font-size:8px; opacity:0.75; line-height:1; }
.hmap-cell-pct   { font-size:10px; font-weight:700; }

/* REFRESH BAR */
.refresh-bar {
  height:2px; background:linear-gradient(90deg,var(--accent),var(--accent2));
  width:0%; transition:width 30s linear;
}

/* POPUP */
.popup-overlay {
  display:none; position:fixed; inset:0; z-index:9999;
  background:rgba(10,20,40,0.5); backdrop-filter:blur(3px);
  align-items:center; justify-content:center;
}
.popup-overlay.active { display:flex; }
.popup-box {
  background:var(--surface); border:1px solid var(--border);
  border-radius:12px; box-shadow:0 8px 40px rgba(0,0,0,0.15);
  width:92vw; max-width:1300px; height:88vh;
  display:flex; flex-direction:column; overflow:hidden;
  animation:popIn 0.2s ease;
}
@keyframes popIn { from{opacity:0;transform:scale(0.96) translateY(12px)} to{opacity:1;transform:none} }
.popup-header {
  display:flex; align-items:center; justify-content:space-between;
  padding:12px 18px; border-bottom:1px solid var(--border);
  background:#f7f9fc; flex-shrink:0;
}
.popup-title {
  font-family:var(--font-ui); font-size:15px; font-weight:800;
  color:var(--accent); letter-spacing:1px;
}
.popup-actions { display:flex; gap:10px; align-items:center; }
.btn-open {
  font-size:11px; font-family:var(--font-mono);
  padding:5px 12px; border-radius:5px;
  background:var(--accent); color:#fff; border:none;
  cursor:pointer; text-decoration:none; transition:background 0.15s;
}
.btn-open:hover { background:#0d47a1; }
.btn-close {
  width:28px; height:28px; border-radius:50%;
  border:1px solid var(--border); background:#f0f4f8;
  color:var(--text); font-size:16px; cursor:pointer;
  display:flex; align-items:center; justify-content:center;
  transition:background 0.15s;
}
.btn-close:hover { background:#dde3ec; }
.popup-iframe { flex:1; border:none; width:100%; background:#fff; }

/* FOOTER */
footer {
  text-align:center; padding:12px; color:var(--muted); font-size:10px;
  border-top:1px solid var(--border); margin-top:8px; background:var(--surface);
}

@media(max-width:768px) {
  .popup-box { width:99vw; height:93vh; border-radius:8px; }
}
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

  <!-- TÍN HIỆU HÔM NAY -->
  <div class="panel">
    <div class="panel-header">
      <span class="panel-title">Tín hiệu hôm nay</span>
      <span class="panel-meta" id="sig-meta">Click mã để xem chart</span>
    </div>
    <div class="panel-body">
      <div class="signal-list" id="signal-list">
        <div class="empty-state"><div class="big">📡</div><div>Đang tải...</div></div>
      </div>
    </div>
  </div>

  <!-- HEATMAP -->
  <div class="panel">
    <div class="panel-header">
      <span class="panel-title">Heatmap thị trường</span>
      <span class="panel-meta" id="hmap-ts">Click mã để xem chart</span>
    </div>
    <div class="panel-body" style="padding:10px 8px;">
      <div class="heatmap-outer">
        <div class="heatmap-grid" id="heatmap-grid">
          <div class="empty-state"><div class="big">🗺</div><div>Đang tải...</div></div>
        </div>
      </div>
    </div>
  </div>

</div>

<footer>Scanner Bot Dashboard • Tự động làm mới mỗi 30 giây • Click vào mã để xem chart</footer>

<!-- POPUP CHART -->
<div class="popup-overlay" id="popup-overlay">
  <div class="popup-box">
    <div class="popup-header">
      <span class="popup-title" id="popup-title">Chart</span>
      <div class="popup-actions">
        <a class="btn-open" id="popup-open-link" href="#" target="_blank">↗ Mở tab mới</a>
        <button class="btn-close" onclick="closePopup()">✕</button>
      </div>
    </div>
    <iframe class="popup-iframe" id="popup-iframe" src="about:blank" allowfullscreen></iframe>
  </div>
</div>

<script>
// ── CẤU TRÚC HEATMAP — khớp 1:1 với HEATMAP_COLUMNS trong scanner ────────
const HEATMAP_COLUMNS = [
  { col:1, groups:[
    { name:"VN30", symbols:[
      "FPT","GAS","NVL","VNM","VCB","PLX","TCB","MWG","STB","HPG","PNJ",
      "BID","CTG","HDB","VJC","VPB","KDH","MBB","VHM","POW","VRE","MSN",
      "SSI","ACB","BVH","GVR","TPB",
    ]},
  ]},
  { col:2, groups:[
    { name:"NGAN HANG", symbols:["VCB","BID","CTG","MBB","ACB","TCB","TPB","HDB","SHB","STB","VIB","VPB","MSB","ABB","BVB","LPB"] },
    { name:"DAU KHI",   symbols:["GAS","PVD","PVS","BSR","OIL","PVB","PVC","PLX","PET","PVT"] },
  ]},
  { col:3, groups:[
    { name:"CHUNG KHOAN", symbols:["SSI","VND","CTS","FTS","HCM","MBS","DSE","BSI","SHS","VCI","VCK","ORS"] },
    { name:"XAY DUNG",    symbols:["C47","C32","L14","CII","CTD","CTI","FCN","HBC","HUT","LCG","PC1","DPG","PHC","VCG"] },
  ]},
  { col:4, groups:[
    { name:"BAT DONG SAN", symbols:["IJC","LDG","CEO","D2D","DIG","DXG","HDC","HDG","KDH","NLG","NTL","NVL","PDR","SCR","TIG","KBC","SZC"] },
    { name:"PHAN BON",     symbols:["BFC","DCM","DPM"] },
    { name:"THEP",         symbols:["HPG","HSG","NKG"] },
  ]},
  { col:5, groups:[
    { name:"BAN LE",    symbols:["MSN","FPT","FRT","MWG","PNJ","DGW"] },
    { name:"THUY SAN",  symbols:["ANV","FMC","CMX","VHC","IDI"] },
    { name:"CANG BIEN", symbols:["HAH","GMD","SGP","VSC"] },
    { name:"CAO SU",    symbols:["GVR","DPR","DRI","PHR","DRC"] },
    { name:"NHUA",      symbols:["AAA","BMP","NTP"] },
  ]},
  { col:6, groups:[
    { name:"DIEN NUOC",  symbols:["NT2","PC1","GEG","GEX","POW","TDM","BWE"] },
    { name:"DET MAY",    symbols:["TCM","TNG","VGT","MSH"] },
    { name:"HANG KHONG", symbols:["NCT","ACV","AST","HVN","SCS","VJC"] },
    { name:"BAO HIEM",   symbols:["BMI","MIG","BVH"] },
    { name:"MIA DUONG",  symbols:["LSS","SBT","QNS"] },
  ]},
  { col:7, groups:[
    { name:"DAU TU CONG", symbols:["FCN","HHV","LCG","VCG","C4G","CTD","HBC","HSG","NKG","HPG","KSB","PLC"] },
  ]},
];

// ── MÀU CELL — khớp 1:1 HMAP_COLORS trong scanner ────────────────────────
// HMAP_COLORS dùng RGB tuple → chuyển sang CSS rgb()
// _hmap_cell_color logic:
//   pct >= 6.5  → tran  (250,170,225)
//   pct >= 4.0  → xd    (160,220,170)
//   pct >= 2.0  → xv    (195,235,200)
//   pct >  0.0  → xn    (225,245,228)
//   pct == 0.0  → tc    (245,245,200)
//   pct >= -2.0 → dn    (255,220,210)
//   pct >= -4.0 → dv    (250,185,175)
//   pct >= -6.5 → dd    (240,150,145)
//   else        → san   (175,250,255)
//
// _hmap_fg: lum = 0.299*R + 0.587*G + 0.114*B
//   lum > 160 → fg (30,30,30) else (15,15,15)

function cellColor(pct) {
  let r, g, b;
  if      (pct >=  6.5) { r=250; g=170; b=225; }
  else if (pct >=  4.0) { r=160; g=220; b=170; }
  else if (pct >=  2.0) { r=195; g=235; b=200; }
  else if (pct >   0.0) { r=225; g=245; b=228; }
  else if (pct ===  0.0){ r=245; g=245; b=200; }
  else if (pct >= -2.0) { r=255; g=220; b=210; }
  else if (pct >= -4.0) { r=250; g=185; b=175; }
  else if (pct >= -6.5) { r=240; g=150; b=145; }
  else                  { r=175; g=250; b=255; }
  const lum = 0.299*r + 0.587*g + 0.114*b;
  const fg  = lum > 160 ? 'rgb(30,30,30)' : 'rgb(15,15,15)';
  return { bg:`rgb(${r},${g},${b})`, fg };
}

function avgPct(symbols, data) {
  const vals = symbols.map(s => (data[s]||{}).pct || 0);
  return vals.length ? vals.reduce((a,b)=>a+b,0)/vals.length : 0;
}

// ── RENDER HEATMAP ────────────────────────────────────────────────────────
function renderHeatmap(data) {
  const grid = document.getElementById('heatmap-grid');
  if (!data || !Object.keys(data).length) {
    grid.innerHTML = '<div class="empty-state"><div class="big">🗺</div><div>Chưa có dữ liệu</div></div>';
    return;
  }

  grid.innerHTML = HEATMAP_COLUMNS.map(colDef => {
    const groupsHtml = colDef.groups.map(g => {
      const avg  = avgPct(g.symbols, data);
      const sign = avg >= 0 ? '+' : '';
      const cls  = avg > 0.05 ? 'pos' : avg < -0.05 ? 'neg' : 'zero';

      const cellsHtml = g.symbols.map(sym => {
        const info  = data[sym] || {};
        const pct   = typeof info.pct === 'number' ? info.pct : 0;
        const price = typeof info.price === 'number' ? info.price.toFixed(2) : '—';
        const { bg, fg } = cellColor(pct);
        const psign = pct >= 0 ? '+' : '';
        return `
          <div class="hmap-cell"
               style="background:${bg}; color:${fg};"
               onclick="openChart('${sym}')"
               title="${sym} | Giá: ${price} | ${psign}${pct.toFixed(2)}%">
            <div class="hmap-cell-left">
              <span class="hmap-cell-sym">${sym}</span>
              <span class="hmap-cell-price">${price}</span>
            </div>
            <span class="hmap-cell-pct">${psign}${pct.toFixed(1)}%</span>
          </div>`;
      }).join('');

      return `
        <div class="hmap-group">
          <div class="hmap-group-hdr">
            <span style="overflow:hidden;text-overflow:ellipsis;">${g.name}</span>
            <span class="hmap-hdr-pct ${cls}">${sign}${avg.toFixed(1)}%</span>
          </div>
          ${cellsHtml}
        </div>`;
    }).join('');

    return `<div class="hmap-col">${groupsHtml}</div>`;
  }).join('');
}

// ── POPUP ─────────────────────────────────────────────────────────────────
function openChart(sym) {
  const url = `https://ta.vietstock.vn/?stockcode=${sym.toLowerCase()}`;
  document.getElementById('popup-title').textContent = `📈 ${sym}`;
  document.getElementById('popup-open-link').href    = url;
  document.getElementById('popup-iframe').src        = url;
  document.getElementById('popup-overlay').classList.add('active');
  document.body.style.overflow = 'hidden';
}
function closePopup() {
  document.getElementById('popup-overlay').classList.remove('active');
  document.getElementById('popup-iframe').src = 'about:blank';
  document.body.style.overflow = '';
}
document.getElementById('popup-overlay').addEventListener('click', e => {
  if (e.target === document.getElementById('popup-overlay')) closePopup();
});
document.addEventListener('keydown', e => { if (e.key==='Escape') closePopup(); });

// ── CLOCK ─────────────────────────────────────────────────────────────────
function updateClock() {
  const now = new Date();
  document.getElementById('clock').textContent =
    now.toLocaleTimeString('vi-VN',{hour12:false})+' '+now.toLocaleDateString('vi-VN');
}
setInterval(updateClock,1000); updateClock();

// ── BADGE ─────────────────────────────────────────────────────────────────
function badgeClass(sig) {
  return ({
    'BREAKOUT':'badge-BREAKOUT','POCKET PIVOT':'badge-POCKET',
    'PRE-BREAK':'badge-PREBREAK','BOTTOMBREAKP':'badge-BOTTOMBREAKP',
    'BOTTOMFISH':'badge-BOTTOMFISH','MA_CROSS':'badge-MA_CROSS',
  })[sig]||'badge-MA_CROSS';
}

// ── FETCH SIGNALS ─────────────────────────────────────────────────────────
async function fetchSignals() {
  try {
    const r = await fetch('/api/signals');
    const j = await r.json();
    document.getElementById('sig-meta').textContent = `${j.updated_at} • click để xem chart`;
    const list = document.getElementById('signal-list');
    if (!j.signals.length) {
      list.innerHTML = `<div class="empty-state"><div class="big">💤</div><div>Chưa có tín hiệu nào hôm nay</div></div>`;
      return;
    }
    list.innerHTML = j.signals.map(s => `
      <div class="signal-row" onclick="openChart('${s.symbol}')" title="Xem chart ${s.symbol}">
        <span class="sig-emoji">${s.emoji}</span>
        <span class="sig-sym">${s.symbol}</span>
        <span class="sig-type">${s.signal}</span>
        <span class="sig-badge ${badgeClass(s.signal)}">
          ${s.signal.replace('POCKET PIVOT','PIVOT').replace('PRE-BREAK','PRE')}
        </span>
      </div>`).join('');
  } catch(e) { console.error(e); }
}

// ── FETCH HEATMAP ─────────────────────────────────────────────────────────
async function fetchHeatmap() {
  try {
    const r = await fetch('/api/heatmap');
    const j = await r.json();
    document.getElementById('hmap-ts').textContent =
      (j.timestamp||'')+' • click để xem chart';
    renderHeatmap(j.data||{});
  } catch(e) { console.error(e); }
}

// ── REFRESH BAR ───────────────────────────────────────────────────────────
function startRefreshBar() {
  const bar = document.getElementById('rbar');
  bar.style.transition='none'; bar.style.width='0%';
  setTimeout(()=>{ bar.style.transition='width 30s linear'; bar.style.width='100%'; },50);
}
async function refresh() {
  startRefreshBar();
  await fetchSignals();
}
async function refreshAll() {
  startRefreshBar();
  await Promise.all([fetchSignals(), fetchHeatmap()]);
}
refreshAll();
setInterval(refresh,      30_000);
setInterval(fetchHeatmap, 120_000);
</script>
</body>
</html>
"""
