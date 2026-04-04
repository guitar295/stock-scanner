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
        port               = 8888,
    )

Truy cập: http://YOUR_VPS_IP:8888
=============================================================================
"""

from flask import Flask, jsonify, Response
import threading
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

_heatmap_cache  = {"data": {}, "ts": "", "updated_at": 0}
_heatmap_lock   = threading.Lock()
HEATMAP_TTL_SEC = 120   # heatmap gọi API tối đa 1 lần / 2 phút
SIGNAL_TTL_SEC  = 30    # tín hiệu đọc RAM mỗi 30 giây

# =============================================================================
# API ENDPOINTS
# =============================================================================

@app.route("/api/signals")
def api_signals():
    alerted = _get_alerted_today() if _get_alerted_today else {}
    result  = []
    for sym, sig in alerted.items():
        result.append({
            "symbol": sym,
            "signal": sig,
            "emoji":  _signal_emoji.get(sig, "📌"),
            "rank":   _signal_rank.get(sig, 0),
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
    cache = _get_history_cache() if _get_history_cache else {}
    return jsonify({
        "status":        "running",
        "cache_symbols": len(cache),
        "server_time":   datetime.now(TZ_VN).strftime("%H:%M:%S %d/%m/%Y"),
    })


@app.route("/api/config")
def api_config():
    """Trả về TTL config để frontend hiển thị footer đúng."""
    return jsonify({
        "signal_ttl_sec":  SIGNAL_TTL_SEC,
        "heatmap_ttl_sec": HEATMAP_TTL_SEC,
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
    port=8888,
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
        logging.getLogger('werkzeug').setLevel(logging.ERROR)
        app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)

    threading.Thread(target=_run, daemon=True).start()
    print(f"🌐 Dashboard khởi động tại http://0.0.0.0:{port}")
    print(f"   Tín hiệu refresh : {SIGNAL_TTL_SEC}s (đọc RAM, không gọi API)")
    print(f"   Heatmap refresh  : {HEATMAP_TTL_SEC}s (gọi API, chỉ khi có người mở dashboard)")


# =============================================================================
# HTML DASHBOARD — Light Mode
# =============================================================================

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Scanner Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600;700&family=Barlow+Condensed:wght@600;700;800&display=swap" rel="stylesheet">
<style>
/* ── LIGHT THEME ─────────────────────────────────────────────────────────── */
:root {
  --bg:      #f4f6fb;
  --surface: #ffffff;
  --surf2:   #f0f3f9;
  --border:  #dde3ee;
  --accent:  #1a56db;
  --green:   #0e9f6e;
  --red:     #e02424;
  --text:    #111827;
  --muted:   #6b7280;
  --shadow:  rgba(0,0,0,0.07);
  --font-mono: 'IBM Plex Mono', monospace;
  --font-ui:   'Barlow Condensed', sans-serif;
}
*{margin:0;padding:0;box-sizing:border-box}
body{
  background:var(--bg);color:var(--text);
  font-family:var(--font-mono);font-size:13px;
  min-height:100vh;
}

/* ── REFRESH BAR ─────────────────────────────────────────────────────────── */
#rbar{
  height:2px;
  background:linear-gradient(90deg,var(--accent),var(--green));
  width:0%;position:fixed;top:0;left:0;z-index:200;
}

/* ── HEADER ──────────────────────────────────────────────────────────────── */
header{
  display:flex;align-items:center;justify-content:space-between;
  padding:11px 22px;
  background:var(--surface);border-bottom:1px solid var(--border);
  position:sticky;top:0;z-index:100;
  box-shadow:0 1px 6px var(--shadow);
}
header h1{
  font-family:var(--font-ui);font-size:19px;font-weight:800;
  letter-spacing:2.5px;color:var(--accent);text-transform:uppercase;
}
.hdr-right{display:flex;gap:18px;align-items:center}
#clock{color:var(--muted);font-size:11px}
.dot-live{
  width:8px;height:8px;border-radius:50%;
  background:var(--green);box-shadow:0 0 8px rgba(14,159,110,.5);
  animation:pulse 2s ease-in-out infinite;
}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}

/* ── LAYOUT ──────────────────────────────────────────────────────────────── */
.wrap{padding:16px 20px;display:flex;flex-direction:column;gap:16px}

/* ── PANEL ───────────────────────────────────────────────────────────────── */
.panel{
  background:var(--surface);border:1px solid var(--border);
  border-radius:8px;overflow:hidden;box-shadow:0 1px 4px var(--shadow);
}
.panel-hdr{
  display:flex;align-items:center;justify-content:space-between;
  padding:9px 16px;background:var(--surf2);border-bottom:1px solid var(--border);
}
.panel-title{
  font-family:var(--font-ui);font-size:12px;font-weight:800;
  text-transform:uppercase;letter-spacing:2px;color:var(--accent);
}
.panel-meta{font-size:10px;color:var(--muted)}
.panel-body{padding:12px 14px}

/* ── SIGNAL LIST ─────────────────────────────────────────────────────────── */
.sig-list{display:flex;flex-direction:column;gap:3px}
.sig-row{
  display:grid;grid-template-columns:28px 68px 1fr 106px;
  align-items:center;padding:7px 10px;
  border-radius:5px;border:1px solid var(--border);
  cursor:pointer;transition:all .15s;
  animation:fadeIn .3s ease;background:var(--surface);
}
@keyframes fadeIn{from{opacity:0;transform:translateX(-5px)}to{opacity:1;transform:none}}
.sig-row:hover{background:#eef3ff;border-color:rgba(26,86,219,.3);box-shadow:0 2px 8px rgba(26,86,219,.07)}
.sig-row:hover .s-sym{color:var(--accent)}
.s-emoji{font-size:14px;text-align:center}
.s-sym{font-weight:700;font-size:13px;transition:color .15s}
.s-type{font-size:11px;color:var(--muted)}
.s-badge{
  font-size:10px;font-weight:700;padding:3px 7px;
  border-radius:4px;text-align:center;letter-spacing:.4px;
  font-family:var(--font-ui);
}
.b-BREAKOUT    {background:#dcfce7;color:#15803d;border:1px solid #86efac}
.b-POCKET      {background:#fef9c3;color:#854d0e;border:1px solid #fde047}
.b-PREBREAK    {background:#f3e8ff;color:#7e22ce;border:1px solid #d8b4fe}
.b-BBREAKP     {background:#dbeafe;color:#1d4ed8;border:1px solid #93c5fd}
.b-BFISH       {background:#ffedd5;color:#c2410c;border:1px solid #fdba74}
.b-MACROSS     {background:#f1f5f9;color:#475569;border:1px solid #cbd5e1}
.empty{text-align:center;padding:36px 20px;color:var(--muted);font-size:12px}
.empty .big{font-size:30px;margin-bottom:8px}

/* ── HEATMAP ─────────────────────────────────────────────────────────────── */
.hmap-outer{overflow-x:auto;padding-bottom:4px}
.hmap-outer::-webkit-scrollbar{height:4px}
.hmap-outer::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}

/* Tất cả cột xếp ngang */
.hmap-row{
  display:inline-flex;flex-direction:row;
  gap:4px;align-items:flex-start;
  min-width:max-content;padding:2px;
}
/* Mỗi cột = 162px, khớp HMAP_CELL_W scanner */
.hmap-col{display:flex;flex-direction:column;gap:2px;width:162px;flex-shrink:0}
.hmap-group{display:flex;flex-direction:column;gap:2px;margin-bottom:3px}

/* Header nhóm ngành — khớp màu scanner HMAP_HDR_FILL=(220,228,250) */
.hmap-ghdr{
  display:flex;align-items:center;justify-content:space-between;
  padding:0 7px;height:24px;border-radius:4px;
  background:rgb(220,228,250);border:1px solid rgb(160,180,230);
  overflow:hidden;
}
.hmap-gname{
  font-family:var(--font-ui);font-size:10px;font-weight:700;
  text-transform:uppercase;letter-spacing:.6px;color:rgb(25,55,150);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.hmap-gavg{font-family:var(--font-mono);font-size:9px;font-weight:700;flex-shrink:0;margin-left:4px}
.hmap-gavg.pos{color:rgb(22,120,40)}
.hmap-gavg.neg{color:rgb(185,25,25)}
.hmap-gavg.zer{color:rgb(110,105,20)}

/* Cell — 3 phần grid căn giữa [SYM 56px][GIÁ 48px][% auto]
   khớp w1=int(162*.35)=56, w2=int(162*.30)=48, w3=58 */
.hmap-cell{
  display:grid;grid-template-columns:56px 48px 1fr;
  align-items:center;height:24px;border-radius:4px;
  cursor:pointer;border:1px solid rgba(0,0,0,.1);
  transition:filter .12s,transform .1s,box-shadow .12s;
  overflow:hidden;
}
.hmap-cell:hover{filter:brightness(.88);transform:scale(1.035);z-index:2;box-shadow:0 2px 8px rgba(0,0,0,.18)}
.hmap-cell > span{
  display:flex;align-items:center;justify-content:center;
  height:100%;overflow:hidden;white-space:nowrap;
  font-family:var(--font-mono);
}
.hc-sym  {font-size:10px;font-weight:700}
.hc-price{font-size:8.5px;opacity:.82}
.hc-pct  {font-size:9.5px;font-weight:700}

/* ── POPUP ───────────────────────────────────────────────────────────────── */
.overlay{
  display:none;position:fixed;inset:0;z-index:9999;
  background:rgba(17,24,39,.5);backdrop-filter:blur(4px);
  align-items:center;justify-content:center;
}
.overlay.on{display:flex}
.pbox{
  background:var(--surface);border:1px solid var(--border);
  border-radius:10px;box-shadow:0 20px 60px rgba(0,0,0,.15);
  width:95vw;max-width:1440px;height:90vh;
  display:flex;flex-direction:column;overflow:hidden;
  animation:popIn .2s ease;
}
@keyframes popIn{from{opacity:0;transform:scale(.96) translateY(14px)}to{opacity:1;transform:none}}

.phdr{
  display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;
  padding:9px 14px;gap:8px;
  background:var(--surf2);border-bottom:1px solid var(--border);flex-shrink:0;
}
.ptitle{
  font-family:var(--font-ui);font-size:17px;font-weight:800;
  color:var(--accent);letter-spacing:1.5px;flex-shrink:0;
}

/* Tab chọn: TradingView | 24HMoney */
.ctabs{display:flex;gap:2px;align-items:flex-end}
.ctab{
  font-size:11px;font-family:var(--font-mono);font-weight:600;
  padding:5px 13px;border-radius:5px 5px 0 0;
  border:1px solid var(--border);border-bottom:2px solid transparent;
  background:var(--bg);color:var(--muted);cursor:pointer;
  transition:all .15s;
}
.ctab.on{
  background:var(--surface);color:var(--accent);
  border-color:var(--border);border-bottom-color:var(--accent);
  font-weight:700;
}
.ctab:hover:not(.on){color:var(--accent);background:#eef3ff}

/* Timeframe buttons */
.tfg{display:flex;gap:3px;align-items:center}
.tf-lbl{font-size:10px;color:var(--muted);margin-right:2px}
.tfbtn{
  font-size:10px;font-family:var(--font-mono);font-weight:700;
  padding:4px 10px;border-radius:4px;
  border:1px solid var(--border);background:var(--bg);
  color:var(--muted);cursor:pointer;transition:all .15s;
}
.tfbtn.on{background:var(--accent);color:#fff;border-color:var(--accent)}
.tfbtn:hover:not(.on){border-color:var(--accent);color:var(--accent)}

/* Exchange tabs */
.exg{display:flex;gap:3px;align-items:center}
.ex-lbl{font-size:10px;color:var(--muted);margin-right:2px}
.exbtn{
  font-size:10px;font-family:var(--font-mono);font-weight:700;
  padding:4px 9px;border-radius:4px;
  border:1px solid var(--border);background:var(--bg);
  color:var(--muted);cursor:pointer;transition:all .15s;
}
.exbtn.on{background:#475569;color:#fff;border-color:#475569}
.exbtn:hover:not(.on){border-color:#475569;color:#475569}

.closebtn{
  width:28px;height:28px;border-radius:50%;
  border:1px solid var(--border);background:var(--bg);
  color:var(--muted);font-size:16px;cursor:pointer;
  display:flex;align-items:center;justify-content:center;
  transition:all .15s;flex-shrink:0;
}
.closebtn:hover{background:var(--red);color:#fff;border-color:var(--red)}

/* Tab content */
.pbody{flex:1;overflow:hidden;position:relative;border-top:1px solid var(--border)}
.tpanel{position:absolute;inset:0;display:none}
.tpanel.on{display:block}
.tpanel iframe{width:100%;height:100%;border:none;display:block}

/* ── FOOTER ──────────────────────────────────────────────────────────────── */
footer{
  text-align:center;padding:9px;color:var(--muted);font-size:10px;
  border-top:1px solid var(--border);background:var(--surface);
}

::-webkit-scrollbar{width:5px;height:5px}
::-webkit-scrollbar-track{background:var(--bg)}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
::-webkit-scrollbar-thumb:hover{background:var(--muted)}

@media(max-width:768px){
  .pbox{width:99vw;height:94vh;border-radius:6px}
  header h1{font-size:15px;letter-spacing:1px}
}
</style>
</head>
<body>

<div id="rbar"></div>

<header>
  <h1>⚡ Scanner Dashboard</h1>
  <div class="hdr-right">
    <div class="dot-live"></div>
    <span id="clock">--:--:--</span>
  </div>
</header>

<div class="wrap">

  <!-- TÍN HIỆU -->
  <div class="panel">
    <div class="panel-hdr">
      <span class="panel-title">Tín hiệu hôm nay</span>
      <span class="panel-meta" id="sig-meta">Đang tải...</span>
    </div>
    <div class="panel-body">
      <div class="sig-list" id="sig-list">
        <div class="empty"><div class="big">📡</div><div>Đang tải...</div></div>
      </div>
    </div>
  </div>

  <!-- HEATMAP -->
  <div class="panel">
    <div class="panel-hdr">
      <span class="panel-title">Heatmap thị trường</span>
      <span class="panel-meta" id="hmap-ts">Đang tải...</span>
    </div>
    <div class="panel-body" style="padding:8px">
      <div class="hmap-outer">
        <div class="hmap-row" id="hmap-grid">
          <div class="empty"><div class="big">🗺</div><div>Đang tải...</div></div>
        </div>
      </div>
    </div>
  </div>

</div>

<footer id="footer-txt">Scanner Bot Dashboard</footer>

<!-- ══════════════════════════════════════════════════════════
     POPUP CHART
══════════════════════════════════════════════════════════ -->
<div class="overlay" id="overlay">
  <div class="pbox">

    <div class="phdr">
      <!-- Tiêu đề mã -->
      <span class="ptitle" id="ptitle">Chart</span>

      <!-- Tab chọn nội dung -->
      <div class="ctabs">
        <button class="ctab on"  id="ctab-tv"  onclick="switchTab('tv')">📈 TradingView</button>
        <button class="ctab"     id="ctab-24h" onclick="switchTab('24h')">📰 24HMoney</button>
      </div>

      <!-- Timeframe (chỉ hiện ở tab TV) -->
      <div class="tfg" id="tfg">
        <span class="tf-lbl">Khung:</span>
        <button class="tfbtn"    id="tf-15" onclick="setTF('15')">15m</button>
        <button class="tfbtn on" id="tf-D"  onclick="setTF('D')">Daily</button>
        <button class="tfbtn"    id="tf-W"  onclick="setTF('W')">Weekly</button>
      </div>

      <!-- Exchange (chỉ hiện ở tab TV) -->
      <div class="exg" id="exg">
        <span class="ex-lbl">Sàn:</span>
        <button class="exbtn on" id="ex-HOSE"  onclick="setEx('HOSE')">HOSE</button>
        <button class="exbtn"    id="ex-HNX"   onclick="setEx('HNX')">HNX</button>
        <button class="exbtn"    id="ex-UPCOM" onclick="setEx('UPCOM')">UPCOM</button>
      </div>

      <button class="closebtn" onclick="closePopup()">✕</button>
    </div>

    <div class="pbody">
      <!-- Tab TradingView -->
      <div class="tpanel on" id="panel-tv">
        <div id="tv-container" style="width:100%;height:100%;background:#fff"></div>
      </div>
      <!-- Tab 24HMoney -->
      <div class="tpanel" id="panel-24h">
        <iframe id="iframe-24h" src="about:blank" allowfullscreen></iframe>
      </div>
    </div>

  </div>
</div>

<script>
// ═══════════════════════════════════════════════════════════════════════════
// CONFIG
// ═══════════════════════════════════════════════════════════════════════════
let SIG_TTL  = 30;
let HMAP_TTL = 120;

async function loadConfig() {
  try {
    const j = await fetch('/api/config').then(r=>r.json());
    SIG_TTL  = j.signal_ttl_sec  || 30;
    HMAP_TTL = j.heatmap_ttl_sec || 120;
  } catch(e) {}
  document.getElementById('footer-txt').textContent =
    `Scanner Bot Dashboard  •  Tín hiệu tự động làm mới sau ${SIG_TTL}s  •  Heatmap tự động làm mới sau ${HMAP_TTL}s`;
}

// ═══════════════════════════════════════════════════════════════════════════
// HEATMAP CONFIG — khớp 1:1 scanner Python
// ═══════════════════════════════════════════════════════════════════════════
const HMAP_COLS = [
  { groups:[
    { name:"VN30", syms:[
      "FPT","GAS","NVL","VNM","VCB","PLX","TCB","MWG","STB","HPG","PNJ",
      "BID","CTG","HDB","VJC","VPB","KDH","MBB","VHM","POW","VRE","MSN",
      "SSI","ACB","BVH","GVR","TPB",
    ]},
  ]},
  { groups:[
    { name:"NGAN HANG", syms:["VCB","BID","CTG","MBB","ACB","TCB","TPB","HDB","SHB","STB","VIB","VPB","MSB","ABB","BVB","LPB"] },
    { name:"DAU KHI",   syms:["GAS","PVD","PVS","BSR","OIL","PVB","PVC","PLX","PET","PVT"] },
  ]},
  { groups:[
    { name:"CHUNG KHOAN", syms:["SSI","VND","CTS","FTS","HCM","MBS","DSE","BSI","SHS","VCI","VCK","ORS"] },
    { name:"XAY DUNG",    syms:["C47","C32","L14","CII","CTD","CTI","FCN","HBC","HUT","LCG","PC1","DPG","PHC","VCG"] },
  ]},
  { groups:[
    { name:"BAT DONG SAN", syms:["IJC","LDG","CEO","D2D","DIG","DXG","HDC","HDG","KDH","NLG","NTL","NVL","PDR","SCR","TIG","KBC","SZC"] },
    { name:"PHAN BON",     syms:["BFC","DCM","DPM"] },
    { name:"THEP",         syms:["HPG","HSG","NKG"] },
  ]},
  { groups:[
    { name:"BAN LE",    syms:["MSN","FPT","FRT","MWG","PNJ","DGW"] },
    { name:"THUY SAN",  syms:["ANV","FMC","CMX","VHC","IDI"] },
    { name:"CANG BIEN", syms:["HAH","GMD","SGP","VSC"] },
    { name:"CAO SU",    syms:["GVR","DPR","DRI","PHR","DRC"] },
    { name:"NHUA",      syms:["AAA","BMP","NTP"] },
  ]},
  { groups:[
    { name:"DIEN NUOC",  syms:["NT2","PC1","GEG","GEX","POW","TDM","BWE"] },
    { name:"DET MAY",    syms:["TCM","TNG","VGT","MSH"] },
    { name:"HANG KHONG", syms:["NCT","ACV","AST","HVN","SCS","VJC"] },
    { name:"BAO HIEM",   syms:["BMI","MIG","BVH"] },
    { name:"MIA DUONG",  syms:["LSS","SBT","QNS"] },
  ]},
  { groups:[
    { name:"DAU TU CONG", syms:["FCN","HHV","LCG","VCG","C4G","CTD","HBC","HSG","NKG","HPG","KSB","PLC"] },
  ]},
];

const TS_POOL = [
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

// Màu cell — khớp _hmap_cell_color() Python
function cellStyle(pct) {
  let r,g,b;
  if      (pct >=  6.5){r=250;g=170;b=225}
  else if (pct >=  4.0){r=160;g=220;b=170}
  else if (pct >=  2.0){r=195;g=235;b=200}
  else if (pct >   0.0){r=225;g=245;b=228}
  else if (pct === 0.0){r=245;g=245;b=200}
  else if (pct >= -2.0){r=255;g=220;b=210}
  else if (pct >= -4.0){r=250;g=185;b=175}
  else if (pct >= -6.5){r=240;g=150;b=145}
  else                 {r=175;g=250;b=255}
  const lum=.299*r+.587*g+.114*b;
  return {bg:`rgb(${r},${g},${b})`, fg:lum>160?'rgb(30,30,30)':'rgb(15,15,15)'};
}

function avgPct(syms,data){
  const v=syms.filter(s=>data[s]).map(s=>data[s].pct||0);
  return v.length?v.reduce((a,b)=>a+b,0)/v.length:0;
}
function sortDesc(syms,data){
  return [...syms].sort((a,b)=>((data[b]||{}).pct||0)-((data[a]||{}).pct||0));
}
function fmtP(p){
  if(!p||p<=0)return'—';
  return p<100?p.toFixed(2):Math.round(p).toLocaleString('vi-VN');
}

function mkCell(sym,data){
  const d=data[sym]||{};
  const pct=typeof d.pct==='number'?d.pct:0;
  const price=typeof d.price==='number'?d.price:0;
  const {bg,fg}=cellStyle(pct);
  const sign=pct>=0?'+':'';
  return `<div class="hmap-cell" style="background:${bg};color:${fg};"
    onclick="openChart('${sym}')"
    title="${sym} | ${fmtP(price)} | ${sign}${pct.toFixed(2)}%">
    <span class="hc-sym">${sym}</span>
    <span class="hc-price">${fmtP(price)}</span>
    <span class="hc-pct">${sign}${pct.toFixed(1)}%</span>
  </div>`;
}

function mkGroup(name,syms,data){
  const sorted=sortDesc(syms,data);
  const avg=avgPct(syms,data);
  const sign=avg>=0?'+':'';
  const cls=avg>0.05?'pos':avg<-0.05?'neg':'zer';
  return `<div class="hmap-group">
    <div class="hmap-ghdr">
      <span class="hmap-gname">${name}</span>
      <span class="hmap-gavg ${cls}">${sign}${avg.toFixed(1)}%</span>
    </div>
    ${sorted.map(s=>mkCell(s,data)).join('')}
  </div>`;
}

function renderHeatmap(data){
  const grid=document.getElementById('hmap-grid');
  if(!data||!Object.keys(data).length){
    grid.innerHTML='<div class="empty"><div class="big">🗺</div><div>Chưa có dữ liệu</div></div>';
    return;
  }
  // Tính maxRows để biết col-0 hiển thị bao nhiêu mã
  const maxRows=Math.max(...HMAP_COLS.map(cd=>
    cd.groups.reduce((s,g)=>s+g.syms.length,0)
  ));
  // Col-0: Trading Stocks sorted desc, top maxRows
  const tsSyms=TS_POOL
    .filter(s=>data[s]!==undefined)
    .sort((a,b)=>((data[b]||{}).pct||0)-((data[a]||{}).pct||0))
    .slice(0,maxRows);
  const col0=`<div class="hmap-col">${mkGroup('TRADING STOCKS',tsSyms,data)}</div>`;
  const rest=HMAP_COLS.map(cd=>
    `<div class="hmap-col">${cd.groups.map(g=>mkGroup(g.name,g.syms,data)).join('')}</div>`
  ).join('');
  grid.innerHTML=col0+rest;
}

// ═══════════════════════════════════════════════════════════════════════════
// POPUP STATE
// ═══════════════════════════════════════════════════════════════════════════
let _sym='', _ex='HOSE', _tf='D', _tab='tv', _tvScriptLoaded=false;

// Studies: EMA10 đỏ, EMA20 xanh đậm, EMA50 tím, MA200 nâu, MACD
const TV_STUDIES=[
  {id:"MAExp@tv-basicstudies",   inputs:{length:10},  override:{"Plot.color":"#e53935","Plot.linewidth":1}},
  {id:"MAExp@tv-basicstudies",   inputs:{length:20},  override:{"Plot.color":"#2e7d32","Plot.linewidth":1}},
  {id:"MAExp@tv-basicstudies",   inputs:{length:50},  override:{"Plot.color":"#7b1fa2","Plot.linewidth":1}},
  {id:"MASimple@tv-basicstudies",inputs:{length:200}, override:{"Plot.color":"#6d4c41","Plot.linewidth":1}},
  {id:"MACD@tv-basicstudies",    inputs:{fast_length:12,slow_length:26,signal_smoothing:9}},
];

const TF_MAP={'15':'15','D':'D','W':'W'};

function buildTV(){
  const ticker=`${_ex}:${_sym}`;
  const container=document.getElementById('tv-container');
  container.innerHTML='';

  const cfg={
    autosize:true,
    symbol:ticker,
    interval:TF_MAP[_tf]||'D',
    timezone:"Asia/Ho_Chi_Minh",
    theme:"light",
    style:"1",
    locale:"vi_VN",
    enable_publishing:false,
    hide_top_toolbar:false,
    hide_legend:false,
    save_image:true,
    studies:TV_STUDIES,
    container_id:"tv-container",
    favorites:{intervals:["15","D","W"]},
  };

  function init(){ new window.TradingView.widget(cfg); }

  if(window.TradingView){
    init();
  } else if(!_tvScriptLoaded){
    _tvScriptLoaded=true;
    const sc=document.createElement('script');
    sc.src='https://s3.tradingview.com/tv.js';
    sc.async=true;
    sc.onload=init;
    document.head.appendChild(sc);
  } else {
    // Script đang tải — poll
    const t=setInterval(()=>{if(window.TradingView){clearInterval(t);init();}},200);
  }
}

function openChart(sym){
  _sym=sym.toUpperCase().trim();
  _ex='HOSE'; _tf='D'; _tab='tv';

  document.getElementById('ptitle').textContent=`📈 ${_sym}`;

  // Reset exchange
  ['HOSE','HNX','UPCOM'].forEach(e=>
    document.getElementById('ex-'+e).classList.toggle('on',e==='HOSE'));
  // Reset TF
  ['15','D','W'].forEach(t=>
    document.getElementById('tf-'+t).classList.toggle('on',t==='D'));

  _activateTab('tv');
  document.getElementById('iframe-24h').src='about:blank';

  document.getElementById('overlay').classList.add('on');
  document.body.style.overflow='hidden';
  buildTV();
}

function _activateTab(tab){
  _tab=tab;
  ['tv','24h'].forEach(t=>{
    document.getElementById('ctab-'+t).classList.toggle('on',t===tab);
    document.getElementById('panel-'+t).classList.toggle('on',t===tab);
  });
  const show=tab==='tv';
  document.getElementById('tfg').style.display=show?'flex':'none';
  document.getElementById('exg').style.display=show?'flex':'none';
}

function switchTab(tab){
  _activateTab(tab);
  if(tab==='24h'){
    const f=document.getElementById('iframe-24h');
    if(f.src==='about:blank') f.src=`https://24hmoney.vn/stock/${_sym}`;
  }
}

function setTF(tf){
  _tf=tf;
  ['15','D','W'].forEach(t=>document.getElementById('tf-'+t).classList.toggle('on',t===tf));
  if(_tab==='tv') buildTV();
}

function setEx(ex){
  _ex=ex;
  ['HOSE','HNX','UPCOM'].forEach(e=>document.getElementById('ex-'+e).classList.toggle('on',e===ex));
  if(_tab==='tv') buildTV();
}

function closePopup(){
  document.getElementById('overlay').classList.remove('on');
  document.getElementById('tv-container').innerHTML='';
  document.getElementById('iframe-24h').src='about:blank';
  document.body.style.overflow='';
}

document.getElementById('overlay').addEventListener('click',e=>{
  if(e.target===document.getElementById('overlay')) closePopup();
});
document.addEventListener('keydown',e=>{if(e.key==='Escape')closePopup();});

// ═══════════════════════════════════════════════════════════════════════════
// CLOCK
// ═══════════════════════════════════════════════════════════════════════════
function tick(){
  const n=new Date();
  document.getElementById('clock').textContent=
    n.toLocaleTimeString('vi-VN',{hour12:false})+'  '+n.toLocaleDateString('vi-VN');
}
setInterval(tick,1000);tick();

// ═══════════════════════════════════════════════════════════════════════════
// BADGE
// ═══════════════════════════════════════════════════════════════════════════
function badgeCls(sig){
  return({'BREAKOUT':'b-BREAKOUT','POCKET PIVOT':'b-POCKET','PRE-BREAK':'b-PREBREAK',
    'BOTTOMBREAKP':'b-BBREAKP','BOTTOMFISH':'b-BFISH','MA_CROSS':'b-MACROSS'})[sig]||'b-MACROSS';
}

// ═══════════════════════════════════════════════════════════════════════════
// FETCH
// ═══════════════════════════════════════════════════════════════════════════
async function fetchSigs(){
  try{
    const j=await fetch('/api/signals').then(r=>r.json());
    document.getElementById('sig-meta').textContent=
      `Cập nhật ${j.updated_at}  •  ${j.count} tín hiệu  •  click để xem chart`;
    const el=document.getElementById('sig-list');
    if(!j.signals.length){
      el.innerHTML='<div class="empty"><div class="big">💤</div><div>Chưa có tín hiệu nào hôm nay</div></div>';
      return;
    }
    el.innerHTML=j.signals.map(s=>`
      <div class="sig-row" onclick="openChart('${s.symbol}')">
        <span class="s-emoji">${s.emoji}</span>
        <span class="s-sym">${s.symbol}</span>
        <span class="s-type">${s.signal}</span>
        <span class="s-badge ${badgeCls(s.signal)}">
          ${s.signal.replace('POCKET PIVOT','PIVOT').replace('PRE-BREAK','PRE')}
        </span>
      </div>`).join('');
  }catch(e){console.error('fetchSigs:',e)}
}

async function fetchHmap(){
  try{
    const j=await fetch('/api/heatmap').then(r=>r.json());
    document.getElementById('hmap-ts').textContent=
      `${j.timestamp||''}  •  data cách ${j.cached_age||0}s  •  click để xem chart`;
    renderHeatmap(j.data||{});
  }catch(e){console.error('fetchHmap:',e)}
}

// ═══════════════════════════════════════════════════════════════════════════
// REFRESH BAR
// ═══════════════════════════════════════════════════════════════════════════
function bar(sec){
  const el=document.getElementById('rbar');
  el.style.transition='none';el.style.width='0%';
  requestAnimationFrame(()=>{
    el.style.transition=`width ${sec}s linear`;
    el.style.width='100%';
  });
}

// ═══════════════════════════════════════════════════════════════════════════
// INIT
// ═══════════════════════════════════════════════════════════════════════════
async function init(){
  await loadConfig();
  bar(HMAP_TTL);
  await Promise.all([fetchSigs(), fetchHmap()]);

  setInterval(async()=>{ bar(SIG_TTL);  await fetchSigs(); },  SIG_TTL  * 1000);
  setInterval(async()=>{ bar(HMAP_TTL); await fetchHmap(); }, HMAP_TTL * 1000);
}
init();
</script>
</body>
</html>
"""
