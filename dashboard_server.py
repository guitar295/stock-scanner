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
_fetch_chart_fn     = None
_signal_emoji       = {}
_signal_rank        = {}

_heatmap_cache  = {"data": {}, "ts": "", "updated_at": 0}
_heatmap_lock   = threading.Lock()
HEATMAP_TTL_SEC = 120
SIGNAL_TTL_SEC  = 30

_chart_cache: dict = {}
_chart_lock         = threading.Lock()
CHART_TTL_SEC       = 0   # Không cache — chart luôn vẽ mới mỗi lần click tab

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


@app.route("/api/chart_images/<symbol>")
def api_chart_images(symbol):
    import base64
    symbol = symbol.upper().strip()
    now    = time.time()

    with _chart_lock:
        cached = _chart_cache.get(symbol)
        if cached and (now - cached["updated_at"]) < CHART_TTL_SEC:
            return jsonify({
                "symbol": symbol,
                "images": cached["images"],
                "labels": cached["labels"],
                "cached": True,
            })

    if not _fetch_chart_fn:
        return jsonify({"error": "chart_fn_not_registered"}), 503

    try:
        ts = datetime.now(TZ_VN).strftime('%H:%M:%S')
        print(f"  [Dashboard] 📊 Tạo chart {symbol}...")
        png_list, labels = _fetch_chart_fn(symbol)
        if not png_list:
            return jsonify({"error": "no_data"}), 404

        b64_list = [base64.b64encode(b).decode() for b in png_list]

        with _chart_lock:
            _chart_cache[symbol] = {
                "images":     b64_list,
                "labels":     labels,
                "updated_at": time.time(),
            }

        ts2 = datetime.now(TZ_VN).strftime('%H:%M:%S')
        print(f"  [Dashboard] ✅ Chart {symbol}: {len(b64_list)} ảnh OK ({ts}→{ts2})")
        return jsonify({
            "symbol": symbol,
            "images": b64_list,
            "labels": labels,
            "cached": False,
        })

    except Exception as e:
        print(f"  [Dashboard] ❌ Chart {symbol} lỗi: {e}")
        return jsonify({"error": str(e)}), 500


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


@app.route("/api/chart_cache_clear/<symbol>", methods=["DELETE"])
def api_chart_cache_clear(symbol):
    symbol = symbol.upper().strip()
    with _chart_lock:
        removed = symbol in _chart_cache
        _chart_cache.pop(symbol, None)
    return jsonify({"symbol": symbol, "cleared": removed})


@app.route("/api/config")
def api_config():
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
    fetch_chart_fn=None,
    port=8888,
):
    global _get_alerted_today, _get_history_cache, _cache_lock
    global _fetch_heatmap_fn, _fetch_chart_fn, _signal_emoji, _signal_rank

    _get_alerted_today = alerted_today_ref
    _get_history_cache = history_cache_ref
    _cache_lock        = cache_lock_ref
    _fetch_heatmap_fn  = fetch_heatmap_fn
    _fetch_chart_fn    = fetch_chart_fn
    _signal_emoji      = signal_emoji_ref
    _signal_rank       = signal_rank_ref

    def _run():
        import logging
        logging.getLogger('werkzeug').setLevel(logging.ERROR)
        app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)

    threading.Thread(target=_run, daemon=True).start()
    print(f"🌐 Dashboard khởi động tại http://0.0.0.0:{port}")
    print(f"   Tín hiệu refresh : {SIGNAL_TTL_SEC}s (đọc RAM, không gọi API)")
    print(f"   Heatmap refresh  : {HEATMAP_TTL_SEC}s (gọi API, chỉ khi có người mở)")
    print(f"   Scanner Chart    : {'✅ đã đăng ký' if fetch_chart_fn else '❌ chưa đăng ký'} (cache {CHART_TTL_SEC}s)")


# =============================================================================
# HTML DASHBOARD
# =============================================================================

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Scanner Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600;700&family=Barlow+Condensed:wght@600;700;800&display=swap" rel="stylesheet">
<style>
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
body{background:var(--bg);color:var(--text);font-family:var(--font-mono);font-size:13px;min-height:100vh}

#rbar{height:2px;background:linear-gradient(90deg,var(--accent),var(--green));width:0%;position:fixed;top:0;left:0;z-index:200}

header{display:flex;align-items:center;justify-content:space-between;padding:11px 22px;background:var(--surface);border-bottom:1px solid var(--border);position:sticky;top:0;z-index:100;box-shadow:0 1px 6px var(--shadow)}
header h1{font-family:var(--font-ui);font-size:19px;font-weight:800;letter-spacing:2.5px;color:var(--accent);text-transform:uppercase}
.hdr-right{display:flex;gap:18px;align-items:center}
#clock{color:var(--muted);font-size:11px}
.dot-live{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 8px rgba(14,159,110,.5);animation:pulse 2s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}

.wrap{padding:16px 20px;display:flex;flex-direction:column;gap:16px}

.panel{background:var(--surface);border:1px solid var(--border);border-radius:8px;overflow:hidden;box-shadow:0 1px 4px var(--shadow)}
.panel-hdr{display:flex;align-items:center;justify-content:space-between;padding:9px 16px;background:var(--surf2);border-bottom:1px solid var(--border)}
.panel-title{font-family:var(--font-ui);font-size:12px;font-weight:800;text-transform:uppercase;letter-spacing:2px;color:var(--accent)}
.panel-meta{font-size:10px;color:var(--muted)}
.panel-body{padding:12px 14px}

.sig-list{display:flex;flex-direction:column;gap:3px}
.sig-row{display:grid;grid-template-columns:28px 68px 1fr 106px;align-items:center;padding:7px 10px;border-radius:5px;border:1px solid var(--border);cursor:pointer;transition:all .15s;animation:fadeIn .3s ease;background:var(--surface)}
@keyframes fadeIn{from{opacity:0;transform:translateX(-5px)}to{opacity:1;transform:none}}
.sig-row:hover{background:#eef3ff;border-color:rgba(26,86,219,.3);box-shadow:0 2px 8px rgba(26,86,219,.07)}
.sig-row:hover .s-sym{color:var(--accent)}
.s-emoji{font-size:14px;text-align:center}
.s-sym{font-weight:700;font-size:13px;transition:color .15s}
.s-type{font-size:11px;color:var(--muted)}
.s-badge{font-size:10px;font-weight:700;padding:3px 7px;border-radius:4px;text-align:center;letter-spacing:.4px;font-family:var(--font-ui)}
.b-BREAKOUT    {background:#dcfce7;color:#15803d;border:1px solid #86efac}
.b-POCKET      {background:#fef9c3;color:#854d0e;border:1px solid #fde047}
.b-PREBREAK    {background:#f3e8ff;color:#7e22ce;border:1px solid #d8b4fe}
.b-BBREAKP     {background:#dbeafe;color:#1d4ed8;border:1px solid #93c5fd}
.b-BFISH       {background:#ffedd5;color:#c2410c;border:1px solid #fdba74}
.b-MACROSS     {background:#f1f5f9;color:#475569;border:1px solid #cbd5e1}
.empty{text-align:center;padding:36px 20px;color:var(--muted);font-size:12px}
.empty .big{font-size:30px;margin-bottom:8px}

/* ── HEATMAP ─── */
.hmap-outer{overflow-x:auto;padding-bottom:4px}
.hmap-outer::-webkit-scrollbar{height:4px}
.hmap-outer::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
.hmap-row{display:inline-flex;flex-direction:row;gap:4px;align-items:flex-start;min-width:max-content;padding:2px}
.hmap-col{display:flex;flex-direction:column;gap:2px;width:162px;flex-shrink:0}
.hmap-group{display:flex;flex-direction:column;gap:2px;margin-bottom:3px}

.hmap-ghdr{
  display:flex;align-items:center;justify-content:center;
  padding:0 8px;height:24px;border-radius:4px;
  background:rgb(220,228,250);border:1px solid rgb(160,180,230);
  overflow:hidden;gap:16px;
}
.hmap-gname{
  font-family:var(--font-ui);font-size:10px;font-weight:700;
  text-transform:uppercase;letter-spacing:.6px;color:rgb(25,55,150);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.hmap-gavg{font-family:var(--font-mono);font-size:9px;font-weight:400;flex-shrink:0}
.hmap-gavg.pos{color:rgb(22,120,40)}
.hmap-gavg.neg{color:rgb(185,25,25)}
.hmap-gavg.zer{color:rgb(110,105,20)}

.hmap-cell{
  display:grid;grid-template-columns:56px 48px 1fr;
  align-items:center;height:24px;border-radius:4px;
  cursor:pointer;border:1px solid rgba(0,0,0,.1);
  transition:filter .12s,transform .1s,box-shadow .12s;
  overflow:hidden;
}
.hmap-cell:hover{filter:brightness(.88);transform:scale(1.035);z-index:2;box-shadow:0 2px 8px rgba(0,0,0,.18)}
.hmap-cell > span{display:flex;align-items:center;justify-content:center;height:100%;overflow:hidden;white-space:nowrap;font-family:var(--font-mono)}
.hc-sym  {font-size:10px;font-weight:400}
.hc-price{font-size:8.5px;font-weight:400;opacity:.82}
.hc-pct  {font-size:9.5px;font-weight:400}

/* ── POPUP ─── */
.overlay{display:none;position:fixed;inset:0;z-index:9999;background:rgba(17,24,39,.5);backdrop-filter:blur(4px);align-items:center;justify-content:center}
.overlay.on{display:flex}

.pbox{
  background:var(--surface);border:1px solid var(--border);border-radius:10px;
  box-shadow:0 20px 60px rgba(0,0,0,.15);
  width:99vw;max-width:1800px;height:94vh;
  display:flex;flex-direction:column;overflow:hidden;animation:popIn .2s ease
}
@keyframes popIn{from{opacity:0;transform:scale(.96) translateY(14px)}to{opacity:1;transform:none}}

.phdr{
  display:flex;align-items:center;justify-content:space-between;
  flex-wrap:wrap;padding:9px 14px;gap:6px;
  background:var(--surf2);border-bottom:1px solid var(--border);flex-shrink:0
}
.ptitle{font-family:var(--font-ui);font-size:17px;font-weight:800;color:var(--accent);letter-spacing:1.5px;flex-shrink:0}

/* Tab order: Scanner Chart ngay sau Vietstock */
.ctabs{display:flex;gap:2px;align-items:flex-end;flex-wrap:wrap}
.ctab{
  font-size:11px;font-family:var(--font-mono);font-weight:600;
  padding:5px 11px;border-radius:5px 5px 0 0;
  border:1px solid var(--border);border-bottom:2px solid transparent;
  background:var(--bg);color:var(--muted);cursor:pointer;transition:all .15s;
  white-space:nowrap;
}
.ctab.on{background:var(--surface);color:var(--accent);border-color:var(--border);border-bottom-color:var(--accent);font-weight:700}
.ctab:hover:not(.on){color:var(--accent);background:#eef3ff}

.closebtn{width:28px;height:28px;border-radius:50%;border:1px solid var(--border);background:var(--bg);color:var(--muted);font-size:16px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all .15s;flex-shrink:0}
.closebtn:hover{background:var(--red);color:#fff;border-color:var(--red)}

.pbody{flex:1;overflow:hidden;position:relative;border-top:1px solid var(--border)}
.tpanel{position:absolute;inset:0;display:none}
.tpanel.on{display:block}
.tpanel iframe{width:100%;height:100%;border:none;display:block}

/* ── Scanner Chart tab ─── */
#panel-scanner{overflow:hidden;background:#1a1a2e;display:none;flex-direction:column}
#panel-scanner.on{display:flex}

.scanner-loading{
  display:flex;align-items:center;justify-content:center;
  flex:1;color:#aaa;font-size:14px;font-family:var(--font-mono)
}

/*
  Album layout: ảnh chiếm toàn bộ, navigation bar nằm ở giữa cùng hàng với dots
  Không còn nút trái/phải cạnh ảnh — thay bằng nút ◀ ▶ inline trong nav bar
*/
.album-outer{
  flex:1;display:flex;flex-direction:column;overflow:hidden;
}

/* Vùng ảnh: chiếm toàn bộ chiều cao còn lại */
.album-center{
  flex:1;overflow-y:auto;display:flex;flex-direction:column;
  align-items:center;padding:12px 16px 4px;gap:8px;
}
.album-center::-webkit-scrollbar{width:4px}
.album-center::-webkit-scrollbar-thumb{background:#444;border-radius:2px}

.album-slide{display:none;flex-direction:column;align-items:center;gap:8px;width:100%}
.album-slide.on{display:flex}
.album-slide img{max-width:100%;max-height:calc(94vh - 160px);object-fit:contain;border-radius:6px;border:1px solid #333}
.album-label{font-size:11px;color:#888;font-family:var(--font-mono)}

/* Nav bar: nằm dưới ảnh, chứa nút ◀ + dots + ▶ + refresh trên cùng một hàng */
.album-nav-bar{
  display:flex;align-items:center;justify-content:center;
  gap:10px;padding:6px 0 8px;flex-shrink:0;
}
.album-nav-btn{
  width:30px;height:30px;border-radius:50%;
  border:1px solid #444;background:rgba(255,255,255,.08);
  color:#ccc;font-size:14px;cursor:pointer;
  display:flex;align-items:center;justify-content:center;
  transition:background .15s,color .15s,border-color .15s;
  user-select:none;flex-shrink:0;
}
.album-nav-btn:hover:not(.disabled){background:rgba(26,86,219,.5);color:#fff;border-color:#4d9ff5}
.album-nav-btn.disabled{opacity:.2;cursor:default;pointer-events:none}

.album-dots-wrap{display:flex;gap:6px;align-items:center}
.album-dot{width:8px;height:8px;border-radius:50%;background:#444;cursor:pointer;transition:all .15s}
.album-dot.on{background:#4d9ff5;transform:scale(1.3)}

/* Nút refresh — nằm ngay cạnh nút ▶ */
.album-refresh-btn{
  height:26px;padding:0 10px;border-radius:13px;
  border:1px solid #555;background:rgba(255,255,255,.06);
  color:#aaa;font-size:10px;font-family:var(--font-mono);cursor:pointer;
  display:flex;align-items:center;gap:5px;white-space:nowrap;
  transition:background .15s,color .15s,border-color .15s;
  user-select:none;flex-shrink:0;
}
.album-refresh-btn:hover{background:rgba(14,159,110,.35);color:#6ee7b7;border-color:#0e9f6e}
.album-refresh-btn.spinning span.ri{display:inline-block;animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

.album-hint{text-align:center;font-size:10px;color:#555;padding:0 0 4px;font-family:var(--font-mono);flex-shrink:0}

footer{text-align:center;padding:9px;color:var(--muted);font-size:10px;border-top:1px solid var(--border);background:var(--surface)}

::-webkit-scrollbar{width:5px;height:5px}
::-webkit-scrollbar-track{background:var(--bg)}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
::-webkit-scrollbar-thumb:hover{background:var(--muted)}

@media(max-width:768px){
  .pbox{width:100vw;height:100vh;border-radius:0}
  header h1{font-size:15px;letter-spacing:1px}
  .album-nav-btn{width:26px;height:26px;font-size:12px}
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

<!-- ══ POPUP CHART ══ -->
<div class="overlay" id="overlay">
  <div class="pbox">

    <div class="phdr">
      <span class="ptitle" id="ptitle">Chart</span>

      <!--
        THỨ TỰ TAB: Vietstock | Scanner Chart | Cơ bản | Tin tức | Tổng quan | 24HMoney
        Scanner Chart được đặt ngay sau Vietstock (yêu cầu 1)
      -->
      <div class="ctabs">
        <button class="ctab on"  id="ctab-vs"       onclick="switchTab('vs')">📈 Vietstock</button>
        <button class="ctab"     id="ctab-scanner"  onclick="switchTab('scanner')">🖼 Scanner Chart</button>
        <button class="ctab"     id="ctab-vnd-cs"   onclick="switchTab('vnd-cs')">⚖️ Cơ bản</button>
        <button class="ctab"     id="ctab-vnd-news" onclick="switchTab('vnd-news')">🗞️ Tin tức</button>
        <button class="ctab"     id="ctab-vnd-sum"  onclick="switchTab('vnd-sum')">📄 Tổng quan</button>
        <button class="ctab"     id="ctab-24h"      onclick="switchTab('24h')">💬 24HMoney</button>
      </div>

      <button class="closebtn" onclick="closePopup()">✕</button>
    </div>

    <div class="pbody">
      <!-- Tab Vietstock -->
      <div class="tpanel on" id="panel-vs">
        <iframe id="iframe-vs" src="about:blank" allowfullscreen></iframe>
      </div>
      <!-- Tab Scanner Chart: ngay sau Vietstock (yêu cầu 1) -->
      <div class="tpanel" id="panel-scanner">
        <div class="scanner-loading" id="scanner-loading">
          <span>⏳ Đang tạo chart từ scanner...</span>
        </div>
        <!--
          Album layout mới (yêu cầu 2):
          - Không có nút trái/phải cạnh ảnh
          - Nút ◀ ▶ nằm cùng hàng với dots trong nav bar bên dưới
        -->
        <div class="album-outer" id="album-outer" style="display:none">
          <div class="album-center">
            <div id="album-slides"></div>
          </div>
          <!-- Nav bar: ◀ · · · ▶ 🔄 -->
          <div class="album-nav-bar">
            <button class="album-nav-btn disabled" id="btn-prev" onclick="albumNav(-1)" title="Ảnh trước (←)">&#9664;</button>
            <div class="album-dots-wrap" id="album-dots"></div>
            <button class="album-nav-btn" id="btn-next" onclick="albumNav(1)" title="Ảnh sau (→)">&#9654;</button>
            <button class="album-refresh-btn" id="btn-refresh" onclick="refreshScannerChart()" title="Xoá cache, tải lại chart">
              <span class="ri">&#8635;</span><span>Làm mới</span>
            </button>
          </div>
          <div class="album-hint">◀ ▶ hoặc phím ← → để chuyển ảnh</div>
        </div>
      </div>
      <!-- Tab VND Cơ bản -->
      <div class="tpanel" id="panel-vnd-cs">
        <iframe id="iframe-vnd-cs" src="about:blank" allowfullscreen></iframe>
      </div>
      <!-- Tab VND Tin tức -->
      <div class="tpanel" id="panel-vnd-news">
        <iframe id="iframe-vnd-news" src="about:blank" allowfullscreen></iframe>
      </div>
      <!-- Tab VND Tổng quan -->
      <div class="tpanel" id="panel-vnd-sum">
        <iframe id="iframe-vnd-sum" src="about:blank" allowfullscreen></iframe>
      </div>
      <!-- Tab 24HMoney -->
      <div class="tpanel" id="panel-24h">
        <iframe id="iframe-24h" src="about:blank" allowfullscreen></iframe>
      </div>
    </div>

  </div>
</div>

<script>
// ═══════════════════════════════════════════════════════
// CONFIG
// ═══════════════════════════════════════════════════════
let SIG_TTL=30, HMAP_TTL=120;

async function loadConfig(){
  try{
    const j=await fetch('/api/config').then(r=>r.json());
    SIG_TTL=j.signal_ttl_sec||30; HMAP_TTL=j.heatmap_ttl_sec||120;
  }catch(e){}
  document.getElementById('footer-txt').textContent=
    `Scanner Bot Dashboard  •  Tín hiệu tự động làm mới sau ${SIG_TTL}s  •  Heatmap tự động làm mới sau ${HMAP_TTL}s`;
}

// ═══════════════════════════════════════════════════════
// HEATMAP CONFIG
// ═══════════════════════════════════════════════════════
const HMAP_COLS = [
  {groups:[{name:"VN30",syms:["FPT","GAS","NVL","VNM","VCB","PLX","TCB","MWG","STB","HPG","PNJ","BID","CTG","HDB","VJC","VPB","KDH","MBB","VHM","POW","VRE","MSN","SSI","ACB","BVH","GVR","TPB"]}]},
  {groups:[
    {name:"NGAN HANG",syms:["VCB","BID","CTG","MBB","ACB","TCB","TPB","HDB","SHB","STB","VIB","VPB","MSB","ABB","BVB","LPB"]},
    {name:"DAU KHI",  syms:["GAS","PVD","PVS","BSR","OIL","PVB","PVC","PLX","PET","PVT"]},
  ]},
  {groups:[
    {name:"CHUNG KHOAN",syms:["SSI","VND","CTS","FTS","HCM","MBS","DSE","BSI","SHS","VCI","VCK","ORS"]},
    {name:"XAY DUNG",   syms:["C47","C32","L14","CII","CTD","CTI","FCN","HBC","HUT","LCG","PC1","DPG","PHC","VCG"]},
  ]},
  {groups:[
    {name:"BAT DONG SAN",syms:["IJC","LDG","CEO","D2D","DIG","DXG","HDC","HDG","KDH","NLG","NTL","NVL","PDR","SCR","TIG","KBC","SZC"]},
    {name:"PHAN BON",    syms:["BFC","DCM","DPM"]},
    {name:"THEP",        syms:["HPG","HSG","NKG"]},
  ]},
  {groups:[
    {name:"BAN LE",    syms:["MSN","FPT","FRT","MWG","PNJ","DGW"]},
    {name:"THUY SAN",  syms:["ANV","FMC","CMX","VHC","IDI"]},
    {name:"CANG BIEN", syms:["HAH","GMD","SGP","VSC"]},
    {name:"CAO SU",    syms:["GVR","DPR","DRI","PHR","DRC"]},
    {name:"NHUA",      syms:["AAA","BMP","NTP"]},
  ]},
  {groups:[
    {name:"DIEN NUOC",  syms:["NT2","PC1","GEG","GEX","POW","TDM","BWE"]},
    {name:"DET MAY",    syms:["TCM","TNG","VGT","MSH"]},
    {name:"HANG KHONG", syms:["NCT","ACV","AST","HVN","SCS","VJC"]},
    {name:"BAO HIEM",   syms:["BMI","MIG","BVH"]},
    {name:"MIA DUONG",  syms:["LSS","SBT","QNS"]},
  ]},
  {groups:[
    {name:"DAU TU CONG",syms:["FCN","HHV","LCG","VCG","C4G","CTD","HBC","HSG","NKG","HPG","KSB","PLC"]},
  ]},
];

const TS_POOL=[
  "AAA","ACB","AGG","ANV","BCG","BFC","BID","BMI","BSR","BVB","BVH","BWE",
  "CII","CKG","CRE","CTD","CTG","CTI","CTR","CTS","D2D","DBC","DCM","DSE",
  "DGW","DIG","DPG","DPM","DRC","DRH","DXG","FCN","FMC","FPT","FRT","FTS",
  "GAS","GEG","GEX","GMD","GVR","HAG","HAX","HBC","HCM","HDB","HDC","VCK",
  "HDG","HNG","HPG","HSG","HTN","HVN","IDC","IJC","KBC","KDH","KSB","LCG",
  "LDG","LPB","LTG","MBB","MBS","MSB","MSN","MWG","NKG","NLG","NTL","NVL",
  "PC1","PDR","PET","PHR","PLC","PLX","PNJ","POW","PTB","PVD","PVS","PVT",
  "QNS","REE","SBT","SCR","SHB","SHS","SSI","STB","SZC","TCB","TDM","TIG",
  "TNG","TPB","TV2","VCB","VCI","VCS","VGT","VHC","VHM","VIB","VIC","VJC",
  "VNM","VPB","VRE",
];

function cellStyle(pct){
  let r,g,b;
  if      (pct>=6.5) {r=250;g=170;b=225}
  else if (pct>=4.0) {r=160;g=220;b=170}
  else if (pct>=2.0) {r=195;g=235;b=200}
  else if (pct>0.0)  {r=225;g=245;b=228}
  else if (pct===0)  {r=245;g=245;b=200}
  else if (pct>=-2.0){r=255;g=220;b=210}
  else if (pct>=-4.0){r=250;g=185;b=175}
  else if (pct>=-6.5){r=240;g=150;b=145}
  else               {r=175;g=250;b=255}
  const lum=.299*r+.587*g+.114*b;
  return{bg:`rgb(${r},${g},${b})`,fg:lum>160?'rgb(30,30,30)':'rgb(15,15,15)'};
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
  return p<100?p.toFixed(2):p.toFixed(1);
}

function mkCell(sym,data){
  const d=data[sym]||{};
  const pct=typeof d.pct==='number'?d.pct:0;
  const price=typeof d.price==='number'?d.price:0;
  const{bg,fg}=cellStyle(pct);
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
  const maxRows=Math.max(...HMAP_COLS.map(cd=>cd.groups.reduce((s,g)=>s+g.syms.length,0)));
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

// ═══════════════════════════════════════════════════════
// ALBUM STATE (Scanner Chart tab)
// ═══════════════════════════════════════════════════════
let _albumIdx=0, _albumTotal=0;

function _showAlbum(images){
  const slidesEl=document.getElementById('album-slides');
  const dotsEl  =document.getElementById('album-dots');
  slidesEl.innerHTML='';
  dotsEl.innerHTML='';
  _albumTotal=images.length;
  images.forEach((img,i)=>{
    slidesEl.innerHTML+=`<div class="album-slide${i===0?' on':''}" id="slide-${i}">
      <img src="${img.url}" alt="${img.label}" loading="lazy">
    </div>`;
    dotsEl.innerHTML+=`<div class="album-dot${i===0?' on':''}" id="dot-${i}" onclick="albumGoto(${i})"></div>`;
  });
  _albumIdx=0;
  _updateAlbumNav();
  document.getElementById('album-outer').style.display='flex';
  document.getElementById('scanner-loading').style.display='none';
}

function albumGoto(i){
  if(i<0||i>=_albumTotal)return;
  document.querySelectorAll('.album-slide').forEach((s,idx)=>s.classList.toggle('on',idx===i));
  document.querySelectorAll('.album-dot').forEach((d,idx)=>d.classList.toggle('on',idx===i));
  _albumIdx=i;
  _updateAlbumNav();
}

function albumNav(dir){ albumGoto(_albumIdx+dir); }

function refreshScannerChart(){
  if(!_sym) return;
  const btn=document.getElementById('btn-refresh');
  if(btn){ btn.classList.add('spinning'); btn.disabled=true; }
  // Xóa cache phía client bằng cách gọi API với tham số bust
  fetch(`/api/chart_images/${_sym}?bust=${Date.now()}`,{cache:'no-store'})
    .then(()=>{})
    .catch(()=>{});
  // Load lại với cache bị bỏ qua — dùng trick đổi URL
  loadScannerChartForce(_sym);
}

async function loadScannerChartForce(sym){
  document.getElementById('album-outer').style.display='none';
  document.getElementById('scanner-loading').style.display='flex';
  document.getElementById('scanner-loading').innerHTML=
    `<span>🔄 Đang làm mới chart <b>${sym}</b>…</span>`;
  // Gọi endpoint xóa cache rồi tải lại
  try{
    await fetch(`/api/chart_cache_clear/${sym}`,{method:'DELETE'}).catch(()=>{});
  }catch(e){}
  const btn=document.getElementById('btn-refresh');
  if(btn){ btn.classList.remove('spinning'); btn.disabled=false; }
  await loadScannerChart(sym);
}

function _updateAlbumNav(){
  document.getElementById('btn-prev').classList.toggle('disabled', _albumIdx===0);
  document.getElementById('btn-next').classList.toggle('disabled', _albumIdx===_albumTotal-1);
}

// Touch/swipe
let _touchStartX=0;
document.getElementById('panel-scanner').addEventListener('touchstart',e=>{_touchStartX=e.touches[0].clientX},{passive:true});
document.getElementById('panel-scanner').addEventListener('touchend',e=>{
  const dx=e.changedTouches[0].clientX-_touchStartX;
  if(Math.abs(dx)>50) albumNav(dx<0?1:-1);
},{passive:true});

// ── Phím mũi tên bàn phím (yêu cầu 3) ──────────────────
// Chỉ kích hoạt khi popup đang mở VÀ đang ở tab Scanner Chart
document.addEventListener('keydown', e => {
  const overlayOn = document.getElementById('overlay').classList.contains('on');
  if (!overlayOn) return;
  if (_tab !== 'scanner') return;
  if (_albumTotal === 0) return;
  if (e.key === 'ArrowLeft')  { e.preventDefault(); albumNav(-1); }
  if (e.key === 'ArrowRight') { e.preventDefault(); albumNav(1);  }
});

async function loadScannerChart(sym){
  document.getElementById('album-outer').style.display='none';
  document.getElementById('scanner-loading').style.display='flex';
  document.getElementById('scanner-loading').innerHTML=
    `<span>⏳ Đang tạo chart <b>${sym}</b>… (15–30 giây lần đầu)</span>`;

  try{
    const r=await fetch(`/api/chart_images/${sym}`);
    if(!r.ok){
      const j=await r.json().catch(()=>({}));
      throw new Error(j.error||`HTTP ${r.status}`);
    }
    const j=await r.json();
    if(j.images && j.images.length>0){
      const labels=j.labels||['📊 Daily [D]','📈 Weekly [W]','⚡ 15m'];
      _showAlbum(j.images.map((b64,i)=>({
        url:`data:image/png;base64,${b64}`,
        label: labels[i]||`Chart ${i+1}`,
      })));
      if(j.cached){
        const hint=document.querySelector('.album-hint');
        if(hint) hint.textContent='♻️ Dùng cache — click lại tab để làm mới';
      }
      return;
    }
    throw new Error('no_images');
  }catch(e){
    document.getElementById('scanner-loading').innerHTML=`
      <div style="text-align:center;color:#aaa;padding:24px">
        <div style="font-size:24px;margin-bottom:10px">⚠️</div>
        <div style="margin-bottom:8px">Không tải được chart <b style="color:#4d9ff5">${sym}</b></div>
        <div style="font-size:11px;color:#666;margin-bottom:16px">${e.message}</div>
        <div style="display:flex;gap:8px;justify-content:center;flex-wrap:wrap">
          <button onclick="loadScannerChart('${sym}')"
            style="padding:6px 14px;border-radius:5px;background:#1a56db;color:#fff;border:none;cursor:pointer;font-size:12px">
            🔄 Thử lại
          </button>
          <a href="https://stockchart.vietstock.vn/?stockcode=${sym}" target="_blank"
             style="padding:6px 14px;border-radius:5px;background:#374151;color:#fff;text-decoration:none;font-size:12px">
            📈 Stockchart
          </a>
        </div>
      </div>`;
  }
}

// ═══════════════════════════════════════════════════════
// POPUP STATE
// ═══════════════════════════════════════════════════════
let _sym='', _tab='vs';

const IFRAME_TABS = ['vnd-cs','vnd-news','vnd-sum','24h'];

function openChart(sym){
  _sym=sym.toUpperCase().trim();
  _tab='vs';
  document.getElementById('ptitle').textContent=`📈 ${_sym}`;

  document.getElementById('iframe-vs').src=
    `https://ta.vietstock.vn/?stockcode=${_sym.toLowerCase()}`;

  IFRAME_TABS.forEach(t=>{
    document.getElementById(`iframe-${t}`).src='about:blank';
  });

  document.getElementById('album-outer').style.display='none';
  document.getElementById('scanner-loading').style.display='flex';
  document.getElementById('scanner-loading').innerHTML='<span>⏳ Đang tạo chart từ scanner...</span>';

  _activateTab('vs');
  document.getElementById('overlay').classList.add('on');
  document.body.style.overflow='hidden';
}

function _activateTab(tab){
  _tab=tab;
  // Danh sách tất cả tab IDs khớp với thứ tự mới
  const allTabs=['vs','scanner','vnd-cs','vnd-news','vnd-sum','24h'];
  allTabs.forEach(t=>{
    document.getElementById('ctab-'+t).classList.toggle('on',t===tab);
    document.getElementById('panel-'+t).classList.toggle('on',t===tab);
  });

  if(tab==='vnd-cs'){
    const f=document.getElementById('iframe-vnd-cs');
    if(f.src==='about:blank')
      f.src=`https://dstock.vndirect.com.vn/tong-quan/${_sym}/diem-nhan-co-ban-popup`;
  }
  if(tab==='vnd-news'){
    const f=document.getElementById('iframe-vnd-news');
    if(f.src==='about:blank')
      f.src=`https://dstock.vndirect.com.vn/tong-quan/${_sym}/tin-tuc-ma-popup?type=dn`;
  }
  if(tab==='vnd-sum'){
    const f=document.getElementById('iframe-vnd-sum');
    if(f.src==='about:blank')
      f.src=`https://dstock.vndirect.com.vn/tong-quan/${_sym}`;
  }
  if(tab==='24h'){
    const f=document.getElementById('iframe-24h');
    if(f.src==='about:blank')
      f.src=`https://24hmoney.vn/stock/${_sym}/news`;
  }
  if(tab==='scanner'){
    loadScannerChart(_sym);
  }
}

function switchTab(tab){ _activateTab(tab); }

function closePopup(){
  document.getElementById('overlay').classList.remove('on');
  document.getElementById('iframe-vs').src='about:blank';
  IFRAME_TABS.forEach(t=>{
    document.getElementById(`iframe-${t}`).src='about:blank';
  });
  document.body.style.overflow='';
}

document.getElementById('overlay').addEventListener('click',e=>{
  if(e.target===document.getElementById('overlay'))closePopup();
});
document.addEventListener('keydown',e=>{
  if(e.key==='Escape') closePopup();
});

// ═══════════════════════════════════════════════════════
// CLOCK
// ═══════════════════════════════════════════════════════
function tick(){
  const n=new Date();
  document.getElementById('clock').textContent=
    n.toLocaleTimeString('vi-VN',{hour12:false})+'  '+n.toLocaleDateString('vi-VN');
}
setInterval(tick,1000);tick();

// ═══════════════════════════════════════════════════════
// BADGE
// ═══════════════════════════════════════════════════════
function badgeCls(sig){
  return({'BREAKOUT':'b-BREAKOUT','POCKET PIVOT':'b-POCKET','PRE-BREAK':'b-PREBREAK',
    'BOTTOMBREAKP':'b-BBREAKP','BOTTOMFISH':'b-BFISH','MA_CROSS':'b-MACROSS'})[sig]||'b-MACROSS';
}

// ═══════════════════════════════════════════════════════
// FETCH
// ═══════════════════════════════════════════════════════
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

// ═══════════════════════════════════════════════════════
// REFRESH BAR
// ═══════════════════════════════════════════════════════
function bar(sec){
  const el=document.getElementById('rbar');
  el.style.transition='none';el.style.width='0%';
  requestAnimationFrame(()=>{
    el.style.transition=`width ${sec}s linear`;
    el.style.width='100%';
  });
}

// ═══════════════════════════════════════════════════════
// INIT
// ═══════════════════════════════════════════════════════
async function init(){
  await loadConfig();
  bar(HMAP_TTL);
  await Promise.all([fetchSigs(),fetchHmap()]);
  setInterval(async()=>{bar(SIG_TTL);  await fetchSigs(); }, SIG_TTL  * 1000);
  setInterval(async()=>{bar(HMAP_TTL); await fetchHmap();}, HMAP_TTL * 1000);
}
init();
</script>
</body>
</html>
"""
