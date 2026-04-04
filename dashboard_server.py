"""
=============================================================================
DASHBOARD SERVER — Tích hợp vào scanner bot
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

_get_alerted_today  = None
_get_history_cache  = None
_cache_lock         = None
_fetch_heatmap_fn   = None
_signal_emoji       = {}
_signal_rank        = {}

_heatmap_cache      = {"data": {}, "ts": "", "updated_at": 0}
_heatmap_lock       = threading.Lock()
HEATMAP_TTL_SEC     = 120

# =============================================================================
# API ENDPOINTS
# =============================================================================

@app.route("/api/signals")
def api_signals():
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
    cache = _get_history_cache() if _get_history_cache else {}
    info  = []
    with _cache_lock:
        for sym, df in list(cache.items())[:10]:
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
        log.setLevel(logging.ERROR)
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
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600;700&family=Barlow+Condensed:wght@600;800&display=swap" rel="stylesheet">
<style>
:root {
  --bg:      #0d1117;
  --surface: #161b22;
  --surface2:#1c2330;
  --border:  #30363d;
  --accent:  #58a6ff;
  --accent2: #3fb950;
  --accent3: #f78166;
  --text:    #e6edf3;
  --muted:   #7d8590;
  --font-mono:'IBM Plex Mono', monospace;
  --font-ui:  'Barlow Condensed', sans-serif;
}
* { margin:0; padding:0; box-sizing:border-box; }
body {
  background:var(--bg); color:var(--text);
  font-family:var(--font-mono); font-size:13px;
  min-height:100vh; overflow-x:hidden;
}

/* SCANLINES OVERLAY */
body::before {
  content:''; position:fixed; inset:0; pointer-events:none; z-index:0;
  background: repeating-linear-gradient(
    0deg, transparent, transparent 2px,
    rgba(0,0,0,0.03) 2px, rgba(0,0,0,0.03) 4px
  );
}

/* HEADER */
header {
  display:flex; align-items:center; justify-content:space-between;
  padding:12px 24px; border-bottom:1px solid var(--border);
  background:var(--surface); position:sticky; top:0; z-index:100;
  box-shadow:0 1px 12px rgba(0,0,0,0.4);
}
header h1 {
  font-family:var(--font-ui); font-size:20px; font-weight:800;
  letter-spacing:3px; color:var(--accent); text-transform:uppercase;
}
.header-right { display:flex; gap:20px; align-items:center; }
#clock { color:var(--muted); font-size:11px; font-family:var(--font-mono); }
.dot-live {
  width:8px; height:8px; border-radius:50%;
  background:var(--accent2); box-shadow:0 0 10px rgba(63,185,80,0.6);
  animation:pulse 2s ease-in-out infinite;
}
@keyframes pulse { 0%,100%{opacity:1;box-shadow:0 0 10px rgba(63,185,80,0.6)} 50%{opacity:0.4;box-shadow:none} }

/* LAYOUT */
.container { padding:16px 20px; display:flex; flex-direction:column; gap:16px; position:relative; z-index:1; }

/* PANEL */
.panel {
  background:var(--surface); border:1px solid var(--border);
  border-radius:8px; overflow:hidden;
}
.panel-header {
  padding:10px 16px; border-bottom:1px solid var(--border);
  display:flex; align-items:center; justify-content:space-between;
  background:var(--surface2);
}
.panel-title {
  font-family:var(--font-ui); font-size:13px; font-weight:800;
  text-transform:uppercase; letter-spacing:2px; color:var(--accent);
}
.panel-meta { font-size:10px; color:var(--muted); }
.panel-body { padding:12px 16px; }

/* SIGNAL LIST */
.signal-list { display:flex; flex-direction:column; gap:3px; }
.signal-row {
  display:grid; grid-template-columns:28px 68px 1fr 100px;
  align-items:center; padding:7px 10px;
  border-radius:5px; border:1px solid var(--border);
  cursor:pointer; transition:all 0.15s;
  animation:fadeIn 0.3s ease; background:var(--surface2);
}
@keyframes fadeIn { from{opacity:0;transform:translateX(-6px)} to{opacity:1;transform:none} }
.signal-row:hover {
  background:rgba(88,166,255,0.08); border-color:rgba(88,166,255,0.35);
  box-shadow:0 0 12px rgba(88,166,255,0.08);
}
.signal-row:hover .sig-sym { color:var(--accent); }
.sig-emoji { font-size:14px; text-align:center; }
.sig-sym   { font-weight:700; color:var(--text); font-size:13px; transition:color 0.15s; }
.sig-type  { font-size:11px; color:var(--muted); }
.sig-badge {
  font-size:10px; font-weight:700; padding:3px 8px;
  border-radius:4px; text-align:center; letter-spacing:0.5px;
  font-family:var(--font-ui);
}
.badge-BREAKOUT     { background:rgba(63,185,80,0.15);  color:#3fb950; border:1px solid rgba(63,185,80,0.3); }
.badge-POCKET       { background:rgba(210,153,34,0.15); color:#e3b341; border:1px solid rgba(210,153,34,0.3); }
.badge-PREBREAK     { background:rgba(163,113,247,0.15);color:#a371f7; border:1px solid rgba(163,113,247,0.3); }
.badge-BOTTOMBREAKP { background:rgba(88,166,255,0.15); color:#58a6ff; border:1px solid rgba(88,166,255,0.3); }
.badge-BOTTOMFISH   { background:rgba(247,129,102,0.15);color:#f78166; border:1px solid rgba(247,129,102,0.3); }
.badge-MA_CROSS     { background:rgba(125,133,144,0.15);color:#7d8590; border:1px solid rgba(125,133,144,0.3); }
.empty-state { text-align:center; padding:40px 20px; color:var(--muted); font-size:12px; }
.empty-state .big { font-size:32px; margin-bottom:8px; }

/* ═══════════════════════════════════════════════════════════════════════════
   HEATMAP — layout khớp 100% scanner Python
   Mỗi cột (col 0..7) xếp ngang, mỗi group trong cột xếp dọc.
   Mỗi cell: 3 phần căn giữa = [SYM | GIÁ | %]
   ═══════════════════════════════════════════════════════════════════════════ */

.heatmap-outer {
  overflow-x:auto; padding-bottom:6px;
}
.heatmap-outer::-webkit-scrollbar { height:5px; }
.heatmap-outer::-webkit-scrollbar-thumb { background:var(--border); border-radius:3px; }

/* Row ngoài cùng chứa tất cả các cột xếp ngang */
.hmap-row {
  display:inline-flex;
  flex-direction:row;
  gap:4px;                /* HMAP_COL_GAP = 4px */
  align-items:flex-start;
  min-width:max-content;
  padding:2px;
}

/* Mỗi cột */
.hmap-col {
  display:flex;
  flex-direction:column;
  gap:2px;
  width:162px;            /* HMAP_CELL_W = 162px */
  flex-shrink:0;
}

/* Group = header + cells */
.hmap-group {
  display:flex;
  flex-direction:column;
  gap:2px;
  margin-bottom:3px;
}

/* Header nhóm ngành */
.hmap-group-hdr {
  display:flex;
  align-items:center;
  justify-content:space-between;
  padding:0 7px;
  height:24px;                /* HMAP_CELL_H ≈ 26px */
  border-radius:4px;
  background:rgb(34,42,62);
  border:1px solid rgb(50,65,110);
  overflow:hidden;
}
.hmap-hdr-name {
  font-family:var(--font-ui);
  font-size:10px; font-weight:700;
  text-transform:uppercase; letter-spacing:0.8px;
  color:rgb(140,170,240);
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
}
.hmap-hdr-pct {
  font-family:var(--font-mono);
  font-size:9px; font-weight:700;
  flex-shrink:0; margin-left:4px;
  white-space:nowrap;
}
.hmap-hdr-pct.pos { color:rgb(80,210,110); }
.hmap-hdr-pct.neg { color:rgb(240,80,80); }
.hmap-hdr-pct.zero{ color:rgb(200,180,60); }

/* Cell mã cổ phiếu
   3 phần xếp ngang BẰNG NHAU, đều căn giữa:
   [SYM 35%] [GIÁ 32%] [% 33%]
   Khớp w1/w2/w3 trong scanner:
     w1 = int(162*0.35) = 56
     w2 = int(162*0.30) = 48
     w3 = 162-56-48 = 58
*/
.hmap-cell {
  display:grid;
  grid-template-columns:56px 48px 1fr;  /* w1 w2 w3 */
  align-items:center;
  height:24px;                           /* HMAP_CELL_H-2 */
  border-radius:4px;
  cursor:pointer;
  border:1px solid rgba(0,0,0,0.12);
  transition:filter 0.12s, transform 0.1s, box-shadow 0.12s;
  overflow:hidden;
}
.hmap-cell:hover {
  filter:brightness(1.18);
  transform:scale(1.035);
  z-index:2;
  box-shadow:0 2px 10px rgba(0,0,0,0.5);
}
/* Mỗi ô con căn giữa */
.hmap-cell > span {
  display:flex;
  align-items:center;
  justify-content:center;
  height:100%;
  font-size:9.5px;
  font-weight:700;
  font-family:var(--font-mono);
  white-space:nowrap;
  overflow:hidden;
}
.hmap-sym  { font-size:10px; font-weight:700; letter-spacing:0.2px; }
.hmap-price{ font-size:9px;  font-weight:400; opacity:0.88; }
.hmap-pct  { font-size:9.5px; font-weight:700; }

/* POPUP */
.popup-overlay {
  display:none; position:fixed; inset:0; z-index:9999;
  background:rgba(0,0,0,0.75); backdrop-filter:blur(4px);
  align-items:center; justify-content:center;
}
.popup-overlay.active { display:flex; }
.popup-box {
  background:var(--surface); border:1px solid var(--border);
  border-radius:10px; box-shadow:0 16px 60px rgba(0,0,0,0.6);
  width:94vw; max-width:1400px; height:88vh;
  display:flex; flex-direction:column; overflow:hidden;
  animation:popIn 0.2s ease;
}
@keyframes popIn { from{opacity:0;transform:scale(0.96) translateY(16px)} to{opacity:1;transform:none} }
.popup-header {
  display:flex; align-items:center; justify-content:space-between;
  padding:10px 16px; border-bottom:1px solid var(--border);
  background:var(--surface2); flex-shrink:0;
}
.popup-title {
  font-family:var(--font-ui); font-size:16px; font-weight:800;
  color:var(--accent); letter-spacing:1.5px;
}
.popup-actions { display:flex; gap:10px; align-items:center; }

/* Nút chọn exchange */
.exchange-tabs { display:flex; gap:4px; }
.ex-btn {
  font-size:10px; font-family:var(--font-mono); font-weight:700;
  padding:4px 10px; border-radius:4px; border:1px solid var(--border);
  background:var(--bg); color:var(--muted); cursor:pointer;
  transition:all 0.15s; letter-spacing:0.5px;
}
.ex-btn.active { background:var(--accent); color:#0d1117; border-color:var(--accent); }
.ex-btn:hover:not(.active) { border-color:var(--accent); color:var(--accent); }

.btn-open {
  font-size:11px; font-family:var(--font-mono); font-weight:600;
  padding:5px 12px; border-radius:5px;
  background:transparent; color:var(--accent);
  border:1px solid var(--accent); cursor:pointer;
  text-decoration:none; transition:all 0.15s;
}
.btn-open:hover { background:var(--accent); color:#0d1117; }
.btn-close {
  width:28px; height:28px; border-radius:50%;
  border:1px solid var(--border); background:var(--bg);
  color:var(--muted); font-size:16px; cursor:pointer;
  display:flex; align-items:center; justify-content:center;
  transition:all 0.15s;
}
.btn-close:hover { background:var(--accent3); color:#0d1117; border-color:var(--accent3); }

/* TradingView widget container */
.popup-tv-container {
  flex:1; width:100%; position:relative; background:#131722;
  overflow:hidden;
}
.popup-tv-container iframe {
  width:100%; height:100%; border:none; display:block;
}

/* REFRESH BAR */
.refresh-bar {
  height:2px;
  background:linear-gradient(90deg, var(--accent), var(--accent2), var(--accent3));
  width:0%; position:fixed; top:0; left:0; z-index:200;
}

/* FOOTER */
footer {
  text-align:center; padding:10px; color:var(--muted); font-size:10px;
  border-top:1px solid var(--border); background:var(--surface);
  position:relative; z-index:1;
}

/* Scrollbar global */
::-webkit-scrollbar { width:5px; height:5px; }
::-webkit-scrollbar-track { background:var(--bg); }
::-webkit-scrollbar-thumb { background:var(--border); border-radius:3px; }
::-webkit-scrollbar-thumb:hover { background:var(--muted); }

@media(max-width:768px) {
  .popup-box { width:99vw; height:93vh; border-radius:6px; }
  header h1 { font-size:15px; letter-spacing:1px; }
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
    <div class="panel-body" style="padding:8px;">
      <div class="heatmap-outer">
        <div class="hmap-row" id="heatmap-grid">
          <div class="empty-state"><div class="big">🗺</div><div>Đang tải heatmap...</div></div>
        </div>
      </div>
    </div>
  </div>

</div>

<footer>Scanner Bot Dashboard • Tự động làm mới 30s/2p • TradingView chart với EMA 10/20/50/200 + MACD</footer>

<!-- POPUP CHART TRADINGVIEW -->
<div class="popup-overlay" id="popup-overlay">
  <div class="popup-box">
    <div class="popup-header">
      <span class="popup-title" id="popup-title">Chart</span>
      <div class="popup-actions">
        <div class="exchange-tabs">
          <button class="ex-btn active" id="ex-hose" onclick="setExchange('HOSE')">HOSE</button>
          <button class="ex-btn" id="ex-hnx"  onclick="setExchange('HNX')">HNX</button>
          <button class="ex-btn" id="ex-upcom" onclick="setExchange('UPCOM')">UPCOM</button>
        </div>
        <a class="btn-open" id="popup-open-link" href="#" target="_blank">↗ TradingView</a>
        <button class="btn-close" onclick="closePopup()">✕</button>
      </div>
    </div>
    <div class="popup-tv-container" id="tv-container"></div>
  </div>
</div>

<script>
// ═══════════════════════════════════════════════════════════════════════════
// CẤU TRÚC HEATMAP — khớp 1:1 với HEATMAP_COLUMNS trong scanner Python
// col 0 = Trading Stocks (tạo động từ data)
// col 1..7 = các cột ngành cố định
// ═══════════════════════════════════════════════════════════════════════════
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

// Pool Trading Stocks (col 0)
const TRADING_STOCKS_POOL = [
  "AAA","ACB","AGG","ANV","BCG","BFC","BID","BMI","BSR","BVB","BVH","BWE",
  "CCL","CII","CKG","CRE","CTD","CTG","CTI","CTR","CTS","D2D","DBC","DCM",
  "DGW","DIG","DPG","DPM","DRC","DRH","DXG","FCN","FMC","FPT","FRT","FTS",
  "GAS","GEG","GEX","GIL","GMD","GVR","HAG","HAX","HBC","HCM","HDB","HDC",
  "HDG","HNG","HPG","HSG","HTN","HVN","IDC","IJC","ITA","KBC","KDH","KSB",
  "LCG","LDG","LHG","LPB","LSS","LTG","MBB","MBS","MHC","MSB","MSN","MWG",
  "NKG","NLG","NTL","NVL","PC1","PDR","PET","PHR","PLC","PLX","PNJ","POW",
  "PTB","PVD","PVS","PVT","QNS","REE","SBT","SCR","SHB","SHS","SJS","SSI",
  "STB","SZC","TCB","TCM","TDM","TIG","TNG","TPB","TV2","VCB","VCI","VCS",
  "VGT","VHC","VHM","VIB","VIC","VJC","VNM","VPB","VRE",
];

// ═══════════════════════════════════════════════════════════════════════════
// MÀU CELL — khớp 1:1 _hmap_cell_color() trong scanner Python
// ═══════════════════════════════════════════════════════════════════════════
function cellStyle(pct) {
  let r, g, b;
  if      (pct >=  6.5) { r=250; g=170; b=225; }   // tran (trần)
  else if (pct >=  4.0) { r=160; g=220; b=170; }   // xd
  else if (pct >=  2.0) { r=195; g=235; b=200; }   // xv
  else if (pct >   0.0) { r=225; g=245; b=228; }   // xn
  else if (pct === 0.0) { r=245; g=245; b=200; }   // tc (tham chiếu)
  else if (pct >= -2.0) { r=255; g=220; b=210; }   // dn
  else if (pct >= -4.0) { r=250; g=185; b=175; }   // dv
  else if (pct >= -6.5) { r=240; g=150; b=145; }   // dd
  else                  { r=175; g=250; b=255; }   // san (sàn)

  // _hmap_fg: lum > 160 → (30,30,30) else (15,15,15)
  const lum = 0.299*r + 0.587*g + 0.114*b;
  const fg  = lum > 160 ? 'rgb(30,30,30)' : 'rgb(15,15,15)';
  return { bg:`rgb(${r},${g},${b})`, fg };
}

// Tính avg pct của một nhóm
function avgPct(symbols, data) {
  const vals = symbols.filter(s => data[s] !== undefined).map(s => data[s].pct || 0);
  return vals.length ? vals.reduce((a,b)=>a+b,0)/vals.length : 0;
}

// Sắp xếp symbols theo pct cao → thấp (khớp scanner srt())
function sortByPct(symbols, data) {
  return [...symbols].sort((a, b) => {
    const pa = (data[a]||{}).pct||0;
    const pb = (data[b]||{}).pct||0;
    return pb - pa;
  });
}

// Format giá: < 100 → 2 chữ số thập phân, >= 100 → 0 chữ số (khớp scanner)
function fmtPrice(price) {
  if (!price || price <= 0) return '—';
  return price < 100 ? price.toFixed(2) : Math.round(price).toLocaleString('vi-VN');
}

// ═══════════════════════════════════════════════════════════════════════════
// RENDER HEATMAP
// Cột 0 = Trading Stocks (sorted top N by |pct|, hiển thị theo pct desc)
// Cột 1..7 = từ HEATMAP_COLUMNS, mỗi group sort pct desc
// ═══════════════════════════════════════════════════════════════════════════
function renderHeatmap(data) {
  const grid = document.getElementById('heatmap-grid');
  if (!data || !Object.keys(data).length) {
    grid.innerHTML = '<div class="empty-state"><div class="big">🗺</div><div>Chưa có dữ liệu</div></div>';
    return;
  }

  // Tính maxRows để biết col 0 hiển thị bao nhiêu mã
  const maxGroupRows = Math.max(...HEATMAP_COLUMNS.map(cd =>
    cd.groups.reduce((s,g) => s + g.symbols.length, 0)
  ));

  // Col 0: Trading Stocks — filter pool có data, sort pct desc, lấy maxRows
  const tsSymbols = TRADING_STOCKS_POOL
    .filter(s => data[s] !== undefined)
    .sort((a,b) => (data[b]||{}).pct - (data[a]||{}).pct)
    .slice(0, maxGroupRows);

  function buildCell(sym, clk) {
    const info  = data[sym] || {};
    const pct   = typeof info.pct   === 'number' ? info.pct   : 0;
    const price = typeof info.price === 'number' ? info.price : 0;
    const { bg, fg } = cellStyle(pct);
    const psign = pct >= 0 ? '+' : '';
    const priceStr = fmtPrice(price);
    return `
      <div class="hmap-cell"
           style="background:${bg};color:${fg};"
           onclick="openChart('${sym}')"
           title="${sym}  Giá: ${priceStr}  ${psign}${pct.toFixed(2)}%">
        <span class="hmap-sym">${sym}</span>
        <span class="hmap-price">${priceStr}</span>
        <span class="hmap-pct">${psign}${pct.toFixed(1)}%</span>
      </div>`;
  }

  function buildGroup(g, data) {
    const sorted = sortByPct(g.symbols, data);
    const avg    = avgPct(g.symbols, data);
    const sign   = avg >= 0 ? '+' : '';
    const cls    = avg > 0.05 ? 'pos' : avg < -0.05 ? 'neg' : 'zero';
    const cells  = sorted.map(sym => buildCell(sym)).join('');
    return `
      <div class="hmap-group">
        <div class="hmap-group-hdr">
          <span class="hmap-hdr-name">${g.name}</span>
          <span class="hmap-hdr-pct ${cls}">${sign}${avg.toFixed(1)}%</span>
        </div>
        ${cells}
      </div>`;
  }

  // Cột 0: Trading Stocks
  const tsGroup = { name: "TRADING STOCKS", symbols: tsSymbols };
  let colsHtml = `<div class="hmap-col">${buildGroup(tsGroup, data)}</div>`;

  // Cột 1..7: từ HEATMAP_COLUMNS
  for (const colDef of HEATMAP_COLUMNS) {
    const groupsHtml = colDef.groups.map(g => buildGroup(g, data)).join('');
    colsHtml += `<div class="hmap-col">${groupsHtml}</div>`;
  }

  grid.innerHTML = colsHtml;
}

// ═══════════════════════════════════════════════════════════════════════════
// TRADINGVIEW CHART POPUP
// Tích hợp EMA 10/20/50/200 + MACD sẵn trong widget
// ═══════════════════════════════════════════════════════════════════════════
let _currentSym   = '';
let _currentExch  = 'HOSE';

function buildTVWidget(sym, exchange) {
  // Ticker TradingView: HOSE:HPG, HNX:SHB, UPCOM:OIL
  const ticker = `${exchange}:${sym}`;
  const openUrl = `https://www.tradingview.com/chart/?symbol=${ticker}`;
  document.getElementById('popup-open-link').href = openUrl;

  // Cấu hình studies: EMA 10, 20, 50, 200 + MACD
  const studies = [
    { id:"MAExp@tv-basicstudies", inputs:{ length:10  }, override:{ "Plot.color":"#ef5350", "Plot.linewidth":1 } },
    { id:"MAExp@tv-basicstudies", inputs:{ length:20  }, override:{ "Plot.color":"#26a69a", "Plot.linewidth":1 } },
    { id:"MAExp@tv-basicstudies", inputs:{ length:50  }, override:{ "Plot.color":"#ab47bc", "Plot.linewidth":1 } },
    { id:"MAExp@tv-basicstudies", inputs:{ length:200 }, override:{ "Plot.color":"#795548", "Plot.linewidth":1 } },
    { id:"MACD@tv-basicstudies",  inputs:{ fast_length:12, slow_length:26, signal_smoothing:9 } },
  ];

  const cfg = {
    autosize:     true,
    symbol:       ticker,
    interval:     "D",
    timezone:     "Asia/Ho_Chi_Minh",
    theme:        "dark",
    style:        "1",           // candlestick
    locale:       "vi_VN",
    toolbar_bg:   "#131722",
    enable_publishing: false,
    hide_top_toolbar:  false,
    hide_legend:       false,
    save_image:        false,
    studies:      studies,
    container_id: "tv_widget_inner",
  };

  const container = document.getElementById('tv-container');
  container.innerHTML = `<div id="tv_widget_inner" style="width:100%;height:100%;"></div>`;

  // Load TradingView widget script
  const script = document.createElement('script');
  script.src   = 'https://s3.tradingview.com/tv.js';
  script.async = true;
  script.onload = () => {
    if (window.TradingView) {
      new TradingView.widget(cfg);
    }
  };
  // Nếu TradingView đã load rồi thì khởi tạo ngay
  if (window.TradingView) {
    new TradingView.widget(cfg);
  } else {
    document.head.appendChild(script);
  }
}

function openChart(sym) {
  _currentSym  = sym.toUpperCase().trim();
  _currentExch = 'HOSE';
  // Reset exchange tabs
  ['hose','hnx','upcom'].forEach(e => document.getElementById('ex-'+e).classList.remove('active'));
  document.getElementById('ex-hose').classList.add('active');

  document.getElementById('popup-title').textContent = `📈 ${_currentSym}`;
  document.getElementById('popup-overlay').classList.add('active');
  document.body.style.overflow = 'hidden';

  buildTVWidget(_currentSym, _currentExch);
}

function setExchange(ex) {
  _currentExch = ex;
  ['hose','hnx','upcom'].forEach(e => document.getElementById('ex-'+e).classList.remove('active'));
  document.getElementById('ex-'+ex.toLowerCase()).classList.add('active');
  buildTVWidget(_currentSym, _currentExch);
}

function closePopup() {
  document.getElementById('popup-overlay').classList.remove('active');
  document.getElementById('tv-container').innerHTML = '';
  document.body.style.overflow = '';
}

document.getElementById('popup-overlay').addEventListener('click', e => {
  if (e.target === document.getElementById('popup-overlay')) closePopup();
});
document.addEventListener('keydown', e => { if (e.key==='Escape') closePopup(); });

// ═══════════════════════════════════════════════════════════════════════════
// ĐỒNG HỒ
// ═══════════════════════════════════════════════════════════════════════════
function updateClock() {
  const now = new Date();
  document.getElementById('clock').textContent =
    now.toLocaleTimeString('vi-VN',{hour12:false}) + '  ' +
    now.toLocaleDateString('vi-VN');
}
setInterval(updateClock, 1000); updateClock();

// ═══════════════════════════════════════════════════════════════════════════
// BADGE
// ═══════════════════════════════════════════════════════════════════════════
function badgeClass(sig) {
  return ({
    'BREAKOUT'    :'badge-BREAKOUT',
    'POCKET PIVOT':'badge-POCKET',
    'PRE-BREAK'   :'badge-PREBREAK',
    'BOTTOMBREAKP':'badge-BOTTOMBREAKP',
    'BOTTOMFISH'  :'badge-BOTTOMFISH',
    'MA_CROSS'    :'badge-MA_CROSS',
  })[sig]||'badge-MA_CROSS';
}

// ═══════════════════════════════════════════════════════════════════════════
// FETCH SIGNALS
// ═══════════════════════════════════════════════════════════════════════════
async function fetchSignals() {
  try {
    const r = await fetch('/api/signals');
    const j = await r.json();
    document.getElementById('sig-meta').textContent =
      `Cập nhật: ${j.updated_at}  •  ${j.count} tín hiệu  •  Click để xem chart`;
    const list = document.getElementById('signal-list');
    if (!j.signals.length) {
      list.innerHTML = `<div class="empty-state"><div class="big">💤</div><div>Chưa có tín hiệu nào hôm nay</div></div>`;
      return;
    }
    list.innerHTML = j.signals.map(s => `
      <div class="signal-row" onclick="openChart('${s.symbol}')">
        <span class="sig-emoji">${s.emoji}</span>
        <span class="sig-sym">${s.symbol}</span>
        <span class="sig-type">${s.signal}</span>
        <span class="sig-badge ${badgeClass(s.signal)}">
          ${s.signal.replace('POCKET PIVOT','PIVOT').replace('PRE-BREAK','PRE')}
        </span>
      </div>`).join('');
  } catch(e) { console.error('fetchSignals:', e); }
}

// ═══════════════════════════════════════════════════════════════════════════
// FETCH HEATMAP
// ═══════════════════════════════════════════════════════════════════════════
async function fetchHeatmap() {
  try {
    const r = await fetch('/api/heatmap');
    const j = await r.json();
    const age = j.cached_age || 0;
    document.getElementById('hmap-ts').textContent =
      `${j.timestamp||''}  •  làm mới ${age}s trước  •  click để xem chart`;
    renderHeatmap(j.data || {});
  } catch(e) { console.error('fetchHeatmap:', e); }
}

// ═══════════════════════════════════════════════════════════════════════════
// REFRESH BAR
// ═══════════════════════════════════════════════════════════════════════════
function startRefreshBar(durationSec) {
  const bar = document.getElementById('rbar');
  bar.style.transition = 'none'; bar.style.width = '0%';
  requestAnimationFrame(() => {
    bar.style.transition = `width ${durationSec}s linear`;
    bar.style.width = '100%';
  });
}

// ═══════════════════════════════════════════════════════════════════════════
// KHỞI ĐỘNG
// ═══════════════════════════════════════════════════════════════════════════
async function init() {
  startRefreshBar(120);
  await Promise.all([fetchSignals(), fetchHeatmap()]);
}
init();
setInterval(async () => { startRefreshBar(30); await fetchSignals(); },    30_000);
setInterval(async () => { startRefreshBar(120); await fetchHeatmap(); }, 120_000);
</script>
</body>
</html>
"""
