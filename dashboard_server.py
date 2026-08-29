"""
DASHBOARD SERVER
"""

from flask import Flask, jsonify, Response, request, session, send_from_directory
from functools import wraps
from io import BytesIO
from pathlib import Path
import gzip
import hmac
import json
import math
import os
import requests
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import pytz
import pandas as pd
import numpy as np

TZ_VN = pytz.timezone('Asia/Ho_Chi_Minh')
app = Flask(__name__)
app.secret_key = os.environ.get("DASHBOARD_SECRET_KEY", "change-this-dashboard-secret-key")
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024

_GZIP_MIN_BYTES = 500

@app.after_request
def _static_cache_headers(response):
    """File tĩnh /static cache cứng 7 ngày (đổi version lib thì đổi tên file).
    Riêng icon/manifest đổi thường xuyên hơn nên dùng no-cache, revalidate qua ETag."""
    if request.path.startswith("/static/"):
        _ICON_STATIC_FILES = (
            "favicon-32.png", "favicon-16.png", "favicon.ico",
            "apple-touch-icon.png", "icon-192.png", "icon-512.png",
            "manifest.json",
        )
        if request.path.rsplit("/", 1)[-1] in _ICON_STATIC_FILES:
            response.headers["Cache-Control"] = "no-cache"
        else:
            response.headers["Cache-Control"] = "public, max-age=604800, immutable"
    return response

@app.route("/favicon.ico")
def _serve_root_favicon():
    """Safari tự dò GET /favicon.ico ở gốc domain, độc lập thẻ <link rel="icon">.
    Path này không đi qua hook _static_cache_headers nên set no-cache thủ công."""
    resp = send_from_directory(app.static_folder, "favicon.ico")
    resp.headers["Cache-Control"] = "no-cache"
    return resp

_LWC_JS_CACHE = None

@app.route("/static/lightweight-charts.min.js")
def _serve_lwc_js():
    """Đè route /static mặc định: send_from_directory dùng direct_passthrough
    khiến hook _gzip_response bỏ qua. Đọc file vào RAM 1 lần để nén được."""
    global _LWC_JS_CACHE
    if _LWC_JS_CACHE is None:
        with open(os.path.join(app.static_folder, "lightweight-charts.min.js"), "rb") as f:
            _LWC_JS_CACHE = f.read()
    return Response(_LWC_JS_CACHE, content_type="application/javascript; charset=utf-8")

@app.after_request
def _gzip_response(response):
    """Nén gzip response JSON/HTML khi trình duyệt hỗ trợ (giảm ~70-80% payload)."""
    if "gzip" not in (request.headers.get("Accept-Encoding", "") or "").lower():
        return response
    if response.direct_passthrough or response.headers.get("Content-Encoding"):
        return response
    data = response.get_data()
    if len(data) < _GZIP_MIN_BYTES:
        return response
    buf = BytesIO()
    with gzip.GzipFile(mode="wb", fileobj=buf, compresslevel=6) as gz:
        gz.write(data)
    compressed = buf.getvalue()
    if len(compressed) >= len(data):
        return response
    response.set_data(compressed)
    response.headers["Content-Encoding"] = "gzip"
    response.headers["Content-Length"] = str(len(compressed))
    vary = response.headers.get("Vary", "")
    if "Accept-Encoding" not in vary:
        response.headers["Vary"] = (vary + ", Accept-Encoding").lstrip(", ")
    return response

_get_alerted_today = None
_get_momentum_today = None
_get_attent_today = None
_get_breakvol_today = None
_get_signal_session_date = None
_get_history_cache = None
_get_rs_universe_symbols = None
_cache_lock = None
_fetch_heatmap_fn = None
_extra_quote_fn = None
_fetch_market_health_fn = None
_vol_forecast_fn = None
_calc_vpa_flag_fn = None
_sync_heatmap_fn = None
_signal_emoji = {}
_signal_rank = {}

_heatmap_cache = {"data": {}, "ts": "", "updated_at": 0}
_heatmap_lock = threading.Lock()
HEATMAP_LIVE_TTL_SEC = 5
HEATMAP_TTL_SEC = 120

_extra_quote_cache = {}
_extra_quote_lock = threading.Lock()
EXTRA_QUOTE_TTL_SEC = 20
EXTRA_QUOTE_MAX_SYMS = 15
MARKET_HEALTH_TTL_SEC = 120
SIGNAL_TTL_SEC = 10

_market_health_cache = {"data": {}, "updated_at": 0, "pending_refresh": False}
_market_health_lock = threading.Lock()

_rs_score_cache = {"scores": {}, "asof": None}
_rs_score_lock = threading.Lock()
_RS_EXCLUDE_SYMBOLS = {"VNINDEX", "VN30"}
_RS_LOOKBACK_WEIGHTS = ((10, 0.5), (20, 0.3), (50, 0.2))
_RS_REQUIRED_BARS = max(days for days, _ in _RS_LOOKBACK_WEIGHTS) + 1
_RS_SMOOTH_DAYS = 5
_RS_RAW_TAIL_DAYS = 10
_RS_CACHE_DIR = os.environ.get("DASHBOARD_DATA_DIR", "/data/trade-journal")
_RS_SCORE_CACHE_FILE = os.environ.get("RS_SCORE_CACHE_FILE", os.path.join(_RS_CACHE_DIR, "rs_score_cache.json"))

_MARKET_HEALTH_CACHE_FILE = os.environ.get("MARKET_HEALTH_CACHE_FILE", "/data/trade-journal/market_health.json")

def _save_market_health_to_disk():
    try:
        os.makedirs(os.path.dirname(_MARKET_HEALTH_CACHE_FILE), exist_ok=True)
        tmp_path = _MARKET_HEALTH_CACHE_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(_market_health_cache, f, ensure_ascii=False)
        os.replace(tmp_path, _MARKET_HEALTH_CACHE_FILE)
    except Exception as e:
        print(f"  [Dashboard] ⚠️  Lưu HEALTH cache xuống đĩa lỗi: {e}")

def _load_market_health_from_disk():
    try:
        with open(_MARKET_HEALTH_CACHE_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict) and saved.get("data", {}).get("ok"):
            _market_health_cache["data"] = saved["data"]
            _market_health_cache["updated_at"] = saved.get("updated_at", 0)
            age_min = (time.time() - _market_health_cache["updated_at"]) / 60
            print(f"  [Dashboard] ✅ Nạp lại HEALTH cache từ đĩa (cũ {age_min:.1f} phút).")
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"  [Dashboard] ⚠️  Nạp HEALTH cache từ đĩa lỗi: {e}")

_load_market_health_from_disk()


def _refresh_market_health(force: bool = False) -> dict:
    """Tính lại HEALTH và ghi vào _market_health_cache."""
    if not _fetch_market_health_fn:
        return _market_health_cache["data"]
    now = time.time()
    with _market_health_lock:
        stale = now - _market_health_cache["updated_at"] > MARKET_HEALTH_TTL_SEC
        if not (force or stale):
            return _market_health_cache["data"]
        try:
            data = _fetch_market_health_fn()
            if data.get("ok"):
                _market_health_cache["data"] = data
                _market_health_cache["updated_at"] = time.time()
                _market_health_cache["pending_refresh"] = False
                _save_market_health_to_disk()
            else:
                _market_health_cache["pending_refresh"] = True
        except Exception as e:
            print(f"  [Dashboard] ❌ Fetch market health lỗi: {e}")
            _market_health_cache["pending_refresh"] = True
        return _market_health_cache["data"]


def warm_market_health_cache():
    """Chủ động tính HEALTH ngay sau khi history_cache load xong."""
    data = _refresh_market_health(force=True)
    ok = bool(data.get("ok"))
    print(f"  [Dashboard] {'✅' if ok else '⚠️ '} Warm HEALTH cache: "
          f"{'OK' if ok else data.get('message', 'lỗi không xác định')}")
    return data

def _rs_universe_set():
    if not _get_rs_universe_symbols:
        return None
    try:
        return {str(sym).upper() for sym in (_get_rs_universe_symbols() or [])}
    except Exception as e:
        print(f"  [Dashboard] ⚠️  Lấy danh sách RS universe lỗi (bỏ qua): {e}")
        return None


def _filter_rs_scores(scores: dict, rs_universe: set | None = None) -> dict:
    if not isinstance(scores, dict):
        return {}
    if rs_universe is None:
        rs_universe = _rs_universe_set()
    filtered = {}
    for sym, val in scores.items():
        sym_key = str(sym).upper()
        if sym_key in _RS_EXCLUDE_SYMBOLS or (rs_universe is not None and sym_key not in rs_universe):
            continue
        try:
            filtered[sym_key] = int(round(float(val)))
        except (TypeError, ValueError):
            continue
    return filtered


def _save_rs_scores_to_disk():
    try:
        cache_dir = os.path.dirname(_RS_SCORE_CACHE_FILE)
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)
        tmp_path = _RS_SCORE_CACHE_FILE + ".tmp"
        with _rs_score_lock:
            payload = {
                "scores": _rs_score_cache["scores"],
                "asof": _rs_score_cache["asof"],
                "saved_at": datetime.now(TZ_VN).isoformat(),
            }
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp_path, _RS_SCORE_CACHE_FILE)
    except Exception as e:
        print(f"  [Dashboard] ⚠️  Lưu RS cache xuống đĩa lỗi (bỏ qua): {e}")


def _load_rs_scores_from_disk():
    try:
        with open(_RS_SCORE_CACHE_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        scores = saved.get("scores", {}) if isinstance(saved, dict) else {}
        if isinstance(scores, dict) and scores:
            clean_scores = _filter_rs_scores(scores)
            if clean_scores:
                with _rs_score_lock:
                    _rs_score_cache["scores"] = clean_scores
                    _rs_score_cache["asof"] = saved.get("asof")
                print(f"  [Dashboard] ✅ Nạp lại RS cache từ đĩa: {len(clean_scores)} mã"
                      f"{' @ ' + str(saved.get('asof')) if saved.get('asof') else ''}.")
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"  [Dashboard] ⚠️  Nạp RS cache từ đĩa lỗi (bỏ qua): {e}")


_load_rs_scores_from_disk()

VND_BASE = "https://api-finfo.vndirect.com.vn/v4"
VND_RATIO_CODES = {
    "pe": "PRICE_TO_EARNINGS",
    "pb": "PRICE_TO_BOOK",
    "ma50": "OVER_MA50D_PCT_CR",
    "ma200": "OVER_MA200D_PCT_CR",
}
VND_TTL_SEC = 300
_vnd_cache: dict = {}
_vnd_lock = threading.Lock()


def _vnd_fetch_json(url, referer="https://dstock.vndirect.com.vn/du-lieu-thi-truong/dinh-gia-thi-truong"):
    resp = requests.get(url, timeout=20, headers={
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json,text/plain,*/*",
        "Origin": "https://dstock.vndirect.com.vn",
        "Referer": referer,
    })
    resp.raise_for_status()
    return resp.json()


def _vnd_ratio_data(ratio_code, from_date):
    url = (
        f"{VND_BASE}/ratios"
        f"?q=ratioCode:{ratio_code}~code:VNINDEX~reportDate:gte:{from_date}"
        "&sort=reportDate:desc&size=10000&fields=value,reportDate"
    )
    return _vnd_fetch_json(url).get("data", [])


def _vnd_price_data(from_date):
    url = (
        f"{VND_BASE}/vnmarket_prices"
        f"?q=code:vnindex~date:gte:{from_date}"
        "&sort=date:desc&size=10000&fields=close,date"
    )
    return _vnd_fetch_json(url).get("data", [])


def _vnd_price_by_date(from_date):
    return {
        item["date"]: float(item["close"])
        for item in _vnd_price_data(from_date)
        if item.get("date") and item.get("close") is not None
    }


def _vnd_valuation_rows(metric, from_date):
    ratio_code = VND_RATIO_CODES.get(metric, VND_RATIO_CODES["pe"])
    prices = _vnd_price_by_date(from_date)
    rows = []
    for item in _vnd_ratio_data(ratio_code, from_date):
        date = item.get("reportDate")
        if date in prices and item.get("value") is not None:
            rows.append({"date": date, "value": float(item["value"]), "index": prices[date]})
    rows.sort(key=lambda row: row["date"])
    return rows


def _vnd_allocation_rows(from_date):
    prices = _vnd_price_by_date(from_date)
    ma50 = {
        item["reportDate"]: float(item["value"]) * 100
        for item in _vnd_ratio_data(VND_RATIO_CODES["ma50"], from_date)
        if item.get("reportDate") and item.get("value") is not None
    }
    ma200 = {
        item["reportDate"]: float(item["value"]) * 100
        for item in _vnd_ratio_data(VND_RATIO_CODES["ma200"], from_date)
        if item.get("reportDate") and item.get("value") is not None
    }
    dates = sorted(set(prices) & set(ma50) & set(ma200))
    return [
        {"date": date, "index": prices[date], "ma50": ma50[date], "ma200": ma200[date]}
        for date in dates
    ]


def _vnd_cached_rows(cache_key, getter):
    """Cache theo (kind, metric, from_date) — TTL VND_TTL_SEC, dùng chung cho cả
    2 route bên dưới để đổi kỳ thời gian/tab P/E-P/B không phải gọi VNDIRECT lại
    ngay nếu vài phút trước đã có người xem đúng tổ hợp đó."""
    now = time.time()
    with _vnd_lock:
        cached = _vnd_cache.get(cache_key)
        if cached and now - cached["updated_at"] < VND_TTL_SEC:
            return cached["rows"]
    rows = getter()
    with _vnd_lock:
        _vnd_cache[cache_key] = {"rows": rows, "updated_at": time.time()}
    return rows


@app.route("/api/vndirect_valuation")
def api_vndirect_valuation():
    metric = (request.args.get("metric") or "pe").lower()
    if metric not in ("pe", "pb"):
        metric = "pe"
    from_date = request.args.get("from") or "2015-01-01"
    cache_key = f"valuation:{metric}:{from_date}"
    try:
        rows = _vnd_cached_rows(cache_key, lambda: _vnd_valuation_rows(metric, from_date))
        return jsonify({
            "ok": True, "kind": metric,
            "from": rows[0]["date"] if rows else from_date,
            "to": rows[-1]["date"] if rows else from_date,
            "rows": rows,
        })
    except Exception as e:
        print(f"  [Dashboard] ❌ Fetch VNDIRECT định giá lỗi: {e}")
        return jsonify({"ok": False, "message": str(e), "rows": []}), 502


@app.route("/api/vndirect_allocation")
def api_vndirect_allocation():
    from_date = request.args.get("from") or "2015-01-01"
    cache_key = f"allocation:{from_date}"
    try:
        rows = _vnd_cached_rows(cache_key, lambda: _vnd_allocation_rows(from_date))
        return jsonify({
            "ok": True, "kind": "allocation",
            "from": rows[0]["date"] if rows else from_date,
            "to": rows[-1]["date"] if rows else from_date,
            "rows": rows,
        })
    except Exception as e:
        print(f"  [Dashboard] ❌ Fetch VNDIRECT phân bổ thị trường lỗi: {e}")
        return jsonify({"ok": False, "message": str(e), "rows": []}), 502


_VND_FOREIGN_CODES = "STOCK_HNX,STOCK_UPCOM,STOCK_HOSE,ETF_HOSE,IFC_HOSE"
_VND_PROPRIETARY_CODES = "HNX,VNINDEX,UPCOM"
_VND_FLOW_SESSIONS = 130


def _vnd_sum_flow_by_date(items, date_key, buy_value_key, sell_value_key, buy_vol_key, sell_vol_key):
    grouped = {}
    for item in items:
        row_date = item.get(date_key)
        if not row_date:
            continue
        row = grouped.setdefault(row_date, {"date": row_date, "netValue": 0.0})
        buy_value = float(item.get(buy_value_key) or 0)
        sell_value = float(item.get(sell_value_key) or 0)
        row["netValue"] += float(item.get("netVal") or (buy_value - sell_value))

    rows = []
    for row in grouped.values():
        rows.append({"date": row["date"], "netValueBn": row["netValue"] / 1e9})
    rows.sort(key=lambda row: row["date"])
    return rows


def _vnd_foreign_flow_rows():
    url = f"{VND_BASE}/foreigns?q=code:{_VND_FOREIGN_CODES}&sort=tradingDate&size=900"
    items = _vnd_fetch_json(
        url, referer="https://dstock.vndirect.com.vn/market-watch/daily-trade-foreign"
    ).get("data", [])
    rows = _vnd_sum_flow_by_date(items, "tradingDate", "buyVal", "sellVal", "buyVol", "sellVol")
    return rows[-_VND_FLOW_SESSIONS:]


def _vnd_proprietary_flow_rows(from_date):
    url = (
        f"{VND_BASE}/proprietary_trading"
        f"?q=code:{_VND_PROPRIETARY_CODES}~date:gte:{from_date}"
        "&sort=date:desc&size=1500"
    )
    items = _vnd_fetch_json(
        url, referer="https://dstock.vndirect.com.vn/market-watch/daily-trade-proprietary"
    ).get("data", [])
    rows = _vnd_sum_flow_by_date(items, "date", "buyingVal", "sellingVal", "buyingVol", "sellingVol")
    return rows[-_VND_FLOW_SESSIONS:]


@app.route("/api/foreign_flow")
def api_foreign_flow():
    try:
        rows = _vnd_cached_rows("foreign_flow", _vnd_foreign_flow_rows)
        return jsonify({
            "ok": True,
            "from": rows[0]["date"] if rows else "",
            "to": rows[-1]["date"] if rows else "",
            "rows": rows,
        })
    except Exception as e:
        print(f"  [Dashboard] ❌ Fetch dữ liệu khối ngoại lỗi: {e}")
        return jsonify({"ok": False, "message": str(e), "rows": []}), 502


@app.route("/api/proprietary_flow")
def api_proprietary_flow():
    from_date = request.args.get("from") or (datetime.now(TZ_VN).date() - timedelta(days=182)).isoformat()
    cache_key = f"proprietary_flow:{from_date}"
    try:
        rows = _vnd_cached_rows(cache_key, lambda: _vnd_proprietary_flow_rows(from_date))
        return jsonify({
            "ok": True,
            "from": rows[0]["date"] if rows else from_date,
            "to": rows[-1]["date"] if rows else from_date,
            "rows": rows,
        })
    except Exception as e:
        print(f"  [Dashboard] ❌ Fetch dữ liệu tự doanh lỗi: {e}")
        return jsonify({"ok": False, "message": str(e), "rows": []}), 502


_VPA_FLAG_COLOR = {1: "#254fcc", 2: "#00ffe5"}

_LITE_CHART_CACHE_TTL = 120
_lite_chart_cache: dict = {}
_lite_chart_cache_lock = threading.Lock()

JOURNAL_DATA_DIR = Path(os.environ.get("DASHBOARD_DATA_DIR", "/data/trade-journal")).expanduser()
JOURNAL_UPLOAD_DIR = JOURNAL_DATA_DIR / "uploads"
JOURNAL_DB_PATH = JOURNAL_DATA_DIR / "trade_journal.sqlite"
JOURNAL_WARNING_PATH = JOURNAL_DATA_DIR / "market_warning.txt"
JOURNAL_ALLOWED_EXT = {"png", "jpg", "jpeg", "webp", "gif"}
_journal_lock = threading.Lock()
_price_alert_lock = threading.Lock()

HMAP_COLS_CONFIG = [
    {"groups": [{"name": "VN30", "syms": ["FPT", "GAS", "NVL", "VNM", "VCB", "PLX", "TCB", "MWG", "STB", "HPG", "PNJ", "BID", "CTG", "HDB", "VJC", "VPB", "KDH", "MBB", "VHM", "POW", "VRE", "MSN", "SSI", "ACB", "BVH", "GVR", "TPB"]}]},
    {"groups": [{"name": "NGÂN HÀNG", "syms": ["VCB", "BID", "CTG", "MBB", "ACB", "TCB", "TPB", "HDB", "SHB", "STB", "VIB", "VPB", "MSB", "ABB", "BVB", "LPB"]}, {"name": "DẦU KHÍ", "syms": ["GAS", "PVD", "PVS", "BSR", "OIL", "PVB", "PVC", "PLX", "PET", "PVT"]}]},
    {"groups": [{"name": "CHỨNG KHOÁN", "syms": ["SSI", "VND", "CTS", "FTS", "HCM", "MBS", "DSE", "BSI", "SHS", "VCI", "VCK", "ORS"]}, {"name": "XÂY DỰNG", "syms": ["C47", "C32", "L14", "CII", "CTD", "CTI", "FCN", "HBC", "HUT", "LCG", "PC1", "DPG", "PHC", "VCG"]}]},
    {"groups": [{"name": "BẤT ĐỘNG SẢN", "syms": ["VHM", "AGG", "IJC", "LDG", "CEO", "D2D", "DIG", "DXG", "HDC", "HDG", "KDH", "NLG", "NTL", "NVL", "PDR", "SCR", "TIG", "KBC", "SZC"]}, {"name": "PHÂN BÓN", "syms": ["BFC", "DCM", "DPM"]}, {"name": "THÉP", "syms": ["HPG", "HSG", "NKG"]}]},
    {"groups": [{"name": "BÁN LẺ", "syms": ["MSN", "FPT", "FRT", "MWG", "PNJ", "DGW", "VNM"]}, {"name": "THỦY SẢN", "syms": ["ANV", "CMX", "VHC", "IDI"]}, {"name": "CẢNG BIỂN", "syms": ["HAH", "GMD", "SGP", "VSC"]}, {"name": "CAO SU", "syms": ["GVR", "DPR", "DRI", "PHR", "DRC"]}, {"name": "NHỰA", "syms": ["AAA", "BMP", "NTP"]}]},
    {"groups": [{"name": "ĐIỆN NƯỚC", "syms": ["NT2", "PC1", "GEG", "GEX", "POW", "TDM", "BWE"]}, {"name": "DỆT MAY", "syms": ["TCM", "TNG", "VGT", "MSH"]}, {"name": "HÀNG KHÔNG", "syms": ["NCT", "ACV", "AST", "HVN", "SCS", "VJC"]}, {"name": "BẢO HIỂM", "syms": ["BMI", "MIG", "BVH"]}, {"name": "MÍA ĐƯỜNG", "syms": ["LSS", "SBT", "QNS"]}]},
    {"groups": [{"name": "ĐẦU TƯ CÔNG", "syms": ["FCN", "HHV", "LCG", "VCG", "C4G", "CTD", "HBC", "HSG", "NKG", "HPG", "KSB", "PLC"]}]},
]

TS_POOL_CONFIG = ["AAA", "ACB", "AGG", "ANV", "BFC", "BID", "BMI", "BSR", "BVB", "BVH", "BWE", "BAF", "CII", "CKG", "CRE", "CTD", "CTG", "CTI", "CTR", "CTS", "ORS", "D2D", "DBC", "DCM", "DSE", "DGW", "DIG", "DPG", "DPM", "DRC", "DRH", "DXG", "FCN", "FPT", "FRT", "FTS", "GAS", "GEG", "GEX", "GMD", "GVR", "HAG", "HAX", "HBC", "HCM", "HDB", "HDC", "VCK", "HDG", "HNG", "HPG", "HSG", "HTN", "HVN", "IDC", "IJC", "KBC", "KDH", "KSB", "LCG", "LDG", "LPB", "MBB", "MBS", "MSB", "MSN", "MWG", "NKG", "NLG", "NTL", "NVL", "PC1", "PDR", "PET", "PHR", "PLC", "PLX", "PNJ", "POW", "PTB", "PVD", "PVS", "PVT", "QNS", "REE", "SBT", "SCR", "SHB", "SHS", "SSI", "STB", "SZC", "TCB", "TDM", "TIG", "TNG", "TPB", "TV2", "VCB", "VCI", "VCS", "VGT", "VHC", "VHM", "VIB", "VIC", "VJC", "VNM", "VPB", "VRE"]


def _now_vn_iso():
    return datetime.now(TZ_VN).strftime("%Y-%m-%d %H:%M:%S")


def _json_safe(obj):
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, float) and not math.isfinite(obj):
        return 0.0
    return obj


_journal_storage_ready = False
_journal_storage_init_lock = threading.Lock()

def _init_journal_storage():
    global _journal_storage_ready
    if _journal_storage_ready:
        return
    with _journal_storage_init_lock:
        if _journal_storage_ready:
            return
        _do_init_journal_storage()
        _journal_storage_ready = True

def _do_init_journal_storage():
    JOURNAL_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(JOURNAL_DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS journal_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                buy_date TEXT,
                signal TEXT,
                price TEXT,
                title TEXT,
                notes TEXT,
                stoploss TEXT,
                target TEXT,
                status TEXT DEFAULT 'check',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(journal_entries)").fetchall()}
        if "stoploss" not in cols:
            conn.execute("ALTER TABLE journal_entries ADD COLUMN stoploss TEXT")
        if "target" not in cols:
            conn.execute("ALTER TABLE journal_entries ADD COLUMN target TEXT")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS journal_images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_id INTEGER NOT NULL,
                filename TEXT NOT NULL,
                original_name TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(entry_id) REFERENCES journal_entries(id) ON DELETE CASCADE
            )
        """)
        conn.commit()


def _journal_conn():
    _init_journal_storage()
    conn = sqlite3.connect(JOURNAL_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


_price_alert_storage_ready = False
_price_alert_storage_init_lock = threading.Lock()

def _init_price_alert_storage():
    global _price_alert_storage_ready
    if _price_alert_storage_ready:
        return
    with _price_alert_storage_init_lock:
        if _price_alert_storage_ready:
            return
        _do_init_price_alert_storage()
        _price_alert_storage_ready = True

def _do_init_price_alert_storage():
    JOURNAL_DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(JOURNAL_DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS price_alert_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                left_type TEXT NOT NULL,
                left_ma_kind TEXT,
                left_period INTEGER,
                operator TEXT NOT NULL,
                right_type TEXT NOT NULL,
                right_value REAL,
                right_ma_kind TEXT,
                right_period INTEGER,
                notify_dashboard INTEGER DEFAULT 1,
                notify_telegram INTEGER DEFAULT 0,
                telegram_chat_id TEXT,
                after_trigger TEXT DEFAULT 'disable',
                active INTEGER DEFAULT 1,
                last_trigger_bar TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS price_alert_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_id INTEGER,
                client_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                message TEXT NOT NULL,
                detail TEXT,
                bar_date TEXT,
                price REAL,
                seen INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY(rule_id) REFERENCES price_alert_rules(id) ON DELETE SET NULL
            )
        """)
        conn.commit()


def _price_alert_conn():
    _init_price_alert_storage()
    conn = sqlite3.connect(JOURNAL_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _clean_alert_client_id(value):
    value = str(value or "").strip()
    return value[:80] if value else ""


def _get_alert_client_id(data=None):
    data = data or {}
    return _clean_alert_client_id(
        request.headers.get("X-Alert-Client-Id")
        or request.args.get("client_id")
        or data.get("client_id")
    )


def _rule_to_dict(row):
    return {
        "id": row["id"],
        "client_id": row["client_id"],
        "symbol": row["symbol"],
        "left_type": row["left_type"],
        "left_ma_kind": row["left_ma_kind"] or "",
        "left_period": row["left_period"],
        "operator": row["operator"],
        "right_type": row["right_type"],
        "right_value": row["right_value"],
        "right_ma_kind": row["right_ma_kind"] or "",
        "right_period": row["right_period"],
        "notify_dashboard": bool(row["notify_dashboard"]),
        "notify_telegram": bool(row["notify_telegram"]),
        "telegram_chat_id": row["telegram_chat_id"] or "",
        "after_trigger": row["after_trigger"] or "disable",
        "active": bool(row["active"]),
        "last_trigger_bar": row["last_trigger_bar"] or "",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _event_to_dict(row):
    return {
        "id": row["id"],
        "rule_id": row["rule_id"],
        "client_id": row["client_id"],
        "symbol": row["symbol"],
        "message": row["message"],
        "detail": row["detail"] or "",
        "bar_date": row["bar_date"] or "",
        "price": row["price"],
        "seen": bool(row["seen"]),
        "created_at": row["created_at"],
    }


def _validate_alert_rule_payload(data):
    periods = {10, 20, 30, 50, 100, 200}
    ma_kinds = {"MA", "EMA"}
    operators = {"gte", "lte"}
    client_id = _clean_alert_client_id(data.get("client_id"))
    symbol = str(data.get("symbol") or "").upper().strip()
    left_type = str(data.get("left_type") or "price").lower()
    operator = str(data.get("operator") or "").lower()
    right_type = str(data.get("right_type") or "ma").lower()
    after_trigger = str(data.get("after_trigger") or "disable").lower()

    if not client_id:
        raise ValueError("missing_client_id")
    if not symbol or len(symbol) > 10 or not all(ch.isalnum() for ch in symbol):
        raise ValueError("invalid_symbol")
    if left_type not in {"price", "ma"}:
        raise ValueError("invalid_left_type")
    if operator not in operators:
        raise ValueError("invalid_operator")
    if right_type not in {"price", "ma"}:
        raise ValueError("invalid_right_type")
    if after_trigger not in {"disable", "keep"}:
        after_trigger = "disable"

    left_ma_kind = None
    left_period = None
    if left_type == "ma":
        left_ma_kind = str(data.get("left_ma_kind") or "MA").upper()
        left_period = int(data.get("left_period") or 20)
        if left_ma_kind not in ma_kinds or left_period not in periods:
            raise ValueError("invalid_left_ma")

    right_value = None
    right_ma_kind = None
    right_period = None
    if right_type == "price":
        right_value = float(data.get("right_value") or 0)
        if not math.isfinite(right_value) or right_value <= 0:
            raise ValueError("invalid_price")
    else:
        right_ma_kind = str(data.get("right_ma_kind") or "MA").upper()
        right_period = int(data.get("right_period") or 20)
        if right_ma_kind not in ma_kinds or right_period not in periods:
            raise ValueError("invalid_right_ma")

    notify_dashboard = 1 if data.get("notify_dashboard", True) else 0
    notify_telegram = 1 if data.get("notify_telegram") else 0
    telegram_chat_id = str(data.get("telegram_chat_id") or "").strip()[:80]
    if not notify_dashboard and not notify_telegram:
        raise ValueError("missing_notify_channel")
    if notify_telegram and not telegram_chat_id:
        raise ValueError("missing_telegram_chat_id")

    return {
        "client_id": client_id,
        "symbol": symbol,
        "left_type": left_type,
        "left_ma_kind": left_ma_kind,
        "left_period": left_period,
        "operator": operator,
        "right_type": right_type,
        "right_value": right_value,
        "right_ma_kind": right_ma_kind,
        "right_period": right_period,
        "notify_dashboard": notify_dashboard,
        "notify_telegram": notify_telegram,
        "telegram_chat_id": telegram_chat_id,
        "after_trigger": after_trigger,
    }


def get_active_price_alert_rules():
    with _price_alert_lock, _price_alert_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM price_alert_rules
            WHERE active=1
            ORDER BY symbol, id
        """).fetchall()
    return [_rule_to_dict(row) for row in rows]


def record_price_alert_event(rule_id, message, detail, bar_date, price, notify_dashboard=True):
    now = _now_vn_iso()
    with _price_alert_lock, _price_alert_conn() as conn:
        rule = conn.execute("SELECT * FROM price_alert_rules WHERE id=?", (rule_id,)).fetchone()
        if not rule:
            return None
        bar_date = str(bar_date or "")
        if rule["last_trigger_bar"] == bar_date:
            return None
        conn.execute("""
            INSERT INTO price_alert_events
            (rule_id, client_id, symbol, message, detail, bar_date, price, seen, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            rule["id"], rule["client_id"], rule["symbol"], message,
            detail or "", bar_date, float(price or 0), 0 if notify_dashboard else 1, now,
        ))
        active = 0 if (rule["after_trigger"] or "disable") == "disable" else 1
        conn.execute("""
            UPDATE price_alert_rules
            SET last_trigger_bar=?, active=?, updated_at=?
            WHERE id=?
        """, (bar_date, active, now, rule["id"]))
        conn.commit()
        event = conn.execute("SELECT * FROM price_alert_events WHERE id=last_insert_rowid()").fetchone()
    return _event_to_dict(event) if event else None


def _safe_text(value, max_len=2000):
    if value is None:
        return ""
    return str(value).strip()[:max_len]


def _entry_to_dict(row, images):
    return {
        "id": row["id"],
        "symbol": row["symbol"],
        "buy_date": row["buy_date"] or "",
        "signal": row["signal"] or "",
        "price": row["price"] or "",
        "stoploss": row["stoploss"] or "",
        "target": row["target"] or "",
        "title": row["title"] or "",
        "notes": row["notes"] or "",
        "status": row["status"] or "check",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "images": images,
    }

def _is_admin():
    return bool(session.get("journal_admin"))


def require_journal_admin(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not _is_admin():
            return jsonify({"error": "unauthorized"}), 401
        return fn(*args, **kwargs)
    return wrapper


def _uploaded_ext(filename):
    name = filename or ""
    if "." not in name:
        return ""
    return name.rsplit(".", 1)[1].lower()


def invalidate_rs_cache():
    """Xoá snapshot RS, dùng sau khi rebuild history_cache sang phiên mới."""
    with _rs_score_lock:
        _rs_score_cache["scores"] = {}
        _rs_score_cache["asof"] = None


def _compute_rs_scores(force: bool = False) -> dict:
    """Công thức RSTOMRK: raw = 0.5*C/Ref(C,-10) + 0.3*C/Ref(C,-20) + 0.2*C/Ref(C,-50),
    xếp hạng percentile trong rổ mã đang có trong history_cache. Dùng close của
    phiên đã hoàn chỉnh gần nhất, không trộn giá intraday."""
    rs_universe = _rs_universe_set()

    with _rs_score_lock:
        if not force and _rs_score_cache["scores"]:
            return _filter_rs_scores(_rs_score_cache["scores"], rs_universe)

    cache = _get_history_cache() if _get_history_cache else {}
    if not cache:
        return {}

    raw_by_sym = {}
    snapshot_dates = []
    today_vn = datetime.now(TZ_VN).date()
    lock = _cache_lock
    if lock:
        lock.acquire()
    try:
        items = list(cache.items())
    finally:
        if lock:
            lock.release()

    for sym, df in items:
        sym_key = str(sym).upper()
        if sym_key in _RS_EXCLUDE_SYMBOLS or (rs_universe is not None and sym_key not in rs_universe):
            continue
        if df is None or len(df) < _RS_REQUIRED_BARS or "close" not in df.columns:
            continue
        close = pd.to_numeric(df["close"], errors="coerce")
        if not isinstance(close.index, pd.DatetimeIndex):
            close.index = pd.to_datetime(close.index, errors="coerce")
        close = close[close.index.notna()]
        close = close[close.index.date < today_vn]
        if len(close) < _RS_REQUIRED_BARS:
            continue
        raw = sum(weight * (close / close.shift(days)) for days, weight in _RS_LOOKBACK_WEIGHTS)
        raw_valid = raw.dropna()
        if raw_valid.empty:
            continue
        daily_raw = {}
        for idx, val in raw_valid.tail(_RS_RAW_TAIL_DAYS).items():
            v = float(val)
            if math.isfinite(v) and v > 0:
                daily_raw[idx.date()] = v
        if not daily_raw:
            continue
        snapshot_dates.extend(daily_raw.keys())
        raw_by_sym[sym_key] = daily_raw

    if not raw_by_sym:
        return {}

    last_dates = sorted(set(snapshot_dates))[-_RS_SMOOTH_DAYS:]
    pct_history = {sym: [] for sym in raw_by_sym}
    for dt in last_dates:
        date_scores = {}
        for sym, daily_raw in raw_by_sym.items():
            v = daily_raw.get(dt)
            if v is not None:
                date_scores[sym] = v
        day_count = len(date_scores)
        if not day_count:
            continue
        for rank, (sym, _) in enumerate(sorted(date_scores.items(), key=lambda item: item[1], reverse=True), start=1):
            pct_history[sym].append(100 - 100 * rank / day_count)

    scores = {
        sym: round(sum(vals) / len(vals))
        for sym, vals in pct_history.items()
        if vals
    }
    if not scores:
        return {}

    with _rs_score_lock:
        _rs_score_cache["scores"] = scores
        _rs_score_cache["asof"] = max(snapshot_dates).isoformat() if snapshot_dates else None
    _save_rs_scores_to_disk()
    return dict(scores)


def _rs_cache_meta() -> dict:
    rs_universe = _rs_universe_set()
    with _rs_score_lock:
        scores = _filter_rs_scores(_rs_score_cache["scores"], rs_universe)
        return {"count": len(scores), "asof": _rs_score_cache["asof"]}


def warm_rs_cache():
    scores = _compute_rs_scores(force=True)
    meta = _rs_cache_meta()
    print(f"  [Dashboard] {'✅' if scores else '⚠️ '} Warm RS cache: {len(scores)} mã"
          f"{' @ ' + str(meta.get('asof')) if meta.get('asof') else ''}.")
    return scores


def _attach_rs(row: dict, rs_scores: dict) -> dict:
    rs = rs_scores.get(str(row.get("symbol", "")).upper())
    if rs is not None:
        row["rs"] = rs
    return row


def _attach_rs_payload(payload: dict, symbol: str) -> dict:
    with _rs_score_lock:
        rs = _rs_score_cache["scores"].get(str(symbol).upper())
    if rs is not None:
        payload["rs"] = rs
    else:
        payload.pop("rs", None)
    return payload

@app.route("/api/signals")
def api_signals():
    alerted = _get_alerted_today() if _get_alerted_today else {}
    momentum = _get_momentum_today() if _get_momentum_today else {}
    attent = _get_attent_today() if _get_attent_today else {}
    breakvol = _get_breakvol_today() if _get_breakvol_today else {}
    session_date = _get_signal_session_date() if _get_signal_session_date else None
    today_vn = datetime.now(TZ_VN).date()
    session_stale = bool(session_date) and session_date != today_vn
    rs_scores = _compute_rs_scores()
    rs_meta = _rs_cache_meta()
    result = []
    for idx, (sym, entry) in enumerate(alerted.items()):
        sig = entry["signal"] if isinstance(entry, dict) else entry
        pct = entry.get("pct") if isinstance(entry, dict) else None
        emoji = _signal_emoji.get(sig, "📌")
        rank  = _signal_rank.get(sig, 0)
        result.append(_attach_rs({"symbol": sym, "signal": sig, "emoji": emoji,
                                  "rank": rank, "order": idx, "pct": pct,
                                  "price": entry.get("price") if isinstance(entry, dict) else None}, rs_scores))
    result.sort(key=lambda x: (x["rank"], x["order"]), reverse=True)
    momentum_result = []
    for sig in ("MACD_W", "MACD_M", "RTM"):
        rows = []
        for sym in sorted(momentum.keys()):
            entry = momentum[sym]
            sigs = entry.get("signals", []) if isinstance(entry, dict) else []
            if sig not in sigs:
                continue
            pct = entry.get("pct") if isinstance(entry, dict) else None
            rows.append(_attach_rs({"symbol": sym, "signal": sig, "pct": pct}, rs_scores))
        momentum_result.extend(rows)
    attent_result = [
        {"symbol": sym, "pct": (entry.get("pct") if isinstance(entry, dict) else None)}
        for sym, entry in sorted(attent.items())
    ]
    breakvol_result = [
        {"symbol": sym, "pct": (entry.get("pct") if isinstance(entry, dict) else None)}
        for sym, entry in sorted(breakvol.items())
    ]
    strength_result = [
        {"symbol": sym, "rs": rs}
        for sym, rs in sorted(rs_scores.items(), key=lambda item: item[1], reverse=True)
        if rs > 80
    ]
    return jsonify({
        "signals": result,
        "count":   len(result),
        "momentum": momentum_result,
        "momentum_count": len(momentum_result),
        "strength": strength_result,
        "strength_count": len(strength_result),
        "rs_count": rs_meta["count"],
        "rs_asof": rs_meta["asof"],
        "attent": attent_result,
        "attent_count": len(attent_result),
        "breakvol": breakvol_result,
        "breakvol_count": len(breakvol_result),
        "updated_at": datetime.now(TZ_VN).strftime("%H:%M:%S"),
        "session_date": session_date.strftime("%d/%m/%Y") if session_date else None,
        "session_stale": session_stale,
    })

def _fetch_ssi_priceboard_batch(symbols: list[str]) -> list[dict]:
    if not symbols:
        return []
    url = "https://iboard-query.ssi.com.vn/stock/multiple"
    results = []
    for i in range(0, len(symbols), 100):
        chunk = [s.upper().strip() for s in symbols[i:i + 100] if s.strip()]
        if not chunk:
            continue
        try:
            res = _ssi_http_session.post(url, json={"stocks": chunk}, timeout=5)
            if res.status_code == 200:
                raw_data = res.json().get("data", [])
                if isinstance(raw_data, list):
                    for it in raw_data:
                        if not isinstance(it, dict):
                            continue
                        sym = str(it.get("stockSymbol") or "").upper().strip()
                        if not sym:
                            continue
                        ref_p = float(it.get('refPrice') or 0.0) / 1000.0
                        match_p = float(it.get('matchedPrice') or 0.0) / 1000.0
                        project_open = float(it.get('expectedMatchedPrice') or 0.0) / 1000.0
                        display_price = match_p if match_p > 0 else (project_open if project_open > 0 else ref_p)
                        total_vol = float(it.get('nmTotalTradedQty') or it.get('stockVol') or 0.0)
                        total_val = float(it.get('nmTotalTradedValue') or 0.0)
                        results.append({
                            'stockSymbol': sym,
                            'refPrice': ref_p,
                            'matchedPrice': display_price,
                            'projectOpen': project_open,
                            'nmTotalTradedValue': total_val,
                            'stockVol': total_vol
                        })
        except Exception:
            pass
    return results

def _default_fetch_heatmap_data():
    need_symbols = []
    for col in HMAP_COLS_CONFIG:
        for g in col.get("groups", []):
            need_symbols.extend(g.get("syms", []))
    need_symbols.extend(TS_POOL_CONFIG)
    need_symbols = list(set(need_symbols))
    items = _fetch_ssi_priceboard_batch(need_symbols)
    result = {}
    for it in items:
        sym = it.get('stockSymbol')
        if not sym:
            continue
        matched = float(it.get('matchedPrice') or 0)
        ref_p = float(it.get('refPrice') or 0)
        total_value = float(it.get('nmTotalTradedValue') or 0)
        close = matched if matched > 0 else ref_p
        pct = round((close - ref_p) / ref_p * 100, 2) if ref_p > 0 else 0.0
        if not math.isfinite(pct):
            pct = 0.0
        result[sym.upper().strip()] = {"price": close, "pct": pct, "total_value": total_value}
    ts_str = datetime.now(TZ_VN).strftime("%H:%M  %d/%m/%Y")
    return result, ts_str

def _is_market_session() -> bool:
    now = datetime.now(TZ_VN)
    if now.weekday() >= 5:
        return False
    t = now.time()
    from datetime import time as _time
    return (_time(9, 0) <= t <= _time(11, 35)) or (_time(13, 0) <= t <= _time(15, 5))

def _heatmap_poll_interval() -> int:
    return HEATMAP_LIVE_TTL_SEC if _is_market_session() else HEATMAP_TTL_SEC

@app.route("/api/heatmap")
def api_heatmap():
    now = time.time()
    with _heatmap_lock:
        fn = _fetch_heatmap_fn or _default_fetch_heatmap_data
        ttl = _heatmap_poll_interval() / 2.0
        if now - _heatmap_cache["updated_at"] > ttl:
            try:
                data, ts_str = fn()
                if data:
                    _heatmap_cache["data"] = data
                    _heatmap_cache["ts"]   = ts_str
                    _heatmap_cache["updated_at"] = time.time()
                    if _sync_heatmap_fn:
                        _sync_heatmap_fn(data)
            except Exception as e:
                print(f"  [Dashboard] ❌ Fetch heatmap lỗi: {e}")
        snap_time = _heatmap_cache["updated_at"]
    return jsonify({
        "data":      _json_safe(_heatmap_cache["data"]),
        "timestamp": _heatmap_cache["ts"],
        "cached_age": int(now - snap_time),
    })

@app.route("/api/quote_extra")
def api_quote_extra():
    """Bù giá on-demand cho MÃ LẺ không nằm trong danh sách quét chung (_HEATMAP_NEED_SYMBOLS
    bên scanner_full.py) — ví dụ mã người dùng tự thêm vào FAVORITE trên sidebar CHART. Khác
    /api/heatmap ở chỗ: nhận list mã tuỳ ý qua query ?syms=A,B,C thay vì trả cố định 1 bộ mã,
    và dùng cache/TTL riêng (EXTRA_QUOTE_TTL_SEC) để không đụng tới cache heatmap chính."""
    raw = request.args.get("syms", "")
    syms = []
    for s in raw.split(","):
        s = s.strip().upper()
        if s and s.isalnum() and 1 <= len(s) <= 10 and s not in syms:
            syms.append(s)
    syms = syms[:EXTRA_QUOTE_MAX_SYMS]
    if not syms or not _extra_quote_fn:
        return jsonify({"data": {}})

    now = time.time()
    with _extra_quote_lock:
        need_fetch = [
            s for s in syms
            if s not in _extra_quote_cache or now - _extra_quote_cache[s]["ts"] > EXTRA_QUOTE_TTL_SEC
        ]
    if need_fetch:
        try:
            fresh = _extra_quote_fn(need_fetch) or {}
            with _extra_quote_lock:
                for s, v in fresh.items():
                    if isinstance(v, dict) and "price" in v and "pct" in v:
                        _extra_quote_cache[s] = {"price": v["price"], "pct": v["pct"], "ts": now}
        except Exception as e:
            print(f"  [Dashboard] ❌ Fetch quote_extra lỗi: {e}")

    with _extra_quote_lock:
        data = {
            s: {"price": _extra_quote_cache[s]["price"], "pct": _extra_quote_cache[s]["pct"]}
            for s in syms if s in _extra_quote_cache
        }
    return jsonify({"data": _json_safe(data)})

@app.route("/api/market_health")
def api_market_health():
    data = _refresh_market_health()
    with _market_health_lock:
        snap_time = _market_health_cache["updated_at"]
        pending_refresh = _market_health_cache.get("pending_refresh", False)
    payload = dict(data or {})
    payload["cached_age"] = int(time.time() - snap_time) if snap_time else 0
    payload["ttl_sec"] = MARKET_HEALTH_TTL_SEC
    payload["pending_refresh"] = pending_refresh
    return jsonify(_json_safe(payload))

@app.route("/api/vol_forecast/<symbol>")
def api_vol_forecast(symbol):
    """NGUỒN DUY NHẤT cho khối 'Giá phóng to' trên panel CHART — trả progress
    (% thời gian phiên, giờ VN) và ratio_prev/ratio_ma50. JS chỉ hiển thị, không tự tính lại."""
    symbol = symbol.upper().strip()
    if not _vol_forecast_fn:
        return jsonify({"symbol": symbol, "error": "unavailable"}), 503
    try:
        return jsonify(_vol_forecast_fn(symbol))
    except Exception as exc:
        return jsonify({"symbol": symbol, "error": "exception", "detail": str(exc)}), 500

_vndirect_http_session = requests.Session()
_vndirect_http_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json,*/*",
    "Referer": "https://dstock.vndirect.com.vn/",
})

_ssi_http_session = requests.Session()
_ssi_http_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json,*/*",
    "Content-Type": "application/json",
})

def _fetch_vndirect_raw_daily(symbol, from_ts, to_ts):
    """Gọi thẳng VNDirect DChart API (resolution=D) dùng HTTP Keep-Alive Session,
    trả về list bar thô đã lọc hợp lệ, sort tăng dần theo thời gian."""
    symbol = symbol.upper().strip()
    url = f"https://dchart-api.vndirect.com.vn/dchart/history?resolution=D&symbol={symbol}&from={from_ts}&to={to_ts}"
    try:
        res = _vndirect_http_session.get(url, timeout=10)
        if res.status_code != 200:
            return None
        data = res.json()
    except Exception:
        pass
        return None


    if not data or data.get("s") != "ok" or not data.get("t"):
        return None

    times  = data.get("t", [])
    opens  = data.get("o", [])
    highs  = data.get("h", [])
    lows   = data.get("l", [])
    closes = data.get("c", [])
    vols   = data.get("v", [])

    raw_bars = []
    for i in range(len(times)):
        try:
            o = float(opens[i]); h = float(highs[i]); l = float(lows[i])
            c = float(closes[i]); v = float(vols[i])
            if all(math.isfinite(x) and x > 0 for x in (o, h, l, c)):
                raw_bars.append({
                    "t": int(times[i]),
                    "open": o, "high": h, "low": l, "close": c, "volume": max(0.0, v)
                })
        except (IndexError, ValueError, TypeError):
            continue
    return raw_bars or None

def _default_calc_signals(df):
    return None, []

_calc_signals_fn = None

def fetch_vndirect_dchart(symbol, tf="1D", limit=450, before_date=None):
    """Fetch + build candles/volume cho panel CHART.
    - limit: số nến tối đa muốn trả (default 400 ≈ 1.8 năm D — đủ 200 nến lùi cho MA200).
    - before_date: chuỗi 'YYYY-MM-DD' — nếu set, chỉ lấy bar CŨ HƠN date này
      (dùng cho lazy load lịch sử khi user kéo trái đến đầu dữ liệu).
    """
    symbol = symbol.upper().strip()
    tf_upper = tf.upper().strip()
    min_limit = 50 if before_date else 5
    limit = max(min_limit, min(1000, int(limit or 400)))

    if before_date:
        try:
            from datetime import date as _date
            bd = _date.fromisoformat(str(before_date))
            to_ts = int(datetime(bd.year, bd.month, bd.day, tzinfo=TZ_VN).timestamp()) - 1
        except Exception:
            now_dt = datetime.now(TZ_VN)
            to_ts = int(datetime(now_dt.year, now_dt.month, now_dt.day, tzinfo=TZ_VN).timestamp()) + 86400
    else:
        now_dt = datetime.now(TZ_VN)
        to_ts = int(datetime(now_dt.year, now_dt.month, now_dt.day, tzinfo=TZ_VN).timestamp()) + 86400

    if tf_upper in ("1W", "W", "WEEK", "WEEKLY"):
        from_ts = to_ts - (limit * 7 + 60) * 86400
        target_tf = "1W"
    elif tf_upper in ("1M", "M", "MONTH", "MONTHLY"):
        from_ts = to_ts - (limit * 31 + 90) * 86400
        target_tf = "1M"
    else:
        fetch_limit = max(150, limit)
        from_ts = to_ts - int(fetch_limit * 1.6 + 30) * 86400
        target_tf = "1D"

    try:
        raw_bars = None
        df_cached = None
        if not before_date and target_tf == "1D" and _get_history_cache:
            try:
                with _cache_lock:
                    cache_dict = _get_history_cache() if callable(_get_history_cache) else _get_history_cache
                    df_c = cache_dict.get(symbol) if isinstance(cache_dict, dict) else None
                if df_c is not None and len(df_c) >= 60:
                    df_cached = df_c
                    df_sub = df_cached.tail(limit)
                    raw_bars = []
                    for row in df_sub.itertuples():
                        raw_bars.append({
                            "t": int(row.Index.timestamp()),
                            "open": float(row.open), "high": float(row.high),
                            "low": float(row.low), "close": float(row.close),
                            "volume": float(row.volume)
                        })
            except Exception:
                raw_bars = None

        if not raw_bars:
            raw_bars = _fetch_vndirect_raw_daily(symbol, from_ts, to_ts)
            
            # --- TỐI ƯU (LAZY CACHING): Lưu mã lẻ vào history_cache để tránh spam DStock mỗi 5s ---
            if raw_bars and target_tf == "1D" and not before_date and _get_history_cache:
                try:
                    bars_for_df = []
                    for b in raw_bars:
                        dt = datetime.fromtimestamp(b["t"] + 25200, timezone.utc).replace(tzinfo=None)
                        bars_for_df.append({
                            "time": dt, "open": b["open"], "high": b["high"], 
                            "low": b["low"], "close": b["close"], "volume": b["volume"]
                        })
                    if bars_for_df:
                        df_new = pd.DataFrame(bars_for_df)
                        df_new.set_index("time", inplace=True)
                        df_cached = df_new  # Re-use for VPA calculation
                        with _cache_lock:
                            cache_dict = _get_history_cache() if callable(_get_history_cache) else _get_history_cache
                            if isinstance(cache_dict, dict):
                                cache_dict[symbol] = df_new
                except Exception as e:
                    print(f"  [Dashboard] Lỗi Lazy Cache cho mã {symbol}: {e}")
    except Exception as exc:
        return None, str(exc)
    if not raw_bars:
        return None, "no_data_from_vndirect"

    if target_tf == "1W":
        weeks = {}
        for bar in raw_bars:
            dt = datetime.fromtimestamp(bar["t"], tz=TZ_VN)
            key = (dt.isocalendar()[0], dt.isocalendar()[1])
            if key not in weeks:
                weeks[key] = {
                    "time": dt.strftime("%Y-%m-%d"),
                    "open": bar["open"], "high": bar["high"], "low": bar["low"],
                    "close": bar["close"], "volume": bar["volume"]
                }
            else:
                w = weeks[key]
                w["high"] = max(w["high"], bar["high"])
                w["low"] = min(w["low"], bar["low"])
                w["close"] = bar["close"]
                w["volume"] += bar["volume"]
                w["time"] = dt.strftime("%Y-%m-%d")
        final_bars = list(weeks.values())

    elif target_tf == "1M":
        months = {}
        for bar in raw_bars:
            dt = datetime.fromtimestamp(bar["t"], tz=TZ_VN)
            key = (dt.year, dt.month)
            if key not in months:
                months[key] = {
                    "time": dt.strftime("%Y-%m-%d"),
                    "open": bar["open"], "high": bar["high"], "low": bar["low"],
                    "close": bar["close"], "volume": bar["volume"]
                }
            else:
                m = months[key]
                m["high"] = max(m["high"], bar["high"])
                m["low"] = min(m["low"], bar["low"])
                m["close"] = bar["close"]
                m["volume"] += bar["volume"]
                m["time"] = dt.strftime("%Y-%m-%d")
        final_bars = list(months.values())

    else:
        final_bars = []
        for bar in raw_bars:
            dt_str = time.strftime("%Y-%m-%d", time.gmtime(bar["t"] + 25200))
            final_bars.append({
                "time": dt_str,
                "open": bar["open"], "high": bar["high"], "low": bar["low"],
                "close": bar["close"], "volume": bar["volume"]
            })

    final_bars = final_bars[-limit:]
    if not final_bars:
        return None, "no_data_after_resample"

    if target_tf == "1D" and len(final_bars) >= 2:
        b1, b2 = final_bars[-2], final_bars[-1]
        if b1["time"] == b2["time"] or (b1["close"] == b2["close"] and b1["open"] == b2["open"] and b1["volume"] == b2["volume"]):
            final_bars.pop()

    vpa_flags_by_date = {}
    signal_info, history_signals = None, []
    if target_tf == "1D" and raw_bars and len(raw_bars) >= 30:
        try:
            if df_cached is not None:
                shared_df = df_cached
                if 'vpa_flag' in shared_df.columns:
                    for idx, val in shared_df['vpa_flag'].items():
                        if pd.notna(val):
                            vpa_flags_by_date[idx.strftime("%Y-%m-%d")] = int(val)
                else:
                    if _calc_vpa_flag_fn:
                        flags = _calc_vpa_flag_fn(shared_df)
                        for idx, f in flags.items():
                            vpa_flags_by_date[idx.strftime("%Y-%m-%d")] = int(f)
            else:
                shared_df = pd.DataFrame(
                    {k: [b[k] for b in raw_bars] for k in ("open","high","low","close","volume")},
                    index=pd.DatetimeIndex([datetime.fromtimestamp(b["t"]+25200, tz=timezone.utc) for b in raw_bars])
                )
                if _calc_vpa_flag_fn:
                    flags = _calc_vpa_flag_fn(shared_df).tolist()
                    for bar, f in zip(raw_bars, flags):
                        dt_str = time.strftime("%Y-%m-%d", time.gmtime(bar["t"] + 25200))
                        vpa_flags_by_date[dt_str] = int(f)
            
            fn = _calc_signals_fn or _default_calc_signals
            signal_info, history_signals = fn(shared_df)
        except Exception:
            pass

    candles = []
    volume = []
    
    # --- BẮC CẦU LIVE PRICE (Patch nến cuối cùng) ---
    live_data = None
    if target_tf == "1D" and final_bars and not before_date:
        # 1. Ưu tiên 1: Lấy từ _heatmap_cache (siêu tốc, update 5s/lần)
        with _heatmap_lock:
            h_data = _heatmap_cache.get("data", {})
            h_updated = _heatmap_cache.get("updated_at", 0)
            
            # Chỉ dùng Heatmap nếu dữ liệu vẫn còn tươi (được update trong vòng 60s)
            if time.time() - h_updated <= 60:
                if symbol in h_data and "price" in h_data[symbol]:
                    live_data = h_data[symbol]
        
        # 2. Ưu tiên 2 (Fallback): Lấy qua hàm ngoại vi (cache 5s)
        if live_data is None and _extra_quote_fn:
            now_ts_sec = time.time()
            need_fetch = False
            with _extra_quote_lock:
                cached_eq = _extra_quote_cache.get(symbol)
                # Dùng 4.5s thay vì 5s để bù trừ độ trễ mạng, khớp nhịp 5s của Frontend
                if not cached_eq or now_ts_sec - cached_eq.get("ts", 0) > 4.5:
                    need_fetch = True
                else:
                    live_data = cached_eq
            
            if need_fetch:
                try:
                    fresh = _extra_quote_fn([symbol]) or {}
                    if symbol in fresh and "price" in fresh[symbol]:
                        live_data = fresh[symbol]
                        with _extra_quote_lock:
                            _extra_quote_cache[symbol] = {
                                "price": live_data["price"], 
                                "pct": live_data.get("pct", 0), 
                                "open": live_data.get("open"),
                                "high": live_data.get("high"),
                                "low": live_data.get("low"),
                                "volume": live_data.get("volume"),
                                "date": live_data.get("date"),
                                "ts": now_ts_sec
                            }
                except Exception as e:
                    print(f"  [Dashboard] ❌ Fallback live price cho {symbol} lỗi: {e}")

    # Áp dụng dữ liệu live vào cây nến cuối cùng nếu có
    if live_data:
        last_b = final_bars[-1]
        live_d = live_data.get("date")
        if not live_d or live_d == last_b["time"]:
            for k in ("open", "high", "low", "volume"):
                if live_data.get(k) is not None: last_b[k] = live_data[k]
            if live_data.get("price") is not None: last_b["close"] = live_data["price"]
    # ------------------------------------------------
    
    for b in final_bars:
        t_val = b["time"]
        o, c = b.get("open") or b.get("close") or 0.0, b.get("close") or 0.0
        h, l = b.get("high") or max(o, c), b.get("low") or min(o, c)
        v = b.get("volume") or 0.0
        candles.append({"time": t_val, "open": o, "high": h, "low": l, "close": c})
        color = "#26a69a" if c >= o else "#ef5350"
        flag = vpa_flags_by_date.get(t_val)
        if flag:
            color = _VPA_FLAG_COLOR.get(flag) or color
        volume.append({"time": t_val, "value": v, "color": color})

    has_more = before_date is not None and len(final_bars) >= limit

    payload = {
        "symbol": symbol,
        "timeframe": target_tf,
        "candles": candles,
        "volume": volume,
        "last_date": str(candles[-1]["time"]),
        "has_more": has_more,
        "signal": signal_info,
        "history_signals": history_signals,
    }
    return _attach_rs_payload(payload, symbol), None

@app.route("/api/lightweight_chart/<symbol>")
def api_lightweight_chart(symbol):
    symbol = symbol.upper().strip()
    tf = (request.args.get("tf") or "1D").strip()
    before_date = (request.args.get("before") or "").strip() or None

    try:
        limit = int(request.args.get("limit", 450) or 450)
    except (TypeError, ValueError):
        limit = 450
    limit = max(5, min(1000, limit))

    if before_date:
        dchart_data, err = fetch_vndirect_dchart(symbol, tf, limit, before_date=before_date)
        if not err and dchart_data and dchart_data.get("candles"):
            return jsonify(dchart_data)
        return jsonify({"error": "vndirect_unavailable", "symbol": symbol,
                        "detail": err or "no_data"}), 502

    nocache = (request.args.get("nocache") == "1") or (request.args.get("refresh") == "1")
    cache_key = (symbol, tf)
    now_ts = time.time()
    with _lite_chart_cache_lock:
        entry = _lite_chart_cache.get(cache_key)

    if not nocache and entry and (now_ts - entry["ts"]) < _LITE_CHART_CACHE_TTL:
        return jsonify(_attach_rs_payload(entry["payload"], symbol))

    dchart_data, err = fetch_vndirect_dchart(symbol, tf, limit)
    if not err and dchart_data and dchart_data.get("candles"):
        dchart_data["has_more"] = True
        if limit >= 400 and not nocache:
            with _lite_chart_cache_lock:
                _lite_chart_cache[cache_key] = {"payload": dchart_data, "ts": now_ts}
        elif entry and entry.get("payload"):
            with _lite_chart_cache_lock:
                fp = entry["payload"]
                fc = fp.get("candles", [])
                fv = fp.get("volume", [])
                lb = dchart_data["candles"][-1]
                lv = dchart_data["volume"][-1]
                if fc:
                    if fc[-1].get("time") == lb.get("time"): fc[-1] = lb
                    else: fc.append(lb)
                if fv:
                    if fv[-1].get("time") == lv.get("time"): fv[-1] = lv
                    else: fv.append(lv)
                if limit < 60:
                    dchart_data["signal"] = fp.get("signal")
                    dchart_data["history_signals"] = fp.get("history_signals", [])
                else:
                    if "signal" in dchart_data: fp["signal"] = dchart_data["signal"]
                    if "history_signals" in dchart_data: fp["history_signals"] = dchart_data["history_signals"]
                entry["ts"] = now_ts
        return jsonify(dchart_data)

    if entry and entry.get("payload"):
        return jsonify(_attach_rs_payload(entry["payload"], symbol))

    return jsonify({"error": "vndirect_unavailable", "symbol": symbol,
                    "detail": err or "no_data"}), 502

@app.route("/api/cache_info")
def api_cache_info():
    cache = _get_history_cache() if _get_history_cache else {}
    info = []
    with _cache_lock:
        for sym, df in list(cache.items())[:10]:
            if df is not None and len(df) > 0:
                info.append({"symbol": sym, "rows": len(df),
                             "last_date": str(df.index[-1].date())})
    return jsonify({"total_symbols": len(cache), "sample": info,
                    "updated_at": datetime.now(TZ_VN).strftime("%H:%M:%S")})

@app.route("/api/status")
def api_status():
    cache = _get_history_cache() if _get_history_cache else {}
    return jsonify({"status": "running", "cache_symbols": len(cache),
                    "server_time": datetime.now(TZ_VN).strftime("%H:%M:%S %d/%m/%Y")})

@app.route("/api/config")
def api_config():
    try:
        import scanner_full as sf
        m_start = getattr(sf, 'SESSION_MORNING_START', 85500)
        m_end   = getattr(sf, 'SESSION_MORNING_END', 113000)
        a_start = getattr(sf, 'SESSION_AFTERNOON_START', 130000)
        a_end   = getattr(sf, 'SESSION_AFTERNOON_END', 233000)
    except Exception:
        m_start, m_end, a_start, a_end = 85500, 113000, 130000, 233000

    return jsonify({
        "signal_ttl_sec": SIGNAL_TTL_SEC,
        "heatmap_ttl_sec": _heatmap_poll_interval(),
        "market_health_ttl_sec": MARKET_HEALTH_TTL_SEC,
        "session_morning_start": m_start,
        "session_morning_end": m_end,
        "session_afternoon_start": a_start,
        "session_afternoon_end": a_end,
    })

@app.route("/api/alerts", methods=["GET", "POST"])
def api_alerts():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        try:
            payload = _validate_alert_rule_payload(data)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400
        now = _now_vn_iso()
        with _price_alert_lock, _price_alert_conn() as conn:
            cur = conn.execute("""
                INSERT INTO price_alert_rules
                (client_id, symbol, left_type, left_ma_kind, left_period, operator,
                 right_type, right_value, right_ma_kind, right_period,
                 notify_dashboard, notify_telegram, telegram_chat_id,
                 after_trigger, active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """, (
                payload["client_id"], payload["symbol"], payload["left_type"],
                payload["left_ma_kind"], payload["left_period"], payload["operator"],
                payload["right_type"], payload["right_value"], payload["right_ma_kind"],
                payload["right_period"], payload["notify_dashboard"],
                payload["notify_telegram"], payload["telegram_chat_id"],
                payload["after_trigger"], now, now,
            ))
            conn.commit()
            row = conn.execute("SELECT * FROM price_alert_rules WHERE id=?", (cur.lastrowid,)).fetchone()
        return jsonify({"rule": _rule_to_dict(row)})

    client_id = _get_alert_client_id()
    if not client_id:
        return jsonify({"error": "missing_client_id"}), 400
    with _price_alert_lock, _price_alert_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM price_alert_rules
            WHERE client_id=?
            ORDER BY active DESC, updated_at DESC, id DESC
        """, (client_id,)).fetchall()
    return jsonify({"rules": [_rule_to_dict(row) for row in rows]})


@app.route("/api/alerts/<int:rule_id>/toggle", methods=["POST"])
def api_alert_toggle(rule_id):
    data = request.get_json(silent=True) or {}
    client_id = _get_alert_client_id(data)
    active = 1 if data.get("active") else 0
    if not client_id:
        return jsonify({"error": "missing_client_id"}), 400
    with _price_alert_lock, _price_alert_conn() as conn:
        conn.execute("""
            UPDATE price_alert_rules SET active=?, updated_at=?
            WHERE id=? AND client_id=?
        """, (active, _now_vn_iso(), rule_id, client_id))
        conn.commit()
        row = conn.execute("""
            SELECT * FROM price_alert_rules WHERE id=? AND client_id=?
        """, (rule_id, client_id)).fetchone()
    if not row:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"rule": _rule_to_dict(row)})


@app.route("/api/alerts/<int:rule_id>", methods=["PUT", "DELETE"])
def api_alert_update_or_delete(rule_id):
    if request.method == "PUT":
        data = request.get_json(silent=True) or {}
        client_id = _get_alert_client_id(data)
        if not client_id:
            return jsonify({"error": "missing_client_id"}), 400
        try:
            payload = _validate_alert_rule_payload(data)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400
        with _price_alert_lock, _price_alert_conn() as conn:
            conn.execute("""
                UPDATE price_alert_rules SET
                    symbol=?, left_type=?, left_ma_kind=?, left_period=?, operator=?,
                    right_type=?, right_value=?, right_ma_kind=?, right_period=?,
                    notify_dashboard=?, notify_telegram=?, telegram_chat_id=?,
                    after_trigger=?, updated_at=?
                WHERE id=? AND client_id=?
            """, (
                payload["symbol"], payload["left_type"], payload["left_ma_kind"],
                payload["left_period"], payload["operator"], payload["right_type"],
                payload["right_value"], payload["right_ma_kind"], payload["right_period"],
                payload["notify_dashboard"], payload["notify_telegram"],
                payload["telegram_chat_id"], payload["after_trigger"], _now_vn_iso(),
                rule_id, client_id,
            ))
            conn.commit()
            row = conn.execute("""
                SELECT * FROM price_alert_rules WHERE id=? AND client_id=?
            """, (rule_id, client_id)).fetchone()
        if not row:
            return jsonify({"error": "not_found"}), 404
        return jsonify({"rule": _rule_to_dict(row)})

    client_id = _get_alert_client_id()
    if not client_id:
        return jsonify({"error": "missing_client_id"}), 400
    with _price_alert_lock, _price_alert_conn() as conn:
        cur = conn.execute("""
            DELETE FROM price_alert_rules WHERE id=? AND client_id=?
        """, (rule_id, client_id))
        conn.commit()
    return jsonify({"deleted": cur.rowcount > 0})


@app.route("/api/alerts/feed")
def api_alert_feed():
    client_id = _get_alert_client_id()
    if not client_id:
        return jsonify({"error": "missing_client_id"}), 400
    try:
        limit = max(1, min(50, int(request.args.get("limit", 20))))
    except (TypeError, ValueError):
        limit = 20
    with _price_alert_lock, _price_alert_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM price_alert_events
            WHERE client_id=?
            ORDER BY id DESC
            LIMIT ?
        """, (client_id, limit)).fetchall()
        unseen = conn.execute("""
            SELECT COUNT(*) AS n FROM price_alert_events
            WHERE client_id=? AND seen=0
        """, (client_id,)).fetchone()["n"]
    return jsonify({"events": [_event_to_dict(row) for row in rows], "unseen_count": unseen})


@app.route("/api/alerts/seen", methods=["POST"])
def api_alert_seen():
    data = request.get_json(silent=True) or {}
    client_id = _get_alert_client_id(data)
    if not client_id:
        return jsonify({"error": "missing_client_id"}), 400
    with _price_alert_lock, _price_alert_conn() as conn:
        conn.execute("UPDATE price_alert_events SET seen=1 WHERE client_id=?", (client_id,))
        conn.commit()
    return jsonify({"ok": True})


@app.route("/api/alerts/test_telegram", methods=["POST"])
def api_alert_test_telegram():
    data = request.get_json(silent=True) or {}
    chat_id = str(data.get("telegram_chat_id") or "").strip()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not chat_id:
        return jsonify({"error": "missing_telegram_chat_id"}), 400
    if not token:
        return jsonify({"error": "telegram_bot_token_not_configured"}), 503
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": "Test cảnh báo từ Dashboard", "parse_mode": "HTML"},
            timeout=10,
        )
        if not resp.ok:
            return jsonify({"error": "telegram_error", "detail": resp.text[:500]}), 400
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

@app.route("/journal")
def journal_view():
    return Response(JOURNAL_HTML, mimetype="text/html")

@app.route("/journal/uploads/<path:filename>")
def journal_upload(filename):
    _init_journal_storage()
    return send_from_directory(JOURNAL_UPLOAD_DIR, filename)

@app.route("/api/journal/me")
def api_journal_me():
    return jsonify({"admin": _is_admin()})

@app.route("/api/journal/login", methods=["POST"])
def api_journal_login():
    admin_password = os.environ.get("DASHBOARD_ADMIN_PASSWORD", "")
    if not admin_password:
        return jsonify({"error": "admin_password_not_configured"}), 503
    data = request.get_json(silent=True) or {}
    password = str(data.get("password", ""))
    if not hmac.compare_digest(password, admin_password):
        return jsonify({"error": "invalid_password"}), 401
    session["journal_admin"] = True
    return jsonify({"admin": True})

@app.route("/api/journal/logout", methods=["POST"])
def api_journal_logout():
    session.pop("journal_admin", None)
    return jsonify({"admin": False})

@app.route("/api/journal/entries")
def api_journal_entries():
    symbol = request.args.get("symbol", "").upper().strip()
    status = request.args.get("status", "").strip()
    with _journal_lock, _journal_conn() as conn:
        where, params = [], []
        if symbol:
            where.append("symbol LIKE ?")
            params.append(f"%{symbol}%")
        if status:
            where.append("status=?")
            params.append(status)
        sql = "SELECT * FROM journal_entries"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY COALESCE(buy_date, created_at) DESC, id DESC"
        rows = conn.execute(sql, params).fetchall()
        ids = [row["id"] for row in rows]
        image_map = {entry_id: [] for entry_id in ids}
        if ids:
            marks = ",".join("?" for _ in ids)
            img_rows = conn.execute(
                f"SELECT * FROM journal_images WHERE entry_id IN ({marks}) ORDER BY id",
                ids,
            ).fetchall()
            for img in img_rows:
                image_map.setdefault(img["entry_id"], []).append({
                    "id": img["id"],
                    "url": f"/journal/uploads/{img['filename']}",
                    "filename": img["filename"],
                    "original_name": img["original_name"] or "",
                    "created_at": img["created_at"],
                })
        entries = [_entry_to_dict(row, image_map.get(row["id"], [])) for row in rows]
    return jsonify({"entries": entries, "count": len(entries), "admin": _is_admin()})

@app.route("/api/journal/warning")
def api_journal_warning():
    _init_journal_storage()
    raw = JOURNAL_WARNING_PATH.read_text(encoding="utf-8") if JOURNAL_WARNING_PATH.exists() else ""
    try:
        data = json.loads(raw) if raw.strip().startswith("{") else {"text": raw, "tone": "normal"}
    except Exception:
        data = {"text": raw, "tone": "normal"}

        data = {"text": raw, "tone": "normal"}
    return jsonify({"text": data.get("text", ""), "tone": data.get("tone", "normal"), "admin": _is_admin()})

@app.route("/api/journal/warning", methods=["PUT"])
@require_journal_admin
def api_journal_warning_update():
    _init_journal_storage()
    data = request.get_json(silent=True) or {}
    text = _safe_text(data.get("text"), 5000)
    tone = _safe_text(data.get("tone"), 20) or "normal"
    if tone not in ("green", "red", "normal"):
        tone = "normal"
    JOURNAL_WARNING_PATH.write_text(json.dumps({"text": text, "tone": tone}, ensure_ascii=False), encoding="utf-8")
    return jsonify({"ok": True})

@app.route("/api/journal/entries", methods=["POST"])
@require_journal_admin
def api_journal_create():
    data = request.get_json(silent=True) or {}
    symbol = _safe_text(data.get("symbol"), 20).upper()
    if not symbol:
        return jsonify({"error": "symbol_required"}), 400
    now = _now_vn_iso()
    with _journal_lock, _journal_conn() as conn:
        cur = conn.execute("""
            INSERT INTO journal_entries
                (symbol, buy_date, signal, price, stoploss, target, title, notes, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            symbol,
            _safe_text(data.get("buy_date"), 20),
            _safe_text(data.get("signal"), 120),
            _safe_text(data.get("price"), 40),
            _safe_text(data.get("stoploss"), 40),
            _safe_text(data.get("target"), 40),
            _safe_text(data.get("title"), 240),
            _safe_text(data.get("notes"), 5000),
            _safe_text(data.get("status"), 40) or "check",
            now,
            now,
        ))
        conn.commit()
        entry_id = cur.lastrowid
    return jsonify({"id": entry_id, "ok": True})

@app.route("/api/journal/entries/<int:entry_id>", methods=["PUT"])
@require_journal_admin
def api_journal_update(entry_id):
    data = request.get_json(silent=True) or {}
    symbol = _safe_text(data.get("symbol"), 20).upper()
    if not symbol:
        return jsonify({"error": "symbol_required"}), 400
    with _journal_lock, _journal_conn() as conn:
        cur = conn.execute("""
            UPDATE journal_entries
            SET symbol=?, buy_date=?, signal=?, price=?, stoploss=?, target=?, title=?, notes=?, status=?, updated_at=?
            WHERE id=?
        """, (
            symbol,
            _safe_text(data.get("buy_date"), 20),
            _safe_text(data.get("signal"), 120),
            _safe_text(data.get("price"), 40),
            _safe_text(data.get("stoploss"), 40),
            _safe_text(data.get("target"), 40),
            _safe_text(data.get("title"), 240),
            _safe_text(data.get("notes"), 5000),
            _safe_text(data.get("status"), 40) or "check",
            _now_vn_iso(),
            entry_id,
        ))
        conn.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"ok": True})

@app.route("/api/journal/entries/<int:entry_id>", methods=["DELETE"])
@require_journal_admin
def api_journal_delete(entry_id):
    with _journal_lock, _journal_conn() as conn:
        imgs = conn.execute("SELECT filename FROM journal_images WHERE entry_id=?", (entry_id,)).fetchall()
        cur = conn.execute("DELETE FROM journal_entries WHERE id=?", (entry_id,))
        conn.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "not_found"}), 404
    for img in imgs:
        try:
            (JOURNAL_UPLOAD_DIR / img["filename"]).unlink(missing_ok=True)
        except Exception:
            pass

            pass
    return jsonify({"ok": True})

@app.route("/api/journal/entries/<int:entry_id>/images", methods=["POST"])
@require_journal_admin
def api_journal_upload_image(entry_id):
    files = request.files.getlist("images")
    if not files:
        return jsonify({"error": "images_required"}), 400
    saved = []
    now = _now_vn_iso()
    with _journal_lock, _journal_conn() as conn:
        entry = conn.execute("SELECT id FROM journal_entries WHERE id=?", (entry_id,)).fetchone()
        if not entry:
            return jsonify({"error": "not_found"}), 404
        for file in files:
            ext = _uploaded_ext(file.filename)
            if ext not in JOURNAL_ALLOWED_EXT:
                return jsonify({"error": f"unsupported_file_type:{ext or 'none'}"}), 400
            filename = f"{datetime.now(TZ_VN).strftime('%Y%m%d')}_{uuid4().hex}.{ext}"
            file.save(JOURNAL_UPLOAD_DIR / filename)
            cur = conn.execute("""
                INSERT INTO journal_images (entry_id, filename, original_name, created_at)
                VALUES (?, ?, ?, ?)
            """, (entry_id, filename, _safe_text(file.filename, 240), now))
            saved.append({"id": cur.lastrowid, "url": f"/journal/uploads/{filename}", "filename": filename})
        conn.execute("UPDATE journal_entries SET updated_at=? WHERE id=?", (now, entry_id))
        conn.commit()
    return jsonify({"ok": True, "images": saved})

@app.route("/api/journal/images/<int:image_id>", methods=["DELETE"])
@require_journal_admin
def api_journal_delete_image(image_id):
    with _journal_lock, _journal_conn() as conn:
        img = conn.execute("SELECT filename FROM journal_images WHERE id=?", (image_id,)).fetchone()
        if not img:
            return jsonify({"error": "not_found"}), 404
        conn.execute("DELETE FROM journal_images WHERE id=?", (image_id,))
        conn.commit()
    try:
        (JOURNAL_UPLOAD_DIR / img["filename"]).unlink(missing_ok=True)
    except Exception:
        pass

        pass
    return jsonify({"ok": True})

@app.route("/")
def index():
    return Response(DASHBOARD_HTML, mimetype="text/html")

@app.route("/dashboard-main.js")
def dashboard_main_js():
    """JS chính của dashboard — tách khỏi DASHBOARD_HTML (xem giải thích ở khai báo
    DASHBOARD_MAIN_JS phía dưới). __HMAP_COLS_CONFIG__/__TS_POOL_CONFIG__ được thay ở
    đây (giống hệt logic .replace() cũ từng nằm trong index()), không đổi giá trị/format."""
    js = (
        DASHBOARD_MAIN_JS
        .replace("__HMAP_COLS_CONFIG__", json.dumps(HMAP_COLS_CONFIG, ensure_ascii=False))
        .replace("__TS_POOL_CONFIG__", json.dumps(TS_POOL_CONFIG, ensure_ascii=False))
    )
    resp = Response(js, content_type="application/javascript; charset=utf-8")
    resp.headers["Cache-Control"] = "no-cache"
    return resp

def start_dashboard(alerted_today_ref, history_cache_ref, cache_lock_ref,
                    fetch_heatmap_fn, signal_emoji_ref, signal_rank_ref,
                    vol_forecast_fn=None,
                    calc_vpa_flag_fn=None,
                    momentum_today_ref=None, fetch_market_health_fn=None,
                    signal_session_date_ref=None, port=8888,
                    attent_today_ref=None, breakvol_today_ref=None,
                    extra_quote_fn=None, rs_universe_ref=None,
                    calc_signals_fn=None, sync_heatmap_fn=None):
    global _get_alerted_today, _get_momentum_today, _get_attent_today, _get_breakvol_today, _get_signal_session_date, _get_history_cache, _get_rs_universe_symbols, _cache_lock
    global _fetch_heatmap_fn, _fetch_market_health_fn, _vol_forecast_fn, _calc_vpa_flag_fn, _signal_emoji, _signal_rank, _extra_quote_fn, _calc_signals_fn, _sync_heatmap_fn
    _get_alerted_today = alerted_today_ref
    _get_momentum_today = momentum_today_ref
    _get_attent_today = attent_today_ref
    _get_breakvol_today = breakvol_today_ref
    _get_signal_session_date = signal_session_date_ref
    _get_history_cache = history_cache_ref
    _get_rs_universe_symbols = rs_universe_ref
    _cache_lock        = cache_lock_ref
    _fetch_heatmap_fn  = fetch_heatmap_fn
    _fetch_market_health_fn = fetch_market_health_fn
    _vol_forecast_fn   = vol_forecast_fn
    _calc_vpa_flag_fn  = calc_vpa_flag_fn
    _signal_emoji      = signal_emoji_ref
    _signal_rank       = signal_rank_ref
    _extra_quote_fn    = extra_quote_fn
    _calc_signals_fn   = calc_signals_fn
    _sync_heatmap_fn   = sync_heatmap_fn

    def _run():
        import logging
        logging.getLogger('werkzeug').setLevel(logging.ERROR)
        app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)

    threading.Thread(target=_run, daemon=True).start()
    print(f"🌐 Dashboard tại http://0.0.0.0:{port}")
    print(f"   Tín hiệu: {SIGNAL_TTL_SEC}s | Heatmap: {HEATMAP_TTL_SEC}s | HEALTH: {MARKET_HEALTH_TTL_SEC}s")


JOURNAL_HTML = r"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Note</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600;700&family=IBM+Plex+Sans:wght@400;500;600;700&family=Barlow+Condensed:wght@600;700;800&display=swap" rel="stylesheet">
<style>
:root{--bg:#f6f7fb;--surface:#fff;--surf2:#eef2f7;--border:#dbe2ec;--text:#111827;--muted:#6b7280;--accent:#1a56db;--green:#0e9f6e;--red:#e02424;--font-mono:'IBM Plex Mono',monospace;--font-ui:'Barlow Condensed',sans-serif}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:var(--font-mono);font-size:13px;min-height:100vh}
header{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:10px;padding:10px 14px;background:var(--surface);border-bottom:1px solid var(--border);box-shadow:0 1px 5px rgba(0,0,0,.06)}
h1{font-family:var(--font-ui);font-size:18px;letter-spacing:1.8px;text-transform:uppercase;color:var(--accent);white-space:nowrap}
.spacer{flex:1}
.meta{font-size:10px;color:var(--muted);white-space:nowrap}
button,.btn{height:30px;padding:0 12px;border-radius:5px;border:1px solid var(--border);background:var(--surface);color:var(--muted);font-family:var(--font-mono);font-size:12px;font-weight:700;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;text-decoration:none}
button:hover,.btn:hover{background:#eef3ff;color:var(--accent);border-color:var(--accent)}
#login-cancel:hover,#login-close:hover{background:var(--red);color:#fff;border-color:var(--red)}
button.primary{background:var(--accent);color:#fff;border-color:var(--accent)}
button.danger:hover{background:var(--red);color:#fff;border-color:var(--red)}
button.green{background:var(--surface);color:var(--muted);border-color:var(--border);}
button.green:hover{background:#eef3ff;color:var(--accent);border-color:var(--accent);}
#btn-cancel:hover{background:var(--red);color:#fff;border-color:var(--red);}
#warning-clear:hover{background:var(--red);color:#fff;border-color:var(--red);}
.header-close{width:30px;height:30px;border-radius:5px;padding:0;font-size:15px;transition:all .15s;}
.header-close:hover{background:var(--red); color:#fff; border-color:var(--red);}
 main{padding:14px;display:flex;flex-direction:column;gap:12px}
.panel{background:var(--surface);border:1px solid var(--border);border-radius:8px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.05)}
.panel-h{display:flex;align-items:center;justify-content:space-between;padding:9px 12px;background:var(--surf2);border-bottom:1px solid var(--border)}
.panel-h-main{display:flex;align-items:center;gap:10px;min-width:0}
.panel-title{font-family:var(--font-ui);font-size:13px;font-weight:800;text-transform:uppercase;letter-spacing:1.6px;color:var(--accent)}
.panel-b{padding:12px}
.filters{display:flex;gap:7px;align-items:center}
.filters input{width:130px}.filters select{width:130px}
input,textarea,select{width:100%;border:1px solid var(--border);border-radius:5px;background:#fff;color:var(--text);font-family:var(--font-mono);font-size:12px;outline:none}
input,select{height:32px;padding:0 9px}
textarea{min-height:96px;padding:8px 9px;resize:vertical}
input:focus,textarea:focus,select:focus{border-color:var(--accent);box-shadow:0 0 0 2px rgba(26,86,219,.12)}
.form{display:none;grid-template-columns:1fr 1fr;gap:9px}
.form.on{display:grid}
.field.full{grid-column:1/-1}
.field label{display:block;margin-bottom:4px;font-size:10px;color:var(--muted);font-weight:700;text-transform:uppercase;letter-spacing:.7px}
.form-actions{grid-column:1/-1;display:flex;gap:8px;justify-content:flex-end;align-items:center}
.edit-panel{display:none}
.edit-panel.on{display:block}
.list{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:10px}
.card{background:#fff;border:1px solid var(--border);border-radius:8px;overflow:hidden}
.card.editing{border-color:var(--accent);box-shadow:0 0 0 2px rgba(26,86,219,.18),0 6px 18px rgba(26,86,219,.12)}
.card.editing .card-h{background:#eef3ff}
.card-h{display:flex;align-items:flex-start;gap:8px;padding:10px 11px;background:#fbfcff;border-bottom:1px solid var(--border)}
.sym{font-family:var(--font-ui);font-size:21px;font-weight:800;color:var(--accent);letter-spacing:1px;cursor:pointer}
.sym:hover{text-decoration:underline}
.ch-meta{font-size:10px;color:var(--muted);line-height:1.45}
.status{margin-left:auto;font-size:10px;font-weight:800;border-radius:999px;padding:3px 8px;border:1px solid var(--border);color:var(--muted);white-space:nowrap}
.status.bought{color:#0e7b54;background:#dcfce7;border-color:#86efac}
.status.check,.status.watching{color:#9a5b00;background:#fef3c7;border-color:#fcd34d}
.status.closed{color:#6b7280;background:#f1f5f9;border-color:#cbd5e1}
.card-b{padding:10px 11px;display:flex;flex-direction:column;gap:8px}
.title{font-weight:800;font-size:13px}
.notes{font-size:12px;line-height:1.5;white-space:pre-wrap;color:#374151}
.kv{display:flex;flex-wrap:wrap;gap:5px}
.tag{font-size:10px;padding:3px 7px;border-radius:4px;background:#f1f5f9;border:1px solid #dbe2ec;color:#475569;font-weight:700}
.imgs{display:grid;grid-template-columns:repeat(3,1fr);gap:5px}
.img-wrap{position:relative;border:1px solid var(--border);border-radius:5px;overflow:hidden;background:#f8fafc;aspect-ratio:4/3}
.img-wrap img{width:100%;height:100%;object-fit:cover;display:block;cursor:zoom-in}
.img-del{position:absolute;top:4px;right:4px;width:24px;height:24px;padding:0;border:none;border-radius:50%;background:transparent;color:rgba(255,255,255,.75);display:none;align-items:center;justify-content:center;backdrop-filter:blur(2px);transition:all .15s ease}
.img-del:hover{background:rgba(224,36,36,.95);color:#fff}
.admin .img-del{display:flex}
.card-actions{display:none;gap:7px;justify-content:flex-end;border-top:1px solid var(--border);padding:9px 11px;background:#fbfcff}
.admin .card-actions{display:flex}
.upload-inline{display:none;margin-top:4px}
.admin .upload-inline{display:block}
.uploaded-list{display:none;grid-column:1/-1;border:1px solid var(--border);border-radius:6px;background:#fbfcff;padding:8px;gap:6px}
.uploaded-list.on{display:grid}
.uploaded-row{display:grid;grid-template-columns:42px 1fr auto;align-items:center;gap:8px;padding:5px;border:1px solid #e5eaf2;border-radius:5px;background:#fff}
.uploaded-row img{width:42px;height:32px;object-fit:cover;border-radius:4px;border:1px solid var(--border);cursor:zoom-in}
.uploaded-name{font-size:11px;color:#374151;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.warning-panel{display:none;border-color:#fcd34d;background:#fffbeb}
.warning-panel.on{display:block}
.warning-panel.normal{border-color:#fcd34d;background:#fffbeb}
.warning-panel.green{border-color:#86efac;background:#ecfdf5}
.warning-panel.red{border-color:#fecaca;background:#fff1f2}
.warning-text{font-size:12px;line-height:1.55;white-space:pre-wrap;color:#374151}
.warning-edit{display:none;gap:8px}
.warning-edit.on{display:grid}
.tone-dots{display:flex;gap:8px;align-items:center}
.tone-dot{width:18px;height:18px;border-radius:50%;border:2px solid rgba(17,24,39,.18);cursor:pointer;display:inline-flex;align-items:center;justify-content:center}
.tone-dot input{display:none}
.tone-dot.normal{background:#fcd34d}
.tone-dot.green{background:#86efac}
.tone-dot.red{background:#fecaca}
.tone-dot:has(input:checked){box-shadow:0 0 0 3px rgba(26,86,219,.22);border-color:var(--accent)}
.empty{padding:40px 20px;text-align:center;color:var(--muted)}
#viewer{display:none;position:fixed;inset:0;z-index:100;background:rgba(17,24,39,.82);align-items:center;justify-content:center;padding:18px}
#viewer.on{display:flex}
#viewer img{max-width:96vw;max-height:92vh;object-fit:contain;background:#fff;border-radius:4px}
#viewer button{position:absolute;top:14px;right:14px;border-radius:5px;width:36px;height:36px;padding:0;background:#fff;color:#111;border:1px solid var(--border);transition:all .15s}
#viewer button:hover{background:var(--red);color:#fff;border-color:var(--red)}
#viewer .viewer-nav{top:50%;transform:translateY(-50%);width:42px;height:42px;font-size:20px;background:rgba(255,255,255,.92)}
#viewer-prev{left:16px;right:auto}
#viewer-next{right:16px}
.login-modal{display:none;position:fixed;inset:0;z-index:120;background:rgba(17,24,39,.55);align-items:center;justify-content:center;padding:16px}
.login-modal.on{display:flex}
.login-box{width:min(360px,94vw);background:#fff;border:1px solid var(--border);border-radius:8px;box-shadow:0 18px 50px rgba(0,0,0,.2);overflow:hidden}
.login-h{display:flex;align-items:center;justify-content:space-between;padding:10px 12px;background:var(--surf2);border-bottom:1px solid var(--border)}
.pass-wrap{position:relative}
.pass-wrap input{padding-right:42px}
.eye-btn{position:absolute;right:4px;top:4px;width:28px;height:24px;padding:0;border:none;background:transparent;color:var(--muted)}
@media(max-width:840px){.form{grid-template-columns:1fr}.meta{display:none}.list{grid-template-columns:1fr}.panel-h{align-items:flex-start;gap:8px}.panel-h-main{flex-direction:column;align-items:flex-start}.filters{width:100%;overflow-x:auto}.filters input,.filters select{width:120px;flex-shrink:0}}
</style>
</head>
<body>
<header>
  <h1>★ Note</h1>
  <span class="meta" id="mode-meta"></span>
  <div class="spacer"></div>
  <button id="btn-new" style="display:none">+</button>
  <button id="btn-login">✎</button>
  <button id="btn-logout" class="danger" style="display:none">Logout</button>
  <button id="journal-close-inline" class="header-close">✕</button>
</header>
<main id="app">
  <section class="panel edit-panel" id="entry-panel">
    <div class="panel-h"><span class="panel-title">Chi tiết</span></div>
    <div class="panel-b">
      <form id="entry-form" class="form">
        <input type="hidden" id="entry-id">
        <div class="field"><label>Mã</label><input id="symbol" maxlength="20" required></div>
        <div class="field"><label>Ngày mua</label><input id="buy-date" type="date"></div>
        <div class="field"><label>Tín hiệu</label><input id="signal" maxlength="120"></div>
        <div class="field"><label>Giá</label><input id="price" maxlength="40"></div>
        <div class="field"><label>Stoploss</label><input id="stoploss" maxlength="40"></div>
        <div class="field"><label>Target</label><input id="target" maxlength="40"></div>
        <div class="field"><label>Trạng thái</label><select id="status"><option value="check">Check</option><option value="watching">Theo dõi</option><option value="bought">Đã mua</option><option value="closed">Đã đóng</option></select></div>
        <div class="field"><label>Tiêu đề</label><input id="title" maxlength="240"></div>
        <div class="field full"><label>Ghi chú</label><textarea id="notes"></textarea></div>
        <div class="uploaded-list" id="uploaded-list"></div>
        <div class="field full"><label>Ảnh điểm mua</label><input id="images" type="file" accept="image/png,image/jpeg,image/webp,image/gif" multiple></div>
        <div class="form-actions"><button class="green" type="submit">✓</button><button type="button" id="btn-cancel">✕</button></div>
      </form>
    </div>
  </section>
  <section class="panel warning-panel" id="warning-panel">
    <div class="panel-h"><span class="panel-title">Cảnh báo thị trường</span></div>
    <div class="panel-b">
      <div class="warning-text" id="warning-text"></div>
      <form class="warning-edit" id="warning-form">
        <textarea id="warning-input" placeholder="Nhập cảnh báo thị trường..."></textarea>
        <div class="form-actions">
          <div class="tone-dots">
            <label class="tone-dot normal"><input type="radio" name="warning-tone" value="normal" checked></label>
            <label class="tone-dot green"><input type="radio" name="warning-tone" value="green"></label>
            <label class="tone-dot red"><input type="radio" name="warning-tone" value="red"></label>
          </div>
          <button class="green" type="submit">✓</button><button type="button" id="warning-clear">✕</button>
        </div>
      </form>
    </div>
  </section>
  <section class="panel">
    <div class="panel-h">
      <div class="panel-h-main">
        <span class="panel-title">Danh sách</span>
        <div class="filters">
          <input id="f-symbol" placeholder="Mã CK" maxlength="12" autocomplete="off">
          <select id="f-status">
            <option value="">Tất cả</option>
            <option value="check">Check</option>
            <option value="watching">Theo dõi</option>
            <option value="bought">Đã mua</option>
            <option value="closed">Đã đóng</option>
          </select>
        </div>
      </div>
      <span class="meta" id="count-meta">0 mục</span>
    </div>
    <div class="panel-b"><div class="list" id="list"></div></div>
  </section>
</main>
<div id="viewer"><button id="viewer-close">✕</button><button class="viewer-nav" id="viewer-prev">&lt;</button><img id="viewer-img" alt=""><button class="viewer-nav" id="viewer-next">&gt;</button></div>
<div class="login-modal" id="login-modal">
  <form class="login-box" id="login-form">
    <div class="login-h"><span class="panel-title">Edit mode</span><button type="button" id="login-close">✕</button></div>
    <div class="panel-b">
      <div class="field full"><label>Mật khẩu</label><div class="pass-wrap"><input id="login-password" type="password" autocomplete="current-password"><button type="button" class="eye-btn" id="toggle-pass">👁</button></div></div>
      <div class="form-actions" style="margin-top:10px"><button type="button" id="login-cancel">Hủy</button><button class="primary" type="submit">Đăng nhập</button></div>
    </div>
  </form>
</div>
<script>
'use strict';
const $=id=>document.getElementById(id);
const S={admin:false,entries:[],editingId:null,viewerImages:[],viewerIdx:0,symTimer:null,warning:'',warningTone:'normal'};
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
async function api(url,opt){const r=await fetch(url,opt);const j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.error||('HTTP '+r.status));return j;}
function payload(){return{symbol:$('symbol').value.trim().toUpperCase(),buy_date:$('buy-date').value,signal:$('signal').value.trim(),price:$('price').value.trim(),stoploss:$('stoploss').value.trim(),target:$('target').value.trim(),title:$('title').value.trim(),notes:$('notes').value.trim(),status:$('status').value};}
function setAdmin(on){S.admin=!!on;document.body.classList.toggle('admin',S.admin);$('mode-meta').textContent=S.admin?'Edit mode':'';$('btn-login').style.display=S.admin?'none':'';$('btn-logout').style.display=S.admin?'':'none';$('btn-new').style.display=S.admin?'':'none';renderWarning();if(!S.admin)hideForm();}
function renderUploaded(entry){const box=$('uploaded-list');const imgs=(entry&&entry.images)||[];if(!imgs.length){box.classList.remove('on');box.innerHTML='';return;}box.classList.add('on');box.innerHTML=imgs.map((img,i)=>`<div class="uploaded-row"><img src="${img.url}" alt="" data-form-img-idx="${i}"><span class="uploaded-name">${esc(img.original_name||img.filename||img.url)}</span><button type="button" class="danger" data-form-img-del="${img.id}">✕</button></div>`).join('');}
function showForm(entry){if(!S.admin)return;const e=entry||{};S.editingId=e.id||null;$('entry-id').value=e.id||'';$('symbol').value=e.symbol||'';$('buy-date').value=e.buy_date||'';$('signal').value=e.signal||'';$('price').value=e.price||'';$('stoploss').value=e.stoploss||'';$('target').value=e.target||'';$('title').value=e.title||'';$('notes').value=e.notes||'';$('status').value=e.status||'check';$('images').value='';renderUploaded(e);$('entry-panel').classList.add('on');$('entry-form').classList.add('on');render();$('symbol').focus();}
function hideForm(){S.editingId=null;$('entry-form').classList.remove('on');$('entry-panel').classList.remove('on');$('uploaded-list').classList.remove('on');$('uploaded-list').innerHTML='';$('entry-form').reset();$('entry-id').value='';render();}
async function loadMe(){try{const j=await api('/api/journal/me');setAdmin(j.admin);}catch(e){setAdmin(false);}}
async function loadEntries(){const qs=new URLSearchParams();if($('f-symbol').value.trim())qs.set('symbol',$('f-symbol').value.trim().toUpperCase());if($('f-status').value)qs.set('status',$('f-status').value);const j=await api('/api/journal/entries?'+qs.toString());S.entries=j.entries||[];$('count-meta').textContent='';render();}
async function loadWarning(){try{const j=await api('/api/journal/warning');S.warning=j.text||'';S.warningTone=j.tone||'normal';renderWarning();}catch(e){}}
function renderWarning(){const has=S.warning.trim().length>0;const p=$('warning-panel');p.classList.toggle('on',S.admin||has);p.classList.toggle('normal',S.warningTone==='normal');p.classList.toggle('green',S.warningTone==='green');p.classList.toggle('red',S.warningTone==='red');$('warning-text').style.display=has?'':'none';$('warning-text').textContent=S.warning;$('warning-input').value=S.warning;document.querySelectorAll('input[name="warning-tone"]').forEach(r=>{r.checked=r.value===S.warningTone;});$('warning-form').classList.toggle('on',S.admin);}
function statusLabel(s){return s==='check'?'Check':s==='bought'?'Đã mua':s==='closed'?'Đã đóng':'Theo dõi';}
function render(){const box=$('list');if(!S.entries.length){box.innerHTML='<div class="empty">Chưa có Note nào</div>';return;}box.innerHTML=S.entries.map(e=>`
  <article class="card${String(S.editingId||'')===String(e.id)?' editing':''}" data-id="${e.id}">
    <div class="card-h"><div><div class="sym" data-journal-sym="${esc(e.symbol)}" title="Nhảy chart">${esc(e.symbol)}</div><div class="ch-meta">${esc(e.buy_date||'')}</div></div><span class="status ${esc(e.status)}">${statusLabel(e.status)}</span></div>
    <div class="card-b">
      ${e.title?`<div class="title">${esc(e.title)}</div>`:''}
      <div class="kv">${e.signal?`<span class="tag">${esc(e.signal)}</span>`:''}${e.price?`<span class="tag">Giá: ${esc(e.price)}</span>`:''}${e.stoploss?`<span class="tag">SL: ${esc(e.stoploss)}</span>`:''}${e.target?`<span class="tag">TG: ${esc(e.target)}</span>`:''}</div>
      ${e.notes?`<div class="notes">${esc(e.notes)}</div>`:''}
      ${e.images&&e.images.length?`<div class="imgs">${e.images.map((img,i)=>`<div class="img-wrap"><img src="${img.url}" alt="${esc(img.original_name)}" data-entry="${e.id}" data-img-idx="${i}"><button class="img-del" data-img="${img.id}">✕</button></div>`).join('')}</div>`:''}
      <input class="upload-inline" type="file" accept="image/png,image/jpeg,image/webp,image/gif" multiple data-upload="${e.id}">
    </div>
    <div class="card-actions"><button data-edit="${e.id}">✎</button><button class="danger" data-del="${e.id}">✕</button></div>
  </article>`).join('');}
const _jSyncCh=typeof BroadcastChannel!=='undefined'?new BroadcastChannel('chart_popout_sync'):null;
function postSym(sym,type){const s=String(sym||'').toUpperCase().trim();if(s&&_jSyncCh)_jSyncCh.postMessage({type:'CHART_POPOUT_SYNC',symbol:s});if(window.parent)window.parent.postMessage({type:type,symbol:sym},'*');}
function openViewer(entryId,idx){const entry=S.entries.find(x=>String(x.id)===String(entryId));const imgs=(entry&&entry.images)||[];if(!imgs.length)return;S.viewerImages=imgs;S.viewerIdx=Math.max(0,Math.min(idx,imgs.length-1));viewerShow();$('viewer').classList.add('on');}
function viewerShow(){if(!S.viewerImages.length)return;$('viewer-img').src=S.viewerImages[S.viewerIdx].url;$('viewer-prev').style.display=S.viewerImages.length>1?'':'none';$('viewer-next').style.display=S.viewerImages.length>1?'':'none';}
function viewerNav(dir){if(!S.viewerImages.length)return;S.viewerIdx=(S.viewerIdx+dir+S.viewerImages.length)%S.viewerImages.length;viewerShow();}
function closeViewer(){$('viewer').classList.remove('on');S.viewerImages=[];S.viewerIdx=0;}
async function deleteJournalImage(imageId){await api('/api/journal/images/'+imageId,{method:'DELETE'});const keepId=S.editingId;await loadEntries();if(keepId){const fresh=S.entries.find(x=>String(x.id)===String(keepId));if(fresh)showForm(fresh);}}
function openLogin(){$('login-password').value='';$('login-password').type='password';$('login-modal').classList.add('on');setTimeout(()=>$('login-password').focus(),50);}
function closeLogin(){$('login-modal').classList.remove('on');$('login-password').value='';}
$('btn-login').addEventListener('click',openLogin);
$('login-close').addEventListener('click',closeLogin);
$('login-cancel').addEventListener('click',closeLogin);
$('toggle-pass').addEventListener('click',()=>{$('login-password').type=$('login-password').type==='password'?'text':'password';});
$('login-modal').addEventListener('click',e=>{if(e.target.id==='login-modal')closeLogin();});
$('login-form').addEventListener('submit',async e=>{e.preventDefault();const password=$('login-password').value;if(!password)return;try{await api('/api/journal/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password})});closeLogin();setAdmin(true);await loadEntries();}catch(err){alert('Không đăng nhập được: '+err.message);}});
$('journal-close-inline').addEventListener('click',()=>{if(window.parent)window.parent.postMessage({type:'JOURNAL_CLOSE'},'*');});
$('btn-logout').addEventListener('click',async()=>{try{await api('/api/journal/logout',{method:'POST'});}catch(e){}setAdmin(false);});
$('btn-new').addEventListener('click',()=>showForm());
$('btn-cancel').addEventListener('click',hideForm);
$('entry-form').addEventListener('submit',async e=>{e.preventDefault();try{const id=$('entry-id').value;const body=JSON.stringify(payload());let entryId=id;if(id)await api('/api/journal/entries/'+id,{method:'PUT',headers:{'Content-Type':'application/json'},body});else{const j=await api('/api/journal/entries',{method:'POST',headers:{'Content-Type':'application/json'},body});entryId=j.id;}await uploadImages(entryId,$('images').files);hideForm();await loadEntries();}catch(err){alert('Không lưu được: '+err.message);}});
$('warning-form').addEventListener('submit',async e=>{e.preventDefault();try{S.warning=$('warning-input').value.trim();S.warningTone=(document.querySelector('input[name="warning-tone"]:checked')||{}).value||'normal';await api('/api/journal/warning',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:S.warning,tone:S.warningTone})});renderWarning();}catch(err){alert('Không lưu được cảnh báo: '+err.message);}});
$('warning-clear').addEventListener('click',()=>{$('warning-input').value='';S.warning='';renderWarning();});
document.querySelectorAll('input[name="warning-tone"]').forEach(r=>r.addEventListener('change',e=>{S.warning=$('warning-input').value;S.warningTone=e.target.value;renderWarning();}));
$('uploaded-list').addEventListener('click',async e=>{const img=e.target.closest('[data-form-img-idx]');if(img&&S.editingId){openViewer(S.editingId,Number(img.dataset.formImgIdx)||0);return;}const del=e.target.closest('[data-form-img-del]');if(!del)return;if(confirm('Xóa ảnh này?')){try{await deleteJournalImage(del.dataset.formImgDel);}catch(err){alert('Không xóa ảnh được: '+err.message);}}});
$('list').addEventListener('click',async e=>{const imgDel=e.target.closest('[data-img]');if(imgDel){if(confirm('Xóa ảnh này?')){try{await deleteJournalImage(imgDel.dataset.img);}catch(err){alert('Không xóa ảnh được: '+err.message);}}return;}const symBtn=e.target.closest('[data-journal-sym]');if(symBtn){const sym=symBtn.dataset.journalSym;if(S.symTimer)clearTimeout(S.symTimer);S.symTimer=setTimeout(()=>postSym(sym,'JOURNAL_SYM_CLICK'),220);return;}const img=e.target.closest('img[data-entry]');if(img){openViewer(img.dataset.entry,Number(img.dataset.imgIdx)||0);return;}const edit=e.target.closest('[data-edit]');if(edit){const found=S.entries.find(x=>String(x.id)===String(edit.dataset.edit));if(found)showForm(found);return;}const del=e.target.closest('[data-del]');if(del&&confirm('Xóa Note này?')){try{await api('/api/journal/entries/'+del.dataset.del,{method:'DELETE'});if(String(S.editingId||'')===String(del.dataset.del))hideForm();await loadEntries();}catch(err){alert('Không xóa được: '+err.message);}}});
$('list').addEventListener('dblclick',e=>{const symBtn=e.target.closest('[data-journal-sym]');if(!symBtn)return;if(S.symTimer)clearTimeout(S.symTimer);postSym(symBtn.dataset.journalSym,'JOURNAL_SYM_DBLCLICK');});
$('list').addEventListener('change',async e=>{const up=e.target.closest('[data-upload]');if(!up||!S.admin)return;try{await uploadImages(up.dataset.upload,up.files);up.value='';await loadEntries();}catch(err){alert('Không upload được: '+err.message);}});
$('f-symbol').addEventListener('input',()=>{clearTimeout(window._flt);window._flt=setTimeout(loadEntries,250);});
$('f-status').addEventListener('change',loadEntries);
$('viewer').addEventListener('click',e=>{if(e.target.id==='viewer'||e.target.id==='viewer-close')closeViewer();});
$('viewer-prev').addEventListener('click',e=>{e.stopPropagation();viewerNav(-1);});
$('viewer-next').addEventListener('click',e=>{e.stopPropagation();viewerNav(1);});
document.addEventListener('keydown',e=>{if(e.key==='Escape'){if($('viewer').classList.contains('on'))return closeViewer();if($('login-modal').classList.contains('on'))return closeLogin();document.activeElement?.blur();return window.parent?.postMessage({type:'JOURNAL_CLOSE'},'*');}if(!$('viewer').classList.contains('on'))return;if(e.key==='ArrowLeft')viewerNav(-1);else if(e.key==='ArrowRight')viewerNav(1);});
(async function init(){await loadMe();await Promise.all([loadEntries(),loadWarning()]);})();
</script>
</body>
</html>
"""

# ── DASHBOARD HTML ───────────────────────────────────────────────────────────
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
<title>Scanner Dashboard</title>
<!-- Icon/favicon: iOS đọc apple-touch-icon riêng, Android đọc manifest.json. Cả 3 file + manifest
     nằm trong /static, dùng route /static mặc định. _static_cache_headers đặt Cache-Control:
     no-cache cho các file này (khác static khác cache 7 ngày) để đổi icon trên VPS thấy ngay. -->
<link rel="icon" type="image/png" sizes="32x32" href="/static/favicon-32.png">
<link rel="icon" type="image/png" sizes="16x16" href="/static/favicon-16.png">
<link rel="apple-touch-icon" sizes="180x180" href="/static/apple-touch-icon.png">
<link rel="manifest" href="/static/manifest.json">
<meta name="theme-color" content="#9c27b0">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Scanner">
<!-- default: thanh trạng thái (giờ/wifi/pin) nằm TÁCH RIÊNG phía trên trang, không đè lên
     nội dung — tránh che mất tiêu đề header khi mở app từ icon màn hình chính. -->
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<!-- Gắn class chart-popout-mode sớm ngay <head> (trước dashboard-main.js) để CSS ẩn hết
     trừ panel CHART ngay từ đầu, khỏi nháy full dashboard rồi mới thu về chart.
     Đồng thời bắn luôn request /api/lightweight_chart (mã lấy từ ?sym= khi popout, hoặc
     localStorage LITE_LAST_SYMBOL_KEY khi dashboard chính) — chạy song song sớm nhất có thể,
     kết quả lưu vào window.__liteChartPrefetch để loadLiteChart() tái dùng thay vì gọi lại,
     cắt thời gian chờ mạng khỏi đường găng tải trang. Không đổi hành vi, chỉ đổi thời điểm gọi. -->
<script>
try{
  const _qp=new URLSearchParams(window.location.search);
  const _isPopout=_qp.get('chartPopout')==='1';
  if(_isPopout)document.documentElement.classList.add('chart-popout-mode');
  const _pfSym=(_isPopout?(_qp.get('sym')||''):(localStorage.getItem('dashboard_lite_last_symbol')||'VNINDEX')).trim().toUpperCase();
  if(_pfSym){
    window.__liteChartPrefetch={
      sym:_pfSym,tf:'1D',
      promise:fetch('/api/lightweight_chart/'+encodeURIComponent(_pfSym)+'?tf=1D&limit=450')
    };
  }
}catch(e){}
</script>
<!-- Preload thư viện chart ngay từ <head> để tải song song lúc parse HTML, thay vì đợi
     script cuối trang mới bắt đầu tải — giúp panel CHART vẽ sớm hơn. Self-host tại /app/static. -->
<link rel="preload" as="script" href="/static/lightweight-charts.min.js">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600;700&family=IBM+Plex+Sans:wght@400;500;600;700&family=Barlow+Condensed:wght@600;700;800&display=swap" rel="stylesheet">
<style>
/* ═══════════════════════════════════════════
   VARIABLES & RESET
   ═══════════════════════════════════════════ */
:root{
  --bg:#f4f6fb;--surface:#fff;--surf2:#f0f3f9;--border:#dde3ee;
  --accent:#1a56db;--green:#0e9f6e;--red:#e02424;
  --text:#111827;--muted:#6b7280;--shadow:rgba(0,0,0,.07);
  --font-mono:'IBM Plex Mono',monospace;--font-ui:'Barlow Condensed',sans-serif;
  --sab:env(safe-area-inset-bottom,0px);
}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:var(--font-mono);font-size:13px;min-height:100vh}

/* ═══════════════════════════════════════════
   HEADER — desktop
   ═══════════════════════════════════════════ */
header{
  display:flex;align-items:center;justify-content:space-between;
  padding:11px 22px;background:var(--surface);border-bottom:1px solid var(--border);
  position:static;z-index:100;box-shadow:0 1px 6px var(--shadow);
  flex-wrap:wrap;gap:6px;
}
header h1{
  font-family:var(--font-ui);font-size:19px;font-weight:800;
  letter-spacing:2.5px;color:var(--accent);text-transform:uppercase;
  white-space:nowrap;
}
.hdr-right{display:flex;gap:12px;align-items:center;flex-shrink:0}
.hdr-sound-btn{height:24px;min-width:26px;padding:0 6px;border-radius:6px;border:1px solid var(--border);background:var(--surface);color:var(--muted);font-size:12px;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;transition:all .15s;flex-shrink:0;user-select:none;outline:none}
.hdr-sound-btn:hover{background:var(--surf2);color:var(--text)}
.hdr-sound-btn.on{background:#eef3ff;border-color:var(--accent);color:var(--accent)}
#clock{color:var(--muted);font-size:11px;white-space:nowrap}
.dot-live{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 8px rgba(14,159,110,.5);animation:pulse 2s ease-in-out infinite;flex-shrink:0}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}

/* ═══════════════════════════════════════════
   LAYOUT
   ═══════════════════════════════════════════ */
.wrap{padding:16px 20px;display:flex;flex-direction:column;gap:16px}
.panel{background:var(--surface);border:1px solid var(--border);border-radius:8px;overflow:hidden;box-shadow:0 1px 4px var(--shadow)}
.panel-hdr{display:flex;align-items:center;justify-content:space-between;padding:9px 16px;background:var(--surf2);border-bottom:1px solid var(--border)}
.panel-hdr-left{display:flex;align-items:center;gap:8px}
.panel-title{font-family:var(--font-ui);font-size:12px;font-weight:800;text-transform:uppercase;letter-spacing:2px;color:var(--accent)}
.panel-meta{font-size:10px;color:var(--muted)}
.journal-star-btn{width:28px;height:28px;border-radius:50%;border:1px solid var(--border);background:var(--surface);color:#b45309;font-size:15px;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;transition:all .15s;flex-shrink:0}
.journal-star-btn:hover{background:#fef3c7;border-color:#f59e0b;color:#92400e;box-shadow:0 2px 8px rgba(180,83,9,.16)}
.journal-overlay{display:none;position:fixed;inset:0;z-index:9998;background:rgba(17,24,39,.52);backdrop-filter:blur(4px);align-items:center;justify-content:center;padding:18px}
.journal-overlay.on{display:flex}
.journal-box{width:min(1500px,98vw);height:92vh;background:var(--surface);border:1px solid var(--border);border-radius:10px;box-shadow:0 20px 60px rgba(0,0,0,.18);display:flex;flex-direction:column;overflow:hidden}
.journal-frame{width:100%;height:100%;border:none;display:block;flex:1}
.pbar-wrap{height:2px;overflow:hidden}
.pbar-fill{height:100%;width:0%;background:linear-gradient(90deg,var(--accent),var(--green));opacity:.5}
.panel-body{padding:12px 14px}
footer{text-align:center;padding:9px;color:var(--muted);font-size:10px;border-top:1px solid var(--border);background:var(--surface)}

/* ═══════════════════════════════════════════
   HEATMAP HEADER
   ═══════════════════════════════════════════ */
.hmap-panel-hdr{display:flex;align-items:center;gap:6px;padding:8px 16px;background:var(--surf2);border-bottom:1px solid var(--border)}
.hmap-hdr-row1{display:flex;align-items:center;gap:8px;flex-shrink:0}
.hmap-ts-wrap{margin-left:auto;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:10px;color:var(--muted)}
.hmap-link-btn{display:inline-flex;align-items:center;padding:4px 11px;border-radius:5px;border:1px solid var(--border);background:var(--surface);color:var(--muted);font-family:var(--font-mono);font-size:10px;font-weight:600;cursor:pointer;text-decoration:none;white-space:nowrap;transition:all .15s}
.hmap-link-btn:hover:not(.on){background:#eef3ff;color:var(--accent);border-color:var(--accent)}
.hmap-link-btn.on{background:var(--accent);color:#fff;border-color:var(--accent)}
.hmap-search-wrap{position:relative;display:flex;align-items:center}
.hmap-search-wrap .s-icon{position:absolute;left:11px;top:50%;transform:translateY(-50%);color:var(--muted);font-size:13px;pointer-events:none}
.hmap-search-input{width:100px;padding:5px 10px 5px 30px;border-radius:20px;border:1px solid var(--border);background:var(--surface);color:var(--text);font-family:var(--font-mono);font-size:11px;outline:none;transition:border-color .15s,width .2s}
.hmap-search-input:focus{border-color:var(--accent);box-shadow:0 0 0 2px rgba(26,86,219,.12);width:120px}
#hmap-follow-btn{color:var(--muted)}
#hmap-follow-btn.on{background:#fef3c7;color:#92400e;border-color:#f59e0b}

/* ═══════════════════════════════════════════
   SIGNALS
   ═══════════════════════════════════════════ */
.sig-list{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:3px}
.sig-row{display:grid;grid-template-columns:24px max-content max-content max-content max-content;align-items:center;justify-content:space-between;column-gap:8px;padding:7px 8px;border-radius:5px;border:1px solid var(--border);cursor:pointer;transition:background .15s,border-color .15s,box-shadow .15s;background:var(--surface)}
.sig-row:hover{background:#eef3ff;border-color:rgba(26,86,219,.3);box-shadow:0 2px 8px rgba(26,86,219,.07)}
.sig-row:hover .s-sym{color:var(--accent)}
.s-emoji{font-size:13px;height:20px;display:inline-flex;align-items:center;justify-content:center;line-height:1}
.s-sym{font-weight:700;font-size:13px;transition:color .15s;white-space:nowrap;display:inline-flex;align-items:center;line-height:1}
.s-type{font-size:11px;font-weight:600;text-align:center;white-space:nowrap;display:inline-flex;align-items:center;line-height:1}
.s-badge{display:inline-flex;align-items:center;justify-content:center;height:20px;width:62px;box-sizing:border-box;font-size:10px;font-weight:700;line-height:1;padding:0;border-radius:4px;text-align:center;letter-spacing:.4px;font-family:var(--font-ui);white-space:nowrap}
.s-badge-slot{display:flex;align-items:center;justify-content:flex-end}
.rs-badge{width:22px;height:22px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;font-family:var(--font-ui);font-size:10px;font-weight:800;letter-spacing:0;border:1px solid #cbd5e1;background:#f1f5f9;color:#475569;justify-self:center}
.rs-slot{width:22px;height:22px;display:block;justify-self:center}
.rs-90{background:#f3e8ff;color:#7e22ce;border-color:#d8b4fe}
.rs-80{background:#dcfce7;color:#15803d;border-color:#86efac}
.rs-50{background:#fef9c3;color:#854d0e;border-color:#fde047}
.rs-low{background:#fee2e2;color:#b91c1c;border-color:#fecaca}
.b-BREAKOUT{background:#dcfce7;color:#15803d;border:1px solid #86efac}
.b-POCKET{background:#fef9c3;color:#854d0e;border:1px solid #fde047}
.b-PREBREAK{background:#f3e8ff;color:#7e22ce;border:1px solid #d8b4fe}
.b-BBREAKP{background:#dbeafe;color:#1d4ed8;border:1px solid #93c5fd}
.b-BFISH{background:#ffedd5;color:#c2410c;border:1px solid #fdba74}
.b-MACROSS{background:#f1f5f9;color:#475569;border:1px solid #cbd5e1}
.signal-header-toggle{cursor:pointer;user-select:none}
.momentum-box{display:none;border-top:1px solid var(--border);background:#fbfcff;padding:8px 16px}
.momentum-box.on{display:block}
.momentum-section + .momentum-section{margin-top:8px}
.momentum-title{font-family:var(--font-ui);font-size:11px;font-weight:800;letter-spacing:1.8px;text-transform:uppercase;color:var(--accent);margin:0 0 6px}
.momentum-list{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:3px}
.strength-list{grid-template-columns:repeat(6,minmax(0,1fr))}
.momentum-row{display:grid;grid-template-columns:max-content max-content max-content max-content;align-items:center;justify-content:space-between;column-gap:8px;padding:6px 8px;border-radius:5px;border:1px solid var(--border);background:var(--surface);cursor:pointer;transition:background .15s,border-color .15s,box-shadow .15s}
.strength-row{grid-template-columns:max-content max-content max-content}
.momentum-row:hover{background:#eef3ff;border-color:rgba(26,86,219,.3);box-shadow:0 2px 8px rgba(26,86,219,.07)}
.momentum-row:hover .s-sym{color:var(--accent)}
.b-MACD_W{background:#e0f2fe;color:#0369a1;border:1px solid #7dd3fc}
.b-MACD_M{background:#eef2ff;color:#4338ca;border:1px solid #c7d2fe}
.b-RTM{background:#ecfdf5;color:#047857;border:1px solid #86efac}
.empty{text-align:center;padding:36px 20px;color:var(--muted);font-size:12px;grid-column:1/-1}
.empty .big{font-size:30px;margin-bottom:8px}

/* ═══════════════════════════════════════════
   HEATMAP GRID
   ═══════════════════════════════════════════ */
.hmap-outer{overflow-x:auto;padding-bottom:4px;text-align:center}
.hmap-outer::-webkit-scrollbar{height:4px}
.hmap-outer::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
.hmap-row{display:inline-flex;gap:4px;align-items:flex-start;min-width:max-content;padding:2px}
.hmap-col{position:relative;display:flex;flex-direction:column;gap:2px;width:162px;flex-shrink:0}
.hmap-group{display:flex;flex-direction:column;gap:2px}
/* Khối FOLLOW đè lên (overlay) phần cuối cột VN30 thay vì nối thêm xuống dưới —
   neo đáy, tự cao dần lên khi FOLLOW có thêm mã, không cộng thêm chiều cao vào
   layout tổng thể của hàng Heatmap (cột VN30 vẫn giữ nguyên chiều cao gốc). */
.hmap-follow-overlay{position:absolute;left:0;right:0;bottom:0;background:var(--surface);z-index:3}
.hmap-ghdr{display:flex;align-items:center;justify-content:center;padding:0 8px;height:24px;border-radius:4px;background:rgb(220,228,250);border:1px solid rgb(160,180,230);gap:16px}
.hmap-gname{font-family:var(--font-ui);font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:rgb(25,55,150);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hmap-gavg{font-family:var(--font-mono);font-size:9px;flex-shrink:0}
.hmap-gavg.pos{color:rgb(22,120,40)}.hmap-gavg.neg{color:rgb(185,25,25)}.hmap-gavg.zer{color:rgb(110,105,20)}
.hmap-cell{display:grid;grid-template-columns:56px 48px 1fr;align-items:center;height:24px;border-radius:4px;cursor:pointer;border:1px solid rgba(0,0,0,.1);transition:filter .12s,transform .1s,box-shadow .12s;overflow:hidden}
.hmap-cell:hover{filter:brightness(.96);transform:scale(1.035);z-index:2;box-shadow:0 2px 8px rgba(0,0,0,.18)}
.hmap-cell>span{display:flex;align-items:center;justify-content:center;height:100%;overflow:hidden;white-space:nowrap;font-family:var(--font-mono)}
@keyframes hmapFlashUp { 0%, 35%{box-shadow:inset 0 0 0 2.5px #16a34a, 0 0 10px rgba(22,163,74,.8);transform:scale(1.04)} 100%{box-shadow:inset 0 0 0 0 transparent;transform:scale(1)} }
@keyframes hmapFlashDown { 0%, 35%{box-shadow:inset 0 0 0 2.5px #dc2626, 0 0 10px rgba(220,38,38,.8);transform:scale(1.04)} 100%{box-shadow:inset 0 0 0 0 transparent;transform:scale(1)} }
.hmap-cell.flash-up{animation:hmapFlashUp 1.8s ease-out;z-index:3}
.hmap-cell.flash-down{animation:hmapFlashDown 1.8s ease-out;z-index:3}
.hc-sym{font-size:10px}.hc-price{font-size:8.5px;opacity:.82}.hc-pct{font-size:9.5px}
.hmap-sector-group{width:130px;margin:26px auto 0}
.hmap-sector-cell{display:grid;grid-template-columns:1fr auto;align-items:center;height:24px;border-radius:4px;border:1px solid rgba(0,0,0,.1);padding:0 8px;gap:2px;overflow:hidden;transition:filter .12s}
.hmap-sector-cell:hover{filter:brightness(.9)}
.hsc-name{font-family:var(--font-ui);font-size:9px;text-transform:uppercase;letter-spacing:.3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hsc-pct{font-family:var(--font-mono);font-size:9px;text-align:right;flex-shrink:0}
.treemap-wrap{width:calc(100% - 48px);aspect-ratio:16/9;height:auto;margin:0 24px;background:#fff}
.treemap-svg{width:100%;height:100%;display:block;background:#fff;border:none}
.treemap-empty{display:flex;align-items:center;justify-content:center;height:100%;color:var(--muted);font-size:13px}
#tri-content-treemap{position:relative}
.treemap-copy-btn{position:absolute;top:8px;right:2px;z-index:5;background:#fff;border:1px solid var(--border)}
.treemap-copy-btn:hover{background:#f1f5f9}
.tri-hdr{cursor:pointer;user-select:none;display:flex;align-items:center;justify-content:flex-start;gap:16px}
.tri-tabs{display:flex;align-items:center;gap:4px}
.tri-tab{font-family:var(--font-ui);font-size:13px;font-weight:600;padding:3px 9px;border-radius:5px;color:var(--muted);cursor:pointer;transition:all .15s;user-select:none}
.tri-tab:hover:not(.on){background:#eef3ff;color:var(--accent)}
.tri-tab.on{color:var(--accent);font-weight:800}
.tri-toggle{font-size:12px;color:var(--muted);transition:transform .15s;margin-left:auto}
.tri-panel:not(.collapsed) .tri-toggle{transform:rotate(90deg);color:var(--accent)}
/* Cơ chế thu/mở thẻ dùng chung cho tri-panel/hmap-panel/lite-chart-panel: .collapsed ẩn nội
   dung phụ header+thân, LUÔN dùng !important để @media mobile không ghi đè ngược lại (bug cũ:
   hmap-ts-wrap vẫn hiện trên mobile dù đã thu gọn, vì rule ẩn thiếu !important). */
.tri-panel.collapsed .tri-tabs,
.tri-panel.collapsed>.tri-body{display:none!important}
.tri-content{display:none}
.tri-content.on{display:block}
.health-svg{cursor:crosshair;display:block;width:100%;height:100%}
.health-vni-toggle{position:absolute;top:6px;left:88.4%;z-index:2;display:flex;align-items:center;gap:5px;font-size:11px;color:#334155;background:transparent;padding:0;border-radius:0;border:none;cursor:pointer;user-select:none}
.health-vni-toggle input{margin:0;cursor:pointer}
.health-vni-swatch{display:inline-block;width:12px;height:2px;background:#f97316;border-radius:1px}
.health-period-tabs{position:absolute;top:6px;left:8px;z-index:2;display:flex;gap:4px}
.health-period-tab{font-size:11px;color:#334155;background:#fff;border:1px solid var(--border);border-radius:4px;padding:1px 7px;cursor:pointer;user-select:none}
.health-period-tab.on{background:var(--accent);border-color:var(--accent);color:#fff}
.health-body{padding:12px 14px;background:#fff;height:720px;display:flex;align-items:center;overflow:auto}
#tri-content-health{position:relative}
.health-copy-btn{position:absolute;top:8px;right:2px;z-index:5;background:#fff;border:1px solid var(--border)}
.health-copy-btn:hover{background:#f1f5f9}
.chart-copy-btn{position:absolute;top:8px;right:2px;z-index:5;background:#fff;border:1px solid var(--border)}
.chart-copy-btn:hover{background:#f1f5f9}
.health-layout{width:100%;display:grid;grid-template-columns:minmax(520px,1.45fr) minmax(320px,.85fr);gap:14px;align-items:stretch}
.health-chartbox{min-height:328px;border:1px solid var(--border);border-radius:8px;background:#fff;overflow:hidden;position:relative}
.health-side{display:grid;grid-template-rows:auto 1fr;gap:12px;min-width:0}
.health-score-card{border:1px solid var(--border);border-radius:8px;padding:16px;background:#fbfcff}
.health-score-top{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}
.health-score{font-family:var(--font-ui);font-size:56px;line-height:.9;font-weight:800;color:var(--accent);letter-spacing:0}
.health-label{font-family:var(--font-ui);font-size:20px;font-weight:800;text-transform:uppercase;letter-spacing:1.2px;color:var(--accent)}
.health-meta{margin-top:6px;font-size:13px;color:var(--muted)}
.health-tags{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.health-tag{font-family:var(--font-ui);font-size:12px;font-weight:800;letter-spacing:.7px;text-transform:uppercase;border-radius:4px;border:1px solid #cbd5e1;padding:4px 8px;color:#334155;background:#f8fafc}
.health-analysis{border:1px solid var(--border);border-radius:8px;padding:18px 20px;background:#fff;min-height:120px;display:flex;flex-direction:column;justify-content:center}
.health-analysis-title{font-family:var(--font-ui);font-size:15px;font-weight:800;letter-spacing:1.6px;text-transform:uppercase;color:var(--accent);margin-bottom:12px}
.health-analysis p{font-family:'IBM Plex Sans',sans-serif;font-size:15px;line-height:1.65;margin:0 0 12px;color:#1f2937}
.health-analysis ul{margin:0 0 12px 20px;color:#374151;font-size:14.5px;line-height:1.65;font-family:'IBM Plex Sans',sans-serif}
.health-analysis li{margin-bottom:5px}
.health-empty{display:flex;align-items:center;justify-content:center;height:100%;color:var(--muted);font-size:12px;text-align:center;padding:24px}
.vnd-panel{margin:14px 14px 0;border:1px solid var(--border);border-radius:8px;padding:14px 16px 12px;background:#fff}
.vnd-panel:last-child{margin-bottom:14px}
.vnd-panel-hdr{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:6px}
.vnd-panel-title{font-family:var(--font-ui);font-size:14px;font-weight:800;text-transform:uppercase;letter-spacing:1.4px;color:var(--accent)}
.vnd-status{font-size:11px;color:var(--muted);text-align:right;white-space:nowrap}
.vnd-controls{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin:6px 0}
.vnd-tabs{display:inline-flex;gap:6px;position:relative;top:8px}
.vnd-tab{height:26px;padding:0 12px;display:inline-flex;align-items:center;border:1px solid var(--border);border-radius:6px;background:#fff;color:var(--muted);font-size:12px;font-weight:700;cursor:pointer;transition:all .15s;user-select:none}
.vnd-tab:hover:not(.on){background:#eef3ff;color:var(--accent)}
.vnd-tab.on{background:var(--accent);border-color:var(--accent);color:#fff}
.vnd-period{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--muted);font-weight:700;white-space:nowrap}
.vnd-period select{height:26px;width:auto;border:1px solid var(--border);border-radius:6px;background:#fff;color:var(--text);padding:0 6px;font-size:12px}
.vnd-chart-area{position:relative;height:270px}
.vnd-svg{width:100%;height:100%;display:block;overflow:visible}
.vnd-grid-line{stroke:var(--border);stroke-width:1;stroke-dasharray:4 5;stroke-opacity:.35}
.vnd-axis-label{fill:var(--muted);font-size:11px;font-weight:700}
.vnd-x-label{fill:var(--muted);font-size:10px;font-weight:600}
.vnd-legend{display:flex;justify-content:center;align-items:center;gap:18px;margin-top:2px;color:var(--muted);font-size:11px;flex-wrap:wrap}
.vnd-legend-item{display:inline-flex;align-items:center;gap:5px}
.vnd-swatch{display:inline-block;width:12px;height:3px;border-radius:2px}
.vnd-summary{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin-top:10px}
.vnd-tile{border:1px solid var(--border);border-radius:7px;padding:8px 10px;background:var(--surf2);min-height:52px}
.vnd-tile span{display:block;color:var(--muted);font-size:11px;font-weight:700;margin-bottom:4px}
.vnd-tile strong{display:block;font-size:16px;line-height:20px;color:var(--text)}
.vnd-error{display:none;margin-top:8px;border:1px solid #efc5c5;background:#fff5f5;color:#9b2424;border-radius:6px;padding:8px 10px;font-size:12px}
.vnd-tooltip{position:fixed;z-index:50;display:none;min-width:150px;padding:8px 10px;background:rgba(17,24,39,.94);color:#fff;border-radius:6px;font-size:11px;pointer-events:none;box-shadow:0 8px 24px rgba(0,0,0,.18)}
.vnd-tooltip strong{display:block;margin-bottom:4px}
.vnd-bar-positive{fill:var(--green)}
.vnd-bar-negative{fill:var(--red)}
.lite-chart-panel .panel-hdr{cursor:pointer;user-select:none}
.lite-chart-toggle-icon{font-size:12px;color:var(--muted);transition:transform .15s;flex-shrink:0}
.lite-chart-panel:not(.collapsed) .lite-chart-toggle-icon{transform:rotate(90deg);color:var(--accent)}
.lite-chart-panel.collapsed .lite-chart-toolbar>*:not(.panel-title){display:none!important}
.lite-chart-panel.collapsed .lite-chart-frame{display:none!important}
/* Cửa sổ CHART popout: ẩn hết dashboard, chỉ hiện panel CHART. Dùng html.chart-popout-mode
   (gắn ở <head>, không phải body) để có hiệu lực ngay lần vẽ đầu, không đợi JS cuối trang. */
html.chart-popout-mode>body>header,
html.chart-popout-mode #main-wrap>*:not(#lite-chart-panel){display:none!important}
html.chart-popout-mode #main-wrap{padding:8px}
html.chart-popout-mode #lite-chart-popout-btn{display:none}
/* HTML gốc panel CHART luôn có sẵn class .collapsed (JS mới gỡ ra để mở) — vô hiệu hoá 2 rule
   ẩn của .collapsed ngay khi html.chart-popout-mode có mặt để panel luôn hiện mở sẵn, khỏi nháy
   thu/mở khi JS/mạng chậm. Dùng display:flex (không dùng revert) vì các con trực tiếp của
   .lite-chart-toolbar đều tự cần display:flex cho layout riêng. */
html.chart-popout-mode .lite-chart-panel.collapsed .lite-chart-toolbar>*:not(.panel-title){display:flex!important}
html.chart-popout-mode .lite-chart-panel.collapsed .lite-chart-frame{display:block!important}
/* Popout mọi kích thước: cho #lite-chart-panel cao bằng viewport còn lại, .lite-chart-frame
   tự giãn lấp phần sau toolbar bằng flexbox (thay vì height:720px cố định) — applyLitePaneLayout()
   đọc clientHeight thực tế để chia main/RSI/MACD. Rule mobile portrait bên dưới sẽ ghi đè. */
html.chart-popout-mode #lite-chart-panel{
  display:flex;
  flex-direction:column;
  height:calc(100vh - 16px);
}
html.chart-popout-mode .lite-chart-frame{
  flex:1 1 auto;
  height:auto!important;
  max-height:none!important;
  min-height:0!important;
}
/* Mobile portrait + popout: cùng cơ chế trên nhưng trừ thêm safe-area đáy iPhone thay vì đoán
   cứng chiều cao toolbar — tránh cắt panel MACD hoặc che góc bo dưới. */
@media screen and (max-width:768px) and (orientation:portrait){
  html.chart-popout-mode #main-wrap{padding:8px 8px var(--sab) 8px}
  html.chart-popout-mode #lite-chart-panel{
    height:calc(100dvh - 8px - var(--sab));
  }
  /* Thu nhỏ ô Tìm mã bằng scale (font-size giữ 16px để iOS không auto-zoom khi focus);
     áp dụng đồng bộ cho cả ô Tìm mã HEATMAP và CHART để 2 ô cùng kích thước trên mobile. */
  .hmap-search-wrap,
  .lite-chart-search-wrap{
    transform:scale(0.72);
    transform-origin:left center;
    margin-right:calc((0.72 - 1) * 90px);
  }
  .mob-search-wrap{
    transform:scale(0.72);
    transform-origin:right center;
    margin-left:calc((0.72 - 1) * 72px);
  }
  /* Title chart portrait: ẩn O, H, L để vừa 1 dòng (chỉ còn C và %). */
  .lite-chart-title .lct-open,.lite-chart-title .lct-hl{display:none}
  /* Portrait mobile: lite-chart-frame tự giãn theo nội dung khi thêm RSI/MACD panel. */
  .lite-chart-frame{
    height:auto!important;
    max-height:none!important;
    min-height:300px!important;
  }
}
.hmap-panel-hdr{cursor:pointer;user-select:none}
.hmap-toggle-icon{font-size:12px;color:var(--muted);transition:transform .15s;flex-shrink:0}
.hmap-panel:not(.collapsed) .hmap-toggle-icon{transform:rotate(90deg);color:var(--accent)}
/* Đồng bộ với .tri-panel.collapsed / .lite-chart-panel.collapsed ở trên & dưới: mọi rule ẩn khi
   thu gọn đều dùng !important để không bị @media max-width:768px ghi đè (xem giải thích ở khối
   .tri-panel.collapsed). Thu gọn hmap-panel chỉ còn lại đúng "Heatmap" + icon mũi tên. */
.hmap-panel.collapsed .hmap-hdr-row1>*:not(.panel-title){display:none!important}
.hmap-panel.collapsed .hmap-ts-wrap{display:none!important}
.hmap-panel.collapsed .hmap-toggle-icon{margin-left:auto}
.hmap-panel.collapsed>.panel-body{display:none!important}
.market-frame{width:100%;height:720px;border:none;display:block;background:#fff}
.frame-shrink{width:100%;height:720px;overflow:hidden;position:relative;background:#fff}
.frame-shrink iframe{position:absolute;top:0;left:0;width:125%;height:125%;border:none;background:#fff;transform:scale(.8);transform-origin:0 0}
.lite-chart-frame{width:100%;height:720px;background:#fff;position:relative}
.lite-chart-frame:focus,.lite-chart-frame:focus-visible{outline:none}
#lite-chart{width:100%;height:540px}
#lite-rsi-chart{width:100%;height:176px;border-top:1px solid var(--border);display:none}
#lite-macd-chart{width:100%;height:176px;border-top:1px solid var(--border);display:none}
#lite-chart.hide-tv-logo a[href*="tradingview"],#lite-chart.hide-tv-logo [class*="logo"],#lite-chart.hide-tv-logo [class*="attribution"],
#lite-rsi-chart.hide-tv-logo a[href*="tradingview"],#lite-rsi-chart.hide-tv-logo [class*="logo"],#lite-rsi-chart.hide-tv-logo [class*="attribution"],
#lite-macd-chart.hide-tv-logo a[href*="tradingview"],#lite-macd-chart.hide-tv-logo [class*="logo"],#lite-macd-chart.hide-tv-logo [class*="attribution"]{display:none!important}
.lite-macd-resizer{height:4px;background:transparent;cursor:ns-resize;display:none;position:relative;z-index:4}
.lite-macd-resizer.on{display:block}
.lite-macd-resizer:hover{background:rgba(26,86,219,.12)}
.lite-chart-toolbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.lite-chart-input{width:100px;padding:5px 10px 5px 30px;border-radius:20px;border:1px solid var(--border);background:var(--surface);color:var(--text);font-family:var(--font-mono);font-size:11px;text-transform:uppercase;outline:none;transition:border-color .15s,width .2s}
.lite-chart-input::placeholder{color:var(--muted);text-transform:none}
.lite-chart-input:focus{border-color:var(--accent);box-shadow:0 0 0 2px rgba(26,86,219,.12);width:120px}
.lite-chart-search-wrap{position:relative;display:flex;align-items:center}
.lite-chart-search-wrap .s-icon{position:absolute;left:11px;top:50%;transform:translateY(-50%);color:var(--muted);font-size:13px;pointer-events:none}
.lite-tf-tabs{display:flex;align-items:center;gap:3px}
.lite-tf-btn{height:24px;min-width:28px;border:1px solid var(--border);border-radius:6px;background:#f8fafc;color:var(--muted);font-family:var(--font-mono);font-size:10px;font-weight:700;cursor:pointer}
.lite-tf-btn.on{background:#eef3ff;border-color:var(--accent);color:var(--accent)}
.lite-indicators{display:flex;align-items:center;gap:7px;flex-wrap:wrap}
.lite-indicators label{display:flex;align-items:center;gap:3px;font-family:var(--font-mono);font-size:10px;color:var(--muted);cursor:pointer;position:relative}
.lite-ind-label{cursor:pointer}
.lite-ind-label:hover{color:var(--accent)}
.lite-ind-color{position:absolute;width:1px;height:1px;opacity:0;pointer-events:none;left:0;top:0}
.lite-indicators input{width:12px;height:12px;margin:0}
.lite-ind-group{position:relative;display:flex;align-items:center;gap:4px}
.lite-ind-color-visible{position:static!important;width:14px!important;height:14px!important;opacity:1!important;pointer-events:auto!important;border:1px solid var(--border);border-radius:3px;padding:0;cursor:pointer}
.lite-ind-dropdown-sub-title{font-size:9px;font-weight:700;color:var(--muted);margin:4px 0 1px;letter-spacing:.03em}
.lite-ind-group-btn{display:flex;align-items:center;gap:3px;height:24px;padding:0 8px;border:1px solid var(--border);border-radius:6px;background:#f8fafc;color:var(--muted);font-family:var(--font-mono);font-size:10px;font-weight:700;cursor:pointer}
.lite-ind-group-btn:hover{color:var(--accent);border-color:var(--accent)}
.lite-ind-group.open .lite-ind-group-btn{background:#eef3ff;border-color:var(--accent);color:var(--accent)}
.lite-ind-caret{font-size:8px;transition:transform .15s}
.lite-ind-group.open .lite-ind-caret{transform:rotate(180deg)}
.lite-ind-count{display:none;min-width:13px;height:13px;padding:0 3px;border-radius:7px;background:var(--accent);color:#fff;font-size:8px;font-weight:700;align-items:center;justify-content:center;line-height:13px}
.lite-ind-count.on{display:inline-flex}
.lite-ind-dropdown{display:none;position:absolute;top:calc(100% + 4px);left:0;z-index:20;flex-direction:column;gap:5px;background:#fff;border:1px solid var(--border);border-radius:8px;padding:8px 10px;box-shadow:0 8px 24px rgba(17,24,39,.12);min-width:100px}
.lite-ind-group.open .lite-ind-dropdown{display:flex}
.lite-ind-dropdown label{font-size:10px}
.lite-ind-simple{display:flex}
#lite-vietstock-toggle-btn.on{background:#eef3ff;color:var(--accent);border-color:var(--accent)}
#lite-vietstock-toggle-btn{font-size:10px;font-weight:700;color:var(--muted);background:#f8fafc;border:1px solid var(--border);min-width:28px}
#lite-vietstock-toggle-btn:hover:not(.on){background:#f8fafc}
.lite-vietstock-iframe{display:none;position:absolute;inset:0;width:100%;height:100%;border:none;background:#fff;z-index:6}
.lite-chart-frame.vietstock-mode .lite-vietstock-iframe{display:block}
.lite-groups-sidebar.on~.lite-vietstock-iframe{left:180px;width:calc(100% - 180px)}
.lite-chart-title{position:absolute;top:8px;left:10px;z-index:3;font-family:var(--font-mono);font-size:11px;color:#111827;white-space:nowrap;background:rgba(255,255,255,.78);padding:2px 5px;border-radius:4px;pointer-events:none;transition:left .15s}
.lite-chart-signal{position:absolute;top:29px;left:10px;z-index:3;display:none;align-items:center;gap:6px;line-height:1;background:rgba(255,255,255,.78);padding:4px 8px;border-radius:6px;pointer-events:none;transition:left .15s}
.lite-chart-signal.on{display:flex}
/* Khi sidebar nhóm ngành mở, dịch title/tín hiệu sang phải để không bị cột che mất */
.lite-groups-sidebar.on+.lite-chart-title,
.lite-groups-sidebar.on~.lite-chart-signal{left:192px}
/* Giá phóng to — đặt sát cạnh trên, canh giữa theo chiều ngang khung chart (kiểu "Magnified
   Market Price" của AmiBroker): dòng 1 là giá lớn, dòng 2 là biến động/khối lượng nhỏ hơn. */
.lite-chart-bigprice{position:absolute;top:6px;left:0;right:0;margin:0 auto;width:max-content;z-index:3;display:none;flex-direction:column;align-items:center;gap:1px;pointer-events:none;transition:left .15s;white-space:nowrap}
.lite-chart-bigprice.on{display:flex}
.lite-chart-bigprice .bp-price{font-family:var(--font-mono);font-size:20px;font-weight:700;line-height:1.1}
.lite-chart-bigprice .bp-sub{font-family:var(--font-mono);font-size:11px;line-height:1.2}
/* Sidebar nhóm ngành mở: đẩy mốc left sang 180px (bề rộng sidebar), margin:auto tự canh giữa
   trong phần khung chart còn hiển thị — khác title/tín hiệu (neo left tuyệt đối, không canh giữa). */
.lite-groups-sidebar.on~.lite-chart-bigprice{left:180px}
.lite-chart-search{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);z-index:5;width:42px;min-width:42px;max-width:120px;height:34px;border:1px solid var(--accent);border-radius:8px;background:#fff;color:var(--text);font-family:var(--font-mono);font-size:16px;font-weight:800;text-align:center;text-transform:uppercase;box-shadow:0 8px 28px rgba(17,24,39,.15);outline:none;display:none;transition:width .12s}
.lite-chart-search.on{display:block}
.lite-chart-empty{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;background:#fff;color:var(--muted);font-size:12px;pointer-events:none}
/* Sidebar nhóm ngành / mã (overlay bên trái CHART) — không đụng layout/resize chart */
.lite-groups-sidebar{position:absolute;top:0;left:0;bottom:0;width:180px;background:rgba(255,255,255,.98);border-right:1px solid var(--border);z-index:9;display:none;flex-direction:column;box-shadow:2px 0 14px rgba(17,24,39,.12)}
.lite-groups-sidebar.on{display:flex}
.lg-toolbar{display:flex;align-items:center;padding:6px 8px;border-bottom:1px solid var(--border);background:#f8fafc;flex-shrink:0}
.lg-toolbar-title{font-family:var(--font-mono);font-size:10px;font-weight:800;letter-spacing:.06em;color:var(--muted)}
.lg-sort-btn{height:22px;padding:0 8px;border:1px solid var(--border);border-radius:6px;background:#fff;color:var(--muted);font-family:var(--font-mono);font-size:10px;font-weight:700;cursor:pointer}
.lg-sort-btn:hover{color:var(--accent);border-color:var(--accent)}
.lg-ghdr .lg-sort-btn{height:18px;padding:0 6px;font-size:9.5px}
.lite-groups-list{flex:1;overflow-y:auto;scrollbar-width:thin}
.lg-group{border-bottom:1px solid var(--border)}
.lg-ghdr{position:sticky;top:0;z-index:2;display:flex;align-items:center;justify-content:space-between;padding:7px 10px;cursor:pointer;font-family:var(--font-mono);font-size:11px;font-weight:700;color:var(--text);user-select:none;background:#f8fafc}
.lg-ghdr:hover{color:var(--accent)}
.lg-ghdr.dragging{opacity:.35}
.lg-ghdr.drag-over{box-shadow:inset 0 2px 0 var(--accent)}
.lg-ghdr-right{display:flex;align-items:center;gap:6px;flex-shrink:0}
.lg-add-btn{width:16px;height:16px;line-height:14px;text-align:center;padding:0;border:1px solid var(--border);border-radius:4px;background:#fff;color:var(--accent);font-size:12px;font-weight:800;cursor:pointer}
.lg-add-btn:hover{background:#eef3ff;border-color:var(--accent)}
.lg-caret{font-size:9px;color:var(--muted);transition:transform .15s;flex-shrink:0}
.lg-group.open .lg-caret{transform:rotate(90deg)}
.lg-symlist{display:flex;flex-direction:column}
.lg-sym-item{display:flex;align-items:center;gap:4px;padding:5px 6px 5px 10px;font-family:var(--font-mono);font-size:10.5px;cursor:pointer;border-top:1px solid #f1f5f9}
body:not(.key-nav) .lg-sym-item:hover,
.lg-sym-item.on{background:#eef3ff}
.lg-sym-item.dragging{opacity:.35}
.lg-sym-item.drag-over{box-shadow:inset 0 2px 0 var(--accent)}
.lg-star{flex-shrink:0;width:14px;text-align:center;cursor:pointer;color:#d1d5db;font-size:13px;line-height:1}
.lg-star.on{color:#f59e0b}
/* Nút ⭐ FAVORITE trên toolbar chart (cạnh ô Tìm mã) — dùng chung style .lite-draw-btn, chỉ đổi màu khi mã đang xem đã có trong Favorite */
.lite-fav-btn{color:#d1d5db}
.lite-fav-btn.on{color:#f59e0b}
.lg-sym-name{width:36px;flex-shrink:0;font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.lg-sym-pct{width:40px;flex-shrink:0;text-align:right;border-radius:4px;padding:1px 2px;transition:background .2s}
@keyframes pctFlashUp { 0%, 35%{background:rgba(34,197,94,.45);box-shadow:0 0 6px rgba(34,197,94,.6)} 100%{background:transparent;box-shadow:none} }
@keyframes pctFlashDown { 0%, 35%{background:rgba(239,68,68,.45);box-shadow:0 0 6px rgba(239,68,68,.6)} 100%{background:transparent;box-shadow:none} }
.lg-sym-pct.flash-up{animation:pctFlashUp 1.8s ease-out}
.lg-sym-pct.flash-down{animation:pctFlashDown 1.8s ease-out}
.lg-sym-price{width:48px;flex-shrink:0;text-align:right;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.lg-empty-hint{padding:14px 10px;font-family:var(--font-mono);font-size:10.5px;color:var(--muted);text-align:center}
/* Khu vực FOLLOW bên trong FAVORITE: nền xanh mờ nhạt hơn màu nền header, để phân khai rõ với khu vực Favorite thường */
.lg-sym-item.lg-follow{background:#f5f8fd}
body:not(.key-nav) .lg-sym-item.lg-follow:hover,
.lg-sym-item.lg-follow.on{background:#eaf1fd}
.lg-sym-item.lg-follow+.lg-sym-item:not(.lg-follow){border-top:1px solid var(--border)}
.lite-rect-tooltip{position:absolute;z-index:8;display:none;padding:3px 8px;border-radius:5px;font-family:var(--font-mono);font-size:11px;font-weight:700;background:rgba(255,255,255,.94);border:1px solid var(--border);box-shadow:0 2px 8px rgba(17,24,39,.16);pointer-events:none;white-space:nowrap;transform:translate(8px,-100%)}
.lite-xhair-v{position:absolute;top:0;bottom:0;left:0;width:0;border-left:1px dashed rgba(55,65,81,.55);pointer-events:none;z-index:4;display:none}
.lite-xhair-h{position:absolute;left:0;right:0;top:0;height:0;border-top:1px dashed rgba(55,65,81,.55);pointer-events:none;z-index:4;display:none}
.lite-xhair-price{position:absolute;right:1px;top:0;transform:translateY(-50%);min-width:54px;padding:2px 6px;font-family:var(--font-mono);font-size:11px;font-weight:600;color:#fff;background:#1f2937;border-radius:3px;pointer-events:none;z-index:5;display:none;text-align:center;white-space:nowrap}
.lite-xhair-time{position:absolute;left:0;bottom:2px;transform:translateX(-50%);padding:2px 6px;font-family:var(--font-mono);font-size:11px;font-weight:600;color:#fff;background:#1f2937;border-radius:3px;pointer-events:none;z-index:5;display:none;white-space:nowrap}
.lite-draw-toolbar{display:flex;align-items:center;gap:3px;flex-wrap:wrap;padding-left:6px;border-left:1px solid var(--border)}
.lite-draw-btn{width:24px;height:24px;border:1px solid transparent;border-radius:6px;background:transparent;color:#374151;font-size:12px;line-height:1;cursor:pointer;display:flex;align-items:center;justify-content:center}
.lite-draw-btn:hover{background:#f1f5f9}
.lite-draw-btn.on{background:#eef3ff;border-color:var(--accent);color:var(--accent)}
.lite-alert-wrap{position:relative;display:flex}
.lite-alert-badge{position:absolute;top:-5px;right:-5px;min-width:14px;height:14px;padding:0 3px;border-radius:8px;background:#dc2626;color:#fff;font-size:8px;font-weight:800;line-height:14px;text-align:center;display:none}
.lite-alert-badge.on{display:block}
.lite-alert-panel{display:none;position:absolute;top:calc(100% + 7px);right:0;z-index:40;width:360px;max-width:calc(100vw - 28px);background:#fff;border:1px solid var(--border);border-radius:8px;box-shadow:0 14px 34px rgba(17,24,39,.18);padding:10px;color:var(--text)}
.lite-alert-panel.on{display:block}
.lite-alert-title{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:8px;font-size:11px;font-weight:800;color:var(--accent);letter-spacing:.08em;text-transform:uppercase}
.lite-alert-grid{display:grid;grid-template-columns:1fr 1fr;gap:7px}
.lite-alert-field{display:flex;flex-direction:column;gap:3px}
.lite-alert-field.full{grid-column:1/-1}
.lite-alert-field label,.lite-alert-check{font-size:10px;color:var(--muted);font-family:var(--font-mono)}
.lite-alert-field select,.lite-alert-field input{height:28px;border:1px solid var(--border);border-radius:6px;background:#fff;color:var(--text);font-size:11px;padding:0 7px;outline:none}
.lite-alert-field input{text-transform:uppercase}
.lite-alert-field select:focus,.lite-alert-field input:focus{border-color:var(--accent);box-shadow:0 0 0 2px rgba(26,86,219,.1)}
.lite-alert-checks{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.lite-alert-check{display:flex;align-items:center;gap:4px;cursor:pointer}
.lite-alert-actions{display:flex;gap:6px;margin-top:9px;align-items:center;justify-content:flex-end}
.lite-alert-action{height:28px;padding:0 10px;border:1px solid var(--border);border-radius:6px;background:#f8fafc;color:#374151;font-size:11px;font-weight:700;cursor:pointer}
.lite-alert-action.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.lite-alert-action:hover{filter:brightness(.97)}
.lite-alert-list{border-top:1px solid var(--border);margin-top:10px;padding-top:8px;max-height:210px;overflow:auto;display:flex;flex-direction:column;gap:6px}
.lite-alert-row{display:grid;grid-template-columns:1fr auto;gap:6px;align-items:center;border:1px solid var(--border);border-radius:6px;padding:7px;background:#fbfcff}
.lite-alert-row.off{opacity:.62}
.lite-alert-row-main{min-width:0}
.lite-alert-row-title{font-size:11px;font-weight:800;color:#111827;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.lite-alert-row-sub{font-size:10px;color:var(--muted);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.lite-alert-row-actions{display:flex;gap:4px}
.lite-alert-mini{height:23px;min-width:28px;border:1px solid var(--border);border-radius:5px;background:#fff;color:#374151;font-size:10px;font-weight:700;cursor:pointer}
.lite-alert-mini.danger{color:#dc2626}
.lite-alert-mini.on{background:#eef3ff;border-color:var(--accent);color:var(--accent)}
.alert-toast-wrap{position:fixed;top:8px;right:18px;z-index:9999;display:flex;flex-direction:column;gap:8px;max-width:min(360px,calc(100vw - 28px))}
.alert-toast{background:#fff;color:#111827;border:1px solid rgba(17,24,39,.12);border-radius:8px;box-shadow:0 12px 32px rgba(17,24,39,.18);padding:10px 12px;cursor:pointer}
.alert-toast-title{font-size:12px;font-weight:800;margin-bottom:3px;color:#111827}
.alert-toast-sub{font-size:11px;color:#4b5563}
.lite-draw-sep{width:1px;height:16px;background:var(--border);margin:0 2px}
.lite-draw-color{width:24px;height:20px;padding:0;border:1px solid var(--border);border-radius:6px;background:none;cursor:pointer}
.lite-draw-color::-webkit-color-swatch-wrapper{padding:2px}
.lite-draw-color::-webkit-color-swatch{border:none;border-radius:4px}
.lite-draw-canvas{position:absolute;top:0;left:0;z-index:2;pointer-events:none}
.lite-draw-canvas.drawing{pointer-events:auto;cursor:crosshair}
.lite-shape-bar{position:absolute;z-index:7;display:none;align-items:center;gap:4px;background:#fff;border:1px solid var(--border);border-radius:8px;padding:4px 5px;box-shadow:0 2px 8px rgba(17,24,39,.14);transform:translate(-50%,-100%);margin-top:-10px}
.lite-shape-bar.on{display:flex}
.lite-text-input{position:absolute;z-index:8;display:none;min-width:16px;max-width:360px;min-height:18px;padding:2px 4px;font:12px "IBM Plex Mono",monospace;line-height:1.35;color:#111827;background:rgba(255,255,255,.96);border:1px dashed #1a56db;border-radius:3px;outline:none;white-space:pre-wrap;overflow:hidden;resize:none;cursor:text}
.lite-text-input.on{display:inline-block}
.lite-shape-color{width:22px;height:20px;padding:0;border:1px solid var(--border);border-radius:5px;background:none;cursor:pointer}
.lite-shape-color::-webkit-color-swatch-wrapper{padding:2px}
.lite-shape-color::-webkit-color-swatch{border:none;border-radius:4px}
.lite-shape-select{height:22px;padding:0 2px;border:1px solid var(--border);border-radius:5px;background:#fff;color:#374151;font-size:11px;cursor:pointer}
.lite-shape-del{width:20px;height:20px;border:1px solid var(--border);border-radius:5px;background:#fff;color:#ef4444;font-size:11px;cursor:pointer;display:flex;align-items:center;justify-content:center}
#lite-shape-delete:hover{background:#fef2f2}
.lite-shape-del.on{background:#eef3ff;border-color:var(--accent);color:var(--accent)}
#lite-shape-target2{font-size:9px;font-weight:700;color:#374151}
#lite-shape-target2:hover{background:#f1f5f9}
#lite-shape-dash:hover{background:#f1f5f9}
#lite-shape-edit{color:#374151}
#lite-shape-edit:hover{background:#f1f5f9}
#lite-shape-bg-clear{color:#374151}
#lite-shape-bg-clear:hover{background:#f1f5f9}

/* ═══════════════════════════════════════════
   POPUP — desktop
   ═══════════════════════════════════════════ */
.overlay{display:none;position:fixed;inset:0;z-index:9999;background:rgba(17,24,39,.5);backdrop-filter:blur(4px);align-items:center;justify-content:center}
.overlay.on{display:flex}
.pbox{background:var(--surface);border:1px solid var(--border);border-radius:10px;box-shadow:0 20px 60px rgba(0,0,0,.15);width:99vw;max-width:1800px;height:94vh;display:flex;flex-direction:column;overflow:hidden;animation:popIn .2s ease;outline:none}
@keyframes popIn{from{opacity:0;transform:scale(.96) translateY(14px)}to{opacity:1;transform:none}}
.phdr{display:grid;grid-template-columns:1fr auto 1fr;align-items:center;padding:7px 14px;background:var(--surf2);border-bottom:1px solid var(--border);flex-shrink:0}
.phdr-left{display:flex;align-items:center;gap:8px}
.phdr-center{display:flex;align-items:flex-end;justify-content:center}
.phdr-right{display:flex;align-items:center;justify-content:flex-end}
.ptitle{font-family:var(--font-ui);font-size:17px;font-weight:800;color:var(--accent);letter-spacing:1.5px;flex-shrink:0;white-space:nowrap}
.popup-search-wrap{position:relative;display:flex;align-items:center}
.popup-search-wrap .s-icon{position:absolute;left:10px;top:50%;transform:translateY(-50%);color:var(--muted);font-size:12px;pointer-events:none}
.popup-search-input{width:100px;padding:5px 10px 5px 28px;border-radius:20px;border:1px solid var(--border);background:var(--surface);color:var(--text);font-family:var(--font-mono);font-size:11px;outline:none;transition:border-color .15s,width .2s}
.popup-search-input:focus{border-color:var(--accent);box-shadow:0 0 0 2px rgba(26,86,219,.12);width:200px}
.ctabs{display:flex;gap:2px;align-items:center;flex-wrap:wrap}
.ctab{height:30px;line-height:1;display:inline-flex;align-items:center;justify-content:center;font-size:11px;font-family:var(--font-mono);font-weight:600;padding:0 11px;border-radius:5px;border:1px solid var(--border);background:var(--bg);color:var(--muted);cursor:pointer;transition:all .15s;white-space:nowrap}
.ctab.on{background:var(--surface);color:var(--accent);border-color:var(--border);box-shadow:inset 0 -2px 0 var(--accent);font-weight:700}
.ctab:hover:not(.on){color:var(--accent);background:#eef3ff}
.closebtn{width:28px;height:28px;border-radius:50%;border:1px solid var(--border);background:var(--bg);color:var(--muted);font-size:16px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all .15s}
.closebtn:hover{background:var(--red);color:#fff;border-color:var(--red)}
.pbody{flex:1;overflow:hidden;position:relative}
.tpanel{position:absolute;inset:0;display:none}
.tpanel.on{display:block}
.tpanel iframe{width:100%;height:100%;border:none;display:block}

/* ═══════════════════════════════════════════
   MOBILE PORTRAIT
   ═══════════════════════════════════════════ */
@media(max-width:768px){
  header{padding:8px 14px;gap:4px}
  header h1{font-size:15px;letter-spacing:1.5px}
  #clock{font-size:10px}

  .overlay{backdrop-filter:none;background:rgba(17,24,39,0)}
  .pbox{width:100vw;height:100dvh;border-radius:0;border:none;animation:none}
  .phdr{display:flex;flex-direction:column;flex-shrink:0}
  .phdr-left,.phdr-center,.phdr-right{display:none}

  .sig-list{display:flex;flex-direction:column;gap:3px}
  .momentum-list{display:flex;flex-direction:column;gap:3px}
  .momentum-box{padding:8px 10px}
  #signal-header{flex-direction:column;align-items:flex-start;gap:4px;padding:7px 10px}
  #signal-header .panel-hdr-left{display:flex;align-items:center;gap:8px;flex-wrap:nowrap;width:100%}
  #signal-header .panel-title{white-space:nowrap;flex-shrink:0}
  #signal-header #sig-meta{display:block;width:100%;white-space:nowrap;overflow:visible;line-height:1.35}
  /* Header HEATMAP mobile dùng CSS Grid (không flex-column) để mũi tên thu/mở luôn nằm cùng
     hàng với tiêu đề "Heatmap" ở mọi trạng thái mở/thu gọn, padding phải luôn cố định 16px
     giống .panel-hdr — không cần rule riêng cho .collapsed vì hàng 2 tự co về 0 khi ẩn. */
  .hmap-panel-hdr{
    display:grid;
    grid-template-columns:1fr auto;
    align-items:center;
    gap:4px 6px;
    padding:9px 16px;
  }
  .hmap-hdr-row1{grid-column:1;grid-row:1;min-width:0;width:100%;overflow-x:auto;scrollbar-width:none;gap:6px}
  .hmap-hdr-row1::-webkit-scrollbar{display:none}
  .hmap-hdr-row1>*{flex-shrink:0}
  .hmap-toggle-icon{grid-column:2;grid-row:1;justify-self:end;margin-left:0}
  .hmap-search-input{width:90px !important}
  .hmap-search-input:focus{width:90px !important}
  .hmap-ts-wrap{
    grid-column:1/-1;
    grid-row:2;
    white-space:nowrap !important;
    overflow-x:auto !important;
    overflow-y:hidden !important;
    text-overflow:clip !important;
    width:100% !important;
    max-width:100% !important;
    margin-left:0 !important;
    display:block !important;
    font-size:10px !important;
    line-height:1.4 !important;
    scrollbar-width:none;
    -webkit-overflow-scrolling:touch;
  }
  .hmap-ts-wrap::-webkit-scrollbar{display:none}
  .health-layout{grid-template-columns:1fr}
  .health-body{height:auto;display:block;overflow:visible}
  .health-chartbox{height:280px}
  /* Neo theo right thay vì left:% — trên mobile .health-chartbox giãn hết màn hình khiến
     left:88.4% (đúng cho desktop 2 cột) rơi sát mép phải, chữ "VNINDEX" bị overflow:hidden cắt mất. */
  .health-vni-toggle{left:auto;right:8px}
  /* Trục dọc health-svg L=52,R=112,W=900 cố định (xem comment tại hàm vẽ chart) → nhãn "100"
     luôn nằm ~1.3-4.7% bề rộng khung, dải màu bắt đầu ~5.78%. .health-period-tabs mặc định
     left:8px sẽ đè lên nhãn "100" khi khung giãn hết bề ngang ở mobile — dùng % để nhóm nút
     luôn bắt đầu đúng mép dải màu trên mọi bề rộng máy. */
  .health-period-tabs{left:5.8%}
  .health-score{font-size:36px}
  .vnd-panel{margin:12px 10px 0;padding:12px 12px 10px}
  .vnd-panel:last-child{margin-bottom:12px}
  .vnd-controls{flex-direction:column;align-items:flex-start;gap:8px}
  .vnd-chart-area{height:220px}
  .vnd-summary{grid-template-columns:repeat(2,minmax(0,1fr))}
  .treemap-wrap{
    width:100% !important;
    margin-left:0 !important;
    aspect-ratio:16/9;
    height:auto;
  }
  .market-frame{height:70vh}
  .frame-shrink{height:70vh}
  .tri-tabs [data-tab="fireant"],
  #tri-content-fireant{display:none !important}

  /* Panel CHART trên mobile/iPhone: toolbar cuộn ngang mượt (-webkit-overflow-scrolling:touch),
     input font-size 16px để iOS không tự zoom, hỗ trợ safe-area cho notch/Home bar. Dropdown
     chỉ báo được portal ra <body> + neo động bằng JS (_litePositionIndDropdown/
     syncLiteIndDropdownPortal) — CSS ở đây chỉ giữ khung (bo góc/đổ bóng/cuộn), không định vị cứng. */
  #lite-chart-panel{display:block}
  .lite-chart-toolbar{
    flex-wrap:nowrap;
    overflow-x:auto;
    overflow-y:hidden;
    -webkit-overflow-scrolling:touch;
    scrollbar-width:none;
    padding-bottom:2px;
  }
  .lite-chart-toolbar::-webkit-scrollbar{display:none}
  .lite-chart-toolbar>*{flex-shrink:0}
  .lite-indicators{flex-wrap:nowrap}
  .lite-draw-toolbar{flex-wrap:nowrap}
  .lite-chart-input{width:90px !important}
  .lite-chart-input:focus{width:90px !important}
  .mob-search-input, .mob-land-search, .popup-search-input, .hmap-search-input, .lite-chart-input {
    font-size: 16px !important;
  }
  button, input, select, .ctab, .mob-tab-btn, .mob-land-tab, .lite-draw-btn, .lite-tf-btn, .lite-ind-group-btn {
    touch-action: manipulation;
    -webkit-tap-highlight-color: transparent;
  }
  #lite-chart {
    touch-action: pan-x pan-y;
  }
  /* QUAN TRỌNG: top/left ở đây KHÔNG đánh dấu !important — nếu có, nó sẽ luôn thắng inline
     style JS gán (_litePositionIndDropdown), khiến dropdown dính cứng góc (0,0) bất kể JS tính
     ra vị trí nào (lỗi đã từng gặp). 0,0 chỉ là giá trị khởi tạo trước khi JS chạy lần đầu. */
  .lite-ind-dropdown {
    position: fixed !important;
    top: 0;
    left: 0;
    width: max-content !important;
    max-width: min(260px, 92vw) !important;
    max-height: 55vh !important;
    overflow-y: auto !important;
    box-shadow: 0 8px 24px rgba(17,24,39,.22) !important;
    border-radius: 10px !important;
    z-index: 99999 !important;
    padding: 8px 10px !important;
    -webkit-overflow-scrolling: touch;
  }
  .lite-chart-frame{height:56vh;min-height:300px;max-height:520px}
  .lite-groups-sidebar{width:150px}
  .lite-groups-sidebar.on~.lite-vietstock-iframe{left:150px;width:calc(100% - 150px)}
  .lite-groups-sidebar.on+.lite-chart-title,
  .lite-groups-sidebar.on~.lite-chart-signal{left:162px}
  .lite-groups-sidebar.on~.lite-chart-bigprice{left:150px}
  .lite-chart-bigprice .bp-price{font-size:16px}
  .lite-alert-panel{width:calc(100vw - 28px)}
}
@media screen and (max-width:768px) and (orientation:landscape){
  /* Xoay ngang: rộng hơn portrait nên khung chart có thể cao hơn 1 chút mà
     vẫn còn chỗ cho toolbar + phần dashboard phía trên. */
  .lite-chart-frame{height:72vh;max-height:640px}
  /* Landscape giữ dropdown theo cơ chế absolute trong .lite-ind-group như desktop.
     Rule mobile chung phía trên dùng fixed cho portrait portal; override này tránh
     các thiết bị landscape hẹp bị rơi về góc viewport. */
  .lite-ind-dropdown{
    position:absolute !important;
    top:calc(100% + 4px) !important;
    left:0 !important;
    width:max-content !important;
    max-width:min(260px, 92vw) !important;
    max-height:55vh !important;
    z-index:20 !important;
  }
}

/* ═══════════════════════════════════════════
   MOBILE POPUP HEADER — portrait
   ═══════════════════════════════════════════ */
.mob-hdr-row1{
  display:flex;align-items:center;gap:6px;
  padding:8px 10px 6px;
  background:var(--surf2);
  border-bottom:1px solid var(--border);
  flex-shrink:0;
}
.mob-sym-title{
  font-family:var(--font-ui);font-size:20px;font-weight:800;
  color:var(--accent);letter-spacing:1px;flex-shrink:0;white-space:nowrap;
}
.mob-search-wrap{position:relative;flex-shrink:0}
.mob-search-wrap .s-icon{position:absolute;left:8px;top:50%;transform:translateY(-50%);color:var(--muted);font-size:11px;pointer-events:none}
.mob-search-input{
  width:60px;padding:4px 6px 4px 22px;
  border-radius:20px;border:1px solid var(--border);
  background:var(--surface);color:var(--text);
  font-family:var(--font-mono);font-size:11px;outline:none;
}

/* Row dưới: tabs cuộn */
.mob-tab-row{
  display:flex;flex-direction:row;flex-wrap:nowrap;align-items:center;
  overflow-x:auto;overflow-y:hidden;
  -webkit-overflow-scrolling:touch;
  overscroll-behavior-x:contain;
  padding:4px 8px;gap:4px;
  background:var(--surf2);
  border-bottom:1px solid var(--border);
  scrollbar-width:none;-ms-overflow-style:none;
  flex-shrink:0;
}
.mob-tab-row::-webkit-scrollbar{display:none}
.mob-tab-btn{
  flex-shrink:0;white-space:nowrap;
  padding:6px 12px;border-radius:6px;
  border:1px solid var(--border);
  font-size:12px;font-family:var(--font-mono);font-weight:600;
  cursor:pointer;background:var(--bg);color:var(--muted);
  display:inline-flex;align-items:center;
  min-height:36px;touch-action:manipulation;
  transition:all .15s;
}
.mob-tab-btn.on{
  background:var(--surface);color:var(--accent);
  border-color:var(--accent);font-weight:700;
  box-shadow:0 2px 0 var(--accent);
}

/* ═══════════════════════════════════════════
   MOBILE POPUP HEADER — landscape
   ═══════════════════════════════════════════ */
.mob-hdr-landscape{
  display:none;
  flex-direction:row;align-items:center;
  padding:0 6px 0 8px;
  background:var(--surf2);border-bottom:1px solid var(--border);
  flex-shrink:0;height:40px;gap:4px;overflow:hidden;
}
.mob-land-sym{
  font-family:var(--font-ui);font-size:18px;font-weight:800;
  color:var(--accent);white-space:nowrap;flex-shrink:0;letter-spacing:.8px;
}
.mob-land-search-wrap{position:relative;flex-shrink:0}
.mob-land-search-wrap .s-icon{position:absolute;left:7px;top:50%;transform:translateY(-50%);color:var(--muted);font-size:10px;pointer-events:none}
.mob-land-search{
  width:68px;height:30px;padding:4px 6px 4px 22px;
  border-radius:16px;border:1px solid var(--border);
  background:var(--surface);color:var(--text);
  font-family:var(--font-mono);font-size:10px;outline:none;
  transition:border-color .15s; 
}
.mob-land-search:focus{
  border-color:var(--accent);
  box-shadow:0 0 0 2px rgba(26,86,219,.12);
  /* Tuyệt đối không khai báo width ở đây -> ô tìm kiếm sẽ đứng im, không bị giãn ra */
}
/* Tabs cuộn giữa */
.mob-land-tabs{
  display:flex;flex-direction:row;flex-wrap:nowrap;
  overflow-x:auto;overflow-y:hidden;
  -webkit-overflow-scrolling:touch;
  scrollbar-width:none;-ms-overflow-style:none;
  gap:3px;flex:1;min-width:0;align-items:center;padding:2px 0;
}
.mob-land-tabs::-webkit-scrollbar{display:none}
.mob-land-tab{
  flex-shrink:0;white-space:nowrap;
  padding:4px 10px;border-radius:4px;
  border:1px solid var(--border);
  font-size:11px;font-family:var(--font-mono);font-weight:600;
  cursor:pointer;background:var(--bg);color:var(--muted);
  display:inline-flex;align-items:center;height:30px;
  touch-action:manipulation;transition:all .15s;
}
.mob-land-tab.on{
  background:var(--surface);color:var(--accent);
  border-color:var(--accent);font-weight:700;
}
/* Nút X vuông cố định phải */
.mob-land-close{
  flex-shrink:0;width:30px;height:30px;
  border-radius:4px;border:1px solid var(--border);
  background:var(--bg);color:var(--muted);
  font-size:14px;cursor:pointer;
  display:flex;align-items:center;justify-content:center;
  transition:all .15s;touch-action:manipulation;
}
.mob-land-close:hover,.mob-land-close:active{background:var(--red);color:#fff;border-color:var(--red)}

/* ═══════════════════════════════════════════
   Nút X bên cạnh phải — portrait only
   ═══════════════════════════════════════════ */
#mob-close-float{display:none}

@media screen and (max-width:768px) and (orientation:portrait){
  .mob-hdr-row1{display:none !important}
  .mob-tab-row{display:none !important}
  .mob-hdr-landscape{display:flex !important}

  /* FIX #4: nút X gần như hoàn toàn trong suốt */
  #mob-close-float{
    display:flex;
    position:fixed;right:0;top:50%;transform:translateY(-50%);
    z-index:10001;
    width:11px;
    height:320px;
    border-radius:6px 0 0 6px;
    background:rgba(17,24,39,.03);
    border:1px solid rgba(17,24,39,.04);
    border-right:none;
    color:rgba(0,0,0,.06);
    font-size:9px;
    align-items:center;justify-content:center;
    cursor:pointer;
    touch-action:manipulation;
    -webkit-tap-highlight-color:transparent;
    writing-mode:vertical-rl;
  }
  #mob-close-float:active{
    background:rgba(17,24,39,.15);
    color:rgba(0,0,0,.3);
  }
}

@media screen and (max-width:768px) and (orientation:landscape){
  /* Ẩn portrait rows */
  .mob-hdr-row1{display:none !important}
  .mob-tab-row{display:none !important}
  /* Hiện landscape row */
  .mob-hdr-landscape{display:flex !important}

  #mob-close-float{
    display:none !important;
  }

}

#edge-swipe-zone{position:fixed;left:0;top:0;width:30px;height:100%;z-index:10000;display:none;touch-action:pan-y}
#edge-swipe-zone.on{display:block}

/* Scrollbar global */
::-webkit-scrollbar{width:5px;height:5px}
::-webkit-scrollbar-track{background:var(--bg)}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
::-webkit-scrollbar-thumb:hover{background:var(--muted)}
</style>
</head>
<body>

<header>
  <h1>⚡ Scanner Dashboard</h1>
  <div class="hdr-right">
    <div style="display:flex;gap:4px;align-items:center">
      <button class="hdr-sound-btn" id="hdr-sound-btn" title="Bật âm thanh thông báo (đang tắt)">🔕</button>
      <button class="hdr-sound-btn" id="hdr-desktop-notify-btn" title="Bật thông báo desktop">🖥</button>
    </div>
    <div class="dot-live"></div>
    <span id="clock">--:--:--</span>
  </div>
</header>

<div class="wrap" id="main-wrap">
  <!-- SIGNALS -->
  <div class="panel">
    <div class="panel-hdr signal-header-toggle" id="signal-header">
      <div class="panel-hdr-left">
        <span class="panel-title">Tín hiệu hôm nay</span>
        <button class="journal-star-btn" id="journal-open-btn" title="Mở Note mua">★</button>
      </div>
      <span class="panel-meta" id="sig-meta">Đang tải...</span>
    </div>
    <div class="pbar-wrap"><div class="pbar-fill" id="pbar-sig"></div></div>
    <div class="panel-body">
      <div class="sig-list" id="sig-list">
        <div class="empty"><div class="big">📡</div><div>Đang tải...</div></div>
      </div>
	    </div>
	    <div class="momentum-box" id="momentum-box">
	      <div class="momentum-section">
	        <div class="momentum-title">Động lượng</div>
	        <div class="momentum-list" id="momentum-list">
	        </div>
	      </div>
	      <div class="momentum-section">
	        <div class="momentum-title">Sức mạnh</div>
	        <div class="momentum-list strength-list" id="strength-list">
	        </div>
	      </div>
	    </div>
	  </div>

  <!-- HEATMAP -->
  <div class="panel hmap-panel" id="hmap-panel">
    <div class="hmap-panel-hdr" id="hmap-toggle">
      <div class="hmap-hdr-row1">
        <span class="panel-title">Heatmap</span>
        <div class="hmap-search-wrap">
          <span class="s-icon">🔍</span>
          <input class="hmap-search-input" id="hmap-search" type="text" placeholder="Tìm mã" maxlength="10" autocomplete="off" spellcheck="false">
        </div>
        <button class="hmap-link-btn" id="hmap-follow-btn">FOLLOW</button>
      </div>
      <span class="panel-meta hmap-ts-wrap" id="hmap-ts">Đang tải...</span>
      <span class="hmap-toggle-icon">▶</span>
    </div>
    <div class="panel-body" style="padding:8px">
      <div class="hmap-outer">
        <div class="hmap-row" id="hmap-grid">
          <div class="empty"><div class="big">🗺</div><div>Đang tải...</div></div>
        </div>
      </div>
    </div>
  </div>

  <!-- LIGHTWEIGHT CHART -->
  <div class="panel lite-chart-panel collapsed" id="lite-chart-panel">
    <div class="panel-hdr" id="lite-chart-toggle">
      <div class="lite-chart-toolbar">
        <span class="panel-title" id="lite-chart-title-label" title="Bấm để quay lại chart tự vẽ mặc định">CHART</span>
        <div class="lite-chart-search-wrap">
          <span class="s-icon">🔍</span>
          <input class="lite-chart-input" id="lite-chart-input" placeholder="Tìm mã" maxlength="10" spellcheck="false" lang="en" autocapitalize="characters" autocorrect="off" autocomplete="off" inputmode="text" translate="no">
        </div>
        <button class="lite-draw-btn lite-fav-btn" id="lite-fav-btn" title="Thêm/bỏ mã đang xem khỏi Favorite" aria-label="Thêm/bỏ mã đang xem khỏi Favorite">☆</button>
        <button class="lite-draw-btn" id="lite-groups-toggle-btn" title="Danh sách nhóm ngành / mã" aria-label="Danh sách nhóm ngành / mã">☰</button>
        <button class="lite-draw-btn" id="lite-vietstock-toggle-btn" title="Mở chart Vietstock (thay cho chart tự vẽ) — bấm chữ CHART để quay lại chart tự vẽ" aria-label="Mở chart Vietstock">V</button>
        <div class="lite-tf-tabs" id="lite-chart-tf">
          <button class="lite-tf-btn on" data-tf="1D">D</button>
          <button class="lite-tf-btn" data-tf="1W">W</button>
          <button class="lite-tf-btn" data-tf="1M">M</button>
        </div>
        <div class="lite-indicators" id="lite-indicators">
          <div class="lite-ind-group" data-group="signalgrp">
            <input type="checkbox" class="lite-ind-master" value="signalgrp_on">
            <button type="button" class="lite-ind-group-btn" data-group-btn="signalgrp">Signal<span class="lite-ind-count" data-count="signalgrp"></span><span class="lite-ind-caret">▾</span></button>
            <div class="lite-ind-dropdown" data-dropdown="signalgrp">
              <label><input type="checkbox" value="signal">Buy-Signal</label>
              <label><input type="checkbox" value="volcolor">Volume-Signal</label>
              <label><input type="checkbox" value="bigprice">Giá phóng to</label>
            </div>
          </div>
          <div class="lite-ind-group" data-group="maema">
            <input type="checkbox" class="lite-ind-master" value="maema_on">
            <button type="button" class="lite-ind-group-btn" data-group-btn="maema">MA/EMA<span class="lite-ind-count" data-count="maema"></span><span class="lite-ind-caret">▾</span></button>
            <div class="lite-ind-dropdown" data-dropdown="maema" style="min-width:120px">
              <div class="lite-ind-dropdown-sub-title">MA</div>
              <label><input type="checkbox" value="ma10"><span class="lite-ind-label" data-ind="ma10" title="Bấm để đổi màu">MA10</span><input type="color" class="lite-ind-color" data-ind="ma10" value="#ff0000"></label>
              <label><input type="checkbox" value="ma20"><span class="lite-ind-label" data-ind="ma20" title="Bấm để đổi màu">MA20</span><input type="color" class="lite-ind-color" data-ind="ma20" value="#008000"></label>
              <label><input type="checkbox" value="ma30"><span class="lite-ind-label" data-ind="ma30" title="Bấm để đổi màu">MA30</span><input type="color" class="lite-ind-color" data-ind="ma30" value="#1a56db"></label>
              <label><input type="checkbox" value="ma50"><span class="lite-ind-label" data-ind="ma50" title="Bấm để đổi màu">MA50</span><input type="color" class="lite-ind-color" data-ind="ma50" value="#800080"></label>
              <label><input type="checkbox" value="ma100"><span class="lite-ind-label" data-ind="ma100" title="Bấm để đổi màu">MA100</span><input type="color" class="lite-ind-color" data-ind="ma100" value="#d97706"></label>
              <label><input type="checkbox" value="ma200"><span class="lite-ind-label" data-ind="ma200" title="Bấm để đổi màu">MA200</span><input type="color" class="lite-ind-color" data-ind="ma200" value="#9b6af1"></label>
              <div class="lite-ind-dropdown-sub-title">EMA</div>
              <label><input type="checkbox" value="ema10"><span class="lite-ind-label" data-ind="ema10" title="Bấm để đổi màu">EMA10</span><input type="color" class="lite-ind-color" data-ind="ema10" value="#ff0000"></label>
              <label><input type="checkbox" value="ema20"><span class="lite-ind-label" data-ind="ema20" title="Bấm để đổi màu">EMA20</span><input type="color" class="lite-ind-color" data-ind="ema20" value="#16a34a"></label>
              <label><input type="checkbox" value="ema30"><span class="lite-ind-label" data-ind="ema30" title="Bấm để đổi màu">EMA30</span><input type="color" class="lite-ind-color" data-ind="ema30" value="#0ea5e9"></label>
              <label><input type="checkbox" value="ema50"><span class="lite-ind-label" data-ind="ema50" title="Bấm để đổi màu">EMA50</span><input type="color" class="lite-ind-color" data-ind="ema50" value="#c026d3"></label>
              <label><input type="checkbox" value="ema100"><span class="lite-ind-label" data-ind="ema100" title="Bấm để đổi màu">EMA100</span><input type="color" class="lite-ind-color" data-ind="ema100" value="#eab308"></label>
              <label><input type="checkbox" value="ema200"><span class="lite-ind-label" data-ind="ema200" title="Bấm để đổi màu">EMA200</span><input type="color" class="lite-ind-color" data-ind="ema200" value="#78350f"></label>
            </div>
          </div>
          <div class="lite-ind-group" data-group="trend">
            <input type="checkbox" value="trend">
            <button type="button" class="lite-ind-group-btn" data-group-btn="trend">Trend<span class="lite-ind-caret">▾</span></button>
            <div class="lite-ind-dropdown" data-dropdown="trend">
              <div style="display:flex;gap:10px">
                <label style="display:flex;align-items:center;gap:4px;font-size:10px;cursor:pointer"><input type="radio" name="trend-mode" value="regular" checked>Regular</label>
                <label style="display:flex;align-items:center;gap:4px;font-size:10px;cursor:pointer"><input type="radio" name="trend-mode" value="smoothed">Smoothed</label>
              </div>
              <div style="display:flex;gap:10px;margin-top:6px;padding-top:6px;border-top:1px solid var(--border)">
                <label style="display:flex;align-items:center;gap:4px;font-size:10px">Tăng<input type="color" class="lite-ind-color lite-ind-color-visible" data-ind="trend-up" value="#64fa96"></label>
                <label style="display:flex;align-items:center;gap:4px;font-size:10px">Giảm<input type="color" class="lite-ind-color lite-ind-color-visible" data-ind="trend-down" value="#fa9696"></label>
              </div>
            </div>
          </div>
          <label class="lite-ind-simple"><input type="checkbox" value="bb"><span class="lite-ind-label" data-ind="bb" title="Bấm để đổi màu">BB</span><input type="color" class="lite-ind-color" data-ind="bb" value="#9333ea"></label>
          <label class="lite-ind-simple"><input type="checkbox" value="rsi"><span class="lite-ind-label" data-ind="rsi" title="Bấm để đổi màu">RSI</span><input type="color" class="lite-ind-color" data-ind="rsi" value="#7c6ee6"></label>
          <label class="lite-ind-simple"><input type="checkbox" value="macd">MACD</label>
        </div>
        <div class="lite-draw-toolbar" id="lite-draw-toolbar">
          <button class="lite-draw-btn on" data-tool="cursor" title="Con trỏ / chọn / di chuyển">▲</button>
          <button class="lite-draw-btn" data-tool="trendline" title="Đường kẻ chéo">╱</button>
          <button class="lite-draw-btn" data-tool="hline" title="Đường kẻ ngang">─</button>
          <button class="lite-draw-btn" data-tool="vline" title="Đường kẻ dọc">❘</button>
          <button class="lite-draw-btn" data-tool="rect" title="Hình chữ nhật">▭</button>
          <button class="lite-draw-btn" data-tool="channel" title="Kênh giá: click-click chọn 2 điểm, rồi rê chuột lên/xuống để tạo kênh, click để chốt"><svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><line x1="3" y1="14" x2="9" y2="2"/><line x1="7" y1="14" x2="13" y2="2"/></svg></button>
          <button class="lite-draw-btn" data-tool="arrow" title="Mũi tên: click-click chọn điểm đầu và điểm cuối">↗</button>
          <button class="lite-draw-btn" data-tool="zigzag" title="Zigzag: click nối tiếp từng điểm, double-click để kết thúc"><svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><polyline points="1,13 5,4 8,11 11,3 15,8"/></svg></button>
          <button class="lite-draw-btn" data-tool="arc" title="Đường cong bán nguyệt: click-click chọn 2 điểm như đường thẳng, rồi rê chuột lên/xuống để uốn cong, click để chốt">◠</button>
          <button class="lite-draw-btn" data-tool="position" title="Entry/Target/Stoploss">🎯</button>
          <button class="lite-draw-btn" data-tool="text" title="Text">Aa</button>
          <div class="lite-draw-sep"></div>
          <input type="color" id="lite-draw-color" class="lite-draw-color" value="#1a56db" title="Màu công cụ vẽ">
          <div class="lite-draw-sep"></div>
          <button class="lite-draw-btn" id="lite-draw-undo" title="Xóa nét vừa vẽ">↩</button>
          <button class="lite-draw-btn" id="lite-draw-clear" title="Xóa tất cả">🗑</button>
          <div class="lite-draw-sep"></div>
          <button class="lite-draw-btn" id="lite-fireant-widget" title="Mở widget FireAnt cho mã hiện tại" aria-label="Mở widget FireAnt cho mã hiện tại"><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12a9 9 0 1 0 9-9"/><path d="M12 3v9l6 3"/><path d="M21 3v5h-5"/></svg></button>
          <div class="lite-alert-wrap" id="lite-alert-wrap">
            <button class="lite-draw-btn" id="lite-alert-btn" title="Cảnh báo giá">🔔<span class="lite-alert-badge" id="lite-alert-badge"></span></button>
            <div class="lite-alert-panel" id="lite-alert-panel">
              <div class="lite-alert-title"><span>CẢNH BÁO</span><span style="display:flex;gap:4px"><button class="lite-alert-mini" id="lite-alert-sound-notify" title="Bật âm thanh cảnh báo">🔕</button><button class="lite-alert-mini" id="lite-alert-desktop-notify" title="Bật thông báo desktop">🖥</button><button class="lite-alert-mini" id="lite-alert-seen" title="Đã xem">✓</button></span></div>
              <div class="lite-alert-grid">
                <div class="lite-alert-field">
                  <label>Mã</label>
                  <input id="lite-alert-symbol" maxlength="10" spellcheck="false">
                </div>
                <div class="lite-alert-field">
                  <label>Nguồn</label>
                  <select id="lite-alert-left-type">
                    <option value="price">Giá cổ phiếu</option>
                    <option value="ma">Đường trung bình</option>
                  </select>
                </div>
                <div class="lite-alert-field" id="lite-alert-left-kind-wrap">
                  <label>Loại nguồn</label>
                  <select id="lite-alert-left-kind"><option>MA</option><option>EMA</option></select>
                </div>
                <div class="lite-alert-field" id="lite-alert-left-period-wrap">
                  <label>Chu kỳ nguồn</label>
                  <select id="lite-alert-left-period"><option>10</option><option>20</option><option>30</option><option>50</option><option>100</option><option>200</option></select>
                </div>
                <div class="lite-alert-field">
                  <label>Điều kiện</label>
                  <select id="lite-alert-operator">
                    <option value="gte">&ge; (Tăng lên / Cắt lên)</option>
                    <option value="lte">&le; (Giảm về / Cắt xuống)</option>
                  </select>
                </div>
                <div class="lite-alert-field">
                  <label>Đối tượng</label>
                  <select id="lite-alert-right-type">
                    <option value="ma">Đường trung bình</option>
                    <option value="price" selected>Mức giá</option>
                  </select>
                </div>
                <div class="lite-alert-field" id="lite-alert-price-wrap">
                  <label>Mức giá</label>
                  <input id="lite-alert-price" type="number" step="0.01" min="0" placeholder="0.00">
                </div>
                <div class="lite-alert-field" id="lite-alert-right-kind-wrap">
                  <label>Loại đích</label>
                  <select id="lite-alert-right-kind"><option>MA</option><option>EMA</option></select>
                </div>
                <div class="lite-alert-field" id="lite-alert-right-period-wrap">
                  <label>Chu kỳ đích</label>
                  <select id="lite-alert-right-period"><option>10</option><option selected>20</option><option>30</option><option>50</option><option>100</option><option>200</option></select>
                </div>
                <div class="lite-alert-field full">
                  <label>Kênh báo</label>
                  <div class="lite-alert-checks">
                    <label class="lite-alert-check"><input type="checkbox" id="lite-alert-dashboard" checked>Dashboard</label>
                    <label class="lite-alert-check"><input type="checkbox" id="lite-alert-telegram">Telegram</label>
                  </div>
                </div>
                <div class="lite-alert-field full" id="lite-alert-chat-wrap">
                  <label>Telegram chat ID</label>
                  <input id="lite-alert-chat" placeholder="VD: 1207484510">
                </div>
                <div class="lite-alert-field full">
                  <label>Sau khi khớp</label>
                  <select id="lite-alert-after">
                    <option value="disable" selected>Tự tắt sau khi báo</option>
                    <option value="keep">Giữ cảnh báo</option>
                  </select>
                </div>
              </div>
              <div class="lite-alert-actions">
                <button class="lite-alert-action" id="lite-alert-test">Test Telegram</button>
                <button class="lite-alert-action primary" id="lite-alert-save">Lưu</button>
              </div>
              <div class="lite-alert-list" id="lite-alert-list"></div>
            </div>
          </div>
          <button class="lite-draw-btn" id="lite-chart-popout-align-btn" title="Vị trí Popout: Căn PHẢI (Bấm để đổi sang Trái)" aria-label="Đổi vị trí Popout">◨</button>
          <button class="lite-draw-btn" id="lite-chart-popout-btn" title="Mở CHART trong cửa sổ riêng" aria-label="Mở CHART trong cửa sổ riêng">⧉</button>
        </div>
      </div>
      <span class="lite-chart-toggle-icon">▶</span>
    </div>
    <div class="lite-chart-frame" id="lite-chart-frame" tabindex="0">
      <button class="lite-draw-btn chart-copy-btn" id="lite-draw-copy" title="Sao chép ảnh chart vào clipboard" aria-label="Sao chép ảnh chart vào clipboard"><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7h3l1.6-2h8.8L18 7h2a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2Z"/><circle cx="12" cy="13" r="3.5"/></svg></button>
      <div class="lite-groups-sidebar" id="lite-groups-sidebar">
        <div class="lg-toolbar">
          <span class="lg-toolbar-title">NHÓM NGÀNH</span>
        </div>
        <div class="lite-groups-list" id="lite-groups-list"></div>
      </div>
      <span class="lite-chart-title" id="lite-chart-title">Đang tải...</span>
      <span class="lite-chart-signal" id="lite-chart-signal"></span>
      <span class="lite-chart-bigprice" id="lite-chart-bigprice" title="Giá phóng to + biến động/khối lượng (ước tính hết phiên)"></span>
      <div class="lite-rect-tooltip" id="lite-rect-tooltip"></div>
      <div class="lite-shape-bar" id="lite-shape-bar">
        <input type="color" id="lite-shape-color" class="lite-shape-color" title="Đổi màu hình vẽ">
        <input type="color" id="lite-shape-target-color" class="lite-shape-color" title="Đổi màu Target">
        <button id="lite-shape-target2" class="lite-shape-del" title="Bật/tắt Target 2">T2</button>
        <select id="lite-shape-font-size" class="lite-shape-select" title="Cỡ chữ">
          <option value="10">10</option>
          <option value="11">11</option>
          <option value="12">12</option>
          <option value="13">13</option>
          <option value="14">14</option>
          <option value="16">16</option>
          <option value="18">18</option>
          <option value="20">20</option>
          <option value="24">24</option>
          <option value="28">28</option>
        </select>
        <select id="lite-shape-font-family" class="lite-shape-select" title="Font chữ">
          <option value="mono">Mono</option>
          <option value="sans">Sans</option>
          <option value="serif">Serif</option>
        </select>
        <input type="color" id="lite-shape-bg-color" class="lite-shape-color" title="Màu nền chữ">
        <button id="lite-shape-bg-clear" class="lite-shape-del" title="Bỏ màu nền">⊘</button>
        <button id="lite-shape-edit" class="lite-shape-del" title="Sửa nội dung">✎</button>
        <button id="lite-shape-dash" class="lite-shape-del" title="Chuyển nét liền / nét đứt">┈</button>
        <button id="lite-shape-pct" class="lite-shape-del" title="Bật/tắt hiển thị % ngay trên hộp (mặc định tắt)">%</button>
        <select id="lite-shape-arrow-width" class="lite-shape-select" title="Độ dày mũi tên">
          <option value="1">Mỏng</option>
          <option value="2">Vừa</option>
          <option value="3">Đậm</option>
          <option value="4">Rất đậm</option>
          <option value="6">Siêu đậm</option>
        </select>
        <button id="lite-shape-arrow-style" class="lite-shape-del" title="Chuyển mũi tên thường / mũi tên vệt (đuôi nhọn, thân phình to, đầu nhọn)">◭</button>
        <button id="lite-shape-zigzag-fill" class="lite-shape-del" title="Bật/tắt dải màu tô nền ZigZag (tắt = chỉ còn đường lên xuống)">▥</button>
        <button id="lite-shape-delete" class="lite-shape-del" title="Xóa hình này">✕</button>
      </div>
      <textarea class="lite-text-input" id="lite-text-input" spellcheck="false" rows="1"></textarea>
      <input class="lite-chart-search" id="lite-chart-search" maxlength="10" spellcheck="false" autocomplete="off" lang="en" autocapitalize="characters" autocorrect="off" inputmode="text" translate="no">
      <div id="lite-chart"></div>
      <canvas class="lite-draw-canvas" id="lite-draw-canvas"></canvas>
      <div id="lite-rsi-chart"></div>
      <div class="lite-macd-resizer" id="lite-macd-resizer"></div>
      <div id="lite-macd-chart"></div>
      <iframe id="lite-vietstock-iframe" class="lite-vietstock-iframe" src="about:blank" title="Vietstock chart"></iframe>
      <div class="lite-xhair-v" id="lite-xhair-v"></div>
      <div class="lite-xhair-h" id="lite-xhair-h"></div>
      <div class="lite-xhair-price" id="lite-xhair-price"></div>
      <div class="lite-xhair-time" id="lite-xhair-time"></div>
      <div class="lite-chart-empty" id="lite-chart-empty">Đang tải chart...</div>
    </div>
  </div>

  <!-- MARKET (Fireant / Mrk Health / Treemap — gộp 1 thẻ, chuyển nội dung bằng tab) -->
  <div class="panel tri-panel" id="tri-panel">
    <div class="panel-hdr tri-hdr" id="tri-hdr">
      <span class="panel-title">MARKET</span>
      <div class="tri-tabs" id="tri-tabs">
        <span class="tri-tab" data-tab="fireant">Fireant</span>
        <span class="tri-tab on" data-tab="health">Mrk Health</span>
        <span class="tri-tab" data-tab="treemap">Treemap</span>
      </div>
      <span class="tri-toggle" id="tri-toggle">▶</span>
    </div>
    <div class="tri-body" id="tri-body">
      <div class="tri-content" id="tri-content-fireant">
        <iframe class="market-frame" id="market-frame" src="https://fireant.vn/dashboard" allowfullscreen></iframe>
      </div>
      <div class="tri-content on" id="tri-content-health">
        <button class="lite-draw-btn health-copy-btn" id="health-copy-btn" title="Sao chép ảnh Mrk Health vào clipboard" aria-label="Sao chép ảnh Mrk Health vào clipboard"><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7h3l1.6-2h8.8L18 7h2a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2Z"/><circle cx="12" cy="13" r="3.5"/></svg></button>

        <div class="health-body" id="health-body">
          <div class="health-layout">
            <div class="health-chartbox">
              <div class="health-period-tabs" id="health-period-tabs">
                <button type="button" class="health-period-tab on" data-days="60">60p</button>
                <button type="button" class="health-period-tab" data-days="120">120p</button>
              </div>
              <label class="health-vni-toggle" id="health-vni-toggle" title="Hiện/ẩn đường VNINDEX để đối chiếu">
                <input type="checkbox" id="health-vni-checkbox">
                <span class="health-vni-swatch"></span>VNINDEX
              </label>
              <svg class="health-svg" id="health-svg" viewBox="0 0 900 360" preserveAspectRatio="none"></svg>
            </div>
            <div class="health-side">
              <div class="health-score-card">
                <div class="health-score-top">
                  <div>
                    <div class="health-label" id="health-label">--</div>
                    <div class="health-meta" id="health-date">--</div>
                  </div>
                  <div class="health-score" id="health-score">--</div>
                </div>
                <div class="health-tags" id="health-tags"></div>
              </div>
              <div class="health-analysis" id="health-analysis">
                <div class="health-analysis-title">Nhận định</div>
                <div class="health-empty">Đang tải dữ liệu Mrk Health...</div>
              </div>
            </div>
          </div>
        </div>
        <div class="vnd-panel" id="vnd-valuation-panel">
          <div class="vnd-panel-hdr">
            <span class="vnd-panel-title">Định giá thị trường</span>
            <span class="vnd-status" id="vnd-valuation-status">Đang tải...</span>
          </div>
          <div class="vnd-controls">
            <div class="vnd-tabs" id="vnd-valuation-tabs">
              <span class="vnd-tab on" data-metric="pe">P/E</span>
              <span class="vnd-tab" data-metric="pb">P/B</span>
            </div>
            <label class="vnd-period">Chu kỳ
              <select id="vnd-valuation-period">
                <option value="90">3 tháng</option>
                <option value="180">6 tháng</option>
                <option value="365" selected>1 năm</option>
                <option value="1095">3 năm</option>
                <option value="1825">5 năm</option>
              </select>
            </label>
          </div>
          <div class="vnd-chart-area">
            <svg class="vnd-svg" id="vnd-valuation-svg" preserveAspectRatio="none"></svg>
          </div>
          <div class="vnd-legend">
            <span class="vnd-legend-item"><span class="vnd-swatch" style="background:#9b55ff"></span>VNINDEX</span>
            <span class="vnd-legend-item"><span class="vnd-swatch" id="vnd-valuation-metric-swatch" style="background:#f59b00"></span><span id="vnd-valuation-metric-legend">P/E</span></span>
          </div>
          <div class="vnd-error" id="vnd-valuation-error"></div>
        </div>
        <div class="vnd-panel" id="vnd-allocation-panel">
          <div class="vnd-panel-hdr">
            <span class="vnd-panel-title">Phân bổ thị trường</span>
            <span class="vnd-status" id="vnd-allocation-status">Đang tải...</span>
          </div>
          <div class="vnd-controls">
            <label class="vnd-period" style="margin-left:auto">Chu kỳ
              <select id="vnd-allocation-period">
                <option value="90">3 tháng</option>
                <option value="180">6 tháng</option>
                <option value="365" selected>1 năm</option>
                <option value="1095">3 năm</option>
                <option value="1825">5 năm</option>
              </select>
            </label>
          </div>
          <div class="vnd-chart-area">
            <svg class="vnd-svg" id="vnd-allocation-svg" preserveAspectRatio="none"></svg>
          </div>
          <div class="vnd-legend">
            <span class="vnd-legend-item"><span class="vnd-swatch" style="background:#9b55ff"></span>VNINDEX</span>
            <span class="vnd-legend-item"><span class="vnd-swatch" style="background:#0e9f6e"></span>Trên MA50</span>
            <span class="vnd-legend-item"><span class="vnd-swatch" style="background:#f59b00"></span>Trên MA200</span>
          </div>
          <div class="vnd-error" id="vnd-allocation-error"></div>
        </div>
        <div class="vnd-panel" id="vnd-foreign-panel">
          <div class="vnd-panel-hdr">
            <span class="vnd-panel-title">Khối ngoại</span>
            <span class="vnd-status" id="vnd-foreign-status">Đang tải...</span>
          </div>
          <div class="vnd-chart-area">
            <svg class="vnd-svg" id="vnd-foreign-svg" preserveAspectRatio="none"></svg>
          </div>
          <div class="vnd-error" id="vnd-foreign-error"></div>
        </div>
        <div class="vnd-panel" id="vnd-proprietary-panel">
          <div class="vnd-panel-hdr">
            <span class="vnd-panel-title">Tự doanh</span>
            <span class="vnd-status" id="vnd-proprietary-status">Đang tải...</span>
          </div>
          <div class="vnd-chart-area">
            <svg class="vnd-svg" id="vnd-proprietary-svg" preserveAspectRatio="none"></svg>
          </div>
          <div class="vnd-error" id="vnd-proprietary-error"></div>
        </div>
      </div>
      <div class="tri-content" id="tri-content-treemap">
        <button class="lite-draw-btn treemap-copy-btn" id="treemap-copy-btn" title="Sao chép ảnh Treemap vào clipboard" aria-label="Sao chép ảnh Treemap vào clipboard"><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7h3l1.6-2h8.8L18 7h2a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2Z"/><circle cx="12" cy="13" r="3.5"/></svg></button>
        <div class="treemap-wrap" id="treemap-wrap"><svg class="treemap-svg" id="treemap-svg" viewBox="0 0 1600 900" preserveAspectRatio="xMidYMid meet"></svg></div>
      </div>
    </div>
  </div>
</div>


<!-- TRADE JOURNAL -->
<div class="journal-overlay" id="journal-overlay">
  <div class="journal-box">
    <iframe class="journal-frame" id="journal-frame" src="about:blank"></iframe>
  </div>
</div>

<div class="alert-toast-wrap" id="alert-toast-wrap"></div>

<footer id="footer-txt">Scanner Bot Dashboard</footer>

<!-- POPUP -->
<div class="overlay" id="overlay">
  <button id="mob-close-float" aria-label="Đóng">✕</button>
  <div class="pbox" id="pbox" tabindex="-1">
    <!-- Desktop header (ẩn trên mobile qua CSS) -->
    <div class="phdr" id="popup-phdr">
      <div class="phdr-left">
        <span class="ptitle" id="ptitle">Chart</span>
        <div class="popup-search-wrap">
          <span class="s-icon">🔍</span>
          <input class="popup-search-input" id="popup-search" type="text" placeholder="Tìm mã" maxlength="10" autocomplete="off" spellcheck="false">
        </div>
      </div>
      <div class="phdr-center">
        <div class="ctabs" id="popup-ctabs">
          <button class="ctab on" data-tab="vs">📈 Vietstock</button>
          <button class="ctab" data-tab="chart">📊 Chart</button>
          <button class="ctab" data-tab="vnd-cs">⚖️ Cơ bản</button>
          <button class="ctab" data-tab="vnd-news">🗞️ Tin tức</button>
          <button class="ctab" data-tab="vnd-sum">📄 Tổng quan</button>
          <button class="ctab" data-tab="24h">💬 Fireant</button>
        </div>
      </div>
      <div class="phdr-right">
        <button class="closebtn" id="popup-close-btn">✕</button>
      </div>
    </div>

    <!-- Mobile portrait header — Row 1: tên + search -->
    <div class="mob-hdr-row1" id="mob-hdr-row1" style="display:none">
      <span class="mob-sym-title" id="mob-ptitle">Chart</span>
      <div class="mob-search-wrap">
        <span class="s-icon">🔍</span>
        <input class="mob-search-input" id="mob-search" type="text" placeholder="Tìm mã" maxlength="10" autocomplete="off" spellcheck="false">
      </div>
    </div>

    <!-- Mobile portrait header — Row 2: tabs cuộn -->
    <div class="mob-tab-row" id="mob-tab-row" style="display:none">
      <button class="mob-tab-btn on" data-tab="vs">📈 Vietstock</button>
      <button class="mob-tab-btn" data-tab="chart">📊 Chart</button>
      <button class="mob-tab-btn" data-tab="vnd-cs">⚖️ Cơ bản</button>
      <button class="mob-tab-btn" data-tab="vnd-news">🗞️ Tin tức</button>
      <button class="mob-tab-btn" data-tab="vnd-sum">📄 Tổng quan</button>
      <button class="mob-tab-btn" data-tab="24h">💬 Fireant</button>
    </div>

    <!-- Mobile landscape header — 1 hàng -->
    <div class="mob-hdr-landscape" id="mob-hdr-landscape">
      <span class="mob-land-sym" id="mob-land-sym">Chart</span>
      <div class="mob-land-search-wrap">
        <span class="s-icon">🔍</span>
        <input class="mob-land-search" id="mob-land-search" type="text" placeholder="Tìm mã" maxlength="10" autocomplete="off" spellcheck="false">
      </div>
      <div class="mob-land-tabs" id="mob-land-tabs">
        <button class="mob-land-tab on" data-tab="vs">📈 Vietstock</button>
        <button class="mob-land-tab" data-tab="chart">📊 Chart</button>
        <button class="mob-land-tab" data-tab="vnd-cs">⚖️ Cơ bản</button>
        <button class="mob-land-tab" data-tab="vnd-news">🗞️ Tin tức</button>
        <button class="mob-land-tab" data-tab="vnd-sum">📄 Tổng quan</button>
        <button class="mob-land-tab" data-tab="24h">💬 Fireant</button>
      </div>
      <!-- X vuông cố định phải -->
      <button class="mob-land-close" id="mob-land-close">✕</button>
    </div>

    <div class="pbody">
      <div class="tpanel on" id="panel-vs"><iframe id="iframe-vs" src="about:blank" allowfullscreen></iframe></div>
      <div class="tpanel" id="panel-chart"><iframe id="iframe-chart" src="about:blank" allowfullscreen></iframe></div>
      <div class="tpanel" id="panel-vnd-cs"><iframe id="iframe-vnd-cs" src="about:blank" allowfullscreen></iframe></div>
      <div class="tpanel" id="panel-vnd-news"><iframe id="iframe-vnd-news" src="about:blank" allowfullscreen></iframe></div>
      <div class="tpanel" id="panel-vnd-sum"><iframe id="iframe-vnd-sum" src="about:blank" allowfullscreen></iframe></div>
      <div class="tpanel" id="panel-24h"><iframe id="iframe-24h" src="about:blank" allowfullscreen></iframe></div>
    </div>
  </div>
</div>

<div id="edge-swipe-zone"></div>

<!-- defer: file đã preload song song ở <head>, defer chỉ để khỏi chặn parse HTML tại dòng này.
     Script defer luôn chạy đúng thứ tự khai báo nên window.LightweightCharts vẫn sẵn sàng trước
     khi dashboard-main.js cần dùng — hành vi không đổi, chỉ bớt chặn parse. -->
<script defer src="/static/lightweight-charts.min.js"></script>
<script defer src="/dashboard-main.js"></script>
</body>
</html>
"""

# JS chính, tách khỏi DASHBOARD_HTML và serve qua route /dashboard-main.js: HTML
# nhẹ hơn, tải JS song song thay vì đợi hết HTML, tận dụng được hook gzip. Hành
# vi thực thi và nội dung JS giữ nguyên 100% — chỉ đổi cách gửi tới trình duyệt.
DASHBOARD_MAIN_JS = r"""
'use strict';
// DOM CACHE
const $=id=>document.getElementById(id);
const DOM={
  clock:$('clock'),hdrSoundBtn:$('hdr-sound-btn'),hdrDesktopNotifyBtn:$('hdr-desktop-notify-btn'),sigMeta:$('sig-meta'),sigList:$('sig-list'),
  signalHeader:$('signal-header'),momentumBox:$('momentum-box'),momentumList:$('momentum-list'),strengthList:$('strength-list'),
  hmapTs:$('hmap-ts'),hmapGrid:$('hmap-grid'),hmapSearch:$('hmap-search'),
  hmapPanel:$('hmap-panel'),hmapToggle:$('hmap-toggle'),
  triPanel:$('tri-panel'),triHdr:$('tri-hdr'),triTabs:$('tri-tabs'),
  healthVniCheckbox:$('health-vni-checkbox'),healthVniToggle:$('health-vni-toggle'),healthPeriodTabs:$('health-period-tabs'),
  healthSvg:$('health-svg'),healthScore:$('health-score'),healthLabel:$('health-label'),
  healthDate:$('health-date'),healthTags:$('health-tags'),
  healthAnalysis:$('health-analysis'),
  treemapWrap:$('treemap-wrap'),treemapSvg:$('treemap-svg'),treemapCopyBtn:$('treemap-copy-btn'),
  liteChartPanel:$('lite-chart-panel'),liteChartToggle:$('lite-chart-toggle'),
  liteChartTitleLabel:$('lite-chart-title-label'),
  liteFavBtn:$('lite-fav-btn'),
  liteVietstockToggleBtn:$('lite-vietstock-toggle-btn'),liteVietstockIframe:$('lite-vietstock-iframe'),
  liteChart:$('lite-chart'),
  liteChartFrame:$('lite-chart-frame'),liteChartSearch:$('lite-chart-search'),
  liteRsiChart:$('lite-rsi-chart'),liteMacdChart:$('lite-macd-chart'),
  liteMacdResizer:$('lite-macd-resizer'),liteChartInput:$('lite-chart-input'),
  liteChartTf:$('lite-chart-tf'),liteIndicators:$('lite-indicators'),
  liteChartTitle:$('lite-chart-title'),liteChartEmpty:$('lite-chart-empty'),
  liteChartSignal:$('lite-chart-signal'),
  liteChartBigPrice:$('lite-chart-bigprice'),
  liteRectTooltip:$('lite-rect-tooltip'),
  liteXhairV:$('lite-xhair-v'),liteXhairH:$('lite-xhair-h'),liteXhairPrice:$('lite-xhair-price'),liteXhairTime:$('lite-xhair-time'),
  liteDrawToolbar:$('lite-draw-toolbar'),liteDrawCanvas:$('lite-draw-canvas'),
  liteDrawUndo:$('lite-draw-undo'),liteDrawClear:$('lite-draw-clear'),
  liteDrawColor:$('lite-draw-color'),
  liteShapeBar:$('lite-shape-bar'),liteShapeColor:$('lite-shape-color'),liteShapeDelete:$('lite-shape-delete'),
  liteShapeTargetColor:$('lite-shape-target-color'),liteShapeFontSize:$('lite-shape-font-size'),
  liteShapeTarget2:$('lite-shape-target2'),
  liteShapeFontFamily:$('lite-shape-font-family'),liteShapeBgColor:$('lite-shape-bg-color'),
  liteShapeBgClear:$('lite-shape-bg-clear'),liteShapeEdit:$('lite-shape-edit'),
  liteTextInput:$('lite-text-input'),
  liteShapeDash:$('lite-shape-dash'),liteDrawCopy:$('lite-draw-copy'),
  liteShapePct:$('lite-shape-pct'),
  liteFireantBtn:$('lite-fireant-widget'),
  liteShapeArrowStyle:$('lite-shape-arrow-style'),liteShapeZigzagFill:$('lite-shape-zigzag-fill'),
  liteShapeArrowWidth:$('lite-shape-arrow-width'),
  liteAlertWrap:$('lite-alert-wrap'),liteAlertBtn:$('lite-alert-btn'),liteAlertBadge:$('lite-alert-badge'),
  liteAlertSoundNotify:$('lite-alert-sound-notify'),liteAlertDesktopNotify:$('lite-alert-desktop-notify'),
  liteAlertPanel:$('lite-alert-panel'),liteAlertSymbol:$('lite-alert-symbol'),
  liteAlertLeftType:$('lite-alert-left-type'),liteAlertLeftKind:$('lite-alert-left-kind'),
  liteAlertLeftPeriod:$('lite-alert-left-period'),liteAlertLeftKindWrap:$('lite-alert-left-kind-wrap'),
  liteAlertLeftPeriodWrap:$('lite-alert-left-period-wrap'),liteAlertOperator:$('lite-alert-operator'),
  liteAlertRightType:$('lite-alert-right-type'),liteAlertPrice:$('lite-alert-price'),
  liteAlertPriceWrap:$('lite-alert-price-wrap'),liteAlertRightKind:$('lite-alert-right-kind'),
  liteAlertRightPeriod:$('lite-alert-right-period'),liteAlertRightKindWrap:$('lite-alert-right-kind-wrap'),
  liteAlertRightPeriodWrap:$('lite-alert-right-period-wrap'),liteAlertDashboard:$('lite-alert-dashboard'),
  liteAlertTelegram:$('lite-alert-telegram'),liteAlertChat:$('lite-alert-chat'),
  liteAlertChatWrap:$('lite-alert-chat-wrap'),liteAlertAfter:$('lite-alert-after'),
  liteAlertSave:$('lite-alert-save'),liteAlertTest:$('lite-alert-test'),
  liteAlertSeen:$('lite-alert-seen'),liteAlertList:$('lite-alert-list'),alertToastWrap:$('alert-toast-wrap'),
  pbarSig:$('pbar-sig'),healthCopyBtn:$('health-copy-btn'),
  journalOverlay:$('journal-overlay'),journalFrame:$('journal-frame'),
  overlay:$('overlay'),pbox:$('pbox'),
  ptitle:$('ptitle'),popupSearch:$('popup-search'),popupCtabs:$('popup-ctabs'),
  mobHdrRow1:$('mob-hdr-row1'),mobPtitle:$('mob-ptitle'),mobSearch:$('mob-search'),
  mobTabRow:$('mob-tab-row'),
  mobHdrLand:$('mob-hdr-landscape'),mobLandSym:$('mob-land-sym'),
  mobLandSearch:$('mob-land-search'),mobLandTabs:$('mob-land-tabs'),
  ifVs:$('iframe-vs'),
  edgeZone:$('edge-swipe-zone'),mobClose:$('mob-close-float'),
  footer:$('footer-txt'),
  lgToggleBtn:$('lite-groups-toggle-btn'),lgSidebar:$('lite-groups-sidebar'),
  lgList:$('lite-groups-list'),
};
const IS_MOBILE=()=>window.innerWidth<=768;
const IS_LANDSCAPE=()=>window.innerWidth>window.innerHeight;
const IS_STANDALONE_PWA=()=>window.navigator.standalone===true||(window.matchMedia&&window.matchMedia('(display-mode: standalone)').matches);
const TABS_ALL=['vs','chart','vnd-cs','vnd-news','vnd-sum','24h'];
const IFRAME_LAZY={
  'chart':    s=>`/?chartPopout=1&embedded=1&sym=${encodeURIComponent(s)}`,
  'vnd-cs':   s=>`https://dstock.vndirect.com.vn/tong-quan/${s}/diem-nhan-co-ban-popup?theme=light`,
  'vnd-news': s=>`https://dstock.vndirect.com.vn/tong-quan/${s}/tin-tuc-ma-popup?type=dn&theme=light`,
  'vnd-sum':  s=>`https://dstock.vndirect.com.vn/tong-quan/${s}?theme=light`,
  '24h':      s=>`https://fireant.vn/ma-chung-khoan/${s}`,
};
const BADGE_MAP={
  'BREAKOUT':'b-BREAKOUT',
  'PK-PIVOT':'b-POCKET',
  'PRE-BREAK':'b-PREBREAK',
  'BT-BREAK':'b-BBREAKP',
  'BOT-FISH':'b-BFISH',
  'MA-CROSS':'b-MACROSS',
  'PK-BREAK':'b-BREAKOUT'
};
const SIGNAL_LABEL_MAP={
  'BREAKOUT':'BREAKOUT',
  'PK-PIVOT':'PK-PIVOT',
  'PK-BREAK':'PK-BREAK',
  'BT-BREAK':'BT-BREAK',
  'BOT-FISH':'BOT-FISH',
  'MA-CROSS':'MA-CROSS',
  'PRE-BREAK':'PRE-BREAK'
};
const signalLabel=s=>SIGNAL_LABEL_MAP[s]||s;
function rsClass(rs){
  const v=Number(rs);
  if(!Number.isFinite(v))return'';
  if(v>90)return'rs-90';
  if(v>80)return'rs-80';
  if(v>50)return'rs-50';
  return'rs-low';
}
function rsBadge(rs){
  const v=Number(rs);
  if(!Number.isFinite(v))return'<span class="rs-slot"></span>';
  return `<span class="rs-badge ${rsClass(v)}">${Math.round(v)}</span>`;
}
function pctCellForSym(sym,fallbackPct=null){
  const entry=(window._lastHmapData||{})[String(sym||'').toUpperCase()];
  const raw=entry&&typeof entry.pct==='number'?entry.pct:fallbackPct;
  const v=Number(raw);
  if(!Number.isFinite(v))return{txt:'—',color:'#6b7280'};
  return{txt:(v>=0?'+':'')+v.toFixed(1)+'%',color:v>=0?'#0e9f6e':'#e02424'};
}
let _sigTodayMap=new Map();
let _momentumTodayMap=new Map();
let _strengthTodayMap=new Map();
let _attentTodayMap=new Map();
let _breakvolTodayMap=new Map();
let _lastStrengthRows=[];
let _liteRsScore=null;
let SIG_TTL=30,HMAP_TTL=5,HEALTH_TTL=1800;
let _sym='',_tab='vs';
const FOLLOW_KEY='dashboard_follow_symbols';
const FOLLOW_ON_KEY='dashboard_follow_on';
let FOLLOW=loadFollowSymbols();
let FOLLOW_ON=localStorage.getItem(FOLLOW_ON_KEY)!=='0';
const ALERT_CLIENT_KEY='dashboard_alert_client_id';
const ALERT_CHAT_KEY='dashboard_alert_telegram_chat_id';
const ALERT_POLL_SEC=10;
let _alertRules=[],_alertEvents=[],_alertShownIds=new Set(),_editingAlertRuleId=null;
function getAlertClientId(){
  let id=_liteLSGet(ALERT_CLIENT_KEY,'');
  if(!id){
    id=(window.crypto&&window.crypto.randomUUID)?window.crypto.randomUUID():('client-'+Date.now().toString(36)+'-'+Math.random().toString(36).slice(2));
    _liteLSSet(ALERT_CLIENT_KEY,id);
  }
  return id;
}
function loadFollowSymbols(){
  try{return JSON.parse(localStorage.getItem(FOLLOW_KEY)||'[]').filter(Boolean).map(s=>String(s).toUpperCase());}
  catch(e){return [];}
}
function parseFollowSymbols(raw){
  return [...new Set(String(raw||'').toUpperCase().split(/[^A-Z0-9]+/).map(s=>s.trim()).filter(s=>s.length>=2))];
}
function saveFollowSymbols(syms){
  FOLLOW=syms;
  localStorage.setItem(FOLLOW_KEY,JSON.stringify(FOLLOW));
  localStorage.setItem(FOLLOW_ON_KEY,FOLLOW_ON?'1':'0');
  const btn=$('hmap-follow-btn');if(btn){btn.classList.toggle('on',FOLLOW.length>0&&FOLLOW_ON);btn.title=FOLLOW.length?`${FOLLOW_ON?'ON':'OFF'}: ${FOLLOW.join(', ')}`:'Nhập danh sách mã follow';}
}
function editFollowSymbols(){
  const raw=prompt('Nhập mã FOLLOW, cách nhau bằng dấu phẩy:',FOLLOW.join(', '));
  if(raw===null)return false;
  FOLLOW_ON=true;
  saveFollowSymbols(parseFollowSymbols(raw));
  if(typeof _lgSyncFollowIntoFavorites==='function')_lgSyncFollowIntoFavorites();
  renderHeatmap(window._lastHmapData||{});
  return true;
}
let _isChartPanelOpen=false;
const LITE_IND_KEY='dashboard_lite_indicators';
const LITE_IND_COLOR_KEY='dashboard_lite_ind_colors';
const LITE_TREND_MODE_KEY='dashboard_lite_trend_mode';
const LITE_LAST_SYMBOL_KEY='dashboard_lite_last_symbol';
function loadLiteTrendMode(){
  let mode='regular';
  try{mode=localStorage.getItem(LITE_TREND_MODE_KEY)||'regular';}catch(e){}
  document.querySelectorAll('input[name="trend-mode"]').forEach(r=>{r.checked=(r.value===mode);});
}
function saveLiteTrendMode(){
  const mode=_liteTrendMode();
  try{localStorage.setItem(LITE_TREND_MODE_KEY,mode);}catch(e){}
}
function _liteTrendMode(){
  const el=document.querySelector('input[name="trend-mode"]:checked');
  return el?el.value:'regular';
}
function _liteLSGet(key,fallback){
  try{return localStorage.getItem(key)||fallback;}catch(e){return fallback;}
}
function _liteLSSet(key,val){
  try{localStorage.setItem(key,val);}catch(e){}
}
const LITE_MA_PERIODS=[10,20,30,50,100,200];
const LITE_EMA_PERIODS=[10,20,30,50,100,200];
const LITE_MA_DEFAULT_COLORS=['#ff0000','#008000','#1a56db','#800080','#d97706','#9b6af1'];
const LITE_EMA_DEFAULT_COLORS=['#ff0000','#16a34a','#0ea5e9','#c026d3','#eab308','#78350f'];
const LITE_RSI_PERIOD=14;
const LITE_RSI_DEFAULT_COLOR='#7c6ee6';
const LITE_CANDLE_UP_COLOR='#26a69a', LITE_CANDLE_DOWN_COLOR='#ef5350';
const LITE_IND_DEFAULT_COLORS={bb:'#9333ea',rsi:LITE_RSI_DEFAULT_COLOR,'trend-up':'#64fa96','trend-down':'#fa9696'};
LITE_MA_PERIODS.forEach((p,idx)=>{LITE_IND_DEFAULT_COLORS['ma'+p]=LITE_MA_DEFAULT_COLORS[idx];});
LITE_EMA_PERIODS.forEach((p,idx)=>{LITE_IND_DEFAULT_COLORS['ema'+p]=LITE_EMA_DEFAULT_COLORS[idx];});
let _liteIndColors={...LITE_IND_DEFAULT_COLORS};
function loadLiteIndColors(){
  let stored={};
  try{stored=JSON.parse(localStorage.getItem(LITE_IND_COLOR_KEY)||'{}')||{};}catch(e){stored={};}
  _liteIndColors={...LITE_IND_DEFAULT_COLORS,...stored};
  DOM.liteIndicators?.querySelectorAll('.lite-ind-color').forEach(inp=>{
    if(_liteIndColors[inp.dataset.ind])inp.value=_liteIndColors[inp.dataset.ind];
  });
}
function saveLiteIndColors(){
  _liteLSSet(LITE_IND_COLOR_KEY,JSON.stringify(_liteIndColors));
}
function bindLiteIndColorPickers(){
  DOM.liteIndicators?.querySelectorAll('.lite-ind-label').forEach(span=>{
    span.addEventListener('click',e=>{
      e.preventDefault();e.stopPropagation();
      // Tra theo <label> cha trực tiếp — dropdown có thể bị portal ra <body> (mobile portrait).
      const inp=span.closest('label')?.querySelector(`.lite-ind-color[data-ind="${span.dataset.ind}"]`);
      if(inp)inp.click();
    });
  });
  DOM.liteIndicators?.querySelectorAll('.lite-ind-color').forEach(inp=>{
    inp.addEventListener('input',()=>{
      _liteIndColors[inp.dataset.ind]=inp.value;
      saveLiteIndColors();
      renderLiteIndicators();
    });
  });
}
function updateLiteIndGroupCounts(){
  DOM.liteIndicators?.querySelectorAll('.lite-ind-group').forEach(grp=>{
    const key=grp.dataset.group;
    // Tra qua document, không grp.querySelectorAll — dropdown group có thể bị portal ra <body>.
    const dd=document.querySelector(`.lite-ind-dropdown[data-dropdown="${key}"]`);
    const n=dd?dd.querySelectorAll('input[type="checkbox"]:checked').length:0;
    const badge=grp.querySelector(`.lite-ind-count[data-count="${key}"]`);
    if(badge){badge.textContent=n||'';badge.classList.toggle('on',n>0);}
  });
}
function closeAllLiteIndDropdowns(except){
  DOM.liteIndicators?.querySelectorAll('.lite-ind-group.open').forEach(g=>{
    if(g!==except){
      g.classList.remove('open');
      syncLiteIndDropdownPortal(g,false);
    }
  });
}
function _liteUseIndDropdownPortal(){
  return window.matchMedia('(max-width:768px) and (orientation:portrait)').matches;
}
function _litePositionIndDropdown(btn,dd){
  /* Neo dropdown ngay dưới nút vừa bấm, tự tính top/left theo viewport (thay vì
     để CSS lo) vì dd đã bị portal ra <body>. Dùng visualViewport khi có để lấy
     đúng vùng nhìn thấy lúc bàn phím ảo che một phần màn hình. */
  const margin=8; // khoảng cách tối thiểu giữ tới mép vùng nhìn thấy
  const vv=window.visualViewport;
  const voX=vv?vv.offsetLeft:0, voY=vv?vv.offsetTop:0;
  const vw=vv?vv.width:window.innerWidth, vh=vv?vv.height:window.innerHeight;
  const r=btn.getBoundingClientRect();
  const dw=dd.offsetWidth, dh=dd.offsetHeight;
  // Canh trái theo mép trái của nút, kẹp lại để dropdown không tràn ra ngoài 2 mép màn hình.
  let left=Math.min(Math.max(r.left,voX+margin),voX+vw-dw-margin);
  // Mặc định bung xuống dưới nút, không đủ chỗ thì bung lên trên, thiếu cả 2 thì kẹp trong vùng nhìn thấy.
  let top;
  if(r.bottom+dh+margin<=voY+vh)top=r.bottom+4;
  else if(r.top-dh-4>=voY+margin)top=r.top-dh-4;
  else top=Math.max(voY+margin,voY+vh-dh-margin);
  dd.style.left=left+'px';
  dd.style.top=top+'px';
}
function _liteRepositionOpenDropdown(){
  // Gọi lại khi toolbar cuộn/resize/bàn phím ảo đóng-mở lúc dropdown đang mở trên mobile (đã portal ra <body>).
  const grp=DOM.liteIndicators?.querySelector('.lite-ind-group.open');
  if(!grp)return;
  const dd=document.querySelector(`.lite-ind-dropdown[data-dropdown="${grp.dataset.group}"]`);
  const btn=grp.querySelector('.lite-ind-group-btn');
  if(dd&&btn&&dd.parentElement===document.body)_litePositionIndDropdown(btn,dd);
}
function syncLiteIndDropdownPortal(grp,open){
  /* Portrait mobile: dropdown chỉ báo dùng position:fixed nhưng là con của .lite-chart-toolbar
     (có -webkit-overflow-scrolling:touch) — quirk WebKit khiến container cuộn này tự thành
     "khung chứa" mới cho fixed bên trong, dropdown bị kẹt không hiện ra. Xử lý: khi MỞ, chuyển
     dropdown ra làm con trực tiếp <body> rồi neo theo vị trí nút; khi ĐÓNG, trả về chỗ cũ +
     xoá top/left inline. Desktop/landscape (>768px) không cần portal (vẫn absolute thường).
     3 lưu ý từng gây lỗi: (1) dùng document.querySelector theo data-dropdown, không dùng
     grp.querySelector — vì sau khi portal ra <body>, dropdown không còn là con của grp nữa;
     (2) rule CSS ẩn/hiện dựa vào .lite-ind-group.open không còn hiệu lực khi đã portal, phải tự
     set display:flex qua inline style; (3) phải set display:flex TRƯỚC khi đo offsetWidth/Height
     trong _litePositionIndDropdown, nếu không kích thước đo được sẽ luôn là 0. */
  const key=grp.dataset.group;
  const dd=document.querySelector(`.lite-ind-dropdown[data-dropdown="${key}"]`);
  if(!dd)return;
  if(!_liteUseIndDropdownPortal()){
    if(dd.parentElement===document.body){
      dd.style.display='';
      dd.style.visibility='';
      dd.style.left='';
      dd.style.top='';
      grp.appendChild(dd);
    }
    return;
  }
  if(open){
    if(dd.parentElement!==document.body)document.body.appendChild(dd);
    dd.style.visibility='hidden';
    dd.style.display='flex';
    const btn=grp.querySelector('.lite-ind-group-btn');
    if(btn){
      _litePositionIndDropdown(btn,dd);
      requestAnimationFrame(()=>{
        if(grp.classList.contains('open')&&dd.parentElement===document.body){
          _litePositionIndDropdown(btn,dd);
        }
      });
    }
    dd.style.visibility='';
  }else if(dd.parentElement===document.body){
    dd.style.display='';
    dd.style.visibility='';
    dd.style.left='';
    dd.style.top='';
    grp.appendChild(dd);
  }
}
function bindLiteIndGroupDropdowns(){
  DOM.liteIndicators?.querySelectorAll('.lite-ind-group-btn').forEach(btn=>{
    btn.addEventListener('click',e=>{
      e.preventDefault();e.stopPropagation();
      const grp=btn.closest('.lite-ind-group');
      if(!grp)return;
      const willOpen=!grp.classList.contains('open');
      closeAllLiteIndDropdowns();
      grp.classList.toggle('open',willOpen);
      syncLiteIndDropdownPortal(grp,willOpen);
    });
  });
  DOM.liteIndicators?.querySelectorAll('.lite-ind-dropdown').forEach(dd=>{
    dd.addEventListener('click',e=>e.stopPropagation());
  });
  document.addEventListener('click',()=>closeAllLiteIndDropdowns());
  window.addEventListener('orientationchange',()=>closeAllLiteIndDropdowns());
  // Đăng ký lắng nghe cuộn/resize duy nhất 1 lần — hàm tự kiểm tra dropdown nào đang mở.
  document.querySelector('.lite-chart-toolbar')?.addEventListener('scroll',_liteRepositionOpenDropdown,{passive:true});
  window.addEventListener('resize',_liteRepositionOpenDropdown);
  window.visualViewport?.addEventListener('resize',_liteRepositionOpenDropdown);
  updateLiteIndGroupCounts();
}
function _liteHexToRgba(hex,alpha,fallbackRgb='147,51,234'){
  const m=/^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex||'');
  if(!m)return `rgba(${fallbackRgb},${alpha})`;
  const r=parseInt(m[1],16),g=parseInt(m[2],16),b=parseInt(m[3],16);
  return `rgba(${r},${g},${b},${alpha})`;
}
function _litePaneIsActive(chart){
  if(chart===_liteChart)return true;
  if(chart===_liteRsiChart)return _liteChecked('rsi')&&DOM.liteRsiChart?.style.display!=='none';
  if(chart===_liteMacdChart)return _liteChecked('macd')&&DOM.liteMacdChart?.style.display!=='none';
  return false;
}
function _liteSubpaneCharts(){
  return [_liteChart,_liteRsiChart,_liteMacdChart].filter(chart=>chart&&_litePaneIsActive(chart));
}
function _liteGetVisibleLogicalRange(){
  const range=_liteChart&&_liteChart.timeScale&&_liteChart.timeScale().getVisibleLogicalRange();
  return range&&Number.isFinite(range.from)&&Number.isFinite(range.to)?range:null;
}
function _liteApplyVisibleLogicalRange(range){
  if(!range||!Number.isFinite(range.from)||!Number.isFinite(range.to))return false;
  _liteSyncing=true;
  _liteSubpaneCharts().forEach(chart=>{
    try{chart.timeScale().setVisibleLogicalRange(range);}catch(e){}
  });
  _liteSyncing=false;
  return true;
}
function _liteSyncVisibleRangeFrom(source,range){
  if(_liteSyncing||!range||!_litePaneIsActive(source))return;
  _liteSyncing=true;
  _liteSubpaneCharts().forEach(chart=>{
    if(chart!==source){
      try{chart.timeScale().setVisibleLogicalRange(range);}catch(e){}
    }
  });
  _liteSyncing=false;
}
let _liteChart=null,_liteRsiChart=null,_liteMacdChart=null,_liteCandle=null,_liteVolume=null,_liteRsiCrosshairSeries=null,_liteMacdCrosshairSeries=null,_liteSymbol=_liteLSGet(LITE_LAST_SYMBOL_KEY,'VNINDEX');
const _chartSyncChannel=typeof BroadcastChannel!=='undefined'?new BroadcastChannel('chart_popout_sync'):null;
let _liteMainWhite=null,_liteRsiWhite=null,_liteMacdWhite=null,_liteBBFillData=null,_liteTrendFillData=null;
// Mũi tên báo mua tự vẽ canvas (không dùng setMarkers()) vì setMarkers() làm trục giá autoScale lại mỗi lần bật/tắt, gây co giãn chart.
let _liteBuyArrowData=null; // {color} | null — CHỈ giữ màu; time/price của nến LUÔN đọc live từ _liteData
// Vị trí mũi tên tính lại tại thời điểm vẽ (không lưu cứng) để luôn khớp nến mới nhất khi auto-refresh chèn nến mới vào giữa.
let _liteTf='1D',_liteResizeBound=false,_liteSyncing=false,_litePointerInside=false;
let _liteMacdSoloHeight=176;
let _liteData=[],_liteVolumeData=[],_liteIndicatorSeries=[],_liteDataByTime=new Map(),_liteSessionRange=null,_liteUserInteractedZoom=false;
const LITE_RIGHT_OFFSET=22,LITE_HIST_SCALE=2.1;
// Lazy load lịch sử: khi user kéo trái đến đầu dữ liệu, tự động fetch thêm bar cũ hơn.
let _liteHasMore=true;         // còn lịch sử cũ phía trước chưa load (server báo)
let _liteLoadingMore=false;    // đang fetch lazy-load, tránh gọi chồng
let _liteOldestDate=null;      // date của bar đầu tiên đang có ('YYYY-MM-DD')
let _liteChartLoading=false;   // đang load chart lần đầu — block _liteFetchMoreHistory
// Cấu hình chung rightPriceScale (borderColor, minimumWidth) dùng cho cả 3 chart; chỉ scaleMargins/autoScale khác nhau nên để riêng.
const LITE_PRICE_SCALE_BASE={borderColor:'#dde3ee',minimumWidth:64};
// Resize khung 3 chart theo clientWidth/Height — dùng chung cho resize listener và _liteRelayoutViewport().
function _liteApplyChartSizes(){
  if(_liteChart&&DOM.liteChart)_liteChart.applyOptions({width:DOM.liteChart.clientWidth,height:DOM.liteChart.clientHeight});
  if(_liteRsiChart&&DOM.liteRsiChart)_liteRsiChart.applyOptions({width:DOM.liteRsiChart.clientWidth,height:DOM.liteRsiChart.clientHeight});
  if(_liteMacdChart&&DOM.liteMacdChart)_liteMacdChart.applyOptions({width:DOM.liteMacdChart.clientWidth,height:DOM.liteMacdChart.clientHeight});
}
function initLiteChart(){
  if(_liteChart||!DOM.liteChart||!window.LightweightCharts)return;
  const chartOpts={
    layout:{background:{type:'solid',color:'#fff'},textColor:'#111827'},
    grid:{vertLines:{color:'#eef2f7'},horzLines:{color:'#eef2f7'}},
    timeScale:{borderColor:'#dde3ee',rightOffset:LITE_RIGHT_OFFSET,shiftVisibleRangeOnNewBar:false},
    crosshair:{
      mode:LightweightCharts.CrosshairMode.Normal,
      vertLine:{visible:false,labelVisible:false},
      horzLine:{visible:false,labelVisible:false}
    }
  };
  _liteChart=LightweightCharts.createChart(DOM.liteChart,{
    ...chartOpts,width:DOM.liteChart.clientWidth,height:DOM.liteChart.clientHeight,
    rightPriceScale:{...LITE_PRICE_SCALE_BASE,scaleMargins:{top:.12,bottom:.22}},
    handleScale:{axisPressedMouseMove:{time:true,price:true}}
  });
  _liteRsiChart=LightweightCharts.createChart(DOM.liteRsiChart,{
    ...chartOpts,width:DOM.liteRsiChart.clientWidth,height:DOM.liteRsiChart.clientHeight,
    layout:{background:{type:'solid',color:'rgba(255,255,255,0)'},textColor:'#111827'},
    rightPriceScale:{...LITE_PRICE_SCALE_BASE,scaleMargins:{top:.04,bottom:.06}},
    handleScale:{axisPressedMouseMove:{time:false,price:true}}
  });
  _liteMacdChart=LightweightCharts.createChart(DOM.liteMacdChart,{
    ...chartOpts,width:DOM.liteMacdChart.clientWidth,height:DOM.liteMacdChart.clientHeight,
    rightPriceScale:{...LITE_PRICE_SCALE_BASE,scaleMargins:{top:.07,bottom:.10}},
    handleScale:{axisPressedMouseMove:{time:false,price:true}}
  });  _liteCandle=_liteChart.addCandlestickSeries({
    upColor:LITE_CANDLE_UP_COLOR,downColor:LITE_CANDLE_DOWN_COLOR,borderUpColor:LITE_CANDLE_UP_COLOR,
    borderDownColor:LITE_CANDLE_DOWN_COLOR,wickUpColor:LITE_CANDLE_UP_COLOR,wickDownColor:LITE_CANDLE_DOWN_COLOR
  });
  _liteVolume=_liteChart.addHistogramSeries({
    priceFormat:{type:'volume'},priceScaleId:'',lastValueVisible:false,priceLineVisible:false
  });
  _liteVolume.priceScale().applyOptions({scaleMargins:{top:.78,bottom:.007}});
  _liteMainWhite=_liteChart.addLineSeries({lineVisible:false,lastValueVisible:false,priceLineVisible:false,crosshairMarkerVisible:false});
  _liteRsiWhite=_liteRsiChart.addLineSeries({lineVisible:false,lastValueVisible:false,priceLineVisible:false,crosshairMarkerVisible:false});
  _liteMacdWhite=_liteMacdChart.addLineSeries({lineVisible:false,lastValueVisible:false,priceLineVisible:false,crosshairMarkerVisible:false});
  _liteChart.timeScale().subscribeVisibleLogicalRangeChange(range=>{
    redrawLiteDrawings();
    _liteSyncVisibleRangeFrom(_liteChart,range);
    if(_liteUserInteractedZoom&&!_liteChartLoading&&!_liteSyncing&&!_liteLoadingMore&&_liteData.length>0&&range&&Number.isFinite(range.from)&&Number.isFinite(range.to)){
      const last=_liteData.length-1,span=range.to-range.from;
      if(span>5&&span<2000)_liteSessionRange={span,offsetRight:range.to-last};
    }
    if(range&&range.from<=100&&_liteHasMore&&!_liteLoadingMore&&!_liteChartLoading){
      _liteFetchMoreHistory();
    }
  });
  _liteRsiChart.timeScale().subscribeVisibleLogicalRangeChange(range=>{
    _liteSyncVisibleRangeFrom(_liteRsiChart,range);
  });
  _liteMacdChart.timeScale().subscribeVisibleLogicalRangeChange(range=>{
    _liteSyncVisibleRangeFrom(_liteMacdChart,range);
  });
  // Crosshair hợp nhất (1 dọc+1 ngang) cho 2 panel: mỗi panel báo toạ độ cục bộ + offsetTop.
  function _liteHideXhair(){
    if(DOM.liteXhairV)DOM.liteXhairV.style.display='none';
    if(DOM.liteXhairH)DOM.liteXhairH.style.display='none';
    if(DOM.liteXhairPrice)DOM.liteXhairPrice.style.display='none';
    if(DOM.liteXhairTime)DOM.liteXhairTime.style.display='none';
  }
  function _liteMoveXhair(x,y,priceTxt,timeTxt){
    if(DOM.liteXhairV){DOM.liteXhairV.style.left=x+'px';DOM.liteXhairV.style.display='block';}
    if(DOM.liteXhairH){DOM.liteXhairH.style.top=y+'px';DOM.liteXhairH.style.display='block';}
    if(DOM.liteXhairPrice){
      DOM.liteXhairPrice.style.top=y+'px';
      DOM.liteXhairPrice.textContent=priceTxt;
      DOM.liteXhairPrice.style.display=priceTxt?'block':'none';
    }
    if(DOM.liteXhairTime){
      DOM.liteXhairTime.style.left=x+'px';
      DOM.liteXhairTime.textContent=timeTxt;
      DOM.liteXhairTime.style.display=timeTxt?'block':'none';
    }
  }
  function _liteCrosshairPriceTxt(series,localY){
    const price=series&&series.coordinateToPrice&&series.coordinateToPrice(localY);
    return Number.isFinite(price)?fmtLiteNum(price):'';
  }
  function _liteHandleCrosshairMove(param,domEl,priceSeries,isMain){
    if(isMain){
      const key=param&&param.time?liteTimeKey(param.time):'';
      const bar=key?_liteDataByTime.get(key):null;
      if(bar)updateLiteTitle(bar);else updateLiteTitle(_liteData[_liteData.length-1]);
      if(!param||!param.point){_liteHideXhair();return;}
      const x=param.point.x,y=(domEl.offsetTop||0)+param.point.y;
      const priceTxt=_liteCrosshairPriceTxt(priceSeries,param.point.y);
      const timeTxt=key?fmtLiteDate(key):'';
      _liteMoveXhair(x,y,priceTxt,timeTxt);
      return;
    }
    if(!param||!param.point){_liteHideXhair();return;}
    const key=param.time?liteTimeKey(param.time):'';
    const bar=key?_liteDataByTime.get(key):null;
    if(bar)updateLiteTitle(bar);
    const x=param.point.x,y=(domEl.offsetTop||0)+param.point.y;
    const priceTxt=_liteCrosshairPriceTxt(priceSeries,param.point.y);
    const timeTxt=key?fmtLiteDate(key):'';
    _liteMoveXhair(x,y,priceTxt,timeTxt);
  }
  _liteChart.subscribeCrosshairMove(param=>_liteHandleCrosshairMove(param,DOM.liteChart,_liteCandle,true));
  _liteMacdChart.subscribeCrosshairMove(param=>_liteHandleCrosshairMove(param,DOM.liteMacdChart,_liteMacdCrosshairSeries,false));
  _liteRsiChart.subscribeCrosshairMove(param=>_liteHandleCrosshairMove(param,DOM.liteRsiChart,_liteRsiCrosshairSeries,false));
  if(!_liteResizeBound){
    _liteResizeBound=true;
    // Debounce 150ms (giống health-chart, vnd-panel) — chỉ tính lại 1 lần sau khi ngừng resize.
    let _liteResizeTimer=null;
    window.addEventListener('resize',()=>{
      clearTimeout(_liteResizeTimer);
      _liteResizeTimer=setTimeout(()=>{
        // Popout cần _liteRelayoutViewport() (không chỉ resize canvas) để tính lại pane layout
        // khi kéo cạnh cửa sổ; trên mobile còn là lưới an toàn cho 'orientationchange'.
        if(_isChartPopoutWindow)_liteRelayoutViewport();
        else _liteApplyChartSizes();
        resizeLiteDrawCanvas();redrawLiteDrawings();
      },150);
    });
    // Vẽ lại canvas khi có nội dung cần vẽ và panel đang hiển thị.
    // Dùng document.hidden + offsetParent check để không tốn CPU khi tab ẩn hoặc panel thu gọn.
    let _liteDrawLoopActive=true;
    const _liteDrawLoop=()=>{
      if(!_liteDrawLoopActive)return;
      if(!document.hidden&&!_liteChartLoading&&_liteDrawCtx&&DOM.liteChartFrame&&DOM.liteChartFrame.offsetParent!==null
         &&(_liteDrawings.length||_liteDrawActive||_liteBBFillData||_liteTrendFillData||_liteBuyArrowData||(_liteHistorySignals&&_liteHistorySignals.length))){
        redrawLiteDrawings();
      }
      requestAnimationFrame(_liteDrawLoop);
    };
    requestAnimationFrame(_liteDrawLoop);
    document.addEventListener('visibilitychange',()=>{
      if(!document.hidden&&_liteDrawLoopActive)requestAnimationFrame(_liteDrawLoop);
    });
  }
}
// Cache checkbox theo value: querySelector CSS selector bị gọi hàng chục-hàng trăm lần/giây
// trong lúc kéo/zoom chart (mỗi lần đổi visible range đều gọi lại _litePaneIsActive→_liteChecked)
// và trong renderLiteIndicators/quiet-refresh — tra Map là O(1), rẻ hơn nhiều so với CSS attribute
// selector match lại toàn DOM mỗi lần. Checkbox là phần tử tĩnh (không tạo/huỷ động), kể cả khi bị
// "portal" ra <body> ở mobile portrait (xem syncLiteIndDropdownPortal) vẫn là CÙNG 1 node — chỉ đổi
// cha, không đổi định danh — nên cache theo tham chiếu phần tử vẫn đúng sau khi bị portal.
let _liteCheckedMap=null;
function _liteChecked(name){
  if(!_liteCheckedMap){
    _liteCheckedMap=new Map();
    _liteAllIndCheckboxes().forEach(cb=>{if(!_liteCheckedMap.has(cb.value))_liteCheckedMap.set(cb.value,cb);});
  }
  let cb=_liteCheckedMap.get(name);
  if(!cb){
    cb=document.querySelector(`input[value="${name}"]`);
    if(cb)_liteCheckedMap.set(name,cb);
  }
  return !!(cb&&cb.checked);
}
function _liteAllIndCheckboxes(){
  return document.querySelectorAll('#lite-indicators input[type="checkbox"], .lite-ind-dropdown input[type="checkbox"]');
}
function loadLiteIndicatorPrefs(){
  let prefs={};
  try{prefs=JSON.parse(localStorage.getItem(LITE_IND_KEY)||'{}')||{};}catch(e){prefs={};}
  const DEFAULT_ON_INDICATORS=new Set(['macd','signal','signalgrp_on','volcolor','ma10','ma200','ema20','ema50']);
  _liteAllIndCheckboxes().forEach(cb=>{
    cb.checked=DEFAULT_ON_INDICATORS.has(cb.value)?(prefs[cb.value]!==false):(prefs[cb.value]===true);
  });
  loadLiteIndColors();
}
function saveLiteIndicatorPrefs(){
  const prefs={};
  _liteAllIndCheckboxes().forEach(cb=>{prefs[cb.value]=cb.checked;});
  localStorage.setItem(LITE_IND_KEY,JSON.stringify(prefs));
}
function fmtLiteNum(v){
  return Number.isFinite(v)?Number(v).toFixed(2):'--';
}
function fmtLiteDate(t){
  if(typeof t==='number'){
    const d=new Date(t*1000);
    return `${String(d.getDate()).padStart(2,'0')}/${String(d.getMonth()+1).padStart(2,'0')}/${d.getFullYear()} ${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;
  }
  const p=liteTimeKey(t).split('-');
  return p.length===3?`${p[2]}/${p[1]}/${p[0]}`:(liteTimeKey(t)||'--');
}
function _liteTitleSegments(bar){
  if(!bar)return [];
  const tf=(_liteTf||'D').replace(/^1/,'');
  const pct=Number.isFinite(bar.pct)?bar.pct:0;
  const sign=pct>0?'+':'';
  const up=Number.isFinite(bar.close)&&Number.isFinite(bar.open)?bar.close>=bar.open:pct>=0;
  const col=up?LITE_CANDLE_UP_COLOR:LITE_CANDLE_DOWN_COLOR;
  const hlHtml=`<span class="lct-hl"><span style="color:#111827"> H:</span><span style="color:${col}">${fmtLiteNum(bar.high)}</span><span style="color:#111827"> L:</span><span style="color:${col}">${fmtLiteNum(bar.low)}</span></span>`;
  const openHtml=`<span class="lct-open"><span style="color:#111827"> O:</span><span style="color:${col}">${fmtLiteNum(bar.open)}</span></span>`;
  const segments=[
    {text:`${_liteSymbol} [${tf}] ${fmtLiteDate(bar.time)} |`,color:'#111827'},
    {text:openHtml,color:'__html',_open:fmtLiteNum(bar.open)},
    {text:hlHtml,color:'__html',_high:fmtLiteNum(bar.high),_low:fmtLiteNum(bar.low)},
    {text:' C:',color:'#111827'},
    {text:fmtLiteNum(bar.close),color:col},
    {text:' (',color:'#111827'},
    {text:`${sign}${pct.toFixed(2)}%`,color:col},
    {text:')',color:'#111827'}
  ];
  if(Number.isFinite(_liteRsScore))segments.push({text:' '+rsBadge(_liteRsScore),color:'__html',_rs:Math.round(_liteRsScore)});
  return segments;
}
let _liteVolForecast=null,_liteVolForecastReqId=0;
async function _liteFetchVolForecast(sym){
  const reqId=++_liteVolForecastReqId;
  try{
    const r=await fetch('/api/vol_forecast/'+encodeURIComponent(sym));
    const j=await r.json();
    if(reqId!==_liteVolForecastReqId||sym!==_liteSymbol)return;
    _liteVolForecast=(j&&!j.error)?j:null;
  }catch(e){
    if(reqId===_liteVolForecastReqId&&sym===_liteSymbol)_liteVolForecast=null;
  }
  if(reqId===_liteVolForecastReqId&&sym===_liteSymbol)updateLiteBigPrice(_liteData[_liteData.length-1]);
}
function updateLiteBigPrice(bar){
  const el=DOM.liteChartBigPrice;
  if(!el)return;
  if(!bar||!_liteChecked('signalgrp_on')||!_liteChecked('bigprice')){el.classList.remove('on');el.innerHTML='';return;}
  const pct=Number.isFinite(bar.pct)?bar.pct:0;
  const change=Number.isFinite(bar.close)&&Number.isFinite(bar.pct)&&pct!==0
    ?bar.close-bar.close/(1+pct/100):(Number.isFinite(bar.close)&&Number.isFinite(bar.open)?bar.close-bar.open:0);
  const up=Number.isFinite(bar.close)&&Number.isFinite(bar.open)?bar.close>=bar.open:pct>=0;
  const col=up?LITE_CANDLE_UP_COLOR:LITE_CANDLE_DOWN_COLOR;
  const sign=pct>0?'+':(pct<0?'':'');
  const fc=_liteVolForecast;
  const sameSym=fc&&fc.symbol===_liteSymbol;
  const progress=sameSym&&Number.isFinite(fc.progress)?fc.progress:null;
  const fmtEst=v=>(Number.isFinite(v)&&progress>0.001)?(v/progress).toFixed(2):'--';
  const fmtProgress=v=>Number.isFinite(v)?String(Math.round(v*100)/100):'--';
  const ratioPrev=sameSym?fc.ratio_prev:null;
  const ratioMA50=sameSym?fc.ratio_ma50:null;
  el.classList.add('on');
  el.innerHTML=
    `<span class="bp-price" style="color:${col}">${fmtLiteNum(bar.close)}</span>`+
    `<span class="bp-sub" style="color:${col}">${sign}${fmtLiteNum(change)}(${sign}${pct.toFixed(2)}%)--(${fmtEst(ratioPrev)}-${fmtEst(ratioMA50)}/${fmtProgress(progress)})</span>`;
}
function _liteCleanSym(v){
  return String(v||'')
    .normalize('NFD').replace(/[\u0300-\u036f]/g,'')
    .replace(/[đĐ]/g,'d')
    .toUpperCase().replace(/[^A-Z0-9]/g,'');
}
function _liteBindSymInput(el){
  if(!el)return ()=>'';
  function _apply(){
    const raw=_liteCleanSym(el.value);
    if(el.value!==raw)el.value=raw;
    return raw;
  }
  el.addEventListener('blur',_apply);
  return _apply;
}
function _liteFutureTimes(lastTimeStr,n,tf){
  const out=[];
  let stepDays = 1;
  if(tf==='1W'||tf==='W') stepDays=7;
  else if(tf==='1M'||tf==='M') stepDays=30;
  let d=new Date(lastTimeStr+'T00:00:00Z'),added=0,guard=0;
  while(added<n&&guard<n*4){
    guard++;
    d=new Date(d.getTime()+stepDays*86400000);
    if(stepDays===1){const wd=d.getUTCDay();if(wd===0||wd===6)continue;}
    out.push(d.toISOString().slice(0,10));added++;
  }
  return out;
}
let _liteWhiteLastKey='';
function _liteUpdateWhitespace(force){
  if(!_liteData.length)return;
  const lastT=liteTimeKey(_liteData[_liteData.length-1].time);
  const key=lastT+'|'+_liteTf;
  if(!force&&key===_liteWhiteLastKey)return; // nến cuối + TF không đổi → skip
  _liteWhiteLastKey=key;
  const future=_liteFutureTimes(lastT,LITE_RIGHT_OFFSET+10,_liteTf).map(t=>({time:t}));
  if(_liteMainWhite)_liteMainWhite.setData(future);
  if(_liteRsiWhite)_liteRsiWhite.setData(future);
  if(_liteMacdWhite)_liteMacdWhite.setData(future);
}
function liteTimeKey(t){
  if(typeof t==='string')return t;
  if(typeof t==='number')return String(t);
  if(t&&typeof t==='object'&&'year'in t&&'month'in t&&'day'in t){
    return `${t.year}-${String(t.month).padStart(2,'0')}-${String(t.day).padStart(2,'0')}`;
  }
  return String(t||'');
}
function updateLiteTitle(bar){
  if(!DOM.liteChartTitle||!bar)return;
  DOM.liteChartTitle.innerHTML=_liteTitleSegments(bar).map(seg=>
    seg.color==='__html'?seg.text:
    seg.color==='#111827'?seg.text:`<span style="color:${seg.color}">${seg.text}</span>`
  ).join('');
}
let _liteHistorySignals=[], _liteCurrentSignal=null;
function _liteApplyBuySignal(sigOverride){
  if(!_liteCandle||!_liteData.length)return;
  if(sigOverride!==undefined)_liteCurrentSignal=sigOverride;
  const sig=(_liteChecked('signalgrp_on')&&_liteChecked('signal'))?_liteCurrentSignal:null;
  if(sig){
    let arrowColor='#9333ea';
    if(DOM.liteChartSignal){
      DOM.liteChartSignal.innerHTML=`<span class="s-emoji">${sig.emoji||'📌'}</span><span class="s-badge ${BADGE_MAP[sig.signal]||'b-MACROSS'}">${signalLabel(sig.signal)}</span>`;
      DOM.liteChartSignal.classList.add('on');
      const badgeEl=DOM.liteChartSignal.querySelector('.s-badge');
      if(badgeEl)arrowColor=getComputedStyle(badgeEl).color||arrowColor;
    }
    _liteBuyArrowData={color:arrowColor};
  }else{
    _liteBuyArrowData=null;
    if(DOM.liteChartSignal){DOM.liteChartSignal.classList.remove('on');DOM.liteChartSignal.innerHTML='';}
  }
}
function setLiteRightOffset(){
  if(!_liteData.length||!_liteChart)return;
  const last=_liteData.length-1;
  if(_liteSessionRange&&Number.isFinite(_liteSessionRange.span)&&_liteSessionRange.span>0){
    const to=last+_liteSessionRange.offsetRight;
    const from=to-_liteSessionRange.span;
    _liteApplyVisibleLogicalRange({from,to});
    return;
  }
  const rightOffset=LITE_RIGHT_OFFSET;
  const to=last+rightOffset;
  // Mobile portrait (~390px): 80 nến đủ rõ từng cây nến; landscape/desktop: 250 như cũ
  const visibleCount=IS_MOBILE()&&window.innerHeight>window.innerWidth?80:250;
  const from=Math.max(0,last-visibleCount+1);
  _liteApplyVisibleLogicalRange({from,to});
}
function setLiteTf(tf){
  _liteTf=tf || '1D';
  DOM.liteChartTf?.querySelectorAll('.lite-tf-btn').forEach(btn=>btn.classList.toggle('on',btn.dataset.tf===_liteTf));
}
function applyLiteTf(tf,force=false){
  if(!tf)return false;
  if(!force&&_liteTf===tf)return true;
  _liteSessionRange=null;
  _liteUserInteractedZoom=false;
  setLiteTf(tf);
  loadLiteChart(_liteSymbol,0);
  return true;
}
function _clearLiteIndicators(){
  for(const s of _liteIndicatorSeries){
    try{s.chart.removeSeries(s.series);}catch(e){}
  }
  _liteIndicatorSeries=[];
  _liteRsiCrosshairSeries=null;
  _liteMacdCrosshairSeries=null;
  _liteBBFillData=null;
  _liteTrendFillData=null;
  _liteBuyArrowData=null; // tránh mũi tên của mã cũ chớp lên sai vị trí trước khi _liteApplyBuySignal() chạy lại cho mã mới
}
function _sma(data,n){
  const out=[];let sum=0;
  for(let i=0;i<data.length;i++){
    sum+=data[i].close;if(i>=n)sum-=data[i-n].close;
    if(i>=n-1)out.push({time:data[i].time,value:sum/n});
  }
  return out;
}
function _ema(data,n){
  const out=[];let e=null,k=2/(n+1);
  for(let i=0;i<data.length;i++){
    const c=data[i].close;e=e===null?c:c*k+e*(1-k);
    if(i>=n-1)out.push({time:data[i].time,value:e});
  }
  return out;
}
function _bbands(data,n=20,mult=2){
  // Sliding window O(n): dùng online variance thay vì slice() O(n*period)
  const mid=_sma(data,n);
  const upper=[],lower=[];
  let sumSq=0,winSum=0;
  for(let i=0;i<data.length;i++){
    const c=data[i].close;
    winSum+=c;sumSq+=c*c;
    if(i>=n){const old=data[i-n].close;winSum-=old;sumSq-=old*old;}
    if(i<n-1)continue;
    const m=winSum/n;
    const sd=Math.sqrt(Math.max(0,sumSq/n-m*m));
    upper.push({time:data[i].time,value:m+mult*sd});
    lower.push({time:data[i].time,value:m-mult*sd});
  }
  return{upper,mid,lower};
}
function _rsi(data,n=LITE_RSI_PERIOD){
  if(!data||data.length<=n)return [];
  const out=[];
  let gainSum=0,lossSum=0;
  for(let i=1;i<=n;i++){
    const delta=(data[i]?.close||0)-(data[i-1]?.close||0);
    gainSum+=Math.max(delta,0);
    lossSum+=Math.max(-delta,0);
  }
  let avgGain=gainSum/n,avgLoss=lossSum/n;
  const firstRs=avgLoss===0?Infinity:(avgGain/avgLoss);
  out.push({time:data[n].time,value:avgLoss===0?100:(100-(100/(1+firstRs)))});
  for(let i=n+1;i<data.length;i++){
    const delta=data[i].close-data[i-1].close;
    const gain=Math.max(delta,0),loss=Math.max(-delta,0);
    avgGain=((avgGain*(n-1))+gain)/n;
    avgLoss=((avgLoss*(n-1))+loss)/n;
    const rs=avgLoss===0?Infinity:(avgGain/avgLoss);
    out.push({time:data[i].time,value:avgLoss===0?100:(100-(100/(1+rs)))});
  }
  return out;
}
function _macd(data){
  const e12=_ema(data,12),e26=_ema(data,26),byTime=new Map(e12.map(x=>[x.time,x.value]));
  const macd=e26.map(x=>({time:x.time,value:(byTime.get(x.time)||0)-x.value}));
  const signal=_ema(macd.map(x=>({time:x.time,close:x.value})),9);
  const sigMap=new Map(signal.map(x=>[x.time,x.value]));
  const histRaw=macd.filter(x=>sigMap.has(x.time)).map(x=>({time:x.time,value:x.value-sigMap.get(x.time)}));
  const hist=histRaw.map((x,i)=>{
    const prev=i>0?histRaw[i-1].value:x.value;
    const color=x.value>=0?(x.value>=prev?'#26a69a':'#b2dfdb'):(x.value<=prev?'#ef5350':'#ffcdd2');
    return{...x,color};
  });
  return{macd,signal,hist};
}
// ═══ TREND (Trailing Stop/Reverse kiểu NRTR) ═══ mult=hệ số biên độ đảo chiều,
// period=chu kỳ WMA H-L; mode 'smoothed' dùng Heikin Ashi theo AFL gốc.
const LITE_TREND_MULT=1.75, LITE_TREND_PERIOD=10;
function _wma(values,n){
  // Sliding window O(n): i<n dùng trọng số tăng dần 1..i+1 (cửa sổ chưa đầy);
  // i>=n phần tử mới trọng số n, trừ lSum (tổng cửa sổ CŨ) và loại values[i-n].
  const out=new Array(values.length).fill(null);
  const denom=n*(n+1)/2;
  let wSum=0,lSum=0;
  for(let i=0;i<values.length;i++){
    if(i<n){
      wSum+=values[i]*(i+1);
      lSum+=values[i];
    }else{
      wSum+=values[i]*n-lSum;
      lSum+=values[i]-values[i-n];
    }
    if(i>=n-1)out[i]=wSum/denom;
  }
  return out;
}
function _heikinAshi(data){
  // HaOpen[i] = AMA(Ref(HaClose,-1), 0.5) = (HaOpen[i-1]+HaClose[i-1])/2 — đúng công thức trong AFL gốc.
  const out=[];
  let prevHaOpen=null,prevHaClose=null;
  for(let i=0;i<data.length;i++){
    const o=data[i].open,h=data[i].high,l=data[i].low,c=data[i].close;
    const haClose=(o+h+l+c)/4;
    const haOpen=(prevHaOpen==null)?(o+c)/2:(prevHaOpen+prevHaClose)/2;
    const haHigh=Math.max(h,haOpen,haClose);
    const haLow=Math.min(l,haOpen,haClose);
    out.push({haOpen,haHigh,haLow,haClose});
    prevHaOpen=haOpen;prevHaClose=haClose;
  }
  return out;
}
function _trendNRTR(data,period=LITE_TREND_PERIOD,mult=LITE_TREND_MULT,mode='regular'){
  const n=data.length;
  let nm,j;
  if(mode==='smoothed'){
    const ha=_heikinAshi(data);
    nm=ha.map(b=>b.haHigh-b.haLow);
    j=ha.map(b=>(b.haOpen+b.haHigh+b.haLow+b.haClose)/4);
  }else{
    nm=data.map(b=>b.high-b.low);
    j=data.map(b=>b.close);
  }
  const wma=_wma(nm,period);
  const trend=new Array(n).fill(1);
  const nw=new Array(n).fill(null);
  let started=false;
  for(let i=0;i<n;i++){
    if(wma[i]==null)continue;
    const rev=mult*wma[i];
    const jj=j[i];
    if(!started){
      trend[i]=1;nw[i]=jj-rev;started=true;continue;
    }
    const prevTrend=trend[i-1],prevNw=nw[i-1]!=null?nw[i-1]:(jj-rev);
    if(prevTrend===1){
      if(jj<prevNw){trend[i]=-1;nw[i]=jj+rev;}
      else{trend[i]=1;nw[i]=Math.max(jj-rev,prevNw);}
    }else{
      if(jj>prevNw){trend[i]=1;nw[i]=jj-rev;}
      else{trend[i]=-1;nw[i]=Math.min(jj+rev,prevNw);}
    }
  }
  return data.map((b,i)=>({time:b.time,value:nw[i],trend:trend[i]}));
}
function _trendCloudData(data,period=LITE_TREND_PERIOD,mult=LITE_TREND_MULT,mode='regular'){
  // Vùng tô nằm giữa đường trailing-stop (NW) và giá đóng cửa thực: xanh khi đang tăng, hồng khi đang giảm.
  const t=_trendNRTR(data,period,mult,mode);
  const out=[];
  for(let i=0;i<t.length;i++){
    if(t[i].value==null)continue;
    const close=data[i].close;
    out.push({time:t[i].time,top:Math.max(t[i].value,close),bottom:Math.min(t[i].value,close),trend:t[i].trend});
  }
  return out;
}
function alignLiteSeries(points){
  // Tạo Map 1 lần từ points (thường nhỏ hơn _liteData do warmup); lookup theo time key của _liteData.
  const n=points.length;
  if(!n)return _liteData.map(bar=>({time:bar.time}));
  const byTime=new Map();
  for(let i=0;i<n;i++){const p=points[i];byTime.set(liteTimeKey(p.time),p);}
  return _liteData.map(bar=>byTime.get(liteTimeKey(bar.time))||{time:bar.time});
}
function applyLitePaneLayout(skipWidthSync){
  const showRsi=_liteChecked('rsi');
  const showMacd=_liteChecked('macd');
  const isPortraitMobile=IS_MOBILE()&&window.innerHeight>window.innerWidth;
  const totalH=Math.max(300,(DOM.liteChartFrame&&DOM.liteChartFrame.clientHeight)||720);
  const bothPanes=showRsi&&showMacd;
  const compactPaneH=132;
  const rsiH=showRsi?(bothPanes?compactPaneH:176):0;
  const macdH=showMacd?(bothPanes?compactPaneH:_liteMacdSoloHeight):0;
  const splitterH=showMacd?4:0;
  const lowerH=rsiH+macdH+splitterH;
  const mobilePortrait=isPortraitMobile&&!_isChartPopoutWindow;
  const mainH=mobilePortrait
    ?Math.max(300,Math.round(window.innerHeight*0.56))
    :(showRsi||showMacd?Math.max(300,totalH-lowerH):totalH);
  const showMainTimeScale=!showRsi&&!showMacd;
  const showRsiTimeScale=showRsi&&!showMacd;
  const showMacdTimeScale=showMacd;
  const prevSyncing=_liteSyncing;
  _liteSyncing=true;
  try{
    DOM.liteRsiChart.style.display=showRsi?'block':'none';
    DOM.liteMacdChart.style.display=showMacd?'block':'none';
    DOM.liteMacdResizer.classList.toggle('on',showMacd&&!showRsi);
    DOM.liteChart.classList.toggle('hide-tv-logo',showRsi||showMacd);
    DOM.liteRsiChart.classList.toggle('hide-tv-logo',showRsi&&showMacd);
    DOM.liteMacdChart.classList.remove('hide-tv-logo');
    if(mobilePortrait&&DOM.liteChartFrame)DOM.liteChartFrame.style.height=`${mainH+lowerH}px`;
    else if(DOM.liteChartFrame)DOM.liteChartFrame.style.height='';
    DOM.liteChart.style.height=`${mainH}px`;
    if(showRsi)DOM.liteRsiChart.style.height=`${rsiH}px`;
    if(showMacd)DOM.liteMacdChart.style.height=`${macdH}px`;
    _liteChart.applyOptions({
      width:DOM.liteChart.clientWidth,height:DOM.liteChart.clientHeight,
      timeScale:{visible:showMainTimeScale,rightOffset:LITE_RIGHT_OFFSET},
      rightPriceScale:{...LITE_PRICE_SCALE_BASE,autoScale:true,scaleMargins:{top:.12,bottom:.18}}
    });
    if(_liteRsiChart)_liteRsiChart.applyOptions({
      width:DOM.liteRsiChart.clientWidth,height:DOM.liteRsiChart.clientHeight,
      timeScale:{visible:showRsiTimeScale,rightOffset:LITE_RIGHT_OFFSET},
      rightPriceScale:{...LITE_PRICE_SCALE_BASE,autoScale:true,scaleMargins:{top:.04,bottom:.06}}
    });
    if(_liteMacdChart)_liteMacdChart.applyOptions({
      width:DOM.liteMacdChart.clientWidth,height:DOM.liteMacdChart.clientHeight,
      timeScale:{visible:showMacdTimeScale,rightOffset:LITE_RIGHT_OFFSET},
      rightPriceScale:{...LITE_PRICE_SCALE_BASE,scaleMargins:{top:.07,bottom:.10}}
    });
  }finally{
    _liteSyncing=prevSyncing;
  }
  resizeLiteDrawCanvas();redrawLiteDrawings();
  if(!skipWidthSync)_liteSyncPriceScaleWidths();
}
function _liteSyncPriceScaleWidths(){
  if(!_liteChart||!_liteRsiChart||!_liteMacdChart)return;
  requestAnimationFrame(()=>{
    try{
      const w1=_liteChart.priceScale('right').width();
      const w2=_liteRsiChart.priceScale('right').width();
      const w3=_liteMacdChart.priceScale('right').width();
      const maxW=Math.max(LITE_PRICE_SCALE_BASE.minimumWidth,w1||0,w2||0,w3||0);
      _liteChart.priceScale('right').applyOptions({minimumWidth:maxW});
      _liteRsiChart.priceScale('right').applyOptions({minimumWidth:maxW});
      _liteMacdChart.priceScale('right').applyOptions({minimumWidth:maxW});
    }catch(e){}
  });
}
const LITE_DRAW_KEY='dashboard_lite_drawings';
const LITE_DRAW_COLOR_KEY='dashboard_lite_draw_color';
const LITE_TEXT_SIZE_KEY='dashboard_lite_text_size';
const LITE_TEXT_FONT_KEY='dashboard_lite_text_font';
const LITE_TEXT_BG_KEY='dashboard_lite_text_bg';
const LITE_HIT_TOL=7;
const LITE_TEXT_FONT_CSS={mono:'"IBM Plex Mono",monospace',sans:'"Inter",Arial,sans-serif',serif:'Georgia,"Times New Roman",serif'};
let _liteDrawTool='cursor',_liteDrawings=[],_liteDrawActive=null,_liteDrawCtx=null,_liteDrawSeq=1;
let _liteDrawColor='#1a56db',_liteSelectedId=null,_liteDragInfo=null,_liteChannelPending=null,_liteLinePending=null;
let _liteArcPending=null,_liteZigzagPending=null,_liteTextEditPos=null,_liteTextEditId=null;
let _liteTextSize=13,_liteTextFont='mono',_liteTextBg='';
function _liteTextFontCSS(sizePx,familyKey){return sizePx+'px '+(LITE_TEXT_FONT_CSS[familyKey]||LITE_TEXT_FONT_CSS.mono);}
function _liteTextLineHeight(sizePx){return Math.round(sizePx*1.35);}
function _liteTextBoxMetrics(d){
  if(!_liteDrawCtx)return null;
  const size=d.fontSize||13,family=d.fontFamily||'mono',pad=4;
  const lines=(d.text||'').split('\n');
  const lh=_liteTextLineHeight(size);
  _liteDrawCtx.save();
  _liteDrawCtx.font=_liteTextFontCSS(size,family);
  let maxW=0;
  for(const line of lines)maxW=Math.max(maxW,_liteDrawCtx.measureText(line||' ').width);
  _liteDrawCtx.restore();
  return{pad,lh,size,family,lines,width:maxW+pad*2,height:lines.length*lh+pad*2};
}
function _liteDrawStoreKey(){return LITE_DRAW_KEY+':'+_liteSymbol;}
function loadLiteDrawings(){
  try{
    let raw=localStorage.getItem(_liteDrawStoreKey());
    if(!raw){
      raw=localStorage.getItem(LITE_DRAW_KEY+':'+_liteSymbol+':'+_liteTf)
        ||localStorage.getItem(LITE_DRAW_KEY+':'+_liteSymbol+':1D')
        ||localStorage.getItem(LITE_DRAW_KEY+':'+_liteSymbol+':D');
    }
    _liteDrawings=JSON.parse(raw||'[]')||[];
    if(_liteDrawings&&_liteData&&_liteData.length){
      for(const d of _liteDrawings){
        if(d&&d.points){
          d.points=d.points.map(pt=>{
            if(!pt||'offsetPrice' in pt)return pt;
            if(!pt.ts&&Number.isFinite(pt.l))return _litePtWithTime(pt.l,pt.p);
            return pt;
          });
        }
      }
    }
  }catch(e){_liteDrawings=[];}
  _liteSelectedId=null;_liteChannelPending=null;_liteArcPending=null;_liteZigzagPending=null;_liteLinePending=null;_liteDrawActive=null;
}
function saveLiteDrawings(){
  if(_liteDrawings&&_liteData&&_liteData.length){
    for(const d of _liteDrawings){
      if(d&&d.points){
        d.points=d.points.map(pt=>{
          if(!pt)return pt;
          if('offsetPrice' in pt)return pt;
          if(!pt.ts&&Number.isFinite(pt.l))return _litePtWithTime(pt.l,pt.p);
          return pt;
        });
      }
    }
  }
  _liteLSSet(_liteDrawStoreKey(),JSON.stringify(_liteDrawings));
  _chartSyncChannel?.postMessage({type:'CHART_DRAW_SYNC',sym:_liteSymbol});
}
function resizeLiteDrawCanvas(){
  if(!DOM.liteDrawCanvas||!DOM.liteChart)return;
  const w=DOM.liteChart.clientWidth,h=DOM.liteChart.clientHeight,dpr=window.devicePixelRatio||1;
  DOM.liteDrawCanvas.style.width=w+'px';DOM.liteDrawCanvas.style.height=h+'px';
  DOM.liteDrawCanvas.width=Math.max(1,Math.round(w*dpr));DOM.liteDrawCanvas.height=Math.max(1,Math.round(h*dpr));
  _liteDrawCtx=DOM.liteDrawCanvas.getContext('2d');
  _liteDrawCtx.setTransform(dpr,0,0,dpr,0,0);
}
function _litePtWithTime(l,p){
  if(l===null||l===undefined||!Number.isFinite(l))return{l:0,p:p||0,ts:null};
  let ts=null,futureOffset=null,futureTf=null;
  if(_liteData&&_liteData.length){
    const lastIdx=_liteData.length-1;
    if(_liteData.length===1){
      ts=new Date(liteTimeKey(_liteData[0].time)+'T00:00:00Z').getTime();
    }else if(l<=0){
      const t0=new Date(liteTimeKey(_liteData[0].time)+'T00:00:00Z').getTime(),t1=new Date(liteTimeKey(_liteData[1].time)+'T00:00:00Z').getTime();
      ts=t0+l*(t1-t0);
    }else if(l>lastIdx+0.4){
      futureOffset=l-lastIdx;futureTf=_liteTf;
      const tLast=new Date(liteTimeKey(_liteData[lastIdx].time)+'T00:00:00Z').getTime(),tPrev=new Date(liteTimeKey(_liteData[lastIdx-1].time)+'T00:00:00Z').getTime();
      ts=tLast+(l-lastIdx)*(tLast-tPrev);
    }else if(l>=lastIdx-0.4){
      ts=new Date(liteTimeKey(_liteData[lastIdx].time)+'T00:00:00Z').getTime();
    }else{
      const b=Math.floor(l),f=l-b;
      const t1=new Date(liteTimeKey(_liteData[b].time)+'T00:00:00Z').getTime(),t2=new Date(liteTimeKey(_liteData[b+1].time)+'T00:00:00Z').getTime();
      ts=t1+f*(t2-t1);
    }
  }
  return{l,p,ts,futureOffset,futureTf,origTf:(_liteTf||'1D').toUpperCase()};
}
function _litePtLogical(pt){
  if(!pt)return null;
  if(typeof pt==='number')return pt;
  if(!_liteData||!_liteData.length)return pt.l;
  const lastIdx=_liteData.length-1;
  if(Number.isFinite(pt.futureOffset)&&pt.futureOffset>0.4)return(pt.futureTf===_liteTf)?lastIdx+pt.futureOffset:lastIdx;
  const targetTs=pt.ts;
  if(!targetTs)return pt.l;
  if(lastIdx===0)return 0;
  const t0=new Date(liteTimeKey(_liteData[0].time)+'T00:00:00Z').getTime();
  if(targetTs<=t0)return (targetTs-t0)/(new Date(liteTimeKey(_liteData[1].time)+'T00:00:00Z').getTime()-t0);
  const tLast=new Date(liteTimeKey(_liteData[lastIdx].time)+'T00:00:00Z').getTime();
  if(targetTs>=tLast)return lastIdx+(targetTs-tLast)/(tLast-new Date(liteTimeKey(_liteData[lastIdx-1].time)+'T00:00:00Z').getTime());

  const tf=(_liteTf||'1D').toUpperCase(),targetDate=new Date(targetTs),tY=targetDate.getUTCFullYear(),tM=targetDate.getUTCMonth();
  if(tf==='1M'||tf==='M'||tf==='MONTH'||tf==='MONTHLY'){
    for(let i=0;i<_liteData.length;i++){const d=new Date(liteTimeKey(_liteData[i].time)+'T00:00:00Z');if(d.getUTCFullYear()===tY&&d.getUTCMonth()===tM)return i;}
  }
  const isSubW=(tf==='1W'||tf==='W')&&(pt.origTf==='1M'||targetDate.getUTCDate()===1);
  const isSubD=(tf==='1D'||tf==='D')&&(pt.origTf==='1M'||pt.origTf==='1W'||targetDate.getUTCDate()===1||targetDate.getUTCDay()===1);
  if(isSubW||isSubD){
    const matchFn=(tf==='1W'||tf==='W'||pt.origTf==='1M'||targetDate.getUTCDate()===1)?d=>(d.getUTCFullYear()===tY&&d.getUTCMonth()===tM):d=>Math.abs(targetTs-d.getTime())<4.0*86400000;
    let bestIdx=null,minDist=Infinity;
    for(let i=0;i<_liteData.length;i++){
      const bar=_liteData[i],d=new Date(liteTimeKey(bar.time)+'T00:00:00Z');
      if(!matchFn(d))continue;
      const p=pt.p||0,dist=Math.min(Math.abs((bar.high||p)-p),Math.abs((bar.low||p)-p),Math.abs((bar.close||p)-p));
      if(dist<minDist){minDist=dist;bestIdx=i;}
    }
    if(bestIdx!==null)return bestIdx;
  }
  if(tf==='1W'||tf==='W'){
    for(let i=0;i<_liteData.length;i++){if(Math.abs(targetTs-new Date(liteTimeKey(_liteData[i].time)+'T00:00:00Z').getTime())<4.0*86400000)return i;}
  }
  const idx=_liteData.findIndex(b=>new Date(liteTimeKey(b.time)+'T00:00:00Z').getTime()>=targetTs);
  if(idx===0)return 0;
  if(idx>0){
    const t1=new Date(liteTimeKey(_liteData[idx-1].time)+'T00:00:00Z').getTime(),t2=new Date(liteTimeKey(_liteData[idx].time)+'T00:00:00Z').getTime();
    return (idx-1)+(targetTs-t1)/(t2-t1);
  }
  return pt.l;
}
function _liteLogicalToX(pt){
  const l=(typeof pt==='object'&&pt!==null)?_litePtLogical(pt):pt;
  const c=_liteChart&&_liteChart.timeScale().logicalToCoordinate(l);
  return Number.isFinite(c)?c:null;
}
function _liteXToLogical(x){
  const l=_liteChart&&_liteChart.timeScale().coordinateToLogical(x);
  return Number.isFinite(l)?l:null;
}
function _litePriceToY(p){
  const c=_liteCandle&&_liteCandle.priceToCoordinate(p);
  return Number.isFinite(c)?c:null;
}
function _liteYToPrice(y){
  const p=_liteCandle&&_liteCandle.coordinateToPrice(y);
  return Number.isFinite(p)?p:null;
}
function _liteXYFromEvent(ev){
  const rect=DOM.liteDrawCanvas.getBoundingClientRect();
  return{x:ev.clientX-rect.left,y:ev.clientY-rect.top};
}
function _litePtFromEvent(ev){
  const{x,y}=_liteXYFromEvent(ev);
  const l=_liteXToLogical(x),p=_liteYToPrice(y);
  if(l===null||p===null)return null;
  return _litePtWithTime(l,p);
}
function _liteDrawLine(ctx,x1,y1,x2,y2,color,dash,width){
  ctx.save();ctx.strokeStyle=color;ctx.lineWidth=width||1.4;if(dash)ctx.setLineDash(dash);
  ctx.beginPath();ctx.moveTo(x1,y1);ctx.lineTo(x2,y2);ctx.stroke();ctx.restore();
}
function _liteDrawHandle(ctx,x,y){
  if(x===null||y===null)return;
  ctx.save();ctx.fillStyle='#fff';ctx.strokeStyle='#1a56db';ctx.lineWidth=1.3;
  ctx.beginPath();ctx.arc(x,y,4,0,Math.PI*2);ctx.fill();ctx.stroke();ctx.restore();
}
function _liteDrawCornerHandles(ctx,x1,y1,x2,y2){
  _liteDrawHandle(ctx,x1,y1);_liteDrawHandle(ctx,x2,y2);
  _liteDrawHandle(ctx,x1,y2);_liteDrawHandle(ctx,x2,y1);
}
function _liteDrawHandlePair(ctx,rx,rw,y){
  _liteDrawHandle(ctx,rx,y);_liteDrawHandle(ctx,rx+rw,y);
}
function _liteChannelOffset(d){
  const pts=d.points;
  return(pts[2]&&Number.isFinite(pts[2].offsetPrice))?pts[2].offsetPrice:(Math.abs(pts[1].p-pts[0].p)||pts[0].p*0.02||1);
}
function _liteArcControlXY(x1,y1,x2,y2,tx,ty){
  if(!Number.isFinite(tx)||!Number.isFinite(ty))return null;
  const midX=(x1+x2)/2,midY=(y1+y2)/2;
  return{cx:2*tx-midX,cy:2*ty-midY};
}
function _liteQuadDist(px,py,x1,y1,cx,cy,x2,y2){
  let min=Infinity;
  for(let i=0;i<=20;i++){
    const t=i/20,mt=1-t;
    const bx=mt*mt*x1+2*mt*t*cx+t*t*x2,by=mt*mt*y1+2*mt*t*cy+t*t*y2;
    const dist=Math.hypot(px-bx,py-by);
    if(dist<min)min=dist;
  }
  return min;
}
function _liteDrawWideArrow(ctx,x1,y1,x2,y2,color,widthScale){
  const dx=x2-x1,dy=y2-y1,len=Math.hypot(dx,dy);
  if(len<1e-3)return;
  const ws=Number.isFinite(widthScale)?widthScale/2:1;
  const ux=dx/len,uy=dy/len,px=-uy,py=ux;
  const headLen=Math.max(10,Math.min(len*.42,32));
  const shaftW=Math.max(2,Math.min(len*.09,7))*ws;
  const headW=Math.max(10,Math.min(len*.34,26))*ws;
  const baseT=Math.max(0,len-headLen);
  const bx=x1+ux*baseT,by=y1+uy*baseT;
  ctx.save();
  ctx.beginPath();
  ctx.moveTo(x1,y1);
  ctx.lineTo(bx+px*shaftW/2,by+py*shaftW/2);
  ctx.lineTo(bx+px*headW/2,by+py*headW/2);
  ctx.lineTo(x2,y2);
  ctx.lineTo(bx-px*headW/2,by-py*headW/2);
  ctx.lineTo(bx-px*shaftW/2,by-py*shaftW/2);
  ctx.closePath();
  ctx.fillStyle=color;
  ctx.fill();
  ctx.restore();
}
function _liteDrawShapeToCanvas(ctx,d){
  const pts=d.points,selected=(d.id===_liteSelectedId);
  if(d.type==='text'){
    const x=_liteLogicalToX(pts[0]),y=_litePriceToY(pts[0].p);
    if(x===null||y===null)return;
    const m=_liteTextBoxMetrics(d);if(!m)return;
    ctx.save();
    if(d.bg){ctx.fillStyle=d.bg;ctx.fillRect(x,y,m.width,m.height);}
    ctx.font=_liteTextFontCSS(m.size,m.family);
    ctx.textBaseline='top';
    ctx.fillStyle=d.color||'#111827';
    for(let i=0;i<m.lines.length;i++)ctx.fillText(m.lines[i],x+m.pad,y+m.pad+i*m.lh);
    if(selected){
      ctx.strokeStyle='#1a56db';ctx.setLineDash([3,3]);ctx.lineWidth=1;
      ctx.strokeRect(x+.5,y+.5,m.width,m.height);
    }
    ctx.restore();
    if(selected)_liteDrawHandle(ctx,x,y);
    return;
  }
  if(d.type==='zigzag'){
    const color=d.color||_liteDrawColor;
    if(pts.length){
      const scr=[];
      for(const pt of pts){
        const xx=_liteLogicalToX(pt),yy=_litePriceToY(pt.p);
        if(xx!==null&&yy!==null)scr.push({x:xx,y:yy});
      }
      if(scr.length>=3&&!d.noFill){
        ctx.save();
        ctx.beginPath();
        ctx.moveTo(scr[0].x,scr[0].y);
        for(let i=1;i<scr.length;i++)ctx.lineTo(scr[i].x,scr[i].y);
        ctx.closePath();
        ctx.fillStyle=_liteHexAlpha(color,.12);
        ctx.fill();
        ctx.restore();
      }
      ctx.save();ctx.strokeStyle=color;ctx.lineWidth=selected?2:1.4;ctx.lineJoin='round';
      ctx.beginPath();
      let started=false;
      for(const p of scr){
        if(!started){ctx.moveTo(p.x,p.y);started=true;}else ctx.lineTo(p.x,p.y);
      }
      if(started)ctx.stroke();
      ctx.restore();
      if(d._hover&&pts.length){
        const last=pts[pts.length-1];
        const lx=_liteLogicalToX(last),ly=_litePriceToY(last.p);
        const hx=_liteLogicalToX(d._hover),hy=_litePriceToY(d._hover.p);
        if(lx!==null&&ly!==null&&hx!==null&&hy!==null)_liteDrawLine(ctx,lx,ly,hx,hy,_liteHexAlpha(color,.5),[4,3]);
      }
      if(selected)for(const pt of pts){const xx=_liteLogicalToX(pt),yy=_litePriceToY(pt.p);if(xx!==null&&yy!==null)_liteDrawHandle(ctx,xx,yy);}
    }
    return;
  }
  if(pts.length<2)return;
  const x1=_liteLogicalToX(pts[0]),y1=_litePriceToY(pts[0].p);
  const x2=_liteLogicalToX(pts[1]),y2=_litePriceToY(pts[1].p);
  if(x1===null||y1===null||x2===null||y2===null)return;
  const color=d.color||_liteDrawColor;
  if(d.type==='trendline'){
    _liteDrawLine(ctx,x1,y1,x2,y2,color,d.dash?[5,4]:null);
    if(selected){_liteDrawHandle(ctx,x1,y1);_liteDrawHandle(ctx,x2,y2);}
  }else if(d.type==='hline'){
    const plotW=_liteMainPlotWidth();
    ctx.save();ctx.strokeStyle=color;ctx.lineWidth=selected?1.8:1.2;if(d.dash)ctx.setLineDash([5,4]);
    ctx.beginPath();ctx.moveTo(0,y1);ctx.lineTo(plotW,y1);ctx.stroke();ctx.restore();
    if(selected)_liteDrawHandle(ctx,plotW/2,y1);
  }else if(d.type==='vline'){
    ctx.save();ctx.strokeStyle=color;ctx.lineWidth=selected?1.8:1.2;if(d.dash)ctx.setLineDash([5,4]);
    ctx.beginPath();ctx.moveTo(x1,0);ctx.lineTo(x1,DOM.liteChart.clientHeight);ctx.stroke();ctx.restore();
    if(selected)_liteDrawHandle(ctx,x1,DOM.liteChart.clientHeight/2);
  }else if(d.type==='rect'){
    ctx.save();ctx.strokeStyle=color;ctx.fillStyle=_liteHexAlpha(color,.10);
    ctx.lineWidth=selected?1.8:1.2;const rx=Math.min(x1,x2),ry=Math.min(y1,y2),rw=Math.abs(x2-x1),rh=Math.abs(y2-y1);
    ctx.fillRect(rx,ry,rw,rh);ctx.strokeRect(rx,ry,rw,rh);ctx.restore();
    if(selected)_liteDrawCornerHandles(ctx,x1,y1,x2,y2);
    // Nhãn % chỉ hiện realtime lúc đang vẽ dở, hoặc khi bật tuỳ chọn hiển thị (d.showPct).
    const pct=(d===_liteLinePending||d.showPct)?_liteRectPct(d):null;
    if(pct!==null){
      const pctColor=pct>=0?'#16a34a':'#ef4444';
      const pctText=(pct>=0?'+':'')+pct.toFixed(2)+'%';
      ctx.save();
      ctx.font='bold 11px "IBM Plex Mono",monospace';
      ctx.textAlign='center';ctx.textBaseline='middle';
      const tw=ctx.measureText(pctText).width,boxH=15;
      const lx=rx+rw/2;
      let boxY=pct>=0?(ry-3-boxH):(ry+rh+3);
      if(pct>=0&&boxY<0)boxY=ry+3;
      if(pct<0&&boxY+boxH>DOM.liteChart.clientHeight)boxY=ry+rh-3-boxH;
      ctx.fillStyle='rgba(255,255,255,.88)';
      ctx.fillRect(lx-tw/2-4,boxY,tw+8,boxH);
      ctx.strokeStyle=_liteHexAlpha(pctColor,.4);ctx.lineWidth=1;
      ctx.strokeRect(lx-tw/2-4,boxY,tw+8,boxH);
      ctx.fillStyle=pctColor;
      ctx.fillText(pctText,lx,boxY+boxH/2+0.5);
      ctx.restore();
    }
  }else if(d.type==='channel'){
    // Kênh (2 cạnh+nền) chỉ hiện khi đã xác lập điểm thứ 3 (độ rộng); bước 1 chỉ hiện đường chéo xem trước.
    if(pts[2]&&Number.isFinite(pts[2].offsetPrice)){
      const offPrice=pts[2].offsetPrice;
      const y1b=_litePriceToY(pts[0].p+offPrice),y2b=_litePriceToY(pts[1].p+offPrice);
      if(y1b!==null&&y2b!==null){
        ctx.save();
        ctx.beginPath();
        ctx.moveTo(x1,y1);ctx.lineTo(x2,y2);ctx.lineTo(x2,y2b);ctx.lineTo(x1,y1b);ctx.closePath();
        ctx.fillStyle=_liteHexAlpha(color,.12);ctx.fill();
        ctx.restore();
        // đường giữa: nét đứt, mờ, chia đôi kênh
        _liteDrawLine(ctx,x1,(y1+y1b)/2,x2,(y2+y2b)/2,_liteHexAlpha(color,.5),[4,3]);
      }
      // 2 cạnh biên kênh: nét liền, đậm
      ctx.save();ctx.strokeStyle=color;ctx.lineWidth=selected?2.6:2.1;ctx.lineCap='round';
      ctx.beginPath();ctx.moveTo(x1,y1);ctx.lineTo(x2,y2);ctx.stroke();
      if(y1b!==null&&y2b!==null){ctx.beginPath();ctx.moveTo(x1,y1b);ctx.lineTo(x2,y2b);ctx.stroke();}
      ctx.restore();
      if(selected){
        _liteDrawHandle(ctx,x1,y1);_liteDrawHandle(ctx,x2,y2);
        if(y1b!==null&&y2b!==null){_liteDrawHandle(ctx,x1,y1b);_liteDrawHandle(ctx,x2,y2b);}
      }
    }else{
      _liteDrawLine(ctx,x1,y1,x2,y2,color,[5,4]);
      if(selected){_liteDrawHandle(ctx,x1,y1);_liteDrawHandle(ctx,x2,y2);}
    }
  }else if(d.type==='arrow'){
    const aw=Number.isFinite(d.arrowW)?d.arrowW:2;
    if(d.wide){
      _liteDrawWideArrow(ctx,x1,y1,x2,y2,color,aw);
    }else{
      _liteDrawLine(ctx,x1,y1,x2,y2,color,d.dash?[5,4]:null,aw);
      const ang=Math.atan2(y2-y1,x2-x1),headLen=8+aw*3;
      ctx.save();ctx.fillStyle=color;ctx.strokeStyle=color;ctx.lineWidth=aw;
      ctx.beginPath();
      ctx.moveTo(x2,y2);
      ctx.lineTo(x2-headLen*Math.cos(ang-Math.PI/7),y2-headLen*Math.sin(ang-Math.PI/7));
      ctx.lineTo(x2-headLen*Math.cos(ang+Math.PI/7),y2-headLen*Math.sin(ang+Math.PI/7));
      ctx.closePath();ctx.fill();
      ctx.restore();
    }
    if(selected){_liteDrawHandle(ctx,x1,y1);_liteDrawHandle(ctx,x2,y2);}
  }else if(d.type==='arc'){
    // Đường cong bán nguyệt: bước 2 rê chuột tự do chọn điểm đáy; quy đổi sang pixel trước rồi tính control-point (_liteArcControlXY) để cong luôn đi đúng qua vị trí chuột.
    const tx=pts[2]?_liteLogicalToX(pts[2]):null,ty=pts[2]?_litePriceToY(pts[2].p):null;
    const ctrl=(tx!==null&&ty!==null)?_liteArcControlXY(x1,y1,x2,y2,tx,ty):null;
    if(ctrl){
      const cx=ctrl.cx,cy=ctrl.cy;
      // Tô màu phần diện tích giữa dây cung và đường cong, giống dải màu Kênh giá.
      if(cx!==null&&cy!==null){
        ctx.save();
        ctx.beginPath();
        ctx.moveTo(x1,y1);
        ctx.quadraticCurveTo(cx,cy,x2,y2);
        ctx.lineTo(x1,y1);
        ctx.closePath();
        ctx.fillStyle=_liteHexAlpha(color,.12);
        ctx.fill();
        ctx.restore();
      }
      ctx.save();ctx.strokeStyle=color;ctx.lineWidth=selected?2.4:1.8;ctx.lineCap='round';
      if(d.dash)ctx.setLineDash([5,4]);
      ctx.beginPath();ctx.moveTo(x1,y1);
      if(cx!==null&&cy!==null)ctx.quadraticCurveTo(cx,cy,x2,y2);else ctx.lineTo(x2,y2);
      ctx.stroke();ctx.restore();
      if(selected){
        _liteDrawHandle(ctx,x1,y1);_liteDrawHandle(ctx,x2,y2);
        // Handle hiển thị đúng tại điểm đáy (nơi chuột đã rê tới), trực quan hơn control-point toán học.
        const tx=_liteLogicalToX(pts[2]),ty=_litePriceToY(pts[2].p);
        if(tx!==null&&ty!==null)_liteDrawHandle(ctx,tx,ty);
      }
    }else{
      _liteDrawLine(ctx,x1,y1,x2,y2,color,[5,4]);
      if(selected){_liteDrawHandle(ctx,x1,y1);_liteDrawHandle(ctx,x2,y2);}
    }
  }else if(d.type==='position'){
    const entryP=pts[0].p,targetP=pts[1].p;
    const stopP=Number.isFinite(d.stopP)?d.stopP:(2*entryP-targetP);
    const hasT2=Number.isFinite(d.target2P);
    const entryY=y1,targetY=y2,stopY=_litePriceToY(stopP);
    const target2Y=hasT2?_litePriceToY(d.target2P):null;
    const rx=Math.min(x1,x2),rw=Math.abs(x2-x1);
    const targetColor=d.targetColor||'#26a69a';
    ctx.save();
    ctx.fillStyle=_liteHexAlpha(targetColor,hasT2?.09:.16);ctx.fillRect(rx,Math.min(entryY,targetY),rw,Math.abs(entryY-targetY));
    if(stopY!==null)ctx.fillStyle='rgba(239,83,80,.16)',ctx.fillRect(rx,Math.min(entryY,stopY),rw,Math.abs(entryY-stopY));
    if(target2Y!==null)ctx.fillStyle=_liteHexAlpha(targetColor,.16),ctx.fillRect(rx,Math.min(targetY,target2Y),rw,Math.abs(targetY-target2Y));
    ctx.lineWidth=selected?2:1.4;
    _liteDrawLine(ctx,rx,entryY,rx+rw,entryY,'#c1c7d0');
    _liteDrawLine(ctx,rx,targetY,rx+rw,targetY,targetColor,null,hasT2?0.6:1.4);
    if(stopY!==null)_liteDrawLine(ctx,rx,stopY,rx+rw,stopY,'#ef5350');
    if(target2Y!==null)_liteDrawLine(ctx,rx,target2Y,rx+rw,target2Y,targetColor);
    ctx.font='10px "IBM Plex Mono",monospace';
    ctx.fillStyle='#111827';ctx.fillText('Entry '+fmtLiteNum(entryP),rx+4,entryY-3);
    const pctT=entryP?((targetP-entryP)/entryP*100):0;
    const pctS=entryP?((stopP-entryP)/entryP*100):0;
    ctx.fillStyle=targetColor;ctx.fillText(`${hasT2?'Target 1':'Target'} ${fmtLiteNum(targetP)} (${pctT>=0?'+':''}${pctT.toFixed(2)}%)`,rx+4,targetY-3);
    if(stopY!==null){ctx.fillStyle='#ef5350';ctx.fillText(`Stop ${fmtLiteNum(stopP)} (${pctS>=0?'+':''}${pctS.toFixed(2)}%)`,rx+4,stopY+11);}
    if(target2Y!==null){
      const pctT2=entryP?((d.target2P-entryP)/entryP*100):0;
      ctx.fillStyle=targetColor;ctx.fillText(`Target 2 ${fmtLiteNum(d.target2P)} (${pctT2>=0?'+':''}${pctT2.toFixed(2)}%)`,rx+4,target2Y-3);
    }
    ctx.restore();
    if(selected){
      _liteDrawCornerHandles(ctx,x1,y1,x2,y2);
      _liteDrawHandlePair(ctx,rx,rw,stopY!==null?stopY:entryY);
      if(target2Y!==null)_liteDrawHandlePair(ctx,rx,rw,target2Y);
    }
  }
}
// _liteHexAlpha chỉ là lớp mỏng gọi lại _liteHexToRgba với màu fallback riêng (#1a56db), không tự parse hex nữa.
function _liteHexAlpha(hex,a){
  return _liteHexToRgba(hex,a,'26,86,219');
}
function _liteTimeToX(t){
  const c=_liteChart&&_liteChart.timeScale().timeToCoordinate(t);
  return Number.isFinite(c)?c:null;
}
function _liteMainPlotWidth(){
  const w=DOM.liteChart?.clientWidth||0;
  let axisW=64;
  try{
    const psW=_liteChart&&_liteChart.priceScale('right')&&_liteChart.priceScale('right').width();
    if(Number.isFinite(psW)&&psW>0)axisW=psW;
  }catch(e){}
  return Math.max(0,w-axisW);
}
function _liteClipMainPlot(ctx){
  ctx.beginPath();
  const hasVisibleTimeScale=_liteChart?.timeScale()?.options?.()?.visible!==false;
  const plotH=(DOM.liteChart?.clientHeight||0)-(hasVisibleTimeScale?26:0);
  ctx.rect(0,0,_liteMainPlotWidth(),plotH);
  ctx.clip();
}
function _liteDrawBBBand(ctx){
  if(!_liteBBFillData||!_liteChart)return;
  const{upper,lower,color}=_liteBBFillData;
  if(!upper||!lower||!upper.length||!lower.length)return;
  ctx.save();
  _liteClipMainPlot(ctx);
  ctx.beginPath();
  let started=false;
  for(let i=0;i<upper.length;i++){
    const x=_liteTimeToX(upper[i].time),y=_litePriceToY(upper[i].value);
    if(x===null||y===null)continue;
    if(!started){ctx.moveTo(x,y);started=true;}else ctx.lineTo(x,y);
  }
  for(let i=lower.length-1;i>=0;i--){
    const x=_liteTimeToX(lower[i].time),y=_litePriceToY(lower[i].value);
    if(x===null||y===null)continue;
    ctx.lineTo(x,y);
  }
  if(started){ctx.closePath();ctx.fillStyle=_liteHexAlpha(color,.075);ctx.fill();}
  ctx.restore();
}
function _liteDrawTrendCloud(ctx){
  if(!_liteTrendFillData||!_liteChart||!_liteTrendFillData.length)return;
  const pts=_liteTrendFillData;
  ctx.save();
  _liteClipMainPlot(ctx);
  let i=0;
  while(i<pts.length){
    const trend=pts[i].trend;
    let j=i+1;
    while(j<pts.length&&pts[j].trend===trend)j++;
    const seg=pts.slice(i,j);
    ctx.beginPath();
    let started=false;
    for(let k=0;k<seg.length;k++){
      const x=_liteTimeToX(seg[k].time),y=_litePriceToY(seg[k].top);
      if(x===null||y===null)continue;
      if(!started){ctx.moveTo(x,y);started=true;}else ctx.lineTo(x,y);
    }
    for(let k=seg.length-1;k>=0;k--){
      const x=_liteTimeToX(seg[k].time),y=_litePriceToY(seg[k].bottom);
      if(x===null||y===null)continue;
      ctx.lineTo(x,y);
    }
    if(started){
      const col=trend===1?(_liteIndColors['trend-up']||'#64fa96'):(_liteIndColors['trend-down']||'#fa9696');
      ctx.fillStyle=_liteHexAlpha(col,trend===1?.28:.24);
      ctx.fill();
    }
    i=j;
  }
  ctx.restore();
}
// Vẽ mũi tên báo mua thật (đầu tam giác+thân que), đặt cách xa dưới low hơn marker mặc định của thư viện để tránh đụng bấc nến/volume.
function _liteDrawBuyArrow(ctx){
  if(!_liteBuyArrowData||!_liteChart||!_liteCandle||!_liteData.length)return;
  // Đọc live nến cuối cùng tại thời điểm vẽ (không lưu sẵn) — mũi tên luôn bám đúng tâm nến hiện tại.
  const lastBar=_liteData[_liteData.length-1];
  const{color}=_liteBuyArrowData;
  const x=_liteTimeToX(lastBar.time),yLow=_litePriceToY(lastBar.low);
  if(x===null||yLow===null)return;
  // GAP: khoảng cách xuống dưới low tới đuôi mũi tên. HEAD_H/HEAD_HALF_W: tam giác đầu. SHAFT_*: thân que.
  const GAP=14,HEAD_H=6,HEAD_HALF_W=2.5,SHAFT_H=5,SHAFT_HALF_W=1;
  const yTip=yLow+GAP;              // đỉnh mũi tên, hướng lên phía nến
  const yHeadBase=yTip+HEAD_H;      // đáy tam giác đầu mũi tên = đỉnh thân que
  ctx.save();
  _liteClipMainPlot(ctx);
  ctx.fillStyle=color;
  // Đầu mũi tên (tam giác)
  ctx.beginPath();
  ctx.moveTo(x,yTip);
  ctx.lineTo(x-HEAD_HALF_W,yHeadBase);
  ctx.lineTo(x+HEAD_HALF_W,yHeadBase);
  ctx.closePath();
  ctx.fill();
  // Thân mũi tên (que mảnh)
  ctx.fillRect(x-SHAFT_HALF_W,yHeadBase,SHAFT_HALF_W*2,SHAFT_H);
  ctx.restore();
}
function _liteDrawHistorySignalDots(ctx){
  if(!_liteHistorySignals||!_liteHistorySignals.length||!_liteChart||!_liteData.length)return;
  if(!_liteChecked('signalgrp_on')||!_liteChecked('signal'))return;
  const hasVisibleTimeScale=_liteChart?.timeScale()?.options?.()?.visible!==false;
  const plotH=(DOM.liteChart?.clientHeight||0)-(hasVisibleTimeScale?26:0);
  const H_DASH=2;
  const yDash=Math.max(0,Math.round(plotH-H_DASH));
  if(yDash<=0)return;
  ctx.save();
  _liteClipMainPlot(ctx);
  ctx.fillStyle='#d946ef';
  for(let i=0;i<_liteHistorySignals.length;i++){
    const x=_liteTimeToX(_liteHistorySignals[i]);
    if(x===null)continue;
    ctx.fillRect(Math.round(x-2.5),yDash,5,H_DASH);
  }
  ctx.restore();
}
function redrawLiteDrawings(){
  if(!_liteDrawCtx||!DOM.liteDrawCanvas)return;
  const w=DOM.liteChart.clientWidth,h=DOM.liteChart.clientHeight;
  if(_liteChartLoading){
    _liteDrawCtx.clearRect(0,0,w,h);
    return;
  }
  _liteDrawCtx.clearRect(0,0,w,h);
  _liteDrawTrendCloud(_liteDrawCtx);
  _liteDrawBBBand(_liteDrawCtx);
  _liteDrawBuyArrow(_liteDrawCtx);
  _liteDrawHistorySignalDots(_liteDrawCtx);
  for(const d of _liteDrawings)_liteDrawShapeToCanvas(_liteDrawCtx,d);
  if(_liteDrawActive)_liteDrawShapeToCanvas(_liteDrawCtx,_liteDrawActive);
  if(_liteChannelPending)_liteDrawShapeToCanvas(_liteDrawCtx,_liteChannelPending);
  if(_liteArcPending)_liteDrawShapeToCanvas(_liteDrawCtx,_liteArcPending);
  if(_liteZigzagPending)_liteDrawShapeToCanvas(_liteDrawCtx,_liteZigzagPending);
  if(_liteLinePending)_liteDrawShapeToCanvas(_liteDrawCtx,_liteLinePending);
  _liteUpdateFloatingBar();
}
function _liteFinishZigzag(){
  if(!_liteZigzagPending)return false;
  const pend=_liteZigzagPending;
  pend._hover=null;
  _liteZigzagPending=null;
  if(pend.points.length>=2){
    _liteDrawings.push(pend);
    saveLiteDrawings();
    _liteSelectShape(pend.id);
  }else{
    redrawLiteDrawings();
  }
  return true;
}
function _liteApplyTextInputStyle(){
  if(!DOM.liteTextInput)return;
  DOM.liteTextInput.style.color=_liteDrawColor;
  DOM.liteTextInput.style.fontSize=_liteTextSize+'px';
  DOM.liteTextInput.style.fontFamily=LITE_TEXT_FONT_CSS[_liteTextFont]||LITE_TEXT_FONT_CSS.mono;
  DOM.liteTextInput.style.background=_liteTextBg||'rgba(255,255,255,.96)';
}
function _liteOpenTextInput(p0,ev,editingShape){
  if(!DOM.liteTextInput)return;
  if(ev&&ev.preventDefault)ev.preventDefault();
  let x,y;
  if(ev){({x,y}=_liteXYFromEvent(ev));}
  else{x=_liteLogicalToX(p0);y=_litePriceToY(p0.p);}
  if(x===null||x===undefined||y===null||y===undefined)return;
  _liteTextEditPos=p0;
  _liteTextEditId=editingShape?editingShape.id:null;
  if(editingShape){
    _liteDrawColor=editingShape.color||_liteDrawColor;
    _liteTextSize=editingShape.fontSize||13;
    _liteTextFont=editingShape.fontFamily||'mono';
    _liteTextBg=editingShape.bg||'';
    DOM.liteTextInput.value=editingShape.text||'';
  }else{
    DOM.liteTextInput.value='';
  }
  DOM.liteTextInput.style.left=x+'px';
  DOM.liteTextInput.style.top=Math.max(0,y)+'px';
  _liteApplyTextInputStyle();
  DOM.liteTextInput.classList.add('on');
  // Focus ngay, rồi focus lại ở animation frame kế tiếp — phòng khi trình duyệt chưa layout xong.
  DOM.liteTextInput.focus();
  if(editingShape){const v=DOM.liteTextInput.value;DOM.liteTextInput.setSelectionRange(v.length,v.length);}
  requestAnimationFrame(()=>DOM.liteTextInput.focus());
}
function _liteCommitTextInput(){
  if(!DOM.liteTextInput||!_liteTextEditPos)return null;
  const text=(DOM.liteTextInput.value||'').replace(/\s+$/,'');
  const p0=_liteTextEditPos;
  const editId=_liteTextEditId;
  DOM.liteTextInput.classList.remove('on');
  DOM.liteTextInput.value='';
  _liteTextEditPos=null;
  _liteTextEditId=null;
  if(!text){
    if(editId!=null){_liteDrawings=_liteDrawings.filter(d=>d.id!==editId);saveLiteDrawings();redrawLiteDrawings();}
    return null;
  }
  if(editId!=null){
    const d=_liteDrawings.find(x=>x.id===editId);
    if(d){
      d.text=text;d.color=_liteDrawColor;d.fontSize=_liteTextSize;d.fontFamily=_liteTextFont;d.bg=_liteTextBg||null;
      saveLiteDrawings();
      _liteSelectShape(editId);
      return editId;
    }
  }
  const id=_liteDrawSeq++;
  _liteDrawings.push({id,type:'text',points:[p0],text,color:_liteDrawColor,fontSize:_liteTextSize,fontFamily:_liteTextFont,bg:_liteTextBg||null});
  saveLiteDrawings();
  _liteSelectShape(id);
  return id;
}
function _liteCloseTextInput(){
  if(!DOM.liteTextInput)return;
  DOM.liteTextInput.classList.remove('on');
  DOM.liteTextInput.value='';
  _liteTextEditPos=null;
  _liteTextEditId=null;
}
function setLiteDrawTool(tool){
  const prevTool=_liteDrawTool,hadZigzag=_liteZigzagPending;
  _liteDrawTool=tool||'cursor';
  if(_liteDrawTool!=='channel')_liteChannelPending=null;
  if(_liteDrawTool!=='arc')_liteArcPending=null;
  if(_liteDrawTool!=='zigzag'&&prevTool==='zigzag'&&hadZigzag)_liteFinishZigzag();
  else if(_liteDrawTool!=='zigzag')_liteZigzagPending=null;
  if(_liteDrawTool!=='text'&&_liteTextEditPos)_liteCommitTextInput();
  if(_liteDrawTool!=='trendline'&&_liteDrawTool!=='rect'&&_liteDrawTool!=='channel'&&_liteDrawTool!=='arc'&&_liteDrawTool!=='arrow')_liteLinePending=null;
  if(_liteDrawTool!=='cursor')_liteSelectedId=null;
  DOM.liteDrawToolbar?.querySelectorAll('.lite-draw-btn[data-tool]').forEach(b=>b.classList.toggle('on',b.dataset.tool===_liteDrawTool));
  if(DOM.liteDrawCanvas)DOM.liteDrawCanvas.classList.toggle('drawing',_liteDrawTool!=='cursor');
  if(DOM.liteChart)DOM.liteChart.style.cursor='';
  _liteHideRectTooltip();
  redrawLiteDrawings();
}
function _liteShapeAnchor(d){
  const pts=d.points;
  if(!pts||!pts.length)return null;
  if(d.type==='text'){
    const x=_liteLogicalToX(pts[0]),y=_litePriceToY(pts[0].p);
    if(x===null||y===null)return null;
    const m=_liteTextBoxMetrics(d);
    const w=m?m.width:0;
    return{x:x+w/2,y:y-6};
  }
  if(pts.length<2)return null;
  const x1=_liteLogicalToX(pts[0]),y1=_litePriceToY(pts[0].p);
  const x2=_liteLogicalToX(pts[1]),y2=_litePriceToY(pts[1].p);
  if(x1===null||y1===null||x2===null||y2===null)return null;
  if(d.type==='hline')return{x:_liteMainPlotWidth()/2,y:y1};
  if(d.type==='vline')return{x:x1,y:12};
  if(d.type==='position'){
    const entryP=pts[0].p,targetP=pts[1].p;
    const stopP=Number.isFinite(d.stopP)?d.stopP:(2*entryP-targetP);
    const stopY=_litePriceToY(stopP);
    const ys=[y1,y2,stopY!==null?stopY:y1];
    if(Number.isFinite(d.target2P)){
      const t2y=_litePriceToY(d.target2P);
      if(t2y!==null)ys.push(t2y);
    }
    return{x:(x1+x2)/2,y:Math.min(...ys)};
  }
  if(d.type==='channel'){
    // Lấy điểm cao nhất trong cả 4 góc kênh (kể cả 2 góc đã dịch offset) để thanh điều chỉnh luôn nằm hẳn trên đỉnh kênh, kể cả khi kênh nghiêng.
    const offPrice=_liteChannelOffset(d);
    const y1b=_litePriceToY(pts[0].p+offPrice),y2b=_litePriceToY(pts[1].p+offPrice);
    const ys=[y1,y2];
    if(y1b!==null)ys.push(y1b);
    if(y2b!==null)ys.push(y2b);
    return{x:(x1+x2)/2,y:Math.min(...ys)};
  }
  if(d.type==='arc'){
    const ty=(pts[2]&&Number.isFinite(pts[2].p))?_litePriceToY(pts[2].p):null;
    return{x:(x1+x2)/2,y:Math.min(y1,y2,ty!==null?ty:Math.min(y1,y2))};
  }
  if(d.type==='zigzag'){
    let minY=Infinity,sumX=0,n=0;
    for(const pt of pts){
      const xx=_liteLogicalToX(pt),yy=_litePriceToY(pt.p);
      if(xx===null||yy===null)continue;
      minY=Math.min(minY,yy);sumX+=xx;n++;
    }
    return n?{x:sumX/n,y:minY}:null;
  }
  if(d.type==='rect'){
    const top=Math.min(y1,y2);
    // Nhãn % vẽ trên cạnh hộp khi giá tăng (đẩy anchor lên để không đè thanh công cụ), dưới hộp khi giá giảm.
    const pct=d.showPct?_liteRectPct(d):null;
    return{x:(x1+x2)/2,y:(pct!==null&&pct>=0)?top-18:top};
  }
  return{x:(x1+x2)/2,y:Math.min(y1,y2)};
}
// Lấy hình đang được chọn (theo _liteSelectedId) — gộp lại 1 chỗ duy nhất thay vì lặp lại cùng 1 biểu thức tra cứu ở rất nhiều handler bên dưới.
function _liteGetSelectedShape(){
  return _liteSelectedId!=null?_liteDrawings.find(d=>d.id===_liteSelectedId):null;
}
function _liteUpdateFloatingBar(){
  if(!DOM.liteShapeBar)return;
  const d=_liteGetSelectedShape();
  if(!d){DOM.liteShapeBar.classList.remove('on');return;}
  const anchor=_liteShapeAnchor(d);
  if(!anchor){DOM.liteShapeBar.classList.remove('on');return;}
  DOM.liteShapeBar.classList.add('on');
  DOM.liteShapeBar.style.left=Math.max(30,Math.min((DOM.liteChart?.clientWidth||600)-30,anchor.x))+'px';
  DOM.liteShapeBar.style.top=Math.max(24,anchor.y)+'px';
  const isText=d.type==='text',isPosition=d.type==='position';
  if(DOM.liteShapeColor){
    DOM.liteShapeColor.style.display=isPosition?'none':'';
    if(d.color)DOM.liteShapeColor.value=d.color;
  }
  if(DOM.liteShapeTargetColor){
    DOM.liteShapeTargetColor.style.display=isPosition?'':'none';
    DOM.liteShapeTargetColor.value=d.targetColor||'#26a69a';
  }
  if(DOM.liteShapeTarget2){
    DOM.liteShapeTarget2.style.display=isPosition?'':'none';
    DOM.liteShapeTarget2.classList.toggle('on',isPosition&&Number.isFinite(d.target2P));
  }
  if(DOM.liteShapeDash){
    const supportsDash=d.type==='trendline'||d.type==='hline'||d.type==='vline';
    DOM.liteShapeDash.style.display=supportsDash?'':'none';
    DOM.liteShapeDash.classList.toggle('on',!!d.dash);
  }
  if(DOM.liteShapePct){
    const isRect=d.type==='rect';
    DOM.liteShapePct.style.display=isRect?'':'none';
    DOM.liteShapePct.classList.toggle('on',isRect&&!!d.showPct);
  }
  if(DOM.liteShapeArrowStyle){
    const isArrow=d.type==='arrow';
    DOM.liteShapeArrowStyle.style.display=isArrow?'':'none';
    DOM.liteShapeArrowStyle.classList.toggle('on',isArrow&&!!d.wide);
  }
  if(DOM.liteShapeArrowWidth){
    const isArrow=d.type==='arrow';
    DOM.liteShapeArrowWidth.style.display=isArrow?'':'none';
    if(isArrow)DOM.liteShapeArrowWidth.value=String(d.arrowW||2);
  }
  if(DOM.liteShapeZigzagFill){
    const isZZ=d.type==='zigzag';
    DOM.liteShapeZigzagFill.style.display=isZZ?'':'none';
    // "on" = đang ở trạng thái tắt dải màu (chỉ còn đường zigzag)
    DOM.liteShapeZigzagFill.classList.toggle('on',isZZ&&!!d.noFill);
  }
  if(DOM.liteShapeFontSize){
    DOM.liteShapeFontSize.style.display=isText?'':'none';
    if(isText)DOM.liteShapeFontSize.value=String(d.fontSize||13);
  }
  if(DOM.liteShapeFontFamily){
    DOM.liteShapeFontFamily.style.display=isText?'':'none';
    if(isText)DOM.liteShapeFontFamily.value=d.fontFamily||'mono';
  }
  if(DOM.liteShapeBgColor){
    DOM.liteShapeBgColor.style.display=isText?'':'none';
    if(isText)DOM.liteShapeBgColor.value=d.bg||'#ffffff';
  }
  if(DOM.liteShapeBgClear)DOM.liteShapeBgClear.style.display=isText?'':'none';
  if(DOM.liteShapeEdit)DOM.liteShapeEdit.style.display=isText?'':'none';
}
function _liteSelectShape(id){
  _liteSelectedId=id;
  const d=_liteDrawings.find(x=>x.id===id);
  if(d&&d.type!=='position'&&d.color&&DOM.liteDrawColor)DOM.liteDrawColor.value=d.color;
  redrawLiteDrawings();
  _liteUpdateFloatingBar();
}
// ─── Hit-testing (để chọn / kéo hình đã vẽ khi ở chế độ con trỏ) ───
function _liteSegDist(px,py,x1,y1,x2,y2){
  const dx=x2-x1,dy=y2-y1,len2=dx*dx+dy*dy;
  if(len2<1e-6)return Math.hypot(px-x1,py-y1);
  let t=((px-x1)*dx+(py-y1)*dy)/len2;t=Math.max(0,Math.min(1,t));
  return Math.hypot(px-(x1+t*dx),py-(y1+t*dy));
}
// Hit-test 4 góc khung 2-điểm (p0,p1 thật + c1,c2 là góc ảo ghép chéo) — dùng chung cho Hộp và Target.
function _liteCornerHitPart(x,y,x1,y1,x2,y2){
  if(Math.hypot(x-x1,y-y1)<=9)return'p0';
  if(Math.hypot(x-x2,y-y2)<=9)return'p1';
  if(Math.hypot(x-x1,y-y2)<=9)return'c1'; // góc ảo: x theo p0, y theo p1
  if(Math.hypot(x-x2,y-y1)<=9)return'c2'; // góc ảo: x theo p1, y theo p0
  return null;
}
function _liteHitTestShape(d,x,y){
  const pts=d.points;
  if(d.type==='text'){
    const px=_liteLogicalToX(pts[0]),py=_litePriceToY(pts[0].p);
    if(px===null||py===null)return null;
    const m=_liteTextBoxMetrics(d);
    const w=m?m.width:20,h=m?m.height:16;
    if(x>=px-LITE_HIT_TOL&&x<=px+w+LITE_HIT_TOL&&y>=py-LITE_HIT_TOL&&y<=py+h+LITE_HIT_TOL)return{part:'p0'};
    return null;
  }
  if(pts.length<2)return null;
  const x1=_liteLogicalToX(pts[0]),y1=_litePriceToY(pts[0].p);
  const x2=_liteLogicalToX(pts[1]),y2=_litePriceToY(pts[1].p);
  if(x1===null||y1===null||x2===null||y2===null)return null;
  if(d.type==='hline'){
    const plotW=_liteMainPlotWidth();
    return(x>=0&&x<=plotW&&Math.abs(y-y1)<=LITE_HIT_TOL)?{part:'line'}:null;
  }
  if(d.type==='vline')return Math.abs(x-x1)<=LITE_HIT_TOL?{part:'line'}:null;
  if(d.type==='trendline'||d.type==='arrow'){
    if(Math.hypot(x-x1,y-y1)<=9)return{part:'p0'};
    if(Math.hypot(x-x2,y-y2)<=9)return{part:'p1'};
    return _liteSegDist(x,y,x1,y1,x2,y2)<=LITE_HIT_TOL?{part:'line'}:null;
  }
  if(d.type==='zigzag'){
    for(let i=0;i<pts.length;i++){
      const px=_liteLogicalToX(pts[i]),py=_litePriceToY(pts[i].p);
      if(px!==null&&py!==null&&Math.hypot(x-px,y-py)<=9)return{part:'v'+i};
    }
    for(let i=0;i<pts.length-1;i++){
      const ax=_liteLogicalToX(pts[i]),ay=_litePriceToY(pts[i].p);
      const bx=_liteLogicalToX(pts[i+1]),by=_litePriceToY(pts[i+1].p);
      if(ax!==null&&ay!==null&&bx!==null&&by!==null&&_liteSegDist(x,y,ax,ay,bx,by)<=LITE_HIT_TOL)return{part:'line'};
    }
    return null;
  }
  if(d.type==='rect'){
    const c=_liteCornerHitPart(x,y,x1,y1,x2,y2);
    if(c)return{part:c};
    const rx=Math.min(x1,x2),ry=Math.min(y1,y2),rw=Math.abs(x2-x1),rh=Math.abs(y2-y1);
    if(x>=rx-LITE_HIT_TOL&&x<=rx+rw+LITE_HIT_TOL&&y>=ry-LITE_HIT_TOL&&y<=ry+rh+LITE_HIT_TOL)return{part:'line'};
    return null;
  }
  if(d.type==='channel'){
    const offPrice=_liteChannelOffset(d);
    const y1b=_litePriceToY(pts[0].p+offPrice),y2b=_litePriceToY(pts[1].p+offPrice);
    if(Math.hypot(x-x1,y-y1)<=9)return{part:'p0'};
    if(Math.hypot(x-x2,y-y2)<=9)return{part:'p1'};
    if(_liteSegDist(x,y,x1,y1,x2,y2)<=LITE_HIT_TOL)return{part:'line'};
    if(y1b!==null&&y2b!==null&&_liteSegDist(x,y,x1,y1b,x2,y2b)<=LITE_HIT_TOL)return{part:'offset'};
    return null;
  }
  if(d.type==='arc'){
    if(Math.hypot(x-x1,y-y1)<=9)return{part:'p0'};
    if(Math.hypot(x-x2,y-y2)<=9)return{part:'p1'};
    const tx=pts[2]?_liteLogicalToX(pts[2]):null,ty=pts[2]?_litePriceToY(pts[2].p):null;
    const ctrl=(tx!==null&&ty!==null)?_liteArcControlXY(x1,y1,x2,y2,tx,ty):null;
    if(ctrl){
      if(tx!==null&&ty!==null&&Math.hypot(x-tx,y-ty)<=9)return{part:'offset'};
      if(_liteQuadDist(x,y,x1,y1,ctrl.cx,ctrl.cy,x2,y2)<=LITE_HIT_TOL)return{part:'offset'};
    }
    if(_liteSegDist(x,y,x1,y1,x2,y2)<=LITE_HIT_TOL)return{part:'line'};
    return null;
  }
  if(d.type==='position'){
    const entryP=pts[0].p,targetP=pts[1].p;
    const stopP=Number.isFinite(d.stopP)?d.stopP:(2*entryP-targetP);
    const entryY=y1,targetY=y2,stopY=_litePriceToY(stopP);
    const target2Y=Number.isFinite(d.target2P)?_litePriceToY(d.target2P):null;
    const rx=Math.min(x1,x2),rw=Math.abs(x2-x1);
    const c=_liteCornerHitPart(x,y,x1,y1,x2,y2);
    if(c)return{part:c};
    if(x<rx-LITE_HIT_TOL||x>rx+rw+LITE_HIT_TOL)return null;
    if(Math.abs(x-rx)<=LITE_HIT_TOL)return{part:'edgeL'};
    if(Math.abs(x-(rx+rw))<=LITE_HIT_TOL)return{part:'edgeR'};
    if(Math.abs(y-targetY)<=LITE_HIT_TOL)return{part:'target'};
    if(target2Y!==null&&Math.abs(y-target2Y)<=LITE_HIT_TOL)return{part:'target2'};
    if(stopY!==null&&Math.abs(y-stopY)<=LITE_HIT_TOL)return{part:'stop'};
    const ys=[entryY,targetY,stopY!==null?stopY:entryY];
    if(target2Y!==null)ys.push(target2Y);
    const top=Math.min(...ys),bottom=Math.max(...ys);
    if(y>=top-LITE_HIT_TOL&&y<=bottom+LITE_HIT_TOL)return{part:'body'};
    return null;
  }
  return null;
}
function _liteHitTest(x,y){
  for(let i=_liteDrawings.length-1;i>=0;i--){
    const hit=_liteHitTestShape(_liteDrawings[i],x,y);
    if(hit)return{id:_liteDrawings[i].id,shape:_liteDrawings[i],part:hit.part};
  }
  return null;
}
const LITE_RECT_TOOLTIP_DELAY_MS=600;
let _liteRectTooltipTimer=null;
function _liteClearRectTooltipTimer(){
  if(_liteRectTooltipTimer){clearTimeout(_liteRectTooltipTimer);_liteRectTooltipTimer=null;}
}
function _liteHideRectTooltip(){
  _liteClearRectTooltipTimer();
  if(DOM.liteRectTooltip)DOM.liteRectTooltip.style.display='none';
}
function _liteRectPct(d){
  if(!d||d.type!=='rect'||!d.points||d.points.length<2)return null;
  const p0=d.points[0].p,p1=d.points[1].p;
  if(typeof p0!=='number'||typeof p1!=='number'||!p0)return null;
  return(p1-p0)/p0*100;
}
function _liteShowRectTooltip(hit,x,y){
  const tip=DOM.liteRectTooltip;
  if(!tip)return;
  const pct=_liteRectPct(hit.shape);
  if(pct===null){tip.style.display='none';return;}
  tip.textContent=(pct>=0?'+':'')+pct.toFixed(2)+'%';
  tip.style.color=pct>=0?'#16a34a':'#ef4444';
  tip.style.left=x+'px';
  tip.style.top=y+'px';
  tip.style.display='block';
}
function _liteApplyDrag(d,info,cur){
  const dl=cur.l-info.startL,dp=cur.p-info.startP,op=info.origPoints;
  const key=d.type+':'+info.part;
  const mvPt=(orig,dL,dP)=>_litePtWithTime(orig.l+dL,orig.p+dP);
  if(key==='trendline:p0'||key==='rect:p0'||key==='channel:p0'||key==='arrow:p0'||key==='arc:p0'||key==='position:p0')d.points[0]=mvPt(op[0],dl,dp);
  else if(key==='trendline:p1'||key==='rect:p1'||key==='channel:p1'||key==='arrow:p1'||key==='arc:p1'||key==='position:p1')d.points[1]=mvPt(op[1],dl,dp);
  else if(key==='rect:c1'||key==='position:c1'){
    d.points[0]=mvPt(op[0],dl,0);
    d.points[1]=mvPt(op[1],0,dp);
  }else if(key==='rect:c2'||key==='position:c2'){
    d.points[0]=mvPt(op[0],0,dp);
    d.points[1]=mvPt(op[1],dl,0);
  }else if(key==='trendline:line'||key==='rect:line'||key==='channel:line'||key==='arrow:line'){
    d.points[0]=mvPt(op[0],dl,dp);d.points[1]=mvPt(op[1],dl,dp);
  }else if(key==='arc:line'){
    d.points[0]=mvPt(op[0],dl,dp);d.points[1]=mvPt(op[1],dl,dp);
    if(op[2]&&Number.isFinite(op[2].l))d.points[2]=mvPt(op[2],dl,dp);
  }else if(key==='hline:line'){
    d.points[0]=mvPt(op[0],0,dp);d.points[1]=mvPt(op[1],0,dp);
  }else if(key==='vline:line'){
    d.points[0]=mvPt(op[0],dl,0);d.points[1]=mvPt(op[1],dl,0);
  }else if(key==='channel:offset'){
    d.points[2]={offsetPrice:(info.origOffsetPrice||0)+dp};
  }else if(key==='arc:offset'){
    const baseL=(op[2]&&Number.isFinite(op[2].l))?op[2].l:(op[0].l+op[1].l)/2;
    const baseP=(op[2]&&Number.isFinite(op[2].p))?op[2].p:(op[0].p+op[1].p)/2;
    d.points[2]=mvPt({l:baseL,p:baseP},dl,dp);
  }else if(d.type==='zigzag'&&info.part==='line'){
    d.points=op.map(pt=>mvPt(pt,dl,dp));
  }else if(d.type==='zigzag'&&info.part[0]==='v'){
    const idx=parseInt(info.part.slice(1),10);
    if(op[idx])d.points[idx]=mvPt(op[idx],dl,dp);
  }else if(key==='position:body'){
    d.points[0]=mvPt(op[0],dl,dp);d.points[1]=mvPt(op[1],dl,dp);
    d.stopP=(info.origStopP??(2*op[0].p-op[1].p))+dp;
    if(Number.isFinite(info.origTarget2P))d.target2P=info.origTarget2P+dp;
  }else if(key==='position:target'){
    d.points[1]=mvPt(op[1],0,dp);
  }else if(key==='position:target2'){
    d.target2P=(info.origTarget2P??d.target2P)+dp;
  }else if(key==='position:stop'){
    d.stopP=(info.origStopP??(2*op[0].p-op[1].p))+dp;
  }else if(key==='position:edgeL'){
    d.points[0]=mvPt(op[0],dl,0);
  }else if(key==='position:edgeR'){
    d.points[1]=mvPt(op[1],dl,0);
  }else if(key==='text:p0'){
    d.points[0]=mvPt(op[0],dl,dp);
  }
}
function _liteStartShapeDrag(hit,ev){
  const d=hit.shape;
  _liteSelectShape(d.id);
  const startPt=_litePtFromEvent(ev);if(!startPt)return;
  const normalizedPoints=(d.points||[]).map(pt=>{
    if(!pt)return pt;
    return{...pt,l:_litePtLogical(pt)};
  });
  _liteDragInfo={
    part:hit.part,
    origPoints:JSON.parse(JSON.stringify(normalizedPoints)),
    origStopP:d.stopP,
    origTarget2P:d.target2P,
    origOffsetPrice:d.points&&d.points[2]&&d.points[2].offsetPrice,
    startL:startPt.l,startP:startPt.p
  };
  const move=ev2=>{
    const cur=_litePtFromEvent(ev2);if(!cur||!_liteDragInfo)return;
    _liteApplyDrag(d,_liteDragInfo,cur);
    redrawLiteDrawings();
  };
  const up=()=>{
    window.removeEventListener('pointermove',move);
    window.removeEventListener('pointerup',up);
    _liteDragInfo=null;
    saveLiteDrawings();
  };
  window.addEventListener('pointermove',move);
  window.addEventListener('pointerup',up);
}
// Bảng màu khớp đúng CSS .rs-90/.rs-80/.rs-50/.rs-low (dùng chung cho badge tròn RS vẽ trên canvas screenshot).
function _liteRsBadgeColors(v){
  if(v>90)return{bg:'#f3e8ff',fg:'#7e22ce',bd:'#d8b4fe'};
  if(v>80)return{bg:'#dcfce7',fg:'#15803d',bd:'#86efac'};
  if(v>50)return{bg:'#fef9c3',fg:'#854d0e',bd:'#fde047'};
  return{bg:'#fee2e2',fg:'#b91c1c',bd:'#fecaca'};
}
// Đo vị trí/kích thước thật của 1 overlay TƯƠNG ĐỐI so với #lite-chart (panes[0] chụp từ đây nên
// toạ độ khớp thẳng với ảnh chụp). frameRect nhận từ ngoài truyền vào (không tự gọi
// getBoundingClientRect() ở đây) vì 1 lượt chụp gọi hàm này nhiều lần với cùng frameRect.
function _liteOverlayRect(el,frameRect,dpr){
  const r=el.getBoundingClientRect();
  return{x:(r.left-frameRect.left)*dpr,y:(r.top-frameRect.top)*dpr,w:r.width*dpr,h:r.height*dpr};
}
function _liteDrawTitleSegments(ctx,segments,x,y,dpr){
  for(const seg of segments){
    if(seg.color==='__html'){
      if(seg._rs!=null){
        // Vẽ badge tròn màu giống hệt .rs-badge trên DOM (không còn vẽ chữ "RS:xx" thuần).
        const c=_liteRsBadgeColors(seg._rs);
        const d=Math.round(22*dpr),r=d/2;
        const savedFont=ctx.font,savedAlign=ctx.textAlign;
        x+=Math.round(5*dpr);
        ctx.beginPath();
        ctx.arc(x+r,y,r,0,Math.PI*2);
        ctx.fillStyle=c.bg;ctx.fill();
        ctx.lineWidth=Math.max(1,dpr);
        ctx.strokeStyle=c.bd;ctx.stroke();
        ctx.fillStyle=c.fg;
        ctx.font=`800 ${Math.round(10*dpr)}px "IBM Plex Mono",monospace`;
        ctx.textAlign='center';
        ctx.fillText(String(seg._rs),x+r,y+dpr);
        ctx.textAlign=savedAlign||'left';
        ctx.font=savedFont;
        x+=d;
        continue;
      }
      // Segment HTML vẽ text thuần trên canvas (đã lọc bỏ trên mobile portrait — xem copyLiteChartImage).
      const col=seg.text.match(/color:([^"]+)"/)?.[1]||'#111827';
      const parts=seg._open!=null
        ?[{t:' O:',c:'#111827'},{t:seg._open||'',c:col}]
        :[{t:' H:',c:'#111827'},{t:seg._high||'',c:col},{t:' L:',c:'#111827'},{t:seg._low||'',c:col}];
      for(const p of parts){
        ctx.fillStyle=p.c;
        ctx.fillText(p.t,x,y);
        x+=ctx.measureText(p.t).width;
      }
    }else{
      ctx.fillStyle=seg.color;
      ctx.fillText(seg.text,x,y);
      x+=ctx.measureText(seg.text).width;
    }
  }
}
// Vẽ nền mờ + title lên canvas copy đúng vị trí thật (overlay đè lên #lite-chart), thay vì
// dải header riêng cộng thêm phía trên như trước (khiến title+badge trông tách khỏi chart).
function _liteDrawTitleOverlay(ctx,segments,frameRect,dpr){
  const el=DOM.liteChartTitle;
  if(!el)return;
  const rect=_liteOverlayRect(el,frameRect,dpr),cs=getComputedStyle(el);
  const br=(parseFloat(cs.borderRadius)||0)*dpr;
  const savedFont=ctx.font,savedBaseline=ctx.textBaseline,savedAlign=ctx.textAlign,savedFill=ctx.fillStyle;
  ctx.beginPath();
  if(ctx.roundRect)ctx.roundRect(rect.x,rect.y,rect.w,rect.h,br);else ctx.rect(rect.x,rect.y,rect.w,rect.h);
  ctx.fillStyle=cs.backgroundColor;ctx.fill();
  ctx.font=`400 ${Math.round(11*dpr)}px "IBM Plex Mono",monospace`;
  ctx.textBaseline='middle';
  ctx.textAlign='left';
  const padX=(parseFloat(cs.paddingLeft)||0)*dpr;
  _liteDrawTitleSegments(ctx,segments,rect.x+padX,rect.y+rect.h/2,dpr);
  ctx.font=savedFont;ctx.textBaseline=savedBaseline;ctx.textAlign=savedAlign;ctx.fillStyle=savedFill;
}
// Vẽ lại khối "Giá phóng to" (overlay DOM, takeScreenshot() không chụp được) lên canvas copy,
// dùng _liteOverlayRect để lấy đúng vị trí thật. mainCenterX: tâm ngang pane main trên canvas tổng hợp.
function _liteDrawBigPrice(ctx,mainCenterX,frameRect,dpr){
  const el=DOM.liteChartBigPrice;
  if(!el||!el.classList.contains('on'))return;
  const priceEl=el.querySelector('.bp-price'),subEl=el.querySelector('.bp-sub');
  if(!priceEl||!priceEl.textContent)return;
  const priceCs=getComputedStyle(priceEl);
  const rect=_liteOverlayRect(el,frameRect,dpr);
  const savedAlign=ctx.textAlign,savedBaseline=ctx.textBaseline,savedFont=ctx.font,savedFill=ctx.fillStyle;
  ctx.textAlign='center';
  ctx.textBaseline='top';
  let y=rect.y;
  ctx.font=`700 ${Math.round(20*dpr)}px "IBM Plex Mono",monospace`;
  ctx.fillStyle=priceCs.color||'#111827';
  ctx.fillText(priceEl.textContent,mainCenterX,y);
  y+=Math.round(20*dpr*1.15);
  if(subEl&&subEl.textContent){
    const subCs=getComputedStyle(subEl);
    ctx.font=`400 ${Math.round(11*dpr)}px "IBM Plex Mono",monospace`;
    ctx.fillStyle=subCs.color||'#111827';
    ctx.fillText(subEl.textContent,mainCenterX,y);
  }
  ctx.textAlign=savedAlign;ctx.textBaseline=savedBaseline;ctx.font=savedFont;ctx.fillStyle=savedFill;
}
// Vẽ badge tín hiệu lên canvas copy (đọc màu/kích thước thật từ DOM badge vì là overlay, không bị
// takeScreenshot() chụp). Dùng _liteOverlayRect để vẽ đúng vị trí cả hàng lẫn từng phần tử con,
// thay vì suy ra toạ độ từ bề rộng emoji như trước (nguồn gốc gây lệch tâm chữ).
function _liteDrawSignalBadge(ctx,frameRect,dpr){
  const el=DOM.liteChartSignal;
  if(!el)return;
  const emojiEl=el.querySelector('.s-emoji'),badgeEl=el.querySelector('.s-badge');
  if(!emojiEl||!badgeEl)return;
  const savedBaseline=ctx.textBaseline,savedAlign=ctx.textAlign,savedFont=ctx.font,savedFill=ctx.fillStyle;
  const rowRect=_liteOverlayRect(el,frameRect,dpr),rowCs=getComputedStyle(el);
  const rowBr=(parseFloat(rowCs.borderRadius)||0)*dpr;
  ctx.beginPath();
  if(ctx.roundRect)ctx.roundRect(rowRect.x,rowRect.y,rowRect.w,rowRect.h,rowBr);else ctx.rect(rowRect.x,rowRect.y,rowRect.w,rowRect.h);
  ctx.fillStyle=rowCs.backgroundColor;ctx.fill();
  const emojiCs=getComputedStyle(emojiEl),badgeCs=getComputedStyle(badgeEl);
  const emojiR=_liteOverlayRect(emojiEl,frameRect,dpr),badgeR=_liteOverlayRect(badgeEl,frameRect,dpr);
  ctx.textBaseline='middle';
  ctx.textAlign='left';
  // fontSize từ getComputedStyle là CSS px, canvas chụp ở device-pixel nên phải nhân dpr thủ công,
  // nếu không icon/chữ trong badge sẽ nhỏ hơn khung badge (đã scale đúng qua getBoundingClientRect()*dpr).
  const emojiSize=Math.round((parseFloat(emojiCs.fontSize)||16)*dpr);
  ctx.font=`${emojiSize}px ${emojiCs.fontFamily||'sans-serif'}`;
  ctx.fillText(emojiEl.textContent,emojiR.x,emojiR.y+emojiR.h/2);
  const bx=badgeR.x,by=badgeR.y,bw=badgeR.w,bh=badgeR.h;
  const br=(parseFloat(badgeCs.borderRadius)||0)*dpr;
  ctx.beginPath();
  if(ctx.roundRect)ctx.roundRect(bx,by,bw,bh,br);else ctx.rect(bx,by,bw,bh);
  ctx.fillStyle=badgeCs.backgroundColor;ctx.fill();
  ctx.lineWidth=Math.max(1,(parseFloat(badgeCs.borderWidth)||1)*dpr);
  ctx.strokeStyle=badgeCs.borderColor;ctx.stroke();
  ctx.fillStyle=badgeCs.color;
  const badgeSize=Math.round((parseFloat(badgeCs.fontSize)||11)*dpr);
  ctx.font=`${badgeCs.fontWeight} ${badgeSize}px ${badgeCs.fontFamily||'sans-serif'}`;
  ctx.textAlign='center';
  // Tâm hộp thuần tuý (bx+bw/2, by+bh/2) qua textBaseline='middle' — bỏ số nhích +dpr cũ (vốn canh
  // cho cỡ chữ nhỏ trước khi sửa, nay làm chữ lệch tâm) để chữ tín hiệu luôn nằm đúng giữa badge.
  ctx.fillText(badgeEl.textContent,bx+bw/2,by+bh/2);
  ctx.textBaseline=savedBaseline;ctx.textAlign=savedAlign;ctx.font=savedFont;ctx.fillStyle=savedFill;
}
function _liteCopyFeedback(btn,status){
  if(!btn)return;
  const states={
    copied:{icon:'✓',title:'Đã sao chép ảnh chart',delay:1200},
    downloaded:{icon:'↓',title:'Clipboard bị chặn — đã tải ảnh PNG',delay:1200},
    failed:{icon:'!',title:'Không thể tạo ảnh chart',delay:2200}
  };
  const state=states[status]||states.failed;
  const icon=btn.dataset.copyIcon||(btn.dataset.copyIcon=btn.innerHTML);
  const title=btn.dataset.copyTitle||(btn.dataset.copyTitle=btn.title);
  if(btn._copyFeedbackTimer)clearTimeout(btn._copyFeedbackTimer);
  btn.innerHTML=state.icon;btn.title=state.title;
  btn._copyFeedbackTimer=setTimeout(()=>{
    btn.innerHTML=icon;btn.title=title;
  },state.delay);
}
function _litePngBlobFromDataUrl(dataUrl){
  const binary=atob(dataUrl.slice(dataUrl.indexOf(',')+1));
  const bytes=new Uint8Array(binary.length);
  for(let i=0;i<binary.length;i++)bytes[i]=binary.charCodeAt(i);
  return new Blob([bytes],{type:'image/png'});
}
function _liteDownloadChartImage(blob){
  const url=URL.createObjectURL(blob);
  const link=document.createElement('a');
  link.href=url;link.download=`chart_${_liteSymbol}_${_liteTf}.png`;
  document.body.appendChild(link);link.click();link.remove();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
}
// Copy PNG vào clipboard, dùng chung cho Mrk Health/Treemap (dựng ảnh bất đồng bộ qua <img>,
// khác thẻ CHART là canvas đồng bộ). Phải gọi clipboard.write() NGAY trong lượt chạm, truyền thẳng
// Promise<Blob> chưa resolve — nếu await xong Blob mới gọi thì user-activation đã hết hạn trên mobile.
async function _liteCopyOrDownloadPng(btn,buildBlob,filename,label){
  if(typeof navigator.clipboard?.write==='function'&&window.ClipboardItem){
    try{
      await navigator.clipboard.write([new ClipboardItem({'image/png':buildBlob()})]);
      _liteCopyFeedback(btn,'copied');
      return;
    }catch(e){console.warn(`Copy ảnh ${label} vào clipboard lỗi, chuyển sang tải PNG:`,e);}
  }
  const pngBlob=await buildBlob();
  const dlUrl=URL.createObjectURL(pngBlob);
  const link=document.createElement('a');
  link.href=dlUrl;link.download=filename;
  document.body.appendChild(link);link.click();link.remove();
  setTimeout(()=>URL.revokeObjectURL(dlUrl),1000);
  _liteCopyFeedback(btn,'downloaded');
}
async function copyLiteChartImage(btn){
  if(!_liteChart||!_liteRsiChart||!_liteMacdChart)return;
  try{
    const panes=[{kind:'main',canvas:_liteChart.takeScreenshot()}];
    if(_liteChecked('rsi')&&DOM.liteRsiChart.style.display!=='none'){
      panes.push({kind:'rsi',canvas:_liteRsiChart.takeScreenshot()});
    }
    if(_liteChecked('macd')&&DOM.liteMacdChart.style.display!=='none'){
      panes.push({kind:'macd',canvas:_liteMacdChart.takeScreenshot()});
    }
    const titleSegments=_liteTitleSegments(_liteData[_liteData.length-1]);
    // Portrait mobile: title thật đã ẩn O/H/L (CSS) để vừa 1 dòng; canvas chụp cũng phải bỏ 2 đoạn
    // này khi vẽ, nếu không sẽ tràn/che khuất phần cuối (RS, %...).
    const isMobilePortraitShot=window.innerWidth<=768&&window.innerHeight>window.innerWidth;
    const finalTitleSegments=isMobilePortraitShot?titleSegments.filter(s=>s._open==null&&s._high==null&&s._low==null):titleSegments;
    const hasSigBadge=!!(DOM.liteChartSignal&&DOM.liteChartSignal.classList.contains('on'));
    const dpr=window.devicePixelRatio||1;
    const out=document.createElement('canvas');
    out.width=Math.max(...panes.map(p=>p.canvas.width));
    // Title/badge/giá phóng to trên chart thật là overlay đè lên #lite-chart, KHÔNG phải dải header
    // cộng thêm. Trước đây ảnh chụp cộng titleH+badgeH vào chiều cao khiến chúng trông tách khỏi chart.
    // Nay canvas tổng chỉ cao bằng tổng pane, overlay được vẽ đè lên sau khi vẽ chart xong.
    out.height=panes.reduce((sum,p)=>sum+p.canvas.height,0);
    const ctx=out.getContext('2d');
    ctx.fillStyle='#ffffff';ctx.fillRect(0,0,out.width,out.height);
    let y=0;
    panes.forEach(p=>{
      ctx.drawImage(p.canvas,0,y);
      y+=p.canvas.height;
    });
    if(DOM.liteDrawCanvas){
      const mainCanvas=panes[0].canvas;
      ctx.drawImage(DOM.liteDrawCanvas,0,0,DOM.liteDrawCanvas.width,DOM.liteDrawCanvas.height,0,0,mainCanvas.width,mainCanvas.height);
    }
    // Title/badge/giá phóng to nằm đè trên pane main nên phải vẽ SAU khi drawImage pane main, theo
    // đúng thứ tự DOM thật. Đo khung #lite-chart 1 lần, dùng chung cho cả 3 lần vẽ overlay bên dưới.
    const frameRect=DOM.liteChart.getBoundingClientRect();
    if(finalTitleSegments.length){
      _liteDrawTitleOverlay(ctx,finalTitleSegments,frameRect,dpr);
    }
    if(hasSigBadge){
      _liteDrawSignalBadge(ctx,frameRect,dpr);
    }
    _liteDrawBigPrice(ctx,panes[0].canvas.width/2,frameRect,dpr);
    // Mã hoá đồng bộ trong cùng lượt click để ClipboardItem nhận Blob PNG thật (không phải Promise), giữ user-gesture.
    const pngBlob=_litePngBlobFromDataUrl(out.toDataURL('image/png'));
    if(typeof navigator.clipboard?.write==='function'&&window.ClipboardItem){
      try{
        await navigator.clipboard.write([new ClipboardItem({'image/png':pngBlob})]);
        _liteCopyFeedback(btn,'copied');
        return;
      }catch(e){
        console.warn('Copy bitmap vào clipboard lỗi, chuyển sang tải ảnh PNG:',e);
      }
    }
    _liteDownloadChartImage(pngBlob);
    _liteCopyFeedback(btn,'downloaded');
  }catch(e){console.error('copyLiteChartImage lỗi:',e);_liteCopyFeedback(btn,'failed');}
}
function bindLiteDrawToolbar(){
  resizeLiteDrawCanvas();
  _liteDrawColor=_liteLSGet(LITE_DRAW_COLOR_KEY,'#1a56db');
  if(DOM.liteDrawColor)DOM.liteDrawColor.value=_liteDrawColor;
  _liteTextSize=parseInt(_liteLSGet(LITE_TEXT_SIZE_KEY,'13'),10)||13;
  _liteTextFont=_liteLSGet(LITE_TEXT_FONT_KEY,'mono');
  _liteTextBg=_liteLSGet(LITE_TEXT_BG_KEY,'');
  DOM.liteDrawToolbar?.addEventListener('click',e=>{
    const btn=e.target.closest('.lite-draw-btn');if(!btn)return;
    if(btn===DOM.liteDrawUndo){_liteDrawings.pop();_liteSelectedId=null;saveLiteDrawings();redrawLiteDrawings();return;}
    if(btn===DOM.liteDrawClear){if(_liteDrawings.length&&confirm('Xóa tất cả hình vẽ trên chart này?')){_liteDrawings=[];_liteSelectedId=null;saveLiteDrawings();redrawLiteDrawings();}return;}
    const tool=btn.dataset.tool;if(tool)setLiteDrawTool(tool);
  });
  DOM.liteDrawColor?.addEventListener('input',()=>{
    _liteDrawColor=DOM.liteDrawColor.value;
    _liteLSSet(LITE_DRAW_COLOR_KEY,_liteDrawColor);
    const sel=_liteGetSelectedShape();
    if(sel&&sel.type!=='position'){sel.color=_liteDrawColor;saveLiteDrawings();redrawLiteDrawings();}
  });
  DOM.liteShapeColor?.addEventListener('input',()=>{
    const sel=_liteGetSelectedShape();
    if(sel&&sel.type!=='position'){
      sel.color=DOM.liteShapeColor.value;
      _liteDrawColor=DOM.liteShapeColor.value;
      if(DOM.liteDrawColor)DOM.liteDrawColor.value=_liteDrawColor;
      _liteLSSet(LITE_DRAW_COLOR_KEY,_liteDrawColor);
      saveLiteDrawings();redrawLiteDrawings();
    }
  });
  DOM.liteShapeTargetColor?.addEventListener('input',()=>{
    const sel=_liteGetSelectedShape();
    if(sel&&sel.type==='position'){
      sel.targetColor=DOM.liteShapeTargetColor.value;
      saveLiteDrawings();redrawLiteDrawings();
    }
  });
  DOM.liteShapeTarget2?.addEventListener('click',()=>{
    const sel=_liteGetSelectedShape();
    if(!sel||sel.type!=='position')return;
    if(Number.isFinite(sel.target2P)){
      // Đang bật → tắt: Target 1 biến mất, Target 2 (giá gốc) trở lại thành Target duy nhất.
      sel.points[1]={...sel.points[1],p:sel.target2P};
      delete sel.target2P;
    }else{
      // Đang tắt → bật: Target hiện có đổi thành Target 2; Target 1 chèn giữa Entry và Target 2.
      const entryP=sel.points[0].p,oldTargetP=sel.points[1].p;
      sel.target2P=oldTargetP;
      sel.points[1]={...sel.points[1],p:entryP+(oldTargetP-entryP)*0.5};
    }
    saveLiteDrawings();redrawLiteDrawings();_liteUpdateFloatingBar();
  });
  DOM.liteShapeFontSize?.addEventListener('change',()=>{
    const size=parseInt(DOM.liteShapeFontSize.value,10)||13;
    _liteTextSize=size;
    _liteLSSet(LITE_TEXT_SIZE_KEY,String(size));
    const sel=_liteGetSelectedShape();
    if(sel&&sel.type==='text'){sel.fontSize=size;saveLiteDrawings();redrawLiteDrawings();_liteUpdateFloatingBar();}
  });
  DOM.liteShapeFontFamily?.addEventListener('change',()=>{
    const fam=DOM.liteShapeFontFamily.value||'mono';
    _liteTextFont=fam;
    _liteLSSet(LITE_TEXT_FONT_KEY,fam);
    const sel=_liteGetSelectedShape();
    if(sel&&sel.type==='text'){sel.fontFamily=fam;saveLiteDrawings();redrawLiteDrawings();_liteUpdateFloatingBar();}
  });
  DOM.liteShapeBgColor?.addEventListener('input',()=>{
    const bg=DOM.liteShapeBgColor.value;
    _liteTextBg=bg;
    _liteLSSet(LITE_TEXT_BG_KEY,bg);
    const sel=_liteGetSelectedShape();
    if(sel&&sel.type==='text'){sel.bg=bg;saveLiteDrawings();redrawLiteDrawings();}
  });
  DOM.liteShapeBgClear?.addEventListener('click',()=>{
    _liteTextBg='';
    _liteLSSet(LITE_TEXT_BG_KEY,'');
    const sel=_liteGetSelectedShape();
    if(sel&&sel.type==='text'){sel.bg=null;saveLiteDrawings();redrawLiteDrawings();}
  });
  DOM.liteShapeEdit?.addEventListener('click',()=>{
    const sel=_liteGetSelectedShape();
    if(sel&&sel.type==='text'){setLiteDrawTool('text');_liteOpenTextInput(sel.points[0],null,sel);}
  });
  DOM.liteDrawCopy?.addEventListener('click',e=>{
    e.preventDefault();
    e.stopPropagation();
    copyLiteChartImage(e.currentTarget);
  });
  DOM.liteFireantBtn?.addEventListener('click',e=>{
    e.preventDefault();
    e.stopPropagation();
    const sym=_liteSymbol||_sym||'VNINDEX';
    openChart(sym,'24h'); // chỉ nút này mở thẳng tab Fireant; các nơi khác gọi openChart(sym) vẫn mặc định Vietstock
  });
  if(DOM.liteTextInput){
    DOM.liteTextInput.addEventListener('keydown',e=>{
      // Chặn nổi bọt phím tắt khác khi đang gõ chữ; Enter xuống dòng, Ctrl/Cmd+Enter chốt, Escape huỷ.
      e.stopPropagation();
      if(e.key==='Enter'&&(e.ctrlKey||e.metaKey)){
        e.preventDefault();
        _liteCommitTextInput();
        setLiteDrawTool('cursor');
      }else if(e.key==='Escape'){
        e.preventDefault();
        _liteCloseTextInput();
        setLiteDrawTool('cursor');
      }
    });
    DOM.liteTextInput.addEventListener('input',()=>{
      DOM.liteTextInput.style.height='auto';
      DOM.liteTextInput.style.height=DOM.liteTextInput.scrollHeight+'px';
    });
    DOM.liteTextInput.addEventListener('pointerdown',e=>e.stopPropagation());
    DOM.liteTextInput.addEventListener('blur',()=>{
      if(_liteTextEditPos){_liteCommitTextInput();setLiteDrawTool('cursor');}
    });
  }
  DOM.liteShapeDash?.addEventListener('click',()=>{
    const sel=_liteGetSelectedShape();
    if(sel&&(sel.type==='trendline'||sel.type==='hline'||sel.type==='vline')){
      sel.dash=!sel.dash;
      saveLiteDrawings();redrawLiteDrawings();
    }
  });
  DOM.liteShapePct?.addEventListener('click',()=>{
    const sel=_liteGetSelectedShape();
    if(sel&&sel.type==='rect'){
      sel.showPct=!sel.showPct;
      saveLiteDrawings();redrawLiteDrawings();_liteUpdateFloatingBar();
    }
  });
  DOM.liteShapeArrowStyle?.addEventListener('click',()=>{
    const sel=_liteGetSelectedShape();
    if(sel&&sel.type==='arrow'){
      sel.wide=!sel.wide;
      saveLiteDrawings();redrawLiteDrawings();_liteUpdateFloatingBar();
    }
  });
  DOM.liteShapeArrowWidth?.addEventListener('change',()=>{
    const sel=_liteGetSelectedShape();
    if(sel&&sel.type==='arrow'){
      sel.arrowW=parseFloat(DOM.liteShapeArrowWidth.value)||2;
      saveLiteDrawings();redrawLiteDrawings();
    }
  });
  DOM.liteShapeZigzagFill?.addEventListener('click',()=>{
    const sel=_liteGetSelectedShape();
    if(sel&&sel.type==='zigzag'){
      sel.noFill=!sel.noFill;
      saveLiteDrawings();redrawLiteDrawings();_liteUpdateFloatingBar();
    }
  });
  DOM.liteShapeDelete?.addEventListener('click',()=>{
    if(_liteSelectedId!=null){
      _liteDrawings=_liteDrawings.filter(d=>d.id!==_liteSelectedId);
      _liteSelectedId=null;saveLiteDrawings();redrawLiteDrawings();
    }
  });
  // Bắt sự kiện ở pha capture để chặn pan/zoom mặc định của thư viện khi người dùng nhắm trúng hình đã vẽ.
  if(DOM.liteChart){
    DOM.liteChart.addEventListener('pointerdown',e=>{
      if(_liteDrawTool!=='cursor'||!DOM.liteDrawCanvas)return;
      const{x,y}=_liteXYFromEvent(e);
      const hit=_liteHitTest(x,y);
      if(!hit){if(_liteSelectedId!=null){_liteSelectedId=null;redrawLiteDrawings();}return;}
      e.preventDefault();e.stopPropagation();
      _liteStartShapeDrag(hit,e);
    },{capture:true});
    DOM.liteChart.addEventListener('pointermove',e=>{
      if(_liteDrawTool!=='cursor'||_liteDragInfo){_liteHideRectTooltip();return;}
      const{x,y}=_liteXYFromEvent(e);
      const hit=_liteHitTest(x,y);
      DOM.liteChart.style.cursor=hit?'move':'';
      // Mỗi lần chuột di chuyển: ẩn tooltip đang hiện (nếu có) và huỷ hẹn giờ cũ — chỉ hiện lại sau khi con trỏ đứng yên đủ LITE_RECT_TOOLTIP_DELAY_MS tại 1 hộp.
      _liteHideRectTooltip();
      if(hit){
        _liteRectTooltipTimer=setTimeout(()=>{_liteRectTooltipTimer=null;_liteShowRectTooltip(hit,x,y);},LITE_RECT_TOOLTIP_DELAY_MS);
      }
    });
    DOM.liteChart.addEventListener('pointerleave',()=>{_liteHideRectTooltip();});
    DOM.liteChart.addEventListener('dblclick',e=>{
      if(_liteDrawTool!=='cursor')return;
      const{x,y}=_liteXYFromEvent(e);
      const hit=_liteHitTest(x,y);
      if(hit&&hit.shape.type==='text'){
        e.preventDefault();e.stopPropagation();
        setLiteDrawTool('text');
        _liteOpenTextInput(hit.shape.points[0],null,hit.shape);
      }
    });
  }
  if(!DOM.liteDrawCanvas)return;
  function _liteOffsetFromChord(pend,p){
    const p0L=_litePtLogical(pend.points[0]),p1L=_litePtLogical(pend.points[1]),pL=_litePtLogical(p);
    const denom=(p1L-p0L)||1e-6;
    const lineP=pend.points[0].p+(pend.points[1].p-pend.points[0].p)*(pL-p0L)/denom;
    return p.p-lineP;
  }
  DOM.liteDrawCanvas.addEventListener('pointermove',e=>{
    if(_liteChannelPending){
      const p=_litePtFromEvent(e);if(!p)return;
      _liteChannelPending.points[2]={offsetPrice:_liteOffsetFromChord(_liteChannelPending,p)};
      redrawLiteDrawings();
      return;
    }
    if(_liteArcPending){
      const p=_litePtFromEvent(e);if(!p)return;
      // Lưu thẳng vị trí chuột làm điểm "đáy" — đáy đi tự do theo chuột cả 2 chiều, không ép về giữa.
      _liteArcPending.points[2]=p;
      redrawLiteDrawings();
      return;
    }
    if(_liteZigzagPending){
      const p=_litePtFromEvent(e);if(!p)return;
      _liteZigzagPending._hover=p;
      redrawLiteDrawings();
      return;
    }
    if(_liteLinePending){
      const p=_litePtFromEvent(e);if(!p)return;
      _liteLinePending.points[1]=p;
      redrawLiteDrawings();
      return;
    }
  });
  DOM.liteDrawCanvas.addEventListener('dblclick',e=>{
    if(_liteDrawTool==='zigzag'&&_liteZigzagPending){
      e.preventDefault();
      if(_liteZigzagPending.points.length>1)_liteZigzagPending.points.pop();
      _liteFinishZigzag();
      setLiteDrawTool('cursor');
    }
  });
  DOM.liteDrawCanvas.addEventListener('pointerdown',e=>{
    if(_liteDrawTool==='cursor')return;
    const p0=_litePtFromEvent(e);if(!p0)return;
    if(_liteDrawTool==='channel'&&_liteChannelPending){
      const pend=_liteChannelPending;
      pend.points[2]={offsetPrice:_liteOffsetFromChord(pend,p0)};
      _liteDrawings.push(pend);_liteChannelPending=null;
      saveLiteDrawings();
      setLiteDrawTool('cursor');_liteSelectShape(pend.id);
      return;
    }
    if(_liteDrawTool==='arc'&&_liteArcPending){
      const pend=_liteArcPending;
      pend.points[2]=p0;
      _liteDrawings.push(pend);_liteArcPending=null;
      saveLiteDrawings();
      setLiteDrawTool('cursor');_liteSelectShape(pend.id);
      return;
    }
    if(_liteDrawTool==='zigzag'){
      if(_liteZigzagPending){
        _liteZigzagPending.points.push(p0);
        redrawLiteDrawings();
      }else{
        _liteZigzagPending={id:_liteDrawSeq++,type:'zigzag',points:[p0],color:_liteDrawColor};
        redrawLiteDrawings();
      }
      return;
    }
    if(_liteDrawTool==='text'){
      if(_liteTextEditPos){
        _liteCommitTextInput();
        setLiteDrawTool('cursor');
        return;
      }
      _liteOpenTextInput(p0,e);
      return;
    }
    if(_liteDrawTool==='hline'){
      const id=_liteDrawSeq++;
      _liteDrawings.push({id,type:'hline',points:[p0,p0],color:_liteDrawColor});
      saveLiteDrawings();
      setLiteDrawTool('cursor');_liteSelectShape(id);
      return;
    }
    if(_liteDrawTool==='vline'){
      const id=_liteDrawSeq++;
      _liteDrawings.push({id,type:'vline',points:[p0,p0],color:_liteDrawColor});
      saveLiteDrawings();
      setLiteDrawTool('cursor');_liteSelectShape(id);
      return;
    }
    if(_liteDrawTool==='trendline'||_liteDrawTool==='rect'||_liteDrawTool==='arrow'||_liteDrawTool==='channel'||_liteDrawTool==='arc'){
      if(_liteLinePending&&_liteLinePending.type===_liteDrawTool){
        _liteLinePending.points[1]=p0;
        const moved=Math.abs(p0.l-_liteLinePending.points[0].l)>0.4||Math.abs(p0.p-_liteLinePending.points[0].p)>1e-9;
        if(moved){
          if(_liteDrawTool==='channel'){
            _liteChannelPending=_liteLinePending;_liteLinePending=null;
            redrawLiteDrawings();
          }else if(_liteDrawTool==='arc'){
            _liteArcPending=_liteLinePending;_liteLinePending=null;
            redrawLiteDrawings();
          }else{
            _liteDrawings.push(_liteLinePending);
            const newId=_liteLinePending.id;
            _liteLinePending=null;
            saveLiteDrawings();
            setLiteDrawTool('cursor');_liteSelectShape(newId);
          }
        }else{
          _liteLinePending=null;
          redrawLiteDrawings();
        }
        return;
      }
      _liteLinePending={id:_liteDrawSeq++,type:_liteDrawTool,points:[p0,p0],color:_liteDrawColor};
      redrawLiteDrawings();
      return;
    }
    _liteDrawActive={id:_liteDrawSeq++,type:_liteDrawTool,points:[p0,p0],color:_liteDrawTool==='position'?'#111827':_liteDrawColor};
    const move=ev=>{
      const p1=_litePtFromEvent(ev);if(!p1||!_liteDrawActive)return;
      _liteDrawActive.points[1]=p1;
      if(_liteDrawActive.type==='position'){
        const entryP=_liteDrawActive.points[0].p,targetP=p1.p;
        const dir=targetP>=entryP?1:-1;
        _liteDrawActive.stopP=entryP-dir*entryP*0.07;
      }
      redrawLiteDrawings();
    };
    const up=ev=>{
      window.removeEventListener('pointermove',move);
      window.removeEventListener('pointerup',up);
      const p1=_litePtFromEvent(ev)||_liteDrawActive.points[1];
      _liteDrawActive.points[1]=p1;
      const moved=Math.abs(p1.l-_liteDrawActive.points[0].l)>0.4||Math.abs(p1.p-_liteDrawActive.points[0].p)>1e-9;
      if(_liteDrawActive.type==='position'){
        const entryP=_liteDrawActive.points[0].p,targetP=_liteDrawActive.points[1].p;
        const dir=targetP>=entryP?1:-1;
        _liteDrawActive.stopP=entryP-dir*entryP*0.07;
      }
      if(moved){
        _liteDrawings.push(_liteDrawActive);
        const newId=_liteDrawActive.id;
        _liteDrawActive=null;
        saveLiteDrawings();
        setLiteDrawTool('cursor');_liteSelectShape(newId);
      }else{
        _liteDrawActive=null;
        redrawLiteDrawings();
      }
    };
    window.addEventListener('pointermove',move);
    window.addEventListener('pointerup',up);
  });
}
function resizeLiteSearchInput(){
  if(!DOM.liteChartSearch)return;
  const n=Math.max(1,DOM.liteChartSearch.value.length);
  DOM.liteChartSearch.style.width=`${Math.min(120,Math.max(42,26+n*16))}px`;
}
// Mở ô tìm mã không pre-fill ký tự — để IME tự gõ sau khi focus, tránh nhân đôi ký tự tiếng Việt.
function openLiteSearch(){
  if(!DOM.liteChartSearch.classList.contains('on'))DOM.liteChartSearch.value='';
  DOM.liteChartSearch.classList.add('on');
  DOM.liteChartSearch.focus();
}
// Phím đơn (không Ctrl/Alt/Meta) ngoài input mở ô tìm mã; không preventDefault() để IME nhận ký tự.
function _liteTryOpenSearchOnKey(e){
  if(e.metaKey||e.ctrlKey||e.altKey||e.key.length!==1||!/^[a-zA-Z0-9]$/.test(e.key))return false;
  if(_liteTextEditPos||document.activeElement?.isContentEditable)return false;
  const tag=(document.activeElement?.tagName||'').toLowerCase();
  if(tag==='input'||tag==='textarea'||tag==='select')return false;
  openLiteSearch();
  return true;
}
function _liteTryDesktopTfShortcut(e){
  if(!window.matchMedia('(min-width:769px)').matches)return false;
  if(!e.shiftKey||e.metaKey||e.ctrlKey||e.altKey)return false;
  if(_liteTextEditPos||document.activeElement?.isContentEditable)return false;
  const tag=(document.activeElement?.tagName||'').toLowerCase();
  if(tag==='input'||tag==='textarea'||tag==='select')return false;
  
  const key=String(e.key||'').toLowerCase();
  if(key==='q'){
    e.preventDefault();
    e.stopPropagation();
    _liteDrawings=[]; _liteSelectedId=null;
    _liteChannelPending=null; _liteArcPending=null; _liteZigzagPending=null; _liteLinePending=null;
    saveLiteDrawings(); redrawLiteDrawings();
    DOM.liteChartFrame?.focus();
    return true;
  }
  
  const tfMap={d:'1D',w:'1W',e:'1M'};
  const tf=tfMap[key];
  if(!tf)return false;
  e.preventDefault();
  e.stopPropagation();
  applyLiteTf(tf);
  DOM.liteChartFrame?.focus();
  return true;
}
// _liteUpdateIndicatorData: bản nhẹ của renderLiteIndicators() cho lazy-load — chỉ update dữ liệu series có sẵn, không destroy/recreate, tránh giật.
function _liteUpdateIndicatorData(){
  if(!_liteChart)return;
  _liteIndicatorSeries.forEach(s=>{
    if(s.kind==='ma')s.series.setData(_sma(_liteData,s.period));
    else if(s.kind==='ema')s.series.setData(_ema(_liteData,s.period));
    else if(s.kind==='bb-upper'||s.kind==='bb-mid'||s.kind==='bb-lower'){/* handled below */}
  });
  // BB: tính 1 lần, cập nhật 3 series
  const bbEntry=_liteIndicatorSeries.find(s=>s.kind==='bb-upper');
  if(bbEntry){
    const bb=_bbands(_liteData,20,2);
    _liteIndicatorSeries.find(s=>s.kind==='bb-upper').series.setData(bb.upper);
    _liteIndicatorSeries.find(s=>s.kind==='bb-mid').series.setData(bb.mid);
    _liteIndicatorSeries.find(s=>s.kind==='bb-lower').series.setData(bb.lower);
    _liteBBFillData={upper:bb.upper,lower:bb.lower,color:_liteIndColors.bb};
  }
  // Trend cloud
  if(_liteTrendFillData){
    _liteTrendFillData=_trendCloudData(_liteData,LITE_TREND_PERIOD,LITE_TREND_MULT,_liteTrendMode());
  }
  // RSI
  if(_liteRsiCrosshairSeries){
    const rsiAligned=alignLiteSeries(_rsi(_liteData,LITE_RSI_PERIOD));
    _liteRsiCrosshairSeries.setData(rsiAligned);
    // RSI các đường nằm ngang (70/50/30/band) không đổi theo số nến — bỏ qua, tiết kiệm CPU.
  }
  // MACD
  if(_liteMacdCrosshairSeries){
    const m=_macd(_liteData);
    // Tìm series histogram và signal line trong MACD panel
    const macdPanelSeries=_liteIndicatorSeries.filter(s=>s.chart===_liteMacdChart);
    if(macdPanelSeries.length>=3){
      const histScaled=alignLiteSeries(m.hist).map(x=>x&&Number.isFinite(x.value)?{...x,value:x.value*LITE_HIST_SCALE}:x);
      macdPanelSeries[0].series.setData(histScaled);
      macdPanelSeries[1].series.setData(alignLiteSeries(m.macd));
      macdPanelSeries[2].series.setData(alignLiteSeries(m.signal));
    }
  }
  // Volume: cập nhật data màu (giữ đúng VPA)
  const showVpaVol=_liteChecked('signalgrp_on')&&_liteChecked('volcolor');
  _liteRefreshVolumeTop(showVpaVol);
  redrawLiteDrawings();
}
function renderLiteIndicators(skipRangeRestore,explicitRange,skipPaneLayout){
  if(!_liteChart||!_liteRsiChart||!_liteMacdChart)return;
  // explicitRange cho phép truyền range đã chốt trước đó, tránh đọc range hiện tại có thể đã bị thư viện tự dịch.
  const prevRange=skipRangeRestore?null:(explicitRange!==undefined?explicitRange:_liteGetVisibleLogicalRange());
  _clearLiteIndicators();
  // Đọc trạng thái checkbox đúng 1 lần/chỉ báo (thay vì querySelector lại lần 2 lúc setData bên dưới).
  const showRsi=_liteChecked('rsi');
  const showMacd=_liteChecked('macd');
  const maEmaOn=_liteChecked('maema_on');
  const maOn=maEmaOn?LITE_MA_PERIODS.filter(p=>_liteChecked('ma'+p)):[];
  const emaOn=maEmaOn?LITE_EMA_PERIODS.filter(p=>_liteChecked('ema'+p)):[];
  const bbOn=_liteChecked('bb');
  const trendOn=_liteChecked('trend');
  const showVpaVol=_liteChecked('signalgrp_on')&&_liteChecked('volcolor');
  // skipPaneLayout: bỏ applyLitePaneLayout() khi layout không đổi (tránh thư viện tự canh lại view, gây "nhảy chart" mỗi 10s).
  // skipWidthSync=true: hàm tự đồng bộ trục giá ở cuối, khỏi cần applyLitePaneLayout() làm trước 1 lượt vô nghĩa.
  if(!skipPaneLayout)applyLitePaneLayout(true);
  // Không cần applyOptions margin cho _liteVolume ở đây — _liteRefreshVolumeTop() tạo lại series và tự set margin, sẽ ghi đè ngay.
  maOn.forEach(p=>{
    _liteIndicatorSeries.push({chart:_liteChart,kind:'ma',period:p,series:_liteChart.addLineSeries({color:_liteIndColors['ma'+p],lineWidth:1,title:'',priceLineVisible:false,lastValueVisible:true,crosshairMarkerVisible:false})});
  });
  emaOn.forEach(p=>{
    _liteIndicatorSeries.push({chart:_liteChart,kind:'ema',period:p,series:_liteChart.addLineSeries({color:_liteIndColors['ema'+p],lineWidth:1,title:'',priceLineVisible:false,lastValueVisible:true,crosshairMarkerVisible:false})});
  });
  if(bbOn){
    // Chỉ vẽ 3 đường BB bằng series thật; phần tô màu giữa 2 đường vẽ riêng bằng canvas (_liteDrawBBBand) để clip chính xác.
    const bbCol=_liteIndColors.bb;
    _liteIndicatorSeries.push({chart:_liteChart,kind:'bb-upper',series:_liteChart.addLineSeries({
      color:_liteHexToRgba(bbCol,.85),lineWidth:1,
      title:'',priceLineVisible:false,lastValueVisible:false,crosshairMarkerVisible:false
    })});
    _liteIndicatorSeries.push({chart:_liteChart,kind:'bb-mid',series:_liteChart.addLineSeries({
      color:_liteHexToRgba(bbCol,.4),lineWidth:1,lineStyle:LightweightCharts.LineStyle.Dashed,
      title:'',priceLineVisible:false,lastValueVisible:false,crosshairMarkerVisible:false
    })});
    _liteIndicatorSeries.push({chart:_liteChart,kind:'bb-lower',series:_liteChart.addLineSeries({
      color:_liteHexToRgba(bbCol,.85),lineWidth:1,
      title:'',priceLineVisible:false,lastValueVisible:false,crosshairMarkerVisible:false
    })});
  }
  _liteIndicatorSeries.forEach(s=>{
    if(s.kind==='ma')s.series.setData(_sma(_liteData,s.period));
    else if(s.kind==='ema')s.series.setData(_ema(_liteData,s.period));
  });
  if(bbOn){
    const bb=_bbands(_liteData,20,2);
    _liteIndicatorSeries.find(s=>s.kind==='bb-upper').series.setData(bb.upper);
    _liteIndicatorSeries.find(s=>s.kind==='bb-mid').series.setData(bb.mid);
    _liteIndicatorSeries.find(s=>s.kind==='bb-lower').series.setData(bb.lower);
    _liteBBFillData={upper:bb.upper,lower:bb.lower,color:_liteIndColors.bb};
  }else{
    _liteBBFillData=null;
  }
  if(trendOn){
    // Không dùng series đường kẻ — tô vùng (cloud) bám theo giá bằng canvas, xem _liteDrawTrendCloud.
    _liteTrendFillData=_trendCloudData(_liteData,LITE_TREND_PERIOD,LITE_TREND_MULT,_liteTrendMode());
  }else{
    _liteTrendFillData=null;
  }
  if(showRsi){
    const rsiCol=_liteIndColors.rsi||LITE_RSI_DEFAULT_COLOR;
    const rsiFill=_liteHexToRgba(rsiCol,.12);
    const rsiBand=_liteRsiChart.addBaselineSeries({
      priceScaleId:'right',baseValue:{type:'price',price:30},
      topFillColor1:rsiFill,topFillColor2:rsiFill,
      bottomFillColor1:'rgba(0,0,0,0)',bottomFillColor2:'rgba(0,0,0,0)',
      topLineColor:'rgba(0,0,0,0)',bottomLineColor:'rgba(0,0,0,0)',lineWidth:1,
      lastValueVisible:false,priceLineVisible:false,crosshairMarkerVisible:false
    });
    const rsiSeries=_liteRsiChart.addLineSeries({
      priceScaleId:'right',color:_liteHexToRgba(rsiCol,.88),lineWidth:1,
      title:'',priceLineVisible:false,lastValueVisible:true,crosshairMarkerVisible:false
    });
    const bounds20=_liteRsiChart.addLineSeries({priceScaleId:'right',color:'rgba(0,0,0,0)',lineVisible:false,lastValueVisible:false,priceLineVisible:false,crosshairMarkerVisible:false});
    const bounds80=_liteRsiChart.addLineSeries({priceScaleId:'right',color:'rgba(0,0,0,0)',lineVisible:false,lastValueVisible:false,priceLineVisible:false,crosshairMarkerVisible:false});
    const level70=_liteRsiChart.addLineSeries({priceScaleId:'right',color:'rgba(107,114,128,.55)',lineWidth:1,lineStyle:LightweightCharts.LineStyle.Dashed,title:'',priceLineVisible:false,lastValueVisible:false,crosshairMarkerVisible:false});
    const level50=_liteRsiChart.addLineSeries({priceScaleId:'right',color:'rgba(107,114,128,.45)',lineWidth:1,lineStyle:LightweightCharts.LineStyle.Dashed,title:'',priceLineVisible:false,lastValueVisible:false,crosshairMarkerVisible:false});
    const level30=_liteRsiChart.addLineSeries({priceScaleId:'right',color:'rgba(107,114,128,.55)',lineWidth:1,lineStyle:LightweightCharts.LineStyle.Dashed,title:'',priceLineVisible:false,lastValueVisible:false,crosshairMarkerVisible:false});
    const rsiAligned=alignLiteSeries(_rsi(_liteData,LITE_RSI_PERIOD));
    const constLine=value=>_liteData.map(bar=>({time:bar.time,value}));
    rsiBand.setData(constLine(70));
    rsiSeries.setData(rsiAligned);
    bounds20.setData(constLine(20));
    bounds80.setData(constLine(80));
    level70.setData(constLine(70));
    level50.setData(constLine(50));
    level30.setData(constLine(30));
    _liteRsiCrosshairSeries=rsiSeries;
    _liteIndicatorSeries.push(
      {chart:_liteRsiChart,series:rsiBand},
      {chart:_liteRsiChart,series:bounds20},
      {chart:_liteRsiChart,series:bounds80},
      {chart:_liteRsiChart,series:level70},
      {chart:_liteRsiChart,series:level50},
      {chart:_liteRsiChart,series:level30},
      {chart:_liteRsiChart,series:rsiSeries}
    );
  }
  if(showMacd){
    const m=_macd(_liteData);
    const hist=_liteMacdChart.addHistogramSeries({priceFormat:{type:'price',precision:2,minMove:.01},priceScaleId:'right',base:0,lastValueVisible:false,priceLineVisible:false});
    const macdLine=_liteMacdChart.addLineSeries({priceScaleId:'right',color:'rgba(59,130,246,.6)',lineWidth:1,title:'',priceLineVisible:false,lastValueVisible:true,crosshairMarkerVisible:false});
    const sigLine=_liteMacdChart.addLineSeries({priceScaleId:'right',color:'orange',lineWidth:1,title:'',priceLineVisible:false,lastValueVisible:true,crosshairMarkerVisible:false});
    const histScaled=alignLiteSeries(m.hist).map(x=>x&&Number.isFinite(x.value)?{...x,value:x.value*LITE_HIST_SCALE}:x);
    const macdAligned=alignLiteSeries(m.macd);
    hist.setData(histScaled);macdLine.setData(macdAligned);sigLine.setData(alignLiteSeries(m.signal));
    _liteMacdCrosshairSeries=macdLine;
    _liteIndicatorSeries.push({chart:_liteMacdChart,series:hist},{chart:_liteMacdChart,series:macdLine},{chart:_liteMacdChart,series:sigLine});
  }
  _liteRefreshVolumeTop(showVpaVol);
  if(!_liteApplyVisibleLogicalRange(prevRange))setLiteRightOffset();
  redrawLiteDrawings();
  // Dữ liệu 3 trục vừa đổi — applyLitePaneLayout() đã reset minimumWidth, phải đo+đồng bộ lại sau khi setData xong.
  _liteSyncPriceScaleWidths();
}
function _liteVolColorFor(volBar,showVpa){
  // Checkbox volcolor BẬT dùng màu VPA server tính; TẮT dùng màu xanh/đỏ mặc định theo close/open. showVpa đọc DOM 1 lần/lượt vẽ để tránh querySelector lặp lại theo từng bar.
  if(showVpa)return volBar.color;
  const cd=_liteDataByTime.get(liteTimeKey(volBar.time));
  return cd?(cd.close>=cd.open?LITE_CANDLE_UP_COLOR:LITE_CANDLE_DOWN_COLOR):volBar.color;
}
function _liteRefreshVolumeTop(showVpaVol){
  if(!_liteChart||!_liteVolume)return;
  _liteVolume.setData(_liteVolumeData.map(v=>({...v,color:_liteVolColorFor(v,showVpaVol)})));
}
// LITE_CHART_RETRY_MAX/DELAY: số lần và khoảng cách thử lại khi API lightweight_chart lỗi trước khi báo hết dữ liệu.
const LITE_CHART_RETRY_MAX=6,LITE_CHART_RETRY_DELAY=4000;
let _liteReqId=0;
async function loadLiteChart(sym='FPT',retry=LITE_CHART_RETRY_MAX,skipPopoutSync=false){
  const s=(sym||'FPT').toUpperCase().trim(),reqId=++_liteReqId;
  _liteSymbol=s;
  _updateVietstockIframeIfActive(s);
  if(!DOM.liteChart)return;
  initLiteChart();
  // Không xoá DOM.liteChartInput.value ở đây — trình duyệt có thể fire lại 'input'; ô input tự clear sau khi loadLiteChart xong.
  if(DOM.liteChartTitle)DOM.liteChartTitle.textContent=window.LightweightCharts?'Đang tải...':'Thiếu thư viện chart';
  DOM.liteChartEmpty.textContent=window.LightweightCharts?'Đang tải chart...':'Không tải được Lightweight Charts';
  DOM.liteChartEmpty.style.display='flex';
  if(!window.LightweightCharts){
    if(retry>0)setTimeout(()=>loadLiteChart(s,retry-1,skipPopoutSync),1200);
    return;
  }
  _liteChartLoading=true;
  if(_liteDrawCtx&&DOM.liteChart)_liteDrawCtx.clearRect(0,0,DOM.liteChart.clientWidth,DOM.liteChart.clientHeight);
  try{
    const _pf=window.__liteChartPrefetch;
    const r=(_pf&&_pf.sym===s&&_pf.tf===_liteTf)
      ?(window.__liteChartPrefetch=null,await _pf.promise)
      :await fetch('/api/lightweight_chart/'+encodeURIComponent(s)+'?tf='+encodeURIComponent(_liteTf)+'&limit=450');
    if(!r.ok)throw new Error('vndirect_unavailable');
    if(reqId!==_liteReqId)return;
    const j=await r.json();
    if(reqId!==_liteReqId)return;
    setLiteTf(j.timeframe||_liteTf);
    _liteLSSet(LITE_LAST_SYMBOL_KEY,s);
    _lgUpdateChartFavBtn();
    if(!skipPopoutSync){
      _chartSyncChannel?.postMessage({type:'CHART_POPOUT_SYNC',symbol:s});
      if(window.parent&&window.parent!==window)window.parent.postMessage({type:'CHART_EMBED_SYM_CHANGE',symbol:s},'*');
    }
	    if(DOM.liteAlertSymbol)DOM.liteAlertSymbol.value=_liteSymbol;
	    _liteRsScore=Number.isFinite(Number(j.rs))?Number(j.rs):null;
	    // Tính pct inline, gán trực tiếp — tránh tạo 450 object spread mới
    const rawCandles=j.candles||[];
    _liteData=new Array(rawCandles.length);
    for(let i=0;i<rawCandles.length;i++){
      const bar=rawCandles[i];
      bar.pct=i>0?((bar.close-rawCandles[i-1].close)/rawCandles[i-1].close*100):0;
      _liteData[i]=bar;
    }
    _liteDataByTime=new Map();for(let i=0;i<_liteData.length;i++){const b=_liteData[i];_liteDataByTime.set(liteTimeKey(b.time),b);}
    // Khởi động trạng thái lazy-load: server báo has_more=true khi còn lịch sử cũ có thể load thêm
    _liteHasMore=j.has_more!==false;
    _liteOldestDate=_liteData.length?liteTimeKey(_liteData[0].time):null;
    _liteLoadingMore=false;
    _liteVolumeData=j.volume||[];
    _liteCandle.setData(_liteData);
    _liteUpdateWhitespace(true);
    setLiteRightOffset();
    renderLiteIndicators(true);
    if(DOM.liteChartInput)DOM.liteChartInput.value='';
    DOM.liteChartEmpty.style.display='none';
    updateLiteTitle(_liteData[_liteData.length-1]);
    _liteVolForecast=null;
    updateLiteBigPrice(_liteData[_liteData.length-1]);
    _liteFetchVolForecast(_liteSymbol);
    _liteHistorySignals=j.history_signals||[];
    _liteCurrentSignal=j.signal||null;
    _liteApplyBuySignal(_liteCurrentSignal);
    loadLiteDrawings();resizeLiteDrawCanvas();
  }catch(e){
    if(DOM.liteChartTitle)DOM.liteChartTitle.textContent='Không có dữ liệu';
    updateLiteBigPrice(null);
    DOM.liteChartEmpty.textContent='Không lấy được dữ liệu VNDirect cho '+s;
    if(retry>0)setTimeout(()=>loadLiteChart(s,retry-1,skipPopoutSync),LITE_CHART_RETRY_DELAY);
  }finally{
    _liteChartLoading=false;
    redrawLiteDrawings();
  }
}
// AUTO-REFRESH CHART — chỉ vá cây nến cuối (series.update(), không setData() lại toàn bộ) nên không nháy màn hình/mất zoom/pan.
const LITE_CHART_AUTOREFRESH_SEC=5;
let _liteQuietRefreshing=false;
async function _liteQuietRefreshChart(){
  if(_liteChartLoading||_liteQuietRefreshing)return;                          // đang load mã mới hoặc lượt trước chưa xong
  if(!_isChartPanelOpen&&!_isChartPopoutWindow)return;
  if(!_liteChart||!_liteCandle||!_liteVolume||!_liteData.length)return;
  if(document.hidden)return;
  if(_liteDrawTool!=='cursor')return;
  const sym=_liteSymbol,tf=_liteTf;
  _liteQuietRefreshing=true;
  try{
    const r=await fetch('/api/lightweight_chart/'+encodeURIComponent(sym)+'?tf='+encodeURIComponent(tf)+'&limit=10&nocache=1');
    if(!r.ok)return;
    const j=await r.json();
    if(sym!==_liteSymbol||tf!==_liteTf||!j.candles||!j.candles.length)return;
    _liteRsScore=Number.isFinite(Number(j.rs))?Number(j.rs):null;
    const rawBar=j.candles[j.candles.length-1];
    const key=liteTimeKey(rawBar.time);
    const isNewBar=!_liteDataByTime.has(key);
    const prevBar=isNewBar?_liteData[_liteData.length-1]:(_liteData.length>1?_liteData[_liteData.length-2]:null);
    rawBar.pct=prevBar?((rawBar.close-prevBar.close)/prevBar.close*100):0;
    const prevRangeBeforeUpdate=_liteGetVisibleLogicalRange();
    if(isNewBar)_liteData.push(rawBar);else _liteData[_liteData.length-1]=rawBar;
    _liteDataByTime.set(key,rawBar);
    _liteCandle.update(rawBar);
    const rawVol=(j.volume||[]).find(v=>liteTimeKey(v.time)===key);
    if(rawVol){
      if(isNewBar)_liteVolumeData.push(rawVol);else _liteVolumeData[_liteVolumeData.length-1]=rawVol;
    }
    if(isNewBar)_liteUpdateWhitespace();
    if(isNewBar){
      renderLiteIndicators(false,prevRangeBeforeUpdate,true);
    }else{
      _liteUpdateIndicatorData();
      if(!_liteApplyVisibleLogicalRange(prevRangeBeforeUpdate))setLiteRightOffset();
    }
    updateLiteTitle(_liteData[_liteData.length-1]);
    updateLiteBigPrice(_liteData[_liteData.length-1]);
    _liteFetchVolForecast(sym);
    if(j.history_signals&&j.history_signals.length)_liteHistorySignals=j.history_signals;
    if(j.signal!==undefined)_liteCurrentSignal=j.signal;
    _liteApplyBuySignal(_liteCurrentSignal);
  }catch(e){
  }finally{
    _liteQuietRefreshing=false;
  }
}
// LAZY LOAD LỊCH SỬ — kéo trái tới đầu dữ liệu tự fetch thêm 300 bar cũ hơn và prepend, không nháy màn hình/mất zoom.
async function _liteFetchMoreHistory(){
  if(_liteLoadingMore||!_liteHasMore||!_liteOldestDate||!_liteChart||!_liteCandle)return;
  const sym=_liteSymbol,tf=_liteTf,oldestDate=_liteOldestDate;
  _liteLoadingMore=true;
  try{
    const url='/api/lightweight_chart/'+encodeURIComponent(sym)+'?tf='+encodeURIComponent(tf)+'&limit=300&before='+encodeURIComponent(oldestDate);
    const r=await fetch(url);
    if(!r.ok){_liteHasMore=false;return;}
    const j=await r.json();
    if(sym!==_liteSymbol||tf!==_liteTf)return;
    if(!j.candles||!j.candles.length){_liteHasMore=false;return;}
    _liteHasMore=j.has_more!==false;
    const rawNew=j.candles;
    rawNew.forEach((b,i)=>{b.pct=i>0?((b.close-rawNew[i-1].close)/rawNew[i-1].close*100):0;});
    _liteData=[...rawNew,..._liteData];
    _liteVolumeData=[...(j.volume||[]),..._liteVolumeData];
    if(j.history_signals&&j.history_signals.length)_liteHistorySignals=Array.from(new Set([...j.history_signals,..._liteHistorySignals]));
    _liteDataByTime=new Map(_liteData.map(b=>[liteTimeKey(b.time),b]));
    _liteOldestDate=liteTimeKey(_liteData[0].time);
    const prevRange=_liteChart.timeScale().getVisibleLogicalRange();
    const prependCount=rawNew.length;
    _liteCandle.setData(_liteData);
    _liteUpdateIndicatorData();
    if(prevRange&&Number.isFinite(prevRange.from)&&Number.isFinite(prevRange.to)){
      const target={from:prevRange.from+prependCount,to:prevRange.to+prependCount};
      requestAnimationFrame(()=>{_liteApplyVisibleLogicalRange(target);redrawLiteDrawings();});
    }else{
      setLiteRightOffset();
      redrawLiteDrawings();
    }
  }catch(e){
  }finally{
    _liteLoadingMore=false;
  }
}
function bindLiteChartControls(){
  loadLiteIndicatorPrefs();
  loadLiteTrendMode();
  bindLiteIndColorPickers();
  bindLiteIndGroupDropdowns();
  bindLiteDrawToolbar();
  const _liteApplyChartInput=_liteBindSymInput(DOM.liteChartInput);
  DOM.liteChartInput?.addEventListener('keydown',e=>{
    if(e.key==='Enter'||e.key===' '){
      e.preventDefault();
      const raw=_liteApplyChartInput();
      loadLiteChart(raw||_liteSymbol,0);
    }
  });
  DOM.liteChartTf?.addEventListener('click',e=>{
    const btn=e.target.closest('.lite-tf-btn');if(!btn)return;
    applyLiteTf(btn.dataset.tf,true);
  });
  // Gắn lên document (không DOM.liteIndicators) + lọc bằng closest — dropdown có thể bị portal ra
  // <body> trên mobile portrait, khi đó sự kiện 'change' không bubble lên tới DOM.liteIndicators nữa.
  document.addEventListener('change',(e)=>{
    if(!(e.target.closest('#lite-indicators')||e.target.closest('.lite-ind-dropdown')))return;
    saveLiteIndicatorPrefs();saveLiteTrendMode();updateLiteIndGroupCounts();
    // 4 checkbox nhóm Signal chỉ ảnh hưởng mũi tên/badge/màu volume/giá phóng to — không cần renderLiteIndicators() đầy đủ.
    const val=e.target?.value;
    if(val==='signal'||val==='volcolor'||val==='signalgrp_on'||val==='bigprice'){
      if(val!=='signal'&&val!=='bigprice')_liteRefreshVolumeTop(_liteChecked('signalgrp_on')&&_liteChecked('volcolor'));
      if(val==='bigprice'||val==='signalgrp_on')updateLiteBigPrice(_liteData&&_liteData.length?_liteData[_liteData.length-1]:null);
      if(val!=='bigprice'){ // bigprice riêng lẻ không đụng tới marker tín hiệu hay hình vẽ tay — khỏi vẽ lại thừa
        _liteApplyBuySignal();
        redrawLiteDrawings(); // renderLiteIndicators() không chạy ở nhánh này nên không ai tự redraw — phải tự gọi
      }
    }else{
      renderLiteIndicators();
      _liteApplyBuySignal();
    }
  });
  DOM.liteChartFrame?.addEventListener('wheel',()=>{_liteUserInteractedZoom=true;},{passive:true});
  DOM.liteChartFrame?.addEventListener('pointerdown',()=>{_liteUserInteractedZoom=true;},{passive:true});
  DOM.liteChartFrame?.addEventListener('click',()=>{
    // Không cướp focus về khung chart khi đang gõ chữ (công cụ Text) — tránh phím gõ sau bị hiểu nhầm thành gõ mã.
    if(_liteTextEditPos)return;
    DOM.liteChartFrame.focus();
  });
  DOM.liteChartFrame?.addEventListener('mouseenter',()=>{
    _litePointerInside=true;
    if(_liteTextEditPos)return;
    const tag=(document.activeElement?.tagName||'').toLowerCase();
    if(tag!=='input'&&tag!=='textarea')DOM.liteChartFrame.focus();
  });
  DOM.liteChartFrame?.addEventListener('mouseleave',()=>{_litePointerInside=false;});
  DOM.liteChartFrame?.addEventListener('keydown',e=>{
    // Đang gõ chữ (công cụ Text) thì bỏ qua phím tắt khung chart; đây là lớp bảo vệ thêm phòng focus chưa kịp chuyển.
    if(_liteTextEditPos)return;
    if((e.key==='Delete'||e.key==='Backspace')&&_liteSelectedId!=null){
      e.preventDefault();
      _liteDrawings=_liteDrawings.filter(d=>d.id!==_liteSelectedId);
      _liteSelectedId=null;saveLiteDrawings();redrawLiteDrawings();
      return;
    }
    // Enter / Space / Escape khi đang vẽ dở Zigzag → KẾT THÚC (chốt) nét vẽ, không phải huỷ.
    if((e.key==='Enter'||e.key===' '||e.key==='Escape')&&_liteZigzagPending){
      e.preventDefault();
      _liteFinishZigzag();
      setLiteDrawTool('cursor');
      return;
    }
    if(e.key==='Escape'){
      if(_liteChannelPending||_liteArcPending||_liteZigzagPending||_liteLinePending||_liteSelectedId!=null){
        _liteChannelPending=null;_liteArcPending=null;_liteZigzagPending=null;_liteLinePending=null;_liteSelectedId=null;redrawLiteDrawings();
      }
      return;
    }
    if(_liteTryDesktopTfShortcut(e))return;
    // stopPropagation() chặn bubble để tránh gọi openLiteSearch() 2 lần cho 1 phím bấm.
    if(_liteTryOpenSearchOnKey(e))e.stopPropagation();
  });
  document.addEventListener('keydown',e=>{
    if(!_litePointerInside||_liteTextEditPos)return;
    const tag=(document.activeElement?.tagName||'').toLowerCase();
    if(tag==='input'||tag==='textarea')return;
    if(e.key==='ArrowLeft'||e.key==='ArrowRight'){
      const ts=_liteChart?.timeScale(),r=ts?.getVisibleLogicalRange();
      if(r&&Number.isFinite(r.from)){
        e.preventDefault();
        ts.setVisibleLogicalRange({from:r.from+(e.key==='ArrowLeft'?-1:1),to:r.to+(e.key==='ArrowLeft'?-1:1)});
        redrawLiteDrawings();
      }
      return;
    }
    if(_liteTryDesktopTfShortcut(e))return;
    _liteTryOpenSearchOnKey(e);
  });
  const _liteApplyChartSearch=_liteBindSymInput(DOM.liteChartSearch);
  DOM.liteChartSearch?.addEventListener('input',resizeLiteSearchInput);
  DOM.liteChartSearch?.addEventListener('keydown',e=>{
    if(e.key==='Escape'){DOM.liteChartSearch.classList.remove('on');DOM.liteChartFrame.focus();}
    if((e.key==='Enter'||e.key===' ')&&DOM.liteChartSearch.value){
      e.preventDefault();
      const raw=_liteApplyChartSearch();
      DOM.liteChartSearch.classList.remove('on');
      loadLiteChart(raw,0);
    }
  });
  DOM.liteMacdResizer?.addEventListener('pointerdown',e=>{
    e.preventDefault();
    const startY=e.clientY,startH=_liteMacdSoloHeight||DOM.liteMacdChart.clientHeight||176;
    // Gộp pointermove bằng rAF thành 1 lần cập nhật layout/frame — applyLitePaneLayout() nặng nên gọi trực tiếp mỗi pointermove sẽ giật.
    let pendingH=null,rafId=null;
    const flush=()=>{
      rafId=null;
      if(pendingH===null)return;
      const prevRange=_liteGetVisibleLogicalRange();
      DOM.liteMacdChart.style.height=pendingH+'px';
      applyLitePaneLayout();
      _liteApplyVisibleLogicalRange(prevRange);
      pendingH=null;
    };
    const move=ev=>{
      pendingH=Math.max(120,Math.min(340,startH-(ev.clientY-startY)));
      _liteMacdSoloHeight=pendingH;
      if(rafId===null)rafId=requestAnimationFrame(flush);
    };
    const up=()=>{
      window.removeEventListener('pointermove',move);
      window.removeEventListener('pointerup',up);
      if(rafId!==null){cancelAnimationFrame(rafId);flush();}
    };
    window.addEventListener('pointermove',move);
    window.addEventListener('pointerup',up);
  });
}
function _getPopupViewport(){
  const left=Number.isFinite(window.screen.availLeft)?window.screen.availLeft:0;
  const top=Number.isFinite(window.screen.availTop)?window.screen.availTop:0;
  const height=Math.max(720,window.screen.availHeight||window.innerHeight||800);
  const width=Math.max(960,window.screen.availWidth||window.innerWidth||1280);
  return{left,top,width,height};
}
function _openMaximizedWindow(url,name,width,height,offsetLeft,offsetTop,extra='',align='right'){
  const box=_getPopupViewport();
  const popupLeft=align==='left'?(box.left+offsetLeft):(box.left+Math.max(0,box.width-width-offsetLeft));
  const features=[
    `left=${popupLeft}`,`top=${box.top+offsetTop}`,`width=${width}`,`height=${height}`,
    'resizable=yes','scrollbars=yes','menubar=no','toolbar=no','location=no','status=no'
  ];
  if(extra)features.push(extra);
  const win=window.open(url,name,features.join(','));
  if(win){
    try{win.moveTo(popupLeft,box.top+offsetTop);}catch(e){}
    try{win.resizeTo(width,height);}catch(e){}
  }
  return win;
}
function _resetPopupChrome(){
  $('popup-phdr').style.display='';
  DOM.mobHdrRow1.style.display='none';
  DOM.mobTabRow.style.display='none';
  DOM.mobHdrLand.style.display='';
  DOM.mobClose.style.display='none';
}
// HEATMAP DATA
const HMAP_COLS=__HMAP_COLS_CONFIG__;
const TS_POOL=__TS_POOL_CONFIG__;
// HEATMAP RENDER
function cellStyle(pct){
  let r,g,b;
  const pos=[[235,248,238],[231,247,234],[225,245,228],[220,243,224],[215,242,220],[205,238,211],[195,235,200],[186,232,193],[178,228,186],[169,224,178],[160,220,170],[154,218,165],[148,216,160]];
  const neg=[[255,232,225],[255,228,221],[255,223,216],[254,216,209],[253,208,201],[252,199,191],[250,190,181],[248,181,172],[246,173,164],[244,166,158],[243,160,153],[242,155,149],[240,150,145]];
  if(pct>=6.5){r=250;g=170;b=225}else if(pct>=0.05){[r,g,b]=pos[Math.min(pos.length-1,Math.floor(pct*2))]}
  else if(pct>-0.05){r=245;g=245;b=200}else if(pct>=-6.5){[r,g,b]=neg[Math.min(neg.length-1,Math.floor(Math.abs(pct)*2))]}
  else{r=175;g=250;b=255}
  return{bg:`rgb(${r},${g},${b})`,fg:(.299*r+.587*g+.114*b)>160?'rgb(30,30,30)':'rgb(15,15,15)'};
}
// Treemap dùng bảng màu riêng: trần/tham chiếu/sàn màu phẳng cố định; còn lại hue cố định xanh lá 142°/đỏ 0°, 13 mức sáng.
function _tmLightness(r,g,b){return(Math.max(r,g,b)+Math.min(r,g,b))/510;}
function _tmHue2Rgb(p,q,t){
  if(t<0)t+=1;if(t>1)t-=1;
  if(t<1/6)return p+(q-p)*6*t;
  if(t<1/2)return q;
  if(t<2/3)return p+(q-p)*(2/3-t)*6;
  return p;
}
function _tmHslToRgb(h,s,l){
  let r,g,b;
  if(s===0){r=g=b=l;}
  else{
    const q=l<0.5?l*(1+s):l+s-l*s,p=2*l-q;
    r=_tmHue2Rgb(p,q,h+1/3);g=_tmHue2Rgb(p,q,h);b=_tmHue2Rgb(p,q,h-1/3);
  }
  return[Math.round(r*255),Math.round(g*255),Math.round(b*255)];
}
const TM_POS_L_MIN=0.714, TM_POS_L_MAX=0.947; // dải sáng gốc của thang xanh (đậm→nhạt theo %)
const TM_NEG_L_MIN=0.755, TM_NEG_L_MAX=0.941; // dải sáng gốc của thang đỏ (đậm→nhạt theo %)
// Pha màu gốc với trắng theo hệ số alpha cố định — tạo "màng mờ" đồng bộ, áp cho cả 3 màu cố định (tím/vàng/xanh dương) để cùng chất liệu với xanh/đỏ.
const TM_VEIL_ALPHA=0.82;
function _tmVeil(r,g,b){
  const a=TM_VEIL_ALPHA;
  return[Math.round(r*a+255*(1-a)),Math.round(g*a+255*(1-a)),Math.round(b*a+255*(1-a))];
}
function treemapCellStyle(pct){
  if(pct>=6.5){const[r,g,b]=_tmVeil(168,85,247);return{bg:`rgb(${r},${g},${b})`,fg:'rgb(255,255,255)'};} // trần: tím
  if(pct>-0.05&&pct<0.05){const[r,g,b]=_tmVeil(242,201,76);return{bg:`rgb(${r},${g},${b})`,fg:'rgb(15,15,15)'};} // tham chiếu: vàng
  if(pct<=-6.5){const[r,g,b]=_tmVeil(59,130,246);return{bg:`rgb(${r},${g},${b})`,fg:'rgb(255,255,255)'};} // sàn: xanh da trời
  const{bg}=cellStyle(pct);
  const m=/rgb\((\d+),\s*(\d+),\s*(\d+)\)/.exec(bg);
  if(!m)return{bg,fg:'rgb(255,255,255)'};
  const l=_tmLightness(+m[1],+m[2],+m[3]);
  let r,g,b;
  if(pct>=0.05){
    // Xanh lá cây thật (hue ~150°): dải lightness hẹp, không quá mờ ở mức % nhỏ
    const t=Math.max(0,Math.min(1,(l-TM_POS_L_MIN)/(TM_POS_L_MAX-TM_POS_L_MIN)));
    const ll=0.42+t*(0.54-0.42);
    [r,g,b]=_tmHslToRgb(150/360,0.58,ll);
  }else{
    // Đỏ thật (hue cố định 0°, không ngả hồng): dải lightness hẹp
    const t=Math.max(0,Math.min(1,(l-TM_NEG_L_MIN)/(TM_NEG_L_MAX-TM_NEG_L_MIN)));
    const ll=0.50+t*(0.62-0.50);
    [r,g,b]=_tmHslToRgb(0,0.68,ll);
  }
  return{bg:`rgb(${r},${g},${b})`,fg:'rgb(255,255,255)'};
}
function avgPct(syms,d){let s=0,c=0;for(const k of syms)if(d[k]){s+=d[k].pct||0;c++;}return c?s/c:0;}
function sortByPct(syms,d){return[...syms].sort((a,b)=>((d[b]||{}).pct||0)-((d[a]||{}).pct||0));}
function fmtP(p){return(!p||p<=0)?'—':(p<100?p.toFixed(2):p.toFixed(1));}
function mkCell(sym,d){
  const e=d[sym]||{},pct=typeof e.pct==='number'?e.pct:0,price=typeof e.price==='number'?e.price:0;
  const{bg,fg}=cellStyle(pct),sign=pct>=0?'+':'';
  return `<div class="hmap-cell" data-sym="${sym}" style="background:${bg};color:${fg}" title="${sym}|${fmtP(price)}|${sign}${pct.toFixed(2)}%"><span class="hc-sym">${sym}</span><span class="hc-price">${fmtP(price)}</span><span class="hc-pct">${sign}${pct.toFixed(1)}%</span></div>`;
}
function mkGroup(name,syms,d){
  const avg=avgPct(syms,d),sign=avg>=0?'+':'',cls=avg>0.05?'pos':avg<-0.05?'neg':'zer';
  return `<div class="hmap-group"><div class="hmap-ghdr"><span class="hmap-gname">${name}</span><span class="hmap-gavg ${cls}">${sign}${avg.toFixed(1)}%</span></div>${sortByPct(syms,d).map(s=>mkCell(s,d)).join('')}</div>`;
}
function mkSectorCol(d){
  const groups=[];
  HMAP_COLS.forEach(cd=>cd.groups.forEach(g=>{if(g.name!=='VN30')groups.push({name:g.name,avg:avgPct(g.syms,d)});}));
  groups.sort((a,b)=>b.avg-a.avg);
  return`<div class="hmap-group hmap-sector-group"><div class="hmap-ghdr"><span class="hmap-gname">NGÀNH NGHỀ</span></div>${groups.slice(0,10).map(g=>{const{bg,fg}=cellStyle(g.avg),sign=g.avg>=0?'+':'';return`<div class="hmap-sector-cell" style="background:${bg};color:${fg}"><span class="hsc-name">${g.name}</span><span class="hsc-pct">${sign}${g.avg.toFixed(1)}%</span></div>`;}).join('')}</div>`;
}
function mkFollowGroup(d){
  if(!FOLLOW.length||!FOLLOW_ON)return'';
  const avg=avgPct(FOLLOW,d),sign=avg>=0?'+':'',cls=avg>0.05?'pos':avg<-0.05?'neg':'zer';
  return `<div class="hmap-group hmap-follow-overlay"><div class="hmap-ghdr"><span class="hmap-gname">FOLLOW</span><span class="hmap-gavg ${cls}">${sign}${avg.toFixed(1)}%</span></div>${sortByPct(FOLLOW,d).map(s=>mkCell(s,d)).join('')}</div>`;
}
function _patchHeatmapCell(sym,price,pct,totalVal,prevPrice){
  const old=window._lastHmapData?.[sym];
  const oldPrice=typeof prevPrice==='number'?prevPrice:(typeof old?.price==='number'?old.price:0);
  window._lastHmapData=window._lastHmapData||{};
  window._lastHmapData[sym]={price,pct,total_value:totalVal>0?totalVal:(old?.total_value||0)};
  const sign=pct>=0?'+':'',style=cellStyle(pct),isChanged=oldPrice>0&&price>0&&price!==oldPrice,isUp=price>oldPrice;

  document.querySelectorAll(`.hmap-cell[data-sym="${sym}"]`).forEach(cell=>{
    const pEl=cell.querySelector('.hc-price'),pctEl=cell.querySelector('.hc-pct');
    if(pEl&&pEl.textContent!==fmtP(price))pEl.textContent=fmtP(price);
    if(pctEl)pctEl.textContent=`${sign}${pct.toFixed(1)}%`;
    cell.style.background=style.bg;cell.style.color=style.fg;
    cell.title=`${sym}|${fmtP(price)}|${sign}${pct.toFixed(2)}%`;
    if(isChanged){
      cell.classList.remove('flash-up','flash-down');
      void cell.offsetWidth;
      cell.classList.add(isUp?'flash-up':'flash-down');
      setTimeout(()=>cell.classList.remove('flash-up','flash-down'),1800);
    }
  });

  document.querySelectorAll(`.lg-sym-item[data-sym="${sym}"]`).forEach(item=>{
    const pEl=item.querySelector('.lg-sym-price'),pctEl=item.querySelector('.lg-sym-pct');
    const color=pct>0?'var(--green)':pct<0?'var(--red)':'#b45309';
    if(pEl&&!item.closest('.lg-group')?.dataset.group?.includes('STRENGTH'))pEl.textContent=fmtP(price);
    if(pctEl){
      pctEl.textContent=`${sign}${pct.toFixed(1)}%`;
      pctEl.style.color=color;
      if(isChanged){
        pctEl.classList.remove('flash-up','flash-down');
        void pctEl.offsetWidth;
        pctEl.classList.add(isUp?'flash-up':'flash-down');
        setTimeout(()=>pctEl.classList.remove('flash-up','flash-down'),1800);
      }
    }
  });
}
function renderHeatmap(d){
  if(!d||!Object.keys(d).length){
    if(!DOM.hmapGrid.querySelector('.hmap-cell')){
      DOM.hmapGrid.innerHTML='<div class="empty"><div class="big">🗺</div><div>Chưa có dữ liệu</div></div>';
    }
    return;
  }
  if(!DOM.hmapGrid.querySelector('.hmap-cell')){
    const maxRows=Math.max(...HMAP_COLS.map(cd=>cd.groups.reduce((s,g)=>s+g.syms.length,0)));
    const tsSyms=TS_POOL.filter(s=>d[s]!==undefined).sort((a,b)=>((d[b]||{}).pct||0)-((d[a]||{}).pct||0)).slice(0,maxRows);
    const parts=[`<div class="hmap-col">${mkGroup('TRADING STOCKS',tsSyms,d)}</div>`];
    HMAP_COLS.forEach((cd,i)=>{
      const extra=i===HMAP_COLS.length-1?mkSectorCol(d):'';
      const followExtra=i===0?mkFollowGroup(d):'';
      parts.push(`<div class="hmap-col">${cd.groups.map(g=>mkGroup(g.name,g.syms,d)).join('')}${followExtra}${extra}</div>`);
    });
    DOM.hmapGrid.innerHTML=parts.join('');
    window._lastHmapData={...d};
    return;
  }

  const old=Object.assign({},window._lastHmapData||{}),changed=[],unchanged=[];
  Object.keys(d).forEach(s=>{
    const it=d[s]||{},oldP=old[s]?.price;
    if(oldP>0&&it.price>0&&it.price!==oldP)changed.push({s,it,oldP});
    else unchanged.push({s,it,oldP:oldP||0});
  });
  unchanged.forEach(({s,it,oldP})=>{_patchHeatmapCell(s,it.price||0,it.pct||0,it.total_value||0,oldP);});
  if(changed.length){
    const maxDelay=Math.max(600,(HMAP_TTL-1.2)*1000);
    changed.sort(()=>Math.random()-0.5).forEach(({s,it,oldP},i)=>{
      setTimeout(()=>{_patchHeatmapCell(s,it.price||0,it.pct||0,it.total_value||0,oldP);},(i/changed.length)*maxDelay+Math.random()*150);
    });
  }

  DOM.hmapGrid.querySelectorAll('.hmap-group').forEach(group=>{
    const avgEl=group.querySelector('.hmap-gavg');
    if(avgEl){
      const cells=Array.from(group.querySelectorAll('.hmap-cell'));
      let sum=0,count=0;
      cells.forEach(c=>{
        const s=c.dataset.sym;
        if(d[s]&&typeof d[s].pct==='number'){sum+=d[s].pct;count++;}
      });
      if(count>0){
        const avg=sum/count,sign=avg>=0?'+':'';
        avgEl.className=`hmap-gavg ${avg>0.05?'pos':avg<-0.05?'neg':'zer'}`;
        avgEl.textContent=`${sign}${avg.toFixed(1)}%`;
      }
      cells.sort((a,b)=>((d[b.dataset.sym]||{}).pct||0)-((d[a.dataset.sym]||{}).pct||0));
      cells.forEach(c=>group.appendChild(c));
    }
  });

  const secGroup=DOM.hmapGrid.querySelector('.hmap-sector-group');
  if(secGroup){
    const groups=[];
    HMAP_COLS.forEach(cd=>cd.groups.forEach(g=>{if(g.name!=='VN30')groups.push({name:g.name,avg:avgPct(g.syms,d)});}));
    groups.sort((a,b)=>b.avg-a.avg);
    secGroup.querySelectorAll('.hmap-sector-cell').forEach((cell,i)=>{
      const g=groups[i];
      if(g){
        const nameEl=cell.querySelector('.hsc-name'),pctEl=cell.querySelector('.hsc-pct');
        if(nameEl)nameEl.textContent=g.name;
        if(pctEl)pctEl.textContent=`${g.avg>=0?'+':''}${g.avg.toFixed(1)}%`;
        const st=cellStyle(g.avg);
        cell.style.background=st.bg;cell.style.color=st.fg;
      }
    });
  }
}
// Event delegation heatmap
DOM.hmapGrid.addEventListener('click',e=>{
  const cell=e.target.closest('.hmap-cell');if(!cell)return;
  const sym=cell.dataset.sym;
  // Mobile dùng chung cơ chế với desktop: thẻ CHART đang mở thì nhảy chart tại chỗ, đóng mới mở popup — xem _hmapDesktopClick().
  _hmapDesktopClick(sym);
});
DOM.hmapGrid.addEventListener('dblclick',e=>{
  // Mobile dùng chung cơ chế dblclick với desktop (double-tap luôn mở openChart) — trước đây bị chặn bằng IS_MOBILE().
  const cell=e.target.closest('.hmap-cell');if(!cell)return;
  if(_hmapClickTimer)clearTimeout(_hmapClickTimer);
  _jumpLiteChart(cell.dataset.sym);
  openChart(cell.dataset.sym);
});
// Gửi mã sang cửa sổ CHART popout ngay khi biết mã, không chờ chart chính tải xong, để 2 chart fetch song song.
function _syncChartPopoutSymbol(sym){
  const s=String(sym||'').toUpperCase().trim();
  if(s&&s!==_liteSymbol){
    _chartSyncChannel?.postMessage({type:'CHART_POPOUT_SYNC',symbol:s});
  }
}
function _jumpLiteChart(sym){
  _syncChartPopoutSymbol(sym);
  loadLiteChart(sym,0,true);
}
let _hmapClickTimer=null;
function _hmapDesktopClick(sym){
  if(_hmapClickTimer)clearTimeout(_hmapClickTimer);
  _hmapClickTimer=setTimeout(()=>{
    _jumpLiteChart(sym);
    if(_isChartPanelOpen)return;
    if(_chartPopoutWin&&!_chartPopoutWin.closed)return;
    if(_liteLSGet('chart_popout_open','0')==='1')return;
    openChart(sym);
  },220);
}
// Event delegation sig-list
DOM.sigList.addEventListener('click',e=>{
  const row=e.target.closest('.sig-row');if(!row)return;
  _hmapDesktopClick(row.dataset.sym); // đồng bộ mobile/desktop — xem ghi chú tại hmapGrid ở trên
});
DOM.sigList.addEventListener('dblclick',e=>{
  // Đồng bộ mobile/desktop — xem ghi chú tại hmapGrid ở trên.
  const row=e.target.closest('.sig-row');if(!row)return;
  if(_hmapClickTimer)clearTimeout(_hmapClickTimer);
  _jumpLiteChart(row.dataset.sym);
  openChart(row.dataset.sym);
});
DOM.momentumList.addEventListener('click',e=>{
  const row=e.target.closest('.momentum-row');if(!row)return;
  _hmapDesktopClick(row.dataset.sym); // đồng bộ mobile/desktop — xem ghi chú tại hmapGrid ở trên
});
DOM.momentumList.addEventListener('dblclick',e=>{
  const row=e.target.closest('.momentum-row');if(!row)return;
  if(_hmapClickTimer)clearTimeout(_hmapClickTimer);
  _jumpLiteChart(row.dataset.sym);
  openChart(row.dataset.sym);
});
DOM.strengthList.addEventListener('click',e=>{
  const row=e.target.closest('.momentum-row');if(!row)return;
  _hmapDesktopClick(row.dataset.sym);
});
DOM.strengthList.addEventListener('dblclick',e=>{
  const row=e.target.closest('.momentum-row');if(!row)return;
  if(_hmapClickTimer)clearTimeout(_hmapClickTimer);
  _jumpLiteChart(row.dataset.sym);
  openChart(row.dataset.sym);
});
DOM.signalHeader.addEventListener('click',e=>{
  if(e.target.closest('#journal-open-btn'))return;
  DOM.momentumBox.classList.toggle('on');
});
// MARKET HEALTH RENDER
function healthEsc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
// Nhãn trục thời gian dưới chart HEALTH chỉ cần Tháng/Năm (ngày cụ thể đã có sẵn qua crosshair khi hover/chạm) — đổi "YYYY-MM-DD" thành "MM/YYYY".
function healthAxisDate(dateStr){
  const m=/^(\d{4})-(\d{2})-(\d{2})$/.exec(String(dateStr||''));
  return m?`${m[2]}/${m[1]}`:String(dateStr||'');
}
function healthBand(score){
  const s=Number(score);
  if(s>=80)return{label:'Hưng phấn',fill:'#7e22ce'};
  if(s>=60)return{label:'Lạc quan',fill:'#16a34a'};
  if(s>=40)return{label:'Trung tính',fill:'#ca8a04'};
  if(s>=20)return{label:'Bi quan',fill:'#dc2626'};
  return{label:'Sợ hãi',fill:'#0284c7'};
}
// Chu kỳ hiển thị HEALTH cố định 60/120 phiên (không zoom/kéo lịch sử); backend trả tối đa 120 phiên.
const HEALTH_PERIODS=[60,120];
let _healthFullHistory=[];      // toàn bộ lịch sử tải về gần nhất từ /api/market_health
let _healthWindowLen=HEALTH_PERIODS[0]; // số phiên đang hiển thị (đổi khi bấm tab chu kỳ)
let _healthLayout=null;         // toạ độ của lần vẽ khung gần nhất, dùng để tính crosshair mà không phải vẽ lại toàn bộ SVG
let _healthShowVni=false;       // có đang bật overlay VNINDEX để đối chiếu không
function renderHealthChart(history){
  _healthFullHistory=(history||[]).filter(p=>Number.isFinite(Number(p.score)));
  _healthRenderWindow();
}
function setHealthPeriod(days){
  if(_healthWindowLen===days)return;
  _healthWindowLen=days;
  DOM.healthPeriodTabs?.querySelectorAll('.health-period-tab').forEach(b=>b.classList.toggle('on',Number(b.dataset.days)===days));
  _healthRenderWindow();
}
function _healthRenderWindow(){
  const total=_healthFullHistory.length;
  if(!total){
    DOM.healthSvg.innerHTML='<foreignObject x="0" y="0" width="900" height="360"><div class="health-empty">Chưa có dữ liệu Mrk Health</div></foreignObject>';
    _healthLayout=null;
    return;
  }
  const windowLen=Math.min(_healthWindowLen,total);
  const start=Math.max(0,total-windowLen);
  const h=_healthFullHistory.slice(start,total);
  // viewBox tính theo tỉ lệ thật của khung (CSS Grid co giãn) thay vì cố định 900x360, để chữ/nét vẽ không bị méo khi khung đổi kích thước.
  const rectBox=DOM.healthSvg.getBoundingClientRect();
  const aspect=(rectBox.width>0&&rectBox.height>0)?rectBox.height/rectBox.width:(360/900);
  const W=900,H=Math.round(Math.min(720,Math.max(320,W*aspect)));
  const scale=H/360;
  // Lề trái/phải (L/R) giữ cố định, không co theo scale (tỉ lệ cao/rộng) để tránh ăn vào dải màu và giữ đúng vị trí checkbox VNINDEX.
  const L=52,R=112,T=Math.round(28*scale),B=Math.round(34*scale),plotW=W-L-R,plotH=H-T-B;
  const fs=Math.max(9,Math.round(10*scale));
  // Chừa khoảng đệm 2 bên để đường line không chạm sát mép trái/phải của khung.
  const padX=16;
  DOM.healthSvg.setAttribute('viewBox',`0 0 ${W} ${H}`);
  const bands=[
    {from:80,to:100,c1:'#f5e8ff',c2:'#7e22ce',label:'Hưng phấn'},
    {from:60,to:80,c1:'#dcfce7',c2:'#16a34a',label:'Lạc quan'},
    {from:40,to:60,c1:'#fef9c3',c2:'#ca8a04',label:'Trung tính'},
    {from:20,to:40,c1:'#fee2e2',c2:'#dc2626',label:'Bi quan'},
    {from:0,to:20,c1:'#e0f2fe',c2:'#0284c7',label:'Sợ hãi'},
  ];
  const y=v=>T+(100-v)/100*plotH;
  const x=i=>L+padX+(h.length===1?(plotW-2*padX):(plotW-2*padX)*i/(h.length-1));
  const defs=bands.map((b,i)=>`<linearGradient id="healthBand${i}" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="${b.c2}" stop-opacity=".30"/><stop offset="1" stop-color="${b.c1}" stop-opacity=".74"/></linearGradient>`).join('');
  const rects=bands.map((b,i)=>{
    const y0=y(b.to),y1=y(b.from),mid=(y0+y1)/2+4;
    // Nhãn dải màu đặt NGOÀI vùng tô (x=W-R+8, cùng lề với trục giá trị bên phải), giống cách nhãn thời gian (ngày) nằm ngoài, dưới trục dưới.
    return `<rect x="${L}" y="${y0}" width="${plotW}" height="${y1-y0}" fill="url(#healthBand${i})"/><text x="${W-R+8}" y="${mid}" text-anchor="start" fill="#334155" font-family="IBM Plex Sans, sans-serif" font-size="${fs}" font-weight="700">${b.label}</text>`;
  }).join('');
  // Trục dọc: mốc cố định mỗi 20 điểm (0-20-40-60-80-100), tách riêng khỏi ngưỡng phân vùng màu ở trên — hai việc khác nhau.
  const grid=[0,20,40,60,80,100].map(v=>`<line x1="${L}" x2="${W-R}" y1="${y(v)}" y2="${y(v)}" stroke="#94a3b8" stroke-opacity=".35"/><text x="${L-10}" y="${y(v)+4}" text-anchor="end" fill="#64748b" font-family="IBM Plex Sans, sans-serif" font-size="${fs}">${v}</text>`).join('');
  const lineW=(1.75*scale).toFixed(2); // HEALTH và VNINDEX dùng chung độ dày nét
  const pts=h.map((p,i)=>`${x(i)},${y(Number(p.score))}`).join(' ');
  // Overlay VNINDEX chuẩn hoá về thang 0-100 theo min/max cửa sổ đang xem để dùng chung trục dọc với HEALTH; giá trị thật vẫn hiện đúng qua crosshair.
  let vniPolyline='',vniMin=null,vniMax=null;
  if(_healthShowVni){
    const vals=h.map(p=>Number(p.vnindex)).filter(v=>Number.isFinite(v));
    if(vals.length>=2){
      vniMin=Math.min(...vals);vniMax=Math.max(...vals);
      const norm=v=>vniMax>vniMin?10+((v-vniMin)/(vniMax-vniMin))*80:50;
      let segs=[],cur=[];
      h.forEach((p,i)=>{
        const v=Number(p.vnindex);
        if(Number.isFinite(v))cur.push(`${x(i)},${y(norm(v))}`);
        else{if(cur.length>1)segs.push(cur);cur=[];}
      });
      if(cur.length>1)segs.push(cur);
      vniPolyline=segs.map(seg=>`<polyline points="${seg.join(' ')}" fill="none" stroke="#f97316" stroke-width="${lineW}" stroke-linejoin="round" stroke-linecap="round"/>`).join('');
    }
  }
  // Trục X: nhiều mốc thời gian dọc theo trục thay vì chỉ đầu/cuối
  const tickCount=Math.min(6,h.length);
  const tickIdxs=[...new Set(tickCount<=1?[0]:Array.from({length:tickCount},(_,k)=>Math.round(k*(h.length-1)/(tickCount-1))))];
  const xLabels=tickIdxs.map(i=>{
    const anchor=i===0?'start':(i===h.length-1?'end':'middle');
    return `<text x="${x(i)}" y="${H-10}" text-anchor="${anchor}" fill="#64748b" font-family="IBM Plex Sans, sans-serif" font-size="${fs}">${healthAxisDate(h[i].date)}</text>`;
  }).join('');
  DOM.healthSvg.innerHTML=`<defs>${defs}</defs><rect x="0" y="0" width="${W}" height="${H}" fill="#fff"/>${rects}${grid}<polyline points="${pts}" fill="none" stroke="#0f172a" stroke-width="${lineW}" stroke-linejoin="round" stroke-linecap="round"/>${vniPolyline}${xLabels}<g id="health-crosshair" style="display:none"></g>`;
  _healthLayout={h,L,R,T,B,H,W,plotW,plotH,padX,x,y,vniMin,vniMax,scale};
}
// Crosshair gắn nhãn vào trục dưới/phải (giống thẻ CHART); SVG preserveAspectRatio="none" nên phải quy đổi pixel về hệ viewBox trước khi tính điểm gần nhất.
function _healthClientX(evt){
  return evt.touches&&evt.touches.length?evt.touches[0].clientX:evt.clientX;
}
// Quy đổi toạ độ con trỏ (pixel thật) sang hệ viewBox — dùng chung cho tìm điểm gần nhất (hover) và kiểm tra vùng zoom.
function _healthEventToSvgPoint(evt){
  if(!_healthLayout)return null;
  const cx=_healthClientX(evt);
  const cy=evt.touches&&evt.touches.length?evt.touches[0].clientY:evt.clientY;
  if(cx==null||cy==null)return null;
  const rect=DOM.healthSvg.getBoundingClientRect();
  if(!rect.width||!rect.height)return null;
  const{W,H}=_healthLayout;
  return{x:(cx-rect.left)*(W/rect.width),y:(cy-rect.top)*(H/rect.height)};
}
function _healthIdxFromEvent(evt){
  if(!_healthLayout)return null;
  const pt=_healthEventToSvgPoint(evt);
  if(!pt)return null;
  const {h,L,plotW,padX}=_healthLayout;
  if(!h.length)return null;
  const usableW=plotW-2*padX;
  const ratio=usableW>0?Math.min(1,Math.max(0,(pt.x-L-padX)/usableW)):0;
  return Math.min(h.length-1,Math.max(0,Math.round(ratio*(h.length-1))));
}
function _healthShowCrosshair(idx){
  if(!_healthLayout)return;
  const g=DOM.healthSvg.querySelector('#health-crosshair');
  if(!g)return;
  const{h,x,y,T,H,B,L,W,R,vniMin,vniMax,scale=1}=_healthLayout,p=h[idx];
  if(!p)return;
  const px=x(idx),py=y(Number(p.score)),band=healthBand(p.score),bottomY=H-B;
  // Nhãn hover (thời gian/giá trị) dùng cỡ chữ + khung riêng, nhỏ hơn nhãn trục, để không bị to quá khổ khi khung chart giãn cao.
  const crossFs=Math.max(8,Math.round(8*scale));
  let svg=`<line x1="${px}" x2="${px}" y1="${T}" y2="${bottomY}" stroke="#0f172a" stroke-width="1" stroke-dasharray="3,3" opacity=".55"/>`;
  svg+=`<line x1="${L}" x2="${W-R}" y1="${py}" y2="${py}" stroke="#0f172a" stroke-width="1" stroke-dasharray="3,3" opacity=".35"/>`;
  svg+=`<circle cx="${px}" cy="${py}" r="${(4.5*scale).toFixed(2)}" fill="${band.fill}" stroke="#fff" stroke-width="1.5"/>`;
  // Nhãn thời gian gắn vào trục dưới, ngay dưới đường crosshair dọc
  const dateW=Math.round(70*scale),dateH=Math.round(16*scale);
  const dateX=Math.max(L,Math.min(W-R-dateW,px-dateW/2));
  svg+=`<rect x="${dateX}" y="${bottomY+4}" width="${dateW}" height="${dateH}" rx="4" fill="#0f172a"/><text x="${dateX+dateW/2}" y="${bottomY+4+dateH/2+4}" text-anchor="middle" fill="#fff" font-family="IBM Plex Mono, monospace" font-size="${crossFs}" font-weight="600">${p.date}</text>`;
  // Nhãn giá trị (score) gắn vào trục phải, ngang với đường crosshair ngang
  const valW=Math.round(28*scale),valH=Math.round(16*scale);
  svg+=`<rect x="${W-R+8}" y="${py-valH/2}" width="${valW}" height="${valH}" rx="4" fill="${band.fill}"/><text x="${W-R+8+valW/2}" y="${py+4}" text-anchor="middle" fill="#fff" font-family="IBM Plex Mono, monospace" font-size="${crossFs}" font-weight="700">${Number(p.score).toFixed(1)}</text>`;
  // Nếu đang bật overlay VNINDEX và điểm này có dữ liệu → thêm đường ngang + nhãn trục phải riêng cho VNINDEX
  const vRaw=Number(p.vnindex);
  if(_healthShowVni&&Number.isFinite(vRaw)&&Number.isFinite(vniMax)&&vniMax>vniMin){
    const py2=y(((vRaw-vniMin)/(vniMax-vniMin))*100);
    svg+=`<line x1="${L}" x2="${W-R}" y1="${py2}" y2="${py2}" stroke="#f97316" stroke-width="1" stroke-dasharray="2,2" opacity=".6"/>`;
    svg+=`<circle cx="${px}" cy="${py2}" r="${(4*scale).toFixed(2)}" fill="#f97316" stroke="#fff" stroke-width="1.5"/>`;
  }
  g.style.display='';
  g.innerHTML=svg;
}
function _healthHideCrosshair(){
  const g=DOM.healthSvg&&DOM.healthSvg.querySelector('#health-crosshair');
  if(g)g.style.display='none';
}
function _healthOnMove(evt){
  if(!_healthLayout)return;
  const idx=_healthIdxFromEvent(evt);
  if(idx!=null)_healthShowCrosshair(idx);
}
// Copy ảnh Mrk Health tránh SVG<foreignObject> (Chrome coi canvas là "tainted", chặn xuất ảnh) — rasterize SVG thuần rồi vẽ tay phần điểm số/nhãn bằng fillText.
function _healthWrapText(ctx,text,maxWidth){
  const words=String(text||'').split(/\s+/).filter(Boolean);
  const lines=[];let line='';
  for(const w of words){
    const test=line?line+' '+w:w;
    if(ctx.measureText(test).width>maxWidth&&line){lines.push(line);line=w;}
    else line=test;
  }
  if(line)lines.push(line);
  return lines.length?lines:[''];
}
async function copyHealthImage(btn){
  const svgEl=DOM.healthSvg;
  if(!svgEl)return;
  // Đo trực tiếp vị trí/kích thước thật của 2 khung trên DOM rồi vẽ lại y hệt trên canvas, để ảnh xuất khớp 1:1 với layout thật kể cả khi responsive.
  const layoutEl=svgEl.closest('.health-layout');
  const chartBoxEl=svgEl.closest('.health-chartbox');
  const scoreCardEl=DOM.healthAnalysis?.previousElementSibling; // .health-score-card — đứng ngay trước .health-analysis trong .health-side
  if(!layoutEl||!chartBoxEl||!scoreCardEl)return;
  try{
    const buildBlob=async()=>{
    const layoutRect=layoutEl.getBoundingClientRect();
    const chartRect=chartBoxEl.getBoundingClientRect();
    const cardRect=scoreCardEl.getBoundingClientRect();
    const analysisRect=DOM.healthAnalysis.getBoundingClientRect();
    const relX=r=>Math.round(r.left-layoutRect.left);
    const relY=r=>Math.round(r.top-layoutRect.top);
    const chartX=relX(chartRect),chartY=relY(chartRect);
    const chartW=Math.max(1,Math.round(chartRect.width)),chartH=Math.max(1,Math.round(chartRect.height));
    const cardX=relX(cardRect),cardY=relY(cardRect);
    const cardW=Math.max(1,Math.round(cardRect.width)),cardH=Math.max(1,Math.round(cardRect.height));
    const anaX=relX(analysisRect),anaY=relY(analysisRect);
    const anaW=Math.max(1,Math.round(analysisRect.width)),anaH=Math.max(1,Math.round(analysisRect.height));

    const dpr=window.devicePixelRatio||1;
    const svgClone=svgEl.cloneNode(true);
    svgClone.setAttribute('width',chartW*dpr);
    svgClone.setAttribute('height',chartH*dpr);
    svgClone.setAttribute('xmlns','http://www.w3.org/2000/svg');
    const svgXml=new XMLSerializer().serializeToString(svgClone);
    const chartImg=new Image();
    await new Promise((res,rej)=>{
      chartImg.onload=res;chartImg.onerror=rej;
      chartImg.src='data:image/svg+xml;charset=utf-8,'+encodeURIComponent(svgXml);
    });

    // Màu lấy từ đúng CSS variable đang dùng thật (--accent/--border/--muted), tránh hardcode lệch nếu theme đổi.
    const cs=getComputedStyle(document.documentElement);
    const cAccent=cs.getPropertyValue('--accent').trim()||'#1a56db';
    const cBorder=cs.getPropertyValue('--border').trim()||'#dde3ee';
    const cMuted=cs.getPropertyValue('--muted').trim()||'#6b7280';
    // health-score tô màu động theo band (đọc từ DOM.healthScore.style.color) khác với health-label dùng màu accent cố định — lấy đúng màu DOM để ảnh copy khớp 100%.
    const cScore=DOM.healthScore?getComputedStyle(DOM.healthScore).color:cAccent;

    // Lấy nội dung khung phân tích trực tiếp từ DOM đang hiển thị (giá trị mới nhất đã render sẵn)
    const scoreText=DOM.healthScore?.textContent||'--';
    const labelText=DOM.healthLabel?.textContent||'--';
    const dateText=DOM.healthDate?.textContent||'';
    const tags=Array.from(DOM.healthTags?.children||[]).map(el=>el.textContent||'');
    const paras=Array.from(DOM.healthAnalysis?.querySelectorAll('p')||[]).map(p=>p.textContent||'');
    const factors=Array.from(DOM.healthAnalysis?.querySelectorAll('li')||[]).map(li=>li.textContent||'');
    const summary=paras[0]||'',conclusion=paras.length>1?paras[paras.length-1]:'';

    const measCanvas=document.createElement('canvas'),mctx=measCanvas.getContext('2d');

    // Đo trước nội dung "Nhận định" theo đúng bề rộng thật của khung phải (anaW) — .health-analysis{padding:18px 20px} nên trừ đúng 20px mỗi bên.
    const anaPadX=20,anaContentW=anaW-2*anaPadX;
    mctx.font='400 15px "IBM Plex Sans",sans-serif';
    const bodyBlocks=[];
    if(summary)bodyBlocks.push(_healthWrapText(mctx,summary,anaContentW));
    mctx.font='400 14.5px "IBM Plex Sans",sans-serif';
    factors.forEach(f=>bodyBlocks.push(_healthWrapText(mctx,'•  '+f,anaContentW)));
    mctx.font='400 15px "IBM Plex Sans",sans-serif';
    if(conclusion)bodyBlocks.push(_healthWrapText(mctx,conclusion,anaContentW));
    const lineH=24,blockGap=8;
    const titleH=15+12; // .health-analysis-title: font-size 15px + margin-bottom 12px
    const analysisLines=bodyBlocks.reduce((n,b)=>n+b.length,0);
    const contentH=bodyBlocks.length?(titleH+analysisLines*lineH+(bodyBlocks.length-1)*blockGap):0;

    // Đo trước layout tag theo đúng bề rộng thật của score-card (cardW) — .health-score-card{padding:16px} nên trừ đúng 16px mỗi bên.
    const cardPadX=16,cardContentW=cardW-2*cardPadX;
    const pillH=24,pillGap=6,pillRowGap=8;
    mctx.font='800 11px "IBM Plex Sans",sans-serif';
    const tagLayout=[];
    if(tags.length){
      let tx=0,trow=0;
      tags.forEach(t=>{
        const tw=mctx.measureText(t).width+16;
        if(tx+tw>cardContentW&&tx>0){tx=0;trow++;}
        tagLayout.push({text:t,x:tx,row:trow,w:tw});
        tx+=tw+pillGap;
      });
    }

    // Kích thước canvas = đúng kích thước thật của .health-layout, cộng lề ngoài khớp .health-body{padding:12px 14px}.
    const outerPadX=14,outerPadY=12;
    const W=Math.round(layoutRect.width),H=Math.round(layoutRect.height);
    const canvasW=W+2*outerPadX,canvasH=H+2*outerPadY;
    const canvas=document.createElement('canvas');
    canvas.width=canvasW*dpr;canvas.height=canvasH*dpr;
    const ctx=canvas.getContext('2d');
    ctx.scale(dpr,dpr);
    ctx.fillStyle='#ffffff';ctx.fillRect(0,0,canvasW,canvasH);
    ctx.textBaseline='alphabetic';
    const ox=outerPadX,oy=outerPadY;
    const roundBox=(x,y,w,h,r,fill,stroke)=>{
      if(fill){ctx.fillStyle=fill;if(ctx.roundRect){ctx.beginPath();ctx.roundRect(x,y,w,h,r);ctx.fill();}else ctx.fillRect(x,y,w,h);}
      if(stroke){ctx.strokeStyle=stroke;ctx.lineWidth=1;if(ctx.roundRect){ctx.beginPath();ctx.roundRect(x+.5,y+.5,w-1,h-1,r);ctx.stroke();}else ctx.strokeRect(x,y,w,h);}
    };

    // ── Cột trái: khung chart (rasterize thẳng từ SVG gốc, viền khớp .health-chartbox) ──
    roundBox(ox+chartX,oy+chartY,chartW,chartH,8,'#ffffff',cBorder);
    ctx.drawImage(chartImg,ox+chartX,oy+chartY,chartW,chartH);
    roundBox(ox+chartX,oy+chartY,chartW,chartH,8,null,cBorder);

    // Checkbox VNINDEX là overlay HTML ngoài SVG, rasterize không chụp được nên phải vẽ tay, vị trí
    // lấy từ getBoundingClientRect() thật (không hard-code %) để tự khớp khi CSS breakpoint đổi.
    const vniToggleRect=DOM.healthVniToggle?DOM.healthVniToggle.getBoundingClientRect():null;
    const vniChecked=!!DOM.healthVniCheckbox?.checked;
    const vniDisabled=!!DOM.healthVniCheckbox?.disabled;
    const vx=vniToggleRect?ox+relX(vniToggleRect):ox+chartX+chartW*0.884;
    const vy=vniToggleRect?oy+relY(vniToggleRect):oy+chartY+6;
    const boxSize=13;
    ctx.fillStyle=vniDisabled?'#f1f5f9':'#ffffff';
    ctx.strokeStyle='#94a3b8';ctx.lineWidth=1;
    if(ctx.roundRect){ctx.beginPath();ctx.roundRect(vx,vy,boxSize,boxSize,3);ctx.fill();ctx.stroke();}
    else{ctx.fillRect(vx,vy,boxSize,boxSize);ctx.strokeRect(vx,vy,boxSize,boxSize);}
    if(vniChecked){
      ctx.strokeStyle=cAccent;ctx.lineWidth=1.6;ctx.lineCap='round';ctx.lineJoin='round';
      ctx.beginPath();
      ctx.moveTo(vx+2.5,vy+7);ctx.lineTo(vx+5.3,vy+9.8);ctx.lineTo(vx+10.5,vy+3.5);
      ctx.stroke();
    }
    const swX=vx+boxSize+5,swY=vy+boxSize/2;
    ctx.strokeStyle='#f97316';ctx.lineWidth=2;ctx.lineCap='round';
    ctx.beginPath();ctx.moveTo(swX,swY);ctx.lineTo(swX+12,swY);ctx.stroke();
    ctx.fillStyle='#334155';
    ctx.font='400 11px "IBM Plex Sans",sans-serif';
    ctx.fillText('VNINDEX',swX+17,vy+boxSize-2);

    // ── Cột phải, khối trên: score-card (label/ngày/điểm + tags) ──
    roundBox(ox+cardX,oy+cardY,cardW,cardH,8,'#fbfcff',cBorder);
    ctx.fillStyle=cAccent;
    ctx.font='800 20px "Barlow Condensed",sans-serif';
    ctx.fillText(labelText.toUpperCase(),ox+cardX+cardPadX,oy+cardY+cardPadX+16);
    ctx.fillStyle=cMuted;
    ctx.font='400 13px "IBM Plex Mono",monospace';
    ctx.fillText(dateText,ox+cardX+cardPadX,oy+cardY+cardPadX+34);
    ctx.fillStyle=cScore;
    ctx.font='800 44px "Barlow Condensed",sans-serif';
    const scoreW=ctx.measureText(scoreText).width;
    ctx.fillText(scoreText,ox+cardX+cardW-cardPadX-scoreW,oy+cardY+cardPadX+34);
    if(tagLayout.length){
      const tagsTop=oy+cardY+cardPadX+50;
      ctx.font='800 11px "IBM Plex Sans",sans-serif';
      tagLayout.forEach(p=>{
        const px=ox+cardX+cardPadX+p.x,py=tagsTop+p.row*(pillH+pillRowGap);
        ctx.fillStyle='#f8fafc';ctx.strokeStyle='#cbd5e1';ctx.lineWidth=1;
        if(ctx.roundRect){ctx.beginPath();ctx.roundRect(px,py,p.w,pillH,4);ctx.fill();ctx.stroke();}
        else{ctx.fillRect(px,py,p.w,pillH);ctx.strokeRect(px,py,p.w,pillH);}
        ctx.fillStyle='#334155';
        ctx.fillText(p.text,px+8,py+16);
      });
    }

    // Khối Nhận định căn giữa theo chiều dọc (flex column, justify-content:center) nên canh giữa nội dung trong anaH thay vì ghim đầu.
    roundBox(ox+anaX,oy+anaY,anaW,anaH,8,'#ffffff',cBorder);
    if(bodyBlocks.length){
      let ly=oy+anaY+Math.max(anaPadX,(anaH-contentH)/2)+titleH-12;
      ctx.fillStyle=cAccent;
      ctx.font='800 15px "Barlow Condensed",sans-serif';
      ctx.fillText('NHẬN ĐỊNH',ox+anaX+anaPadX,ly);
      ly+=12;
      bodyBlocks.forEach((block,bi)=>{
        const isFactor=summary&&bi>=1&&bi<=factors.length&&factors.length>0;
        ctx.font=isFactor?'400 14.5px "IBM Plex Sans",sans-serif':'400 15px "IBM Plex Sans",sans-serif';
        // .health-analysis p{color:#1f2937} vs .health-analysis ul{color:#374151} — dùng đúng màu tương ứng cho từng loại dòng thay vì gộp chung 1 màu như trước.
        ctx.fillStyle=isFactor?'#374151':'#1f2937';
        block.forEach(line=>{ly+=lineH;ctx.fillText(line,ox+anaX+anaPadX,ly);});
        ly+=blockGap;
      });
    }

    const pngBlob=_litePngBlobFromDataUrl(canvas.toDataURL('image/png'));
    return pngBlob;
    };
    await _liteCopyOrDownloadPng(btn,buildBlob,`mrk_health_${_sym||''}.png`,'Mrk Health');
  }catch(e){console.error('copyHealthImage lỗi:',e);_liteCopyFeedback(btn,'failed');}
}
DOM.healthCopyBtn?.addEventListener('click',e=>{
  e.preventDefault();
  e.stopPropagation();
  copyHealthImage(e.currentTarget);
});
DOM.healthSvg.addEventListener('mousemove',_healthOnMove);
DOM.healthSvg.addEventListener('mouseleave',_healthHideCrosshair);
DOM.healthSvg.addEventListener('touchmove',_healthOnMove,{passive:true});
DOM.healthSvg.addEventListener('touchend',_healthHideCrosshair);
if(DOM.healthVniCheckbox){
  DOM.healthVniCheckbox.addEventListener('change',()=>{
    _healthShowVni=!!DOM.healthVniCheckbox.checked;
    _healthHideCrosshair();
    _healthRenderWindow();
  });
}
DOM.healthPeriodTabs?.addEventListener('click',e=>{
  const btn=e.target.closest('.health-period-tab');
  if(btn)setHealthPeriod(Number(btn.dataset.days));
});
// viewBox tính theo kích thước khung thật nên cần vẽ lại khi resize; debounce nhẹ để không vẽ liên tục lúc đang kéo.
let _healthResizeTimer=null;
window.addEventListener('resize',()=>{
  if(!_healthFullHistory.length)return;
  clearTimeout(_healthResizeTimer);
  _healthResizeTimer=setTimeout(_healthRenderWindow,150);
});
function renderHealth(data){
  const d=data||{};
  if(!d.ok){
    DOM.healthScore.textContent='--';
    DOM.healthLabel.textContent='--';
    DOM.healthDate.textContent='--';
    DOM.healthTags.innerHTML='';
    DOM.healthAnalysis.innerHTML='<div class="health-analysis-title">Nhận định</div><div class="health-empty">'+healthEsc(d.message||'Chưa có dữ liệu Mrk Health')+'</div>';
    renderHealthChart([]);
    return;
  }
  const score=Number(d.score),localBand=healthBand(score),band=d.band||localBand,delta=Number(d.delta||0);
  if(DOM.healthVniCheckbox){
    DOM.healthVniCheckbox.disabled=!d.vnindex_available;
    if(!d.vnindex_available&&DOM.healthVniCheckbox.checked){
      DOM.healthVniCheckbox.checked=false;
      _healthShowVni=false;
    }
  }
  DOM.healthScore.textContent=Number.isFinite(score)?score.toFixed(1):'--';
  DOM.healthScore.style.color=localBand.fill;
  DOM.healthLabel.textContent=band.label||localBand.label;
  DOM.healthDate.textContent=`Phiên ${d.as_of||'--'} • ${delta>=0?'+':''}${delta.toFixed(1)} điểm`;
  DOM.healthTags.innerHTML=(d.tags||[]).map(t=>`<span class="health-tag">${healthEsc(t)}</span>`).join('');
  const a=d.analysis||{};
  DOM.healthAnalysis.innerHTML=`<div class="health-analysis-title">Nhận định</div><p>${healthEsc(a.summary||'')}</p><ul>${(a.factors||[]).map(x=>`<li>${healthEsc(x)}</li>`).join('')}</ul><p>${healthEsc(a.conclusion||'')}</p>`;
  renderHealthChart(d.history||[]);
}
// Khi /api/market_health trả ok:false hoặc pending_refresh:true (đang build cache), tự thử lại nhanh hơn HEALTH_TTL cho tới khi có dữ liệu mới, tránh phải F5 trang.
const HEALTH_RETRY_MS=20000;
let _healthRetryTimer=null;
async function fetchHealth(){
  try{
    const j=await fetch('/api/market_health').then(r=>r.json());
    renderHealth(j);
    if(_healthRetryTimer){clearTimeout(_healthRetryTimer);_healthRetryTimer=null;}
    if(j.ok&&!j.pending_refresh){
      // Không gọi pbarHealth nữa
    }else{
      _healthRetryTimer=setTimeout(fetchHealth,HEALTH_RETRY_MS);
    }
  }catch(e){
    console.error('fetchHealth:',e);
    renderHealth({ok:false,message:'Không tải được dữ liệu Mrk Health'});
    if(_healthRetryTimer)clearTimeout(_healthRetryTimer);
    _healthRetryTimer=setTimeout(fetchHealth,HEALTH_RETRY_MS);
  }
}
// =============================================================================
// TREEMAP RENDER
// =============================================================================
const TREEMAP_SECTORS=[];
HMAP_COLS.forEach(col=>col.groups.forEach(g=>{if(g.name!=='VN30')TREEMAP_SECTORS.push(g);}));
const TREEMAP_SVG_NS='http://www.w3.org/2000/svg';
const TREEMAP_MIN_WEIGHT=10000000;

function _svgEl(tag,attrs={},text=''){
  const el=document.createElementNS(TREEMAP_SVG_NS,tag);
  Object.entries(attrs).forEach(([k,v])=>el.setAttribute(k,String(v)));
  if(text)el.textContent=text;
  return el;
}

function treemapWeight(entry){
  if(!entry||typeof entry!=='object')return 0;
  const totalValue=Number(entry.total_value);
  return Number.isFinite(totalValue)&&totalValue>0?totalValue:0;
}

function treemapDataset(data){
  return TREEMAP_SECTORS.map(g=>{
    const stocks=g.syms.map(sym=>{
      const entry=data[sym],weight=treemapWeight(entry);
      return{sym,pct:Number(entry?.pct),weight};
    }).filter(x=>x.weight>TREEMAP_MIN_WEIGHT).sort((a,b)=>b.weight-a.weight);
    return{name:g.name,stocks,weight:stocks.reduce((s,x)=>s+x.weight,0)};
  }).filter(sec=>sec.weight>0).sort((a,b)=>b.weight-a.weight);
}
// Squarified treemap (Bruls et al.) — items cần field .weight>0, out nhận {item,x,y,w,h}.
function tmSquarify(items,x,y,w,h,out){
  if(!items.length)return;
  if(items.length===1){out.push({item:items[0],x,y,w,h});return;}
  const total=items.reduce((s,it)=>s+it.weight,0);
  const vertical=w>=h;
  const worstOf=(row,rowLen)=>row.reduce((wo,it)=>{
    const area=it.weight/total*w*h;
    const s=Math.max(rowLen,area/rowLen),r=Math.min(rowLen,area/rowLen);
    return Math.max(wo,s/r);
  },0);
  let sum=0,row=[],i=0;
  while(i<items.length){
    row.push(items[i]);sum+=items[i].weight;
    const rowLen=sum/total*(vertical?w:h);
    const worst=worstOf(row,rowLen);
    if(i+1<items.length){
      const next=[...row,items[i+1]],sum2=sum+items[i+1].weight;
      const rowLen2=sum2/total*(vertical?w:h);
      if(worstOf(next,rowLen2)<=worst){i++;continue;}
    }
    break;
  }
  const rowSum=row.reduce((s,it)=>s+it.weight,0);
  const rowThick=rowSum/total*(vertical?w:h);
  let pos=vertical?y:x;
  row.forEach(it=>{
    const len=it.weight/rowSum*(vertical?h:w);
    if(vertical)out.push({item:it,x,y:pos,w:rowThick,h:len});
    else out.push({item:it,x:pos,y,w:len,h:rowThick});
    pos+=len;
  });
  const rest=items.slice(row.length);
  if(vertical)tmSquarify(rest,x+rowThick,y,w-rowThick,h,out);
  else tmSquarify(rest,x,y+rowThick,w,h-rowThick,out);
}
// Định dạng tên ngành dùng cho Treemap (chỉ viết hoa chữ cái đầu, VD "NGÂN HÀNG" -> "Ngân hàng").
function sectorLabel(name){
  return name?name.charAt(0)+name.slice(1).toLowerCase():name;
}
let _lastTreemapRenderTime=0;
function renderTreemap(data){
  _lastTreemapRenderTime=Date.now();
  const svg=DOM.treemapSvg;if(!svg)return;
  svg.innerHTML='';
  const sectors=treemapDataset(data||{});
  if(!sectors.length){
    const fo=_svgEl('foreignObject',{x:0,y:0,width:1600,height:900});
    const div=document.createElement('div');
    div.className='treemap-empty';div.textContent='Chưa có dữ liệu heatmap để dựng Treemap';
    fo.appendChild(div);svg.appendChild(fo);return;
  }
  const W=1600,H=900,GAP=4;
  const secLayout=[];tmSquarify(sectors,0,0,W,H,secLayout);
  secLayout.forEach(sec=>{
    const sx=sec.x+GAP/2,sy=sec.y+GAP/2,sw=Math.max(0,sec.w-GAP),sh=Math.max(0,sec.h-GAP);
    // Nền chung (header+vùng ô mã) 1 màu xám-xanh nhạt, gộp 1 rect vừa tô nền vừa vẽ viền để giảm số node SVG.
    svg.appendChild(_svgEl('rect',{x:sx,y:sy,width:sw,height:sh,rx:10,ry:10,fill:'#f4f6f9',stroke:'#d8d6cc','stroke-width':1}));
    const headerH=(sw>40&&sh>18)?22:0;
    if(headerH){
      // Đường kẻ phân cách header/vùng ô mã thay cho rect nền riêng (2 vùng đã đồng màu).
      svg.appendChild(_svgEl('line',{x1:sx,y1:sy+headerH,x2:sx+sw,y2:sy+headerH,stroke:'#e4e8ec','stroke-width':1}));
      // Cắt tên ngành + thêm "…" nếu không đủ chỗ, tránh tràn (~7.3px/ký tự, font-size 12 monospace).
      const label=sectorLabel(sec.item.name);
      const maxChars=Math.max(3,Math.floor((sw-12)/7.3));
      const shown=label.length>maxChars?label.slice(0,Math.max(1,maxChars-1))+'…':label;
      svg.appendChild(_svgEl('text',{x:sx+6,y:sy+15,'font-family':'IBM Plex Mono, monospace','font-size':12,'font-weight':700,fill:'#1f2937'},shown));
    }
    const stockLayout=[];
    tmSquarify(sec.item.stocks,sx+2,sy+headerH,Math.max(0,sw-4),Math.max(0,sh-headerH-2),stockLayout);
    stockLayout.forEach(cell=>{
      const pct=Number.isFinite(cell.item.pct)?cell.item.pct:0;
      const{bg,fg}=treemapCellStyle(pct);
      const cx=cell.x,cy=cell.y,cw=Math.max(0,cell.w-2),ch=Math.max(0,cell.h-2);
      if(cw<=0||ch<=0)return;
      const grp=_svgEl('g',{'data-sym':cell.item.sym,style:'cursor:pointer'});
      grp.appendChild(_svgEl('rect',{x:cx,y:cy,width:cw,height:ch,rx:4,ry:4,fill:bg}));
      if(cw>36&&ch>16){
        const fs=Math.min(26,Math.max(10,Math.min(cw/4.5,ch/3)));
        const fs2=Math.max(11,fs*0.62);
        grp.appendChild(_svgEl('text',{x:cx+cw/2,y:cy+ch/2-fs*0.25,'text-anchor':'middle','font-family':'IBM Plex Mono, monospace','font-size':fs.toFixed(0),'font-weight':700,fill:fg},cell.item.sym));
        if(ch>28){
          const sign=pct>=0?'+':'';
          grp.appendChild(_svgEl('text',{x:cx+cw/2,y:cy+ch/2+fs*0.62+4,'text-anchor':'middle','font-family':'IBM Plex Mono, monospace','font-size':fs2.toFixed(0),fill:fg},`${sign}${pct.toFixed(2)}%`));
        }
      }
      svg.appendChild(grp);
    });
  });
}
DOM.treemapSvg.addEventListener('click',e=>{
  const node=e.target.closest('[data-sym]');if(!node)return;
  _hmapDesktopClick(node.dataset.sym); // đồng bộ mobile/desktop — xem ghi chú tại hmapGrid ở trên
});
DOM.treemapSvg.addEventListener('dblclick',e=>{
  // Đồng bộ mobile/desktop — xem ghi chú tại hmapGrid ở trên.
  const node=e.target.closest('[data-sym]');if(!node)return;
  if(_hmapClickTimer)clearTimeout(_hmapClickTimer);
  const sym=node.dataset.sym;
  openChart(sym);
});
// Copy ảnh Treemap chỉ cần rasterize thẳng #treemap-svg (SVG thuần), không cần vẽ tay thêm phần nào.
async function copyTreemapImage(btn){
  const svgEl=DOM.treemapSvg;
  if(!svgEl)return;
  try{
    const buildBlob=async()=>{
    const wrapRect=DOM.treemapWrap.getBoundingClientRect();
    const W=Math.max(1,Math.round(wrapRect.width)),H=Math.max(1,Math.round(wrapRect.height));
    const dpr=window.devicePixelRatio||1;
    const svgClone=svgEl.cloneNode(true);
    svgClone.setAttribute('width',W*dpr);
    svgClone.setAttribute('height',H*dpr);
    svgClone.setAttribute('xmlns','http://www.w3.org/2000/svg');
    const svgXml=new XMLSerializer().serializeToString(svgClone);
    const img=new Image();
    await new Promise((res,rej)=>{
      img.onload=res;img.onerror=rej;
      img.src='data:image/svg+xml;charset=utf-8,'+encodeURIComponent(svgXml);
    });
    const canvas=document.createElement('canvas');
    canvas.width=W*dpr;canvas.height=H*dpr;
    const ctx=canvas.getContext('2d');
    ctx.fillStyle='#ffffff';ctx.fillRect(0,0,canvas.width,canvas.height);
    ctx.drawImage(img,0,0,canvas.width,canvas.height);
    return _litePngBlobFromDataUrl(canvas.toDataURL('image/png'));
    };
    await _liteCopyOrDownloadPng(btn,buildBlob,'treemap.png','Treemap');
  }catch(e){console.error('copyTreemapImage lỗi:',e);_liteCopyFeedback(btn,'failed');}
}
DOM.treemapCopyBtn?.addEventListener('click',e=>{
  e.preventDefault();
  e.stopPropagation();
  copyTreemapImage(e.currentTarget);
});
// VNDIRECT: Định giá thị trường (P/E, P/B) & Phân bổ (MA50/MA200) tải lười khi mở tab Mrk Health lần đầu, tự làm mới định kỳ theo vndirect_valuation_chart.py.
const VND_AUTO_REFRESH_MS=5*60*1000;
const vndValuationState={metric:'pe',period:365,rows:[]};
const vndAllocationState={period:365,rows:[]};
let _vndLoaded=false,_vndRefreshTimer=null,_vndResizeTimer=null;
const vndTooltip=document.createElement('div');
vndTooltip.className='vnd-tooltip';
vndTooltip.id='vnd-tooltip';
document.body.appendChild(vndTooltip);

function vndFmt(value,digits=2){
  if(!Number.isFinite(Number(value)))return'--';
  return Number(value).toLocaleString('en-US',{minimumFractionDigits:digits,maximumFractionDigits:digits});
}
function vndFmtSigned(value,digits=2){
  if(!Number.isFinite(Number(value)))return'--';
  const n=Number(value);
  return `${n>0?'+':''}${vndFmt(n,digits)}`;
}
function vndDayMonth(value){const d=new Date(value+'T00:00:00');return `${String(d.getDate()).padStart(2,'0')}/${String(d.getMonth()+1).padStart(2,'0')}`;}
function vndYmd(date){return date.toISOString().slice(0,10);}
function vndLabelDate(value){const d=new Date(value+'T00:00:00');return `${String(d.getMonth()+1).padStart(2,'0')}/${d.getFullYear()}`;}
function vndFullDate(value){const d=new Date(value+'T00:00:00');return `${String(d.getDate()).padStart(2,'0')}/${String(d.getMonth()+1).padStart(2,'0')}/${d.getFullYear()}`;}
function vndNiceTicks(min,max,count=4){if(min===max)return[min];const step=(max-min)/(count-1);return Array.from({length:count},(_,idx)=>min+idx*step);}
function vndPickXTicks(rows,count=5){if(rows.length<=count)return rows;return Array.from({length:count},(_,idx)=>rows[Math.round(idx*(rows.length-1)/(count-1))]);}
function vndPeriodStart(days){const start=new Date();start.setDate(start.getDate()-days);return vndYmd(start);}
function vndToPath(points){return points.map(([x,y],idx)=>`${idx?'L':'M'}${x.toFixed(2)},${y.toFixed(2)}`).join(' ');}

async function vndLoadJson(url){
  const res=await fetch(url);
  if(!res.ok)throw new Error(`HTTP ${res.status}`);
  const data=await res.json();
  if(!data.ok)throw new Error(data.message||'Không lấy được dữ liệu');
  return data;
}
function vndShowError(statusId,errorId,err){
  $(statusId).textContent='Không tải được dữ liệu';
  const box=$(errorId);
  box.style.display='block';
  box.textContent=`Lỗi: ${err.message}`;
}

async function loadVndValuation(){
  $('vnd-valuation-status').textContent='Đang tải...';
  $('vnd-valuation-error').style.display='none';
  try{
    const url=`/api/vndirect_valuation?metric=${vndValuationState.metric}&from=${vndPeriodStart(vndValuationState.period)}`;
    const data=await vndLoadJson(url);
    vndValuationState.rows=data.rows;
    $('vnd-valuation-status').textContent=`${vndFullDate(data.from)} - ${vndFullDate(data.to)}`;
    renderVndValuation();
  }catch(e){vndShowError('vnd-valuation-status','vnd-valuation-error',e);}
}
async function loadVndAllocation(){
  $('vnd-allocation-status').textContent='Đang tải...';
  $('vnd-allocation-error').style.display='none';
  try{
    const url=`/api/vndirect_allocation?from=${vndPeriodStart(vndAllocationState.period)}`;
    const data=await vndLoadJson(url);
    vndAllocationState.rows=data.rows;
    $('vnd-allocation-status').textContent=`${vndFullDate(data.from)} - ${vndFullDate(data.to)}`;
    renderVndAllocation();
  }catch(e){vndShowError('vnd-allocation-status','vnd-allocation-error',e);}
}

// Khối ngoại & Tự doanh: 2 bar chart theo phiên, luôn hiện đủ dữ liệu gần nhất trả về (không có bộ lọc kỳ thời gian).
const vndForeignState={rows:[]};
const vndProprietaryState={rows:[]};

async function loadVndForeignFlow(){
  $('vnd-foreign-status').textContent='Đang tải...';
  $('vnd-foreign-error').style.display='none';
  try{
    const data=await vndLoadJson('/api/foreign_flow');
    vndForeignState.rows=data.rows||[];
    $('vnd-foreign-status').textContent=vndForeignState.rows.length?`${vndFullDate(data.from)} - ${vndFullDate(data.to)}`:'--';
    renderVndForeignFlow();
  }catch(e){vndShowError('vnd-foreign-status','vnd-foreign-error',e);}
}
async function loadVndProprietaryFlow(){
  $('vnd-proprietary-status').textContent='Đang tải...';
  $('vnd-proprietary-error').style.display='none';
  try{
    const data=await vndLoadJson('/api/proprietary_flow');
    vndProprietaryState.rows=data.rows||[];
    $('vnd-proprietary-status').textContent=vndProprietaryState.rows.length?`${vndFullDate(data.from)} - ${vndFullDate(data.to)}`:'--';
    renderVndProprietaryFlow();
  }catch(e){vndShowError('vnd-proprietary-status','vnd-proprietary-error',e);}
}

function renderVndFlowPanel(prefix,rows,label){
  renderVndFlowChart(`vnd-${prefix}-svg`,rows,row=>`<strong>${vndFullDate(row.date)}</strong><div>${label} mua ròng: ${vndFmt(row.netValueBn,2)} tỷ</div>`);
}
function renderVndForeignFlow(){renderVndFlowPanel('foreign',vndForeignState.rows||[],'NN');}
function renderVndProprietaryFlow(){renderVndFlowPanel('proprietary',vndProprietaryState.rows||[],'Tự doanh');}

function renderVndFlowChart(svgId,rows,tooltipBuilder){
  const svg=$(svgId);
  svg.innerHTML='';
  if(!rows.length)return;
  const rect=svg.getBoundingClientRect();
  const width=Math.max(320,rect.width||640);
  const height=Math.max(200,rect.height||270);
  svg.setAttribute('viewBox',`0 0 ${width} ${height}`);
  const margin={top:16,right:56,bottom:30,left:52};
  const innerW=width-margin.left-margin.right;
  const innerH=height-margin.top-margin.bottom;
  const vals=rows.map(r=>r.netValueBn);
  const rawMin=Math.min(0,...vals),rawMax=Math.max(0,...vals);
  const pad=(rawMax-rawMin)*0.14||1;
  const yMin=rawMin-pad,yMax=rawMax+pad;
  // Khoảng trắng phải 5% đồng bộ renderVndChart để bar cuối thẳng hàng với điểm giá trị cuối khung Định giá/Phân bổ.
  const rightPadRatio=0.05;
  const barAreaW=innerW/(1+rightPadRatio);
  const step=barAreaW/rows.length;
  const barW=Math.max(3,Math.min(40,step*0.56));
  const sy=v=>margin.top+(1-((v-yMin)/(yMax-yMin||1)))*innerH;
  const zeroY=sy(0);
  function add(tag,attrs,text){
    const el=document.createElementNS('http://www.w3.org/2000/svg',tag);
    for(const[key,value]of Object.entries(attrs))el.setAttribute(key,value);
    if(text!==undefined)el.textContent=text;
    svg.appendChild(el);
    return el;
  }
  // Tick grid luôn qua đúng 0 và cách đều 2 phía, tránh vndNiceTicks() sinh tick sát 0 gây nhìn nhầm 2 đường trùng.
  const flowStepUnit=Math.max(Math.abs(yMax),Math.abs(yMin),1e-9)/3;
  const flowGridTicks=[0];
  for(let t=flowStepUnit;t<=yMax+1e-9;t+=flowStepUnit)flowGridTicks.push(t);
  for(let t=-flowStepUnit;t>=yMin-1e-9;t-=flowStepUnit)flowGridTicks.push(t);
  for(const tick of flowGridTicks){
    const y=sy(tick);
    add('line',{x1:margin.left,x2:width-margin.right,y1:y,y2:y,class:'vnd-grid-line'});
  }
  const tickDates=new Set(vndPickXTicks(rows,Math.min(6,rows.length)).map(r=>r.date));
  rows.forEach((row,idx)=>{
    const x=margin.left+idx*step+step/2;
    const y=sy(Math.max(0,row.netValueBn));
    const h=Math.max(2,Math.abs(sy(row.netValueBn)-zeroY));
    const bar=add('rect',{
      x:x-barW/2,
      y:row.netValueBn>=0?y:zeroY,
      width:barW,height:h,rx:2.5,
      class:row.netValueBn>=0?'vnd-bar-positive':'vnd-bar-negative',
    });
    bar.addEventListener('mousemove',event=>{
      vndTooltip.innerHTML=tooltipBuilder(row);
      vndTooltip.style.display='block';
      vndTooltip.style.left=`${event.clientX+14}px`;
      vndTooltip.style.top=`${event.clientY+14}px`;
    });
    if(tickDates.has(row.date)){
      add('text',{x,y:height-10,'text-anchor':'middle',class:'vnd-x-label'},vndDayMonth(row.date));
    }
  });
  const last=rows[rows.length-1];
  const lastX=margin.left+(rows.length-1)*step+step/2;
  const lastY=sy(last.netValueBn);
  add('text',{
    x:Math.min(width-margin.right,lastX+barW/2+7),
    y:lastY+(last.netValueBn>=0?-5:14),
    'text-anchor':'start',
    fill:last.netValueBn>=0?'var(--green)':'var(--red)',
    'font-size':11,'font-weight':800,
  },`${vndFmtSigned(last.netValueBn,1)} tỷ`);
  svg.addEventListener('mouseleave',()=>{vndTooltip.style.display='none';});
}

function renderVndValuation(){
  const rows=vndValuationState.rows||[];
  if(!rows.length)return;
  const metricLabel=vndValuationState.metric==='pe'?'P/E':'P/B';
  renderVndChart({
    svgId:'vnd-valuation-svg',rows,
    rightSeries:[{key:'value',color:vndValuationState.metric==='pe'?'#f59b00':'#0e9f6e',digits:vndValuationState.metric==='pe'?2:3,axisDigits:vndValuationState.metric==='pe'?1:2}],
    leftColor:'#9b55ff',rightMin:null,rightMax:null,
    tooltipBuilder:row=>`<strong>${vndFullDate(row.date)}</strong><div>VNINDEX: ${vndFmt(row.index,2)}</div><div>${metricLabel}: ${vndFmt(row.value,3)}</div>`,
  });
}
function renderVndAllocation(){
  const rows=vndAllocationState.rows||[];
  if(!rows.length)return;
  renderVndChart({
    svgId:'vnd-allocation-svg',rows,
    rightSeries:[{key:'ma50',color:'#0e9f6e',digits:1,axisDigits:0},{key:'ma200',color:'#f59b00',digits:1,axisDigits:0}],
    leftColor:'#9b55ff',rightMin:0,rightMax:100,
    tooltipBuilder:row=>`<strong>${vndFullDate(row.date)}</strong><div>VNINDEX: ${vndFmt(row.index,2)}</div><div>Trên MA50: ${vndFmt(row.ma50,1)}%</div><div>Trên MA200: ${vndFmt(row.ma200,1)}%</div>`,
  });
}

function renderVndChart(config){
  const rows=config.rows||[];
  const svg=$(config.svgId);
  svg.innerHTML='';
  if(!rows.length)return;
  const rect=svg.getBoundingClientRect();
  const width=Math.max(320,rect.width||640);
  const height=Math.max(200,rect.height||270);
  svg.setAttribute('viewBox',`0 0 ${width} ${height}`);
  const margin={top:16,right:56,bottom:30,left:52};
  const innerW=width-margin.left-margin.right;
  const innerH=height-margin.top-margin.bottom;
  const xVals=rows.map(r=>new Date(r.date+'T00:00:00').getTime());
  const indexVals=rows.map(r=>r.index);
  const rightVals=config.rightSeries.flatMap(series=>rows.map(r=>r[series.key]));
  const xMin=xVals[0],xMax=xVals[xVals.length-1];
  const xRange=xMax-xMin||1;
  const xMaxPadded=xMax+xRange*0.05; // 5% khoảng trống bên phải giữa điểm cuối và trục giá trị
  const idxMin=Math.min(...indexVals),idxMax=Math.max(...indexVals);
  const rightRawMin=Math.min(...rightVals),rightRawMax=Math.max(...rightVals);
  const idxPad=(idxMax-idxMin)*0.08||1;
  const rightPad=(rightRawMax-rightRawMin)*0.12||0.1;
  const rightMin=config.rightMin===null?rightRawMin-rightPad:config.rightMin;
  const rightMax=config.rightMax===null?rightRawMax+rightPad:config.rightMax;
  const sx=x=>margin.left+((x-xMin)/(xMaxPadded-xMin||1))*innerW;
  const syIndex=y=>margin.top+(1-((y-(idxMin-idxPad))/((idxMax+idxPad)-(idxMin-idxPad))))*innerH;
  const syRight=y=>margin.top+(1-((y-rightMin)/(rightMax-rightMin||1)))*innerH;
  function add(tag,attrs,text){
    const el=document.createElementNS('http://www.w3.org/2000/svg',tag);
    for(const[key,value]of Object.entries(attrs))el.setAttribute(key,value);
    if(text!==undefined)el.textContent=text;
    svg.appendChild(el);
    return el;
  }
  for(const tick of vndNiceTicks(idxMin-idxPad,idxMax+idxPad,4)){
    const y=syIndex(tick);
    add('line',{x1:margin.left,x2:width-margin.right,y1:y,y2:y,class:'vnd-grid-line'});
  }
  const axisDigits=config.rightSeries[0].axisDigits;
  const rightSuffix=config.rightMax===100?'%':''; // '%' cho khung Phân bổ (MA50/MA200), rỗng cho P/E,P/B — dùng chung cho axis label lẫn badge bên dưới
  // Bỏ vẽ các nấc giá trị trên trục phải — chỉ giữ badge giá trị hiện tại (_vBadge bên dưới) để đỡ rối mắt.
  for(const row of vndPickXTicks(rows,5)){
    const x=sx(new Date(row.date+'T00:00:00').getTime());
    add('text',{x,y:height-10,'text-anchor':'middle',class:'vnd-x-label'},vndLabelDate(row.date));
  }
  const indexPoints=rows.map(r=>[sx(new Date(r.date+'T00:00:00').getTime()),syIndex(r.index)]);
  add('path',{d:vndToPath(indexPoints),fill:'none',stroke:config.leftColor,'stroke-width':2.2,'stroke-linejoin':'round','stroke-linecap':'round'});
  for(const series of config.rightSeries){
    const points=rows.map(r=>[sx(new Date(r.date+'T00:00:00').getTime()),syRight(r[series.key])]);
    add('path',{d:vndToPath(points),fill:'none',stroke:series.color,'stroke-width':2.2,'stroke-linejoin':'round','stroke-linecap':'round'});
  }
  const last=rows[rows.length-1];
  config.onLast?.(last);
  // Badge giá trị cuối bên phải chart để đọc nhanh không cần hover (đã chuyển badge VNINDEX từ trái sang phải ngày 2026-08-06).
  const _vBadge=(y,text,color)=>{
    const H=15,FONT=10,PAD=5,approxW=Math.max(text.length*6.3+PAD*2,28);
    add('rect',{x:width-margin.right,y:y-H/2,width:approxW,height:H,rx:2.5,fill:color});
    add('text',{x:width-margin.right+PAD,y:y+FONT/2-0.5,
      'text-anchor':'start','font-size':FONT,'font-weight':'700',
      fill:'#fff','font-family':'inherit'},text);
  };
  _vBadge(syIndex(last.index),Math.round(last.index).toLocaleString('en-US'),config.leftColor);
  for(const series of config.rightSeries){
    _vBadge(syRight(last[series.key]),`${vndFmt(last[series.key],series.axisDigits)}${rightSuffix}`,series.color);
  }
  // ── Crosshair & tooltip ────────────────────────────────────────────────────
  const guide=add('line',{y1:margin.top,y2:height-margin.bottom,stroke:'#9aa3b2','stroke-width':1,'stroke-dasharray':'3 4',opacity:0});
  const dots=[add('circle',{r:3.5,fill:config.leftColor,stroke:'#fff','stroke-width':2,opacity:0}),
    ...config.rightSeries.map(series=>add('circle',{r:3.5,fill:series.color,stroke:'#fff','stroke-width':2,opacity:0}))];
  const hit=add('rect',{x:margin.left,y:margin.top,width:innerW,height:innerH,fill:'transparent'});
  hit.addEventListener('mousemove',event=>{
    const box=svg.getBoundingClientRect();
    // Đổi sang tọa độ SVG (viewBox) — xử lý đúng khi SVG bị scale bởi CSS
    const scaleX=width/box.width;
    const curX=Math.max(margin.left,Math.min(width-margin.right,(event.clientX-box.left)*scaleX));
    const ratio=(curX-margin.left)/innerW;
    // Tìm điểm dữ liệu gần nhất theo tọa độ X con trỏ (dùng xMaxPadded cho nhất quán với sx())
    const target=xMin+ratio*(xMaxPadded-xMin);
    let nearest=rows[0],best=Infinity;
    for(const row of rows){
      const time=new Date(row.date+'T00:00:00').getTime();
      const delta=Math.abs(time-target);
      if(delta<best){best=delta;nearest=row;}
    }
    // Guide bám đúng vị trí con trỏ (không snap sang điểm dữ liệu) → crosshair = cursor
    guide.setAttribute('x1',curX);guide.setAttribute('x2',curX);guide.setAttribute('opacity','1');
    // Dots tại X con trỏ, Y tại điểm dữ liệu gần nhất
    dots[0].setAttribute('cx',curX);dots[0].setAttribute('cy',syIndex(nearest.index));dots[0].setAttribute('opacity','1');
    config.rightSeries.forEach((series,idx)=>{
      dots[idx+1].setAttribute('cx',curX);dots[idx+1].setAttribute('cy',syRight(nearest[series.key]));dots[idx+1].setAttribute('opacity','1');
    });
    vndTooltip.innerHTML=config.tooltipBuilder(nearest);
    vndTooltip.style.display='block';
    vndTooltip.style.left=`${event.clientX+14}px`;
    vndTooltip.style.top=`${event.clientY+14}px`;
  });
  hit.addEventListener('mouseleave',()=>{
    guide.setAttribute('opacity','0');
    dots.forEach(dot=>dot.setAttribute('opacity','0'));
    vndTooltip.style.display='none';
  });
}

function vndRefreshAll(){loadVndValuation();loadVndAllocation();loadVndForeignFlow();loadVndProprietaryFlow();}
function vndRerenderVisible(){
  if(vndValuationState.rows.length)renderVndValuation();
  if(vndAllocationState.rows.length)renderVndAllocation();
  if(vndForeignState.rows.length)renderVndForeignFlow();
  if(vndProprietaryState.rows.length)renderVndProprietaryFlow();
}
function vndInitOnce(){
  if(_vndLoaded)return;
  _vndLoaded=true;
  $('vnd-valuation-tabs').addEventListener('click',e=>{
    const btn=e.target.closest('.vnd-tab');
    if(!btn)return;
    const metric=btn.dataset.metric;
    if(metric===vndValuationState.metric)return;
    vndValuationState.metric=metric;
    $('vnd-valuation-tabs').querySelectorAll('.vnd-tab').forEach(b=>b.classList.toggle('on',b.dataset.metric===metric));
    $('vnd-valuation-metric-legend').textContent=metric==='pe'?'P/E':'P/B';
    const _ml=$('vnd-valuation-metric-swatch');if(_ml)_ml.style.background=metric==='pe'?'#f59b00':'#0e9f6e';
    loadVndValuation();
  });
  $('vnd-valuation-period').addEventListener('change',e=>{
    vndValuationState.period=Number(e.target.value);
    loadVndValuation();
  });
  $('vnd-allocation-period').addEventListener('change',e=>{
    vndAllocationState.period=Number(e.target.value);
    loadVndAllocation();
  });
  vndRefreshAll();
  _vndRefreshTimer=setInterval(vndRefreshAll,VND_AUTO_REFRESH_MS);
}
window.addEventListener('resize',()=>{
  if(!_vndLoaded)return;
  clearTimeout(_vndResizeTimer);
  _vndResizeTimer=setTimeout(vndRerenderVisible,150);
});

// ── MARKET (Fireant / Mrk Health / Treemap) — 1 thẻ, chuyển nội dung bằng tab ──
const TRI_TABS=['fireant','health','treemap'];
function triActivateTab(tab){
  if(!TRI_TABS.includes(tab))return;
  DOM.triTabs.querySelectorAll('.tri-tab').forEach(b=>b.classList.toggle('on',b.dataset.tab===tab));
  TRI_TABS.forEach(t=>{
    const el=document.getElementById('tri-content-'+t);
    if(el)el.classList.toggle('on',t===tab);
  });
  // Tab HEALTH vẽ theo kích thước khung thật — cần vẽ lại ngay khi tab vừa hiện (lúc ẩn kích thước=0), giống panel CHART.
  if(tab==='health'){
    if(_healthFullHistory.length)requestAnimationFrame(_healthRenderWindow);
    // Khung PE/PB + Phân bổ thị trường: nạp lần đầu (lazy), các lần mở lại sau chỉ cần vẽ lại theo đúng kích thước khung hiện tại (dữ liệu đã có sẵn trong state).
    vndInitOnce();
    requestAnimationFrame(vndRerenderVisible);
  }else if(tab==='treemap'){
    requestAnimationFrame(()=>renderTreemap(window._lastHmapData||{}));
  }
}
DOM.triTabs.addEventListener('click',e=>{
  const btn=e.target.closest('.tri-tab');
  if(!btn)return;
  triActivateTab(btn.dataset.tab);
});
DOM.triHdr.addEventListener('click',e=>{
  if(e.target.closest('.tri-tab'))return; // bấm vào tên tab không tính là bấm thu/mở cả thẻ
  const collapsed=DOM.triPanel.classList.toggle('collapsed');
  if(!collapsed){
    const activeTab=DOM.triTabs.querySelector('.tri-tab.on');
    if(activeTab&&activeTab.dataset.tab==='health'){
      if(_healthFullHistory.length)requestAnimationFrame(_healthRenderWindow);
      requestAnimationFrame(vndRerenderVisible);
    }
  }
});
// Mặc định mở tab Mrk Health — phải gọi triActivateTab() (không chỉ set class) để kích hoạt vndInitOnce(), nếu không sẽ kẹt "Đang tải...".
triActivateTab('health');
DOM.hmapToggle.addEventListener('click',e=>{
  // Control trong header (nút MARKET/VNINDEX/FOLLOW, ô tìm mã, popout...) vẫn bấm bình thường — chỉ coi là bấm để thu/mở khi không trúng control.
  if(e.target.closest('button,input,.hmap-search-wrap'))return;
  DOM.hmapPanel.classList.toggle('collapsed');
});
DOM.liteChartToggle.addEventListener('click',e=>{
  // Control trong thanh công cụ vẫn bấm bình thường khi thẻ mở. #lite-fav-btn (nút ⭐)
  // phải nằm trong danh sách loại trừ — thiếu nó sẽ bị hiểu nhầm thành bấm header, tự thu gọn thẻ.
  if(e.target.closest('.lite-chart-search-wrap,.lite-tf-tabs,.lite-indicators,.lite-draw-toolbar,#lite-fav-btn,#lite-groups-toggle-btn,#lite-vietstock-toggle-btn,.panel-title'))return;
  const collapsed=DOM.liteChartPanel.classList.toggle('collapsed');
  _isChartPanelOpen=!collapsed;
  if(_isChartPanelOpen){
    // Panel vừa mở lại sau khi ẩn cần ép resize canvas (có thể đang mang kích thước 0) và reset visible range, tránh nến bị dồn cụm.
    requestAnimationFrame(()=>{
      if(_liteChart&&DOM.liteChart)_liteChart.applyOptions({width:DOM.liteChart.clientWidth,height:DOM.liteChart.clientHeight});
      if(_liteRsiChart&&DOM.liteRsiChart)_liteRsiChart.applyOptions({width:DOM.liteRsiChart.clientWidth,height:DOM.liteRsiChart.clientHeight});
      if(_liteMacdChart&&DOM.liteMacdChart)_liteMacdChart.applyOptions({width:DOM.liteMacdChart.clientWidth,height:DOM.liteMacdChart.clientHeight});
      if(_liteData.length)setLiteRightOffset();
      resizeLiteDrawCanvas();redrawLiteDrawings();
    });
  }
});
// CLOCK & CONFIG
let _hmapCountdown=5,_lastHmapUpdateTs='';
let _sessionConfig = { m_start: 535, m_end: 695, a_start: 780, a_end: 1205 };

function _hhmmssToMinutes(t) {
  if (!t) return 0;
  const hh = Math.floor(t / 10000);
  const mm = Math.floor((t % 10000) / 100);
  return hh * 60 + mm;
}

const _isLiveMarket=()=>{
  const d=new Date(),w=d.getDay(),m=d.getHours()*60+d.getMinutes();
  return w>0&&w<6&&((m>=_sessionConfig.m_start&&m<=_sessionConfig.m_end)||(m>=_sessionConfig.a_start&&m<=_sessionConfig.a_end));
};
function _updateHmapTsDisplay(){
  if(!DOM.hmapTs)return;
  const live=_isLiveMarket(),tag=live?'<span style="color:#16a34a;font-weight:700">● LIVE</span>':'<span style="color:#ca8a04;font-weight:700">⏸ NGHỈ PHIÊN</span>';
  DOM.hmapTs.innerHTML=`${tag} `+(live && _hmapCountdown>=1 ? `<span style="font-family:var(--font-mono);font-size:10px;color:var(--muted)">(${_hmapCountdown}s)</span> ` : '')+`• Cập nhật: ${_lastHmapUpdateTs||'—'}`;
}
function tick(){
  const n=new Date();
  DOM.clock.textContent=n.toLocaleTimeString('vi-VN',{hour12:false})+' '+n.toLocaleDateString('vi-VN');
  HMAP_TTL=_isLiveMarket()?(IS_MOBILE()?10:5):120;
  if(_hmapCountdown>HMAP_TTL)_hmapCountdown=HMAP_TTL;
  if(--_hmapCountdown<=0){_hmapCountdown=HMAP_TTL;fetchHmap();}
  _updateHmapTsDisplay();
}
setInterval(tick,1000);tick();
async function loadConfig(){
  try{
    const j=await fetch('/api/config').then(r=>r.json());
    SIG_TTL=j.signal_ttl_sec||30;
    HEALTH_TTL=j.market_health_ttl_sec||1800;
    if(j.session_morning_start!=null)   _sessionConfig.m_start=_hhmmssToMinutes(j.session_morning_start);
    if(j.session_morning_end!=null)     _sessionConfig.m_end=_hhmmssToMinutes(j.session_morning_end)+5;
    if(j.session_afternoon_start!=null) _sessionConfig.a_start=_hhmmssToMinutes(j.session_afternoon_start);
    if(j.session_afternoon_end!=null)   _sessionConfig.a_end=_hhmmssToMinutes(j.session_afternoon_end)+5;
  }catch(e){}
  DOM.footer.textContent=`Scanner Bot Dashboard • Tín hiệu tự động làm mới sau ${SIG_TTL}s • Heatmap ${HMAP_TTL}s • Mrk Health ${Math.round(HEALTH_TTL/60)} phút`;
}
// FETCH
function renderStrengthList(strength){
  if(!DOM.strengthList)return;
  if(!strength.length){
    DOM.strengthList.innerHTML='';
    return;
  }
  DOM.strengthList.innerHTML=strength.map(s=>{
    const p=pctCellForSym(s.symbol);
    return `<div class="momentum-row strength-row" data-sym="${s.symbol}"><span class="s-sym">${s.symbol}</span><span class="s-type" style="color:${p.color}">${p.txt}</span>${rsBadge(s.rs)}</div>`;
  }).join('');
}
async function fetchSigs(){
  try{
    const j=await fetch('/api/signals').then(r=>r.json());
    const rsMeta=`RS ${j.rs_count||0}${j.rs_asof?' @ '+j.rs_asof:''}`;
    DOM.sigMeta.textContent=j.session_stale&&j.session_date
      ?`Phiên gần nhất ${j.session_date} (chưa có phiên mới) • ${j.count} tín hiệu • ${j.momentum_count||0} động lượng • ${j.strength_count||0} sức mạnh • ${rsMeta}`
      :`Cập nhật ${j.updated_at} • ${j.count} tín hiệu • ${j.momentum_count||0} động lượng • ${j.strength_count||0} sức mạnh • ${rsMeta}`;
    _sigTodayMap=new Map((j.signals||[]).map(s=>[s.symbol,s]));
    const momentum=j.momentum||[];
    const strength=j.strength||[];
    _lastStrengthRows=strength;
    _momentumTodayMap=new Map(momentum.map(s=>[s.symbol,s]));
    _strengthTodayMap=new Map(strength.map(s=>[s.symbol,s]));
    _attentTodayMap=new Map((j.attent||[]).map(s=>[s.symbol,s]));
    _breakvolTodayMap=new Map((j.breakvol||[]).map(s=>[s.symbol,s]));
    if(!momentum.length){
      DOM.momentumList.innerHTML='';
    }else{
      DOM.momentumList.innerHTML=momentum.map(s=>{
        const pct=s.pct!=null?(s.pct>=0?'+':'')+Number(s.pct).toFixed(1)+'%':'—';
        const pctColor=s.pct==null?'#6b7280':s.pct>=0?'#0e9f6e':'#e02424';
        return `<div class="momentum-row" data-sym="${s.symbol}"><span class="s-sym">${s.symbol}</span><span class="s-type" style="color:${pctColor}">${pct}</span>${rsBadge(s.rs)}<span class="s-badge-slot"><span class="s-badge b-${s.signal}">${s.signal}</span></span></div>`;
      }).join('');
    }
    renderStrengthList(strength);
    if(!_isChartPopoutWindow&&j.signals?.length){
      j.signals.forEach(s=>{
        const k=`${s.symbol}_${s.signal}_${j.session_date||''}`;
        if(!_knownSignalsSet.has(k)){
          if(_isSignalsInitialized){
            const p=s.pct!=null?(s.pct>=0?'+':'')+Number(s.pct).toFixed(1)+'%':'', pr=s.price!=null?Number(s.price).toFixed(2):'';
            const priceWithPct=pr?(p?`${pr} (${p})`:pr):(p?`(${p})`:'');
            showToastNotify(s.symbol,`${s.emoji||'🎯'} ${s.symbol} - ${signalLabel(s.signal)}`,`${s.symbol} | ${signalLabel(s.signal)} | ${priceWithPct}`,'sig-'+s.symbol+'-'+s.signal);
          }
          _knownSignalsSet.add(k);
        }
      });
    }
    _isSignalsInitialized=true;

    if(!j.signals.length){DOM.sigList.innerHTML='<div class="empty"><div class="big">💤</div><div>Chưa có tín hiệu nào hôm nay</div></div>';return;}
    DOM.sigList.innerHTML=j.signals.map(s=>`<div class="sig-row" data-sym="${s.symbol}"><span class="s-emoji">${s.emoji}</span><span class="s-sym">${s.symbol}</span><span class="s-type" style="color:${s.pct>=0?'#0e9f6e':'#e02424'}">${s.pct!=null?(s.pct>=0?'+':'')+Number(s.pct).toFixed(1)+'%':'—'}</span>${rsBadge(s.rs)}<span class="s-badge-slot"><span class="s-badge ${BADGE_MAP[s.signal]||'b-MACROSS'}">${signalLabel(s.signal)}</span></span></div>`).join('');
    if(DOM.lgSidebar&&DOM.lgSidebar.classList.contains('on'))_lgRenderList();
  }catch(e){console.error('fetchSigs:',e);}
}
let _isSignalsInitialized=false;
const _knownSignalsSet=new Set();
async function fetchHmap(){
  try{
    const j=await fetch('/api/heatmap').then(r=>r.json());
    _lastHmapUpdateTs=new Date().toLocaleTimeString('vi-VN',{hour12:false});
    _hmapCountdown=HMAP_TTL;
    _updateHmapTsDisplay();
    const newData = j.data || {};
    renderHeatmap(newData);

    // Chỉ render Treemap khi tab đó đang active và sau ít nhất 120s để tiết kiệm CPU tối đa
    const activeTab = DOM.triTabs?.querySelector('.tri-tab.on')?.dataset.tab;
    if(activeTab==='treemap'&&Date.now()-_lastTreemapRenderTime>=120000) renderTreemap(newData);

    if(_lastStrengthRows.length)renderStrengthList(_lastStrengthRows);
  }catch(e){
    _hmapCountdown=HMAP_TTL;_updateHmapTsDisplay();
    console.error('fetchHmap:',e);
  }
}
function startBar(elOrId,sec){
  const el=typeof elOrId==='string'?$(elOrId):elOrId;if(!el)return;
  el.style.transition='none';el.style.width='0%';
  requestAnimationFrame(()=>requestAnimationFrame(()=>{el.style.transition=`width ${sec}s linear`;el.style.width='100%';}));
}
// PRICE ALERTS
function _esc(s){return String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));}
function alertReq(path,opts={}){
  const headers={...(opts.headers||{}),'X-Alert-Client-Id':getAlertClientId()};
  if(opts.body&&!headers['Content-Type'])headers['Content-Type']='application/json';
  return fetch(path,{...opts,headers});
}
function _alertMaText(kind,period){return `${(kind||'MA').toUpperCase()}${period||20}`;}
function _alertSideText(type,kind,period,value){
  return type==='ma'?_alertMaText(kind,period):(value?Number(value).toFixed(2):'Giá cổ phiếu');
}
function _alertOperatorText(op){return({gte:'≥ (Tăng lên/Cắt lên)',lte:'≤ (Giảm về/Cắt xuống)'}[op]||op);}
function _alertRuleText(r){
  const left=_alertSideText(r.left_type,r.left_ma_kind,r.left_period);
  const right=r.right_type==='ma'?_alertMaText(r.right_ma_kind,r.right_period):Number(r.right_value||0).toFixed(2);
  return `${r.symbol}: ${left} ${_alertOperatorText(r.operator)} ${right}`;
}
function _alertJumpSymbol(sym){
  sym=String(sym||'').toUpperCase().trim();if(!sym)return;
  _hmapDesktopClick(sym); // đồng bộ mobile/desktop — xem ghi chú tại hmapGrid ở trên
}
function updateAlertFormVisibility(){
  const leftType=DOM.liteAlertLeftType.value;
  DOM.liteAlertLeftKindWrap.style.display=leftType==='ma'?'':'none';
  DOM.liteAlertLeftPeriodWrap.style.display=leftType==='ma'?'':'none';
  const rightType=DOM.liteAlertRightType.value;
  DOM.liteAlertPriceWrap.style.display=rightType==='price'?'':'none';
  DOM.liteAlertRightKindWrap.style.display=rightType==='ma'?'':'none';
  DOM.liteAlertRightPeriodWrap.style.display=rightType==='ma'?'':'none';
  DOM.liteAlertChatWrap.style.display=DOM.liteAlertTelegram.checked?'':'none';
}
function renderAlertRules(){
  if(!DOM.liteAlertList)return;
  const rows=[];
  if(_alertEvents.length){
    rows.push(`<div class="lite-alert-row"><div class="lite-alert-row-main"><div class="lite-alert-row-title">Gần nhất</div><div class="lite-alert-row-sub">${_esc(_alertEvents[0].message)} • ${_esc(_alertEvents[0].created_at)}</div></div><div class="lite-alert-row-actions"><button class="lite-alert-mini" data-alert-jump="${_esc(_alertEvents[0].symbol)}">Mở</button></div></div>`);
  }
  if(!_alertRules.length){
    rows.push('<div class="lite-alert-row"><div class="lite-alert-row-main"><div class="lite-alert-row-title">Chưa có cảnh báo</div><div class="lite-alert-row-sub">Tạo rule mới cho mã đang mở trên CHART.</div></div></div>');
  }else{
    _alertRules.forEach(r=>{
      rows.push(`<div class="lite-alert-row ${r.active?'':'off'}"><div class="lite-alert-row-main"><div class="lite-alert-row-title">${_esc(_alertRuleText(r))}</div><div class="lite-alert-row-sub">${r.active?'Đang bật':'Đang tắt'} • ${r.after_trigger==='disable'?'Tự tắt sau khi báo':'Giữ cảnh báo'}</div></div><div class="lite-alert-row-actions"><button class="lite-alert-mini" data-alert-edit="${r.id}">Sửa</button><button class="lite-alert-mini" data-alert-toggle="${r.id}" data-active="${r.active?0:1}">${r.active?'Tắt':'Bật'}</button><button class="lite-alert-mini danger" data-alert-delete="${r.id}">Xóa</button></div></div>`);
    });
  }
  DOM.liteAlertList.innerHTML=rows.join('');
}
const _isChartPopoutWindow=new URLSearchParams(window.location.search).get('chartPopout')==='1';
const DESKTOP_NOTIFY_KEY='dashboard_desktop_notify_on';
function desktopNotifyEnabled(){
  return 'Notification' in window&&Notification.permission==='granted'&&_liteLSGet(DESKTOP_NOTIFY_KEY,'0')==='1';
}
function syncDesktopNotifyBtn(){
  const on=desktopNotifyEnabled();
  [DOM.hdrDesktopNotifyBtn,DOM.liteAlertDesktopNotify].forEach(btn=>{
    if(!btn)return;
    btn.classList.toggle('on',on);
    btn.title=on?'Tắt thông báo desktop (đang bật)':'Bật thông báo desktop (đang tắt)';
  });
}
async function toggleDesktopNotify(){
  if(!('Notification' in window)){alert('Trình duyệt không hỗ trợ thông báo desktop');return;}
  if(desktopNotifyEnabled()){
    _liteLSSet(DESKTOP_NOTIFY_KEY,'0');
    syncDesktopNotifyBtn();
    return;
  }
  if(Notification.permission==='denied'){
    alert('Thông báo desktop đang bị chặn cho trang này. Vào cài đặt trình duyệt (biểu tượng khóa cạnh URL) để bật lại quyền, sau đó bấm lại nút này.');
    return;
  }
  if(Notification.permission==='default'){
    try{await Notification.requestPermission();}catch(e){}
  }
  if(Notification.permission==='granted')_liteLSSet(DESKTOP_NOTIFY_KEY,'1');
  syncDesktopNotifyBtn();
}
function initDesktopNotifyBtn(){
  syncDesktopNotifyBtn();
  [DOM.hdrDesktopNotifyBtn,DOM.liteAlertDesktopNotify].forEach(btn=>{
    if(!btn)return;
    if(_isChartPopoutWindow){btn.style.display='none';return;}
    btn.addEventListener('click',e=>{e.stopPropagation();toggleDesktopNotify();});
  });
}
const SOUND_NOTIFY_KEY='dashboard_sound_notify_on';
let _audioCtx=null,_lastSoundTime=0;
function soundNotifyEnabled(){return _liteLSGet(SOUND_NOTIFY_KEY,'0')==='1';}
function _unlockAudio(){
  try{
    let needsUnlock=false;
    if(!_audioCtx){_audioCtx=new(window.AudioContext||window.webkitAudioContext)();needsUnlock=true;}
    if(_audioCtx&&_audioCtx.state==='suspended'){_audioCtx.resume().catch(()=>{});needsUnlock=true;}
    if(needsUnlock){
      const osc=_audioCtx.createOscillator(),g=_audioCtx.createGain();
      g.gain.value=0;
      osc.connect(g);g.connect(_audioCtx.destination);
      osc.start();osc.stop(_audioCtx.currentTime+0.01);
    }
  }catch(e){}
}
function playPingSound(force=false){
  if(!force&&!soundNotifyEnabled())return;
  const now=Date.now();
  if(!force&&now-_lastSoundTime<600)return;
  _lastSoundTime=now;
  try{
    if(!_audioCtx)_audioCtx=new(window.AudioContext||window.webkitAudioContext)();
    if(_audioCtx.state==='suspended')_audioCtx.resume().catch(()=>{});
    const t=_audioCtx.currentTime;
    [[880,t,0.2,0.35],[1318.5,t+0.11,0.25,0.45]].forEach(([freq,st,vol,dur])=>{
      const osc=_audioCtx.createOscillator(),g=_audioCtx.createGain();
      osc.type='sine';osc.frequency.setValueAtTime(freq,st);
      g.gain.setValueAtTime(vol,st);g.gain.exponentialRampToValueAtTime(0.0001,st+dur);
      osc.connect(g);g.connect(_audioCtx.destination);
      osc.start(st);osc.stop(st+dur);
    });
  }catch(e){}
}
function syncSoundNotifyBtn(){
  const on=soundNotifyEnabled();
  [DOM.hdrSoundBtn,DOM.liteAlertSoundNotify].forEach(btn=>{
    if(!btn)return;
    btn.classList.toggle('on',on);btn.innerHTML=on?'🔔':'🔕';
    btn.title=(on?'Tắt':'Bật')+' âm thanh thông báo';
  });
}
function toggleSoundNotify(){
  const next=soundNotifyEnabled()?'0':'1';
  _liteLSSet(SOUND_NOTIFY_KEY,next);
  syncSoundNotifyBtn();
  if(next==='1')playPingSound(true);
}
function initSoundNotifyBtn(){
  syncSoundNotifyBtn();
  [DOM.hdrSoundBtn,DOM.liteAlertSoundNotify].forEach(btn=>{
    if(!btn)return;
    if(_isChartPopoutWindow){btn.style.display='none';return;}
    btn.addEventListener('click',e=>{e.stopPropagation();toggleSoundNotify();});
  });
  // Tự động mở luồng âm thanh ngay lần đầu user tương tác với trang.
  // Sau khi unlock thành công, tự xóa listener để không tốn tài nguyên.
  function _autoUnlockOnce(){
    _unlockAudio();
    if(_audioCtx && _audioCtx.state !== 'suspended'){
      ['pointerdown','click','keydown','touchstart'].forEach(evt=>{
        window.removeEventListener(evt,_autoUnlockOnce);
      });
    }
  }
  ['pointerdown','click','keydown','touchstart'].forEach(evt=>{
    window.addEventListener(evt,_autoUnlockOnce,{passive:true});
  });
}
async function loadAlerts(){
  try{
    const r=await alertReq('/api/alerts');
    if(r.ok){const j=await r.json();_alertRules=j.rules||[];renderAlertRules();}
  }catch(e){console.error('loadAlerts:',e);}
}
async function pollAlertFeed(showToast=true){
  try{
    const r=await alertReq('/api/alerts/feed?limit=20');
    if(!r.ok)return;
    const j=await r.json();
    _alertEvents=j.events||[];
    const n=j.unseen_count||0;
    DOM.liteAlertBadge.textContent=n>9?'9+':String(n);
    DOM.liteAlertBadge.classList.toggle('on',n>0);
    renderAlertRules();
    // Cửa sổ CHART popout không cần xử lý toast/notification — bỏ qua để đỡ việc thừa mỗi lần poll.
    if(showToast&&!_isChartPopoutWindow){
      [..._alertEvents].reverse().forEach(ev=>{
        if(ev.seen||_alertShownIds.has(ev.id))return;
        _alertShownIds.add(ev.id);
        showAlertToast(ev);
      });
    }
  }catch(e){console.error('pollAlertFeed:',e);}
}
function showToastNotify(sym,title,body,tag){
  playPingSound();
  if(desktopNotifyEnabled()){
    try{
      const n=new Notification(title,{body,tag});
      n.onclick=()=>{window.focus();_alertJumpSymbol(sym);n.close();};
      return;
    }catch(e){console.error('Notification error:',e);}
  }
  if(!DOM.alertToastWrap)return;
  const el=document.createElement('div');
  el.className='alert-toast';
  el.innerHTML=`<div class="alert-toast-title">${_esc(title)}</div><div class="alert-toast-sub">${_esc(body)}</div>`;
  el.addEventListener('click',()=>_alertJumpSymbol(sym));
  DOM.alertToastWrap.prepend(el);
  setTimeout(()=>{el.style.opacity='0';el.style.transform='translateY(-4px)';setTimeout(()=>el.remove(),260);},10000);
}
function showAlertToast(ev){
  showToastNotify(ev.symbol,`${ev.symbol} - Cảnh báo`,ev.message||'',`price-alert-${ev.id}`);
}
function alertPayload(){
  const leftType=DOM.liteAlertLeftType.value,rightType=DOM.liteAlertRightType.value;
  return {
    client_id:getAlertClientId(),
    symbol:(DOM.liteAlertSymbol.value||_liteSymbol||'').toUpperCase().trim(),
    left_type:leftType,
    left_ma_kind:leftType==='ma'?DOM.liteAlertLeftKind.value:null,
    left_period:leftType==='ma'?Number(DOM.liteAlertLeftPeriod.value):null,
    operator:DOM.liteAlertOperator.value,
    right_type:rightType,
    right_value:rightType==='price'?Number(DOM.liteAlertPrice.value):null,
    right_ma_kind:rightType==='ma'?DOM.liteAlertRightKind.value:null,
    right_period:rightType==='ma'?Number(DOM.liteAlertRightPeriod.value):null,
    notify_dashboard:DOM.liteAlertDashboard.checked,
    notify_telegram:DOM.liteAlertTelegram.checked,
    telegram_chat_id:DOM.liteAlertChat.value.trim(),
    after_trigger:DOM.liteAlertAfter.value
  };
}
function fillAlertFormForEdit(r){
  _editingAlertRuleId=r.id;
  DOM.liteAlertSymbol.value=r.symbol||'';
  DOM.liteAlertLeftType.value=r.left_type||'price';
  if(r.left_ma_kind)DOM.liteAlertLeftKind.value=r.left_ma_kind;
  if(r.left_period)DOM.liteAlertLeftPeriod.value=String(r.left_period);
  DOM.liteAlertOperator.value=r.operator||'gte';
  DOM.liteAlertRightType.value=r.right_type||'price';
  DOM.liteAlertPrice.value=r.right_type==='price'?(r.right_value||''):'';
  if(r.right_ma_kind)DOM.liteAlertRightKind.value=r.right_ma_kind;
  if(r.right_period)DOM.liteAlertRightPeriod.value=String(r.right_period);
  DOM.liteAlertDashboard.checked=!!r.notify_dashboard;
  DOM.liteAlertTelegram.checked=!!r.notify_telegram;
  DOM.liteAlertChat.value=r.telegram_chat_id||'';
  DOM.liteAlertAfter.value=r.after_trigger||'disable';
  updateAlertFormVisibility();
  if(DOM.liteAlertSave)DOM.liteAlertSave.textContent='Cập nhật';
}
function cancelEditAlertRule(){
  _editingAlertRuleId=null;
  if(DOM.liteAlertSave)DOM.liteAlertSave.textContent='Lưu';
}
async function saveAlertRule(){
  const payload=alertPayload();
  if(!payload.symbol){alert('Chưa có mã cổ phiếu');return;}
  if(payload.right_type==='price'&&(!payload.right_value||payload.right_value<=0)){alert('Nhập mức giá hợp lệ');return;}
  if(!payload.notify_dashboard&&!payload.notify_telegram){alert('Chọn ít nhất một kênh báo');return;}
  try{
    const editingId=_editingAlertRuleId;
    const r=editingId?await alertReq(`/api/alerts/${editingId}`,{method:'PUT',body:JSON.stringify(payload)})
                      :await alertReq('/api/alerts',{method:'POST',body:JSON.stringify(payload)});
    const j=await r.json().catch(()=>({}));
    if(!r.ok)throw new Error(j.error||'Không lưu được cảnh báo');
    DOM.liteAlertPrice.value='';
    cancelEditAlertRule();
    await loadAlerts();
  }catch(e){alert('Lỗi lưu cảnh báo: '+e.message);}
}
async function markAlertsSeen(){
  try{
    await alertReq('/api/alerts/seen',{method:'POST',body:JSON.stringify({client_id:getAlertClientId()})});
    await pollAlertFeed(false);
  }catch(e){console.error('markAlertsSeen:',e);}
}
async function testAlertTelegram(){
  const chat=DOM.liteAlertChat.value.trim();
  if(!chat){alert('Nhập Telegram chat ID trước');return;}
  try{
    const r=await alertReq('/api/alerts/test_telegram',{method:'POST',body:JSON.stringify({telegram_chat_id:chat})});
    const j=await r.json().catch(()=>({}));
    if(!r.ok)throw new Error(j.detail||j.error||'Test Telegram lỗi');
    alert('Telegram OK');
  }catch(e){alert('Telegram lỗi: '+e.message);}
}
function bindAlertControls(){
  if(!DOM.liteAlertBtn)return;
  DOM.liteAlertBtn.addEventListener('click',e=>{
    e.stopPropagation();
    cancelEditAlertRule();
    DOM.liteAlertSymbol.value=(_liteSymbol||'').toUpperCase();
    if(DOM.liteAlertChat&&!DOM.liteAlertChat.value.trim()){
      const savedChat=_liteLSGet(ALERT_CHAT_KEY,'');
      if(savedChat){DOM.liteAlertChat.value=savedChat;DOM.liteAlertTelegram.checked=true;}
    }
    DOM.liteAlertPanel.classList.toggle('on');
    updateAlertFormVisibility();loadAlerts();pollAlertFeed(false);
  });
  DOM.liteAlertChat?.addEventListener('change',()=>{
    const v=DOM.liteAlertChat.value.trim();
    if(v)_liteLSSet(ALERT_CHAT_KEY,v);
  });
  document.addEventListener('click',e=>{
    if(DOM.liteAlertWrap&&!DOM.liteAlertWrap.contains(e.target))DOM.liteAlertPanel?.classList.remove('on');
  });
  [DOM.liteAlertLeftType,DOM.liteAlertRightType,DOM.liteAlertTelegram].forEach(el=>el?.addEventListener('change',updateAlertFormVisibility));
  DOM.liteAlertSave?.addEventListener('click',saveAlertRule);
  DOM.liteAlertSeen?.addEventListener('click',markAlertsSeen);
  DOM.liteAlertTest?.addEventListener('click',testAlertTelegram);
  DOM.liteAlertList?.addEventListener('click',async e=>{
    const jump=e.target.closest('[data-alert-jump]');
    if(jump){_alertJumpSymbol(jump.dataset.alertJump);return;}
    const edit=e.target.closest('[data-alert-edit]');
    if(edit){
      const rule=_alertRules.find(x=>String(x.id)===String(edit.dataset.alertEdit));
      if(rule)fillAlertFormForEdit(rule);
      return;
    }
    const tog=e.target.closest('[data-alert-toggle]');
    if(tog){
      await alertReq(`/api/alerts/${tog.dataset.alertToggle}/toggle`,{method:'POST',body:JSON.stringify({client_id:getAlertClientId(),active:Number(tog.dataset.active)===1})});
      await loadAlerts();return;
    }
    const del=e.target.closest('[data-alert-delete]');
    if(del&&confirm('Xóa cảnh báo này?')){
      await alertReq(`/api/alerts/${del.dataset.alertDelete}?client_id=${encodeURIComponent(getAlertClientId())}`,{method:'DELETE'});
      await loadAlerts();
    }
  });
  updateAlertFormVisibility();
}
// SEARCH helper
function _bindSearch(el,onEnter){
  if(!el)return;
  el.addEventListener('keydown',function(e){
    if(e.key==='Enter'){const s=this.value.trim().toUpperCase();if(s.length>=2){this.value='';this.blur();onEnter(s);}}
    if(e.key==='Escape'){this.value='';this.blur();}
  });
  el.addEventListener('focus',function(){this.select();});
}
_bindSearch(DOM.hmapSearch,sym=>openChart(sym));
saveFollowSymbols(FOLLOW);
if(IS_STANDALONE_PWA()){
  // Nhánh riêng cho app cài vào MH chính — 'dblclick' có thể không bắn, tự phân biệt tap đơn/đúp
  // bằng mốc thời gian của 'click'. Nhánh else giữ nguyên logic gốc.
  let _followLastTapTs=0,_followTapTimer=null;
  $('hmap-follow-btn').addEventListener('click',function(){
    const el=this,now=Date.now();
    if(now-_followLastTapTs<400){
      clearTimeout(_followTapTimer);
      _followLastTapTs=0;
      editFollowSymbols();
      el.blur();
      return;
    }
    _followLastTapTs=now;
    clearTimeout(_followTapTimer);
    _followTapTimer=setTimeout(()=>{
      if(!FOLLOW.length){editFollowSymbols();el.blur();return;}
      FOLLOW_ON=!FOLLOW_ON;
      saveFollowSymbols(FOLLOW);
      renderHeatmap(window._lastHmapData||{});
      el.blur();
    },400);
  });
}else{
  let _followClickTimer=null;
  $('hmap-follow-btn').addEventListener('click',function(){
    clearTimeout(_followClickTimer);
    _followClickTimer=setTimeout(()=>{
      if(!FOLLOW.length){editFollowSymbols();this.blur();return;}
      FOLLOW_ON=!FOLLOW_ON;
      saveFollowSymbols(FOLLOW);
      renderHeatmap(window._lastHmapData||{});
      this.blur();
    },180);
  });
  $('hmap-follow-btn').addEventListener('dblclick',function(e){
    e.preventDefault();
    clearTimeout(_followClickTimer);
    editFollowSymbols();
    this.blur();
  });
}
$('journal-open-btn').addEventListener('click',()=>{
  if(DOM.journalFrame.src==='about:blank')DOM.journalFrame.src='/journal';
  DOM.journalOverlay.classList.add('on');
  document.body.style.overflow='hidden';
});
function closeJournal(){
  DOM.journalOverlay.classList.remove('on');
  if(!DOM.overlay.classList.contains('on'))document.body.style.overflow='';
}
DOM.journalOverlay.addEventListener('click',e=>{if(e.target===DOM.journalOverlay)closeJournal();});
// POPUP — tab activation (dùng chung cho cả 3 header)
function _activateTab(tab){
  _tab=tab;
  // Desktop tabs
  DOM.popupCtabs.querySelectorAll('.ctab').forEach(b=>b.classList.toggle('on',b.dataset.tab===tab));
  // Mobile portrait tabs
  DOM.mobTabRow.querySelectorAll('.mob-tab-btn').forEach(b=>b.classList.toggle('on',b.dataset.tab===tab));
  // Mobile landscape tabs
  DOM.mobLandTabs.querySelectorAll('.mob-land-tab').forEach(b=>b.classList.toggle('on',b.dataset.tab===tab));
  // Panels
  TABS_ALL.forEach(t=>document.getElementById('panel-'+t).classList.toggle('on',t===tab));
  // Lazy iframes
  if(IFRAME_LAZY[tab]){const f=$('iframe-'+tab);if(f&&f.src==='about:blank')f.src=IFRAME_LAZY[tab](_sym);}
  // Scroll active tab into view (portrait)
  if(IS_MOBILE()&&!IS_LANDSCAPE()){
    const activeBtn=DOM.mobTabRow.querySelector('.mob-tab-btn.on');
    if(activeBtn)activeBtn.scrollIntoView({behavior:'smooth',block:'nearest',inline:'center'});
  }
  // Scroll active tab into view (landscape)
  if(IS_MOBILE()&&IS_LANDSCAPE()){
    const activeBtn=DOM.mobLandTabs.querySelector('.mob-land-tab.on');
    if(activeBtn)activeBtn.scrollIntoView({behavior:'smooth',block:'nearest',inline:'center'});
  }
}
// Event delegation — desktop tabs
DOM.popupCtabs.addEventListener('click',e=>{const btn=e.target.closest('.ctab');if(btn)_activateTab(btn.dataset.tab);});
// Event delegation — mobile portrait tabs
DOM.mobTabRow.addEventListener('click',e=>{const btn=e.target.closest('.mob-tab-btn');if(btn)_activateTab(btn.dataset.tab);});
// Event delegation — mobile landscape tabs
DOM.mobLandTabs.addEventListener('click',e=>{const btn=e.target.closest('.mob-land-tab');if(btn)_activateTab(btn.dataset.tab);});
// POPUP OPEN / CLOSE
function _updateSymDisplay(sym){
  DOM.ptitle.textContent=sym;
  DOM.mobPtitle.textContent=sym;
  DOM.mobLandSym.textContent=sym;
}
function _openPopup(){
  DOM.overlay.classList.add('on');
  document.body.style.overflow='hidden';
  DOM.edgeZone.classList.add('on');
  // Portrait: show float close
  if(IS_MOBILE()&&!IS_LANDSCAPE())
    DOM.mobClose.style.display='flex';
  else
    DOM.mobClose.style.display='none';
}
function openChart(sym,tab='chart'){
  _resetPopupChrome();
  _sym=sym.toUpperCase().trim();_tab=tab;
  _updateSymDisplay(_sym);
  DOM.ifVs.src='https://ta.vietstock.vn/?stockcode='+_sym.toLowerCase();
  ['chart','vnd-cs','vnd-news','vnd-sum','24h'].forEach(t=>{const f=$('iframe-'+t);if(f)f.src='about:blank';});
  _activateTab(tab);
  _openPopup();
  setTimeout(()=>DOM.pbox.focus(),0);
  // Clear search inputs
  DOM.popupSearch.value='';DOM.mobSearch.value='';DOM.mobLandSearch.value='';
}
function closePopup(){
  const pbox=DOM.pbox;
  _resetPopupChrome();
  pbox.style.visibility='hidden';
  DOM.ifVs.src='about:blank';
  ['chart','vnd-cs','vnd-news','vnd-sum','24h'].forEach(t=>{const f=$('iframe-'+t);if(f)f.src='about:blank';});
  pbox.style.animation='none';
  DOM.overlay.classList.remove('on');
  document.body.style.overflow='';
  DOM.edgeZone.classList.remove('on');
  DOM.mobClose.style.display='none';
  requestAnimationFrame(()=>{pbox.style.visibility='';pbox.style.animation='';});
}
// Close buttons
$('popup-close-btn').addEventListener('click',closePopup);
DOM.mobClose.addEventListener('click',closePopup);
$('mob-land-close').addEventListener('click',closePopup);
DOM.overlay.addEventListener('click',e=>{if(e.target===DOM.overlay)closePopup();});
// Search bindings
_bindSearch(DOM.popupSearch,sym=>openChart(sym));
_bindSearch(DOM.mobSearch,sym=>openChart(sym));
_bindSearch(DOM.mobLandSearch,sym=>openChart(sym));
// Mobile swipe right to close
if(IS_MOBILE()){
  let _swX=0,_swDir='',_swFired=false;
  DOM.pbox.addEventListener('touchstart',e=>{
    if(!DOM.overlay.classList.contains('on'))return;
    if(e.touches[0].clientX>40)return;
    _swX=e.touches[0].clientX;_swDir='';_swFired=false;
  },{passive:true});
  DOM.pbox.addEventListener('touchmove',e=>{
    if(_swFired)return;
    const dx=e.touches[0].clientX-_swX;
    if(!_swDir&&Math.abs(dx)>10)_swDir='h';
    if(_swDir==='h'&&dx>50){_swFired=true;closePopup();}
  },{passive:true});
}
// Orientation change
window.addEventListener('orientationchange',()=>{
  setTimeout(()=>{
    if(DOM.overlay.classList.contains('on')){
      if(IS_MOBILE()&&!IS_LANDSCAPE())
        DOM.mobClose.style.display='flex';
      else
        DOM.mobClose.style.display='none';
    }
  },300);
});
// Keyboard
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'){if(DOM.overlay.classList.contains('on')){closePopup();return;}}
  if(e.key==='Escape'&&DOM.journalOverlay.classList.contains('on')){closeJournal();return;}
});
// Nhóm mã dùng chung cho sidebar CHART (TRADING, VN30, nhóm ngành...) — tên biến giữ nguyên lịch sử.
const _hvGroups=[];
(function(){
  _hvGroups.push({name:'TRADING',syms:TS_POOL});
  _hvGroups.push({name:'VN30',syms:HMAP_COLS[0].groups[0].syms});
  HMAP_COLS.forEach(cd=>cd.groups.forEach(g=>{if(g.name!=='VN30')_hvGroups.push({name:g.name,syms:g.syms});}));
})();
// Helper đọc window._lastHmapData, dùng cho NHÓM NGÀNH (sidebar CHART).
function _symDisplayFields(sym,data){
  const entry=(data||window._lastHmapData||{})[sym];
  const pct=entry&&typeof entry.pct==='number'?entry.pct:null;
  const price=entry&&typeof entry.price==='number'?fmtP(entry.price):'—';
  const pctStr=pct!==null?(pct>=0?'+':'')+pct.toFixed(1)+'%':'—';
  const color=pct===null?'var(--muted)':pct>0?'var(--green)':pct<0?'var(--red)':'#b45309';
  return{pct,price,pctStr,color};
}
// Bù giá on-demand cho mã lẻ hiển thị trên sidebar nhưng không có trong window._lastHmapData (mã
// người dùng tự thêm ngoài TS_POOL_CONFIG/HMAP_COLS_CONFIG) — trước đây các mã này luôn hiện "--".
// _extraQuoteAsked: nhớ lần gọi gần nhất theo mã, khớp TTL cache server EXTRA_QUOTE_TTL_SEC=20s.
const _extraQuoteAsked=new Map();
const _EXTRA_QUOTE_MIN_INTERVAL=15000;
async function _lgFillMissingQuotes(){
  if(!DOM.lgSidebar||!DOM.lgSidebar.classList.contains('on'))return;
  const now=Date.now(),missing=[],seen=new Set();
  _lgGetGroups().forEach(g=>g.syms.forEach(sym=>{
    if(!sym||seen.has(sym))return;
    seen.add(sym);
    if(window._lastHmapData&&window._lastHmapData[sym])return; // đã có giá, không cần bù
    if(now-(_extraQuoteAsked.get(sym)||0)<_EXTRA_QUOTE_MIN_INTERVAL)return; // vừa hỏi gần đây, chờ thêm
    missing.push(sym);
  }));
  if(!missing.length)return;
  missing.forEach(sym=>_extraQuoteAsked.set(sym,now));
  try{
    const j=await fetch('/api/quote_extra?syms='+encodeURIComponent(missing.join(','))).then(r=>r.json());
    const data=j.data||{};
    if(Object.keys(data).length){
      window._lastHmapData=Object.assign({},window._lastHmapData,data);
      _lgRenderList(); // vẽ lại đúng 1 lần để hiện giá/% vừa lấy được (mã đã có data nên lần gọi kế _lgFillMissingQuotes sẽ tự bỏ qua, không lặp vô hạn)
    }
  }catch(e){console.error('_lgFillMissingQuotes:',e);}
}
function _sortSymsByMode(syms,mode='pct'){
  if(mode==='custom')return[...syms];
  if(mode==='alpha')return[...syms].sort((a,b)=>a.localeCompare(b));
  if(mode==='rs')return[...syms].sort((a,b)=>(_strengthTodayMap.get(b)?.rs??-1)-(_strengthTodayMap.get(a)?.rs??-1)||a.localeCompare(b));
  const d=window._lastHmapData||{};
  return[...syms].sort((a,b)=>{
    const ea=d[String(a||'').toUpperCase()],eb=d[String(b||'').toUpperCase()];
    const pa=Number(ea?.pct),pb=Number(eb?.pct);
    return (Number.isFinite(pb)?pb:-999)-(Number.isFinite(pa)?pa:-999)||a.localeCompare(b);
  });
}
// CHART — SIDEBAR NHÓM NGÀNH / FAVORITE (overlay, không đụng logic vẽ chart)
const LG_FAVORITE_KEY='dashboard_favorite_symbols';
let LG_FAVORITES=_lgLoadFavorites();
function _lgLoadFavorites(){
  try{return JSON.parse(localStorage.getItem(LG_FAVORITE_KEY)||'[]').filter(Boolean).map(s=>String(s).toUpperCase());}catch(e){return[];}
}
function _lgSaveFavorites(){
  try{localStorage.setItem(LG_FAVORITE_KEY,JSON.stringify(LG_FAVORITES));}catch(e){}
  _chartSyncChannel?.postMessage({type:'FAVORITE_SYNC'});
}
// Đẩy các mã đang có trong FOLLOW lên đầu, giữ nguyên thứ tự tương đối còn lại
function _lgFollowFirstOrder(syms){
  const inFollow=syms.filter(s=>FOLLOW.includes(s));
  const rest=syms.filter(s=>!FOLLOW.includes(s));
  return [...inFollow,...rest];
}
// Chèn mã mới vào LG_FAVORITES: mã FOLLOW chèn cuối khối Follow (đầu danh sách), mã khác thêm cuối danh sách. Dùng chung cho sync Follow, bấm sao, nhập nhanh.
function _lgInsertFavorite(sym){
  if(FOLLOW.includes(sym)){
    let pos=0;while(pos<LG_FAVORITES.length&&FOLLOW.includes(LG_FAVORITES[pos]))pos++;
    LG_FAVORITES.splice(pos,0,sym);
  }else{
    LG_FAVORITES.push(sym);
  }
}
// Danh sách FOLLOW tự động được gắn ⭐ (thêm vào FAVORITE), xếp thành khối ở đầu danh sách. Không tự bỏ sao nếu người dùng gỡ mã khỏi FOLLOW hoặc tự tay bỏ sao trong FAVORITE.
function _lgSyncFollowIntoFavorites(){
  let changed=false;
  FOLLOW.forEach(sym=>{
    if(!LG_FAVORITES.includes(sym)){_lgInsertFavorite(sym);changed=true;}
  });
  const reordered=_lgFollowFirstOrder(LG_FAVORITES);
  if(reordered.join(',')!==LG_FAVORITES.join(',')){LG_FAVORITES=reordered;changed=true;}
  if(changed){_lgSaveFavorites();if(DOM.lgList)_lgRenderList();}
}
_lgSyncFollowIntoFavorites();
function _lgToggleFavorite(sym){
  sym=String(sym||'').toUpperCase();if(!sym)return;
  const i=LG_FAVORITES.indexOf(sym);
  if(i===-1)_lgInsertFavorite(sym);
  else LG_FAVORITES.splice(i,1);
  _lgSaveFavorites();_lgRenderList();
  _lgUpdateChartFavBtn();
}
// Nút ⭐ trên toolbar chart: thêm/bỏ FAVORITE cho mã đang xem mà không cần mở sidebar, dùng chung LG_FAVORITES/_lgToggleFavorite.
function _lgUpdateChartFavBtn(){
  if(!DOM.liteFavBtn)return;
  const on=LG_FAVORITES.includes((_liteSymbol||'').toUpperCase());
  DOM.liteFavBtn.classList.toggle('on',on);
  DOM.liteFavBtn.textContent=on?'★':'☆';
}
DOM.liteFavBtn?.addEventListener('click',()=>{
  if(!_liteSymbol)return;
  _lgToggleFavorite(_liteSymbol);
});
function _lgReorderFavorite(dragSym,targetSym){
  // Không cho kéo-thả qua ranh giới giữa khu vực FOLLOW và khu vực FAVORITE thường — hai khu vực này phải luôn phân khai rõ, chỉ sắp xếp lại được trong cùng khu vực.
  if(FOLLOW.includes(dragSym)!==FOLLOW.includes(targetSym))return;
  const from=LG_FAVORITES.indexOf(dragSym);if(from===-1)return;
  LG_FAVORITES.splice(from,1);
  let to=LG_FAVORITES.indexOf(targetSym);if(to===-1)to=LG_FAVORITES.length;
  LG_FAVORITES.splice(to,0,dragSym);
  _lgSaveFavorites();_lgRenderList();
}
const LG_GROUP_ORDER_KEY='scanner_lg_group_order_v1';
function _lgLoadGroupOrder(){try{return JSON.parse(localStorage.getItem(LG_GROUP_ORDER_KEY)||'[]');}catch{return[];}}
function _lgSaveGroupOrder(o){
  try{localStorage.setItem(LG_GROUP_ORDER_KEY,JSON.stringify(o));}catch(e){}
  _chartSyncChannel?.postMessage({type:'FAVORITE_SYNC'});
}
function _lgReorderGroup(fromName,toName){
  const names=_lgGetGroups().map(g=>g.name);
  const f=names.indexOf(fromName),t=names.indexOf(toName);
  if(f===-1||t===-1||f===t)return;
  names.splice(f,1);names.splice(t,0,fromName);
  _lgSaveGroupOrder(names);
  _lgRenderList();
}
let _lgSortModes=new Map(),_lgActiveGroupName=null,_lgActiveSym='',_lgDragSym=null,_lgDragGroup=null,_lgDragOverEl=null;
function _lgGetGroups(){
  const all=[
    {name:'FAVORITE',syms:LG_FAVORITES,isFavorite:true},
    {name:'SIGNAL',syms:[..._sigTodayMap.keys()]},
    {name:'MOMENTUM',syms:[..._momentumTodayMap.keys()]},
    {name:'SỨC MẠNH',syms:[..._strengthTodayMap.keys()],isStrength:true},
    {name:'ATTENT',syms:[..._attentTodayMap.keys()]},
    {name:'BREAKVOL',syms:[..._breakvolTodayMap.keys()]},
    ..._hvGroups
  ];
  const ord=_lgLoadGroupOrder();
  if(!ord.length)return all;
  const map=new Map(all.map(g=>[g.name,g]));
  const res=[];
  ord.forEach(n=>{if(map.has(n)){res.push(map.get(n));map.delete(n);}});
  return[...res,...map.values()];
}
function _lgDefaultSortMode(g){return g&&g.isFavorite?'custom':(g&&g.isStrength?'rs':'pct');}
function _lgSortModeFor(g){return _lgSortModes.get(g.name)||_lgDefaultSortMode(g);}
function _lgSortLabel(g){const m=_lgSortModeFor(g);return m==='custom'?'≡':(m==='alpha'?'A↕Z':m==='rs'?'RS↕':'%↕');}
function _lgNextSortMode(g){
  const m=_lgSortModeFor(g);
  if(g.isFavorite) {
     _lgSortModes.set(g.name, m==='custom'?'pct':(m==='pct'?'alpha':(m==='alpha'?'rs':'custom')));
  } else {
     _lgSortModes.set(g.name, m==='pct'?'alpha':(m==='alpha'?'rs':'pct'));
  }
}
function _lgSortSyms(syms,g){
  return _sortSymsByMode(syms,_lgSortModeFor(g));
}
let _lgOffTimer=null;
function _lgScheduleOffTimer(){clearTimeout(_lgOffTimer);_lgOffTimer=setTimeout(()=>{_lgActiveSym='';DOM.lgList?.querySelectorAll('.lg-sym-item.on').forEach(el=>el.classList.remove('on'));},5000);}
function _lgSymRow(sym,draggable,g=null){
  const{price,pctStr,color}=_symDisplayFields(sym);
  const isStrength=!!(g&&g.isStrength);
  const rs=_strengthTodayMap.get(sym)?.rs;
  const rightValue=isStrength&&Number.isFinite(Number(rs))?Math.round(Number(rs)):price;
  const starred=LG_FAVORITES.includes(sym);
  const isFollow=FOLLOW.includes(sym);
  return `<div class="lg-sym-item${isFollow?' lg-follow':''}${sym===_lgActiveSym?' on':''}" data-sym="${sym}"${draggable?' draggable="true"':''}>`
    +`<span class="lg-star${starred?' on':''}" data-star="${sym}" title="Thêm/bỏ khỏi Favorite">${starred?'★':'☆'}</span>`
    +`<span class="lg-sym-name">${sym}</span>`
    +`<span class="lg-sym-pct" style="color:${color}">${pctStr}</span>`
    +`<span class="lg-sym-price">${rightValue}</span></div>`;
}
function _lgRenderList(){
  if(!DOM.lgList)return;
  const groups=_lgGetGroups();
  DOM.lgList.innerHTML=groups.map(g=>{
    const open=g.name===_lgActiveGroupName;
    let body='';
    if(open){
      if(!g.syms.length)body='<div class="lg-empty-hint">Chưa có mã nào</div>';
      else body=_lgSortSyms(g.syms,g).map(s=>_lgSymRow(s, !!g.isFavorite && _lgSortModeFor(g)==='custom', g)).join('');
    }
    const addBtn=(g.isFavorite&&open)?'<button type="button" class="lg-add-btn" data-add-fav title="Nhập nhanh nhiều mã vào FAVORITE">+</button>':'';
    const sortBtn=(open)?`<button type="button" class="lg-sort-btn" data-sort-toggle="${g.name}" title="Đổi kiểu sắp xếp">${_lgSortLabel(g)}</button>`:'';
    return `<div class="lg-group${open?' open':''}" data-group="${g.name}">`
      +`<div class="lg-ghdr" data-ghdr="${g.name}" draggable="true"><span>${g.name}${g.isFavorite?' ('+g.syms.length+')':''}</span><span class="lg-ghdr-right">${addBtn}${sortBtn}<span class="lg-caret">▸</span></span></div>`
      +`<div class="lg-symlist">${body}</div></div>`;
  }).join('');
  _lgFillMissingQuotes();
}
function _lgQuickAddFavorites(){
  const raw=prompt('Nhập các mã muốn thêm vào FAVORITE, cách nhau bằng dấu phẩy hoặc khoảng trắng:','');
  if(raw===null)return;
  const syms=parseFollowSymbols(raw);
  if(!syms.length)return;
  let changed=false;
  syms.forEach(sym=>{
    if(LG_FAVORITES.includes(sym))return;
    _lgInsertFavorite(sym);
    changed=true;
  });
  if(changed){_lgSaveFavorites();_lgRenderList();}
}
DOM.lgToggleBtn?.addEventListener('click',e=>{
  e.stopPropagation();
  DOM.lgSidebar.classList.toggle('on');
  if(DOM.lgSidebar.classList.contains('on')){
    _lgActiveSym=_liteSymbol||_lgActiveSym;
    if(!_lgActiveGroupName)_lgActiveGroupName='FAVORITE';
    _lgRenderList();
  }
});
function _updateVietstockIframeIfActive(sym){
  if(!DOM.liteChartFrame.classList.contains('vietstock-mode'))return;
  DOM.liteVietstockIframe.src='https://ta.vietstock.vn/?stockcode='+(sym||_liteSymbol||'VNINDEX').toLowerCase();
}
function _setVietstockMode(on){
  DOM.liteChartFrame.classList.toggle('vietstock-mode',on);
  DOM.liteVietstockToggleBtn.classList.toggle('on',on);
  if(on)_updateVietstockIframeIfActive();
  else DOM.liteVietstockIframe.src='about:blank';
}
DOM.liteVietstockToggleBtn.addEventListener('click',e=>{
  e.stopPropagation();
  _setVietstockMode(!DOM.liteChartFrame.classList.contains('vietstock-mode'));
});
DOM.liteChartTitleLabel.addEventListener('click',e=>{
  e.stopPropagation();
  _setVietstockMode(false);
});
DOM.lgList?.addEventListener('click',e=>{
  const star=e.target.closest('.lg-star');
  if(star){_lgToggleFavorite(star.dataset.star);return;}
  const addBtn=e.target.closest('[data-add-fav]');
  if(addBtn){e.stopPropagation();_lgQuickAddFavorites();return;}
  const sortBtn=e.target.closest('[data-sort-toggle]');
  if(sortBtn){
    e.stopPropagation();
    const g=_lgGetGroups().find(x=>x.name===sortBtn.dataset.sortToggle);
    if(g)_lgNextSortMode(g);
    _lgRenderList();
    return;
  }
  const hdr=e.target.closest('.lg-ghdr');
  if(hdr){_lgActiveGroupName=(_lgActiveGroupName===hdr.dataset.ghdr)?null:hdr.dataset.ghdr;_lgRenderList();return;}
  const item=e.target.closest('.lg-sym-item');
  if(item){
    _lgActiveSym=item.dataset.sym;
    loadLiteChart(_lgActiveSym);
    DOM.lgList.querySelectorAll('.lg-sym-item').forEach(el=>el.classList.toggle('on',el.dataset.sym===_lgActiveSym));
    _lgScheduleOffTimer();
  }
});
DOM.lgList?.addEventListener('mousemove',e=>{if(e.movementX===0&&e.movementY===0)return;document.body.classList.remove('key-nav');});
DOM.lgList?.addEventListener('mouseleave',()=>{DOM.lgList.querySelectorAll('.lg-sym-item.on').forEach(el=>el.classList.remove('on'));});
DOM.lgList?.addEventListener('dragstart',e=>{
  const ghdr=e.target.closest('.lg-ghdr[draggable="true"]');
  if(ghdr){
    _lgDragGroup=ghdr.dataset.ghdr;
    ghdr.classList.add('dragging');
    try{e.dataTransfer.effectAllowed='move';e.dataTransfer.setData('text/plain',_lgDragGroup);}catch(err){}
    return;
  }
  const item=e.target.closest('.lg-sym-item[draggable="true"]');
  if(!item){e.preventDefault();return;}
  _lgDragSym=item.dataset.sym;
  item.classList.add('dragging');
  try{e.dataTransfer.effectAllowed='move';e.dataTransfer.setData('text/plain',_lgDragSym);}catch(err){}
});
DOM.lgList?.addEventListener('dragend',e=>{
  const ghdr=e.target.closest('.lg-ghdr');
  if(ghdr)ghdr.classList.remove('dragging');
  const item=e.target.closest('.lg-sym-item');
  if(item)item.classList.remove('dragging');
  if(_lgDragOverEl){_lgDragOverEl.classList.remove('drag-over');_lgDragOverEl=null;}
  _lgDragSym=null;_lgDragGroup=null;
});
DOM.lgList?.addEventListener('dragover',e=>{
  if(_lgDragGroup){
    const ghdr=e.target.closest('.lg-ghdr[draggable="true"]');
    if(!ghdr||ghdr.dataset.ghdr===_lgDragGroup)return;
    e.preventDefault();
    try{e.dataTransfer.dropEffect='move';}catch(err){}
    if(_lgDragOverEl&&_lgDragOverEl!==ghdr)_lgDragOverEl.classList.remove('drag-over');
    ghdr.classList.add('drag-over');
    _lgDragOverEl=ghdr;
    return;
  }
  if(!_lgDragSym)return;
  const item=e.target.closest('.lg-sym-item[draggable="true"]');
  if(!item||item.dataset.sym===_lgDragSym)return;
  if(FOLLOW.includes(_lgDragSym)!==FOLLOW.includes(item.dataset.sym))return;
  e.preventDefault();
  try{e.dataTransfer.dropEffect='move';}catch(err){}
  if(_lgDragOverEl&&_lgDragOverEl!==item)_lgDragOverEl.classList.remove('drag-over');
  item.classList.add('drag-over');
  _lgDragOverEl=item;
});
DOM.lgList?.addEventListener('drop',e=>{
  if(_lgDragGroup){
    const ghdr=e.target.closest('.lg-ghdr[draggable="true"]');
    if(!ghdr)return;
    e.preventDefault();
    ghdr.classList.remove('drag-over');
    if(_lgDragOverEl===ghdr)_lgDragOverEl=null;
    const target=ghdr.dataset.ghdr;
    if(target&&target!==_lgDragGroup)_lgReorderGroup(_lgDragGroup,target);
    return;
  }
  if(!_lgDragSym)return;
  const item=e.target.closest('.lg-sym-item[draggable="true"]');
  if(!item)return;
  e.preventDefault();
  item.classList.remove('drag-over');
  if(_lgDragOverEl===item)_lgDragOverEl=null;
  const targetSym=item.dataset.sym;
  if(targetSym!==_lgDragSym)_lgReorderFavorite(_lgDragSym,targetSym);
});
// Điều hướng phím lên/xuống trong nhóm đang mở
let _lgKeyThrottle=false,_lgKeyLoadTimer=null;
document.addEventListener('keydown',e=>{
  if(!DOM.lgSidebar||!DOM.lgSidebar.classList.contains('on')||!_lgActiveGroupName)return;
  if(document.activeElement&&['INPUT','TEXTAREA'].includes(document.activeElement.tagName))return;
  if(e.key!=='ArrowUp'&&e.key!=='ArrowDown')return;
  if(DOM.overlay&&DOM.overlay.classList.contains('on'))return;
  e.preventDefault();
  document.body.classList.add('key-nav');
  if(_lgKeyThrottle)return;_lgKeyThrottle=true;setTimeout(()=>{_lgKeyThrottle=false;},60);
  const items=[...DOM.lgList.querySelectorAll('.lg-group.open .lg-sym-item')];
  if(!items.length)return;
  let cur=items.findIndex(el=>el.dataset.sym===_lgActiveSym);
  let next=cur===-1?0:(e.key==='ArrowDown'?cur+1:cur-1);
  next=Math.max(0,Math.min(next,items.length-1));
  if(next===cur&&cur!==-1)return;
  _lgActiveSym=items[next].dataset.sym;
  items.forEach((el,i)=>el.classList.toggle('on',i===next));
  _lgScheduleOffTimer();
  const el=items[next],relTop=el.offsetTop-DOM.lgList.offsetTop,h=el.offsetHeight;
  if(relTop-h*2.5<DOM.lgList.scrollTop)DOM.lgList.scrollTop=Math.max(0,relTop-h*2.5);
  else if(relTop+h*2>DOM.lgList.scrollTop+DOM.lgList.clientHeight)DOM.lgList.scrollTop=relTop+h*2-DOM.lgList.clientHeight;
  // Debounce load (300ms): lướt nhanh qua nhiều mã chỉ tải đúng mã dừng lại, không tải từng mã đã đi qua — huỷ lịch cũ mỗi lần có phím mới.
  clearTimeout(_lgKeyLoadTimer);
  _lgKeyLoadTimer=setTimeout(()=>loadLiteChart(_lgActiveSym),300);
});
// Click ra ngoài khu vực sidebar (ví dụ vào khung chart) sau khi đã lướt mã bằng phím lên/xuống: bỏ nền xanh (bỏ focus) khỏi ô mã cuối cùng đang được tô trong cột danh sách.
document.addEventListener('click',e=>{
  if(!DOM.lgSidebar||!DOM.lgSidebar.classList.contains('on'))return;
  if(DOM.lgSidebar.contains(e.target))return;
  DOM.lgList?.querySelectorAll('.lg-sym-item.on').forEach(el=>el.classList.remove('on'));
});
// CHART POPOUT (mở panel CHART trong cửa sổ riêng, đồng bộ mã 2 chiều)
let _chartPopoutWin=null;
// CHART_POPOUT_CONTENT_H tính sẵn từ CSS cố định (720 chart+80 header+18 padding+34 footer); scrollbars=yes dự phòng.
const CHART_POPOUT_CONTENT_H=720+80+18+34;
// Hệ số thu hẹp bề rộng cửa sổ popout so với bề rộng tối đa ban đầu — 2 lần giảm dồn lại (giảm 10% rồi giảm thêm 5% nữa): 0.9 * 0.95 = 0.855.
const CHART_POPOUT_WIDTH_RATIO=0.855;
const CHART_POPOUT_ALIGN_KEY='chart_popout_align';
const getChartPopoutAlign=()=>_liteLSGet(CHART_POPOUT_ALIGN_KEY,'right');
function updatePopoutAlignBtn(){
  const a=getChartPopoutAlign(),btn=document.getElementById('lite-chart-popout-align-btn');
  if(btn){btn.textContent=a==='left'?'◧':'◨';btn.title=a==='left'?'Căn TRÁI (Bấm để đổi sang Phải)':'Căn PHẢI (Bấm để đổi sang Trái)';}
}
function togglePopoutAlign(){
  const next=getChartPopoutAlign()==='left'?'right':'left';
  _liteLSSet(CHART_POPOUT_ALIGN_KEY,next);
  updatePopoutAlignBtn();
  const box=_getPopupViewport(),w=_isChartPopoutWindow?(window.outerWidth||window.innerWidth):(_chartPopoutWin?.outerWidth||Math.min(1600,box.width-40)*CHART_POPOUT_WIDTH_RATIO);
  const nextLeft=next==='left'?box.left:(box.left+Math.max(0,box.width-w)),targetWin=_isChartPopoutWindow?window:_chartPopoutWin;
  if(targetWin&&!targetWin.closed){try{targetWin.moveTo(nextLeft,box.top);}catch(e){}}
  _chartSyncChannel?.postMessage({type:'CHART_POPOUT_ALIGN_SYNC'});
}
const CHART_POPOUT_SIZE_KEY='chart_popout_size';
function openChartPopout(){
  if(_chartPopoutWin&&!_chartPopoutWin.closed){_chartPopoutWin.focus();return;}
  const sym=_liteSymbol||'VNINDEX',box=_getPopupViewport();
  let w=Math.min(1600,box.width-40)*CHART_POPOUT_WIDTH_RATIO,h=Math.min(box.height,CHART_POPOUT_CONTENT_H);
  try{const s=JSON.parse(_liteLSGet(CHART_POPOUT_SIZE_KEY,'{}')||'{}');if(s.w>300)w=Math.min(box.width,s.w);if(s.h>200)h=Math.min(box.height,s.h);}catch(e){}
  const align=getChartPopoutAlign(),url=window.location.origin+window.location.pathname+'?chartPopout=1&sym='+encodeURIComponent(sym);
  _chartPopoutWin=_openMaximizedWindow(url,'ChartPopout',w,h,0,0,'scrollbars=yes',align);
  if(!_chartPopoutWin){alert('Trình duyệt chặn popup!');return;}
}
document.getElementById('lite-chart-popout-align-btn')?.addEventListener('click',togglePopoutAlign);
document.getElementById('lite-chart-popout-btn')?.addEventListener('click',openChartPopout);
function _onPopoutSyncMessage(e){
  if(!e||!e.data)return;
  const d=e.data;
  if(d.type==='CHART_POPOUT_ALIGN_SYNC'){
    updatePopoutAlignBtn();
    if(_isChartPopoutWindow){
      const a=getChartPopoutAlign(),b=_getPopupViewport(),w=window.outerWidth||window.innerWidth;
      try{window.moveTo(a==='left'?b.left:(b.left+Math.max(0,b.width-w)),b.top);}catch(err){}
    }
  }else if(d.type==='CHART_POPOUT_SYNC'&&d.symbol){
    const s=String(d.symbol).toUpperCase().trim();
    if(s&&s!==_liteSymbol){
      loadLiteChart(s,0,true);
    }
  }else if(d.type==='CHART_DRAW_SYNC'&&d.sym===_liteSymbol){
    loadLiteDrawings();redrawLiteDrawings();
  }else if(d.type==='FAVORITE_SYNC'){
    LG_FAVORITES=_lgLoadFavorites();
    _lgRenderList();
    _lgUpdateChartFavBtn();
  }else if(d.type==='JOURNAL_SYM_CLICK'&&d.symbol){
    const sym=String(d.symbol).toUpperCase().trim();
    if(sym)_hmapDesktopClick(sym);
  }else if(d.type==='JOURNAL_SYM_DBLCLICK'&&d.symbol){
    const sym=String(d.symbol).toUpperCase().trim();
    if(sym)openChart(sym);
  }else if(d.type==='JOURNAL_CLOSE'){
    closeJournal();
  }else if(d.type==='CHART_EMBED_SYM_CHANGE'&&d.symbol){
    const s=String(d.symbol).toUpperCase().trim();
    if(!s)return;
    _sym=s;
    _updateSymDisplay(s);
    if(_tab==='vs')DOM.ifVs.src='https://ta.vietstock.vn/?stockcode='+s.toLowerCase();
    ['vnd-cs','vnd-news','vnd-sum','24h'].forEach(t=>{
      if(_tab===t&&IFRAME_LAZY[t]){const f=$('iframe-'+t);if(f)f.src=IFRAME_LAZY[t](s);}
    });
  }
}
window.addEventListener('message',_onPopoutSyncMessage);
_chartSyncChannel?.addEventListener('message',_onPopoutSyncMessage);
(function(){
  if(!_isChartPopoutWindow)return;
  _liteLSSet('chart_popout_open','1');
  window.addEventListener('beforeunload',()=>{_liteLSSet('chart_popout_open','0');});
  document.documentElement.classList.add('chart-popout-mode');
  DOM.liteChartPanel.classList.remove('collapsed');
  const qsym=(new URLSearchParams(window.location.search).get('sym')||'').trim();
  if(qsym){_liteSymbol=qsym.toUpperCase();}
  if(!IS_MOBILE()){
    let _tmr=null;
    window.addEventListener('resize',()=>{
      clearTimeout(_tmr);
      _tmr=setTimeout(()=>{if(window.outerWidth>300)_liteLSSet(CHART_POPOUT_SIZE_KEY,JSON.stringify({w:window.outerWidth,h:window.outerHeight}));},300);
    });
  }
})();

// INIT
async function init(){
  initSoundNotifyBtn();
  initDesktopNotifyBtn();
  updatePopoutAlignBtn();
  bindLiteChartControls();
  bindAlertControls();
  if(IS_MOBILE()){
    DOM.liteChartPanel.classList.remove('collapsed');
    _isChartPanelOpen=true;
  }
  // Tải CHART ngay lập tức, không chờ config/tín hiệu/heatmap/health (chạy song song thay vì tuần tự) để chart không phụ thuộc API khác.
  loadLiteChart(_liteSymbol);
  await loadConfig();
  startBar(DOM.pbarSig,SIG_TTL);
  await Promise.all([fetchSigs(),fetchHmap(),fetchHealth(),loadAlerts(),pollAlertFeed(false)]);
  setInterval(async()=>{startBar(DOM.pbarSig,SIG_TTL);await fetchSigs();},SIG_TTL*1000);
  setInterval(fetchHealth,HEALTH_TTL*1000);
  setInterval(()=>pollAlertFeed(true),ALERT_POLL_SEC*1000);
  setInterval(_liteQuietRefreshChart,LITE_CHART_AUTOREFRESH_SEC*1000);
}
// Tính lại layout thẻ CHART (main+RSI+MACD+pane+right offset). Tách hàm riêng vì orientationchange
// gọi lại nhiều lần — trên iOS Safari, đo 1 lần dễ kẹt kích thước cũ khiến chart lệch vị trí.
function _liteRelayoutViewport(){
  // Popout CHART trên mobile: reset scroll về 0 sau mỗi lần đo — iOS Safari giữ scrollTop cũ khi
  // 100dvh co giãn. Chỉ áp dụng IS_MOBILE() vì popout cũng mở trên desktop, không tự cuộn ở đó.
  if(_isChartPopoutWindow&&IS_MOBILE()){window.scrollTo(0,0);document.documentElement.scrollTop=0;document.body.scrollTop=0;}
  _liteApplyChartSizes();
  // Tính lại pane layout (totalH thay đổi) và số nến hiển thị (portrait↔landscape)
  if(_liteData.length){applyLitePaneLayout();setLiteRightOffset();}
}
window.addEventListener('orientationchange',()=>{
  // Đo lại nhiều mốc thời gian (150/350/600/900ms) — lần đo cuối ghi đè lần sai trước, đảm bảo khớp
  // khi dvh/viewport ổn định. orientationchange chỉ bắn trên di động nên khỏi cần IS_MOBILE().
  [0,150,350,600,900].forEach(delay=>setTimeout(_liteRelayoutViewport,delay));
});
init();
"""