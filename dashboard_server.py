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

from flask import Flask, jsonify, Response, send_from_directory
import threading
import time
import os
from datetime import datetime
import pytz

TZ_VN = pytz.timezone('Asia/Ho_Chi_Minh')
app   = Flask(__name__)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# ---------------------------------------------------------------------------
# Module-level references (set by start_dashboard)
# ---------------------------------------------------------------------------
_get_alerted_today = None
_get_history_cache = None
_cache_lock        = None
_fetch_heatmap_fn  = None
_fetch_chart_fn    = None   # dashboard_chart_fn từ scanner_full.py (Scanner Chart tab)
_signal_emoji      = {}
_signal_rank       = {}

# Heatmap cache
_heatmap_cache  = {"data": {}, "ts": "", "updated_at": 0}
_heatmap_lock   = threading.Lock()
HEATMAP_TTL_SEC = 120

# Scanner Chart image cache (giữ lại cho Scanner Chart tab)
_chart_cache: dict = {}
_chart_lock        = threading.Lock()
CHART_TTL_SEC      = 0   # 0 = không cache, luôn fetch mới

# Signal auto-refresh interval
SIGNAL_TTL_SEC = 10

# vnstock source (phải khớp với scanner_full.py)
DATA_SOURCE = 'KBS'

# =============================================================================
# STATIC FILES
# =============================================================================

@app.route("/static/<path:filename>")
def serve_static(filename):
    return send_from_directory(STATIC_DIR, filename)

# =============================================================================
# HELPER: BUILD OHLCV RESPONSE JSON
# Dùng chung cho Daily, Weekly, 15m từ bất kỳ DataFrame nào.
# =============================================================================

def _build_ohlcv_json(symbol: str, df):
    """
    Nhận một DataFrame với cột open/high/low/close/volume (index là DatetimeIndex),
    tính EMA/MACD và trả về flask.Response chứa JSON.
    """
    import pandas as pd
    import numpy as np

    def ema(s, span):
        return s.ewm(span=span, adjust=False).mean()

    close  = df['close'].astype(float)
    highs  = df['high'].astype(float)  if 'high'   in df.columns else close
    lows   = df['low'].astype(float)   if 'low'    in df.columns else close
    opens  = df['open'].astype(float)  if 'open'   in df.columns else close
    volume = df['volume'].astype(float) if 'volume' in df.columns else pd.Series([0]*len(close), index=close.index)

    ema10  = ema(close, 10)
    ema20  = ema(close, 20)
    ema50  = ema(close, 50)
    ma200  = close.rolling(200).mean()
    ema30  = ema(close, 30)
    ema100 = ema(close, 100)
    ema200 = ema(close, 200)

    ema12       = ema(close, 12)
    ema26       = ema(close, 26)
    macd_line   = ema12 - ema26
    macd_signal = ema(macd_line, 9)
    macd_hist   = macd_line - macd_signal

    def ts(idx):
        if hasattr(idx, 'timestamp'):
            return int(idx.timestamp())
        try:
            return int(pd.Timestamp(idx).timestamp())
        except Exception:
            return 0

    def series_to_list(s):
        return [{"time": ts(i), "value": round(float(v), 4)}
                for i, v in s.items() if not pd.isna(v)]

    idx_list = list(df.index)
    candles  = []
    for i in idx_list:
        t = ts(i)
        try:
            candles.append({
                "time":  t,
                "open":  round(float(opens[i]),  2),
                "high":  round(float(highs[i]),  2),
                "low":   round(float(lows[i]),   2),
                "close": round(float(close[i]),  2),
            })
        except Exception:
            pass

    vols = []
    for pos, i in enumerate(idx_list):
        try:
            color = ("rgba(14,159,110,0.5)"
                     if close.iloc[pos] >= opens.iloc[pos]
                     else "rgba(224,36,36,0.5)")
            vols.append({"time": ts(i), "value": round(float(volume[i]), 0), "color": color})
        except Exception:
            pass

    macd_hist_list = [
        {
            "time": ts(i),
            "value": round(float(v), 4),
            "color": ("rgba(14,159,110,0.7)" if v >= 0 else "rgba(224,36,36,0.7)"),
        }
        for i, v in macd_hist.items() if not pd.isna(v)
    ]

    return jsonify({
        "symbol":      symbol,
        "candles":     candles,
        "volume":      vols,
        "ema10":       series_to_list(ema10),
        "ema20":       series_to_list(ema20),
        "ema50":       series_to_list(ema50),
        "ma200":       series_to_list(ma200),
        "ema30":       series_to_list(ema30),
        "ema100":      series_to_list(ema100),
        "ema200":      series_to_list(ema200),
        "macd_line":   series_to_list(macd_line),
        "macd_signal": series_to_list(macd_signal),
        "macd_hist":   macd_hist_list,
        "updated_at":  datetime.now(TZ_VN).strftime("%H:%M:%S"),
    })

# =============================================================================
# HELPER: FETCH FRESH DATA (Daily/Weekly/15m) — không dùng history_cache
# Tái sử dụng logic từ scanner_full.py: fetch_fresh_for_chart / fetch_intraday_15m
# =============================================================================

def _fetch_daily_fresh(symbol: str):
    """
    Fetch dữ liệu daily mới nhất từ server (giống fetch_fresh_for_chart).
    Trả về DataFrame hoặc None.
    """
    import pandas as pd
    import numpy as np

    current_date = datetime.now(TZ_VN).date()
    for attempt in range(3):
        try:
            from vnstock import Quote
            quote  = Quote(symbol=symbol, source=DATA_SOURCE)
            df_raw = quote.history(length='1000', interval='1D')
            if df_raw is None or len(df_raw) < 60:
                return None
            df_raw['time'] = pd.to_datetime(df_raw['time'])
            df_raw.set_index('time', inplace=True)
            df_raw.columns = [c.lower() for c in df_raw.columns]
            df_raw = df_raw[['open', 'high', 'low', 'close', 'volume']].copy()

            # Bỏ nến hôm nay nếu volume < 100 (chưa giao dịch)
            today_rows = df_raw[df_raw.index.date == current_date]
            if not today_rows.empty:
                vol_today = float(today_rows.iloc[-1].get('volume', 0) or 0)
                if vol_today < 100:
                    df_raw = df_raw[df_raw.index.date < current_date]

            if len(df_raw) < 60:
                return None
            return df_raw
        except Exception as e:
            if attempt < 2:
                time.sleep(2)
            else:
                print(f"  [Dashboard] ❌ _fetch_daily_fresh {symbol}: {e}")
    return None


def _fetch_weekly_fresh(symbol: str):
    """
    Fetch daily rồi resample sang tuần.
    Trả về DataFrame tuần hoặc None.
    """
    import pandas as pd

    df_daily = _fetch_daily_fresh(symbol)
    if df_daily is None or len(df_daily) < 10:
        return None
    df_w = df_daily[['open', 'high', 'low', 'close', 'volume']].resample('W-FRI').agg({
        'open':   'first',
        'high':   'max',
        'low':    'min',
        'close':  'last',
        'volume': 'sum',
    }).dropna()
    return df_w if len(df_w) >= 2 else None


def _fetch_15m_fresh(symbol: str):
    """
    Fetch dữ liệu 15 phút mới nhất (giống fetch_intraday_15m).
    Trả về DataFrame hoặc None.
    """
    import pandas as pd
    import numpy as np

    for attempt in range(3):
        try:
            from vnstock import Quote
            quote  = Quote(symbol=symbol, source=DATA_SOURCE)
            df_raw = quote.history(length='200', interval='15m')
            if df_raw is None or df_raw.empty:
                return None
            df_raw['time'] = pd.to_datetime(df_raw['time'])
            df_raw.set_index('time', inplace=True)
            df_raw.columns = [c.lower() for c in df_raw.columns]
            for col in ['open', 'high', 'low', 'close']:
                if col not in df_raw.columns:
                    df_raw[col] = np.nan
            if 'volume' not in df_raw.columns:
                df_raw['volume'] = 0
            df_raw = df_raw[['open', 'high', 'low', 'close', 'volume']].dropna(subset=['close'])
            return df_raw if len(df_raw) >= 5 else None
        except Exception as e:
            if attempt < 2:
                time.sleep(2)
            else:
                print(f"  [Dashboard] ❌ _fetch_15m_fresh {symbol}: {e}")
    return None


def _fetch_index_history(symbol: str):
    """
    Fetch lịch sử chỉ số (VNINDEX, VN30, ...).
    Trả về DataFrame hoặc None.
    """
    import pandas as pd
    import numpy as np

    for attempt in range(3):
        try:
            from vnstock import Quote
            quote  = Quote(symbol=symbol, source=DATA_SOURCE)
            df_raw = quote.history(length='1000', interval='1D')
            if df_raw is None or df_raw.empty:
                return None
            df_raw['time'] = pd.to_datetime(df_raw['time'])
            df_raw.set_index('time', inplace=True)
            df_raw.columns = [c.lower() for c in df_raw.columns]
            for col in ['open', 'high', 'low', 'close']:
                if col not in df_raw.columns:
                    df_raw[col] = float('nan')
            if 'volume' not in df_raw.columns:
                df_raw['volume'] = 0
            df_raw = df_raw[['open', 'high', 'low', 'close', 'volume']].dropna(subset=['close'])
            return df_raw if len(df_raw) >= 10 else None
        except Exception as e:
            if attempt < 2:
                time.sleep(2)
            else:
                print(f"  [Dashboard] ❌ _fetch_index_history {symbol}: {e}")
    return None

# =============================================================================
# API ENDPOINTS
# =============================================================================

INDEX_SYMBOLS = {'VNINDEX', 'VN30', 'HNX', 'HNXINDEX', 'UPCOM', 'VN100', 'VN30F1M'}


@app.route("/api/signals")
def api_signals():
    alerted = _get_alerted_today() if _get_alerted_today else {}
    cache   = _get_history_cache() if _get_history_cache else {}
    result  = []
    for sym, sig in alerted.items():
        df  = cache.get(sym)
        pct = None
        if df is not None and len(df) >= 2:
            pct = round(float(df['close'].iloc[-1] / df['close'].iloc[-2] - 1) * 100, 2)
        result.append({
            "symbol": sym,
            "signal": sig,
            "emoji":  _signal_emoji.get(sig, "📌"),
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
                print(f"  [Dashboard] ❌ Fetch heatmap: {e}")
        return jsonify({
            "data":       _heatmap_cache["data"],
            "timestamp":  _heatmap_cache["ts"],
            "cached_age": int(time.time() - _heatmap_cache["updated_at"]),
        })


# ---------------------------------------------------------------------------
# TV Chart OHLCV endpoints — luôn fetch fresh, không dùng history_cache
# ---------------------------------------------------------------------------

@app.route("/api/ohlcv/<symbol>")
def api_ohlcv_daily(symbol):
    """Daily OHLCV — fetch fresh từ vnstock (giống scanner_full fetch_fresh_for_chart)."""
    symbol = symbol.upper().strip()
    is_index = symbol in INDEX_SYMBOLS
    try:
        df = _fetch_index_history(symbol) if is_index else _fetch_daily_fresh(symbol)
        if df is None or len(df) < 2:
            return jsonify({"error": "no_data"}), 404
        return _build_ohlcv_json(symbol, df)
    except Exception as e:
        print(f"  [Dashboard] ❌ api_ohlcv_daily {symbol}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/ohlcv_weekly/<symbol>")
def api_ohlcv_weekly(symbol):
    """Weekly OHLCV — fetch daily fresh rồi resample."""
    symbol = symbol.upper().strip()
    is_index = symbol in INDEX_SYMBOLS
    try:
        if is_index:
            df_d = _fetch_index_history(symbol)
            if df_d is None:
                return jsonify({"error": "no_data"}), 404
            import pandas as pd
            df = df_d[['open', 'high', 'low', 'close', 'volume']].resample('W-FRI').agg({
                'open': 'first', 'high': 'max', 'low': 'min',
                'close': 'last', 'volume': 'sum',
            }).dropna()
        else:
            df = _fetch_weekly_fresh(symbol)

        if df is None or len(df) < 2:
            return jsonify({"error": "no_data"}), 404
        return _build_ohlcv_json(symbol, df)
    except Exception as e:
        print(f"  [Dashboard] ❌ api_ohlcv_weekly {symbol}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/ohlcv_15m/<symbol>")
def api_ohlcv_15m(symbol):
    """15-minute OHLCV — luôn fetch live, không cache."""
    symbol = symbol.upper().strip()
    try:
        df = _fetch_15m_fresh(symbol)
        if df is None:
            return jsonify({"error": "no_15m_data"}), 404
        return _build_ohlcv_json(symbol, df)
    except Exception as e:
        print(f"  [Dashboard] ❌ api_ohlcv_15m {symbol}: {e}")
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Scanner Chart (PNG) endpoint — dùng _fetch_chart_fn từ scanner_full.py
# ---------------------------------------------------------------------------

@app.route("/api/chart_images/<symbol>")
def api_chart_images(symbol):
    """Trả về list ảnh PNG base64 cho Scanner Chart tab."""
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
        print(f"  [Dashboard] 📊 Tạo Scanner Chart {symbol}...")
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

        print(f"  [Dashboard] ✅ Scanner Chart {symbol}: {len(b64_list)} ảnh OK")
        return jsonify({
            "symbol": symbol,
            "images": b64_list,
            "labels": labels,
            "cached": False,
        })
    except Exception as e:
        print(f"  [Dashboard] ❌ Scanner Chart {symbol}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/chart_cache_clear/<symbol>", methods=["DELETE"])
def api_chart_cache_clear(symbol):
    symbol = symbol.upper().strip()
    with _chart_lock:
        removed = symbol in _chart_cache
        _chart_cache.pop(symbol, None)
    return jsonify({"symbol": symbol, "cleared": removed})


# ---------------------------------------------------------------------------
# Misc endpoints
# ---------------------------------------------------------------------------

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
    return jsonify({
        "signal_ttl_sec":  SIGNAL_TTL_SEC,
        "heatmap_ttl_sec": HEATMAP_TTL_SEC,
    })


@app.route("/")
def index():
    return Response(DASHBOARD_HTML, mimetype="text/html")

# =============================================================================
# START
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
    print(f"   Tín hiệu refresh : {SIGNAL_TTL_SEC}s")
    print(f"   Heatmap refresh  : {HEATMAP_TTL_SEC}s")
    print(f"   TV Chart         : ✅ fetch fresh (Daily/Weekly/15m), không dùng cache")
    print(f"   Scanner Chart    : {'✅ đã đăng ký' if fetch_chart_fn else '❌ chưa đăng ký'} (cache {CHART_TTL_SEC}s)")

    js_path = os.path.join(STATIC_DIR, "lightweight-charts.min.js")
    if os.path.exists(js_path):
        size_kb = os.path.getsize(js_path) // 1024
        print(f"   TV Chart JS      : ✅ static/lightweight-charts.min.js ({size_kb} KB)")
    else:
        print(f"   TV Chart JS      : ⚠️  CHƯA CÓ FILE! Chạy: bash setup_static.sh")

# =============================================================================
# HTML DASHBOARD
# Notes về thiết kế chart:
#   - Volume được vẽ overlay dưới cây nến (scaleMarginBottom lớn) trong cùng panel price
#   - MACD histogram x3 (scaleMarginTop nhỏ để bar cao hơn)
#   - RIGHT_PADDING_BARS = 30 bar trống bên phải
#   - TV Chart luôn fetch /api/ohlcv/* (fresh, không dùng history_cache)
#   - Search box trong popup header
# =============================================================================

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Scanner Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600;700&family=Barlow+Condensed:wght@600;700;800&display=swap" rel="stylesheet">
<style>
/* ─── DESIGN TOKENS ─────────────────────────────────────── */
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
*, *::before, *::after { margin:0; padding:0; box-sizing:border-box; }
body { background:var(--bg); color:var(--text); font-family:var(--font-mono); font-size:13px; min-height:100vh; }

/* ─── HEADER ────────────────────────────────────────────── */
header {
  display:flex; align-items:center; justify-content:space-between;
  padding:11px 22px; background:var(--surface);
  border-bottom:1px solid var(--border);
  position:sticky; top:0; z-index:100;
  box-shadow:0 1px 6px var(--shadow);
}
header h1 { font-family:var(--font-ui); font-size:19px; font-weight:800; letter-spacing:2.5px; color:var(--accent); text-transform:uppercase; }
.hdr-right { display:flex; gap:18px; align-items:center; }
#clock { color:var(--muted); font-size:11px; }
.dot-live { width:8px; height:8px; border-radius:50%; background:var(--green); box-shadow:0 0 8px rgba(14,159,110,.5); animation:pulse 2s ease-in-out infinite; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.3} }

/* ─── LAYOUT ────────────────────────────────────────────── */
.wrap { padding:16px 20px; display:flex; flex-direction:column; gap:16px; }

/* ─── PANEL ─────────────────────────────────────────────── */
.panel { background:var(--surface); border:1px solid var(--border); border-radius:8px; overflow:hidden; box-shadow:0 1px 4px var(--shadow); }
.panel-hdr { display:flex; align-items:center; justify-content:space-between; padding:9px 16px; background:var(--surf2); border-bottom:1px solid var(--border); }
.panel-title { font-family:var(--font-ui); font-size:12px; font-weight:800; text-transform:uppercase; letter-spacing:2px; color:var(--accent); }
.panel-meta { font-size:10px; color:var(--muted); }
.pbar-wrap { height:2px; background:transparent; overflow:hidden; }
.pbar-fill { height:100%; width:0%; background:linear-gradient(90deg,var(--accent),var(--green)); opacity:.5; transition:none; }
.panel-body { padding:12px 14px; }

/* ─── HEATMAP PANEL HEADER ──────────────────────────────── */
.hmap-panel-hdr { display:flex; align-items:center; gap:6px; padding:8px 16px; background:var(--surf2); border-bottom:1px solid var(--border); flex-wrap:nowrap; }
.hmap-hdr-row1 { display:flex; align-items:center; gap:8px; flex-shrink:0; min-width:0; }
.hmap-ts-wrap { margin-left:auto; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; flex-shrink:1; min-width:0; }
.hmap-link-btn { display:inline-flex; align-items:center; gap:5px; padding:4px 11px; border-radius:5px; border:1px solid var(--border); background:var(--surface); color:var(--accent); font-family:var(--font-mono); font-size:10px; font-weight:600; cursor:pointer; text-decoration:none; white-space:nowrap; transition:all .15s; }
.hmap-link-btn:hover { background:var(--accent); color:#fff; border-color:var(--accent); }
.hmap-search-wrap { position:relative; display:flex; align-items:center; flex-shrink:0; }
.hmap-search-wrap .s-icon { position:absolute; left:11px; top:50%; transform:translateY(-50%); color:var(--muted); font-size:13px; pointer-events:none; }
.hmap-search-input { width:140px; padding:5px 10px 5px 30px; border-radius:20px; border:1px solid var(--border); background:var(--surface); color:var(--text); font-family:var(--font-mono); font-size:11px; outline:none; transition:border-color .15s,box-shadow .15s,width .2s; }
.hmap-search-input::placeholder { color:var(--muted); }
.hmap-search-input:focus { border-color:var(--accent); box-shadow:0 0 0 2px rgba(26,86,219,.12); width:170px; }

/* ─── HEATMAP GRID ──────────────────────────────────────── */
.hmap-outer { overflow-x:auto; padding-bottom:4px; }
.hmap-outer::-webkit-scrollbar { height:4px; }
.hmap-outer::-webkit-scrollbar-thumb { background:var(--border); border-radius:2px; }
.hmap-row { display:inline-flex; flex-direction:row; gap:4px; align-items:flex-start; min-width:max-content; padding:2px; }
.hmap-col { display:flex; flex-direction:column; gap:2px; width:162px; flex-shrink:0; }
.hmap-group { display:flex; flex-direction:column; gap:2px; }
.hmap-ghdr { display:flex; align-items:center; justify-content:center; padding:0 8px; height:24px; border-radius:4px; background:rgb(220,228,250); border:1px solid rgb(160,180,230); overflow:hidden; gap:16px; }
.hmap-gname { font-family:var(--font-ui); font-size:10px; font-weight:700; text-transform:uppercase; letter-spacing:.6px; color:rgb(25,55,150); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.hmap-gavg { font-family:var(--font-mono); font-size:9px; font-weight:400; flex-shrink:0; }
.hmap-gavg.pos { color:rgb(22,120,40); }
.hmap-gavg.neg { color:rgb(185,25,25); }
.hmap-gavg.zer { color:rgb(110,105,20); }
.hmap-cell { display:grid; grid-template-columns:56px 48px 1fr; align-items:center; height:24px; border-radius:4px; cursor:pointer; border:1px solid rgba(0,0,0,.1); transition:filter .12s,transform .1s,box-shadow .12s; overflow:hidden; }
.hmap-cell:hover { filter:brightness(.88); transform:scale(1.035); z-index:2; box-shadow:0 2px 8px rgba(0,0,0,.18); }
.hmap-cell > span { display:flex; align-items:center; justify-content:center; height:100%; overflow:hidden; white-space:nowrap; font-family:var(--font-mono); }
.hc-sym   { font-size:10px; font-weight:400; }
.hc-price { font-size:8.5px; font-weight:400; opacity:.82; }
.hc-pct   { font-size:9.5px; font-weight:400; }

/* ─── SIGNAL LIST ───────────────────────────────────────── */
.sig-list { display:flex; flex-direction:column; gap:3px; }
.sig-row {
  display:grid; grid-template-columns:28px 68px 1fr 106px;
  align-items:center; padding:7px 10px; border-radius:5px;
  border:1px solid var(--border); cursor:pointer; transition:all .15s;
  animation:fadeIn .3s ease; background:var(--surface);
}
@keyframes fadeIn { from{opacity:0;transform:translateX(-5px)} to{opacity:1;transform:none} }
.sig-row:hover { background:#eef3ff; border-color:rgba(26,86,219,.3); box-shadow:0 2px 8px rgba(26,86,219,.07); }
.sig-row:hover .s-sym { color:var(--accent); }
.s-emoji { font-size:14px; text-align:center; }
.s-sym { font-weight:700; font-size:13px; transition:color .15s; }
.s-type { font-size:11px; color:var(--muted); }
.s-badge { font-size:10px; font-weight:700; padding:3px 7px; border-radius:4px; text-align:center; letter-spacing:.4px; font-family:var(--font-ui); }
.b-BREAKOUT { background:#dcfce7; color:#15803d; border:1px solid #86efac; }
.b-POCKET   { background:#fef9c3; color:#854d0e; border:1px solid #fde047; }
.b-PREBREAK { background:#f3e8ff; color:#7e22ce; border:1px solid #d8b4fe; }
.b-BBREAKP  { background:#dbeafe; color:#1d4ed8; border:1px solid #93c5fd; }
.b-BFISH    { background:#ffedd5; color:#c2410c; border:1px solid #fdba74; }
.b-MACROSS  { background:#f1f5f9; color:#475569; border:1px solid #cbd5e1; }
.empty { text-align:center; padding:36px 20px; color:var(--muted); font-size:12px; }
.empty .big { font-size:30px; margin-bottom:8px; }

/* ─── POPUP OVERLAY ─────────────────────────────────────── */
.overlay { display:none; position:fixed; inset:0; z-index:9999; background:rgba(17,24,39,.5); backdrop-filter:blur(4px); align-items:center; justify-content:center; }
.overlay.on { display:flex; }
.pbox {
  background:var(--surface); border:1px solid var(--border); border-radius:10px;
  box-shadow:0 20px 60px rgba(0,0,0,.15); width:99vw; max-width:1800px; height:94vh;
  display:flex; flex-direction:column; overflow:hidden;
  animation:popIn .2s ease;
  will-change:transform; transform:translateZ(0); backface-visibility:hidden;
}
@keyframes popIn { from{opacity:0;transform:scale(.96) translateY(14px)} to{opacity:1;transform:none} }

/* ─── POPUP HEADER ──────────────────────────────────────── */
.phdr {
  display:grid; grid-template-columns:auto 1fr auto auto;
  align-items:center; gap:10px; padding:7px 14px;
  background:var(--surf2); border-bottom:1px solid var(--border);
  flex-shrink:0;
}
.ptitle { font-family:var(--font-ui); font-size:17px; font-weight:800; color:var(--accent); letter-spacing:1.5px; white-space:nowrap; }

/* Search box trong popup */
.phdr-search-wrap { position:relative; display:flex; align-items:center; }
.phdr-search-wrap .si { position:absolute; left:9px; top:50%; transform:translateY(-50%); color:var(--muted); font-size:12px; pointer-events:none; }
.phdr-search-input { width:130px; padding:4px 8px 4px 26px; border-radius:16px; border:1px solid var(--border); background:var(--surface); color:var(--text); font-family:var(--font-mono); font-size:11px; outline:none; transition:border-color .15s,box-shadow .15s,width .2s; }
.phdr-search-input::placeholder { color:var(--muted); }
.phdr-search-input:focus { border-color:var(--accent); box-shadow:0 0 0 2px rgba(26,86,219,.12); width:160px; }

.ctabs { display:flex; gap:2px; align-items:flex-end; flex-wrap:wrap; }
.ctab { font-size:11px; font-family:var(--font-mono); font-weight:600; padding:5px 11px; border-radius:5px 5px 0 0; border:1px solid var(--border); border-bottom:2px solid transparent; background:var(--bg); color:var(--muted); cursor:pointer; transition:all .15s; white-space:nowrap; }
.ctab.on { background:var(--surface); color:var(--accent); border-color:var(--border); border-bottom-color:var(--accent); font-weight:700; }
.ctab:hover:not(.on) { color:var(--accent); background:#eef3ff; }
.closebtn { width:28px; height:28px; border-radius:50%; border:1px solid var(--border); background:var(--bg); color:var(--muted); font-size:16px; cursor:pointer; display:flex; align-items:center; justify-content:center; transition:all .15s; flex-shrink:0; }
.closebtn:hover { background:var(--red); color:#fff; border-color:var(--red); }

/* ─── POPUP BODY & PANELS ───────────────────────────────── */
.pbody { flex:1; overflow:hidden; position:relative; border-top:1px solid var(--border); }
.tpanel { position:absolute; inset:0; display:none; }
.tpanel.on { display:block; }
.tpanel iframe { width:100%; height:100%; border:none; display:block; }

/* ─── TV CHART PANEL ────────────────────────────────────── */
#panel-tv { overflow:hidden; background:#fff; display:none; flex-direction:column; }
#panel-tv.on { display:flex; }
#tv-loading { display:flex; align-items:center; justify-content:center; flex:1; color:#6b7280; font-size:13px; font-family:var(--font-mono); background:#fff; }

#tv-chart-wrap { flex:1; display:flex; flex-direction:column; overflow:hidden; background:#fff; }

/* Toolbar */
#tv-toolbar { display:flex; align-items:center; gap:8px; padding:5px 10px; background:#f8f9fb; border-bottom:1px solid #eee; flex-shrink:0; flex-wrap:wrap; }
.tv-tf-btn { font-family:var(--font-mono); font-size:10px; font-weight:600; padding:3px 9px; border-radius:4px; border:1px solid #dde3ee; background:#fff; color:#374151; cursor:pointer; transition:all .15s; }
.tv-tf-btn.on { background:#1a56db; color:#fff; border-color:#1a56db; }
.tv-tf-btn:hover:not(.on) { background:#eef3ff; border-color:#1a56db; color:#1a56db; }
.tv-tf-loading { font-size:10px; color:#9ca3af; font-family:var(--font-mono); display:none; }
.tv-ema-toggle { display:flex; align-items:center; gap:5px; margin-left:6px; cursor:pointer; user-select:none; }
.tv-ema-toggle input[type=checkbox] { appearance:none; -webkit-appearance:none; width:14px; height:14px; border-radius:50%; border:1.5px solid #9ca3af; background:#fff; cursor:pointer; position:relative; transition:all .15s; flex-shrink:0; }
.tv-ema-toggle input[type=checkbox]:checked { background:#7c3aed; border-color:#7c3aed; }
.tv-ema-toggle input[type=checkbox]:checked::after { content:''; position:absolute; top:2px; left:2px; width:6px; height:6px; border-radius:50%; background:#fff; }
.tv-ema-toggle span { font-family:var(--font-mono); font-size:10px; color:#6b7280; }

/* Legend bar */
#tv-legend { display:flex; align-items:center; gap:14px; padding:4px 10px; background:#fff; border-bottom:1px solid #f0f0f0; font-family:var(--font-mono); font-size:10px; flex-wrap:wrap; flex-shrink:0; }
.tv-leg-item { display:flex; align-items:center; gap:4px; white-space:nowrap; }
.tv-leg-label { color:#9ca3af; }
.tv-leg-val { font-weight:600; }

/* ─── TV Chart areas ─────────────────────────────────────
   Price + Volume trong CÙNG một container (overlay).
   MACD riêng bên dưới.
──────────────────────────────────────────────────────── */
#tv-charts-area { flex:1; display:flex; flex-direction:column; overflow:hidden; background:#fff; }

/* Price panel — chiếm phần lớn, volume sẽ overlay bên trong */
#tv-price-panel { flex:1; position:relative; background:#fff; min-height:0; }

/* MACD panel — cố định chiều cao, không quá lớn */
#tv-macd-container { height:160px; flex-shrink:0; background:#fff; border-top:1px solid #f0f0f0; position:relative; }
#tv-macd-legend { position:absolute; top:3px; left:8px; z-index:10; display:flex; align-items:center; gap:10px; font-family:var(--font-mono); font-size:10px; pointer-events:none; }

/* Ticker overlay khi gõ mã */
#tv-ticker-overlay { position:absolute; top:50%; left:50%; transform:translate(-50%,-50%); background:rgba(26,86,219,.92); color:#fff; font-family:var(--font-mono); font-size:22px; font-weight:700; letter-spacing:3px; padding:10px 28px; border-radius:8px; display:none; z-index:100; pointer-events:none; min-width:140px; text-align:center; box-shadow:0 4px 24px rgba(26,86,219,.35); }

/* ─── SCANNER CHART PANEL ───────────────────────────────── */
#panel-scanner { overflow:hidden; background:#fff; display:none; flex-direction:column; }
#panel-scanner.on { display:flex; }
.scanner-loading { display:flex; align-items:center; justify-content:center; flex:1; color:#6b7280; font-size:14px; font-family:var(--font-mono); }
.album-outer { flex:1; display:flex; flex-direction:column; overflow:hidden; }
.album-center { flex:1; overflow-y:auto; display:flex; flex-direction:column; align-items:center; padding:4px; gap:4px; background:#fff; }
.album-center::-webkit-scrollbar { width:4px; }
.album-center::-webkit-scrollbar-thumb { background:#444; border-radius:2px; }
.album-slide { display:none; flex-direction:column; align-items:center; gap:8px; width:100%; }
.album-slide.on { display:flex; }
.album-slide img { max-width:100%; max-height:calc(94vh - 120px); object-fit:contain; border-radius:3px; border:1px solid #dde3ee; }
.album-label { font-size:11px; color:#888; font-family:var(--font-mono); }
.album-nav-bar { display:flex; align-items:center; justify-content:center; gap:10px; padding:6px 0 8px; flex-shrink:0; background:#fff; }
.album-nav-btn { width:30px; height:30px; border-radius:50%; border:1px solid #dde3ee; background:#f4f6fb; color:#6b7280; font-size:14px; cursor:pointer; display:flex; align-items:center; justify-content:center; transition:background .15s,color .15s,border-color .15s; user-select:none; flex-shrink:0; }
.album-nav-btn:hover:not(.disabled) { background:#1a56db; color:#fff; border-color:#1a56db; }
.album-nav-btn.disabled { opacity:.25; cursor:default; pointer-events:none; }
.album-dots-wrap { display:flex; gap:6px; align-items:center; }
.album-dot { width:8px; height:8px; border-radius:50%; background:#dde3ee; cursor:pointer; transition:all .15s; }
.album-dot.on { background:#1a56db; transform:scale(1.3); }
.album-refresh-btn { width:30px; height:30px; padding:0; border-radius:50%; border:1px solid #dde3ee; background:#f4f6fb; color:#6b7280; font-size:15px; cursor:pointer; display:flex; align-items:center; justify-content:center; transition:background .15s,color .15s,border-color .15s; user-select:none; flex-shrink:0; }
.album-refresh-btn:hover { background:#0e9f6e; color:#fff; border-color:#0e9f6e; }
.album-refresh-btn.spinning span.ri { display:inline-block; animation:spin .7s linear infinite; }
@keyframes spin { to{transform:rotate(360deg)} }
.album-hint { text-align:center; font-size:10px; color:#9ca3af; padding:0 0 4px; font-family:var(--font-mono); flex-shrink:0; background:#fff; }

/* ─── FOOTER ────────────────────────────────────────────── */
footer { text-align:center; padding:9px; color:var(--muted); font-size:10px; border-top:1px solid var(--border); background:var(--surface); }

/* ─── SCROLLBARS ────────────────────────────────────────── */
::-webkit-scrollbar { width:5px; height:5px; }
::-webkit-scrollbar-track { background:var(--bg); }
::-webkit-scrollbar-thumb { background:var(--border); border-radius:3px; }
::-webkit-scrollbar-thumb:hover { background:var(--muted); }

/* ─── MOBILE ────────────────────────────────────────────── */
@media(max-width:768px) {
  .pbox { width:100vw; height:100vh; border-radius:0; }
  .phdr-search-input { width:100px; }
  #tv-macd-container { height:110px; }
  .hmap-panel-hdr { flex-direction:column; align-items:flex-start; gap:4px; padding:7px 10px; }
  .hmap-hdr-row1 { width:100%; flex-wrap:nowrap; overflow-x:auto; scrollbar-width:none; gap:6px; }
  .hmap-hdr-row1::-webkit-scrollbar { display:none; }
  .hmap-hdr-row1 > * { flex-shrink:0; }
  .hmap-search-input { width:122px; font-size:11px; }
  .hmap-ts-wrap { margin-left:0; width:100%; white-space:normal; word-break:break-word; font-size:9px; }
}
@media screen and (max-width:768px) {
  .overlay { backdrop-filter:none; background:rgba(17,24,39,0); }
  .pbox { width:100vw!important; max-width:100vw!important; height:100dvh!important; border-radius:0!important; border:none!important; }
  .phdr { display:flex!important; flex-direction:column!important; padding:0!important; gap:0!important; }
  .phdr > * { display:none!important; }
}
#mob-tabrow { display:none; }
@media screen and (max-width:768px) {
  #mob-tabrow { display:flex!important; flex-direction:row!important; flex-wrap:nowrap!important; align-items:center; overflow-x:scroll!important; overflow-y:hidden!important; -webkit-overflow-scrolling:touch; padding:6px 8px!important; gap:5px!important; background:var(--surf2)!important; border-bottom:1px solid var(--border)!important; scrollbar-width:none!important; }
  #mob-tabrow::-webkit-scrollbar { display:none; }
  #mob-tabrow > button { flex-shrink:0!important; white-space:nowrap!important; padding:7px 14px!important; border-radius:6px!important; border:1px solid var(--border)!important; font-size:12px!important; font-family:var(--font-mono)!important; font-weight:600!important; cursor:pointer!important; background:var(--bg)!important; color:var(--muted)!important; transition:background .15s,color .15s!important; }
  #mob-tabrow > button.on { background:var(--surface)!important; color:var(--accent)!important; border-color:var(--accent)!important; font-weight:700!important; box-shadow:0 2px 0 0 var(--accent)!important; }
}

/* ─── MOBILE LIGHTBOX ───────────────────────────────────── */
#mob-lightbox { display:none; position:fixed; inset:0; z-index:99999; background:#fff; overflow:hidden; touch-action:none; }
#mob-lightbox.on { display:block; }
#lb-viewport { position:absolute; inset:0; overflow:hidden; }
#lb-strip { display:flex; flex-direction:row; height:100%; will-change:transform; }
#lb-strip.snapping { transition:transform .32s cubic-bezier(.25,.46,.45,.94); }
.lb-slide { flex-shrink:0; width:100vw; height:100%; display:flex; align-items:center; justify-content:center; overflow:hidden; }
.lb-slide img { max-width:100vw; max-height:100dvh; object-fit:contain; display:block; transform-origin:center center; will-change:transform; user-select:none; -webkit-user-drag:none; pointer-events:none; transition:transform .25s ease; }
.lb-slide img.zooming { transition:none!important; }
#mob-lightbox-close { position:absolute; top:14px; right:14px; width:38px; height:38px; border-radius:50%; background:rgba(0,0,0,.07); border:1px solid rgba(0,0,0,.15); color:#333; font-size:20px; display:flex; align-items:center; justify-content:center; cursor:pointer; z-index:10; touch-action:manipulation; }
#mob-lightbox-close:active { background:rgba(0,0,0,.14); }
#mob-lightbox-counter { position:absolute; bottom:20px; left:50%; transform:translateX(-50%); display:flex; gap:8px; align-items:center; z-index:10; pointer-events:none; }
.mob-lb-dot { width:7px; height:7px; border-radius:50%; background:rgba(0,0,0,.18); transition:background .2s,transform .2s; }
.mob-lb-dot.on { background:#1a56db; transform:scale(1.4); }
#mob-lightbox-label { position:absolute; top:16px; left:50%; transform:translateX(-50%); color:rgba(30,30,30,.85); font-family:var(--font-mono); font-size:12px; white-space:nowrap; z-index:10; pointer-events:none; background:rgba(0,0,0,.08); padding:3px 12px; border-radius:20px; }
#lb-zoom-hint { position:absolute; bottom:52px; left:50%; transform:translateX(-50%); background:rgba(0,0,0,.45); color:#fff; font-family:var(--font-mono); font-size:10px; padding:3px 10px; border-radius:20px; z-index:11; pointer-events:none; opacity:0; transition:opacity .3s; white-space:nowrap; }
#edge-swipe-zone { position:fixed; left:0; top:0; width:30px; height:100%; z-index:10000; display:none; touch-action:pan-y; }
#edge-swipe-zone.on { display:block; }
#mob-close-float { display:none; }
@media screen and (max-width:768px) and (orientation:portrait) {
  #mob-close-float { display:flex; position:fixed; right:0; top:50%; transform:translateY(-50%); z-index:10001; width:15px; height:100px; border-radius:6px 0 0 6px; background:rgba(17,24,39,.02); border:1px solid rgba(255,255,255,.03); border-right:none; color:rgba(255,255,255,.10); font-size:12px; align-items:center; justify-content:center; cursor:pointer; touch-action:manipulation; pointer-events:auto; }
  #mob-close-float:active { background:rgba(17,24,39,.6); color:rgba(255,255,255,.8); }
}
</style>
</head>
<body>

<!-- ══ MAIN HEADER ══ -->
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
    <div class="pbar-wrap"><div class="pbar-fill" id="pbar-sig"></div></div>
    <div class="panel-body">
      <div class="sig-list" id="sig-list">
        <div class="empty"><div class="big">📡</div><div>Đang tải...</div></div>
      </div>
    </div>
  </div>

  <!-- HEATMAP -->
  <div class="panel">
    <div class="hmap-panel-hdr">
      <div class="hmap-hdr-row1">
        <span class="panel-title">Heatmap</span>
        <button class="hmap-link-btn" onclick="openUrl('https://dstock.vndirect.com.vn','MARKET')">MARKET</button>
        <button class="hmap-link-btn" onclick="openUrl('https://24hmoney.vn/indices/vn-index','VNINDEX')">VNINDEX</button>
        <div class="hmap-search-wrap">
          <span class="s-icon">🔍</span>
          <input class="hmap-search-input" id="hmap-search-input" type="text" placeholder="Tìm kiếm mã" maxlength="10" autocomplete="off" spellcheck="false">
        </div>
      </div>
      <span class="panel-meta hmap-ts-wrap" id="hmap-ts">Đang tải...</span>
    </div>
    <div class="pbar-wrap"><div class="pbar-fill" id="pbar-hmap"></div></div>
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
  <button id="mob-close-float" onclick="closePopup()" aria-label="Đóng">✕</button>
  <div class="pbox">

    <!-- POPUP HEADER: Symbol | Tabs | SearchBox | Close -->
    <div class="phdr">
      <span class="ptitle" id="ptitle">Chart</span>
      <div class="ctabs">
        <button class="ctab on"  id="ctab-tv"       onclick="switchTab('tv')">📊 TV Chart</button>
        <button class="ctab"     id="ctab-vs"       onclick="switchTab('vs')">📈 Vietstock</button>
        <button class="ctab"     id="ctab-scanner"  onclick="switchTab('scanner')">🖼 Scanner Chart</button>
        <button class="ctab"     id="ctab-vnd-cs"   onclick="switchTab('vnd-cs')">⚖️ Cơ bản</button>
        <button class="ctab"     id="ctab-vnd-news" onclick="switchTab('vnd-news')">🗞️ Tin tức</button>
        <button class="ctab"     id="ctab-vnd-sum"  onclick="switchTab('vnd-sum')">📄 Tổng quan</button>
        <button class="ctab"     id="ctab-24h"      onclick="switchTab('24h')">💬 24HMoney</button>
      </div>
      <!-- Search box trong popup -->
      <div class="phdr-search-wrap">
        <span class="si">🔍</span>
        <input class="phdr-search-input" id="phdr-search-input" type="text"
               placeholder="Tìm mã..." maxlength="10" autocomplete="off" spellcheck="false">
      </div>
      <button class="closebtn" onclick="closePopup()">✕</button>
    </div>

    <div class="pbody">

      <!-- TV CHART TAB -->
      <div class="tpanel on" id="panel-tv">
        <div id="tv-loading">⏳ Đang tải dữ liệu...</div>
        <div id="tv-chart-wrap" style="display:none">

          <!-- Toolbar: timeframe + EMA toggle -->
          <div id="tv-toolbar">
            <span style="font-family:var(--font-ui);font-size:10px;font-weight:700;color:#6b7280;letter-spacing:1px;text-transform:uppercase;">Khung:</span>
            <button class="tv-tf-btn on" id="tf-D"  onclick="tvSwitchTF('D')">D</button>
            <button class="tv-tf-btn"    id="tf-W"  onclick="tvSwitchTF('W')">W</button>
            <button class="tv-tf-btn"    id="tf-15" onclick="tvSwitchTF('15')">15m</button>
            <span class="tv-tf-loading" id="tv-tf-loading">⏳ đang tải...</span>
            <span style="flex:1"></span>
            <label class="tv-ema-toggle" title="Hiển thị EMA 30 / 100 / 200">
              <input type="checkbox" id="chk-ema-extra" onchange="tvToggleExtraEMA(this.checked)">
              <span>EMA 30·100·200</span>
            </label>
          </div>

          <!-- Legend: OHLC + % -->
          <div id="tv-legend">
            <span style="font-family:var(--font-ui);font-size:11px;font-weight:700;color:#374151;letter-spacing:.5px" id="tv-sym-label">—</span>
            <span class="tv-leg-item"><span class="tv-leg-label">O</span><span class="tv-leg-val" id="leg-open">—</span></span>
            <span class="tv-leg-item"><span class="tv-leg-label">H</span><span class="tv-leg-val" id="leg-high" style="color:#0e9f6e">—</span></span>
            <span class="tv-leg-item"><span class="tv-leg-label">L</span><span class="tv-leg-val" id="leg-low" style="color:#e02424">—</span></span>
            <span class="tv-leg-item"><span class="tv-leg-label">C</span><span class="tv-leg-val" id="leg-close">—</span></span>
            <span class="tv-leg-item"><span class="tv-leg-val" id="leg-pct" style="font-weight:700">—</span></span>
          </div>

          <!-- Charts area: Price(+Volume overlay) trên, MACD dưới -->
          <div id="tv-charts-area">
            <div id="tv-price-panel">
              <div id="tv-ticker-overlay"></div>
              <!-- LightweightCharts sẽ render vào đây (#tv-price-panel) -->
            </div>
            <div id="tv-macd-container">
              <div id="tv-macd-legend">
                <span style="font-family:var(--font-ui);font-size:9px;font-weight:700;color:#374151;letter-spacing:.5px">MACD(12,26,9)</span>
                <span class="tv-leg-item"><span class="tv-leg-label" style="font-size:9px">MACD</span><span class="tv-leg-val" id="leg-macd" style="color:#2563eb;font-size:10px">—</span></span>
                <span class="tv-leg-item"><span class="tv-leg-label" style="font-size:9px">Sig</span><span class="tv-leg-val" id="leg-signal" style="color:#f97316;font-size:10px">—</span></span>
                <span class="tv-leg-item"><span class="tv-leg-label" style="font-size:9px">Hist</span><span class="tv-leg-val" id="leg-hist" style="font-size:10px">—</span></span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- VIETSTOCK TAB -->
      <div class="tpanel" id="panel-vs">
        <iframe id="iframe-vs" src="about:blank" allowfullscreen></iframe>
      </div>

      <!-- SCANNER CHART TAB -->
      <div class="tpanel" id="panel-scanner">
        <div class="scanner-loading" id="scanner-loading"><span>⏳ Đang tạo chart từ scanner...</span></div>
        <div class="album-outer" id="album-outer" style="display:none">
          <div class="album-center"><div id="album-slides"></div></div>
          <div class="album-nav-bar">
            <button class="album-nav-btn disabled" id="btn-prev" onclick="albumNav(-1)">&#9664;</button>
            <div class="album-dots-wrap" id="album-dots"></div>
            <button class="album-nav-btn" id="btn-next" onclick="albumNav(1)">&#9654;</button>
            <button class="album-refresh-btn" id="btn-refresh" onclick="refreshScannerChart()"><span class="ri">&#8635;</span></button>
          </div>
          <div class="album-hint">◀ ▶ hoặc phím ← → để chuyển ảnh</div>
        </div>
      </div>

      <!-- OTHER IFRAME TABS -->
      <div class="tpanel" id="panel-vnd-cs">  <iframe id="iframe-vnd-cs"   src="about:blank" allowfullscreen></iframe></div>
      <div class="tpanel" id="panel-vnd-news"><iframe id="iframe-vnd-news" src="about:blank" allowfullscreen></iframe></div>
      <div class="tpanel" id="panel-vnd-sum"> <iframe id="iframe-vnd-sum"  src="about:blank" allowfullscreen></iframe></div>
      <div class="tpanel" id="panel-24h">     <iframe id="iframe-24h"      src="about:blank" allowfullscreen></iframe></div>
      <div class="tpanel" id="panel-url">     <iframe id="iframe-url"      src="about:blank" allowfullscreen></iframe></div>
    </div>
  </div>
</div>

<!-- Mobile Lightbox -->
<div id="mob-lightbox" onclick="lbClose()">
  <div id="lb-viewport" onclick="event.stopPropagation()"><div id="lb-strip"></div></div>
  <div id="mob-lightbox-label">📊 Daily [D]</div>
  <button id="mob-lightbox-close" onclick="lbClose()">✕</button>
  <div id="mob-lightbox-counter"></div>
  <div id="lb-zoom-hint">Chụm 2 ngón để zoom</div>
</div>
<div id="edge-swipe-zone"></div>

<script src="/static/lightweight-charts.min.js"></script>
<script>
// ╔══════════════════════════════════════════════════════╗
// ║  CONFIG                                              ║
// ╚══════════════════════════════════════════════════════╝
let SIG_TTL = 30, HMAP_TTL = 120;

async function loadConfig() {
  try {
    const j = await fetch('/api/config').then(r => r.json());
    SIG_TTL  = j.signal_ttl_sec  || 30;
    HMAP_TTL = j.heatmap_ttl_sec || 120;
  } catch (e) {}
  document.getElementById('footer-txt').textContent =
    `Scanner Bot Dashboard  •  Tín hiệu tự động làm mới sau ${SIG_TTL}s  •  Heatmap tự động làm mới sau ${HMAP_TTL}s`;
}

// ╔══════════════════════════════════════════════════════╗
// ║  HEATMAP CONFIG & RENDER                             ║
// ╚══════════════════════════════════════════════════════╝
const HMAP_COLS = [
  {groups:[{name:"VN30",syms:["FPT","GAS","NVL","VNM","VCB","PLX","TCB","MWG","STB","HPG","PNJ","BID","CTG","HDB","VJC","VPB","KDH","MBB","VHM","POW","VRE","MSN","SSI","ACB","BVH","GVR","TPB"]}]},
  {groups:[{name:"NGAN HANG",syms:["VCB","BID","CTG","MBB","ACB","TCB","TPB","HDB","SHB","STB","VIB","VPB","MSB","ABB","BVB","LPB"]},{name:"DAU KHI",syms:["GAS","PVD","PVS","BSR","OIL","PVB","PVC","PLX","PET","PVT"]}]},
  {groups:[{name:"CHUNG KHOAN",syms:["SSI","VND","CTS","FTS","HCM","MBS","DSE","BSI","SHS","VCI","VCK","ORS"]},{name:"XAY DUNG",syms:["C47","C32","L14","CII","CTD","CTI","FCN","HBC","HUT","LCG","PC1","DPG","PHC","VCG"]}]},
  {groups:[{name:"BAT DONG SAN",syms:["VHM","AGG","IJC","LDG","CEO","D2D","DIG","DXG","HDC","HDG","KDH","NLG","NTL","NVL","PDR","SCR","TIG","KBC","SZC"]},{name:"PHAN BON",syms:["BFC","DCM","DPM"]},{name:"THEP",syms:["HPG","HSG","NKG"]}]},
  {groups:[{name:"BAN LE",syms:["MSN","FPT","FRT","MWG","PNJ","DGW"]},{name:"THUY SAN",syms:["ANV","FMC","CMX","VHC","IDI"]},{name:"CANG BIEN",syms:["HAH","GMD","SGP","VSC"]},{name:"CAO SU",syms:["GVR","DPR","DRI","PHR","DRC"]},{name:"NHUA",syms:["AAA","BMP","NTP"]}]},
  {groups:[{name:"DIEN NUOC",syms:["NT2","PC1","GEG","GEX","POW","TDM","BWE"]},{name:"DET MAY",syms:["TCM","TNG","VGT","MSH"]},{name:"HANG KHONG",syms:["NCT","ACV","AST","HVN","SCS","VJC"]},{name:"BAO HIEM",syms:["BMI","MIG","BVH"]},{name:"MIA DUONG",syms:["LSS","SBT","QNS"]}]},
  {groups:[{name:"DAU TU CONG",syms:["FCN","HHV","LCG","VCG","C4G","CTD","HBC","HSG","NKG","HPG","KSB","PLC"]}]},
];
const TS_POOL = ["AAA","ACB","AGG","ANV","BCG","BFC","BID","BMI","BSR","BVB","BVH","BWE","CII","CKG","CRE","CTD","CTG","CTI","CTR","CTS","D2D","DBC","DCM","DSE","DGW","DIG","DPG","DPM","DRC","DRH","DXG","FCN","FMC","FPT","FRT","FTS","GAS","GEG","GEX","GMD","GVR","HAG","HAX","HBC","HCM","HDB","HDC","VCK","HDG","HNG","HPG","HSG","HTN","HVN","IDC","IJC","KBC","KDH","KSB","LCG","LDG","LPB","LTG","MBB","MBS","MSB","MSN","MWG","NKG","NLG","NTL","NVL","PC1","PDR","PET","PHR","PLC","PLX","PNJ","POW","PTB","PVD","PVS","PVT","QNS","REE","SBT","SCR","SHB","SHS","SSI","STB","SZC","TCB","TDM","TIG","TNG","TPB","TV2","VCB","VCI","VCS","VGT","VHC","VHM","VIB","VIC","VJC","VNM","VPB","VRE"];

function hmapCellColor(pct) {
  let r, g, b;
  if      (pct >=  6.5) { r=250; g=170; b=225; }
  else if (pct >=  4.0) { r=160; g=220; b=170; }
  else if (pct >=  2.0) { r=195; g=235; b=200; }
  else if (pct >   0.0) { r=225; g=245; b=228; }
  else if (pct === 0  ) { r=245; g=245; b=200; }
  else if (pct >= -2.0) { r=255; g=220; b=210; }
  else if (pct >= -4.0) { r=250; g=185; b=175; }
  else if (pct >= -6.5) { r=240; g=150; b=145; }
  else                   { r=175; g=250; b=255; }
  const lum = .299*r + .587*g + .114*b;
  return { bg: `rgb(${r},${g},${b})`, fg: lum > 160 ? 'rgb(30,30,30)' : 'rgb(15,15,15)' };
}

function hmapAvgPct(syms, data) {
  const vals = syms.filter(s => data[s]).map(s => data[s].pct || 0);
  return vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : 0;
}

function hmapSortDesc(syms, data) {
  return [...syms].sort((a, b) => ((data[b] || {}).pct || 0) - ((data[a] || {}).pct || 0));
}

function hmapFmtPrice(p) {
  return (!p || p <= 0) ? '—' : p < 100 ? p.toFixed(2) : p.toFixed(1);
}

function hmapMakeCell(sym, data) {
  const d = data[sym] || {};
  const pct = typeof d.pct === 'number' ? d.pct : 0;
  const price = typeof d.price === 'number' ? d.price : 0;
  const { bg, fg } = hmapCellColor(pct);
  const sign = pct >= 0 ? '+' : '';
  return `<div class="hmap-cell" style="background:${bg};color:${fg};" onclick="openChart('${sym}')" title="${sym} | ${hmapFmtPrice(price)} | ${sign}${pct.toFixed(2)}%">
    <span class="hc-sym">${sym}</span>
    <span class="hc-price">${hmapFmtPrice(price)}</span>
    <span class="hc-pct">${sign}${pct.toFixed(1)}%</span>
  </div>`;
}

function hmapMakeGroup(name, syms, data) {
  const sorted = hmapSortDesc(syms, data);
  const avg  = hmapAvgPct(syms, data);
  const sign = avg >= 0 ? '+' : '';
  const cls  = avg > 0.05 ? 'pos' : avg < -0.05 ? 'neg' : 'zer';
  return `<div class="hmap-group">
    <div class="hmap-ghdr">
      <span class="hmap-gname">${name}</span>
      <span class="hmap-gavg ${cls}">${sign}${avg.toFixed(1)}%</span>
    </div>
    ${sorted.map(s => hmapMakeCell(s, data)).join('')}
  </div>`;
}

function renderHeatmap(data) {
  const grid = document.getElementById('hmap-grid');
  if (!data || !Object.keys(data).length) {
    grid.innerHTML = '<div class="empty"><div class="big">🗺</div><div>Chưa có dữ liệu</div></div>';
    return;
  }
  const maxRows = Math.max(...HMAP_COLS.map(cd => cd.groups.reduce((s, g) => s + g.syms.length, 0)));
  const tsSyms = TS_POOL
    .filter(s => data[s] !== undefined)
    .sort((a, b) => ((data[b] || {}).pct || 0) - ((data[a] || {}).pct || 0))
    .slice(0, maxRows);

  const col0 = `<div class="hmap-col">${hmapMakeGroup('TRADING STOCKS', tsSyms, data)}</div>`;
  const rest  = HMAP_COLS.map(cd =>
    `<div class="hmap-col">${cd.groups.map(g => hmapMakeGroup(g.name, g.syms, data)).join('')}</div>`
  ).join('');
  grid.innerHTML = col0 + rest;
}

// ╔══════════════════════════════════════════════════════╗
// ║  TV CHART (LightweightCharts)                        ║
// ║  - Fetch fresh từ /api/ohlcv/* (không dùng cache)   ║
// ║  - Volume overlay trong price panel                  ║
// ║  - MACD histogram x3                                 ║
// ║  - RIGHT_PADDING_BARS = 30                           ║
// ╚══════════════════════════════════════════════════════╝

// Chart instances
let tvPriceChart = null;   // Chart giá + volume overlay
let tvMacdChart  = null;   // Chart MACD riêng

// Series trong price chart
let tvCandleSeries = null;
let tvVolSeries    = null;  // volume histogram overlay bên dưới nến
let tvEma10 = null, tvEma20 = null, tvEma50 = null, tvMa200 = null;
let tvEma30 = null, tvEma100 = null, tvEma200 = null;

// Series trong MACD chart
let tvMacdHist   = null;
let tvMacdLine   = null;
let tvMacdSignal = null;

// State
let tvCurrentSym   = '';
let tvCurrentTF    = 'D';
let tvExtraEMA     = false;
let tvLastCandles  = [];   // dùng cho crosshair legend

const TV_RIGHT_PADDING = 30;

const TV_COLORS = {
  ema10:   '#e02424',
  ema20:   '#059669',
  ema50:   '#7c3aed',
  ma200:   '#92400e',
  ema30:   '#0ea5e9',
  ema100:  '#f97316',
  ema200:  '#64748b',
  macdLine:   '#2563eb',
  macdSignal: '#f97316',
};

// Resize observer
const tvResizeObs = new ResizeObserver(() => tvHandleResize());

function tvHandleResize() {
  const pEl = document.getElementById('tv-price-panel');
  const mEl = document.getElementById('tv-macd-container');
  if (pEl && tvPriceChart) tvPriceChart.applyOptions({ width: pEl.clientWidth, height: pEl.clientHeight });
  if (mEl && tvMacdChart)  tvMacdChart.applyOptions({ width: mEl.clientWidth, height: mEl.clientHeight });
}

function tvDestroyCharts() {
  tvResizeObs.disconnect();
  if (tvPriceChart) { try { tvPriceChart.remove(); } catch(e) {} tvPriceChart = null; }
  if (tvMacdChart)  { try { tvMacdChart.remove();  } catch(e) {} tvMacdChart  = null; }
  tvCandleSeries = tvVolSeries = null;
  tvEma10 = tvEma20 = tvEma50 = tvMa200 = null;
  tvEma30 = tvEma100 = tvEma200 = null;
  tvMacdHist = tvMacdLine = tvMacdSignal = null;
  tvLastCandles = [];
}

// Scroll đến nến cuối, đảm bảo RIGHT_PADDING_BARS bar trống bên phải
function tvScrollToEnd(chart) {
  chart.timeScale().applyOptions({ rightOffset: TV_RIGHT_PADDING });
  chart.timeScale().scrollToRealTime();
}

// Format ngày hiển thị trên legend
function tvFmtDate(candles, tf) {
  if (!candles || !candles.length) return '';
  const last = candles[candles.length - 1];
  const d = new Date(last.time * 1000);
  if (tf === '15') {
    const pad = n => String(n).padStart(2, '0');
    return `${pad(d.getDate())}/${pad(d.getMonth()+1)}/${d.getFullYear()} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }
  // Daily/Weekly: UTC để tránh off-by-one
  const pad = n => String(n).padStart(2, '0');
  return `${pad(d.getUTCDate())}/${pad(d.getUTCMonth()+1)}/${d.getUTCFullYear()}`;
}

// TimeScale options cho chart nào KHÔNG hiển thị nhãn thời gian
function tvTsHidden() {
  return {
    rightOffset:    TV_RIGHT_PADDING,
    timeVisible:    false,
    secondsVisible: false,
    borderVisible:  false,
    ticksVisible:   false,
    visible:        false,
  };
}

// TimeScale options cho MACD (duy nhất có nhãn thời gian)
function tvTsMacd(tf) {
  return {
    rightOffset:    TV_RIGHT_PADDING,
    timeVisible:    true,
    secondsVisible: false,
    borderVisible:  true,
    borderColor:    '#e5e7eb',
    ticksVisible:   true,
    visible:        true,
    tickMarkFormatter: (time) => {
      const d = new Date(time * 1000);
      const pad = n => String(n).padStart(2, '0');
      if (tf === '15') {
        return `${pad(d.getDate())}/${pad(d.getMonth()+1)} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
      }
      return `${pad(d.getUTCDate())}/${pad(d.getUTCMonth()+1)}/${d.getUTCFullYear()}`;
    },
  };
}

function tvBuildCharts(data, tf) {
  const { candles, volume, ema10, ema20, ema50, ma200, ema30, ema100, ema200,
          macd_line: macdLine, macd_signal: macdSig, macd_hist: macdHist } = data;

  tvDestroyCharts();
  tvLastCandles = candles || [];

  const pEl = document.getElementById('tv-price-panel');
  const mEl = document.getElementById('tv-macd-container');

  // ── PRICE CHART (nến + volume overlay) ─────────────────────────────────
  tvPriceChart = LightweightCharts.createChart(pEl, {
    width:  pEl.clientWidth,
    height: pEl.clientHeight,
    layout: {
      background:  { type: 'solid', color: '#ffffff' },
      textColor:   '#374151',
      fontSize:    11,
      fontFamily:  "'IBM Plex Mono',monospace",
    },
    grid: {
      vertLines: { color: 'transparent' },
      horzLines: { color: 'transparent' },
    },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    rightPriceScale: {
      borderColor:     '#e5e7eb',
      // Volume overlay: chiếm 20% dưới (scaleMarginBottom=0.2), nến chiếm 80% trên
      scaleMarginTop:    0.04,
      scaleMarginBottom: 0.20,
    },
    timeScale: tvTsHidden(),
    handleScroll: true,
    handleScale:  true,
  });

  // Nến
  tvCandleSeries = tvPriceChart.addCandlestickSeries({
    upColor:         '#0e9f6e',
    downColor:       '#e02424',
    borderUpColor:   '#0e9f6e',
    borderDownColor: '#e02424',
    wickUpColor:     '#0e9f6e',
    wickDownColor:   '#e02424',
  });
  tvCandleSeries.setData(candles || []);

  // Volume — dùng priceScaleId riêng 'vol' để không ảnh hưởng scale giá nến
  tvVolSeries = tvPriceChart.addHistogramSeries({
    priceFormat:      { type: 'volume' },
    priceLineVisible: false,
    lastValueVisible: false,
    // Dùng overlay scale riêng, chiều cao tự động scale theo dữ liệu vol
    priceScaleId:     'vol',
  });
  // Cấu hình scale vol: chiếm 20% phía dưới chart
  tvPriceChart.priceScale('vol').applyOptions({
    scaleMarginTop:    0.80,
    scaleMarginBottom: 0.0,
    borderVisible:     false,
  });
  tvVolSeries.setData(volume || []);

  // EMAs mặc định
  const addLine = (color, visible = true) => {
    const s = tvPriceChart.addLineSeries({ color, lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false, visible });
    return s;
  };
  tvEma10  = addLine(TV_COLORS.ema10);   tvEma10.setData(ema10   || []);
  tvEma20  = addLine(TV_COLORS.ema20);   tvEma20.setData(ema20   || []);
  tvEma50  = addLine(TV_COLORS.ema50);   tvEma50.setData(ema50   || []);
  tvMa200  = addLine(TV_COLORS.ma200);   tvMa200.setData(ma200   || []);
  tvEma30  = addLine(TV_COLORS.ema30,   tvExtraEMA); tvEma30.setData(ema30   || []);
  tvEma100 = addLine(TV_COLORS.ema100,  tvExtraEMA); tvEma100.setData(ema100 || []);
  tvEma200 = addLine(TV_COLORS.ema200,  tvExtraEMA); tvEma200.setData(ema200 || []);

  tvScrollToEnd(tvPriceChart);

  // ── MACD CHART ─────────────────────────────────────────────────────────
  // Histogram x3: scaleMarginTop nhỏ (0.02) để bar có nhiều không gian vươn lên
  tvMacdChart = LightweightCharts.createChart(mEl, {
    width:  mEl.clientWidth,
    height: mEl.clientHeight,
    layout: {
      background: { type: 'solid', color: '#ffffff' },
      textColor:  '#9ca3af',
      fontSize:   9,
      fontFamily: "'IBM Plex Mono',monospace",
    },
    grid: {
      vertLines: { color: 'transparent' },
      horzLines: { color: 'transparent' },
    },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    rightPriceScale: {
      borderColor:     '#e5e7eb',
      // Margin nhỏ → histogram có chiều cao tối đa, gấp ~3x so với margin lớn
      scaleMarginTop:    0.02,
      scaleMarginBottom: 0.02,
    },
    timeScale: tvTsMacd(tf),
    handleScroll: true,
    handleScale:  true,
  });

  // Histogram trước (dưới cùng layer)
  tvMacdHist = tvMacdChart.addHistogramSeries({
    priceLineVisible: false,
    lastValueVisible: false,
    base: 0,
  });
  tvMacdHist.setData(macdHist || []);

  tvMacdLine = tvMacdChart.addLineSeries({ color: TV_COLORS.macdLine,   lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
  tvMacdLine.setData(macdLine || []);

  tvMacdSignal = tvMacdChart.addLineSeries({ color: TV_COLORS.macdSignal, lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
  tvMacdSignal.setData(macdSig || []);

  tvScrollToEnd(tvMacdChart);

  // ── SYNC timescale 2 chart ──────────────────────────────────────────────
  function syncRange(src, target) {
    src.timeScale().subscribeVisibleLogicalRangeChange(range => {
      if (!range) return;
      try { target.timeScale().setVisibleLogicalRange(range); } catch(e) {}
    });
  }
  syncRange(tvPriceChart, tvMacdChart);
  syncRange(tvMacdChart,  tvPriceChart);

  // Crosshair sync + legend update
  tvPriceChart.subscribeCrosshairMove(param => {
    if (!param.time) return;
    if (tvMacdChart) tvMacdChart.setCrosshairPosition(param.point?.x ?? 0, 0, tvMacdHist);

    // OHLC legend
    const c = param.seriesData.get(tvCandleSeries);
    if (c) {
      document.getElementById('leg-open').textContent  = c.open?.toFixed(2)  || '—';
      document.getElementById('leg-high').textContent  = c.high?.toFixed(2)  || '—';
      document.getElementById('leg-low').textContent   = c.low?.toFixed(2)   || '—';
      document.getElementById('leg-close').textContent = c.close?.toFixed(2) || '—';
      const curIdx = tvLastCandles.findIndex(x => x.time === param.time);
      if (curIdx > 0) {
        const prev = tvLastCandles[curIdx - 1].close;
        const pct  = (c.close - prev) / prev * 100;
        const el   = document.getElementById('leg-pct');
        el.textContent = (pct >= 0 ? '+' : '') + pct.toFixed(2) + '%';
        el.style.color = pct >= 0 ? '#0e9f6e' : '#e02424';
      }
    }
  });

  tvMacdChart.subscribeCrosshairMove(param => {
    if (!param.time) return;
    if (tvPriceChart) tvPriceChart.setCrosshairPosition(param.point?.x ?? 0, 0, tvCandleSeries);
    // MACD legend
    const mh = param.seriesData.get(tvMacdHist);
    const ml = param.seriesData.get(tvMacdLine);
    const ms = param.seriesData.get(tvMacdSignal);
    if (mh !== undefined) {
      const el = document.getElementById('leg-hist');
      el.textContent = typeof mh.value === 'number' ? mh.value.toFixed(4) : '—';
      el.style.color = mh.value >= 0 ? 'rgba(14,159,110,1)' : 'rgba(224,36,36,1)';
    }
    if (ml !== undefined) document.getElementById('leg-macd').textContent   = ml.value?.toFixed(4) || '—';
    if (ms !== undefined) document.getElementById('leg-signal').textContent = ms.value?.toFixed(4) || '—';
  });

  // Resize observer
  tvResizeObs.observe(pEl);
  tvResizeObs.observe(mEl);

  // ── Legend: symbol label + OHLC cuối ───────────────────────────────────
  const tfLabel = tf === '15' ? '15m' : tf;
  document.getElementById('tv-sym-label').textContent = `${data.symbol} [${tfLabel}]  ${tvFmtDate(candles, tf)}`;
  if (candles && candles.length >= 2) {
    const last = candles[candles.length - 1];
    const prev = candles[candles.length - 2];
    document.getElementById('leg-open').textContent  = last.open?.toFixed(2)  || '—';
    document.getElementById('leg-high').textContent  = last.high?.toFixed(2)  || '—';
    document.getElementById('leg-low').textContent   = last.low?.toFixed(2)   || '—';
    document.getElementById('leg-close').textContent = last.close?.toFixed(2) || '—';
    const pct = (last.close - prev.close) / prev.close * 100;
    const pctEl = document.getElementById('leg-pct');
    pctEl.textContent = (pct >= 0 ? '+' : '') + pct.toFixed(2) + '%';
    pctEl.style.color = pct >= 0 ? '#0e9f6e' : '#e02424';
  }
  // MACD legend cuối
  const lHist = macdHist?.at(-1);
  if (lHist) {
    const el = document.getElementById('leg-hist');
    el.textContent = lHist.value?.toFixed(4) || '—';
    el.style.color = lHist.value >= 0 ? 'rgba(14,159,110,1)' : 'rgba(224,36,36,1)';
  }
  if (macdLine?.at(-1))  document.getElementById('leg-macd').textContent   = macdLine.at(-1).value?.toFixed(4)  || '—';
  if (macdSig?.at(-1))   document.getElementById('leg-signal').textContent = macdSig.at(-1).value?.toFixed(4)   || '—';
}

// API URL theo timeframe
function tvApiUrl(sym, tf) {
  if (tf === 'W')  return `/api/ohlcv_weekly/${sym}`;
  if (tf === '15') return `/api/ohlcv_15m/${sym}`;
  return `/api/ohlcv/${sym}`;
}

// Load TV chart (fetch fresh)
async function loadTVChart(sym) {
  sym = sym.toUpperCase().trim();
  tvCurrentSym = sym;

  const loading = document.getElementById('tv-loading');
  const wrap    = document.getElementById('tv-chart-wrap');
  loading.style.display = 'flex';
  wrap.style.display    = 'none';
  loading.innerHTML     = `⏳ Đang tải dữ liệu  <b>${sym}</b> [${tvCurrentTF === '15' ? '15m' : tvCurrentTF}]...`;

  try {
    const r = await fetch(tvApiUrl(sym, tvCurrentTF));
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      throw new Error(j.error || `HTTP ${r.status}`);
    }
    const d = await r.json();
    if (!d.candles || d.candles.length < 2) throw new Error('Không đủ dữ liệu');

    tvBuildCharts(d, tvCurrentTF);

    loading.style.display = 'none';
    wrap.style.display    = 'flex';

    // Sau khi wrap hiển thị, resize + scroll lại để layout đúng
    requestAnimationFrame(() => {
      tvHandleResize();
      if (tvPriceChart) tvScrollToEnd(tvPriceChart);
      if (tvMacdChart)  tvScrollToEnd(tvMacdChart);
    });
  } catch (err) {
    loading.innerHTML = `
      <div style="text-align:center;color:#9ca3af;padding:24px">
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

// Switch timeframe
async function tvSwitchTF(tf) {
  if (tf === tvCurrentTF) return;
  tvCurrentTF = tf;
  ['D', 'W', '15'].forEach(t =>
    document.getElementById('tf-' + t).classList.toggle('on', t === tf)
  );
  const loadEl = document.getElementById('tv-tf-loading');
  loadEl.style.display = 'inline';
  try {
    const r = await fetch(tvApiUrl(tvCurrentSym, tf));
    if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.error || `HTTP ${r.status}`); }
    const d = await r.json();
    if (!d.candles || d.candles.length < 2) throw new Error('Không đủ dữ liệu');
    tvBuildCharts(d, tf);
  } catch (err) {
    console.error('tvSwitchTF:', err);
    loadEl.textContent = `❌ ${err.message}`;
    setTimeout(() => { loadEl.textContent = '⏳ đang tải...'; loadEl.style.display = 'none'; }, 3000);
    return;
  }
  loadEl.style.display = 'none';
}

// Toggle EMA phụ
function tvToggleExtraEMA(checked) {
  tvExtraEMA = checked;
  [tvEma30, tvEma100, tvEma200].forEach(s => {
    if (s) try { s.applyOptions({ visible: checked }); } catch(e) {}
  });
}

// Gõ mã trực tiếp trong TV Chart
let tvTickerBuf   = '';
let tvTickerTimer = null;
const tvTickerOverlay = document.getElementById('tv-ticker-overlay');

function tvTickerShow(s) { tvTickerOverlay.textContent = s; tvTickerOverlay.style.display = 'block'; }
function tvTickerHide()  { tvTickerOverlay.style.display = 'none'; tvTickerBuf = ''; }

document.addEventListener('keydown', e => {
  const tag = document.activeElement?.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA') return;
  if (!document.getElementById('overlay').classList.contains('on')) return;
  if (popupCurrentTab !== 'tv') return;

  const key = e.key;
  if (/^[A-Za-z0-9.]$/.test(key)) {
    e.preventDefault();
    tvTickerBuf += key.toUpperCase();
    tvTickerShow(tvTickerBuf);
    clearTimeout(tvTickerTimer);
    tvTickerTimer = setTimeout(() => {
      const sym = tvTickerBuf.trim();
      tvTickerHide();
      if (sym.length >= 2) openChart(sym);
    }, 1200);
    return;
  }
  if (key === 'Enter' && tvTickerBuf.length >= 2) {
    e.preventDefault();
    clearTimeout(tvTickerTimer);
    const sym = tvTickerBuf.trim();
    tvTickerHide();
    openChart(sym);
    return;
  }
  if (key === 'Backspace' && tvTickerBuf.length > 0) {
    e.preventDefault();
    tvTickerBuf = tvTickerBuf.slice(0, -1);
    if (!tvTickerBuf.length) { tvTickerHide(); clearTimeout(tvTickerTimer); }
    else tvTickerShow(tvTickerBuf);
    return;
  }
  if (key === 'Escape' && tvTickerBuf.length > 0) {
    e.preventDefault();
    tvTickerHide();
    clearTimeout(tvTickerTimer);
  }
});

// ╔══════════════════════════════════════════════════════╗
// ║  SCANNER CHART (PNG album)                           ║
// ╚══════════════════════════════════════════════════════╝
let albumIdx = 0, albumTotal = 0, albumImages = [];

function showAlbum(images) {
  albumImages = images;
  const slidesEl = document.getElementById('album-slides');
  const dotsEl   = document.getElementById('album-dots');
  slidesEl.innerHTML = '';
  dotsEl.innerHTML   = '';
  albumTotal = images.length;
  const isMobile = window.innerWidth <= 768;
  images.forEach((img, i) => {
    const clickAttr = isMobile ? `onclick="lbOpen(albumImages,${i})" style="cursor:zoom-in"` : '';
    slidesEl.innerHTML += `<div class="album-slide${i === 0 ? ' on' : ''}" id="slide-${i}"><img src="${img.url}" alt="${img.label}" loading="lazy" ${clickAttr}></div>`;
    dotsEl.innerHTML   += `<div class="album-dot${i === 0 ? ' on' : ''}" id="dot-${i}" onclick="albumGoto(${i})"></div>`;
  });
  albumIdx = 0;
  updateAlbumNav();
  document.getElementById('album-outer').style.display   = 'flex';
  document.getElementById('scanner-loading').style.display = 'none';
}

function albumGoto(i) {
  if (i < 0 || i >= albumTotal) return;
  document.querySelectorAll('.album-slide').forEach((s, idx) => s.classList.toggle('on', idx === i));
  document.querySelectorAll('.album-dot').forEach((d, idx) => d.classList.toggle('on', idx === i));
  albumIdx = i;
  updateAlbumNav();
}

function albumNav(dir) { albumGoto(albumIdx + dir); }

function updateAlbumNav() {
  document.getElementById('btn-prev').classList.toggle('disabled', albumIdx === 0);
  document.getElementById('btn-next').classList.toggle('disabled', albumIdx === albumTotal - 1);
}

async function loadScannerChart(sym) {
  document.getElementById('album-outer').style.display    = 'none';
  document.getElementById('scanner-loading').style.display = 'flex';
  document.getElementById('scanner-loading').innerHTML    = `<span>⏳ Đang tạo chart <b>${sym}</b>… (5–10 giây)</span>`;
  try {
    const r = await fetch(`/api/chart_images/${sym}`);
    if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.error || `HTTP ${r.status}`); }
    const j = await r.json();
    if (j.images && j.images.length > 0) {
      const labels = j.labels || ['📊 Daily [D]', '📈 Weekly [W]', '⚡ 15m'];
      showAlbum(j.images.map((b64, i) => ({
        url:   `data:image/png;base64,${b64}`,
        label: labels[i] || `Chart ${i + 1}`,
      })));
      return;
    }
    throw new Error('no_images');
  } catch (e) {
    document.getElementById('scanner-loading').innerHTML = `
      <div style="text-align:center;color:#aaa;padding:24px">
        <div style="font-size:24px;margin-bottom:10px">⚠️</div>
        <div style="margin-bottom:8px">Không tải được chart <b style="color:#4d9ff5">${sym}</b></div>
        <div style="font-size:11px;color:#666;margin-bottom:16px">${e.message}</div>
        <div style="display:flex;gap:8px;justify-content:center;flex-wrap:wrap">
          <button onclick="loadScannerChart('${sym}')" style="padding:6px 14px;border-radius:5px;background:#1a56db;color:#fff;border:none;cursor:pointer;font-size:12px">🔄 Thử lại</button>
          <a href="https://ta.vietstock.vn/?stockcode=${sym.toLowerCase()}" target="_blank" style="padding:6px 14px;border-radius:5px;background:#374151;color:#fff;text-decoration:none;font-size:12px">📈 Stockchart</a>
        </div>
      </div>`;
  }
}

async function refreshScannerChart() {
  if (!popupCurrentSym) return;
  const btn = document.getElementById('btn-refresh');
  if (btn) { btn.classList.add('spinning'); btn.disabled = true; }
  try { await fetch(`/api/chart_cache_clear/${popupCurrentSym}`, { method: 'DELETE' }).catch(() => {}); } catch(e) {}
  if (btn) { btn.classList.remove('spinning'); btn.disabled = false; }
  await loadScannerChart(popupCurrentSym);
}

// Album keyboard
document.addEventListener('keydown', e => {
  const overlayOn = document.getElementById('overlay').classList.contains('on');
  if (!overlayOn || popupCurrentTab !== 'scanner' || albumTotal === 0) return;
  if (e.key === 'ArrowLeft')  { e.preventDefault(); albumNav(-1); }
  if (e.key === 'ArrowRight') { e.preventDefault(); albumNav(1);  }
});

// Album touch swipe (desktop)
let albumTouchStartX = 0;
document.getElementById('panel-scanner').addEventListener('touchstart', e => {
  if (window.innerWidth > 768) albumTouchStartX = e.touches[0].clientX;
}, { passive: true });
document.getElementById('panel-scanner').addEventListener('touchend', e => {
  if (window.innerWidth > 768) {
    const dx = e.changedTouches[0].clientX - albumTouchStartX;
    if (Math.abs(dx) > 50) albumNav(dx < 0 ? 1 : -1);
  }
}, { passive: true });

// ╔══════════════════════════════════════════════════════╗
// ║  POPUP STATE & NAVIGATION                            ║
// ╚══════════════════════════════════════════════════════╝
let popupCurrentSym = '';
let popupCurrentTab = 'tv';

const ALL_TABS = ['tv', 'vs', 'scanner', 'vnd-cs', 'vnd-news', 'vnd-sum', '24h', 'url'];
const IFRAME_TABS = ['vs', 'vnd-cs', 'vnd-news', 'vnd-sum', '24h', 'url'];

// Iframe URLs theo tab
function iframeUrlFor(tab, sym) {
  const urls = {
    'vs':       `https://ta.vietstock.vn/?stockcode=${sym.toLowerCase()}`,
    'vnd-cs':   `https://dstock.vndirect.com.vn/tong-quan/${sym}/diem-nhan-co-ban-popup?theme=light`,
    'vnd-news': `https://dstock.vndirect.com.vn/tong-quan/${sym}/tin-tuc-ma-popup?type=dn&theme=light`,
    'vnd-sum':  `https://dstock.vndirect.com.vn/tong-quan/${sym}?theme=light`,
    '24h':      `https://24hmoney.vn/stock/${sym}/news`,
  };
  return urls[tab] || null;
}

function activateTab(tab) {
  popupCurrentTab = tab;
  ALL_TABS.forEach(t => {
    const ct = document.getElementById('ctab-' + t);
    if (ct) ct.classList.toggle('on', t === tab);
    document.getElementById('panel-' + t).classList.toggle('on', t === tab);
  });

  // Mobile tab bar highlight
  if (window.innerWidth <= 768) {
    syncMobileTabBar(tab);
  }

  // Load nội dung theo tab
  if (tab === 'tv') {
    loadTVChart(popupCurrentSym);
  } else if (IFRAME_TABS.includes(tab) && tab !== 'url') {
    const f = document.getElementById('iframe-' + tab);
    if (f && f.src === 'about:blank') {
      const url = iframeUrlFor(tab, popupCurrentSym);
      if (url) f.src = url;
    }
  } else if (tab === 'scanner') {
    loadScannerChart(popupCurrentSym);
  }
}

function switchTab(tab) { activateTab(tab); }

function openChart(sym) {
  popupCurrentSym = sym.toUpperCase().trim();
  popupCurrentTab = 'tv';

  document.getElementById('ptitle').textContent = `📊 ${popupCurrentSym}`;
  // Reset iframes
  IFRAME_TABS.forEach(t => { document.getElementById('iframe-' + t).src = 'about:blank'; });
  // Reset scanner
  document.getElementById('album-outer').style.display    = 'none';
  document.getElementById('scanner-loading').style.display = 'flex';
  document.getElementById('scanner-loading').innerHTML    = '<span>⏳ Đang tạo chart từ scanner...</span>';

  buildMobileHeaderIfNeeded();
  activateTab('tv');

  document.getElementById('overlay').classList.add('on');
  document.body.style.overflow = 'hidden';
  document.getElementById('edge-swipe-zone').classList.add('on');
  document.getElementById('mob-close-float').style.display = '';

  tvTickerBuf = '';
  tvTickerHide();
}

function openUrl(url, label) {
  popupCurrentSym = label || 'URL';
  popupCurrentTab = 'url';

  document.getElementById('ptitle').textContent = label || '🌐 Web';
  IFRAME_TABS.forEach(t => { document.getElementById('iframe-' + t).src = 'about:blank'; });

  buildMobileHeaderIfNeeded();

  ALL_TABS.forEach(t => {
    const ct = document.getElementById('ctab-' + t);
    if (ct) ct.classList.toggle('on', t === 'url');
    document.getElementById('panel-' + t).classList.toggle('on', t === 'url');
  });
  document.getElementById('iframe-url').src = url;

  document.getElementById('overlay').classList.add('on');
  document.body.style.overflow = 'hidden';
  document.getElementById('edge-swipe-zone').classList.add('on');
  document.getElementById('mob-close-float').style.display = '';
}

function closePopup() {
  tvDestroyCharts();
  tvTickerHide();
  clearTimeout(tvTickerTimer);

  const overlay = document.getElementById('overlay');
  const pbox    = overlay.querySelector('.pbox');
  pbox.style.visibility = 'hidden';
  IFRAME_TABS.forEach(t => { document.getElementById('iframe-' + t).src = 'about:blank'; });
  pbox.style.animation = 'none';
  overlay.classList.remove('on');
  document.body.style.overflow = '';
  document.getElementById('edge-swipe-zone').classList.remove('on');
  document.getElementById('mob-close-float').style.display = 'none';
  requestAnimationFrame(() => { pbox.style.visibility = ''; pbox.style.animation = ''; });
}

// Click backdrop to close
document.getElementById('overlay').addEventListener('click', e => {
  if (e.target === document.getElementById('overlay')) closePopup();
});

// ESC to close
document.addEventListener('keydown', e => {
  if (lb.el && lb.el.classList.contains('on')) return;
  if (e.key === 'Escape' && tvTickerBuf.length === 0) {
    if (document.getElementById('overlay').classList.contains('on')) closePopup();
  }
});

// ╔══════════════════════════════════════════════════════╗
// ║  SEARCH BOX IN POPUP HEADER                          ║
// ╚══════════════════════════════════════════════════════╝
(function () {
  const inp = document.getElementById('phdr-search-input');
  inp.addEventListener('keydown', function(e) {
    e.stopPropagation(); // đừng để trigger TV ticker
    if (e.key === 'Enter') {
      const sym = this.value.trim().toUpperCase();
      if (sym.length >= 2) { this.value = ''; this.blur(); openChart(sym); }
    }
    if (e.key === 'Escape') { this.value = ''; this.blur(); }
  });
  inp.addEventListener('focus', function() { this.select(); });
})();

// Search bar ngoài heatmap
(function () {
  const inp = document.getElementById('hmap-search-input');
  inp.addEventListener('keydown', function(e) {
    if (e.key === 'Enter') {
      const sym = this.value.trim().toUpperCase();
      if (sym.length >= 2) { this.value = ''; this.blur(); openChart(sym); }
    }
    if (e.key === 'Escape') { this.value = ''; this.blur(); }
  });
  inp.addEventListener('focus', function() { this.select(); });
})();

// ╔══════════════════════════════════════════════════════╗
// ║  MOBILE: header + tab bar                            ║
// ╚══════════════════════════════════════════════════════╝
let mobileHeaderBuilt = false;

function buildMobileHeaderIfNeeded() {
  if (window.innerWidth > 768 || mobileHeaderBuilt) return;
  mobileHeaderBuilt = true;

  const phdr = document.querySelector('.phdr');
  phdr.style.cssText = 'display:flex;flex-direction:column;flex-shrink:0;';
  phdr.innerHTML = '';

  // Row 1: title + close
  const r1 = document.createElement('div');
  r1.style.cssText = 'display:flex;align-items:center;gap:8px;padding:8px 12px;background:var(--surf2);border-bottom:1px solid var(--border);';
  r1.innerHTML = `
    <span id="ptitle" style="font-family:var(--font-ui);font-size:17px;font-weight:800;color:var(--accent);letter-spacing:1px;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">Chart</span>
    <button class="closebtn" onclick="closePopup()">✕</button>`;
  phdr.appendChild(r1);

  // Row 2: tab bar
  const r2 = document.createElement('div');
  r2.id = 'mob-tabrow';
  const tabs = [
    { id: 'tv',       label: '📊 TV Chart'  },
    { id: 'vs',       label: '📈 Vietstock' },
    { id: 'scanner',  label: '🖼 Scanner'   },
    { id: 'vnd-cs',   label: '⚖️ Cơ bản'   },
    { id: 'vnd-news', label: '🗞️ Tin tức'  },
    { id: 'vnd-sum',  label: '📄 Tổng quan' },
    { id: '24h',      label: '💬 24HMoney'  },
  ];
  tabs.forEach(t => {
    const btn = document.createElement('button');
    btn.id = 'ctab-' + t.id;
    btn.textContent = t.label;
    if (t.id === 'tv') btn.classList.add('on');
    btn.onclick = () => switchTab(t.id);
    r2.appendChild(btn);
  });
  phdr.appendChild(r2);
}

function syncMobileTabBar(tab) {
  const row = document.getElementById('mob-tabrow');
  const activeBtn = document.getElementById('ctab-' + tab);
  if (!row || !activeBtn) return;
  row.querySelectorAll('button').forEach(btn => {
    btn.classList.toggle('on', btn.id === 'ctab-' + tab);
  });
  // Scroll tab vào view
  const btnLeft  = activeBtn.offsetLeft;
  const btnWidth = activeBtn.offsetWidth;
  const rowWidth = row.offsetWidth;
  row.scrollTo({ left: btnLeft - (rowWidth / 2) + (btnWidth / 2), behavior: 'smooth' });
}

// Mobile swipe từ cạnh trái để đóng popup
(function() {
  if (window.innerWidth > 768) return;
  const pbox = document.querySelector('.pbox');
  let startX = 0, fired = false;
  pbox.addEventListener('touchstart', e => {
    if (!document.getElementById('overlay').classList.contains('on')) return;
    if (lb.el && lb.el.classList.contains('on')) return;
    if (e.touches[0].clientX > 40) return;
    startX = e.touches[0].clientX; fired = false;
  }, { passive: true });
  pbox.addEventListener('touchmove', e => {
    if (fired) return;
    const dx = e.touches[0].clientX - startX;
    if (dx > 40) { fired = true; closePopup(); }
  }, { passive: true });
})();

// ╔══════════════════════════════════════════════════════╗
// ║  MOBILE LIGHTBOX                                     ║
// ╚══════════════════════════════════════════════════════╝
const lb = {
  el: null, stripEl: null, labelEl: null, counterEl: null,
  images: [], idx: 0, W: 0,
  dragStartX: 0, dragStartY: 0, dragDx: 0, dragDy: 0,
  dragging: false, dragDir: '', stripOffset: 0,
  scale: 1, scaleMin: 1, scaleMax: 4, panX: 0, panY: 0,
  isPinching: false, pinchStartDist: 0, pinchStartScale: 1,
  pinchMidX: 0, pinchMidY: 0, pinchStartPanX: 0, pinchStartPanY: 0,
  isPanning: false, panStartX: 0, panStartY: 0, panStartPanX: 0, panStartPanY: 0,
  lastTapTime: 0, lastTapX: 0, lastTapY: 0,
};

function lbCurrentImg() {
  const slides = lb.stripEl?.querySelectorAll('.lb-slide img') || [];
  return slides[lb.idx] || null;
}
function lbResetZoom(animate) {
  lb.scale = 1; lb.panX = 0; lb.panY = 0;
  const img = lbCurrentImg();
  if (!img) return;
  if (animate) img.classList.remove('zooming'); else img.classList.add('zooming');
  img.style.transform = 'translate(0px,0px) scale(1)';
}
function lbApplyZoom() {
  const img = lbCurrentImg();
  if (!img) return;
  img.classList.add('zooming');
  img.style.transform = `translate(${lb.panX}px,${lb.panY}px) scale(${lb.scale})`;
}
function lbClampPan() {
  const img = lbCurrentImg();
  if (!img || lb.scale <= 1) { lb.panX = 0; lb.panY = 0; return; }
  const rect = img.getBoundingClientRect();
  const W = window.innerWidth, H = window.innerHeight;
  const iW = (rect.width / lb.scale) * lb.scale;
  const iH = (rect.height / lb.scale) * lb.scale;
  const maxPanX = Math.max(0, (iW - W) / 2);
  const maxPanY = Math.max(0, (iH - H) / 2);
  lb.panX = Math.max(-maxPanX, Math.min(maxPanX, lb.panX));
  lb.panY = Math.max(-maxPanY, Math.min(maxPanY, lb.panY));
}

function lbInit() {
  lb.el       = document.getElementById('mob-lightbox');
  lb.stripEl  = document.getElementById('lb-strip');
  lb.labelEl  = document.getElementById('mob-lightbox-label');
  lb.counterEl= document.getElementById('mob-lightbox-counter');
  const vp = document.getElementById('lb-viewport');
  vp.addEventListener('touchstart', lbOnTouchStart, { passive: false });
  vp.addEventListener('touchmove',  lbOnTouchMove,  { passive: false });
  vp.addEventListener('touchend',   lbOnTouchEnd,   { passive: false });
  vp.addEventListener('touchcancel',lbOnTouchCancel,{ passive: true  });
}

function lbOpen(images, idx) {
  if (!lb.el) lbInit();
  lb.images = images; lb.idx = idx; lb.W = window.innerWidth;
  lb.stripEl.innerHTML = images.map((img, i) =>
    `<div class="lb-slide"><img src="${img.url}" alt="${img.label}" draggable="false"></div>`
  ).join('');
  lb.stripEl.style.width = `${lb.W * images.length}px`;
  lb.scale = 1; lb.panX = 0; lb.panY = 0;
  lbSnapTo(idx, false);
  lbUpdateMeta();
  lb.el.classList.add('on');
  document.body.style.overflow = 'hidden';
}
function lbClose() {
  if (!lb.el) return;
  lbResetZoom(false);
  lb.el.classList.remove('on');
  document.body.style.overflow = '';
}
function lbSnapTo(idx, animate) {
  lb.scale = 1; lb.panX = 0; lb.panY = 0;
  const prevImg = lbCurrentImg();
  if (prevImg) { prevImg.classList.add('zooming'); prevImg.style.transform = 'translate(0,0) scale(1)'; }
  lb.idx = Math.max(0, Math.min(idx, lb.images.length - 1));
  const target = -lb.idx * lb.W;
  lb.stripOffset = target;
  if (animate) {
    lb.stripEl.classList.add('snapping');
    lb.stripEl.style.transform = `translateX(${target}px)`;
    setTimeout(() => lb.stripEl.classList.remove('snapping'), 350);
  } else {
    lb.stripEl.classList.remove('snapping');
    lb.stripEl.style.transform = `translateX(${target}px)`;
  }
  lb.lastTapTime = 0;
  lbUpdateMeta();
}
function lbUpdateMeta() {
  if (!lb.images.length) return;
  lb.labelEl.textContent  = lb.images[lb.idx].label;
  lb.counterEl.innerHTML  = lb.images.map((_, i) =>
    `<div class="mob-lb-dot${i === lb.idx ? ' on' : ''}"></div>`
  ).join('');
}
function lbPinchDist(t) { const dx = t[0].clientX - t[1].clientX, dy = t[0].clientY - t[1].clientY; return Math.sqrt(dx*dx + dy*dy); }
function lbPinchMid(t)  { return { x: (t[0].clientX + t[1].clientX) / 2, y: (t[0].clientY + t[1].clientY) / 2 }; }

function lbOnTouchStart(e) {
  if (e.touches.length === 2) {
    e.preventDefault();
    lb.isPinching = true; lb.dragging = false;
    lb.pinchStartDist  = lbPinchDist(e.touches);
    lb.pinchStartScale = lb.scale;
    lb.pinchStartPanX  = lb.panX; lb.pinchStartPanY = lb.panY;
    const mid = lbPinchMid(e.touches); lb.pinchMidX = mid.x; lb.pinchMidY = mid.y;
    return;
  }
  if (e.touches.length !== 1) return;
  const now = Date.now(), tx = e.touches[0].clientX, ty = e.touches[0].clientY;
  const dt = now - lb.lastTapTime, dd = Math.hypot(tx - lb.lastTapX, ty - lb.lastTapY);
  if (dt < 300 && dd < 40) { e.preventDefault(); lbDoubleTap(tx, ty); lb.lastTapTime = 0; return; }
  lb.lastTapTime = now; lb.lastTapX = tx; lb.lastTapY = ty;
  if (lb.scale > 1.05) {
    lb.isPanning = true; lb.panStartX = tx; lb.panStartY = ty;
    lb.panStartPanX = lb.panX; lb.panStartPanY = lb.panY;
    lb.dragging = false; return;
  }
  lb.dragging = true; lb.isPanning = false; lb.dragDir = '';
  lb.dragDx = 0; lb.dragDy = 0; lb.dragStartX = tx; lb.dragStartY = ty;
  lb.stripEl.classList.remove('snapping');
}

function lbOnTouchMove(e) {
  if (lb.isPinching && e.touches.length === 2) {
    e.preventDefault();
    const ratio = lbPinchDist(e.touches) / lb.pinchStartDist;
    lb.scale = Math.min(lb.scaleMax, Math.max(lb.scaleMin, lb.pinchStartScale * ratio));
    lb.panX = lb.pinchStartPanX; lb.panY = lb.pinchStartPanY;
    lbClampPan(); lbApplyZoom(); return;
  }
  if (e.touches.length !== 1) return;
  const tx = e.touches[0].clientX, ty = e.touches[0].clientY;
  if (lb.isPanning) {
    e.preventDefault();
    lb.panX = lb.panStartPanX + (tx - lb.panStartX);
    lb.panY = lb.panStartPanY + (ty - lb.panStartY);
    lbClampPan(); lbApplyZoom(); return;
  }
  if (!lb.dragging) return;
  const dx = tx - lb.dragStartX, dy = ty - lb.dragStartY;
  if (!lb.dragDir && (Math.abs(dx) > 6 || Math.abs(dy) > 6))
    lb.dragDir = Math.abs(dy) > Math.abs(dx) ? 'v' : 'h';
  if (!lb.dragDir) return;
  e.preventDefault();
  if (lb.dragDir === 'v') {
    lb.dragDy = dy;
    const pullDown = Math.max(0, dy);
    lb.el.style.opacity = Math.max(0, 1 - pullDown / 280);
    lb.stripEl.style.transform = `translateX(${lb.stripOffset}px) translateY(${pullDown * .6}px) scale(${Math.max(.85, 1 - pullDown / 900)})`;
    return;
  }
  lb.dragDx = dx;
  let offset = lb.stripOffset + dx;
  const maxOffset = 0, minOffset = -(lb.images.length - 1) * lb.W;
  if (offset > maxOffset) offset = dx * .3;
  if (offset < minOffset) offset = minOffset + (offset - minOffset) * .3;
  lb.stripEl.style.transform = `translateX(${offset}px)`;
}

function lbOnTouchEnd(e) {
  if (lb.isPinching) {
    lb.isPinching = false;
    if (lb.scale < 1.1) lbResetZoom(true);
    else { const img = lbCurrentImg(); if (img) img.classList.remove('zooming'); }
    return;
  }
  if (lb.isPanning) {
    lb.isPanning = false;
    const img = lbCurrentImg(); if (img) img.classList.remove('zooming');
    return;
  }
  if (!lb.dragging) return;
  lb.dragging = false;
  if (lb.dragDir === 'v') {
    const pullDown = Math.max(0, lb.dragDy);
    if (pullDown > 80) {
      lb.stripEl.style.transition = 'transform .22s ease';
      lb.el.style.transition      = 'opacity .22s ease';
      lb.stripEl.style.transform  = `translateX(${lb.stripOffset}px) translateY(100vh) scale(.9)`;
      lb.el.style.opacity         = '0';
      setTimeout(() => {
        lb.stripEl.style.transition = '';
        lb.el.style.transition      = '';
        lb.stripEl.style.transform  = `translateX(${lb.stripOffset}px)`;
        lb.el.style.opacity         = '';
        lbClose();
      }, 230);
    } else {
      lb.stripEl.style.transition = 'transform .22s ease';
      lb.el.style.transition      = 'opacity .15s ease';
      lb.stripEl.style.transform  = `translateX(${lb.stripOffset}px)`;
      lb.el.style.opacity         = '1';
      setTimeout(() => { lb.stripEl.style.transition = ''; lb.el.style.transition = ''; }, 230);
    }
    lb.dragDy = 0; lb.dragDir = ''; return;
  }
  const dx = lb.dragDx, absX = Math.abs(dx), THRESHOLD = lb.W * .25;
  let nextIdx = lb.idx;
  if      (absX > THRESHOLD && dx < 0) nextIdx = lb.idx + 1;
  else if (absX > THRESHOLD && dx > 0) nextIdx = lb.idx - 1;
  else if (absX > 80 && dx < 0 && lb.idx < lb.images.length - 1) nextIdx = lb.idx + 1;
  else if (absX > 80 && dx > 0 && lb.idx > 0) nextIdx = lb.idx - 1;
  lbSnapTo(nextIdx, true);
  lb.dragDx = 0; lb.dragDir = '';
}

function lbOnTouchCancel() { lb.isPinching = false; lb.isPanning = false; lb.dragging = false; }

function lbDoubleTap(tapX, tapY) {
  if (lb.scale > 1.05) { lbResetZoom(true); return; }
  lb.scale = 2.5;
  const W = window.innerWidth, H = window.innerHeight;
  lb.panX = (W / 2 - tapX) * (lb.scale - 1);
  lb.panY = (H / 2 - tapY) * (lb.scale - 1);
  lbClampPan();
  const img = lbCurrentImg();
  if (img) { img.classList.remove('zooming'); img.style.transform = `translate(${lb.panX}px,${lb.panY}px) scale(${lb.scale})`; }
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && lb.el && lb.el.classList.contains('on')) lbClose();
});

// ╔══════════════════════════════════════════════════════╗
// ║  CLOCK + SIGNAL/HEATMAP FETCH + PROGRESS BAR         ║
// ╚══════════════════════════════════════════════════════╝
function updateClock() {
  const n = new Date();
  document.getElementById('clock').textContent =
    n.toLocaleTimeString('vi-VN', { hour12: false }) + '  ' +
    n.toLocaleDateString('vi-VN');
}
setInterval(updateClock, 1000);
updateClock();

function badgeCls(sig) {
  return ({
    'BREAKOUT':     'b-BREAKOUT',
    'POCKET PIVOT': 'b-POCKET',
    'PRE-BREAK':    'b-PREBREAK',
    'BOTTOMBREAKP': 'b-BBREAKP',
    'BOTTOMFISH':   'b-BFISH',
    'MA_CROSS':     'b-MACROSS',
  })[sig] || 'b-MACROSS';
}

async function fetchSignals() {
  try {
    const j = await fetch('/api/signals').then(r => r.json());
    document.getElementById('sig-meta').textContent =
      `Cập nhật ${j.updated_at}  •  ${j.count} tín hiệu  •  click để xem chart`;
    const el = document.getElementById('sig-list');
    if (!j.signals.length) {
      el.innerHTML = '<div class="empty"><div class="big">💤</div><div>Chưa có tín hiệu nào hôm nay</div></div>';
      return;
    }
    el.innerHTML = j.signals.map(s => `
      <div class="sig-row" onclick="openChart('${s.symbol}')">
        <span class="s-emoji">${s.emoji}</span>
        <span class="s-sym">${s.symbol}</span>
        <span class="s-type" style="font-weight:600;color:${s.pct >= 0 ? '#0e9f6e' : '#e02424'}">
          ${s.pct != null ? (s.pct >= 0 ? '+' : '') + s.pct + '%' : '—'}
        </span>
        <span class="s-badge ${badgeCls(s.signal)}">
          ${s.signal.replace('POCKET PIVOT', 'PIVOT').replace('PRE-BREAK', 'PRE')}
        </span>
      </div>`).join('');
  } catch (e) { console.error('fetchSignals:', e); }
}

async function fetchHeatmap() {
  try {
    const j   = await fetch('/api/heatmap').then(r => r.json());
    const now = new Date().toLocaleTimeString('vi-VN', { hour12: false });
    const isMob = window.innerWidth <= 768;
    document.getElementById('hmap-ts').textContent = isMob
      ? `Data: ${j.timestamp || '--'}  •  Cập nhật: ${now}`
      : `Data: ${j.timestamp || '--'}  •  Cập nhật: ${now}  •  click để xem chart`;
    renderHeatmap(j.data || {});
  } catch (e) { console.error('fetchHeatmap:', e); }
}

function startProgressBar(id, sec) {
  const el = document.getElementById(id);
  if (!el) return;
  el.style.transition = 'none';
  el.style.width      = '0%';
  requestAnimationFrame(() => requestAnimationFrame(() => {
    el.style.transition = `width ${sec}s linear`;
    el.style.width      = '100%';
  }));
}

// ╔══════════════════════════════════════════════════════╗
// ║  INIT                                                ║
// ╚══════════════════════════════════════════════════════╝
async function init() {
  await loadConfig();
  startProgressBar('pbar-sig',  SIG_TTL);
  startProgressBar('pbar-hmap', HMAP_TTL);
  await Promise.all([fetchSignals(), fetchHeatmap()]);
  setInterval(async () => { startProgressBar('pbar-sig',  SIG_TTL);  await fetchSignals();  }, SIG_TTL  * 1000);
  setInterval(async () => { startProgressBar('pbar-hmap', HMAP_TTL); await fetchHeatmap(); }, HMAP_TTL * 1000);
}
init();
</script>
</body>
</html>
"""
