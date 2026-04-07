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
from flask import Flask, jsonify, Response, send_from_directory
import threading
import time
import os
from datetime import datetime
import pytz

TZ_VN = pytz.timezone('Asia/Ho_Chi_Minh')
app   = Flask(__name__)

# Thư mục static nằm cùng chỗ với dashboard_server.py
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

@app.route("/static/<path:filename>")
def serve_static(filename):
    """Serve file JS/CSS từ thư mục static/ local — không cần internet."""
    return send_from_directory(STATIC_DIR, filename)

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
SIGNAL_TTL_SEC  = 10

_chart_cache: dict = {}
_chart_lock         = threading.Lock()
CHART_TTL_SEC       = 0

# =============================================================================
# API ENDPOINTS
# =============================================================================

@app.route("/api/signals")
def api_signals():
    alerted = _get_alerted_today() if _get_alerted_today else {}
    result  = []
    cache = _get_history_cache() if _get_history_cache else {}
    
    for sym, sig in alerted.items():
        df = cache.get(sym)
        pct = round(float(df['close'].iloc[-1] / df['close'].iloc[-2] - 1) * 100, 2) if df is not None and len(df) >= 2 else None
        result.append({
            "symbol": sym,
            "signal": sig,
            "emoji":  _signal_emoji.get(sig, " 📌 "),
            "rank":   _signal_rank.get(sig, 0),
            "pct":    pct,
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
                print(f"  [Dashboard]  ❌  Fetch heatmap lỗi: {e}")
                
    return jsonify({
        "data":       _heatmap_cache["data"],
        "timestamp":  _heatmap_cache["ts"],
        "cached_age": int(time.time() - _heatmap_cache["updated_at"]),
    })

@app.route("/api/ohlcv/<symbol>")
def api_ohlcv(symbol):
    """
    Trả về OHLCV + indicators từ history_cache để vẽ TradingView Lightweight Chart.
    Tính EMA10, EMA20, EMA50, MA200, MACD(12,26,9) từ dữ liệu close.
    """
    symbol = symbol.upper().strip()
    cache  = _get_history_cache() if _get_history_cache else {}
    df = cache.get(symbol)
    if df is None or len(df) < 2:
        return jsonify({"error": "no_data"}), 404
        
    try:
        import pandas as pd
        close = df['close'].astype(float)
        highs  = df['high'].astype(float)  if 'high'   in df.columns else close
        lows   = df['low'].astype(float)   if 'low'    in df.columns else close
        opens  = df['open'].astype(float)  if 'open'   in df.columns else close
        volume = df['volume'].astype(float) if 'volume' in df.columns else pd.Series([0]*len(close), index=close.index)

        def ema(s, span):
            return s.ewm(span=span, adjust=False).mean()

        ema10  = ema(close, 10)
        ema20  = ema(close, 20)
        ema50  = ema(close, 50)
        ma200  = close.rolling(200).mean()

        # MACD
        ema12  = ema(close, 12)
        ema26  = ema(close, 26)
        macd_line   = ema12 - ema26
        macd_signal = ema(macd_line, 9)
        macd_hist   = macd_line - macd_signal

        def ts(idx):
            import calendar
            if hasattr(idx, 'timestamp'):
                return int(idx.timestamp())
            try:
                import pandas as pd
                return int(pd.Timestamp(idx).timestamp())
            except:
                return 0

        def ser(s):
            return [{"time": ts(i), "value": round(float(v), 4)} for i, v in s.items() if not pd.isna(v)]

        candles = []
        for i in df.index:
            t = ts(i)
            try:
                candles.append({
                    "time":  t,
                    "open":  round(float(opens[i]),  2),
                    "high":  round(float(highs[i]),  2),
                    "low":   round(float(lows[i]),   2),
                    "close": round(float(close[i]),  2),
                })
            except:
                pass

        vols = [{"time": ts(i), "value": round(float(volume[i]), 0),
                 "color": "rgba(14,159,110,0.5)" if close.iloc[list(df.index).index(i)] >= opens.iloc[list(df.index).index(i)] else "rgba(224,36,36,0.5)"}
                for i in df.index]

        return jsonify({
            "symbol":      symbol,
            "candles":     candles,
            "volume":      vols,
            "ema10":       ser(ema10),
            "ema20":       ser(ema20),
            "ema50":       ser(ema50),
            "ma200":       ser(ma200),
            "macd_line":   ser(macd_line),
            "macd_signal": ser(macd_signal),
            "macd_hist":   [{"time": ts(i), "value": round(float(v), 4),
                             "color": "rgba(14,159,110,0.7)" if v >= 0 else "rgba(224,36,36,0.7)"}
                            for i, v in macd_hist.items() if not pd.isna(v)],
            "updated_at":  datetime.now(TZ_VN).strftime("%H:%M:%S"),
        })

    except Exception as e:
        print(f"  [Dashboard]  ❌  OHLCV {symbol} lỗi: {e}")
        return jsonify({"error": str(e)}), 500

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
        print(f"  [Dashboard]  📊  Tạo chart {symbol}...")
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
        print(f"  [Dashboard]  ✅  Chart {symbol}: {len(b64_list)} ảnh OK ({ts}→{ts2})")
        return jsonify({
            "symbol": symbol,
            "images": b64_list,
            "labels": labels,
            "cached": False,
        })
    except Exception as e:
        print(f"  [Dashboard]  ❌  Chart {symbol} lỗi: {e}")
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
    print(f" 🌐  Dashboard khởi động tại http://0.0.0.0:{port}")
    print(f"   Tín hiệu refresh : {SIGNAL_TTL_SEC}s (đọc RAM, không gọi API)")
    print(f"   Heatmap refresh  : {HEATMAP_TTL_SEC}s (gọi API, chỉ khi có người mở)")
    print(f"   Scanner Chart    : {' ✅  đã đăng ký' if fetch_chart_fn else ' ❌  chưa đăng ký'} (cache {CHART_TTL_SEC}s)")
    print(f"   TradingView Tab  :  ✅  EMA10/20/50, MA200, VOL, MACD — đọc trực tiếp từ RAM cache")
    
    js_path = os.path.join(STATIC_DIR, "lightweight-charts.min.js")
    if os.path.exists(js_path):
        size_kb = os.path.getsize(js_path) // 1024
        print(f"   TV Chart JS      :  ✅  static/lightweight-charts.min.js ({size_kb} KB) — serve local")
    else:
        print(f"   TV Chart JS      :  ⚠️   CHƯA CÓ FILE! Chạy: bash setup_static.sh")

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
.hmap-panel-hdr{display:flex;align-items:center;gap:6px;padding:8px 16px;background:var(--surf2);border-bottom:1px solid var(--border);flex-wrap:nowrap}
.hmap-hdr-row1{display:flex;align-items:center;gap:8px;flex-shrink:0;min-width:0}
.hmap-ts-wrap{margin-left:auto;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex-shrink:1;min-width:0}
@media screen and (max-width:768px){
.hmap-panel-hdr{flex-direction:column!important;align-items:flex-start!important;gap:4px!important;padding:7px 10px!important}
.hmap-hdr-row1{width:100%!important;flex-wrap:nowrap!important;overflow-x:auto!important;overflow-y:visible!important;scrollbar-width:none!important;-ms-overflow-style:none!important;gap:6px!important}
.hmap-hdr-row1::-webkit-scrollbar{display:none!important}
.hmap-hdr-row1 > *{flex-shrink:0!important}
.hmap-search-wrap{flex-shrink:0!important}
.hmap-search-input{width:122px!important;font-size:11px!important}
.hmap-search-input:focus{width:122px!important}
.hmap-ts-wrap{margin-left:0!important;width:100%!important;white-space:normal!important;overflow:visible!important;text-overflow:unset!important;font-size:9px!important;line-height:1.5!important;display:block!important;word-break:break-word!important}
.panel-title{white-space:nowrap!important;font-size:11px!important;letter-spacing:1px!important}
.panel-meta{font-size:9px!important;white-space:nowrap!important;overflow:hidden!important;text-overflow:ellipsis!important;max-width:55%!important}
.panel-hdr{gap:6px!important;padding:7px 10px!important}
}
.hmap-link-btn{display:inline-flex;align-items:center;gap:5px;padding:4px 11px;border-radius:5px;border:1px solid var(--border);background:var(--surface);color:var(--accent);font-family:var(--font-mono);font-size:10px;font-weight:600;cursor:pointer;text-decoration:none;white-space:nowrap;transition:all .15s}
.hmap-link-btn:hover{background:var(--accent);color:#fff;border-color:var(--accent)}
.hmap-search-wrap{position:relative;display:flex;align-items:center;flex-shrink:0}
.hmap-search-wrap .s-icon{position:absolute;left:11px;top:50%;transform:translateY(-50%);color:var(--muted);font-size:13px;pointer-events:none}
.hmap-search-input{width:140px;padding:5px 10px 5px 30px;border-radius:20px;border:1px solid var(--border);background:var(--surface);color:var(--text);font-family:var(--font-mono);font-size:11px;outline:none;transition:border-color .15s,box-shadow .15s,width .2s}
.hmap-search-input::placeholder{color:var(--muted)}
.hmap-search-input:focus{border-color:var(--accent);box-shadow:0 0 0 2px rgba(26,86,219,.12);width:170px}
.pbar-wrap{height:2px;background:transparent;overflow:hidden}
.pbar-fill{height:100%;width:0%;background:linear-gradient(90deg,var(--accent),var(--green));opacity:0.5;transition:none}
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
.b-BREAKOUT{background:#dcfce7;color:#15803d;border:1px solid #86efac}
.b-POCKET{background:#fef9c3;color:#854d0e;border:1px solid #fde047}
.b-PREBREAK{background:#f3e8ff;color:#7e22ce;border:1px solid #d8b4fe}
.b-BBREAKP{background:#dbeafe;color:#1d4ed8;border:1px solid #93c5fd}
.b-BFISH{background:#ffedd5;color:#c2410c;border:1px solid #fdba74}
.b-MACROSS{background:#f1f5f9;color:#475569;border:1px solid #cbd5e1}
.empty{text-align:center;padding:36px 20px;color:var(--muted);font-size:12px}
.empty .big{font-size:30px;margin-bottom:8px}
.hmap-outer{overflow-x:auto;padding-bottom:4px}
.hmap-outer::-webkit-scrollbar{height:4px}
.hmap-outer::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
.hmap-row{display:inline-flex;flex-direction:row;gap:4px;align-items:flex-start;min-width:max-content;padding:2px}
.hmap-col{display:flex;flex-direction:column;gap:2px;width:162px;flex-shrink:0}
.hmap-group{display:flex;flex-direction:column;gap:2px}
.hmap-ghdr{display:flex;align-items:center;justify-content:center;padding:0 8px;height:24px;border-radius:4px;background:rgb(220,228,250);border:1px solid rgb(160,180,230);overflow:hidden;gap:16px}
.hmap-gname{font-family:var(--font-ui);font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:rgb(25,55,150);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hmap-gavg{font-family:var(--font-mono);font-size:9px;font-weight:400;flex-shrink:0}
.hmap-gavg.pos{color:rgb(22,120,40)}
.hmap-gavg.neg{color:rgb(185,25,25)}
.hmap-gavg.zer{color:rgb(110,105,20)}
.hmap-cell{display:grid;grid-template-columns:56px 48px 1fr;align-items:center;height:24px;border-radius:4px;cursor:pointer;border:1px solid rgba(0,0,0,.1);transition:filter .12s,transform .1s,box-shadow .12s;overflow:hidden}
.hmap-cell:hover{filter:brightness(.88);transform:scale(1.035);z-index:2;box-shadow:0 2px 8px rgba(0,0,0,.18)}
.hmap-cell > span{display:flex;align-items:center;justify-content:center;height:100%;overflow:hidden;white-space:nowrap;font-family:var(--font-mono)}
.hc-sym{font-size:10px;font-weight:400}
.hc-price{font-size:8.5px;font-weight:400;opacity:.82}
.hc-pct{font-size:9.5px;font-weight:400}
/* ── POPUP ── */
.overlay{display:none;position:fixed;inset:0;z-index:9999;background:rgba(17,24,39,.5);backdrop-filter:blur(4px);align-items:center;justify-content:center}
@media(max-width:768px){.overlay{backdrop-filter:none!important;-webkit-backdrop-filter:none!important;background:rgba(17,24,39,0)!important}}
.overlay.on{display:flex}
.pbox{background:var(--surface);border:1px solid var(--border);border-radius:10px;box-shadow:0 20px 60px rgba(0,0,0,.15);width:99vw;max-width:1800px;height:94vh;display:flex;flex-direction:column;overflow:hidden;animation:popIn .2s ease;will-change:transform;transform:translateZ(0);-webkit-transform:translateZ(0);backface-visibility:hidden;-webkit-backface-visibility:hidden}
@keyframes popIn{from{opacity:0;transform:scale(.96) translateY(14px)}to{opacity:1;transform:none}}
.phdr{display:grid;grid-template-columns:auto 1fr auto;align-items:center;padding:7px 14px;gap:0;background:var(--surf2);border-bottom:1px solid var(--border);flex-shrink:0}
.phdr-left{display:flex;align-items:center;gap:8px;justify-content:flex-start}
.phdr-center{display:flex;align-items:flex-end;justify-content:center}
.phdr-right{display:flex;align-items:center;justify-content:flex-end}
.ptitle{font-family:var(--font-ui);font-size:17px;font-weight:800;color:var(--accent);letter-spacing:1.5px;flex-shrink:0;white-space:nowrap}
.popup-search-wrap{position:relative;display:flex;align-items:center}
.popup-search-wrap .ps-icon{position:absolute;left:10px;top:50%;transform:translateY(-50%);color:var(--muted);font-size:12px;pointer-events:none}
.popup-search-input{width:160px;padding:5px 10px 5px 28px;border-radius:20px;border:1px solid var(--border);background:var(--surface);color:var(--text);font-family:var(--font-mono);font-size:11px;outline:none;transition:border-color .15s,box-shadow .15s,width .2s}
.popup-search-input::placeholder{color:var(--muted)}
.popup-search-input:focus{border-color:var(--accent);box-shadow:0 0 0 2px rgba(26,86,219,.12);width:200px}
.ctabs{display:flex;gap:2px;align-items:flex-end;flex-wrap:wrap}
.ctab{font-size:11px;font-family:var(--font-mono);font-weight:600;padding:5px 11px;border-radius:5px 5px 0 0;border:1px solid var(--border);border-bottom:2px solid transparent;background:var(--bg);color:var(--muted);cursor:pointer;transition:all .15s;white-space:nowrap}
.ctab.on{background:var(--surface);color:var(--accent);border-color:var(--border);border-bottom-color:var(--accent);font-weight:700}
.ctab:hover:not(.on){color:var(--accent);background:#eef3ff}
.closebtn{width:28px;height:28px;border-radius:50%;border:1px solid var(--border);background:var(--bg);color:var(--muted);font-size:16px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all .15s;flex-shrink:0}
.closebtn:hover{background:var(--red);color:#fff;border-color:var(--red)}
.pbody{flex:1;overflow:hidden;position:relative;border-top:1px solid var(--border)}
.tpanel{position:absolute;inset:0;display:none}
.tpanel.on{display:block}
.tpanel iframe{width:100%;height:100%;border:none;display:block}
.pbox.swiping .tpanel iframe{pointer-events:none;will-change:auto;visibility:hidden}
.pbox.swiping{isolation:isolate;contain:strict}
/* ── TRADINGVIEW TAB ── */
#panel-tv{overflow:hidden;background:#ffffff;display:none;flex-direction:column}
#panel-tv.on{display:flex}
#tv-loading{display:flex;align-items:center;justify-content:center;flex:1;color:#6b7280;font-size:13px;font-family:var(--font-mono);background:#fff}
#tv-chart-wrap{flex:1;display:flex;flex-direction:column;overflow:hidden;background:#ffffff}
#tv-chart-container{flex:1;position:relative;background:#ffffff}
/* MACD panel */
#tv-macd-container{height:120px;flex-shrink:0;background:#ffffff;border-top:1px solid #f0f0f0;position:relative}
#tv-macd-legend{
position:absolute;top:4px;left:8px;z-index:10;
display:flex;align-items:center;gap:10px;
font-family:'IBM Plex Mono',monospace;font-size:10px;pointer-events:none;
}
/* Scanner Chart tab */
#panel-scanner{overflow:hidden;background:#ffffff;display:none;flex-direction:column}
#panel-scanner.on{display:flex}
.scanner-loading{display:flex;align-items:center;justify-content:center;flex:1;color:#6b7280;font-size:14px;font-family:var(--font-mono)}
.album-outer{flex:1;display:flex;flex-direction:column;overflow:hidden}
.album-center{flex:1;overflow-y:auto;display:flex;flex-direction:column;align-items:center;padding:4px 4px 2px;gap:4px;background:#ffffff}
.album-center::-webkit-scrollbar{width:4px}
.album-center::-webkit-scrollbar-thumb{background:#444;border-radius:2px}
.album-slide{display:none;flex-direction:column;align-items:center;gap:8px;width:100%}
.album-slide.on{display:flex}
.album-slide img{max-width:100%;max-height:calc(94vh - 120px);object-fit:contain;border-radius:3px;border:1px solid #dde3ee}
.album-label{font-size:11px;color:#888;font-family:var(--font-mono)}
.album-nav-bar{display:flex;align-items:center;justify-content:center;gap:10px;padding:6px 0 8px;flex-shrink:0;background:#ffffff}
.album-nav-btn{width:30px;height:30px;border-radius:50%;border:1px solid #dde3ee;background:#f4f6fb;color:#6b7280;font-size:14px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:background .15s,color .15s,border-color .15s;user-select:none;flex-shrink:0}
.album-nav-btn:hover:not(.disabled){background:#1a56db;color:#fff;border-color:#1a56db}
.album-nav-btn.disabled{opacity:.25;cursor:default;pointer-events:none}
.album-dots-wrap{display:flex;gap:6px;align-items:center}
.album-dot{width:8px;height:8px;border-radius:50%;background:#dde3ee;cursor:pointer;transition:all .15s}
.album-dot.on{background:#1a56db;transform:scale(1.3)}
.album-refresh-btn{width:30px;height:30px;padding:0;border-radius:50%;border:1px solid #dde3ee;background:#f4f6fb;color:#6b7280;font-size:15px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:background .15s,color .15s,border-color .15s;user-select:none;flex-shrink:0}
.album-refresh-btn:hover{background:#0e9f6e;color:#fff;border-color:#0e9f6e}
.album-refresh-btn.spinning span.ri{display:inline-block;animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.album-hint{text-align:center;font-size:10px;color:#9ca3af;padding:0 0 4px;font-family:var(--font-mono);flex-shrink:0;background:#ffffff}
footer{text-align:center;padding:9px;color:var(--muted);font-size:10px;border-top:1px solid var(--border);background:var(--surface)}
::-webkit-scrollbar{width:5px;height:5px}
::-webkit-scrollbar-track{background:var(--bg)}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
::-webkit-scrollbar-thumb:hover{background:var(--muted)}
@media(max-width:768px){
.pbox{width:100vw;height:100vh;border-radius:0}
header h1{font-size:15px;letter-spacing:1px}
.album-nav-btn{width:26px;height:26px;font-size:12px}
.popup-search-input{width:120px}
.popup-search-input:focus{width:150px}
#tv-macd-container{height:80px}
}
@media screen and (max-width:768px){
.pbox{width:100vw!important;max-width:100vw!important;height:100dvh!important;border-radius:0!important;border:none!important;overflow:visible!important;max-height:100dvh!important}
.pbody{overflow:hidden!important}
.phdr{display:flex!important;flex-direction:column!important;padding:0!important;gap:0!important;overflow:visible!important;width:100vw!important;max-width:100vw!important;flex-shrink:0!important}
.phdr-left,.phdr-center,.phdr-right{display:none!important}
}
#mob-tabrow{display:flex!important;flex-direction:row!important;flex-wrap:nowrap!important;align-items:center;overflow-x:scroll!important;overflow-y:hidden!important;-webkit-overflow-scrolling:touch;overscroll-behavior-x:contain;scroll-snap-type:x proximity;padding:6px 8px!important;gap:5px!important;background:var(--surf2)!important;border-bottom:1px solid var(--border)!important;scrollbar-width:none!important;-ms-overflow-style:none!important}
#mob-tabrow::-webkit-scrollbar{display:none!important}
#mob-tabrow > button{flex-shrink:0!important;flex-grow:0!important;white-space:nowrap!important;scroll-snap-align:start;padding:7px 14px!important;border-radius:6px!important;border:1px solid var(--border)!important;font-size:12px!important;font-family:var(--font-mono)!important;font-weight:600!important;cursor:pointer!important;transition:background .15s,color .15s!important;background:var(--bg)!important;color:var(--muted)!important;display:inline-flex!important;align-items:center!important;justify-content:center!important}
#mob-tabrow > button.on{background:var(--surface)!important;color:var(--accent)!important;border-color:var(--accent)!important;font-weight:700!important;box-shadow:0 2px 0 0 var(--accent)!important}
/* Mobile lightbox */
#mob-lightbox{display:none;position:fixed;inset:0;z-index:99999;background:#fff;overflow:hidden;touch-action:none}
#mob-lightbox.on{display:block}
#lb-viewport{position:absolute;inset:0;overflow:hidden}
#lb-strip{display:flex;flex-direction:row;height:100%;will-change:transform}
#lb-strip.snapping{transition:transform 0.32s cubic-bezier(0.25,0.46,0.45,0.94)}
.lb-slide{flex-shrink:0;width:100vw;height:100%;display:flex;align-items:center;justify-content:center;overflow:hidden}
.lb-slide img{max-width:100vw;max-height:100dvh;object-fit:contain;display:block;transform-origin:center center;will-change:transform;user-select:none;-webkit-user-drag:none;pointer-events:none;transition:transform 0.25s ease}
.lb-slide img.zooming{transition:none!important}
#mob-lightbox-close{position:absolute;top:14px;right:14px;width:38px;height:38px;border-radius:50%;background:rgba(0,0,0,.07);border:1px solid rgba(0,0,0,.15);color:#333;font-size:20px;display:flex;align-items:center;justify-content:center;cursor:pointer;z-index:10;touch-action:manipulation}
#mob-lightbox-close:active{background:rgba(0,0,0,.14)}
#mob-lightbox-counter{position:absolute;bottom:20px;left:50%;transform:translateX(-50%);display:flex;gap:8px;align-items:center;z-index:10;pointer-events:none}
.mob-lb-dot{width:7px;height:7px;border-radius:50%;background:rgba(0,0,0,.18);transition:background .2s,transform .2s}
.mob-lb-dot.on{background:#1a56db;transform:scale(1.4)}
#mob-lightbox-label{position:absolute;top:16px;left:50%;transform:translateX(-50%);color:rgba(30,30,30,.85);font-family:var(--font-mono);font-size:12px;white-space:nowrap;z-index:10;pointer-events:none;background:rgba(0,0,0,.08);padding:3px 12px;border-radius:20px;transition:opacity .15s}
#lb-zoom-hint{position:absolute;bottom:52px;left:50%;transform:translateX(-50%);background:rgba(0,0,0,.45);color:#fff;font-family:var(--font-mono);font-size:10px;padding:3px 10px;border-radius:20px;z-index:11;pointer-events:none;opacity:0;transition:opacity .3s;white-space:nowrap}
#edge-swipe-zone{position:fixed;left:0;top:0;width:30px;height:100%;z-index:10000;display:none;touch-action:pan-y}
#edge-swipe-zone.on{display:block}
#mob-close-float{display:none}
@media screen and (max-width:768px) and (orientation:portrait){
#mob-close-float{display:flex;position:fixed;right:0;top:50%;transform:translateY(-50%);z-index:10001;width:15px;height:100px;border-radius:6px 0 0 6px;background:rgba(17,24,39,.02);backdrop-filter:blur(1px);-webkit-backdrop-filter:blur(1px);border:1px solid rgba(255,255,255,.03);border-right:none;color:rgba(255,255,255,.10);font-size:12px;align-items:center;justify-content:center;cursor:pointer;transition:background .2s,color .2s;touch-action:manipulation;pointer-events:auto}
#mob-close-float:active{background:rgba(17,24,39,.6);color:rgba(255,255,255,.8)}
}
@media screen and (max-width:768px) and (orientation:landscape){#mob-close-float{display:none!important}}
@media screen and (min-width:769px){#mob-close-float{display:none!important}}

/* ── TIMEFRAME BUTTONS ── */
.tf-btn{font-family:'IBM Plex Mono',monospace;font-size:11px;font-weight:600;padding:3px 10px;border-radius:4px;border:1px solid #dde3ee;background:#f4f6fb;color:#6b7280;cursor:pointer;transition:all .15s;line-height:1.4;}
.tf-btn:hover{background:#eef3ff;color:#1a56db;border-color:#1a56db;}
.tf-btn.tf-on{background:#1a56db;color:#fff;border-color:#1a56db;}
</style>
</head>
<body>
<header>
<h1> ⚡  Scanner Dashboard</h1>
<div class="hdr-right">
<div class="dot-live"></div>
<span id="clock">--:--:--</span>
</div>
</header>
<div class="wrap">
<div class="panel">
<div class="panel-hdr">
<span class="panel-title">Tín hiệu hôm nay</span>
<span class="panel-meta" id="sig-meta">Đang tải...</span>
</div>
<div class="pbar-wrap"><div class="pbar-fill" id="pbar-sig"></div></div>
<div class="panel-body">
<div class="sig-list" id="sig-list">
<div class="empty"><div class="big"> 📡 </div><div>Đang tải...</div></div>
</div>
</div>
</div>
<div class="panel">
<div class="hmap-panel-hdr">
<div class="hmap-hdr-row1">
<span class="panel-title">Heatmap</span>
<button class="hmap-link-btn" onclick="openUrl('https://dstock.vndirect.com.vn','MARKET')">MARKET</button>
<button class="hmap-link-btn" onclick="openUrl('https://24hmoney.vn/indices/vn-index','VNINDEX')">VNINDEX</button>
<div class="hmap-search-wrap">
<span class="s-icon"> 🔍 </span>
<input class="hmap-search-input" id="hmap-search-input" type="text" placeholder="Tìm kiếm mã" maxlength="10" autocomplete="off" spellcheck="false">
</div>
</div>
<span class="panel-meta hmap-ts-wrap" id="hmap-ts">Đang tải...</span>
</div>
<div class="pbar-wrap"><div class="pbar-fill" id="pbar-hmap"></div></div>
<div class="panel-body" style="padding:8px">
<div class="hmap-outer">
<div class="hmap-row" id="hmap-grid">
<div class="empty"><div class="big"> 🗺 </div><div>Đang tải...</div></div>
</div>
</div>
</div>
</div>
</div>
<footer id="footer-txt">Scanner Bot Dashboard</footer>
<div class="overlay" id="overlay">
<button id="mob-close-float" onclick="closePopup()" aria-label="Đóng"> ✕ </button>
<div class="pbox">
<div class="phdr">
<div class="phdr-left">
<span class="ptitle" id="ptitle">Chart</span>
<div class="popup-search-wrap">
<span class="ps-icon"> 🔍 </span>
<input class="popup-search-input" id="popup-search-input" type="text" placeholder="Tìm mã khác" maxlength="10" autocomplete="off" spellcheck="false">
</div>
</div>
<div class="phdr-center">
<div class="ctabs">
<button class="ctab on"  id="ctab-tv"       onclick="switchTab('tv')"> 📊  TV Chart</button>
<button class="ctab"     id="ctab-vs"       onclick="switchTab('vs')"> 📈  Vietstock</button>
<button class="ctab"     id="ctab-scanner"  onclick="switchTab('scanner')"> 🖼  Scanner Chart</button>
<button class="ctab"     id="ctab-vnd-cs"   onclick="switchTab('vnd-cs')"> ⚖️  Cơ bản</button>
<button class="ctab"     id="ctab-vnd-news" onclick="switchTab('vnd-news')"> 🗞️  Tin tức</button>
<button class="ctab"     id="ctab-vnd-sum"  onclick="switchTab('vnd-sum')"> 📄  Tổng quan</button>
<button class="ctab"     id="ctab-24h"      onclick="switchTab('24h')"> 💬  24HMoney</button>
</div>
</div>
<div class="phdr-right">
<button class="closebtn" onclick="closePopup()"> ✕ </button>
</div>
</div>
<div class="pbody">
<div class="tpanel on" id="panel-tv">
  <div id="tv-loading">⏳ Đang tải dữ liệu...</div>
  <div id="tv-chart-wrap" style="display:none;flex-direction:column;height:100%;">
    <div id="tv-toolbar" style="display:flex;align-items:center;gap:8px;padding:5px 10px;background:#fafafa;border-bottom:1px solid #eeeeee;flex-shrink:0;">
      <span id="tv-sym-label" style="font-family:'Barlow Condensed',sans-serif;font-size:14px;font-weight:800;color:#1a56db;letter-spacing:1px;min-width:52px;">—</span>
      <div style="display:flex;gap:3px;">
        <button id="tf-btn-D" onclick="tvSetTF('D')" class="tf-btn tf-on">D</button>
        <button id="tf-btn-W" onclick="tvSetTF('W')" class="tf-btn">W</button>
      </div>
      <span style="font-family:'IBM Plex Mono',monospace;font-size:10px;color:#6b7280;margin-left:4px;">
        O<span id="leg-open" style="color:#374151;margin-left:2px;">—</span>
        &nbsp;H<span id="leg-high" style="color:#0e9f6e;margin-left:2px;">—</span>
        &nbsp;L<span id="leg-low" style="color:#e02424;margin-left:2px;">—</span>
        &nbsp;C<span id="leg-close" style="color:#374151;margin-left:2px;font-weight:700;">—</span>
      </span>
      <div style="margin-left:auto;position:relative;display:flex;align-items:center;">
        <span style="position:absolute;left:9px;color:#9ca3af;font-size:11px;pointer-events:none;">🔍</span>
        <input id="tv-sym-input" type="text" placeholder="Nhập mã..." maxlength="10" autocomplete="off" spellcheck="false"
          style="width:110px;padding:4px 8px 4px 26px;border-radius:16px;border:1px solid #dde3ee;background:#fff;color:#111827;font-family:'IBM Plex Mono',monospace;font-size:11px;outline:none;transition:border-color .15s,box-shadow .15s,width .2s;"
          onfocus="this.select();this.style.borderColor='#1a56db';this.style.boxShadow='0 0 0 2px rgba(26,86,219,.12)';this.style.width='140px';"
          onblur="this.style.borderColor='#dde3ee';this.style.boxShadow='none';this.style.width='110px';">
      </div>
    </div>
    <div id="tv-chart-container" style="flex:1;position:relative;background:#fff;min-height:0;"></div>
    <div id="tv-macd-container" style="height:110px;flex-shrink:0;background:#fff;border-top:1px solid #f0f0f0;position:relative;">
      <div id="tv-macd-legend" style="position:absolute;top:3px;left:8px;z-index:10;display:flex;align-items:center;gap:8px;font-family:'IBM Plex Mono',monospace;font-size:9px;pointer-events:none;">
        <span style="font-weight:700;color:#374151;">MACD(12,26,9)</span>
        <span style="color:#9ca3af;">MACD</span><span id="leg-macd" style="color:#2563eb;font-weight:600;">—</span>
        <span style="color:#9ca3af;">Signal</span><span id="leg-signal" style="color:#f97316;font-weight:600;">—</span>
        <span style="color:#9ca3af;">Hist</span><span id="leg-hist" style="font-weight:600;">—</span>
      </div>
    </div>
  </div>
</div>
<div class="tpanel" id="panel-vs">
<iframe id="iframe-vs" src="about:blank" allowfullscreen></iframe>
</div>
<div class="tpanel" id="panel-scanner">
<div class="scanner-loading" id="scanner-loading"><span> ⏳  Đang tạo chart từ scanner...</span></div>
<div class="album-outer" id="album-outer" style="display:none">
<div class="album-center"><div id="album-slides"></div></div>
<div class="album-nav-bar">
<button class="album-nav-btn disabled" id="btn-prev" onclick="albumNav(-1)" title="Ảnh trước (←)">&#9664;</button>
<div class="album-dots-wrap" id="album-dots"></div>
<button class="album-nav-btn" id="btn-next" onclick="albumNav(1)" title="Ảnh sau (→)">&#9654;</button>
<button class="album-refresh-btn" id="btn-refresh" onclick="refreshScannerChart()" title="Làm mới chart"><span class="ri">&#8635;</span></button>
</div>
<div class="album-hint"> ◀   ▶  hoặc phím ← → để chuyển ảnh</div>
</div>
</div>
<div class="tpanel" id="panel-vnd-cs"><iframe id="iframe-vnd-cs" src="about:blank" allowfullscreen></iframe></div>
<div class="tpanel" id="panel-vnd-news"><iframe id="iframe-vnd-news" src="about:blank" allowfullscreen></iframe></div>
<div class="tpanel" id="panel-vnd-sum"><iframe id="iframe-vnd-sum" src="about:blank" allowfullscreen></iframe></div>
<div class="tpanel" id="panel-24h"><iframe id="iframe-24h" src="about:blank" allowfullscreen></iframe></div>
<div class="tpanel" id="panel-url"><iframe id="iframe-url" src="about:blank" allowfullscreen></iframe></div>
</div>
</div>
</div>
<div id="mob-lightbox" onclick="lbClose()">
<div id="lb-viewport" onclick="event.stopPropagation()"><div id="lb-strip"></div></div>
<div id="mob-lightbox-label"> 📊  Daily [D]</div>
<button id="mob-lightbox-close" onclick="lbClose()"> ✕ </button>
<div id="mob-lightbox-counter"></div>
<div id="lb-zoom-hint">Chụm 2 ngón để zoom</div>
</div>
<div id="edge-swipe-zone"></div>
<script src="/static/lightweight-charts.min.js"></script>
<script>
// ═══════════════════════════════════════════════════════
// CONFIG
// ═══════════════════════════════════════════════════════
let SIG_TTL=30, HMAP_TTL=120;
async function loadConfig(){
try{const j=await fetch('/api/config').then(r=>r.json());SIG_TTL=j.signal_ttl_sec||30;HMAP_TTL=j.heatmap_ttl_sec||120;}catch(e){}
document.getElementById('footer-txt').textContent=`Scanner Bot Dashboard  •  Tín hiệu tự động làm mới sau ${SIG_TTL}s  •  Heatmap tự động làm mới sau ${HMAP_TTL}s`;
}
// ═══════════════════════════════════════════════════════
// SEARCH BARS
// ═══════════════════════════════════════════════════════
(function(){
const inp=document.getElementById('hmap-search-input');
inp.addEventListener('keydown',function(e){
if(e.key==='Enter'){const sym=this.value.trim().toUpperCase();if(sym.length>=2){this.value='';this.blur();openChart(sym);}}
if(e.key==='Escape'){this.value='';this.blur();}
});
inp.addEventListener('focus',function(){this.select();});
})();
(function(){
const inp=document.getElementById('popup-search-input');
inp.addEventListener('keydown',function(e){
if(e.key==='Enter'){const sym=this.value.trim().toUpperCase();if(sym.length>=2){this.value='';this.blur();openChart(sym);}}
if(e.key==='Escape'){closePopup();}
});
inp.addEventListener('focus',function(){this.select();});
})();
// ═══════════════════════════════════════════════════════
// HEATMAP CONFIG
// ═══════════════════════════════════════════════════════
const HMAP_COLS=[
{groups:[{name:"VN30",syms:["FPT","GAS","NVL","VNM","VCB","PLX","TCB","MWG","STB","HPG","PNJ","BID","CTG","HDB","VJC","VPB","KDH","MBB","VHM","POW","VRE","MSN","SSI","ACB","BVH","GVR","TPB"]}]},
{groups:[{name:"NGAN HANG",syms:["VCB","BID","CTG","MBB","ACB","TCB","TPB","HDB","SHB","STB","VIB","VPB","MSB","ABB","BVB","LPB"]},{name:"DAU KHI",syms:["GAS","PVD","PVS","BSR","OIL","PVB","PVC","PLX","PET","PVT"]}]},
{groups:[{name:"CHUNG KHOAN",syms:["SSI","VND","CTS","FTS","HCM","MBS","DSE","BSI","SHS","VCI","VCK","ORS"]},{name:"XAY DUNG",syms:["C47","C32","L14","CII","CTD","CTI","FCN","HBC","HUT","LCG","PC1","DPG","PHC","VCG"]}]},
{groups:[{name:"BAT DONG SAN",syms:["VHM","AGG","IJC","LDG","CEO","D2D","DIG","DXG","HDC","HDG","KDH","NLG","NTL","NVL","PDR","SCR","TIG","KBC","SZC"]},{name:"PHAN BON",syms:["BFC","DCM","DPM"]},{name:"THEP",syms:["HPG","HSG","NKG"]}]},
{groups:[{name:"BAN LE",syms:["MSN","FPT","FRT","MWG","PNJ","DGW"]},{name:"THUY SAN",syms:["ANV","FMC","CMX","VHC","IDI"]},{name:"CANG BIEN",syms:["HAH","GMD","SGP","VSC"]},{name:"CAO SU",syms:["GVR","DPR","DRI","PHR","DRC"]},{name:"NHUA",syms:["AAA","BMP","NTP"]}]},
{groups:[{name:"DIEN NUOC",syms:["NT2","PC1","GEG","GEX","POW","TDM","BWE"]},{name:"DET MAY",syms:["TCM","TNG","VGT","MSH"]},{name:"HANG KHONG",syms:["NCT","ACV","AST","HVN","SCS","VJC"]},{name:"BAO HIEM",syms:["BMI","MIG","BVH"]},{name:"MIA DUONG",syms:["LSS","SBT","QNS"]}]},
{groups:[{name:"DAU TU CONG",syms:["FCN","HHV","LCG","VCG","C4G","CTD","HBC","HSG","NKG","HPG","KSB","PLC"]}]},
];
const TS_POOL=["AAA","ACB","AGG","ANV","BCG","BFC","BID","BMI","BSR","BVB","BVH","BWE","CII","CKG","CRE","CTD","CTG","CTI","CTR","CTS","D2D","DBC","DCM","DSE","DGW","DIG","DPG","DPM","DRC","DRH","DXG","FCN","FMC","FPT","FRT","FTS","GAS","GEG","GEX","GMD","GVR","HAG","HAX","HBC","HCM","HDB","HDC","VCK","HDG","HNG","HPG","HSG","HTN","HVN","IDC","IJC","KBC","KDH","KSB","LCG","LDG","LPB","LTG","MBB","MBS","MSB","MSN","MWG","NKG","NLG","NTL","NVL","PC1","PDR","PET","PHR","PLC","PLX","PNJ","POW","PTB","PVD","PVS","PVT","QNS","REE","SBT","SCR","SHB","SHS","SSI","STB","SZC","TCB","TDM","TIG","TNG","TPB","TV2","VCB","VCI","VCS","VGT","VHC","VHM","VIB","VIC","VJC","VNM","VPB","VRE"];
function cellStyle(pct){
let r,g,b;
if(pct>=6.5){r=250;g=170;b=225}
else if(pct>=4.0){r=160;g=220;b=170}
else if(pct>=2.0){r=195;g=235;b=200}
else if(pct>0.0){r=225;g=245;b=228}
else if(pct===0){r=245;g=245;b=200}
else if(pct>=-2.0){r=255;g=220;b=210}
else if(pct>=-4.0){r=250;g=185;b=175}
else if(pct>=-6.5){r=240;g=150;b=145}
else{r=175;g=250;b=255}
const lum=.299*r+.587*g+.114*b;
return{bg:`rgb(${r},${g},${b})`,fg:lum>160?'rgb(30,30,30)':'rgb(15,15,15)'};
}
function avgPct(syms,data){const v=syms.filter(s=>data[s]).map(s=>data[s].pct||0);return v.length?v.reduce((a,b)=>a+b,0)/v.length:0;}
function sortDesc(syms,data){return[...syms].sort((a,b)=>((data[b]||{}).pct||0)-((data[a]||{}).pct||0));}
function fmtP(p){if(!p||p<=0)return'—';return p<100?p.toFixed(2):p.toFixed(1);}
function mkCell(sym,data){
const d=data[sym]||{};const pct=typeof d.pct==='number'?d.pct:0;const price=typeof d.price==='number'?d.price:0;
const{bg,fg}=cellStyle(pct);const sign=pct>=0?'+':'';
return`<div class="hmap-cell" style="background:${bg};color:${fg};" onclick="openChart('${sym}')" title="${sym} | ${fmtP(price)} | ${sign}${pct.toFixed(2)}%"><span class="hc-sym">${sym}</span><span class="hc-price">${fmtP(price)}</span><span class="hc-pct">${sign}${pct.toFixed(1)}%</span></div>`;
}
function mkGroup(name,syms,data){
const sorted=sortDesc(syms,data);const avg=avgPct(syms,data);const sign=avg>=0?'+':'';const cls=avg>0.05?'pos':avg<-0.05?'neg':'zer';
return`<div class="hmap-group"><div class="hmap-ghdr"><span class="hmap-gname">${name}</span><span class="hmap-gavg ${cls}">${sign}${avg.toFixed(1)}%</span></div>${sorted.map(s=>mkCell(s,data)).join('')}</div>`;
}
function renderHeatmap(data){
const grid=document.getElementById('hmap-grid');
if(!data||!Object.keys(data).length){grid.innerHTML='<div class="empty"><div class="big"> 🗺 </div><div>Chưa có dữ liệu</div></div>';return;}
const maxRows=Math.max(...HMAP_COLS.map(cd=>cd.groups.reduce((s,g)=>s+g.syms.length,0)));
const tsSyms=TS_POOL.filter(s=>data[s]!==undefined).sort((a,b)=>((data[b]||{}).pct||0)-((data[a]||{}).pct||0)).slice(0,maxRows);
const col0=`<div class="hmap-col">${mkGroup('TRADING STOCKS',tsSyms,data)}</div>`;
const rest=HMAP_COLS.map(cd=>`<div class="hmap-col">${cd.groups.map(g=>mkGroup(g.name,g.syms,data)).join('')}</div>`).join('');
grid.innerHTML=col0+rest;
}

// ═══════════════════════════════════════════════════════
// TRADINGVIEW LIGHTWEIGHT CHART
// ═══════════════════════════════════════════════════════
let _tvChart=null, _tvMainSeries=null, _tvMacdChart=null;
let _tvHistSeries=null, _tvMacdLine=null, _tvSignalLine=null;
let _tvVolSeries=null;
let _tvLastSym='', _tvTF='D';

// Màu đúng theo ảnh mẫu
const TV_COLORS={
  ema10:  '#26a69a',   // xanh lá
  ema20:  '#ef5350',   // đỏ
  ema50:  '#9c27b0',   // tím
  ma200:  '#795548',   // nâu đỏ
  macdLine:   '#1565c0',
  macdSignal: '#f57c00',
  macdHistUp: 'rgba(38,166,154,0.65)',
  macdHistDn: 'rgba(239,83,80,0.65)',
  volUp:  'rgba(38,166,154,0.5)',
  volDn:  'rgba(239,83,80,0.5)',
};

function _tvDestroy(){
  if(_tvChart)    {try{_tvChart.remove();}   catch(e){} _tvChart=null;}
  if(_tvMacdChart){try{_tvMacdChart.remove();}catch(e){} _tvMacdChart=null;}
  _tvMainSeries=null;_tvVolSeries=null;
  _tvHistSeries=null;_tvMacdLine=null;_tvSignalLine=null;
}

function _tvResize(){
  const mainEl=document.getElementById('tv-chart-container');
  const macdEl=document.getElementById('tv-macd-container');
  if(mainEl&&_tvChart)     _tvChart.applyOptions({width:mainEl.clientWidth,height:mainEl.clientHeight});
  if(macdEl&&_tvMacdChart) _tvMacdChart.applyOptions({width:macdEl.clientWidth,height:macdEl.clientHeight});
}
const _tvResizeObs=new ResizeObserver(()=>_tvResize());

// Resample daily → weekly (lấy nến tuần từ dữ liệu ngày)
function _toWeekly(candles){
  if(!candles||!candles.length) return candles;
  const weeks={};
  candles.forEach(c=>{
    const d=new Date(c.time*1000);
    // ISO week start = Monday
    const day=d.getUTCDay()||7; // 1=Mon..7=Sun
    const monday=new Date(d);monday.setUTCDate(d.getUTCDate()-(day-1));monday.setUTCHours(0,0,0,0);
    const key=Math.floor(monday.getTime()/1000);
    if(!weeks[key]){weeks[key]={time:key,open:c.open,high:c.high,low:c.low,close:c.close};}
    else{
      weeks[key].high=Math.max(weeks[key].high,c.high);
      weeks[key].low=Math.min(weeks[key].low,c.low);
      weeks[key].close=c.close;
    }
  });
  return Object.values(weeks).sort((a,b)=>a.time-b.time);
}
function _toWeeklyLine(arr){
  if(!arr||!arr.length) return arr;
  const weeks={};
  arr.forEach(p=>{
    const d=new Date(p.time*1000);const day=d.getUTCDay()||7;
    const monday=new Date(d);monday.setUTCDate(d.getUTCDate()-(day-1));monday.setUTCHours(0,0,0,0);
    const key=Math.floor(monday.getTime()/1000);
    weeks[key]={time:key,value:p.value};
  });
  return Object.values(weeks).sort((a,b)=>a.time-b.time);
}
function _toWeeklyHist(arr){
  if(!arr||!arr.length) return arr;
  const weeks={};
  arr.forEach(p=>{
    const d=new Date(p.time*1000);const day=d.getUTCDay()||7;
    const monday=new Date(d);monday.setUTCDate(d.getUTCDate()-(day-1));monday.setUTCHours(0,0,0,0);
    const key=Math.floor(monday.getTime()/1000);
    weeks[key]={time:key,value:p.value,color:p.color};
  });
  return Object.values(weeks).sort((a,b)=>a.time-b.time);
}
function _toWeeklyVol(arr){
  if(!arr||!arr.length) return arr;
  const weeks={};
  arr.forEach(p=>{
    const d=new Date(p.time*1000);const day=d.getUTCDay()||7;
    const monday=new Date(d);monday.setUTCDate(d.getUTCDate()-(day-1));monday.setUTCHours(0,0,0,0);
    const key=Math.floor(monday.getTime()/1000);
    if(!weeks[key]) weeks[key]={time:key,value:0,color:p.color};
    else { weeks[key].value+=p.value; weeks[key].color=p.color; }
  });
  return Object.values(weeks).sort((a,b)=>a.time-b.time);
}

function tvSetTF(tf){
  _tvTF=tf;
  document.querySelectorAll('.tf-btn').forEach(b=>b.classList.toggle('tf-on',b.id==='tf-btn-'+tf));
  if(_tvLastSym) _tvBuildChart(_tvLastSym, window._tvRawData);
}

// Cache raw data để switch TF không gọi API lại
window._tvRawData=null;

async function loadTVChart(sym){
  sym=sym.toUpperCase().trim();
  _tvLastSym=sym;
  window._tvRawData=null;
  const loading=document.getElementById('tv-loading');
  const wrap=document.getElementById('tv-chart-wrap');
  loading.style.display='flex'; wrap.style.display='none';
  loading.innerHTML=`⏳ Đang tải dữ liệu <b>${sym}</b>...`;
  document.getElementById('tv-sym-label').textContent=sym;

  try{
    const r=await fetch(`/api/ohlcv/${sym}`);
    if(!r.ok){const j=await r.json().catch(()=>({}));throw new Error(j.error||`HTTP ${r.status}`);}
    const d=await r.json();
    if(!d.candles||d.candles.length<2) throw new Error('Không đủ dữ liệu');
    window._tvRawData=d;
    _tvBuildChart(sym,d);
  }catch(err){
    loading.innerHTML=`<div style="text-align:center;color:#9ca3af;padding:24px">
      <div style="font-size:22px;margin-bottom:8px">⚠️</div>
      <div style="margin-bottom:6px">Không tải được dữ liệu <b style="color:#1a56db">${sym}</b></div>
      <div style="font-size:11px;color:#6b7280;margin-bottom:14px">${err.message}</div>
      <button onclick="loadTVChart('${sym}')"
        style="padding:6px 16px;border-radius:5px;background:#1a56db;color:#fff;border:none;cursor:pointer;font-size:12px">
        🔄 Thử lại
      </button>
    </div>`;
  }
}

function _tvBuildChart(sym, d){
  const loading=document.getElementById('tv-loading');
  const wrap=document.getElementById('tv-chart-wrap');
  const isWeekly=(_tvTF==='W');

  // Chuyển đổi dữ liệu theo TF
  const candles = isWeekly ? _toWeekly(d.candles) : d.candles;
  const ema10   = isWeekly ? _toWeeklyLine(d.ema10)   : d.ema10;
  const ema20   = isWeekly ? _toWeeklyLine(d.ema20)   : d.ema20;
  const ema50   = isWeekly ? _toWeeklyLine(d.ema50)   : d.ema50;
  const ma200   = isWeekly ? _toWeeklyLine(d.ma200)   : d.ma200;
  const volume  = isWeekly ? _toWeeklyVol(d.volume)   : d.volume;
  const macdLine   = isWeekly ? _toWeeklyLine(d.macd_line)   : d.macd_line;
  const macdSignal = isWeekly ? _toWeeklyLine(d.macd_signal) : d.macd_signal;
  const macdHist   = isWeekly ? _toWeeklyHist(d.macd_hist)   : d.macd_hist;

  _tvDestroy();

  const mainEl=document.getElementById('tv-chart-container');
  const macdEl=document.getElementById('tv-macd-container');

  // ── CHART CHÍNH ──
  _tvChart=LightweightCharts.createChart(mainEl,{
    width:mainEl.clientWidth,
    height:mainEl.clientHeight,
    layout:{
      background:{type:'solid',color:'#ffffff'},
      textColor:'#9ca3af',
      fontSize:10,
      fontFamily:"'IBM Plex Mono',monospace",
    },
    grid:{
      vertLines:{color:'#f5f5f5',style:0},
      horzLines:{color:'#f5f5f5',style:0},
    },
    crosshair:{
      mode:LightweightCharts.CrosshairMode.Normal,
      vertLine:{color:'#bbbbbb',width:1,style:2,labelBackgroundColor:'#374151'},
      horzLine:{color:'#bbbbbb',width:1,style:2,labelBackgroundColor:'#374151'},
    },
    rightPriceScale:{
      borderColor:'#eeeeee',
      scaleMarginTop:0.06,
      scaleMarginBottom:0.22,  // chừa chỗ cho volume phía dưới
      textColor:'#9ca3af',
    },
    timeScale:{
      borderColor:'#eeeeee',
      rightOffset:8,
      fixLeftEdge:false,
      fixRightEdge:false,
      timeVisible:true,
      secondsVisible:false,
      tickMarkFormatter:(time)=>{
        const d=new Date(time*1000);
        const dd=String(d.getUTCDate()).padStart(2,'0');
        const mm=String(d.getUTCMonth()+1).padStart(2,'0');
        return `${dd}/${mm}`;
      },
    },
    handleScroll:true,
    handleScale:true,
  });

  // Candlestick — nến nhỏ gọn
  _tvMainSeries=_tvChart.addCandlestickSeries({
    upColor:'#26a69a', downColor:'#ef5350',
    borderUpColor:'#26a69a', borderDownColor:'#ef5350',
    wickUpColor:'#26a69a', wickDownColor:'#ef5350',
    priceLineVisible:false,
    lastValueVisible:true,
  });
  _tvMainSeries.setData(candles);

  // Volume histogram (price scale riêng, nằm dưới)
  _tvVolSeries=_tvChart.addHistogramSeries({
    priceFormat:{type:'volume'},
    priceScaleId:'vol',
    lastValueVisible:false,
    priceLineVisible:false,
  });
  _tvChart.priceScale('vol').applyOptions({
    scaleMarginTop:0.84,
    scaleMarginBottom:0.0,
    drawTicks:false,
    borderVisible:false,
    visible:false,
  });
  _tvVolSeries.setData(volume||[]);

  // EMA10 — xanh lá
  if(ema10&&ema10.length){
    const s=_tvChart.addLineSeries({color:TV_COLORS.ema10,lineWidth:1,priceLineVisible:false,lastValueVisible:false,crosshairMarkerVisible:false});
    s.setData(ema10);
  }
  // EMA20 — đỏ
  if(ema20&&ema20.length){
    const s=_tvChart.addLineSeries({color:TV_COLORS.ema20,lineWidth:1,priceLineVisible:false,lastValueVisible:false,crosshairMarkerVisible:false});
    s.setData(ema20);
  }
  // EMA50 — tím
  if(ema50&&ema50.length){
    const s=_tvChart.addLineSeries({color:TV_COLORS.ema50,lineWidth:1.5,priceLineVisible:false,lastValueVisible:false,crosshairMarkerVisible:false});
    s.setData(ema50);
  }
  // MA200 — nâu đỏ, dashed
  if(ma200&&ma200.length){
    const s=_tvChart.addLineSeries({
      color:TV_COLORS.ma200,lineWidth:1.5,
      lineStyle:LightweightCharts.LineStyle.Dashed,
      priceLineVisible:false,lastValueVisible:false,crosshairMarkerVisible:false,
    });
    s.setData(ma200);
  }

  // ── MACD CHART ──
  _tvMacdChart=LightweightCharts.createChart(macdEl,{
    width:macdEl.clientWidth,
    height:macdEl.clientHeight,
    layout:{background:{type:'solid',color:'#ffffff'},textColor:'#9ca3af',fontSize:9,fontFamily:"'IBM Plex Mono',monospace"},
    grid:{vertLines:{color:'#f8f8f8'},horzLines:{color:'#f8f8f8'}},
    crosshair:{
      mode:LightweightCharts.CrosshairMode.Normal,
      vertLine:{color:'#cccccc',width:1,style:2,labelVisible:false},
      horzLine:{color:'#cccccc',width:1,style:2,labelVisible:false},
    },
    rightPriceScale:{borderColor:'#eeeeee',scaleMarginTop:0.1,scaleMarginBottom:0.1,textColor:'#9ca3af'},
    timeScale:{borderColor:'#eeeeee',rightOffset:8,timeVisible:false,visible:false},
    handleScroll:true,
    handleScale:true,
  });

  _tvHistSeries=_tvMacdChart.addHistogramSeries({
    color:TV_COLORS.macdHistUp,
    priceLineVisible:false,lastValueVisible:false,
  });
  if(macdHist&&macdHist.length) _tvHistSeries.setData(macdHist);

  _tvMacdLine=_tvMacdChart.addLineSeries({color:TV_COLORS.macdLine,lineWidth:1.5,priceLineVisible:false,lastValueVisible:false,crosshairMarkerVisible:false});
  if(macdLine&&macdLine.length) _tvMacdLine.setData(macdLine);

  _tvSignalLine=_tvMacdChart.addLineSeries({color:TV_COLORS.macdSignal,lineWidth:1,priceLineVisible:false,lastValueVisible:false,crosshairMarkerVisible:false});
  if(macdSignal&&macdSignal.length) _tvSignalLine.setData(macdSignal);

  // Sync timescale
  _tvChart.timeScale().subscribeVisibleLogicalRangeChange(range=>{
    if(range&&_tvMacdChart) _tvMacdChart.timeScale().setVisibleLogicalRange(range);
  });
  _tvMacdChart.timeScale().subscribeVisibleLogicalRangeChange(range=>{
    if(range&&_tvChart) _tvChart.timeScale().setVisibleLogicalRange(range);
  });

  // Crosshair sync + legend update
  _tvChart.subscribeCrosshairMove(param=>{
    if(!param||!_tvMacdChart) return;
    if(param.time) _tvMacdChart.setCrosshairPosition(0,0,_tvHistSeries);
    if(!param.time||!param.seriesData) return;
    const c=param.seriesData.get(_tvMainSeries);
    if(c){
      document.getElementById('leg-open').textContent=c.open?.toFixed(2)||'—';
      document.getElementById('leg-high').textContent=c.high?.toFixed(2)||'—';
      document.getElementById('leg-low').textContent=c.low?.toFixed(2)||'—';
      document.getElementById('leg-close').textContent=c.close?.toFixed(2)||'—';
    }
    // MACD legend
    const mh=_tvHistSeries&&param.seriesData.get(_tvHistSeries);
  });
  _tvMacdChart.subscribeCrosshairMove(param=>{
    if(!param||!param.time||!param.seriesData) return;
    const ml=param.seriesData.get(_tvMacdLine);
    const ms=param.seriesData.get(_tvSignalLine);
    const mh=param.seriesData.get(_tvHistSeries);
    if(ml) document.getElementById('leg-macd').textContent=ml.value?.toFixed(4)||'—';
    if(ms) document.getElementById('leg-signal').textContent=ms.value?.toFixed(4)||'—';
    if(mh){
      const hEl=document.getElementById('leg-hist');
      hEl.textContent=mh.value?.toFixed(4)||'—';
      hEl.style.color=mh.value>=0?TV_COLORS.macdHistUp.replace('0.65','1'):TV_COLORS.macdHistDn.replace('0.65','1');
    }
  });

  // Hiển thị giá trị cuối ở legend
  const last=candles[candles.length-1];
  document.getElementById('tv-sym-label').textContent=sym+(isWeekly?' [W]':' [D]');
  document.getElementById('leg-open').textContent=last?.open?.toFixed(2)||'—';
  document.getElementById('leg-high').textContent=last?.high?.toFixed(2)||'—';
  document.getElementById('leg-low').textContent=last?.low?.toFixed(2)||'—';
  document.getElementById('leg-close').textContent=last?.close?.toFixed(2)||'—';
  const lastMacd=macdLine?.at(-1);if(lastMacd) document.getElementById('leg-macd').textContent=lastMacd.value?.toFixed(4)||'—';
  const lastSig=macdSignal?.at(-1);if(lastSig) document.getElementById('leg-signal').textContent=lastSig.value?.toFixed(4)||'—';
  const lastHist=macdHist?.at(-1);
  if(lastHist){
    const hEl=document.getElementById('leg-hist');
    hEl.textContent=lastHist.value?.toFixed(4)||'—';
    hEl.style.color=lastHist.value>=0?TV_COLORS.macdHistUp.replace('0.65','1'):TV_COLORS.macdHistDn.replace('0.65','1');
  }

  // Observer resize
  _tvResizeObs.disconnect();
  _tvResizeObs.observe(mainEl);
  _tvResizeObs.observe(macdEl);

  loading.style.display='none';
  wrap.style.display='flex';
}

// Inline search trên TV Chart
(function(){
  function _initTVInlineSearch(){
    const inp=document.getElementById('tv-sym-input');
    if(!inp) return;
    inp.addEventListener('keydown',function(e){
      if(e.key==='Enter'){
        const sym=this.value.trim().toUpperCase();
        if(sym.length>=2){ this.value=''; this.blur(); openChart(sym); }
      }
      if(e.key==='Escape'){ this.value=''; this.blur(); }
    });
  }
  // Gọi ngay và cả khi rebuild mobile header
  document.addEventListener('DOMContentLoaded',_initTVInlineSearch);
  setTimeout(_initTVInlineSearch,500);
  window._initTVInlineSearch=_initTVInlineSearch;
})();

// ═══════════════════════════════════════════════════════
// ALBUM STATE
// ═══════════════════════════════════════════════════════
let _albumIdx=0,_albumTotal=0,_albumImages=[];
function _showAlbum(images){
_albumImages=images;const slidesEl=document.getElementById('album-slides');const dotsEl=document.getElementById('album-dots');
slidesEl.innerHTML='';dotsEl.innerHTML='';_albumTotal=images.length;
const isMobile=window.innerWidth<=768;
images.forEach((img,i)=>{
const clickAttr=isMobile?`onclick="lbOpen(_albumImages,${i})" style="cursor:zoom-in"`:'';
slidesEl.innerHTML+=`<div class="album-slide${i===0?' on':''}" id="slide-${i}"><img src="${img.url}" alt="${img.label}" loading="lazy" ${clickAttr}></div>`;
dotsEl.innerHTML+=`<div class="album-dot${i===0?' on':''}" id="dot-${i}" onclick="albumGoto(${i})"></div>`;
});
_albumIdx=0;_updateAlbumNav();
document.getElementById('album-outer').style.display='flex';
document.getElementById('scanner-loading').style.display='none';
}
function albumGoto(i){if(i<0||i>=_albumTotal)return;document.querySelectorAll('.album-slide').forEach((s,idx)=>s.classList.toggle('on',idx===i));document.querySelectorAll('.album-dot').forEach((d,idx)=>d.classList.toggle('on',idx===i));_albumIdx=i;_updateAlbumNav();}
function albumNav(dir){albumGoto(_albumIdx+dir);}
function refreshScannerChart(){if(!_sym)return;const btn=document.getElementById('btn-refresh');if(btn){btn.classList.add('spinning');btn.disabled=true;}loadScannerChartForce(_sym);}
async function loadScannerChartForce(sym){document.getElementById('album-outer').style.display='none';document.getElementById('scanner-loading').style.display='flex';document.getElementById('scanner-loading').innerHTML=`<span> 🔄  Đang làm mới chart <b>${sym}</b>…</span>`;try{await fetch(`/api/chart_cache_clear/${sym}`,{method:'DELETE'}).catch(()=>{});}catch(e){}const btn=document.getElementById('btn-refresh');if(btn){btn.classList.remove('spinning');btn.disabled=false;}await loadScannerChart(sym);}
function _updateAlbumNav(){document.getElementById('btn-prev').classList.toggle('disabled',_albumIdx===0);document.getElementById('btn-next').classList.toggle('disabled',_albumIdx===_albumTotal-1);}
let _touchStartX=0;
document.getElementById('panel-scanner').addEventListener('touchstart',e=>{if(window.innerWidth>768)_touchStartX=e.touches[0].clientX;},{passive:true});
document.getElementById('panel-scanner').addEventListener('touchend',e=>{if(window.innerWidth>768){const dx=e.changedTouches[0].clientX-_touchStartX;if(Math.abs(dx)>50)albumNav(dx<0?1:-1);}},{passive:true});
document.addEventListener('keydown',e=>{const overlayOn=document.getElementById('overlay').classList.contains('on');if(!overlayOn)return;if(document.activeElement===document.getElementById('popup-search-input'))return;if(_tab!=='scanner')return;if(_albumTotal===0)return;if(e.key==='ArrowLeft'){e.preventDefault();albumNav(-1);}if(e.key==='ArrowRight'){e.preventDefault();albumNav(1);}});
async function loadScannerChart(sym){
document.getElementById('album-outer').style.display='none';
document.getElementById('scanner-loading').style.display='flex';
document.getElementById('scanner-loading').innerHTML=`<span> ⏳  Đang tạo chart <b>${sym}</b>… (5–10 giây)</span>`;
try{
const r=await fetch(`/api/chart_images/${sym}`);if(!r.ok){const j=await r.json().catch(()=>({}));throw new Error(j.error||`HTTP ${r.status}`);}
const j=await r.json();
if(j.images&&j.images.length>0){
const labels=j.labels||[' 📊  Daily [D]',' 📈  Weekly [W]',' ⚡  15m'];
_showAlbum(j.images.map((b64,i)=>({url:`data:image/png;base64,${b64}`,label:labels[i]||`Chart ${i+1}`})));
if(j.cached){const hint=document.querySelector('.album-hint');if(hint)hint.textContent=' ♻️  Dùng cache — click lại tab để làm mới';}return;
}
throw new Error('no_images');
}catch(e){
document.getElementById('scanner-loading').innerHTML=`<div style="text-align:center;color:#aaa;padding:24px"><div style="font-size:24px;margin-bottom:10px"> ⚠️ </div><div style="margin-bottom:8px">Không tải được chart <b style="color:#4d9ff5">${sym}</b></div><div style="font-size:11px;color:#666;margin-bottom:16px">${e.message}</div><div style="display:flex;gap:8px;justify-content:center;flex-wrap:wrap"><button onclick="loadScannerChart('${sym}')" style="padding:6px 14px;border-radius:5px;background:#1a56db;color:#fff;border:none;cursor:pointer;font-size:12px"> 🔄  Thử lại</button><a href="https://ta.vietstock.vn/?stockcode=${sym.toLowerCase()}" target="_blank" style="padding:6px 14px;border-radius:5px;background:#374151;color:#fff;text-decoration:none;font-size:12px"> 📈  Stockchart</a></div></div>`;
}
}
// ═══════════════════════════════════════════════════════
// POPUP STATE
// ═══════════════════════════════════════════════════════
let _sym='', _tab='tv';
const IFRAME_TABS=['vs','vnd-cs','vnd-news','vnd-sum','24h','url'];
const ALL_TABS=['tv','vs','scanner','vnd-cs','vnd-news','vnd-sum','24h','url'];
function openUrl(url,label){
_sym='VNINDEX';_tab='url';
document.getElementById('ptitle').textContent=label||' 🌐  Web';
IFRAME_TABS.forEach(t=>{document.getElementById(`iframe-${t}`).src='about:blank';});
document.getElementById('album-outer').style.display='none';
document.getElementById('scanner-loading').style.display='flex';
document.getElementById('scanner-loading').innerHTML='<span> ⏳  Đang tải...</span>';
ALL_TABS.forEach(t=>{
const ct=document.getElementById('ctab-'+t);
if(ct)ct.classList.toggle('on',t==='url');
document.getElementById('panel-'+t).classList.toggle('on',t==='url');
});
document.getElementById('iframe-url').src=url;
document.getElementById('overlay').classList.add('on');
document.body.style.overflow='hidden';
document.getElementById('popup-search-input').value='';
document.getElementById('edge-swipe-zone').classList.add('on');
document.getElementById('mob-close-float').style.display='';
}
function openChart(sym){
_sym=sym.toUpperCase().trim();
_tab='tv';   // MẶC ĐỊNH là tab TV Chart
document.getElementById('ptitle').textContent=` 📊  ${_sym}`;
IFRAME_TABS.forEach(t=>{document.getElementById(`iframe-${t}`).src='about:blank';});
document.getElementById('album-outer').style.display='none';
document.getElementById('scanner-loading').style.display='flex';
document.getElementById('scanner-loading').innerHTML='<span> ⏳  Đang tạo chart từ scanner...</span>';
_activateTab('tv');
document.getElementById('overlay').classList.add('on');
document.body.style.overflow='hidden';
document.getElementById('popup-search-input').value='';
document.getElementById('edge-swipe-zone').classList.add('on');
document.getElementById('mob-close-float').style.display='';
}
function _activateTab(tab){
_tab=tab;
ALL_TABS.forEach(t=>{
const ct=document.getElementById('ctab-'+t);
if(ct)ct.classList.toggle('on',t===tab);
document.getElementById('panel-'+t).classList.toggle('on',t===tab);
});
if(window.innerWidth<=768){
const row=document.getElementById('mob-tabrow');
const activeBtn=document.getElementById('ctab-'+tab);
if(row&&activeBtn){
const BTN_BASE='flex-shrink:0;flex-grow:0;white-space:nowrap;padding:7px 14px;border-radius:6px;border:1px solid var(--border);font-size:12px;font-family:var(--font-mono);font-weight:600;cursor:pointer;background:var(--bg);color:var(--muted);display:inline-flex;align-items:center;justify-content:center;transition:background .15s,color .15s;touch-action:manipulation';
const BTN_ON='flex-shrink:0;flex-grow:0;white-space:nowrap;padding:7px 14px;border-radius:6px;border:1px solid var(--accent);font-size:12px;font-family:var(--font-mono);font-weight:700;cursor:pointer;background:var(--surface);color:var(--accent);display:inline-flex;align-items:center;justify-content:center;box-shadow:0 2px 0 0 var(--accent);touch-action:manipulation';
row.querySelectorAll('button').forEach(btn=>{btn.style.cssText=btn.id==='ctab-'+tab?BTN_ON:BTN_BASE;});
const btnLeft=activeBtn.offsetLeft,btnWidth=activeBtn.offsetWidth,rowWidth=row.offsetWidth;
row.scrollTo({left:btnLeft-(rowWidth/2)+(btnWidth/2),behavior:'smooth'});
}
}
// Lazy load per tab
if(tab==='tv')     loadTVChart(_sym);
if(tab==='vs')     {const f=document.getElementById('iframe-vs');if(f.src==='about:blank')f.src=`https://ta.vietstock.vn/?stockcode=${_sym.toLowerCase()}`;}
if(tab==='vnd-cs') {const f=document.getElementById('iframe-vnd-cs');if(f.src==='about:blank')f.src=`https://dstock.vndirect.com.vn/tong-quan/${_sym}/diem-nhan-co-ban-popup?theme=light`;}
if(tab==='vnd-news'){const f=document.getElementById('iframe-vnd-news');if(f.src==='about:blank')f.src=`https://dstock.vndirect.com.vn/tong-quan/${_sym}/tin-tuc-ma-popup?type=dn&theme=light`;}
if(tab==='vnd-sum') {const f=document.getElementById('iframe-vnd-sum');if(f.src==='about:blank')f.src=`https://dstock.vndirect.com.vn/tong-quan/${_sym}?theme=light`;}
if(tab==='24h')    {const f=document.getElementById('iframe-24h');if(f.src==='about:blank')f.src=`https://24hmoney.vn/stock/${_sym}/news`;}
if(tab==='scanner') loadScannerChart(_sym);
}
function switchTab(tab){_activateTab(tab);}
function closePopup(){
_tvDestroy();
const overlay=document.getElementById('overlay');
const pbox=overlay.querySelector('.pbox');
pbox.style.visibility='hidden';
IFRAME_TABS.forEach(t=>{document.getElementById(`iframe-${t}`).src='about:blank';});
pbox.style.animation='none';
overlay.classList.remove('on');
document.body.style.overflow='';
document.getElementById('edge-swipe-zone').classList.remove('on');
document.getElementById('mob-close-float').style.display='none';
requestAnimationFrame(()=>{pbox.style.visibility='';pbox.style.animation='';});
}
document.getElementById('overlay').addEventListener('click',e=>{if(e.target===document.getElementById('overlay'))closePopup();});
// Mobile swipe to close
(function(){
if(window.innerWidth>768)return;
const pbox=document.querySelector('.pbox');
let startX=0,startY=0,dir='',fired=false;
pbox.addEventListener('touchstart',function(e){if(!document.getElementById('overlay').classList.contains('on'))return;if(lb.el&&lb.el.classList.contains('on'))return;if(e.touches[0].clientX>40)return;startX=e.touches[0].clientX;startY=e.touches[0].clientY;dir='';fired=false;},{passive:true});
pbox.addEventListener('touchmove',function(e){if(fired)return;const dx=e.touches[0].clientX-startX,dy=e.touches[0].clientY-startY;if(!dir&&(Math.abs(dx)>10||Math.abs(dy)>10))dir=Math.abs(dx)>Math.abs(dy)?'h':'v';if(dir==='h'&&dx>40){fired=true;closePopup();}},{passive:true});
})();
document.addEventListener('keydown',e=>{if(lb.el&&lb.el.classList.contains('on'))return;if(e.key==='Escape'){if(document.activeElement===document.getElementById('popup-search-input')){document.getElementById('popup-search-input').blur();return;}closePopup();}});
// ═══════════════════════════════════════════════════════
// CLOCK + BADGE + FETCH
// ═══════════════════════════════════════════════════════
function tick(){const n=new Date();document.getElementById('clock').textContent=n.toLocaleTimeString('vi-VN',{hour12:false})+'  '+n.toLocaleDateString('vi-VN');}
setInterval(tick,1000);tick();
function badgeCls(sig){return({'BREAKOUT':'b-BREAKOUT','POCKET PIVOT':'b-POCKET','PRE-BREAK':'b-PREBREAK','BOTTOMBREAKP':'b-BBREAKP','BOTTOMFISH':'b-BFISH','MA_CROSS':'b-MACROSS'})[sig]||'b-MACROSS';}
async function fetchSigs(){
try{
const j=await fetch('/api/signals').then(r=>r.json());
document.getElementById('sig-meta').textContent=`Cập nhật ${j.updated_at}  •  ${j.count} tín hiệu  •  click để xem chart`;
const el=document.getElementById('sig-list');
if(!j.signals.length){el.innerHTML='<div class="empty"><div class="big"> 💤 </div><div>Chưa có tín hiệu nào hôm nay</div></div>';return;}
el.innerHTML=j.signals.map(s=>`<div class="sig-row" onclick="openChart('${s.symbol}')"><span class="s-emoji">${s.emoji}</span><span class="s-sym">${s.symbol}</span><span class="s-type" style="font-weight:600;color:${s.pct>=0?'#0e9f6e':'#e02424'}">${s.pct!=null?(s.pct>=0?'+':'')+s.pct+'%':'—'}</span><span class="s-badge ${badgeCls(s.signal)}">${s.signal.replace('POCKET PIVOT','PIVOT').replace('PRE-BREAK','PRE')}</span></div>`).join('');
}catch(e){console.error('fetchSigs:',e)}
}
async function fetchHmap(){
try{
const j=await fetch('/api/heatmap').then(r=>r.json());
const now=new Date().toLocaleTimeString('vi-VN',{hour12:false});
const isMob=window.innerWidth<=768;
document.getElementById('hmap-ts').textContent=isMob?`Data: ${j.timestamp||'--'}  •  Cập nhật: ${now}`:`Data: ${j.timestamp||'--'}  •  Cập nhật: ${now}  •  click để xem chart`;
renderHeatmap(j.data||{});
}catch(e){console.error('fetchHmap:',e)}
}
function startBar(id,sec){const el=document.getElementById(id);if(!el)return;el.style.transition='none';el.style.width='0%';requestAnimationFrame(()=>requestAnimationFrame(()=>{el.style.transition=`width ${sec}s linear`;el.style.width='100%';}));}
// ═══════════════════════════════════════════════════════
// MOBILE HEADER REBUILD
// ═══════════════════════════════════════════════════════
function buildMobileHeader(){
if(window.innerWidth>768)return;
const phdr=document.querySelector('.phdr');
if(!phdr||phdr.dataset.mob==='1')return;
phdr.dataset.mob='1';phdr.innerHTML='';
phdr.style.cssText='display:flex;flex-direction:column;flex-shrink:0;';
const r1=document.createElement('div');
r1.style.cssText=['display:flex','align-items:center','gap:8px','padding:8px 12px','background:var(--surf2)','border-bottom:1px solid var(--border)'].join(';');
r1.innerHTML=`<span id="ptitle" style="font-family:var(--font-ui);font-size:17px;font-weight:800;color:var(--accent);letter-spacing:1px;flex-shrink:0;">Chart</span><div style="position:relative;flex:1;min-width:0;"><span style="position:absolute;left:9px;top:50%;transform:translateY(-50%);color:var(--muted);font-size:12px;pointer-events:none;"> 🔍 </span><input id="popup-search-input" type="text" placeholder="Tìm mã..." maxlength="10" autocomplete="off" spellcheck="false" style="width:100%;padding:6px 10px 6px 28px;border-radius:20px;border:1px solid var(--border);background:var(--surface);color:var(--text);font-family:var(--font-mono);font-size:12px;outline:none;"></div>`;
phdr.appendChild(r1);
const r2=document.createElement('div');
r2.id='mob-tabrow';
r2.style.cssText=['display:flex','flex-direction:row','flex-wrap:nowrap','align-items:center','overflow-x:scroll','overflow-y:hidden','-webkit-overflow-scrolling:touch','overscroll-behavior-x:contain','padding:6px 8px','gap:5px','background:var(--surf2)','border-bottom:1px solid var(--border)','scrollbar-width:none','-ms-overflow-style:none','width:100%','box-sizing:border-box','min-height:44px'].join(';');
const tabs=[
{id:'tv',label:' 📊  TV Chart'},  // TAB ĐẦU TIÊN
{id:'vs',label:' 📈  Vietstock'},
{id:'scanner',label:' 🖼  Scanner'},
{id:'vnd-cs',label:' ⚖️  Cơ bản'},
{id:'vnd-news',label:' 🗞️  Tin tức'},
{id:'vnd-sum',label:' 📄  Tổng quan'},
{id:'24h',label:' 💬  24HMoney'},
];
const BTN_BASE=['flex-shrink:0','flex-grow:0','white-space:nowrap','padding:7px 14px','border-radius:6px','border:1px solid var(--border)','font-size:12px','font-family:var(--font-mono)','font-weight:600','cursor:pointer','background:var(--bg)','color:var(--muted)','display:inline-flex','align-items:center','justify-content:center','transition:background .15s,color .15s','touch-action:manipulation'].join(';');
const BTN_ON=['flex-shrink:0','flex-grow:0','white-space:nowrap','padding:7px 14px','border-radius:6px','border:1px solid var(--accent)','font-size:12px','font-family:var(--font-mono)','font-weight:700','cursor:pointer','background:var(--surface)','color:var(--accent)','display:inline-flex','align-items:center','justify-content:center','box-shadow:0 2px 0 0 var(--accent)','touch-action:manipulation'].join(';');
tabs.forEach(t=>{
const btn=document.createElement('button');
btn.id='ctab-'+t.id;btn.textContent=t.label;
btn.style.cssText=(t.id==='tv')?BTN_ON:BTN_BASE;
if(t.id==='tv')btn.classList.add('on');
btn.onclick=()=>switchTab(t.id);
r2.appendChild(btn);
});
phdr.appendChild(r2);
const inp=document.getElementById('popup-search-input');
inp.addEventListener('keydown',function(e){
if(e.key==='Enter'){const sym=this.value.trim().toUpperCase();if(sym.length>=2){this.value='';this.blur();openChart(sym);}}
if(e.key==='Escape')this.blur();
});
inp.addEventListener('focus',function(){this.select();});
}
const _openChartOrig=openChart;
openChart=function(sym){buildMobileHeader();_openChartOrig(sym);};
const _openUrlOrig=openUrl;
openUrl=function(url,label){buildMobileHeader();_openUrlOrig(url,label);};
// ═══════════════════════════════════════════════════════
// INIT
// ═══════════════════════════════════════════════════════
async function init(){
await loadConfig();
startBar('pbar-sig',SIG_TTL);startBar('pbar-hmap',HMAP_TTL);
await Promise.all([fetchSigs(),fetchHmap()]);
setInterval(async()=>{startBar('pbar-sig',SIG_TTL);await fetchSigs();},SIG_TTL*1000);
setInterval(async()=>{startBar('pbar-hmap',HMAP_TTL);await fetchHmap();},HMAP_TTL*1000);
}
init();
</script>
</body>
</html>
"""
