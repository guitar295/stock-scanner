"""
DASHBOARD SERVER
"""

from flask import Flask, jsonify, Response, request, session, send_from_directory
from functools import wraps
from pathlib import Path
import base64
import hmac
import json
import math
import os
import sqlite3
import threading
import time
from datetime import datetime
from uuid import uuid4
import pytz

TZ_VN = pytz.timezone('Asia/Ho_Chi_Minh')
app = Flask(__name__)
app.secret_key = os.environ.get("DASHBOARD_SECRET_KEY", "change-this-dashboard-secret-key")
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024

_get_alerted_today = None
_get_momentum_today = None
_get_history_cache = None
_cache_lock = None
_fetch_heatmap_fn = None
_fetch_chart_fn = None
_fetch_chart_15m_fn = None
_ensure_chart_symbol_fn = None
_chart_symbol_status_fn = None
_signal_emoji = {}
_signal_rank = {}

_heatmap_cache = {"data": {}, "ts": "", "updated_at": 0}
_heatmap_lock = threading.Lock()
HEATMAP_TTL_SEC = 120
SIGNAL_TTL_SEC = 10

_chart_cache: dict = {}
_chart_lock = threading.Lock()
CHART_TTL_SEC = 120

JOURNAL_DATA_DIR = Path(os.environ.get("DASHBOARD_DATA_DIR", "/data/trade-journal")).expanduser()
JOURNAL_UPLOAD_DIR = JOURNAL_DATA_DIR / "uploads"
JOURNAL_DB_PATH = JOURNAL_DATA_DIR / "trade_journal.sqlite"
JOURNAL_WARNING_PATH = JOURNAL_DATA_DIR / "market_warning.txt"
JOURNAL_ALLOWED_EXT = {"png", "jpg", "jpeg", "webp", "gif"}
_journal_lock = threading.Lock()

HMAP_COLS_CONFIG = [
    {"groups": [{"name": "VN30", "syms": ["FPT", "GAS", "NVL", "VNM", "VCB", "PLX", "TCB", "MWG", "STB", "HPG", "PNJ", "BID", "CTG", "HDB", "VJC", "VPB", "KDH", "MBB", "VHM", "POW", "VRE", "MSN", "SSI", "ACB", "BVH", "GVR", "TPB"]}]},
    {"groups": [{"name": "NGÂN HÀNG", "syms": ["VCB", "BID", "CTG", "MBB", "ACB", "TCB", "TPB", "HDB", "SHB", "STB", "VIB", "VPB", "MSB", "ABB", "BVB", "LPB"]}, {"name": "DẦU KHÍ", "syms": ["GAS", "PVD", "PVS", "BSR", "OIL", "PVB", "PVC", "PLX", "PET", "PVT"]}]},
    {"groups": [{"name": "CHỨNG KHOÁN", "syms": ["SSI", "VND", "CTS", "FTS", "HCM", "MBS", "DSE", "BSI", "SHS", "VCI", "VCK", "ORS"]}, {"name": "XÂY DỰNG", "syms": ["C47", "C32", "L14", "CII", "CTD", "CTI", "FCN", "HBC", "HUT", "LCG", "PC1", "DPG", "PHC", "VCG"]}]},
    {"groups": [{"name": "BẤT ĐỘNG SẢN", "syms": ["VHM", "AGG", "IJC", "LDG", "CEO", "D2D", "DIG", "DXG", "HDC", "HDG", "KDH", "NLG", "NTL", "NVL", "PDR", "SCR", "TIG", "KBC", "SZC"]}, {"name": "PHÂN BÓN", "syms": ["BFC", "DCM", "DPM"]}, {"name": "THÉP", "syms": ["HPG", "HSG", "NKG"]}]},
    {"groups": [{"name": "BÁN LẺ", "syms": ["MSN", "FPT", "FRT", "MWG", "PNJ", "DGW"]}, {"name": "THỦY SẢN", "syms": ["ANV", "FMC", "CMX", "VHC", "IDI"]}, {"name": "CẢNG BIỂN", "syms": ["HAH", "GMD", "SGP", "VSC"]}, {"name": "CAO SU", "syms": ["GVR", "DPR", "DRI", "PHR", "DRC"]}, {"name": "NHỰA", "syms": ["AAA", "BMP", "NTP"]}]},
    {"groups": [{"name": "ĐIỆN NƯỚC", "syms": ["NT2", "PC1", "GEG", "GEX", "POW", "TDM", "BWE"]}, {"name": "DỆT MAY", "syms": ["TCM", "TNG", "VGT", "MSH"]}, {"name": "HÀNG KHÔNG", "syms": ["NCT", "ACV", "AST", "HVN", "SCS", "VJC"]}, {"name": "BẢO HIỂM", "syms": ["BMI", "MIG", "BVH"]}, {"name": "MÍA ĐƯỜNG", "syms": ["LSS", "SBT", "QNS"]}]},
    {"groups": [{"name": "ĐẦU TƯ CÔNG", "syms": ["FCN", "HHV", "LCG", "VCG", "C4G", "CTD", "HBC", "HSG", "NKG", "HPG", "KSB", "PLC"]}]},
]

TS_POOL_CONFIG = ["AAA", "ACB", "AGG", "ANV", "BFC", "BID", "BMI", "BSR", "BVB", "BVH", "BWE", "CII", "CKG", "CRE", "CTD", "CTG", "CTI", "CTR", "CTS", "D2D", "DBC", "DCM", "DSE", "DGW", "DIG", "DPG", "DPM", "DRC", "DRH", "DXG", "FCN", "FMC", "FPT", "FRT", "FTS", "GAS", "GEG", "GEX", "GMD", "GVR", "HAG", "HAX", "HBC", "HCM", "HDB", "HDC", "VCK", "HDG", "HNG", "HPG", "HSG", "HTN", "HVN", "IDC", "IJC", "KBC", "KDH", "KSB", "LCG", "LDG", "LPB", "LTG", "MBB", "MBS", "MSB", "MSN", "MWG", "NKG", "NLG", "NTL", "NVL", "PC1", "PDR", "PET", "PHR", "PLC", "PLX", "PNJ", "POW", "PTB", "PVD", "PVS", "PVT", "QNS", "REE", "SBT", "SCR", "SHB", "SHS", "SSI", "STB", "SZC", "TCB", "TDM", "TIG", "TNG", "TPB", "TV2", "VCB", "VCI", "VCS", "VGT", "VHC", "VHM", "VIB", "VIC", "VJC", "VNM", "VPB", "VRE"]


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


def _init_journal_storage():
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

# =============================================================================
# API
# =============================================================================
def _serve_chart_images(symbol, fetch_fn, cache_key, label):
    symbol = symbol.upper().strip()
    now = time.time()
    with _chart_lock:
        cached = _chart_cache.get(cache_key)
        if cached and (now - cached["updated_at"]) < CHART_TTL_SEC:
            return jsonify({"symbol": symbol, "images": cached["images"],
                            "labels": cached["labels"], "cached": True})
    if not fetch_fn:
        return jsonify({"error": f"{label}_fn_not_registered"}), 503
    try:
        ts = datetime.now(TZ_VN).strftime('%H:%M:%S')
        print(f"  [Dashboard] 📊 Tạo {label} {symbol}...")
        png_list, labels = fetch_fn(symbol)
        if not png_list:
            return jsonify({"error": "no_data"}), 404
        b64_list = [base64.b64encode(b).decode() for b in png_list]
        with _chart_lock:
            _chart_cache[cache_key] = {"images": b64_list, "labels": labels,
                                       "updated_at": time.time()}
        print(f"  [Dashboard] ✅ {label} {symbol}: {len(b64_list)} ảnh ({ts}→{datetime.now(TZ_VN).strftime('%H:%M:%S')})")
        return jsonify({"symbol": symbol, "images": b64_list,
                        "labels": labels, "cached": False})
    except Exception as e:
        print(f"  [Dashboard] ❌ {label} {symbol} lỗi: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/api/signals")
def api_signals():
    alerted = _get_alerted_today() if _get_alerted_today else {}
    momentum = _get_momentum_today() if _get_momentum_today else {}
    result = []
    for sym, entry in alerted.items():
        sig = entry["signal"] if isinstance(entry, dict) else entry
        pct = entry.get("pct") if isinstance(entry, dict) else None
        emoji = _signal_emoji.get(sig, "📌")
        rank  = _signal_rank.get(sig, 0)
        result.append({"symbol": sym, "signal": sig, "emoji": emoji,
                        "rank": rank, "pct": pct})
    result.sort(key=lambda x: x["rank"], reverse=True)
    momentum_result = []
    for sig in ("MACD_W", "MACD_M", "RTM"):
        rows = []
        for sym in sorted(momentum.keys()):
            entry = momentum[sym]
            sigs = entry.get("signals", []) if isinstance(entry, dict) else []
            if sig not in sigs:
                continue
            pct = entry.get("pct") if isinstance(entry, dict) else None
            rows.append({"symbol": sym, "signal": sig, "pct": pct})
        momentum_result.extend(rows)
    return jsonify({
        "signals": result,
        "count":   len(result),
        "momentum": momentum_result,
        "momentum_count": len(momentum_result),
        "updated_at": datetime.now(TZ_VN).strftime("%H:%M:%S"),
    })

@app.route("/api/heatmap")
def api_heatmap():
    now = time.time()
    with _heatmap_lock:
        if now - _heatmap_cache["updated_at"] > HEATMAP_TTL_SEC and _fetch_heatmap_fn:
            try:
                data, ts_str = _fetch_heatmap_fn()
                _heatmap_cache["data"] = data
                _heatmap_cache["ts"]   = ts_str
                _heatmap_cache["updated_at"] = time.time()
            except Exception as e:
                print(f"  [Dashboard] ❌ Fetch heatmap lỗi: {e}")
        snap_time = _heatmap_cache["updated_at"]
    return jsonify({
        "data":      _json_safe(_heatmap_cache["data"]),
        "timestamp": _heatmap_cache["ts"],
        "cached_age": int(now - snap_time),
    })

@app.route("/api/chart_images/<symbol>")
def api_chart_images(symbol):
    symbol = symbol.upper().strip()
    return _serve_chart_images(symbol, _fetch_chart_fn, symbol, "chart")

@app.route("/api/chart_image_15m/<symbol>")
def api_chart_image_15m(symbol):
    symbol = symbol.upper().strip()
    return _serve_chart_images(symbol, _fetch_chart_15m_fn, f"{symbol}:15m", "chart_15m")

@app.route("/api/lightweight_chart_status/<symbol>")
def api_lightweight_chart_status(symbol):
    """Kiểm tra nhanh, không gọi mạng: chart sẽ phục vụ từ cache hay cần
    update (vnstock) — dùng để hiển thị trạng thái trước khi tải dữ liệu."""
    symbol = symbol.upper().strip()
    if _chart_symbol_status_fn:
        try:
            return jsonify(_chart_symbol_status_fn(symbol))
        except Exception as exc:
            return jsonify({"symbol": symbol, "cached": False, "need_fetch": True,
                            "reason": "status_error", "detail": str(exc)})
    cache = _get_history_cache() if _get_history_cache else {}
    if not _cache_lock:
        return jsonify({"symbol": symbol, "cached": False, "need_fetch": True, "reason": "unknown"})
    with _cache_lock:
        df = cache.get(symbol)
        has_cache = df is not None and len(df) >= 60
    return jsonify({"symbol": symbol, "cached": has_cache, "need_fetch": not has_cache,
                    "reason": "fallback_check"})

@app.route("/api/lightweight_chart/<symbol>")
def api_lightweight_chart(symbol):
    symbol = symbol.upper().strip()
    tf = (request.args.get("tf") or "1D").upper()
    try:
        limit = int(request.args.get("limit", 320) or 320)
    except (TypeError, ValueError):
        limit = 320
    limit = max(50, min(1000, limit))
    if _ensure_chart_symbol_fn:
        try:
            _ensure_chart_symbol_fn(symbol)
        except Exception as exc:
            print(f"  [LiteChart] {symbol}: ensure cache lỗi: {exc}")
    cache = _get_history_cache() if _get_history_cache else {}
    if not _cache_lock:
        return jsonify({"error": "cache_not_ready"}), 503
    with _cache_lock:
        df = cache.get(symbol)
        df = df.copy() if df is not None and len(df) else None
    if df is None or df.empty:
        return jsonify({"error": "no_cache", "symbol": symbol}), 404
    if tf in ("1W", "W", "WEEK", "WEEKLY"):
        try:
            df = df.resample("W-FRI").agg({
                "open": "first", "high": "max", "low": "min",
                "close": "last", "volume": "sum"
            }).dropna(subset=["open", "high", "low", "close"])
            tf = "1W"
        except Exception as exc:
            return jsonify({"error": "weekly_resample_failed", "detail": str(exc)}), 500
    else:
        tf = "1D"
    df = df.tail(limit)
    candles, volume = [], []
    for idx, row in df.iterrows():
        try:
            o = float(row.get("open", 0) or 0)
            h = float(row.get("high", 0) or 0)
            l = float(row.get("low", 0) or 0)
            c = float(row.get("close", 0) or 0)
            v = float(row.get("volume", 0) or 0)
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(x) and x > 0 for x in (o, h, l, c)):
            continue
        day = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
        candles.append({"time": day, "open": o, "high": h, "low": l, "close": c})
        volume.append({"time": day, "value": max(0, v),
                       "color": "#26a69a" if c >= o else "#ef5350"})
    if not candles:
        return jsonify({"error": "no_data", "symbol": symbol}), 404
    return jsonify({"symbol": symbol, "timeframe": tf, "candles": candles, "volume": volume,
                    "last_date": candles[-1]["time"]})

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

@app.route("/api/chart_cache_clear/<symbol>", methods=["DELETE"])
def api_chart_cache_clear(symbol):
    symbol = symbol.upper().strip()
    with _chart_lock:
        removed = symbol in _chart_cache or f"{symbol}:15m" in _chart_cache
        _chart_cache.pop(symbol, None)
        _chart_cache.pop(f"{symbol}:15m", None)
    return jsonify({"symbol": symbol, "cleared": removed})

@app.route("/api/config")
def api_config():
    return jsonify({"signal_ttl_sec": SIGNAL_TTL_SEC,
                    "heatmap_ttl_sec": HEATMAP_TTL_SEC})

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
    return jsonify({"ok": True})

@app.route("/popout_full/<symbol>")
def popout_full(symbol):
    return Response(POPOUT_FULL_HTML.replace("__SYMBOL__", symbol.upper().strip()),
                    mimetype="text/html")

@app.route("/")
def index():
    html = (
        DASHBOARD_HTML
        .replace("__HMAP_COLS_CONFIG__", json.dumps(HMAP_COLS_CONFIG, ensure_ascii=False))
        .replace("__TS_POOL_CONFIG__", json.dumps(TS_POOL_CONFIG, ensure_ascii=False))
    )
    return Response(html, mimetype="text/html")

# =============================================================================
# START
# =============================================================================
def start_dashboard(alerted_today_ref, history_cache_ref, cache_lock_ref,
                    fetch_heatmap_fn, signal_emoji_ref, signal_rank_ref,
                    fetch_chart_fn=None, fetch_chart_15m_fn=None,
                    ensure_chart_symbol_fn=None,
                    chart_symbol_status_fn=None,
                    momentum_today_ref=None, port=8888):
    global _get_alerted_today, _get_momentum_today, _get_history_cache, _cache_lock
    global _fetch_heatmap_fn, _fetch_chart_fn, _fetch_chart_15m_fn, _ensure_chart_symbol_fn, _chart_symbol_status_fn, _signal_emoji, _signal_rank
    _get_alerted_today = alerted_today_ref
    _get_momentum_today = momentum_today_ref
    _get_history_cache = history_cache_ref
    _cache_lock        = cache_lock_ref
    _fetch_heatmap_fn  = fetch_heatmap_fn
    _fetch_chart_fn    = fetch_chart_fn
    _fetch_chart_15m_fn = fetch_chart_15m_fn
    _ensure_chart_symbol_fn = ensure_chart_symbol_fn
    _chart_symbol_status_fn = chart_symbol_status_fn
    _signal_emoji      = signal_emoji_ref
    _signal_rank       = signal_rank_ref

    def _run():
        import logging
        logging.getLogger('werkzeug').setLevel(logging.ERROR)
        app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)

    threading.Thread(target=_run, daemon=True).start()
    print(f"🌐 Dashboard tại http://0.0.0.0:{port}")
    print(f"   Tín hiệu: {SIGNAL_TTL_SEC}s | Heatmap: {HEATMAP_TTL_SEC}s | Chart: {'✅' if fetch_chart_fn else '❌'}")

# =============================================================================
# POPOUT FULL HTML
# =============================================================================
POPOUT_FULL_HTML = r"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Full Chart — __SYMBOL__</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600;700&family=Barlow+Condensed:wght@600;700;800&display=swap" rel="stylesheet">
<script>
try{
  const qs=new URLSearchParams(window.location.search);
  if(qs.get('embedded')==='1')
    document.documentElement.classList.add('embedded-popout');
}catch(e){}
</script>
<style>
:root{--bg:#f4f6fb;--surface:#fff;--surf2:#f0f3f9;--border:#dde3ee;--accent:#1a56db;--red:#e02424;--text:#111827;--muted:#6b7280;--font-mono:'IBM Plex Mono',monospace;--font-ui:'Barlow Condensed',sans-serif}
*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%;overflow:hidden}
body{background:var(--bg);color:var(--text);font-family:var(--font-mono);font-size:13px}
.page{height:100vh;display:flex;flex-direction:column}
.phdr{display:flex;align-items:center;justify-content:center;padding:7px 10px;background:var(--surf2);border-bottom:1px solid var(--border);flex-shrink:0}
html.embedded-popout .phdr{display:none !important}
.phdr-center{display:flex;align-items:flex-end;justify-content:center}
.phdr-right{display:flex;align-items:center;justify-content:flex-end}
.ctabs{display:flex;gap:2px;align-items:center;flex-wrap:wrap;justify-content:center}
.ctab{height:30px;line-height:1;display:inline-flex;align-items:center;justify-content:center;font-size:11px;font-family:var(--font-mono);font-weight:600;padding:0 11px;border-radius:5px;border:1px solid var(--border);background:var(--bg);color:var(--muted);cursor:pointer;transition:all .15s;white-space:nowrap}
.ctab.on{background:var(--surface);color:var(--accent);border-color:var(--border);box-shadow:inset 0 -2px 0 var(--accent);font-weight:700}
.ctab:hover:not(.on){color:var(--accent);background:#eef3ff}
.phdr-right{margin-left:2px}
.closebtn{width:30px;height:30px;border-radius:5px;border:1px solid var(--border);background:var(--bg);color:var(--muted);font-size:16px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all .15s}
.closebtn:hover{background:var(--red);color:#fff;border-color:var(--red)}
.pbody{flex:1;overflow:hidden;position:relative;background:#fff}
.tpanel{position:absolute;inset:0;display:none}
.tpanel.on{display:block}
.tpanel iframe{width:100%;height:100%;border:none;display:block}
#panel-scanner{overflow:hidden;background:#fff;display:none;flex-direction:column}
#panel-scanner.on{display:flex}
.scanner-loading{display:flex;align-items:center;justify-content:center;flex:1;color:var(--muted);font-size:14px}
.album-outer{flex:1;display:flex;flex-direction:column;overflow:hidden}
.album-center{flex:1;overflow-y:auto;display:flex;flex-direction:column;align-items:center;padding:6px;gap:6px;background:#fff;scrollbar-width:thin}
.album-slide{display:none;flex-direction:column;align-items:center;width:100%}
.album-slide.on{display:flex}
.album-slide img{max-width:100%;max-height:calc(100vh - 140px);object-fit:contain;border-radius:3px;border:1px solid var(--border)}
.album-nav-bar{display:flex;align-items:center;justify-content:center;gap:10px;padding:6px 0 8px;flex-shrink:0}
.album-nav-btn{width:30px;height:30px;border-radius:50%;border:1px solid #dde3ee;background:#f4f6fb;color:var(--muted);font-size:14px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all .15s;flex-shrink:0;user-select:none}
.album-nav-btn:hover:not(.disabled){background:var(--accent);color:#fff;border-color:var(--accent)}
.album-nav-btn.disabled{opacity:.25;pointer-events:none}
.album-dots-wrap{display:flex;gap:6px;align-items:center}
.album-dot{width:8px;height:8px;border-radius:50%;background:#dde3ee;cursor:pointer;transition:all .15s}
.album-dot.on{background:var(--accent);transform:scale(1.3)}
.album-refresh-btn{width:30px;height:30px;border-radius:50%;border:1px solid #dde3ee;background:#f4f6fb;color:var(--muted);font-size:15px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all .15s;flex-shrink:0}
.album-refresh-btn:hover{background:#0e9f6e;color:#fff;border-color:#0e9f6e}
.album-refresh-btn.spinning span{display:inline-block;animation:spin .7s linear infinite}
.album-hint{text-align:center;font-size:10px;color:#9ca3af;padding:0 0 6px;flex-shrink:0}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes popIn{from{opacity:0;transform:scale(.96) translateY(14px)}to{opacity:1;transform:none}}
::-webkit-scrollbar{width:5px;height:5px}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
@media (min-width: 769px) {
    body.embedded-popout-desktop .phdr{display:none !important}
  }
@media(max-width:980px){
  .phdr{grid-template-columns:1fr;gap:8px}
  .phdr-center,.phdr-right{justify-content:center}
}
@media(max-width:768px){
  .phdr{display:flex !important;align-items:center !important;justify-content:flex-start !important;padding:4px 6px !important;gap:4px !important}
  .phdr-center{display:flex !important;flex:1;min-width:0;align-items:center !important;justify-content:flex-start !important}
  .phdr-right{display:flex !important;flex-shrink:0}
  .ctabs{display:flex !important;flex-wrap:nowrap !important;overflow-x:auto !important;overflow-y:hidden !important;justify-content:flex-start !important;align-items:center !important;gap:4px;width:100%;min-width:0;scrollbar-width:none;-ms-overflow-style:none;-webkit-overflow-scrolling:touch}
  .ctabs::-webkit-scrollbar{display:none}
  .ctab{flex:0 0 auto;display:inline-flex;align-items:center;justify-content:center;height:30px;padding:0 10px;border-radius:4px;border:1px solid var(--border);font-size:11px;white-space:nowrap}
  .ctab.on{border-color:var(--border);box-shadow:inset 0 -2px 0 var(--accent)}
  .closebtn{width:30px;height:30px;border-radius:4px;flex-shrink:0}
}
</style>
</head>
<body>
<div class="page">
  <div class="phdr">
    <div class="phdr-center">
      <div class="ctabs" id="ctabs">
        <button class="ctab on" data-tab="vs">📈 Vietstock</button>
        <button class="ctab" data-tab="scanner">🖼 Scanner Chart</button>
        <button class="ctab" data-tab="vnd-cs">⚖️ Cơ bản</button>
        <button class="ctab" data-tab="vnd-news">🗞️ Tin tức</button>
        <button class="ctab" data-tab="vnd-sum">📄 Tổng quan</button>
        <button class="ctab" data-tab="24h">💬 Fireant</button>
      </div>
    </div>
    <div class="phdr-right">
      <button class="closebtn" id="close-btn">✕</button>
    </div>
  </div>
  <div class="pbody">
    <div class="tpanel on" id="panel-vs"><iframe id="iframe-vs" src="about:blank" allowfullscreen></iframe></div>
    <div class="tpanel" id="panel-scanner">
      <div class="scanner-loading" id="scanner-loading"><span>⏳ Đang tạo chart từ scanner...</span></div>
      <div class="album-outer" id="album-outer" style="display:none">
        <div class="album-center"><div id="album-slides"></div></div>
        <div class="album-nav-bar">
          <button class="album-nav-btn disabled" id="btn-prev">&#9664;</button>
          <div class="album-dots-wrap" id="album-dots"></div>
          <button class="album-nav-btn" id="btn-next">&#9654;</button>
          <button class="album-refresh-btn" id="btn-refresh"><span>&#8635;</span></button>
        </div>
        <div class="album-hint">◀ ▶ hoặc phím ← → để chuyển ảnh</div>
      </div>
    </div>
    <div class="tpanel" id="panel-vnd-cs"><iframe id="iframe-vnd-cs" src="about:blank" allowfullscreen></iframe></div>
    <div class="tpanel" id="panel-vnd-news"><iframe id="iframe-vnd-news" src="about:blank" allowfullscreen></iframe></div>
    <div class="tpanel" id="panel-vnd-sum"><iframe id="iframe-vnd-sum" src="about:blank" allowfullscreen></iframe></div>
    <div class="tpanel" id="panel-24h"><iframe id="iframe-24h" src="about:blank" allowfullscreen></iframe></div>
  </div>
</div>
<script>
'use strict';
const $=id=>document.getElementById(id);
const DOM={
  ifVs:$('iframe-vs'),
  loading:$('scanner-loading'),outer:$('album-outer'),
  slides:$('album-slides'),dots:$('album-dots'),
  btnPrev:$('btn-prev'),btnNext:$('btn-next'),btnRef:$('btn-refresh'),
  ctabs:$('ctabs'),
};
const IFRAME_MAP={
  'vnd-cs': s=>`https://dstock.vndirect.com.vn/tong-quan/${s}/diem-nhan-co-ban-popup?theme=light`,
  'vnd-news':s=>`https://dstock.vndirect.com.vn/tong-quan/${s}/tin-tuc-ma-popup?type=dn&theme=light`,
  'vnd-sum': s=>`https://dstock.vndirect.com.vn/tong-quan/${s}?theme=light`,
  '24h':     s=>`https://fireant.vn/ma-chung-khoan/${s}`,
};
const TABS_ALL=['vs','scanner','vnd-cs','vnd-news','vnd-sum','24h'];
let _sym='__SYMBOL__',_tab='vs';
let _albumIdx=0,_albumTotal=0,_albumImages=[];
function _applyEmbeddedMode(){
  const qs=new URLSearchParams(window.location.search);
  const isEmbedded = qs.get('embedded')==='1';
  const isMobile = (window.innerWidth <= 768);
  document.documentElement.classList.toggle('embedded-popout', isEmbedded);
  document.body.classList.toggle('embedded-popout-mobile-full', isEmbedded && isMobile);
  document.body.classList.toggle('embedded-popout-desktop', isEmbedded && !isMobile);
}
window.addEventListener('resize', _applyEmbeddedMode);
window.addEventListener('orientationchange', _applyEmbeddedMode);
_applyEmbeddedMode();
function notifyHost(sym){
  try{
    if(window.self!==window.top)return window.parent.postMessage({type:'EMBEDDED_FULL_SYMBOL',symbol:sym},'*');
    if(window.opener&&!window.opener.closed)window.opener.postMessage({type:'POPOUT_SYM_SELECT',symbol:sym},'*');
  }catch(e){}
}
function handleClose(){
  try{if(window.self!==window.top)return window.parent.postMessage({type:'EMBEDDED_FULL_CLOSE',symbol:_sym},'*');}catch(e){}
  window.close();
}

DOM.ctabs.addEventListener('click',e=>{
  const btn=e.target.closest('.ctab');if(btn)_activateTab(btn.dataset.tab);
});
function _activateTab(tab){
  _tab=tab;
  DOM.ctabs.querySelectorAll('.ctab').forEach(b=>b.classList.toggle('on',b.dataset.tab===tab));
  TABS_ALL.forEach(t=>document.getElementById('panel-'+t).classList.toggle('on',t===tab));
  if(IFRAME_MAP[tab]){const f=$('iframe-'+tab);if(f&&f.src==='about:blank')f.src=IFRAME_MAP[tab](_sym);}
  if(tab==='scanner')loadScannerChart(_sym);
}

function setSymbol(sym){
  _sym=(sym||'').toUpperCase().trim();if(!_sym)return;
  document.title=_sym+' • Full Chart';
  DOM.ifVs.src='https://ta.vietstock.vn/?stockcode='+_sym.toLowerCase();
  Object.keys(IFRAME_MAP).forEach(t=>{const f=$('iframe-'+t);if(f)f.src='about:blank';});
  DOM.outer.style.display='none';
  DOM.loading.style.display='flex';
  DOM.loading.innerHTML='<span>⏳ Đang tạo chart từ scanner...</span>';
  _activateTab('vs');
  try{history.replaceState(null,'','/popout_full/'+_sym);}catch(e){}
  notifyHost(_sym);
}

function _showAlbum(images){
  _albumImages=images;_albumTotal=images.length;_albumIdx=0;
  DOM.slides.innerHTML=images.map((img,i)=>`<div class="album-slide${i===0?' on':''}" data-idx="${i}"><img src="${img.url}" alt="${img.label}" loading="lazy" decoding="async"></div>`).join('');
  DOM.dots.innerHTML=images.map((_,i)=>`<div class="album-dot${i===0?' on':''}" data-idx="${i}"></div>`).join('');
  _updateAlbumNav();
  DOM.outer.style.display='flex';DOM.loading.style.display='none';
}
function _appendAlbumImages(images){
  if(!images.length)return;
  const start=_albumImages.length;
  _albumImages=_albumImages.concat(images);_albumTotal=_albumImages.length;
  DOM.slides.insertAdjacentHTML('beforeend',images.map((img,i)=>{const idx=start+i;return`<div class="album-slide" data-idx="${idx}"><img src="${img.url}" alt="${img.label}" loading="lazy" decoding="async"></div>`;}).join(''));
  DOM.dots.insertAdjacentHTML('beforeend',images.map((_,i)=>`<div class="album-dot" data-idx="${start+i}"></div>`).join(''));
  _updateAlbumNav();
}
DOM.dots.addEventListener('click',e=>{const d=e.target.closest('.album-dot');if(d)albumGoto(+d.dataset.idx);});
DOM.btnPrev.addEventListener('click',()=>albumNav(-1));
DOM.btnNext.addEventListener('click',()=>albumNav(1));
function albumGoto(i){
  if(i<0||i>=_albumTotal)return;
  DOM.slides.querySelectorAll('.album-slide').forEach((s,idx)=>s.classList.toggle('on',idx===i));
  DOM.dots.querySelectorAll('.album-dot').forEach((d,idx)=>d.classList.toggle('on',idx===i));
  _albumIdx=i;_updateAlbumNav();
}
function albumNav(dir){albumGoto(_albumIdx+dir);}
function _updateAlbumNav(){
  DOM.btnPrev.classList.toggle('disabled',_albumIdx===0);
  DOM.btnNext.classList.toggle('disabled',_albumIdx===_albumTotal-1);
}
DOM.btnRef.addEventListener('click',async()=>{
  if(!_sym)return;
  DOM.btnRef.classList.add('spinning');DOM.btnRef.disabled=true;
  try{await fetch('/api/chart_cache_clear/'+_sym,{method:'DELETE'});}catch(e){}
  DOM.btnRef.classList.remove('spinning');DOM.btnRef.disabled=false;
  await loadScannerChart(_sym);
});
async function loadScannerChart(sym){
  DOM.outer.style.display='none';DOM.loading.style.display='flex';
  DOM.loading.innerHTML=`<span>⏳ Đang tạo chart <b>${sym}</b>…</span>`;
  try{
    const r=await fetch('/api/chart_images/'+sym);
    if(!r.ok){const j=await r.json().catch(()=>({}));throw new Error(j.error||'HTTP '+r.status);}
    const j=await r.json();
    if(!j.images?.length)throw new Error('no_images');
    const labels=j.labels||['📊 Daily [D]','📈 Weekly [W]'];
    _showAlbum(j.images.map((b64,i)=>({url:'data:image/png;base64,'+b64,label:labels[i]||'Chart '+(i+1)})));
    const h=DOM.outer.querySelector('.album-hint');
    if(h)h.textContent='Đang tải 15m...';
    loadScannerChart15m(sym);
  }catch(e){
    DOM.loading.innerHTML=`<div style="text-align:center;color:#aaa;padding:24px"><div style="font-size:24px;margin-bottom:10px">⚠️</div><div style="margin-bottom:8px">Không tải được chart <b style="color:#4d9ff5">${sym}</b></div><div style="font-size:11px;color:#666;margin-bottom:16px">${e.message}</div><div style="display:flex;gap:8px;justify-content:center"><button onclick="loadScannerChart('${sym}')" style="padding:6px 14px;border-radius:5px;background:#1a56db;color:#fff;border:none;cursor:pointer;font-size:12px">🔄 Thử lại</button><a href="https://ta.vietstock.vn/?stockcode=${sym.toLowerCase()}" target="_blank" style="padding:6px 14px;border-radius:5px;background:#374151;color:#fff;text-decoration:none;font-size:12px">📈 Stockchart</a></div></div>`;
  }
}
async function loadScannerChart15m(sym){
  const s=(sym||'').toUpperCase().trim();
  try{
    const r=await fetch('/api/chart_image_15m/'+s);
    if(!r.ok){const h=DOM.outer.querySelector('.album-hint');if(h)h.textContent='';return;}
    const j=await r.json();
    if((_sym||'').toUpperCase().trim()!==s)return;
    if(!j.images?.length)return;
    const labels=j.labels||['⚡ 15 phút [15m]'];
    _appendAlbumImages(j.images.map((b64,i)=>({url:'data:image/png;base64,'+b64,label:labels[i]||'15m'})));
    const h=DOM.outer.querySelector('.album-hint');
    if(h)h.textContent='';
  }catch(e){
    const h=DOM.outer.querySelector('.album-hint');
    if(h)h.textContent='';
  }
}
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'){window.close();return;}
  if(_tab!=='scanner'||_albumTotal===0)return;
  if(e.key==='ArrowLeft'){e.preventDefault();albumNav(-1);}
  if(e.key==='ArrowRight'){e.preventDefault();albumNav(1);}
});
$('close-btn').addEventListener('click',handleClose);
window.addEventListener('message',e=>{if(e.data.type==='UPDATE_CHART'&&e.data.symbol)setSymbol(e.data.symbol);});
_applyEmbeddedMode();
setSymbol(_sym);
</script>
</body>
</html>
"""

# =============================================================================
# TRADE JOURNAL HTML
# =============================================================================
JOURNAL_HTML = r"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Note</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600;700&family=Barlow+Condensed:wght@600;700;800&display=swap" rel="stylesheet">
<style>
:root{--bg:#f6f7fb;--surface:#fff;--surf2:#eef2f7;--border:#dbe2ec;--text:#111827;--muted:#6b7280;--accent:#1a56db;--green:#0e9f6e;--red:#e02424;--yellow:#b45309;--font-mono:'IBM Plex Mono',monospace;--font-ui:'Barlow Condensed',sans-serif}
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
async function uploadImages(entryId,files){if(!files||!files.length)return;const fd=new FormData();[...files].forEach(f=>fd.append('images',f));await api('/api/journal/entries/'+entryId+'/images',{method:'POST',body:fd});}
function postSym(sym,type){if(window.parent)window.parent.postMessage({type:type,symbol:sym},'*');}
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

# =============================================================================
# DASHBOARD HTML
# =============================================================================
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Scanner Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600;700&family=Barlow+Condensed:wght@600;700;800&display=swap" rel="stylesheet">
<style>
/* ═══════════════════════════════════════════
   VARIABLES & RESET
   ═══════════════════════════════════════════ */
:root{
  --bg:#f4f6fb;--surface:#fff;--surf2:#f0f3f9;--border:#dde3ee;
  --accent:#1a56db;--green:#0e9f6e;--red:#e02424;
  --text:#111827;--muted:#6b7280;--shadow:rgba(0,0,0,.07);
  --font-mono:'IBM Plex Mono',monospace;--font-ui:'Barlow Condensed',sans-serif;
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
.hdr-right{display:flex;gap:18px;align-items:center;flex-shrink:0}
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
#hmap-simplize-btn{color:var(--muted)}
.hmap-search-wrap{position:relative;display:flex;align-items:center}
.hmap-search-wrap .s-icon{position:absolute;left:11px;top:50%;transform:translateY(-50%);color:var(--muted);font-size:13px;pointer-events:none}
.hmap-search-input{width:100px;padding:5px 10px 5px 30px;border-radius:20px;border:1px solid var(--border);background:var(--surface);color:var(--text);font-family:var(--font-mono);font-size:11px;outline:none;transition:border-color .15s,width .2s}
.hmap-search-input:focus{border-color:var(--accent);box-shadow:0 0 0 2px rgba(26,86,219,.12);width:120px}
#hmap-follow-btn{color:var(--muted)}
#hmap-follow-btn.on{background:#fef3c7;color:#92400e;border-color:#f59e0b}

/* ═══════════════════════════════════════════
   SIGNALS
   ═══════════════════════════════════════════ */
.sig-list{display:grid;grid-template-columns:repeat(4,1fr);gap:3px}
.sig-row{display:grid;grid-template-columns:28px 68px 1fr 106px;align-items:center;padding:7px 10px;border-radius:5px;border:1px solid var(--border);cursor:pointer;transition:background .15s,border-color .15s,box-shadow .15s;background:var(--surface)}
.sig-row:hover{background:#eef3ff;border-color:rgba(26,86,219,.3);box-shadow:0 2px 8px rgba(26,86,219,.07)}
.sig-row:hover .s-sym{color:var(--accent)}
.s-emoji{font-size:14px;text-align:center}
.s-sym{font-weight:700;font-size:13px;transition:color .15s}
.s-type{font-size:11px;font-weight:600}
.s-badge{font-size:10px;font-weight:700;padding:3px 7px;border-radius:4px;text-align:center;letter-spacing:.4px;font-family:var(--font-ui)}
.b-BREAKOUT{background:#dcfce7;color:#15803d;border:1px solid #86efac}
.b-POCKET{background:#fef9c3;color:#854d0e;border:1px solid #fde047}
.b-PREBREAK{background:#f3e8ff;color:#7e22ce;border:1px solid #d8b4fe}
.b-BBREAKP{background:#dbeafe;color:#1d4ed8;border:1px solid #93c5fd}
.b-BFISH{background:#ffedd5;color:#c2410c;border:1px solid #fdba74}
.b-MACROSS{background:#f1f5f9;color:#475569;border:1px solid #cbd5e1}
.signal-header-toggle{cursor:pointer;user-select:none}
.momentum-box{display:none;border-top:1px solid var(--border);background:#fbfcff;padding:8px 16px}
.momentum-box.on{display:block}
.momentum-title{font-family:var(--font-ui);font-size:11px;font-weight:800;letter-spacing:1.8px;text-transform:uppercase;color:var(--accent);margin:0 0 6px}
.momentum-list{display:grid;grid-template-columns:repeat(4,1fr);gap:3px}
.momentum-row{display:grid;grid-template-columns:68px 1fr 72px;align-items:center;padding:6px 9px;border-radius:5px;border:1px solid var(--border);background:var(--surface);cursor:pointer;transition:background .15s,border-color .15s,box-shadow .15s}
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
.hmap-col{display:flex;flex-direction:column;gap:2px;width:162px;flex-shrink:0}
.hmap-group{display:flex;flex-direction:column;gap:2px}
.hmap-ghdr{display:flex;align-items:center;justify-content:center;padding:0 8px;height:24px;border-radius:4px;background:rgb(220,228,250);border:1px solid rgb(160,180,230);gap:16px}
.hmap-gname{font-family:var(--font-ui);font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:rgb(25,55,150);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hmap-gavg{font-family:var(--font-mono);font-size:9px;flex-shrink:0}
.hmap-gavg.pos{color:rgb(22,120,40)}.hmap-gavg.neg{color:rgb(185,25,25)}.hmap-gavg.zer{color:rgb(110,105,20)}
.hmap-cell{display:grid;grid-template-columns:56px 48px 1fr;align-items:center;height:24px;border-radius:4px;cursor:pointer;border:1px solid rgba(0,0,0,.1);transition:filter .12s,transform .1s,box-shadow .12s;overflow:hidden}
.hmap-cell:hover{filter:brightness(.96);transform:scale(1.035);z-index:2;box-shadow:0 2px 8px rgba(0,0,0,.18)}
.hmap-cell>span{display:flex;align-items:center;justify-content:center;height:100%;overflow:hidden;white-space:nowrap;font-family:var(--font-mono)}
.hc-sym{font-size:10px}.hc-price{font-size:8.5px;opacity:.82}.hc-pct{font-size:9.5px}
.hmap-sector-group{width:130px;margin:26px auto 0}
.hmap-sector-cell{display:grid;grid-template-columns:1fr auto;align-items:center;height:24px;border-radius:4px;border:1px solid rgba(0,0,0,.1);padding:0 8px;gap:2px;overflow:hidden;transition:filter .12s}
.hmap-sector-cell:hover{filter:brightness(.9)}
.hsc-name{font-family:var(--font-ui);font-size:9px;text-transform:uppercase;letter-spacing:.3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hsc-pct{font-family:var(--font-mono);font-size:9px;text-align:right;flex-shrink:0}
.sankey-wrap{width:calc(100% - 24px);aspect-ratio:16/9;height:auto;margin-left:24px;background:#fff}
.sankey-svg{width:100%;height:100%;display:block;background:#fff;border:none}
.sankey-empty{display:flex;align-items:center;justify-content:center;height:100%;color:var(--muted);font-size:13px}
.sankey-panel .panel-hdr{cursor:pointer;user-select:none}
.sankey-toggle{font-size:12px;color:var(--muted);transition:transform .15s}
.sankey-panel.collapsed .sankey-wrap{display:none}
.sankey-panel:not(.collapsed) .sankey-toggle{transform:rotate(90deg);color:var(--accent)}
.lite-chart-panel .panel-hdr{cursor:pointer;user-select:none}
.lite-chart-toggle-icon{font-size:12px;color:var(--muted);transition:transform .15s;flex-shrink:0}
.lite-chart-panel:not(.collapsed) .lite-chart-toggle-icon{transform:rotate(90deg);color:var(--accent)}
.lite-chart-panel.collapsed .lite-chart-search-wrap,
.lite-chart-panel.collapsed .lite-tf-tabs,
.lite-chart-panel.collapsed .lite-indicators,
.lite-chart-panel.collapsed .lite-draw-toolbar,
.lite-chart-panel.collapsed .lite-chart-frame{display:none}
.hmap-panel-hdr{cursor:pointer;user-select:none}
.hmap-toggle-icon{font-size:12px;color:var(--muted);transition:transform .15s;flex-shrink:0}
.hmap-panel:not(.collapsed) .hmap-toggle-icon{transform:rotate(90deg);color:var(--accent)}
.hmap-panel.collapsed .hmap-hdr-row1>*:not(.panel-title){display:none}
.hmap-panel.collapsed .hmap-hdr-row1 #hover-preview-btn{display:none}
.hmap-panel.collapsed .hmap-ts-wrap{display:none}
.hmap-panel.collapsed .hmap-toggle-icon{margin-left:auto}
.hmap-panel.collapsed>.pbar-wrap,
.hmap-panel.collapsed>.panel-body{display:none}
.market-frame{width:100%;height:720px;border:none;display:block;background:#fff}
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
.lite-chart-title{position:absolute;top:8px;left:10px;z-index:3;font-family:var(--font-mono);font-size:11px;color:#111827;white-space:nowrap;background:rgba(255,255,255,.78);padding:2px 5px;border-radius:4px;pointer-events:none}
.lite-chart-signal{position:absolute;top:29px;left:10px;z-index:3;display:none;align-items:center;gap:5px;background:rgba(255,255,255,.78);padding:2px 5px;border-radius:4px;pointer-events:none}
.lite-chart-signal.on{display:flex}
.lite-chart-search{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);z-index:5;width:42px;min-width:42px;max-width:120px;height:34px;border:1px solid var(--accent);border-radius:8px;background:#fff;color:var(--text);font-family:var(--font-mono);font-size:16px;font-weight:800;text-align:center;text-transform:uppercase;box-shadow:0 8px 28px rgba(17,24,39,.15);outline:none;display:none;transition:width .12s}
.lite-chart-search.on{display:block}
.lite-chart-empty{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;background:#fff;color:var(--muted);font-size:12px;pointer-events:none}
.lite-chart-status{position:absolute;top:8px;right:10px;z-index:6;min-width:22px;height:16px;display:flex;align-items:center;justify-content:center;padding:0 5px;font-family:var(--font-mono);font-size:11px;letter-spacing:1.5px;color:#0369a1;background:rgba(224,242,254,.92);border:1px solid #7dd3fc;border-radius:8px;pointer-events:none;opacity:0;transition:opacity .25s}
.lite-chart-status.on{opacity:1}
.lite-chart-status.fetching{color:#b45309;background:rgba(255,247,237,.94);border-color:#fdba74}
.lite-xhair-v{position:absolute;top:0;bottom:0;left:0;width:0;border-left:1px dashed rgba(55,65,81,.55);pointer-events:none;z-index:4;display:none}
.lite-xhair-h{position:absolute;left:0;right:0;top:0;height:0;border-top:1px dashed rgba(55,65,81,.55);pointer-events:none;z-index:4;display:none}
.lite-xhair-price{position:absolute;right:1px;top:0;transform:translateY(-50%);min-width:54px;padding:2px 6px;font-family:var(--font-mono);font-size:11px;font-weight:600;color:#fff;background:#1f2937;border-radius:3px;pointer-events:none;z-index:5;display:none;text-align:center;white-space:nowrap}
.lite-xhair-time{position:absolute;left:0;bottom:2px;transform:translateX(-50%);padding:2px 6px;font-family:var(--font-mono);font-size:11px;font-weight:600;color:#fff;background:#1f2937;border-radius:3px;pointer-events:none;z-index:5;display:none;white-space:nowrap}
.lite-draw-toolbar{display:flex;align-items:center;gap:3px;flex-wrap:wrap;padding-left:6px;border-left:1px solid var(--border)}
.lite-draw-btn{width:24px;height:24px;border:1px solid transparent;border-radius:6px;background:transparent;color:#374151;font-size:12px;line-height:1;cursor:pointer;display:flex;align-items:center;justify-content:center}
.lite-draw-btn:hover{background:#f1f5f9}
.lite-draw-btn.on{background:#eef3ff;border-color:var(--accent);color:var(--accent)}
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
.pbox{background:var(--surface);border:1px solid var(--border);border-radius:10px;box-shadow:0 20px 60px rgba(0,0,0,.15);width:99vw;max-width:1800px;height:94vh;display:flex;flex-direction:column;overflow:hidden;animation:popIn .2s ease}
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
   ALBUM — dùng chung
   ═══════════════════════════════════════════ */
#panel-scanner{overflow:hidden;background:#fff;display:none;flex-direction:column}
#panel-scanner.on{display:flex}
.scanner-loading{display:flex;align-items:center;justify-content:center;flex:1;color:var(--muted);font-size:14px}
.album-outer{flex:1;display:flex;flex-direction:column;overflow:hidden}
.album-center{flex:1;overflow-y:auto;display:flex;flex-direction:column;align-items:center;padding:4px;gap:4px;background:#fff;scrollbar-width:thin}
.album-slide{display:none;flex-direction:column;align-items:center;width:100%}
.album-slide.on{display:flex}
.album-slide img{max-width:100%;max-height:calc(94vh - 120px);object-fit:contain;border-radius:3px;border:1px solid var(--border)}
.album-nav-bar{display:flex;align-items:center;justify-content:center;gap:10px;padding:6px 0 8px;flex-shrink:0}
.album-nav-btn{width:30px;height:30px;border-radius:50%;border:1px solid #dde3ee;background:#f4f6fb;color:var(--muted);font-size:14px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all .15s;flex-shrink:0;user-select:none}
.album-nav-btn:hover:not(.disabled){background:var(--accent);color:#fff;border-color:var(--accent)}
.album-nav-btn.disabled{opacity:.25;pointer-events:none}
.album-dots-wrap{display:flex;gap:6px;align-items:center}
.album-dot{width:8px;height:8px;border-radius:50%;background:#dde3ee;cursor:pointer;transition:all .15s}
.album-dot.on{background:var(--accent);transform:scale(1.3)}
.album-refresh-btn{width:30px;height:30px;border-radius:50%;border:1px solid #dde3ee;background:#f4f6fb;color:var(--muted);font-size:15px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all .15s;flex-shrink:0}
.album-refresh-btn:hover{background:#0e9f6e;color:#fff;border-color:#0e9f6e}
.album-refresh-btn.spinning span{display:inline-block;animation:spin .7s linear infinite}
.album-hint{text-align:center;font-size:10px;color:#9ca3af;padding:0 0 4px;flex-shrink:0}
@keyframes spin{to{transform:rotate(360deg)}}

/* ═══════════════════════════════════════════
   HOVER PREVIEW
   ═══════════════════════════════════════════ */
#hover-preview-panel{display:none;position:fixed;bottom:0;left:0;right:0;height:60vh;min-height:120px;max-height:90vh;z-index:500;background:var(--surface);border-top:2px solid var(--accent);box-shadow:0 -4px 24px rgba(0,0,0,.13);flex-direction:column}
#hover-preview-resizer{position:absolute;top:0;left:0;right:0;height:6px;cursor:ns-resize;z-index:10}
#hover-preview-resizer:hover{background:rgba(26,86,219,.18)}
.hv-header-row1{display:flex;align-items:center;gap:4px;padding:4px 6px 4px 10px;background:var(--surf2);border-bottom:1px solid var(--border);flex-shrink:0}
.hv-grouptabs{display:flex;align-items:center;overflow-x:auto;gap:3px;flex:1;min-width:0;scrollbar-width:none;padding:1px 0}
.hv-grouptabs::-webkit-scrollbar{display:none}
.hv-gtab{height:24px;display:inline-flex;align-items:center;justify-content:center;flex-shrink:0;padding:0 10px;border-radius:4px;border:1px solid var(--border);background:var(--bg);color:var(--muted);font-family:var(--font-mono);font-size:10px;font-weight:600;cursor:pointer;white-space:nowrap;transition:all .15s}
.hv-gtab.on{background:var(--accent);color:#fff;border-color:var(--accent)}
.hv-gtab:hover:not(.on){background:#eef3ff;color:var(--accent);border-color:var(--accent)}
.hv-body{display:flex;flex:1;overflow:hidden}
.hv-symlist{width:120px;flex-shrink:0;overflow-y:auto;border-right:1px solid var(--border);background:var(--bg);scrollbar-width:thin;scrollbar-color:var(--border) transparent}
.hv-symlist::-webkit-scrollbar{width:3px}
.hv-symlist::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
.hv-sym-item{display:grid;grid-template-columns:35px 30px 1fr;align-items:center;padding:5px 6px;cursor:pointer;border-bottom:1px solid rgba(0,0,0,.04);transition:background .1s;gap:2px}
.hv-sym-item:hover,.hv-sym-item.on{background:#e8effd}
.hv-sym-item.on .hv-sym-name{color:#0f3fb3;font-weight:800}
.hv-sym-name{font-family:var(--font-mono);font-size:11px;font-weight:700}
.hv-sym-pct{font-family:var(--font-mono);font-size:10px;text-align:right;font-weight:700}
.hv-sym-price{font-family:var(--font-mono);font-size:10px;text-align:right;color:#374151}
#hover-preview-iframe-wrap{flex:1;overflow:hidden;position:relative}
#hover-preview-iframe-wrap iframe{width:100%;height:100%;border:none;display:block}
.hv-ctrl{height:24px;padding:0 10px;border-radius:4px;border:1px solid var(--border);background:var(--surface);color:var(--muted);font-size:10px;font-family:var(--font-mono);font-weight:600;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;transition:all .15s;white-space:nowrap;flex-shrink:0}
.hv-ctrl:hover{background:var(--accent);color:#fff;border-color:var(--accent)}
.hv-ctrl.danger:hover{background:var(--red);color:#fff;border-color:var(--red)}
#hover-preview-btn{display:inline-flex;align-items:center;gap:5px;padding:4px 11px;border-radius:5px;border:1px solid var(--border);background:var(--surface);color:var(--muted);font-family:var(--font-mono);font-size:10px;font-weight:600;cursor:pointer;white-space:nowrap;transition:all .15s}
#hover-preview-btn:hover:not(.on){background:#eef3ff;color:var(--accent);border-color:var(--accent)}
#hover-preview-btn.on{background:var(--accent);color:#fff;border-color:var(--accent)}

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
  .hmap-panel-hdr{flex-direction:column;align-items:flex-start;gap:4px;padding:7px 10px}
  .hmap-hdr-row1{width:100%;overflow-x:auto;scrollbar-width:none;gap:6px}
  .hmap-hdr-row1::-webkit-scrollbar{display:none}
  .hmap-hdr-row1>*{flex-shrink:0}
  .hmap-search-input{width:90px !important}
  .hmap-search-input:focus{width:90px !important}
  .hmap-ts-wrap{
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
  #hover-preview-btn,#hover-preview-panel{display:none !important}
  .album-slide img{cursor:zoom-in}
  .panel-meta{font-size:9px;overflow:hidden;text-overflow:ellipsis;max-width:55%}
  #hmap-popout-btn:hover, #hmap-popout-btn:focus {
    background: var(--surface) !important;
    color: var(--muted) !important;
    border-color: var(--border) !important;
  }
  #hmap-popout-btn:active {
    background: var(--surf2) !important;
    color: var(--text) !important;
    border-color: var(--border) !important;
  }
  .sankey-wrap{
    width:100% !important;
    margin-left:0 !important;
    aspect-ratio:16/9;
    height:auto;
  }
  .market-frame{height:70vh}
  #market-panel{display:none !important;}

  #lite-chart-panel{display:none !important}
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
  width:72px;padding:5px 6px 5px 24px;
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

  .hv-header-row1{padding:6px 8px 6px 12px}
  .hv-gtab{height:28px;font-size:11px;padding:0 12px}
  .hv-sym-name{font-size:12px}
  .hv-sym-pct{font-size:11px}
  .hv-sym-price{font-size:11px}
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

  /* Album img cao hơn khi landscape */
  .album-slide img{max-height:calc(100dvh - 50px)}

  /* FIX #5 Landscape: hover preview tabs dễ nhấn hơn — tăng chiều cao và vùng chạm */
  .hv-gtab{
    height:40px !important;
    min-height:40px !important;
    padding:0 12px !important;
    font-size:11px !important;
  }
  .hv-header-row1{
    padding:2px 6px 2px 10px !important;
    min-height:40px !important;
  }
}

/* ═══════════════════════════════════════════
   MOBILE LIGHTBOX
   ═══════════════════════════════════════════ */
#mob-lightbox{display:none;position:fixed;inset:0;z-index:99999;background:#fff;overflow:hidden;touch-action:none}
#mob-lightbox.on{display:block}
#lb-viewport{position:absolute;inset:0;overflow:hidden}
#lb-strip{display:flex;height:100%}
#lb-strip.dragging{will-change:transform}
#lb-strip.snapping{transition:transform .32s cubic-bezier(.25,.46,.45,.94)}
.lb-slide{flex-shrink:0;width:100vw;height:100%;display:flex;align-items:center;justify-content:center;overflow:hidden}
.lb-slide img{max-width:100vw;max-height:100dvh;object-fit:contain;display:block;transform-origin:center;user-select:none;-webkit-user-drag:none;pointer-events:none}
.lb-slide img.zooming{will-change:transform;transition:none}
#mob-lightbox-close{position:absolute;top:14px;right:14px;width:44px;height:44px;border-radius:50%;background:rgba(0,0,0,.07);border:1px solid rgba(0,0,0,.15);color:#333;font-size:22px;display:flex;align-items:center;justify-content:center;cursor:pointer;z-index:10;touch-action:manipulation;-webkit-tap-highlight-color:transparent}
#mob-lightbox-close:active{background:rgba(0,0,0,.2)}
#mob-lightbox-counter{position:absolute;bottom:20px;left:50%;transform:translateX(-50%);display:flex;gap:8px;align-items:center;z-index:10;pointer-events:none}
.mob-lb-dot{width:7px;height:7px;border-radius:50%;background:rgba(0,0,0,.18);transition:all .2s}
.mob-lb-dot.on{background:var(--accent);transform:scale(1.4)}
#mob-lightbox-label{position:absolute;top:16px;left:50%;transform:translateX(-50%);color:rgba(30,30,30,.85);font-family:var(--font-mono);font-size:12px;white-space:nowrap;z-index:10;pointer-events:none;background:rgba(0,0,0,.08);padding:3px 12px;border-radius:20px}
#lb-zoom-hint{position:absolute;bottom:52px;left:50%;transform:translateX(-50%);background:rgba(0,0,0,.45);color:#fff;font-family:var(--font-mono);font-size:10px;padding:3px 10px;border-radius:20px;z-index:11;pointer-events:none;opacity:0;transition:opacity .3s;white-space:nowrap}
#lb-zoom-hint.show{opacity:1}
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
      <div class="momentum-title">Động lượng</div>
      <div class="momentum-list" id="momentum-list">
      </div>
    </div>
  </div>

  <!-- HEATMAP -->
  <div class="panel hmap-panel" id="hmap-panel">
    <div class="hmap-panel-hdr" id="hmap-toggle">
      <div class="hmap-hdr-row1">
        <span class="panel-title">Heatmap</span>
        <button class="hmap-link-btn" id="btn-market">MARKET</button>
        <button class="hmap-link-btn" id="btn-vnindex">VNINDEX</button>
        <div class="hmap-search-wrap">
          <span class="s-icon">🔍</span>
          <input class="hmap-search-input" id="hmap-search" type="text" placeholder="Tìm mã" maxlength="10" autocomplete="off" spellcheck="false">
        </div>
        <button class="hmap-link-btn" id="hmap-follow-btn">FOLLOW</button>
        <button id="hover-preview-btn">Chart: OFF</button>
        <button class="hmap-link-btn" id="hmap-popout-btn" style="color:var(--muted)">⧉</button>
        <button class="hmap-link-btn" id="hmap-simplize-btn">SZ</button>
      </div>
      <span class="panel-meta hmap-ts-wrap" id="hmap-ts">Đang tải...</span>
      <span class="hmap-toggle-icon">▶</span>
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

  <!-- LIGHTWEIGHT CHART -->
  <div class="panel lite-chart-panel collapsed" id="lite-chart-panel">
    <div class="panel-hdr" id="lite-chart-toggle">
      <div class="lite-chart-toolbar">
        <span class="panel-title">CHART</span>
        <div class="lite-chart-search-wrap">
          <span class="s-icon">🔍</span>
          <input class="lite-chart-input" id="lite-chart-input" placeholder="Tìm mã" maxlength="10" spellcheck="false" lang="en" autocapitalize="characters" autocorrect="off" autocomplete="off" inputmode="text" translate="no">
        </div>
        <div class="lite-tf-tabs" id="lite-chart-tf">
          <button class="lite-tf-btn on" data-tf="1D">D</button>
          <button class="lite-tf-btn" data-tf="1W">W</button>
        </div>
        <div class="lite-indicators" id="lite-indicators">
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
              <label><input type="checkbox" value="ma200"><span class="lite-ind-label" data-ind="ma200" title="Bấm để đổi màu">MA200</span><input type="color" class="lite-ind-color" data-ind="ma200" value="#8b4513"></label>
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
          <label class="lite-ind-simple"><input type="checkbox" value="signal">Signal</label>
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
          <button class="lite-draw-btn" id="lite-draw-copy" title="Copy hình chart">📋</button>
        </div>
      </div>
      <span class="lite-chart-toggle-icon">▶</span>
    </div>
    <div class="lite-chart-frame" id="lite-chart-frame" tabindex="0">
      <span class="lite-chart-title" id="lite-chart-title">Đang tải...</span>
      <span class="lite-chart-signal" id="lite-chart-signal"></span>
      <span class="lite-chart-status" id="lite-chart-status" title="Trạng thái tải chart">•••</span>
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
      <div class="lite-xhair-v" id="lite-xhair-v"></div>
      <div class="lite-xhair-h" id="lite-xhair-h"></div>
      <div class="lite-xhair-price" id="lite-xhair-price"></div>
      <div class="lite-xhair-time" id="lite-xhair-time"></div>
      <div class="lite-chart-empty" id="lite-chart-empty">Đang tải chart...</div>
    </div>
  </div>

  <!-- MARKET -->
  <div class="panel" id="market-panel">
    <div class="panel-hdr">
      <span class="panel-title">MARKET</span>
    </div>
    <iframe class="market-frame" id="market-frame" src="https://fireant.vn/dashboard" allowfullscreen></iframe>
  </div>

  <!-- SANKEY -->
  <div class="panel sankey-panel collapsed" id="sankey-panel">
    <div class="panel-hdr" id="sankey-toggle">
      <span class="panel-title">Sankey</span>
      <span class="sankey-toggle">▶</span>
    </div>
    <div class="sankey-wrap" id="sankey-wrap" hidden><svg class="sankey-svg" id="sankey-svg" viewBox="0 0 1600 900" preserveAspectRatio="xMidYMid meet"></svg></div>
  </div>
</div>

<!-- TRADE JOURNAL -->
<div class="journal-overlay" id="journal-overlay">
  <div class="journal-box">
    <iframe class="journal-frame" id="journal-frame" src="about:blank"></iframe>
  </div>
</div>

<!-- HOVER PREVIEW -->
<div id="hover-preview-panel">
  <div id="hover-preview-resizer"></div>
  <div class="hv-header-row1">
    <div class="hv-grouptabs" id="hv-grouptabs"></div>
    <div style="display:flex;gap:4px;align-items:center;margin-left:auto;padding-right:4px;flex-shrink:0">
      <button class="hv-ctrl" id="hv-sort-btn" style="display:none">A↕Z</button>
      <button class="hv-ctrl" id="hv-full-btn"> ⛶ </button>
      <button class="hv-ctrl" id="hv-pop-btn"> ⧉ </button>
      <button class="hv-ctrl danger" id="hv-close-btn"> ✕ </button>
    </div>
  </div>
  <div class="hv-body">
    <div class="hv-symlist" id="hv-symlist" style="display:none"></div>
    <div id="hover-preview-iframe-wrap">
      <iframe id="hover-preview-iframe" src="about:blank"></iframe>
    </div>
  </div>
</div>

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
          <button class="ctab" data-tab="scanner">🖼 Scanner Chart</button>
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
      <button class="mob-tab-btn" data-tab="scanner">🖼 Scanner</button>
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
        <button class="mob-land-tab" data-tab="scanner">🖼 Scanner</button>
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
      <div class="tpanel" id="panel-scanner">
        <div class="scanner-loading" id="scanner-loading"><span>⏳ Đang tạo chart từ scanner...</span></div>
        <div class="album-outer" id="album-outer" style="display:none">
          <div class="album-center"><div id="album-slides"></div></div>
          <div class="album-nav-bar">
            <button class="album-nav-btn disabled" id="btn-prev">&#9664;</button>
            <div class="album-dots-wrap" id="album-dots"></div>
            <button class="album-nav-btn" id="btn-next">&#9654;</button>
            <button class="album-refresh-btn" id="btn-refresh"><span>&#8635;</span></button>
          </div>
          <div class="album-hint">◀ ▶ hoặc phím ← → để chuyển ảnh</div>
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

<!-- LIGHTBOX -->
<div id="mob-lightbox">
  <div id="lb-viewport"><div id="lb-strip"></div></div>
  <div id="mob-lightbox-label">📊 Daily [D]</div>
  <button id="mob-lightbox-close">✕</button>
  <div id="mob-lightbox-counter"></div>
  <div id="lb-zoom-hint">Chụm 2 ngón để zoom</div>
</div>
<div id="edge-swipe-zone"></div>

<script src="https://unpkg.com/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js"></script>
<script>
'use strict';
// ═══════════════════════════════════════════════════════
// DOM CACHE
// ═══════════════════════════════════════════════════════
const $=id=>document.getElementById(id);
const DOM={
  clock:$('clock'),sigMeta:$('sig-meta'),sigList:$('sig-list'),
  signalHeader:$('signal-header'),momentumBox:$('momentum-box'),momentumList:$('momentum-list'),
  hmapTs:$('hmap-ts'),hmapGrid:$('hmap-grid'),hmapSearch:$('hmap-search'),
  hmapPanel:$('hmap-panel'),hmapToggle:$('hmap-toggle'),
  sankeyPanel:$('sankey-panel'),sankeyToggle:$('sankey-toggle'),sankeyWrap:$('sankey-wrap'),
  liteChartPanel:$('lite-chart-panel'),liteChartToggle:$('lite-chart-toggle'),
  sankeySvg:$('sankey-svg'),
  liteChart:$('lite-chart'),
  liteChartFrame:$('lite-chart-frame'),liteChartSearch:$('lite-chart-search'),
  liteRsiChart:$('lite-rsi-chart'),liteMacdChart:$('lite-macd-chart'),
  liteMacdResizer:$('lite-macd-resizer'),liteChartInput:$('lite-chart-input'),
  liteChartTf:$('lite-chart-tf'),liteIndicators:$('lite-indicators'),
  liteChartTitle:$('lite-chart-title'),liteChartEmpty:$('lite-chart-empty'),
  liteChartSignal:$('lite-chart-signal'),
  liteChartStatus:$('lite-chart-status'),
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
  liteShapeArrowStyle:$('lite-shape-arrow-style'),liteShapeZigzagFill:$('lite-shape-zigzag-fill'),
  liteShapeArrowWidth:$('lite-shape-arrow-width'),
  pbarSig:$('pbar-sig'),pbarHmap:$('pbar-hmap'),
  journalOverlay:$('journal-overlay'),journalFrame:$('journal-frame'),
  overlay:$('overlay'),pbox:$('pbox'),
  // Desktop popup header
  ptitle:$('ptitle'),popupSearch:$('popup-search'),popupCtabs:$('popup-ctabs'),
  // Mobile portrait rows
  mobHdrRow1:$('mob-hdr-row1'),mobPtitle:$('mob-ptitle'),mobSearch:$('mob-search'),
  mobTabRow:$('mob-tab-row'),
  // Mobile landscape row
  mobHdrLand:$('mob-hdr-landscape'),mobLandSym:$('mob-land-sym'),
  mobLandSearch:$('mob-land-search'),mobLandTabs:$('mob-land-tabs'),
  // iframes
  ifVs:$('iframe-vs'),
  // album
  loading:$('scanner-loading'),albumOuter:$('album-outer'),
  albumSlides:$('album-slides'),albumDots:$('album-dots'),
  btnPrev:$('btn-prev'),btnNext:$('btn-next'),btnRef:$('btn-refresh'),
  // hover
  hpPanel:$('hover-preview-panel'),hpIframe:$('hover-preview-iframe'),
  hpGrouptabs:$('hv-grouptabs'),hpSymlist:$('hv-symlist'),hpSortBtn:$('hv-sort-btn'),
  edgeZone:$('edge-swipe-zone'),mobClose:$('mob-close-float'),
  wrap:$('main-wrap'),footer:$('footer-txt'),
  lb:$('mob-lightbox'),lbStrip:$('lb-strip'),
  lbLabel:$('mob-lightbox-label'),lbCounter:$('mob-lightbox-counter'),
  lbZoomHint:$('lb-zoom-hint'),
};
// ═══════════════════════════════════════════════════════
// HELPERS
// ═══════════════════════════════════════════════════════
const IS_MOBILE=()=>window.innerWidth<=768;
const IS_LANDSCAPE=()=>window.innerWidth>window.innerHeight;
const TABS_ALL=['vs','scanner','vnd-cs','vnd-news','vnd-sum','24h','url'];
const IFRAME_LAZY={
  'vnd-cs': s=>`https://dstock.vndirect.com.vn/tong-quan/${s}/diem-nhan-co-ban-popup?theme=light`,
  'vnd-news':s=>`https://dstock.vndirect.com.vn/tong-quan/${s}/tin-tuc-ma-popup?type=dn&theme=light`,
  'vnd-sum': s=>`https://dstock.vndirect.com.vn/tong-quan/${s}?theme=light`,
  '24h':     s=>`https://fireant.vn/ma-chung-khoan/${s}`,
};
const BADGE_MAP={
  'BREAKOUT':'b-BREAKOUT','POCKET PIVOT':'b-POCKET','PRE-BREAK':'b-PREBREAK',
  'BOTTOMBREAKP':'b-BBREAKP','BOTTOMFISH':'b-BFISH','MA_CROSS':'b-MACROSS'
};
const SIGNAL_LABEL_MAP={
  'BREAKOUT':'BREAKVOL',
  'POCKET PIVOT':'POCKET'
};
const signalLabel=s=>SIGNAL_LABEL_MAP[s]||s;
// Cache tín hiệu "hôm nay" theo mã (được đổ đầy trong fetchSigs() — vòng lặp fetch đã chạy sẵn mỗi
// SIG_TTL giây cho panel "Tín hiệu hôm nay"). Chart CHART chỉ đọc lại map này, không tự fetch riêng.
let _sigTodayMap=new Map();
let SIG_TTL=30,HMAP_TTL=120;
let _sym='',_tab='vs';
const FOLLOW_KEY='dashboard_follow_symbols';
const FOLLOW_ON_KEY='dashboard_follow_on';
let FOLLOW=loadFollowSymbols();
let FOLLOW_ON=localStorage.getItem(FOLLOW_ON_KEY)!=='0';
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
  renderHeatmap(window._lastHmapData||{});
  return true;
}
let _albumIdx=0,_albumTotal=0,_albumImages=[];
let _hoverPreviewOn=false,_hoverPreviewCurrent='';
let _hvActiveGroup=-1,_hvSortAlpha=false;
let _isPopoutMode=false,_popoutWin=null;
let _isChartPanelOpen=false;
let _isSimplizeMode=false,_simplizeWin=null,_simplizeWatch=null;
let _iframeDelay=null,_keyThrottle=false;
const SIMPLIZE_ORIGIN='https://simplize.vn';
function simplizeUrl(sym){return `${SIMPLIZE_ORIGIN}/chart?ticker=${encodeURIComponent((sym||'VNINDEX').toUpperCase())}`;}
const LITE_IND_KEY='dashboard_lite_indicators';
const LITE_IND_COLOR_KEY='dashboard_lite_ind_colors';
const LITE_TREND_MODE_KEY='dashboard_lite_trend_mode';
function loadLiteTrendMode(){
  let mode='regular';
  try{mode=localStorage.getItem(LITE_TREND_MODE_KEY)||'regular';}catch(e){}
  DOM.liteIndicators?.querySelectorAll('input[name="trend-mode"]').forEach(r=>{r.checked=(r.value===mode);});
}
function saveLiteTrendMode(){
  const mode=_liteTrendMode();
  try{localStorage.setItem(LITE_TREND_MODE_KEY,mode);}catch(e){}
}
function _liteTrendMode(){
  const el=DOM.liteIndicators?.querySelector('input[name="trend-mode"]:checked');
  return el?el.value:'regular';
}
// Helper get/set localStorage dùng chung cho toàn bộ chart CHART — gộp lại các khối try/catch
// lặp lại y hệt nhau ở rất nhiều nơi (đọc/ghi màu vẽ, cỡ chữ, font, nền chữ...).
function _liteLSGet(key,fallback){
  try{return localStorage.getItem(key)||fallback;}catch(e){return fallback;}
}
function _liteLSSet(key,val){
  try{localStorage.setItem(key,val);}catch(e){}
}
const LITE_MA_PERIODS=[10,20,30,50,100,200];
const LITE_EMA_PERIODS=[10,20,30,50,100,200];
const LITE_MA_DEFAULT_COLORS=['#ff0000','#008000','#1a56db','#800080','#d97706','#8b4513'];
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
      const inp=DOM.liteIndicators.querySelector(`.lite-ind-color[data-ind="${span.dataset.ind}"]`);
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
    const n=grp.querySelectorAll('.lite-ind-dropdown input[type="checkbox"]:checked').length;
    const badge=grp.querySelector(`.lite-ind-count[data-count="${key}"]`);
    if(badge){badge.textContent=n||'';badge.classList.toggle('on',n>0);}
  });
}
function closeAllLiteIndDropdowns(except){
  DOM.liteIndicators?.querySelectorAll('.lite-ind-group.open').forEach(g=>{
    if(g!==except)g.classList.remove('open');
  });
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
    });
  });
  DOM.liteIndicators?.querySelectorAll('.lite-ind-dropdown').forEach(dd=>{
    dd.addEventListener('click',e=>e.stopPropagation());
  });
  document.addEventListener('click',()=>closeAllLiteIndDropdowns());
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
let _liteChart=null,_liteRsiChart=null,_liteMacdChart=null,_liteCandle=null,_liteVolume=null,_liteRsiCrosshairSeries=null,_liteMacdCrosshairSeries=null,_liteSymbol='FPT';
let _liteMainWhite=null,_liteRsiWhite=null,_liteMacdWhite=null,_liteBBFillData=null,_liteTrendFillData=null;
let _liteTf='1D',_liteResizeBound=false,_liteSyncing=false,_litePointerInside=false,_liteInputTimer=null;
let _liteMacdSoloHeight=176;
let _liteData=[],_liteVolumeData=[],_liteIndicatorSeries=[],_liteDataByTime=new Map();
const LITE_BARS_VISIBLE=320,LITE_RIGHT_OFFSET=50,LITE_HIST_SCALE=2.1;
// Phần chung của mọi cấu hình rightPriceScale trong initLiteChart()/applyLitePaneLayout() —
// borderColor và minimumWidth giống hệt nhau ở cả 3 chart (main/RSI/MACD) và ở mọi lần áp dụng,
// chỉ scaleMargins (và autoScale ở main/RSI khi đổi layout) là khác nhau nên vẫn để riêng.
const LITE_PRICE_SCALE_BASE={borderColor:'#dde3ee',minimumWidth:64};
function initLiteChart(){
  if(_liteChart||!DOM.liteChart||!window.LightweightCharts)return;
  // Crosshair gốc của thư viện bị TẮT HẲN trên cả 2 chart (vertLine + horzLine + label đều visible:false).
  // Lý do: cách cũ dùng applyOptions() để bật/tắt horzLine mỗi khi đổi panel — applyOptions là thao tác
  // nặng (buộc chart vẽ lại toàn bộ), gọi liên tục theo mousemove nên gây giật/nháy và có lúc lộ ra
  // đồng thời 2 đường ngang (do độ trễ giữa 2 lệnh applyOptions ở 2 chart). Từ nay chỉ vẽ 1 crosshair
  // DUY NHẤT bằng overlay DOM riêng (xem _liteMoveXhair/_liteHideXhair) — mượt tuyệt đối vì chỉ set
  // style.left/top, không đụng tới engine vẽ của lightweight-charts.
  const chartOpts={
    layout:{background:{type:'solid',color:'#fff'},textColor:'#111827'},
    grid:{vertLines:{color:'#eef2f7'},horzLines:{color:'#eef2f7'}},
    timeScale:{borderColor:'#dde3ee',rightOffset:LITE_RIGHT_OFFSET},
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
  _liteVolume.priceScale().applyOptions({scaleMargins:{top:.78,bottom:0}});
  // Series "whitespace" vô hình: chỉ chứa các mốc thời gian tương lai (vùng trống bên phải nến cuối),
  // giúp time-scale nhận biết vùng này nên subscribeCrosshairMove vẫn trả về param.time hợp lệ khi
  // trỏ vào vùng trống đó (để overlay crosshair + nhãn ngày vẫn hoạt động, không bị "rớt").
  _liteMainWhite=_liteChart.addLineSeries({lineVisible:false,lastValueVisible:false,priceLineVisible:false,crosshairMarkerVisible:false});
  _liteRsiWhite=_liteRsiChart.addLineSeries({lineVisible:false,lastValueVisible:false,priceLineVisible:false,crosshairMarkerVisible:false});
  _liteMacdWhite=_liteMacdChart.addLineSeries({lineVisible:false,lastValueVisible:false,priceLineVisible:false,crosshairMarkerVisible:false});
  _liteChart.timeScale().subscribeVisibleLogicalRangeChange(range=>{
    redrawLiteDrawings();
    _liteSyncVisibleRangeFrom(_liteChart,range);
  });
  _liteRsiChart.timeScale().subscribeVisibleLogicalRangeChange(range=>{
    _liteSyncVisibleRangeFrom(_liteRsiChart,range);
  });
  _liteMacdChart.timeScale().subscribeVisibleLogicalRangeChange(range=>{
    _liteSyncVisibleRangeFrom(_liteMacdChart,range);
  });
  // ─── Crosshair hợp nhất (1 đường dọc + 1 đường ngang) cho cả 2 panel ───────────────────────────
  // Nguyên lý: KHÔNG dùng crosshair gốc của lightweight-charts nữa (đã tắt hẳn ở chartOpts phía trên).
  // Mỗi panel tự báo toạ độ con trỏ (x,y cục bộ) qua subscribeCrosshairMove, ta cộng thêm offsetTop
  // của panel đó so với khung frame để ra toạ độ TUYỆT ĐỐI trong #lite-chart-frame, rồi set thẳng
  // style.left/top cho 4 phần tử overlay (vạch dọc xuyên suốt cả khung, vạch ngang, nhãn giá, nhãn ngày).
  // Vì #lite-chart và #lite-macd-chart có cùng chiều rộng & cùng gốc trái (0), toạ độ x của 2 panel
  // khớp tuyệt đối nhau nên vạch dọc luôn thẳng hàng liền mạch qua cả 2 panel — không lệch, không giật,
  // vì mỗi lần di chuột chỉ là một phép gán style (rẻ), hoàn toàn không gọi applyOptions/setCrosshairPosition.
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
  // _liteHandleCrosshairMove: dùng chung cho cả 3 panel (main/MACD/RSI) — gộp lại từ 3 khối
  // subscribeCrosshairMove gần như giống hệt nhau trước đây để đỡ lặp code khi cần sửa sau này.
  // isMain=true GIỮ NGUYÊN thứ tự xử lý gốc của chart chính (luôn cập nhật title kể cả khi con trỏ
  // rời khỏi chart, để hiện title của nến cuối); isMain=false giữ nguyên hành vi gốc của MACD/RSI
  // (chỉ cập nhật title khi có điểm trỏ hợp lệ, không có "else" fallback).
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
    window.addEventListener('resize',()=>{
      if(_liteChart&&DOM.liteChart)_liteChart.applyOptions({width:DOM.liteChart.clientWidth,height:DOM.liteChart.clientHeight});
      if(_liteRsiChart&&DOM.liteRsiChart)_liteRsiChart.applyOptions({width:DOM.liteRsiChart.clientWidth,height:DOM.liteRsiChart.clientHeight});
      if(_liteMacdChart&&DOM.liteMacdChart)_liteMacdChart.applyOptions({width:DOM.liteMacdChart.clientWidth,height:DOM.liteMacdChart.clientHeight});
      resizeLiteDrawCanvas();redrawLiteDrawings();
    });
    // vẽ lại canvas liên tục (nhẹ) để bắt các thay đổi price-scale khi kéo trục Y (zoom trục);
    // chỉ thực sự vẽ khi panel Chart đang hiển thị (offsetParent!==null) để không tốn CPU vô ích
    // lúc người dùng đang ở tab khác của dashboard.
    const _liteDrawLoop=()=>{
      if(_liteDrawCtx&&DOM.liteChartFrame&&DOM.liteChartFrame.offsetParent!==null&&(_liteDrawings.length||_liteDrawActive||_liteBBFillData||_liteTrendFillData))redrawLiteDrawings();
      requestAnimationFrame(_liteDrawLoop);
    };
    requestAnimationFrame(_liteDrawLoop);
  }
}
function _liteChecked(name){
  return !!DOM.liteIndicators?.querySelector(`input[value="${name}"]:checked`);
}
function loadLiteIndicatorPrefs(){
  let prefs={};
  try{prefs=JSON.parse(localStorage.getItem(LITE_IND_KEY)||'{}')||{};}catch(e){prefs={};}
  DOM.liteIndicators?.querySelectorAll('input[type="checkbox"]').forEach(cb=>{
    // maema_on là checkbox mới thêm — mặc định BẬT để không làm ẩn mất các đường MA/EMA
    // người dùng đã bật từ trước (localStorage cũ chưa có key này). Các checkbox khác giữ quy ước cũ.
    cb.checked=cb.value==='maema_on'?(prefs[cb.value]!==false):(prefs[cb.value]===true);
  });
  loadLiteIndColors();
}
function saveLiteIndicatorPrefs(){
  const prefs={};
  DOM.liteIndicators?.querySelectorAll('input[type="checkbox"]').forEach(cb=>{prefs[cb.value]=cb.checked;});
  localStorage.setItem(LITE_IND_KEY,JSON.stringify(prefs));
}
function fmtLiteNum(v){
  return Number.isFinite(v)?Number(v).toFixed(2):'--';
}
function fmtLiteDate(t){
  const p=String(t||'').split('-');
  return p.length===3?`${p[2]}/${p[1]}/${p[0]}`:String(t||'--');
}
function _liteTitleSegments(bar){
  if(!bar)return [];
  const tf=_liteTf==='1W'?'W':'D';
  const pct=Number.isFinite(bar.pct)?bar.pct:0;
  const sign=pct>0?'+':'';
  const up=Number.isFinite(bar.close)&&Number.isFinite(bar.open)?bar.close>=bar.open:pct>=0;
  const col=up?LITE_CANDLE_UP_COLOR:LITE_CANDLE_DOWN_COLOR;
  return [
    {text:`${_liteSymbol} [${tf}] ${fmtLiteDate(bar.time)} | O:`,color:'#111827'},
    {text:fmtLiteNum(bar.open),color:col},
    {text:' H:',color:'#111827'},
    {text:fmtLiteNum(bar.high),color:col},
    {text:' L:',color:'#111827'},
    {text:fmtLiteNum(bar.low),color:col},
    {text:' C:',color:'#111827'},
    {text:fmtLiteNum(bar.close),color:col},
    {text:' (',color:'#111827'},
    {text:`${sign}${pct.toFixed(2)}%`,color:col},
    {text:')',color:'#111827'}
  ];
}
function _liteCleanSym(v){
  // Chuẩn hoá ký tự gõ từ IME tiếng Việt (Telex/VNI...) về chữ Latin gốc thay vì để bị mất chữ:
  // ví dụ 'â'→'a', 'ư'→'u', 'đ'→'d', rồi mới loại bỏ ký tự không phải A-Z0-9.
  return String(v||'')
    .normalize('NFD').replace(/[\u0300-\u036f]/g,'')
    .replace(/[đĐ]/g,'d')
    .toUpperCase().replace(/[^A-Z0-9]/g,'');
}
// Gắn sự kiện làm sạch ký tự cho ô nhập mã, TRÁNH ép sửa value trong lúc IME (Unikey Telex/VNI...)
// đang composing — ép sửa value giữa chừng composition sẽ xung đột với bộ đệm nội bộ của IME,
// khiến IME chèn lại phần đang gõ dở đè lên giá trị đã bị sửa → gây lặp chữ liên tục kiểu "VNVNVND".
// Chỉ làm sạch khi: (a) input bình thường không composing, hoặc (b) composition vừa kết thúc.
function _liteBindSymInput(el,onClean){
  if(!el)return;
  let composing=false;
  el.addEventListener('compositionstart',()=>{composing=true;});
  el.addEventListener('compositionend',()=>{composing=false;});
  el.addEventListener('input',e=>{
    if(composing||e.isComposing)return;
    const raw=_liteCleanSym(e.target.value);
    e.target.value=raw;
    onClean(raw);
  });
}
function _liteFutureTimes(lastTimeStr,n,tf){
  const out=[];
  let d=new Date(lastTimeStr+'T00:00:00Z'),added=0,guard=0;
  while(added<n&&guard<n*4){
    guard++;
    d=new Date(d.getTime()+(tf==='1W'?7:1)*86400000);
    if(tf!=='1W'){const wd=d.getUTCDay();if(wd===0||wd===6)continue;}
    out.push(d.toISOString().slice(0,10));added++;
  }
  return out;
}
function _liteUpdateWhitespace(){
  if(!_liteData.length)return;
  const lastT=liteTimeKey(_liteData[_liteData.length-1].time);
  const future=_liteFutureTimes(lastT,LITE_RIGHT_OFFSET+10,_liteTf).map(t=>({time:t}));
  if(_liteMainWhite)_liteMainWhite.setData(future);
  if(_liteRsiWhite)_liteRsiWhite.setData(future);
  if(_liteMacdWhite)_liteMacdWhite.setData(future);
}
function liteTimeKey(t){
  if(typeof t==='string')return t;
  if(t&&typeof t==='object'&&'year'in t&&'month'in t&&'day'in t){
    return `${t.year}-${String(t.month).padStart(2,'0')}-${String(t.day).padStart(2,'0')}`;
  }
  return String(t||'');
}
function updateLiteTitle(bar){
  if(!DOM.liteChartTitle||!bar)return;
  DOM.liteChartTitle.innerHTML=_liteTitleSegments(bar).map(seg=>
    seg.color==='#111827'?seg.text:`<span style="color:${seg.color}">${seg.text}</span>`
  ).join('');
}
// Gắn mũi tên điểm mua lên nến cuối cùng (= nến giao dịch gần nhất, kể cả khi hôm nay không phải
// ngày giao dịch) cho mã đang xem, dựa HOÀN TOÀN vào _sigTodayMap đã cache sẵn từ fetchSigs() —
// không gọi thêm API nào, không tính toán chỉ báo riêng, nên gần như không tốn thêm chi phí.
function _liteApplyBuySignal(){
  if(!_liteCandle||!_liteData.length)return;
  const sig=_liteChecked('signal')?_sigTodayMap.get(_liteSymbol):null;
  if(sig){
    let arrowColor='#9333ea';
    if(DOM.liteChartSignal){
      DOM.liteChartSignal.innerHTML=`<span class="s-emoji">${sig.emoji||'📌'}</span><span class="s-badge ${BADGE_MAP[sig.signal]||'b-MACROSS'}">${signalLabel(sig.signal)}</span>`;
      DOM.liteChartSignal.classList.add('on');
      // Lấy đúng màu viền của badge tín hiệu (đã áp class .b-*) để tô cho mũi tên — không khai báo
      // lại bảng màu riêng, mũi tên luôn đồng bộ màu với badge kể cả khi CSS đổi màu sau này.
      const badgeEl=DOM.liteChartSignal.querySelector('.s-badge');
      if(badgeEl)arrowColor=getComputedStyle(badgeEl).borderColor||arrowColor;
    }
    // Mũi tên báo mua: thu nhỏ còn một nửa (size:1 thay vì 2), không set text để không hiện badge
    // tên tín hiệu ngay dưới mũi tên trên chart (tên tín hiệu đã có ở badge riêng phía trên #lite-chart-signal).
    _liteCandle.setMarkers([{
      time:_liteData[_liteData.length-1].time,position:'belowBar',color:arrowColor,shape:'arrowUp',size:1
    }]);
  }else{
    _liteCandle.setMarkers([]);
    if(DOM.liteChartSignal){DOM.liteChartSignal.classList.remove('on');DOM.liteChartSignal.innerHTML='';}
  }
}
function setLiteRightOffset(){
  if(!_liteData.length||!_liteChart)return;
  const last=_liteData.length-1,to=last+LITE_RIGHT_OFFSET,from=Math.max(0,to-LITE_BARS_VISIBLE);
  _liteApplyVisibleLogicalRange({from,to});
}
function setLiteTf(tf){
  _liteTf=tf==='1W'?'1W':'1D';
  DOM.liteChartTf?.querySelectorAll('.lite-tf-btn').forEach(btn=>btn.classList.toggle('on',btn.dataset.tf===_liteTf));
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
  const mid=_sma(data,n),midByTime=new Map(mid.map(x=>[liteTimeKey(x.time),x.value]));
  const upper=[],lower=[];
  for(let i=n-1;i<data.length;i++){
    const slice=data.slice(i-n+1,i+1);
    const m=midByTime.get(liteTimeKey(data[i].time));
    if(m===undefined)continue;
    let sq=0;for(const b of slice)sq+=(b.close-m)*(b.close-m);
    const sd=Math.sqrt(sq/n);
    upper.push({time:data[i].time,value:m+mult*sd});
    lower.push({time:data[i].time,value:m-mult*sd});
  }
  return{upper,mid,lower};
}
function _liteVolumeColorForBar(bar){
  return bar&&Number(bar.close)>=Number(bar.open)?LITE_CANDLE_UP_COLOR:LITE_CANDLE_DOWN_COLOR;
}
function _liteNormalizeVolumeData(volume,data){
  return (volume||[]).map((v,idx)=>{
    const bar=data[idx];
    return {...v,color:_liteVolumeColorForBar(bar)};
  });
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
// ═══ TREND (Trailing Stop/Reverse kiểu NRTR) ═══
// mult = hệ số nhân biên độ đảo chiều, period = chu kỳ WMA biên độ H-L.
// mode: 'regular' dùng H-L/Close thường; 'smoothed' dùng nến Heikin Ashi (đúng công thức AFL gốc).
// Không vẽ kênh hồi quy — chỉ tô vùng trailing-stop đổi màu theo xu hướng.
const LITE_TREND_MULT=1.75, LITE_TREND_PERIOD=10;
function _wma(values,n){
  // values: mảng số thô (không phải {time,value}) đã align 1-1 theo index với dữ liệu nến.
  const out=new Array(values.length).fill(null);
  const denom=n*(n+1)/2;
  for(let i=n-1;i<values.length;i++){
    let sum=0;
    for(let k=0;k<n;k++)sum+=values[i-k]*(n-k);
    out[i]=sum/denom;
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
  const byTime=new Map(points.map(x=>[liteTimeKey(x.time),x]));
  return _liteData.map(bar=>byTime.get(liteTimeKey(bar.time))||{time:bar.time});
}
function applyLitePaneLayout(){
  const showRsi=_liteChecked('rsi');
  const showMacd=_liteChecked('macd');
  const totalH=720;
  const bothPanes=showRsi&&showMacd;
  const compactPaneH=132;
  const rsiH=showRsi?(bothPanes?compactPaneH:176):0;
  const macdH=showMacd?(bothPanes?compactPaneH:_liteMacdSoloHeight):0;
  const splitterH=showMacd?4:0;
  const lowerH=rsiH+macdH+splitterH;
  const mainH=showRsi||showMacd?Math.max(300,totalH-lowerH):totalH;
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
}
// ═══ DRAWING TOOLS (trend line, horizontal/vertical line, rectangle, channel, entry/target/stop, text) ═══
// Lấy cảm hứng thao tác kiểu TradingView: mọi hình vẽ xong đều chọn được, kéo-di-chuyển được,
// đổi màu được (trừ Entry/Target/Stoploss dùng màu ngữ nghĩa cố định), kênh giá vẽ 2 bước.
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
// Đo kích thước khối chữ (nhiều dòng) trên canvas để tính vùng bắt-trúng (hit-test) và vị trí neo cho thanh điều chỉnh.
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
function _liteDrawStoreKey(){return LITE_DRAW_KEY+':'+_liteSymbol+':'+_liteTf;}
function loadLiteDrawings(){
  try{_liteDrawings=JSON.parse(localStorage.getItem(_liteDrawStoreKey())||'[]')||[];}
  catch(e){_liteDrawings=[];}
  _liteSelectedId=null;_liteChannelPending=null;_liteArcPending=null;_liteZigzagPending=null;_liteLinePending=null;_liteDrawActive=null;
}
function saveLiteDrawings(){
  _liteLSSet(_liteDrawStoreKey(),JSON.stringify(_liteDrawings));
}
function resizeLiteDrawCanvas(){
  if(!DOM.liteDrawCanvas||!DOM.liteChart)return;
  const w=DOM.liteChart.clientWidth,h=DOM.liteChart.clientHeight,dpr=window.devicePixelRatio||1;
  DOM.liteDrawCanvas.style.width=w+'px';DOM.liteDrawCanvas.style.height=h+'px';
  DOM.liteDrawCanvas.width=Math.max(1,Math.round(w*dpr));DOM.liteDrawCanvas.height=Math.max(1,Math.round(h*dpr));
  _liteDrawCtx=DOM.liteDrawCanvas.getContext('2d');
  _liteDrawCtx.setTransform(dpr,0,0,dpr,0,0);
}
function _liteLogicalToX(l){
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
  return{l,p};
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
function _liteChannelOffset(d){
  const pts=d.points;
  return(pts[2]&&Number.isFinite(pts[2].offsetPrice))?pts[2].offsetPrice:(Math.abs(pts[1].p-pts[0].p)||pts[0].p*0.02||1);
}
// Đường cong bán nguyệt (arc): pts[2] lưu trực tiếp toạ độ (logical,price) nơi người dùng rê chuột tới —
// tức là điểm "đáy" (đỉnh cong) mà đường cong PHẢI đi qua.
//
// LƯU Ý QUAN TRỌNG: control-point của quadratic bezier PHẢI được tính trong không gian PIXEL (x,y trên canvas),
// KHÔNG được tính trong không gian logical/price rồi mới quy đổi sang pixel. Lý do: công thức bù trừ
// C = 2*target - trungĐiểm chỉ cho ra đường cong đi đúng qua "target" khi phép quy đổi (logical→x, price→y)
// là TUYẾN TÍNH. Trục giá của chart có thể ở chế độ log/percentage (không tuyến tính) — khi đó nếu tính C theo
// logical/price rồi quy đổi, điểm đáy hiển thị trên màn hình sẽ LỆCH khỏi đúng vị trí chuột (lệch càng nhiều khi
// biên độ giá càng lớn), gây hiện tượng "đáy nhảy chéo xa chuột". Tính thẳng trong pixel-space thì luôn đúng,
// bất kể trục giá tuyến tính hay không.
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
// Mũi tên "vệt" (extended/spike arrow): thân thon dần từ đuôi nhọn, rồi xòe rộng hẳn ra thành đầu mũi tên
// tam giác rõ nét ở phía ngọn — khác mũi tên thân thẳng + đầu tam giác nhỏ thông thường.
// widthScale: hệ số độ dày do người dùng chọn trên thanh điều chỉnh (mặc định 2 = "Vừa").
function _liteDrawWideArrow(ctx,x1,y1,x2,y2,color,widthScale){
  const dx=x2-x1,dy=y2-y1,len=Math.hypot(dx,dy);
  if(len<1e-3)return;
  const ws=Number.isFinite(widthScale)?widthScale/2:1; // chuẩn hoá quanh mức "Vừa" (=2) → hệ số 1
  const ux=dx/len,uy=dy/len,px=-uy,py=ux; // vector đơn vị: dọc thân & vuông góc thân
  const headLen=Math.max(10,Math.min(len*.42,32)); // chiều dài phần đầu mũi tên (tam giác xòe rộng)
  const shaftW=Math.max(2,Math.min(len*.09,7))*ws; // độ rộng thân (thon, hẹp hơn hẳn đầu mũi tên)
  const headW=Math.max(10,Math.min(len*.34,26))*ws; // độ rộng đáy đầu mũi tên (xòe rộng)
  const baseT=Math.max(0,len-headLen); // vị trí bắt đầu xòe đầu mũi tên, tính từ đuôi
  const bx=x1+ux*baseT,by=y1+uy*baseT;
  ctx.save();
  ctx.beginPath();
  ctx.moveTo(x1,y1); // đuôi — điểm nhọn
  ctx.lineTo(bx+px*shaftW/2,by+py*shaftW/2); // mép thân trái, thon dần tới đáy đầu mũi tên
  ctx.lineTo(bx+px*headW/2,by+py*headW/2); // xòe rộng ra đáy đầu mũi tên bên trái
  ctx.lineTo(x2,y2); // đỉnh mũi tên
  ctx.lineTo(bx-px*headW/2,by-py*headW/2); // đáy đầu mũi tên bên phải
  ctx.lineTo(bx-px*shaftW/2,by-py*shaftW/2); // mép thân phải
  ctx.closePath();
  ctx.fillStyle=color;
  ctx.fill();
  ctx.restore();
}
function _liteDrawShapeToCanvas(ctx,d){
  const pts=d.points,selected=(d.id===_liteSelectedId);
  if(d.type==='text'){
    const x=_liteLogicalToX(pts[0].l),y=_litePriceToY(pts[0].p);
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
    // Nhiều điểm (click nối tiếp), có thể mới có 1 điểm khi đang vẽ dở → xử lý riêng, không cần đủ 2 điểm.
    const color=d.color||_liteDrawColor;
    if(pts.length){
      // Quy đổi trước toàn bộ điểm sang toạ độ pixel (bỏ điểm không hợp lệ) để dùng chung cho cả tô nền lẫn vẽ nét.
      const scr=[];
      for(const pt of pts){
        const xx=_liteLogicalToX(pt.l),yy=_litePriceToY(pt.p);
        if(xx!==null&&yy!==null)scr.push({x:xx,y:yy});
      }
      // Tô dải màu phía trong: nối khép kín các điểm (đỉnh trên ↔ đáy ↔ đỉnh trên...) thành 1 vùng,
      // giống kiểu dải màu của Kênh giá / Bán nguyệt, để thấy rõ "vùng" mà ZigZag bao lấy.
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
        const lx=_liteLogicalToX(last.l),ly=_litePriceToY(last.p);
        const hx=_liteLogicalToX(d._hover.l),hy=_litePriceToY(d._hover.p);
        if(lx!==null&&ly!==null&&hx!==null&&hy!==null)_liteDrawLine(ctx,lx,ly,hx,hy,_liteHexAlpha(color,.5),[4,3]);
      }
      if(selected)for(const pt of pts){const xx=_liteLogicalToX(pt.l),yy=_litePriceToY(pt.p);if(xx!==null&&yy!==null)_liteDrawHandle(ctx,xx,yy);}
    }
    return;
  }
  if(pts.length<2)return;
  const x1=_liteLogicalToX(pts[0].l),y1=_litePriceToY(pts[0].p);
  const x2=_liteLogicalToX(pts[1].l),y2=_litePriceToY(pts[1].p);
  if(x1===null||y1===null||x2===null||y2===null)return;
  const color=d.color||_liteDrawColor;
  if(d.type==='trendline'){
    _liteDrawLine(ctx,x1,y1,x2,y2,color,d.dash?[5,4]:null);
    if(selected){_liteDrawHandle(ctx,x1,y1);_liteDrawHandle(ctx,x2,y2);}
  }else if(d.type==='hline'){
    ctx.save();ctx.strokeStyle=color;ctx.lineWidth=selected?1.8:1.2;if(d.dash)ctx.setLineDash([5,4]);
    ctx.beginPath();ctx.moveTo(0,y1);ctx.lineTo(DOM.liteChart.clientWidth,y1);ctx.stroke();ctx.restore();
    if(selected)_liteDrawHandle(ctx,DOM.liteChart.clientWidth/2,y1);
  }else if(d.type==='vline'){
    ctx.save();ctx.strokeStyle=color;ctx.lineWidth=selected?1.8:1.2;if(d.dash)ctx.setLineDash([5,4]);
    ctx.beginPath();ctx.moveTo(x1,0);ctx.lineTo(x1,DOM.liteChart.clientHeight);ctx.stroke();ctx.restore();
    if(selected)_liteDrawHandle(ctx,x1,DOM.liteChart.clientHeight/2);
  }else if(d.type==='rect'){
    ctx.save();ctx.strokeStyle=color;ctx.fillStyle=_liteHexAlpha(color,.10);
    ctx.lineWidth=selected?1.8:1.2;const rx=Math.min(x1,x2),ry=Math.min(y1,y2),rw=Math.abs(x2-x1),rh=Math.abs(y2-y1);
    ctx.fillRect(rx,ry,rw,rh);ctx.strokeRect(rx,ry,rw,rh);ctx.restore();
    if(selected){_liteDrawHandle(ctx,x1,y1);_liteDrawHandle(ctx,x2,y2);}
  }else if(d.type==='channel'){
    // CHỈ hiện kênh (2 cạnh + tô nền) khi đã có điểm thứ 3 (độ rộng kênh) thật sự được xác lập
    // ở bước 2 (rê chuột lên/xuống). Trong lúc bước 1 (mới chọn 2 điểm đầu-cuối, chưa rê) chỉ hiện
    // 1 đường chéo xem trước — không hiện kênh sớm.
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
    // Đường cong bán nguyệt: 2 điểm đầu-cuối như đường thẳng, bước 2 rê chuột tự do (cả trái/phải lẫn
    // lên/xuống) để chọn vị trí "đáy" (đỉnh cong) — đường cong luôn đi qua đúng vị trí chuột, không bị
    // ép về giữa 2 điểm đầu-cuối.
    // Quy đổi điểm "đáy" sang pixel TRƯỚC, rồi mới tính control-point trong pixel-space (xem ghi chú tại
    // _liteArcControlXY) → đường cong luôn đi qua đúng vị trí chuột dù trục giá tuyến tính hay log/percentage.
    const tx=pts[2]?_liteLogicalToX(pts[2].l):null,ty=pts[2]?_litePriceToY(pts[2].p):null;
    const ctrl=(tx!==null&&ty!==null)?_liteArcControlXY(x1,y1,x2,y2,tx,ty):null;
    if(ctrl){
      const cx=ctrl.cx,cy=ctrl.cy;
      // Tô màu phần diện tích giữa dây cung (đường thẳng nối 2 điểm đầu-cuối) và đường cong, giống kiểu
      // dải màu của công cụ Kênh giá, để dễ nhìn thấy "vùng" mà bán nguyệt bao lấy.
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
        const tx=_liteLogicalToX(pts[2].l),ty=_litePriceToY(pts[2].p);
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
      _liteDrawHandle(ctx,rx,entryY);_liteDrawHandle(ctx,rx+rw,entryY);
      _liteDrawHandle(ctx,rx,targetY);_liteDrawHandle(ctx,rx,(stopY!==null?stopY:entryY));
      if(target2Y!==null)_liteDrawHandle(ctx,rx,target2Y);
    }
  }
}
// _liteHexAlpha: trước đây tự parse hex riêng (trùng logic với _liteHexToRgba ở trên) — nay chỉ là
// lớp mỏng gọi lại _liteHexToRgba với màu fallback riêng của mình ('26,86,219' = #1a56db, màu vẽ mặc
// định), kết quả đầu ra giữ NGUYÊN y hệt như cài đặt cũ cho mọi input hex hợp lệ/không hợp lệ.
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
  ctx.rect(0,0,_liteMainPlotWidth(),DOM.liteChart.clientHeight||0);
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
function redrawLiteDrawings(){
  if(!_liteDrawCtx||!DOM.liteDrawCanvas)return;
  const w=DOM.liteChart.clientWidth,h=DOM.liteChart.clientHeight;
  _liteDrawCtx.clearRect(0,0,w,h);
  _liteDrawTrendCloud(_liteDrawCtx);
  _liteDrawBBBand(_liteDrawCtx);
  for(const d of _liteDrawings)_liteDrawShapeToCanvas(_liteDrawCtx,d);
  if(_liteDrawActive)_liteDrawShapeToCanvas(_liteDrawCtx,_liteDrawActive);
  if(_liteChannelPending)_liteDrawShapeToCanvas(_liteDrawCtx,_liteChannelPending);
  if(_liteArcPending)_liteDrawShapeToCanvas(_liteDrawCtx,_liteArcPending);
  if(_liteZigzagPending)_liteDrawShapeToCanvas(_liteDrawCtx,_liteZigzagPending);
  if(_liteLinePending)_liteDrawShapeToCanvas(_liteDrawCtx,_liteLinePending);
  _liteUpdateFloatingBar();
}
// Kết thúc (chốt) zigzag đang vẽ dở: nếu đã có >=2 điểm thì lưu thành hình vẽ hoàn chỉnh,
// nếu mới có 1 điểm (chưa đủ để thành hình) thì huỷ. Dùng chung cho double-click, phím
// Enter/Space/Escape, và khi chuyển sang công cụ khác (đặc biệt là con trỏ chuột).
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
// Công cụ Text: click lên chart mở 1 ô <textarea> ngay tại vị trí click để gõ chữ trực tiếp (hỗ trợ nhiều dòng
// tự nhiên nhờ textarea), thay vì hộp thoại prompt(). Enter xuống dòng bình thường; rời focus (blur) sẽ chốt
// chữ; Escape huỷ. Có thể gọi lại hàm này với 1 hình text có sẵn (editingShape) để SỬA nội dung đã viết.
function _liteApplyTextInputStyle(){
  if(!DOM.liteTextInput)return;
  DOM.liteTextInput.style.color=_liteDrawColor;
  DOM.liteTextInput.style.fontSize=_liteTextSize+'px';
  DOM.liteTextInput.style.fontFamily=LITE_TEXT_FONT_CSS[_liteTextFont]||LITE_TEXT_FONT_CSS.mono;
  DOM.liteTextInput.style.background=_liteTextBg||'rgba(255,255,255,.96)';
}
function _liteOpenTextInput(p0,ev,editingShape){
  if(!DOM.liteTextInput)return;
  // Chặn hành vi mặc định của trình duyệt khi click chuột trái lên 1 phần tử không focus-able (canvas):
  // mặc định trình duyệt sẽ tự dời focus sang phần tử focus-able gần nhất (khung chart), "cướp" mất focus
  // trước khi kịp focus ô chữ bên dưới → gõ chữ bị lọt ra ngoài (thành phím tắt / tìm mã). preventDefault()
  // trên pointerdown/mousedown ngăn được việc dời focus mặc định này.
  if(ev&&ev.preventDefault)ev.preventDefault();
  let x,y;
  if(ev){({x,y}=_liteXYFromEvent(ev));}
  else{x=_liteLogicalToX(p0.l);y=_litePriceToY(p0.p);}
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
  // Focus ngay lập tức (đa số trường hợp đã đủ), rồi focus lại 1 lần nữa ở animation frame kế tiếp để
  // phòng trường hợp trình duyệt chưa kịp layout xong phần tử vừa chuyển từ display:none sang hiện ra.
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
  redrawLiteDrawings();
}
function _liteShapeAnchor(d){
  const pts=d.points;
  if(!pts||!pts.length)return null;
  if(d.type==='text'){
    const x=_liteLogicalToX(pts[0].l),y=_litePriceToY(pts[0].p);
    if(x===null||y===null)return null;
    const m=_liteTextBoxMetrics(d);
    const w=m?m.width:0;
    return{x:x+w/2,y:y-6};
  }
  if(pts.length<2)return null;
  const x1=_liteLogicalToX(pts[0].l),y1=_litePriceToY(pts[0].p);
  const x2=_liteLogicalToX(pts[1].l),y2=_litePriceToY(pts[1].p);
  if(x1===null||y1===null||x2===null||y2===null)return null;
  if(d.type==='hline')return{x:DOM.liteChart.clientWidth/2,y:y1};
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
    // Lấy điểm cao nhất (y nhỏ nhất) trong CẢ 4 góc của kênh (2 góc cạnh gốc + 2 góc cạnh đã dịch offset).
    // Trước đây chỉ xét y1b (góc dịch của điểm đầu) nên khi đường chéo kênh bị nghiêng, góc dịch của điểm
    // cuối (y2b) có thể cao hơn mà không được tính tới → thanh điều chỉnh bị đặt thấp hơn đỉnh kênh thật,
    // khiến nó nằm lọt vào trong kênh thay vì nằm hẳn phía trên.
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
      const xx=_liteLogicalToX(pt.l),yy=_litePriceToY(pt.p);
      if(xx===null||yy===null)continue;
      minY=Math.min(minY,yy);sumX+=xx;n++;
    }
    return n?{x:sumX/n,y:minY}:null;
  }
  return{x:(x1+x2)/2,y:Math.min(y1,y2)};
}
// Lấy hình đang được chọn (theo _liteSelectedId) — gộp lại 1 chỗ duy nhất thay vì lặp lại
// cùng 1 biểu thức tra cứu ở rất nhiều handler bên dưới.
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
function _liteHitTestShape(d,x,y){
  const pts=d.points;
  if(d.type==='text'){
    const px=_liteLogicalToX(pts[0].l),py=_litePriceToY(pts[0].p);
    if(px===null||py===null)return null;
    const m=_liteTextBoxMetrics(d);
    const w=m?m.width:20,h=m?m.height:16;
    if(x>=px-LITE_HIT_TOL&&x<=px+w+LITE_HIT_TOL&&y>=py-LITE_HIT_TOL&&y<=py+h+LITE_HIT_TOL)return{part:'p0'};
    return null;
  }
  if(pts.length<2)return null;
  const x1=_liteLogicalToX(pts[0].l),y1=_litePriceToY(pts[0].p);
  const x2=_liteLogicalToX(pts[1].l),y2=_litePriceToY(pts[1].p);
  if(x1===null||y1===null||x2===null||y2===null)return null;
  if(d.type==='hline')return Math.abs(y-y1)<=LITE_HIT_TOL?{part:'line'}:null;
  if(d.type==='vline')return Math.abs(x-x1)<=LITE_HIT_TOL?{part:'line'}:null;
  if(d.type==='trendline'||d.type==='arrow'){
    if(Math.hypot(x-x1,y-y1)<=9)return{part:'p0'};
    if(Math.hypot(x-x2,y-y2)<=9)return{part:'p1'};
    return _liteSegDist(x,y,x1,y1,x2,y2)<=LITE_HIT_TOL?{part:'line'}:null;
  }
  if(d.type==='zigzag'){
    for(let i=0;i<pts.length;i++){
      const px=_liteLogicalToX(pts[i].l),py=_litePriceToY(pts[i].p);
      if(px!==null&&py!==null&&Math.hypot(x-px,y-py)<=9)return{part:'v'+i};
    }
    for(let i=0;i<pts.length-1;i++){
      const ax=_liteLogicalToX(pts[i].l),ay=_litePriceToY(pts[i].p);
      const bx=_liteLogicalToX(pts[i+1].l),by=_litePriceToY(pts[i+1].p);
      if(ax!==null&&ay!==null&&bx!==null&&by!==null&&_liteSegDist(x,y,ax,ay,bx,by)<=LITE_HIT_TOL)return{part:'line'};
    }
    return null;
  }
  if(d.type==='rect'){
    if(Math.hypot(x-x1,y-y1)<=9)return{part:'p0'};
    if(Math.hypot(x-x2,y-y2)<=9)return{part:'p1'};
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
    const tx=pts[2]?_liteLogicalToX(pts[2].l):null,ty=pts[2]?_litePriceToY(pts[2].p):null;
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
function _liteApplyDrag(d,info,cur){
  const dl=cur.l-info.startL,dp=cur.p-info.startP,op=info.origPoints;
  const key=d.type+':'+info.part;
  if(key==='trendline:p0'||key==='rect:p0'||key==='channel:p0'||key==='arrow:p0'||key==='arc:p0')d.points[0]={l:op[0].l+dl,p:op[0].p+dp};
  else if(key==='trendline:p1'||key==='rect:p1'||key==='channel:p1'||key==='arrow:p1'||key==='arc:p1')d.points[1]={l:op[1].l+dl,p:op[1].p+dp};
  else if(key==='trendline:line'||key==='rect:line'||key==='channel:line'||key==='arrow:line'){
    d.points[0]={l:op[0].l+dl,p:op[0].p+dp};d.points[1]={l:op[1].l+dl,p:op[1].p+dp};
  }else if(key==='arc:line'){
    d.points[0]={l:op[0].l+dl,p:op[0].p+dp};d.points[1]={l:op[1].l+dl,p:op[1].p+dp};
    if(op[2]&&Number.isFinite(op[2].l)&&Number.isFinite(op[2].p))d.points[2]={l:op[2].l+dl,p:op[2].p+dp};
  }else if(key==='hline:line'){
    d.points[0]={...op[0],p:op[0].p+dp};d.points[1]={...op[1],p:op[1].p+dp};
  }else if(key==='vline:line'){
    d.points[0]={...op[0],l:op[0].l+dl};d.points[1]={...op[1],l:op[1].l+dl};
  }else if(key==='channel:offset'){
    d.points[2]={offsetPrice:(info.origOffsetPrice||0)+dp};
  }else if(key==='arc:offset'){
    // pts[2] của arc là toạ độ (logical,price) của điểm "đáy" — kéo bao nhiêu, đáy dịch theo bấy nhiêu
    // theo cả 2 chiều (trái/phải lẫn lên/xuống), không chỉ riêng chiều dọc như channel.
    const baseL=(op[2]&&Number.isFinite(op[2].l))?op[2].l:(op[0].l+op[1].l)/2;
    const baseP=(op[2]&&Number.isFinite(op[2].p))?op[2].p:(op[0].p+op[1].p)/2;
    d.points[2]={l:baseL+dl,p:baseP+dp};
  }else if(d.type==='zigzag'&&info.part==='line'){
    d.points=op.map(pt=>({l:pt.l+dl,p:pt.p+dp}));
  }else if(d.type==='zigzag'&&info.part[0]==='v'){
    const idx=parseInt(info.part.slice(1),10);
    if(op[idx])d.points[idx]={l:op[idx].l+dl,p:op[idx].p+dp};
  }else if(key==='position:body'){
    d.points[0]={l:op[0].l+dl,p:op[0].p+dp};
    d.points[1]={l:op[1].l+dl,p:op[1].p+dp};
    d.stopP=(info.origStopP??(2*op[0].p-op[1].p))+dp;
    if(Number.isFinite(info.origTarget2P))d.target2P=info.origTarget2P+dp;
  }else if(key==='position:target'){
    d.points[1]={...op[1],p:op[1].p+dp};
  }else if(key==='position:target2'){
    d.target2P=(info.origTarget2P??d.target2P)+dp;
  }else if(key==='position:stop'){
    d.stopP=(info.origStopP??(2*op[0].p-op[1].p))+dp;
  }else if(key==='position:edgeL'){
    d.points[0]={...op[0],l:op[0].l+dl};
  }else if(key==='position:edgeR'){
    d.points[1]={...op[1],l:op[1].l+dl};
  }else if(key==='text:p0'){
    d.points[0]={l:op[0].l+dl,p:op[0].p+dp};
  }
}
function _liteStartShapeDrag(hit,ev){
  const d=hit.shape;
  _liteSelectShape(d.id);
  const startPt=_litePtFromEvent(ev);if(!startPt)return;
  _liteDragInfo={
    part:hit.part,
    origPoints:JSON.parse(JSON.stringify(d.points||[])),
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
function _liteDrawTitleSegments(ctx,segments,x,y){
  for(const seg of segments){
    ctx.fillStyle=seg.color;
    ctx.fillText(seg.text,x,y);
    x+=ctx.measureText(seg.text).width;
  }
}
// Vẽ badge tín hiệu (emoji + nhãn màu) lên canvas copy, y hệt badge #lite-chart-signal đang hiển
// thị trên chart — badge đó là lớp DOM nổi phía trên canvas nên takeScreenshot() không chụp được,
// phải tự vẽ vào canvas copy. Đọc trực tiếp kích thước/màu đã render của DOM badge thật (không tự
// định nghĩa lại màu/kích thước riêng) để luôn khớp 100% với badge thật, kể cả khi CSS đổi màu sau này.
function _liteDrawSignalBadge(ctx,x,y,dpr){
  const el=DOM.liteChartSignal;
  if(!el)return;
  const emojiEl=el.querySelector('.s-emoji'),badgeEl=el.querySelector('.s-badge');
  if(!emojiEl||!badgeEl)return;
  const emojiCs=getComputedStyle(emojiEl),badgeCs=getComputedStyle(badgeEl);
  const emojiR=emojiEl.getBoundingClientRect(),badgeR=badgeEl.getBoundingClientRect();
  const gap=Math.round(5*dpr);
  ctx.textBaseline='middle';
  ctx.font=emojiCs.font||`${emojiCs.fontSize} sans-serif`;
  ctx.fillText(emojiEl.textContent,x,y+emojiR.height*dpr/2);
  const bx=x+emojiR.width*dpr+gap,bw=badgeR.width*dpr,bh=badgeR.height*dpr;
  const br=(parseFloat(badgeCs.borderRadius)||0)*dpr;
  ctx.beginPath();
  if(ctx.roundRect)ctx.roundRect(bx,y,bw,bh,br);else ctx.rect(bx,y,bw,bh);
  ctx.fillStyle=badgeCs.backgroundColor;ctx.fill();
  ctx.lineWidth=Math.max(1,(parseFloat(badgeCs.borderWidth)||1)*dpr);
  ctx.strokeStyle=badgeCs.borderColor;ctx.stroke();
  ctx.fillStyle=badgeCs.color;
  ctx.font=badgeCs.font||`${badgeCs.fontWeight} ${badgeCs.fontSize} sans-serif`;
  ctx.textAlign='center';
  ctx.fillText(badgeEl.textContent,bx+bw/2,y+bh/2+dpr);
  ctx.textAlign='left';
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
    const hasSigBadge=!!(DOM.liteChartSignal&&DOM.liteChartSignal.classList.contains('on'));
    const dpr=window.devicePixelRatio||1;
    const titleH=titleSegments.length?Math.round(30*dpr):0;
    const badgeH=hasSigBadge?Math.round(24*dpr):0;
    const out=document.createElement('canvas');
    out.width=Math.max(...panes.map(p=>p.canvas.width));
    out.height=titleH+badgeH+panes.reduce((sum,p)=>sum+p.canvas.height,0);
    const ctx=out.getContext('2d');
    ctx.fillStyle='#ffffff';ctx.fillRect(0,0,out.width,out.height);
    if(titleSegments.length){
      ctx.font=`400 ${Math.round(11*dpr)}px "IBM Plex Mono",monospace`;
      ctx.textBaseline='middle';
      _liteDrawTitleSegments(ctx,titleSegments,10*dpr,titleH/2);
    }
    if(hasSigBadge){
      _liteDrawSignalBadge(ctx,10*dpr,titleH+Math.round(3*dpr),dpr);
    }
    let y=titleH+badgeH;
    panes.forEach(p=>{
      ctx.drawImage(p.canvas,0,y);
      y+=p.canvas.height;
    });
    if(DOM.liteDrawCanvas){
      const mainCanvas=panes[0].canvas;
      ctx.drawImage(DOM.liteDrawCanvas,0,0,DOM.liteDrawCanvas.width,DOM.liteDrawCanvas.height,0,titleH+badgeH,mainCanvas.width,mainCanvas.height);
    }
    const toBlobPromise=()=>new Promise(resolve=>out.toBlob(resolve,'image/png'));
    // Copy as Bitmap: truyền thẳng Promise<Blob> vào ClipboardItem (không await trước) để trình duyệt
    // vẫn coi đây là hành động clipboard gắn liền với cú click của người dùng (user-gesture), tránh bị
    // rớt xuống nhánh tải file mỗi lần do lỡ "mất" quyền clipboard khi await xong mới gọi write().
    if(navigator.clipboard&&window.ClipboardItem){
      try{
        await navigator.clipboard.write([new ClipboardItem({'image/png':toBlobPromise()})]);
        if(btn){const old=btn.textContent;btn.textContent='✅';setTimeout(()=>{btn.textContent=old;},1200);}
        return;
      }catch(e){
        console.warn('Copy bitmap vào clipboard lỗi, chuyển sang tải file:',e);
      }
    }
    // Phương án dự phòng khi trình duyệt không hỗ trợ Clipboard API ảnh (vd Firefox cũ / http không an toàn)
    const blob=await toBlobPromise();
    if(!blob)return;
    const url=URL.createObjectURL(blob);
    const a=document.createElement('a');a.href=url;a.download=`chart_${_liteSymbol}_${_liteTf}.png`;a.click();
    URL.revokeObjectURL(url);
    if(btn){const old=btn.textContent;btn.textContent='⬇️';setTimeout(()=>{btn.textContent=old;},1200);}
  }catch(e){console.error('copyLiteChartImage lỗi:',e);}
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
      // Đang bật → tắt: đường Target (đang là Target 1) biến mất, đường Target 2 (giá đã vẽ/kéo ban đầu)
      // trở lại thành đường Target duy nhất — quay về đúng mặc định chỉ 1 target.
      sel.points[1]={...sel.points[1],p:sel.target2P};
      delete sel.target2P;
    }else{
      // Đang tắt → bật: đường Target hiện có (đã vẽ) đổi thành Target 2 — giữ nguyên đúng giá đó.
      // Đường Target mới (Target 1) được chèn vào giữa Entry và Target 2, nằm ở nửa khoảng cách.
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
  DOM.liteDrawCopy?.addEventListener('click',()=>copyLiteChartImage(DOM.liteDrawCopy));
  if(DOM.liteTextInput){
    DOM.liteTextInput.addEventListener('keydown',e=>{
      // Chặn nổi bọt lên #lite-chart-frame để không kích hoạt phím tắt khác (mở ô tìm mã, xoá hình...)
      // trong lúc đang gõ chữ. Enter giờ xuống dòng bình thường (mặc định của textarea) thay vì chốt chữ;
      // Ctrl/Cmd+Enter chốt nhanh; Escape huỷ.
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
  // ── Kéo / chọn / di chuyển hình đã vẽ (chế độ con trỏ) ──
  // Bắt ở pha capture trên container chart để chặn thao tác pan/zoom mặc định của
  // Lightweight Charts ĐÚNG lúc người dùng nhắm trúng 1 hình đã vẽ.
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
      if(_liteDrawTool!=='cursor'||_liteDragInfo)return;
      const{x,y}=_liteXYFromEvent(e);
      DOM.liteChart.style.cursor=_liteHitTest(x,y)?'move':'';
    });
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
  // Tính offset (theo giá) của điểm chuột hiện tại so với đường chéo gốc — dùng chung cho bước 2
  // của cả công cụ Kênh giá (channel) và Đường cong bán nguyệt (arc).
  function _liteOffsetFromChord(pend,p){
    const denom=(pend.points[1].l-pend.points[0].l)||1e-6;
    const lineP=pend.points[0].p+(pend.points[1].p-pend.points[0].p)*(p.l-pend.points[0].l)/denom;
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
    // Kết thúc Zigzag bằng double-click: bỏ điểm cuối trùng do click thứ 2 của thao tác double-click sinh ra
    if(_liteDrawTool==='zigzag'&&_liteZigzagPending){
      e.preventDefault();
      // Double-click sinh ra 2 lần click liên tiếp ở cùng 1 vị trí → click thứ 2 đã bị pointerdown
      // phía dưới nối thêm thành điểm trùng, cần bỏ điểm cuối trùng đó trước khi chốt hình.
      if(_liteZigzagPending.points.length>1)_liteZigzagPending.points.pop();
      _liteFinishZigzag();
      setLiteDrawTool('cursor');
    }
  });
  DOM.liteDrawCanvas.addEventListener('pointerdown',e=>{
    if(_liteDrawTool==='cursor')return;
    const p0=_litePtFromEvent(e);if(!p0)return;
    // Bước 2 của Kênh giá: đã có đường chéo (bước 1) → click để chốt độ rộng kênh
    if(_liteDrawTool==='channel'&&_liteChannelPending){
      const pend=_liteChannelPending;
      pend.points[2]={offsetPrice:_liteOffsetFromChord(pend,p0)};
      _liteDrawings.push(pend);_liteChannelPending=null;
      saveLiteDrawings();
      setLiteDrawTool('cursor');_liteSelectShape(pend.id);
      return;
    }
    // Bước 2 của Đường cong bán nguyệt: đã có đường chéo (bước 1) → click để chốt độ cong
    if(_liteDrawTool==='arc'&&_liteArcPending){
      const pend=_liteArcPending;
      pend.points[2]=p0;
      _liteDrawings.push(pend);_liteArcPending=null;
      saveLiteDrawings();
      setLiteDrawTool('cursor');_liteSelectShape(pend.id);
      return;
    }
    // Zigzag: mỗi click nối thêm 1 điểm; double-click (xử lý riêng ở trên) để kết thúc
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
      // Đang soạn dở 1 ô chữ (chưa blur): click ra ngoài phạm vi ô chữ chỉ để KẾT THÚC soạn (chốt chữ),
      // không mở thêm khung chữ mới tại vị trí vừa click. Muốn viết chữ tiếp, người dùng phải bấm lại
      // công cụ Text rồi click vị trí mới.
      if(_liteTextEditPos){
        _liteCommitTextInput();
        setLiteDrawTool('cursor');
        return;
      }
      // Chưa soạn gì (mới bật công cụ Text): click thẳng lên chart để gõ chữ tại đúng vị trí click,
      // không dùng hộp thoại prompt() nữa.
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
      // Vẽ kiểu click-click: click điểm đầu, di chuột xem trước, click điểm cuối để chốt (không cần kéo giữ chuột).
      // Với channel/arc, điểm cuối này CHƯA phải là chốt hình — chỉ xác định xong 2 điểm đầu-cuối (đường chéo),
      // sau đó chuyển sang bước 2 (rê chuột lên/xuống để tạo kênh / uốn cong).
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
      if(_liteDrawActive.type==='channel'){
        // Bước 1 (đường chéo) vừa xong → CHƯA push, chuyển sang chờ bước 2 (rê chuột + click để chốt độ rộng)
        if(moved)_liteChannelPending=_liteDrawActive;
        _liteDrawActive=null;
        redrawLiteDrawings();
        return;
      }
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
function openLiteSearchWithChar(ch){
  const cur=DOM.liteChartSearch.classList.contains('on')?DOM.liteChartSearch.value:'';
  DOM.liteChartSearch.value=(cur+String(ch||'').toUpperCase()).slice(0,10);
  resizeLiteSearchInput();
  DOM.liteChartSearch.classList.add('on');
  DOM.liteChartSearch.focus();
}
// Phím đơn (không kèm Ctrl/Alt/Meta) là chữ/số thường — dùng chung cho 2 nơi bắt phím gõ nhanh mã cổ phiếu.
function _liteIsPlainAlnumKey(e){
  return !(e.metaKey||e.ctrlKey||e.altKey||e.key.length!==1||!/^[a-zA-Z0-9]$/.test(e.key));
}
// Đuôi dùng chung cho 2 nơi bắt phím gõ nhanh mở ô tìm mã (#lite-chart-frame và document):
// chỉ xử lý phím chữ/số đơn, bỏ qua khi đang gõ chú thích Text hoặc đang focus sẵn 1 input/textarea/select
// khác (để input đó tự nhận phím, tránh lặp chữ khi gõ tiếng Việt qua IME). Trả về true nếu đã mở ô tìm mã.
function _liteTryOpenSearchOnKey(e){
  if(!_liteIsPlainAlnumKey(e))return false;
  if(_liteTextEditPos||document.activeElement?.isContentEditable)return false;
  const tag=(document.activeElement?.tagName||'').toLowerCase();
  if(tag==='input'||tag==='textarea'||tag==='select')return false;
  e.preventDefault();
  openLiteSearchWithChar(e.key);
  return true;
}
function renderLiteIndicators(){
  if(!_liteChart||!_liteRsiChart||!_liteMacdChart)return;
  const prevRange=_liteGetVisibleLogicalRange();
  _clearLiteIndicators();
  // Đọc trạng thái checkbox đúng 1 lần/chỉ báo (thay vì querySelector lại lần 2 lúc setData bên dưới).
  const showRsi=_liteChecked('rsi');
  const showMacd=_liteChecked('macd');
  const maEmaOn=_liteChecked('maema_on');
  const maOn=maEmaOn?LITE_MA_PERIODS.filter(p=>_liteChecked('ma'+p)):[];
  const emaOn=maEmaOn?LITE_EMA_PERIODS.filter(p=>_liteChecked('ema'+p)):[];
  const bbOn=_liteChecked('bb');
  const trendOn=_liteChecked('trend');
  applyLitePaneLayout();
  // (không cần applyOptions margin cho _liteVolume ở đây — _liteRefreshVolumeTop() phía dưới sẽ
  // tạo lại series volume từ đầu và tự set margin, gọi ở đây sẽ bị ghi đè ngay nên chỉ tốn công.)
  maOn.forEach(p=>{
    _liteIndicatorSeries.push({chart:_liteChart,kind:'ma',period:p,series:_liteChart.addLineSeries({color:_liteIndColors['ma'+p],lineWidth:1,title:'',priceLineVisible:false,lastValueVisible:true,crosshairMarkerVisible:false})});
  });
  emaOn.forEach(p=>{
    _liteIndicatorSeries.push({chart:_liteChart,kind:'ema',period:p,series:_liteChart.addLineSeries({color:_liteIndColors['ema'+p],lineWidth:1,title:'',priceLineVisible:false,lastValueVisible:true,crosshairMarkerVisible:false})});
  });
  if(bbOn){
    // Chỉ vẽ 3 đường (upper/mid/lower) bằng series thật của thư viện.
    // Phần TÔ MÀU giữa 2 đường band được vẽ riêng bằng canvas (xem _liteDrawBBBand)
    // để clip chính xác trong dải, không đụng gì tới phần dưới đường band dưới.
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
    hist.priceScale().applyOptions({autoScale:true,scaleMargins:{top:.07,bottom:.10}});
    _liteMacdCrosshairSeries=macdLine;
    _liteIndicatorSeries.push({chart:_liteMacdChart,series:hist},{chart:_liteMacdChart,series:macdLine},{chart:_liteMacdChart,series:sigLine});
  }
  _liteRefreshVolumeTop();
  if(!_liteApplyVisibleLogicalRange(prevRange))setLiteRightOffset();
  redrawLiteDrawings();
}
function _liteRefreshVolumeTop(){
  // Vẽ lại volume sau cùng để nó luôn nổi trên phần fill/nền trắng của BB, không bị che.
  if(!_liteChart||!_liteVolume)return;
  try{_liteChart.removeSeries(_liteVolume);}catch(e){}
  _liteVolume=_liteChart.addHistogramSeries({priceFormat:{type:'volume'},priceScaleId:'',lastValueVisible:false,priceLineVisible:false});
  _liteVolume.priceScale().applyOptions({scaleMargins:{top:.78,bottom:0}});
  _liteVolume.setData(_liteVolumeData);
}
function showLiteChartStatus(status){
  if(!DOM.liteChartStatus)return;
  if(!status){DOM.liteChartStatus.classList.remove('on');return;}
  const fetching=!!status.need_fetch;
  DOM.liteChartStatus.title=fetching?'Đang update chart (vnstock)':'Đã tải từ cache';
  DOM.liteChartStatus.classList.toggle('fetching',fetching);
  DOM.liteChartStatus.classList.add('on');
  clearTimeout(DOM.liteChartStatus._hideTimer);
  DOM.liteChartStatus._hideTimer=setTimeout(()=>DOM.liteChartStatus.classList.remove('on'),3000);
}
async function loadLiteChart(sym='FPT',retry=1){
  const s=(sym||'FPT').toUpperCase().trim();
  if(!DOM.liteChart)return;
  initLiteChart();
  if(DOM.liteChartInput)DOM.liteChartInput.value='';
  if(DOM.liteChartTitle)DOM.liteChartTitle.textContent=window.LightweightCharts?'Đang tải...':'Thiếu thư viện chart';
  DOM.liteChartEmpty.textContent=window.LightweightCharts?'Đang tải chart...':'Không tải được Lightweight Charts';
  DOM.liteChartEmpty.style.display='flex';
  showLiteChartStatus(null);
  if(!window.LightweightCharts){
    if(retry>0)setTimeout(()=>loadLiteChart(s,retry-1),1200);
    return;
  }
  // Gọi song song: status chỉ để hiển thị placeholder text, không cần chờ nó xong
  // mới bắt đầu fetch data thật — gộp lại giúp giảm ~1 round-trip độ trễ tải chart.
  const statusPromise=fetch('/api/lightweight_chart_status/'+encodeURIComponent(s))
    .then(x=>x.json()).catch(()=>null);
  statusPromise.then(st=>{
    if(DOM.liteChartEmpty.style.display!=='none')
      DOM.liteChartEmpty.textContent=(st&&st.need_fetch)?'Đang update chart (vnstock)...':'Đang tải cache...';
  });
  try{
    const r=await fetch('/api/lightweight_chart/'+encodeURIComponent(s)+'?tf='+encodeURIComponent(_liteTf)+'&limit=1000');
    if(!r.ok)throw new Error('no_cache');
    const j=await r.json();
    _liteSymbol=s;setLiteTf(j.timeframe||_liteTf);
    _liteData=(j.candles||[]).map((bar,idx,arr)=>{
      const prev=idx>0?arr[idx-1].close:null;
      const pct=prev?((bar.close-prev)/prev*100):0;
      return{...bar,pct};
    });
    _liteDataByTime=new Map(_liteData.map(bar=>[liteTimeKey(bar.time),bar]));
    _liteVolumeData=_liteNormalizeVolumeData(j.volume,_liteData);
    _liteCandle.setData(_liteData);
    _liteVolume.setData(_liteVolumeData);
    _liteUpdateWhitespace();
    renderLiteIndicators();
    setLiteRightOffset();
    _liteChart.priceScale('right').applyOptions({autoScale:true,scaleMargins:{top:.12,bottom:.18}});
    DOM.liteChartEmpty.style.display='none';
    updateLiteTitle(_liteData[_liteData.length-1]);
    _liteApplyBuySignal();
    loadLiteDrawings();resizeLiteDrawCanvas();redrawLiteDrawings();
    showLiteChartStatus(await statusPromise);
  }catch(e){
    if(DOM.liteChartTitle)DOM.liteChartTitle.textContent='Không có dữ liệu';
    DOM.liteChartEmpty.textContent='Chưa có dữ liệu cache cho '+s;
    if(retry>0)setTimeout(()=>loadLiteChart(s,retry-1),5000);
  }
}
function bindLiteChartControls(){
  loadLiteIndicatorPrefs();
  loadLiteTrendMode();
  bindLiteIndColorPickers();
  bindLiteIndGroupDropdowns();
  bindLiteDrawToolbar();
  _liteBindSymInput(DOM.liteChartInput,raw=>{
    if(_liteInputTimer)clearTimeout(_liteInputTimer);
    if(raw.length>=2)_liteInputTimer=setTimeout(()=>loadLiteChart(raw,0),450);
  });
  DOM.liteChartInput?.addEventListener('keydown',e=>{
    if(e.key==='Enter')loadLiteChart(DOM.liteChartInput.value||_liteSymbol,0);
  });
  DOM.liteChartTf?.addEventListener('click',e=>{
    const btn=e.target.closest('.lite-tf-btn');if(!btn)return;
    setLiteTf(btn.dataset.tf);loadLiteChart(_liteSymbol,0);
  });
  DOM.liteIndicators?.addEventListener('change',()=>{saveLiteIndicatorPrefs();saveLiteTrendMode();updateLiteIndGroupCounts();renderLiteIndicators();_liteApplyBuySignal();});
  DOM.liteChartFrame?.addEventListener('click',()=>{
    // Không cướp focus về khung chart khi đang gõ chữ (công cụ Text) — nếu không, focus bị giật lại
    // về khung ngay sau click mở ô chữ, khiến phím gõ sau đó bị khung bắt và hiểu nhầm thành gõ mã.
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
    // Đang gõ chữ (công cụ Text) → không xử lý phím tắt của khung chart (xoá hình, mở tìm mã...) ở đây.
    // Bản thân ô chữ (#lite-text-input) đã tự xử lý Enter/Escape và stopPropagation() cho các phím khác,
    // đây chỉ là lớp bảo vệ thêm phòng khi ô chữ chưa kịp nhận focus.
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
    // #lite-chart-search nằm lồng bên trong #lite-chart-frame nên keydown của nó vẫn nổi bọt lên tới đây;
    // _liteTryOpenSearchOnKey tự bỏ qua khi đang focus sẵn 1 input/textarea/select để input đó tự nhận phím
    // (tránh lặp chữ khi gõ tiếng Việt qua IME).
    // stopPropagation() ở đây là bắt buộc: nếu không, event vẫn nổi bọt tiếp lên listener trên document bên
    // dưới và event đó CŨNG cố mở ô tìm mã lần nữa cho cùng 1 lần bấm phím. Bình thường .focus() đã chuyển
    // focus đồng bộ nên listener document tự bỏ qua (activeElement đã là input) — nhưng việc dựa vào đúng
    // thời điểm đó không chắc chắn 100% khi gõ rất nhanh hoặc qua bộ gõ tiếng Việt, khiến ký tự ĐẦU TIÊN
    // (lúc ô tìm mã còn chưa tồn tại/focus) có thể bị 2 nơi cùng xử lý → chữ bị lặp. stopPropagation() chặn
    // triệt để, không phụ thuộc timing của trình duyệt nữa.
    if(_liteTryOpenSearchOnKey(e))e.stopPropagation();
  });
  document.addEventListener('keydown',e=>{
    if(!_litePointerInside)return;
    _liteTryOpenSearchOnKey(e);
  });
  _liteBindSymInput(DOM.liteChartSearch,raw=>{
    resizeLiteSearchInput();
    if(_liteInputTimer)clearTimeout(_liteInputTimer);
    if(raw.length>=2)_liteInputTimer=setTimeout(()=>{DOM.liteChartSearch.classList.remove('on');loadLiteChart(raw,0);},450);
  });
  DOM.liteChartSearch?.addEventListener('keydown',e=>{
    if(e.key==='Escape'){DOM.liteChartSearch.classList.remove('on');DOM.liteChartFrame.focus();}
    if(e.key==='Enter'&&DOM.liteChartSearch.value){DOM.liteChartSearch.classList.remove('on');loadLiteChart(DOM.liteChartSearch.value,0);}
  });
  DOM.liteMacdResizer?.addEventListener('pointerdown',e=>{
    e.preventDefault();
    const startY=e.clientY,startH=_liteMacdSoloHeight||DOM.liteMacdChart.clientHeight||176;
    // Gộp các sự kiện pointermove (bắn nhiều hơn tốc độ khung hình màn hình) thành đúng 1 lần
    // cập nhật layout/chart mỗi khung hình bằng rAF — applyLitePaneLayout() gọi applyOptions() trên
    // cả 2 chart nên là thao tác nặng, gọi trực tiếp theo từng pointermove sẽ gây giật khi kéo.
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
function _openMaximizedWindow(url,name,width,height,offsetLeft,offsetTop,extra=''){
  const box=_getPopupViewport();
  const popupLeft=box.left+Math.max(0,box.width-width-offsetLeft);
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
function _refreshChartModeUI(){
  const chartBtn=$('hover-preview-btn');
  chartBtn.classList.toggle('on',_hoverPreviewOn||_isPopoutMode);
  chartBtn.textContent=_isPopoutMode?'Chart: POP':_hoverPreviewOn?'Chart: ON':'Chart: OFF';
  $('hmap-popout-btn').classList.toggle('on',_isPopoutMode);
  $('hmap-simplize-btn').classList.toggle('on',_isSimplizeMode);
}
function _resetPopupChrome(){
  $('popup-phdr').style.display='';
  DOM.mobHdrRow1.style.display='none';
  DOM.mobTabRow.style.display='none';
  DOM.mobHdrLand.style.display='';
  DOM.mobClose.style.display='none';
}
function _stopSimplizeWatch(){
  if(_simplizeWatch){clearInterval(_simplizeWatch);_simplizeWatch=null;}
}
function closeSimplizeWindow(){
  _isSimplizeMode=false;
  _stopSimplizeWatch();
  if(_simplizeWin&&!_simplizeWin.closed)try{_simplizeWin.close();}catch(e){}
  _simplizeWin=null;
  _refreshChartModeUI();
}
function updateSimplize(sym){
  if(!_simplizeWin||_simplizeWin.closed){
    if(_isSimplizeMode)closeSimplizeWindow();
    return;
  }
  try{_simplizeWin.location.href=simplizeUrl(sym);}catch(e){}
}
function quickSimplize(){
  const sym=_hoverPreviewCurrent||_sym||'VNINDEX';
  if(_isSimplizeMode&&_simplizeWin&&!_simplizeWin.closed){updateSimplize(sym);_simplizeWin.focus();return;}
  const box=_getPopupViewport();
  const w=Math.min(1600,box.width-40),h=box.height;
  _simplizeWin=_openMaximizedWindow(simplizeUrl(sym),'ScannerSimplize',w,h,0,0);
  if(!_simplizeWin){alert('Trình duyệt chặn popup!');closeSimplizeWindow();return;}
  _isSimplizeMode=true;
  _refreshChartModeUI();
  _stopSimplizeWatch();
  _simplizeWatch=setInterval(()=>{
    if(_simplizeWin&&_simplizeWin.closed)closeSimplizeWindow();
  },1000);
}
// ═══════════════════════════════════════════════════════
// HEATMAP DATA
// ═══════════════════════════════════════════════════════
const HMAP_COLS=__HMAP_COLS_CONFIG__;
const TS_POOL=__TS_POOL_CONFIG__;
// ═══════════════════════════════════════════════════════
// HEATMAP RENDER
// ═══════════════════════════════════════════════════════
function cellStyle(pct){
  let r,g,b;
  const pos=[[235,248,238],[231,247,234],[225,245,228],[220,243,224],[215,242,220],[205,238,211],[195,235,200],[186,232,193],[178,228,186],[169,224,178],[160,220,170],[154,218,165],[148,216,160]];
  const neg=[[255,232,225],[255,228,221],[255,223,216],[254,216,209],[253,208,201],[252,199,191],[250,190,181],[248,181,172],[246,173,164],[244,166,158],[243,160,153],[242,155,149],[240,150,145]];
  if(pct>=6.5){r=250;g=170;b=225}else if(pct>=0.05){[r,g,b]=pos[Math.min(pos.length-1,Math.floor(pct*2))]}
  else if(pct>-0.05){r=245;g=245;b=200}else if(pct>=-6.5){[r,g,b]=neg[Math.min(neg.length-1,Math.floor(Math.abs(pct)*2))]}
  else{r=175;g=250;b=255}
  return{bg:`rgb(${r},${g},${b})`,fg:(.299*r+.587*g+.114*b)>160?'rgb(30,30,30)':'rgb(15,15,15)'};
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
  return `<div class="hmap-col"><div class="hmap-group"><div class="hmap-ghdr"><span class="hmap-gname">FOLLOW</span></div>${sortByPct(FOLLOW,d).map(s=>mkCell(s,d)).join('')}</div></div>`;
}
function renderHeatmap(d){
  if(!d||!Object.keys(d).length){DOM.hmapGrid.innerHTML='<div class="empty"><div class="big">🗺</div><div>Chưa có dữ liệu</div></div>';return;}
  const maxRows=Math.max(...HMAP_COLS.map(cd=>cd.groups.reduce((s,g)=>s+g.syms.length,0)));
  const tsSyms=TS_POOL.filter(s=>d[s]!==undefined).sort((a,b)=>((d[b]||{}).pct||0)-((d[a]||{}).pct||0)).slice(0,maxRows);
  const parts=[`<div class="hmap-col">${mkGroup('TRADING STOCKS',tsSyms,d)}</div>`];
  HMAP_COLS.forEach((cd,i)=>{
    const extra=i===HMAP_COLS.length-1?mkSectorCol(d):'';
    parts.push(`<div class="hmap-col">${cd.groups.map(g=>mkGroup(g.name,g.syms,d)).join('')}${extra}</div>`);
  });
  const follow=mkFollowGroup(d);if(follow)parts.push(follow);
  DOM.hmapGrid.innerHTML=parts.join('');
}
// Event delegation heatmap
DOM.hmapGrid.addEventListener('click',e=>{
  const cell=e.target.closest('.hmap-cell');if(!cell)return;
  const sym=cell.dataset.sym;
  if(IS_MOBILE()){openChart(sym);return;}
  _hmapDesktopClick(sym);
});
DOM.hmapGrid.addEventListener('dblclick',e=>{
  const cell=e.target.closest('.hmap-cell');if(!cell||IS_MOBILE())return;
  if(_hmapClickTimer)clearTimeout(_hmapClickTimer);
  _syncHoverPreview(cell.dataset.sym);
  updatePopout(cell.dataset.sym);
  updateSimplize(cell.dataset.sym);
  loadLiteChart(cell.dataset.sym,0);
  openChart(cell.dataset.sym);
});
let _hmapClickTimer=null;
function _hmapDesktopClick(sym){
  if(_hmapClickTimer)clearTimeout(_hmapClickTimer);
  _hmapClickTimer=setTimeout(()=>{
    _syncHoverPreview(sym);
    updatePopout(sym);
    updateSimplize(sym);
    loadLiteChart(sym,0);
    if(_isPopoutMode)return;
    if(_isChartPanelOpen)return;
    if(_isSimplizeMode&&!_hoverPreviewOn)return;
    if(!_hoverPreviewOn){openChart(sym);return;}
  },220);
}
// Event delegation sig-list
DOM.sigList.addEventListener('click',e=>{
  const row=e.target.closest('.sig-row');if(!row)return;
  const s=row.dataset.sym;if(IS_MOBILE())openChart(s);else _hmapDesktopClick(s);
});
DOM.sigList.addEventListener('dblclick',e=>{
  const row=e.target.closest('.sig-row');if(!row||IS_MOBILE())return;
  if(_hmapClickTimer)clearTimeout(_hmapClickTimer);
  _syncHoverPreview(row.dataset.sym);
  updatePopout(row.dataset.sym);
  updateSimplize(row.dataset.sym);
  loadLiteChart(row.dataset.sym,0);
  openChart(row.dataset.sym);
});
DOM.momentumList.addEventListener('click',e=>{
  const row=e.target.closest('.momentum-row');if(!row)return;
  const s=row.dataset.sym;if(IS_MOBILE())openChart(s);else _hmapDesktopClick(s);
});
DOM.signalHeader.addEventListener('click',e=>{
  if(e.target.closest('#journal-open-btn'))return;
  DOM.momentumBox.classList.toggle('on');
});
// ═══════════════════════════════════════════════════════
// SANKEY RENDER
// ═══════════════════════════════════════════════════════
const SANKEY_SECTORS=[];
HMAP_COLS.forEach(col=>col.groups.forEach(g=>{if(g.name!=='VN30')SANKEY_SECTORS.push(g);}));
const SANKEY_SVG_NS='http://www.w3.org/2000/svg';
const SANKEY_COLORS=['#ec8784','#a378e0','#da9672','#d5cc71','#72dacd','#a1e078','#7882e0','#e0b478','#78e0b4','#e078c8','#96c8fa','#b5d67a'];
const SANKEY_MIN_WEIGHT=10000000;
function sankeyFmtNum(v){return(!Number.isFinite(v)||v<=0)?'--':(v/1e9).toFixed(1)+'B';}
function sankeyFmtPct(v){return Number.isFinite(v)?(v>=0?'+':'')+v.toFixed(2)+'%':'--';}
function sankeyBadgeColor(pct){
  if(pct>0)return{fill:'#0e9f6e',text:'#fff'};
  if(pct<0)return{fill:'#e02424',text:'#fff'};
  return{fill:'#d4a017',text:'#fff'};
}
function sankeyWeight(entry){
  if(!entry||typeof entry!=='object')return 0;
  const totalValue=Number(entry.total_value);
  return Number.isFinite(totalValue)&&totalValue>0?totalValue:0;
}
function sankeyPath(x1,y1t,y1b,x2,y2t,y2b){
  const c1=x1+(x2-x1)*0.45,c2=x1+(x2-x1)*0.55;
  return`M ${x1} ${y1t} C ${c1} ${y1t}, ${c2} ${y2t}, ${x2} ${y2t} L ${x2} ${y2b} C ${c2} ${y2b}, ${c1} ${y1b}, ${x1} ${y1b} Z`;
}
function sankeyEl(tag,attrs={},text=''){
  const el=document.createElementNS(SANKEY_SVG_NS,tag);
  Object.entries(attrs).forEach(([k,v])=>el.setAttribute(k,String(v)));
  if(text)el.textContent=text;
  return el;
}
function sankeyLimit(rank){
  if(rank<=1)return 10;
  if(rank<=4)return 6;
  if(rank<=7)return 4;
  if(rank<=11)return 2;
  return 1;
}
function sankeyDataset(data){
  const sectors=SANKEY_SECTORS.map(g=>{
    const stocks=g.syms.map(sym=>{
      const entry=data[sym],weight=sankeyWeight(entry);
      return{sym,pct:Number(entry?.pct),weight,sector:g.name};
    }).filter(x=>x.weight>SANKEY_MIN_WEIGHT);
    return{name:g.name,stocks,weight:stocks.reduce((sum,s)=>sum+s.weight,0)};
  }).filter(sec=>sec.weight>0);
  sectors.sort((a,b)=>b.weight-a.weight);
  sectors.forEach((sec,idx)=>{sec.rank=idx;sec.limit=sankeyLimit(idx);sec.color=SANKEY_COLORS[idx%SANKEY_COLORS.length];});
  const globalStocks=sectors.flatMap(sec=>sec.stocks).sort((a,b)=>b.weight-a.weight);
  sectors.forEach(sec=>{
    let drawn=0;sec.visibleStocks=[];
    for(const stock of globalStocks){
      if(stock.sector!==sec.name)continue;
      sec.visibleStocks.push(stock);
      drawn+=1;
      if(drawn>=sec.limit)break;
    }
  });
  return{sectors,total:sectors.reduce((sum,sec)=>sum+sec.weight,0)};
}
function renderSankey(data){
  const svg=DOM.sankeySvg;if(!svg)return;
  svg.innerHTML='';
  const dataset=sankeyDataset(data||{}),sectors=dataset.sectors;
  if(!sectors.length||dataset.total<=0){
    const fo=sankeyEl('foreignObject',{x:0,y:0,width:1600,height:900});
    const div=document.createElement('div');
    div.className='sankey-empty';div.textContent='Chưa có dữ liệu heatmap để dựng Sankey';
    fo.appendChild(div);svg.appendChild(fo);return;
  }
  const total=dataset.total;
  const chart={yStart:120,drawH:540,marketX:130,sectorX:555,stockX:1285,marketW:6,barW:10};
  const gapSector=5,marketH=chart.drawH*0.5,marketY=chart.yStart+(chart.drawH-marketH)/2+30;
  svg.appendChild(sankeyEl('rect',{x:chart.marketX,y:marketY,width:chart.marketW,height:marketH,rx:2,fill:'#b496fa'}));
  svg.appendChild(sankeyEl('text',{x:chart.marketX-10,y:marketY+marketH/2-4,'text-anchor':'end',fill:'#6b7280','font-family':'IBM Plex Mono, monospace','font-size':14,'font-weight':700},'MARKET'));
  let ySector=chart.yStart,yMarket=marketY;
  const stockLayouts=[];
  sectors.forEach(sec=>{
    sec.h=chart.drawH*(sec.weight/total);sec.y=ySector;sec.marketH=marketH*(sec.weight/total);sec.marketY=yMarket;
    ySector+=sec.h+gapSector;yMarket+=sec.marketH;
    sec.visibleStocks.forEach(stock=>stockLayouts.push({sec,stock}));
  });
  const stockDest=new Map();
  stockLayouts.forEach(({stock})=>{
    let dest=stockDest.get(stock.sym);
    if(!dest){dest={...stock,flows:[],flowWeight:0,destWeight:stock.weight};stockDest.set(stock.sym,dest);}
    dest.flows.push(stock);dest.flowWeight=Math.max(dest.flowWeight||0,stock.weight);
    if(stock.weight>dest.weight)Object.assign(dest,{pct:stock.pct,weight:stock.weight,destWeight:stock.weight,sector:stock.sector});
  });
  let stockY=chart.yStart-60;
  const stockNodes=[...stockDest.values()].sort((a,b)=>b.flowWeight-a.flowWeight);
  stockNodes.forEach(stock=>{stock.nodeH=Math.max(3,chart.drawH*(stock.destWeight/total)*1.6-6);stock.destY=stockY;stockY+=stock.nodeH+3;});
  sectors.forEach(sec=>svg.appendChild(sankeyEl('path',{d:sankeyPath(chart.marketX+chart.marketW,sec.marketY,sec.marketY+sec.marketH,chart.sectorX,sec.y,sec.y+sec.h),fill:sec.color,'fill-opacity':'0.48',stroke:'none'})));
  const sectorSourceY=new Map(sectors.map(sec=>[sec.name,sec.y]));
  stockLayouts.forEach(({sec,stock})=>{
    const dest=stockDest.get(stock.sym);if(!dest)return;
    const flowH=chart.drawH*(stock.weight/total),sourceY=sectorSourceY.get(sec.name)||sec.y;
    sectorSourceY.set(sec.name,sourceY+flowH);
    svg.appendChild(sankeyEl('path',{d:sankeyPath(chart.sectorX+chart.barW,sourceY,sourceY+flowH,chart.stockX,dest.destY,dest.destY+dest.nodeH),fill:sec.color,'fill-opacity':'0.62',stroke:'none'}));
  });
  stockNodes.forEach(stock=>{
    const h2=stock.nodeH,flows=stock.flows.length?stock.flows:[stock];
    let segY=stock.destY,flowTotal=flows.reduce((s,f)=>s+f.weight,0);
    flows.forEach((flow,idx)=>{
      const sec=sectors.find(s=>s.name===flow.sector),remaining=stock.destY+h2-segY;
      const segH=idx===flows.length-1?remaining:Math.max(1,h2*(flow.weight/flowTotal));
      svg.appendChild(sankeyEl('rect',{x:chart.stockX,y:segY,width:chart.barW,height:segH,rx:2,fill:sec?sec.color:'#94a3b8'}));
      segY+=segH;
    });
    if(h2>6){
      const b=sankeyBadgeColor(stock.pct),badgeX=chart.stockX+chart.barW+8,badgeY=stock.destY+h2/2-10,badgeW=152;
      const grp=sankeyEl('g',{'data-sym':stock.sym,style:'cursor:pointer'});
      grp.appendChild(sankeyEl('rect',{x:badgeX,y:badgeY,width:badgeW,height:20,rx:5,fill:b.fill}));
      grp.appendChild(sankeyEl('text',{x:badgeX+6,y:badgeY+14,fill:b.text,'font-family':'IBM Plex Mono, monospace','font-size':11,'font-weight':600},`${stock.sym} (${sankeyFmtNum(stock.weight)}, ${sankeyFmtPct(stock.pct)})`));
      svg.appendChild(grp);
    }
  });
  sectors.forEach(sec=>{
    svg.appendChild(sankeyEl('rect',{x:chart.sectorX,y:sec.y,width:chart.barW,height:sec.h,rx:2,fill:sec.color}));
    if(sec.h>16){
      svg.appendChild(sankeyEl('text',{x:chart.sectorX+chart.barW+8,y:sec.y+sec.h/2-2,fill:'#6b7280','font-family':'IBM Plex Mono, monospace','font-size':12,'font-weight':700},sec.name));
      svg.appendChild(sankeyEl('text',{x:chart.sectorX+chart.barW+8,y:sec.y+sec.h/2+14,fill:'#6b7280','font-family':'IBM Plex Mono, monospace','font-size':10},sankeyFmtNum(sec.weight)));
    }
  });
}
DOM.sankeySvg.addEventListener('click',e=>{
  const node=e.target.closest('[data-sym]');if(!node)return;
  const sym=node.dataset.sym;
  if(IS_MOBILE()){openChart(sym);return;}
  _hmapDesktopClick(sym);
});
DOM.sankeySvg.addEventListener('dblclick',e=>{
  const node=e.target.closest('[data-sym]');if(!node||IS_MOBILE())return;
  if(_hmapClickTimer)clearTimeout(_hmapClickTimer);
  const sym=node.dataset.sym;
  _syncHoverPreview(sym);
  updatePopout(sym);
  updateSimplize(sym);
  openChart(sym);
});
DOM.sankeyToggle.addEventListener('click',()=>{
  const collapsed=DOM.sankeyPanel.classList.toggle('collapsed');
  DOM.sankeyWrap.hidden=collapsed;
});
DOM.hmapToggle.addEventListener('click',e=>{
  // Giống CHART: các control trong header (nút MARKET/VNINDEX/FOLLOW/SZ, ô tìm mã, nút popout...)
  // vẫn phải bấm được bình thường — chỉ coi là "bấm để thu/mở" khi không trúng các control đó.
  if(e.target.closest('button,input,.hmap-search-wrap'))return;
  DOM.hmapPanel.classList.toggle('collapsed');
});
DOM.liteChartToggle.addEventListener('click',e=>{
  // Khi thẻ đang mở, các control bên trong thanh công cụ (tìm mã, D/W, chỉ báo, vẽ...)
  // vẫn phải bấm được bình thường — chỉ coi là "bấm vào thẻ để thu/mở" khi không bấm
  // trúng các vùng control đó. Khi thẻ đang thu gọn thì các vùng đó đã ẩn (display:none)
  // nên toàn bộ header luôn hoạt động như nút mở ra, giống hệt SANKEY.
  if(e.target.closest('.lite-chart-search-wrap,.lite-tf-tabs,.lite-indicators,.lite-draw-toolbar'))return;
  const collapsed=DOM.liteChartPanel.classList.toggle('collapsed');
  _isChartPanelOpen=!collapsed;
  if(_isChartPanelOpen){
    // Panel vừa được mở lại sau khi bị ẩn (display:none) — canvas của lightweight-charts
    // có thể đang mang kích thước 0 nên cần ép resize lại đúng như handler window 'resize',
    // đồng thời set lại visible logical range (số bar hiển thị + khoảng trống lề phải) như
    // mặc định ban đầu, tránh trường hợp nến bị dồn cụm vào một góc do resize sau khi range
    // đã được tính với width=0 lúc panel còn ẩn.
    requestAnimationFrame(()=>{
      if(_liteChart&&DOM.liteChart)_liteChart.applyOptions({width:DOM.liteChart.clientWidth,height:DOM.liteChart.clientHeight});
      if(_liteRsiChart&&DOM.liteRsiChart)_liteRsiChart.applyOptions({width:DOM.liteRsiChart.clientWidth,height:DOM.liteRsiChart.clientHeight});
      if(_liteMacdChart&&DOM.liteMacdChart)_liteMacdChart.applyOptions({width:DOM.liteMacdChart.clientWidth,height:DOM.liteMacdChart.clientHeight});
      if(_liteData.length)setLiteRightOffset();
      resizeLiteDrawCanvas();redrawLiteDrawings();
    });
  }
});
// ═══════════════════════════════════════════════════════
// CLOCK & CONFIG
// ═══════════════════════════════════════════════════════
function tick(){
  const n=new Date();
  DOM.clock.textContent=n.toLocaleTimeString('vi-VN',{hour12:false})+' '+n.toLocaleDateString('vi-VN');
}
setInterval(tick,1000);tick();
async function loadConfig(){
  try{const j=await fetch('/api/config').then(r=>r.json());SIG_TTL=j.signal_ttl_sec||30;HMAP_TTL=j.heatmap_ttl_sec||120;}catch(e){}
  DOM.footer.textContent=`Scanner Bot Dashboard • Tín hiệu tự động làm mới sau ${SIG_TTL}s • Heatmap tự động làm mới sau ${HMAP_TTL}s`;
}
// ═══════════════════════════════════════════════════════
// FETCH
// ═══════════════════════════════════════════════════════
async function fetchSigs(){
  try{
    const j=await fetch('/api/signals').then(r=>r.json());
    DOM.sigMeta.textContent=`Cập nhật ${j.updated_at} • ${j.count} tín hiệu • ${j.momentum_count||0} động lượng`;
    // Cache theo mã để chart CHART tra cứu (xem _liteApplyBuySignal) — không fetch thêm, dùng chung
    // đúng 1 lần gọi API này cho cả panel "Tín hiệu hôm nay" lẫn mũi tên trên chart.
    _sigTodayMap=new Map((j.signals||[]).map(s=>[s.symbol,s]));
    _liteApplyBuySignal();
    const momentum=j.momentum||[];
    if(!momentum.length){
      DOM.momentumList.innerHTML='';
    }else{
      DOM.momentumList.innerHTML=momentum.map(s=>{
        const pct=s.pct!=null?(s.pct>=0?'+':'')+Number(s.pct).toFixed(1)+'%':'—';
        const pctColor=s.pct==null?'#6b7280':s.pct>=0?'#0e9f6e':'#e02424';
        return `<div class="momentum-row" data-sym="${s.symbol}"><span class="s-sym">${s.symbol}</span><span class="s-type" style="color:${pctColor}">${pct}</span><span class="s-badge b-${s.signal}">${s.signal}</span></div>`;
      }).join('');
    }
    if(!j.signals.length){DOM.sigList.innerHTML='<div class="empty"><div class="big">💤</div><div>Chưa có tín hiệu nào hôm nay</div></div>';return;}
    DOM.sigList.innerHTML=j.signals.map(s=>`<div class="sig-row" data-sym="${s.symbol}"><span class="s-emoji">${s.emoji}</span><span class="s-sym">${s.symbol}</span><span class="s-type" style="color:${s.pct>=0?'#0e9f6e':'#e02424'}">${s.pct!=null?(s.pct>=0?'+':'')+Number(s.pct).toFixed(1)+'%':'—'}</span><span class="s-badge ${BADGE_MAP[s.signal]||'b-MACROSS'}">${signalLabel(s.signal)}</span></div>`).join('');
  }catch(e){console.error('fetchSigs:',e);}
}
async function fetchHmap(){
  try{
    const j=await fetch('/api/heatmap').then(r=>r.json());
    const now=new Date().toLocaleTimeString('vi-VN',{hour12:false});
    DOM.hmapTs.textContent=`Data: ${j.timestamp||'--'} • Cập nhật: ${now}`;
    window._lastHmapData=j.data||{};
    renderHeatmap(j.data||{});
    renderSankey(j.data||{});
    if(_hoverPreviewOn)_hvPatchSymList(j.data||{});
    if(_isPopoutMode&&_popoutWin&&!_popoutWin.closed)
      _popoutWin.postMessage({type:'UPDATE_HEATMAP',data:j.data||{}},'*');
  }catch(e){console.error('fetchHmap:',e);}
}
function startBar(elOrId,sec){
  const el=typeof elOrId==='string'?$(elOrId):elOrId;if(!el)return;
  el.style.transition='none';el.style.width='0%';
  requestAnimationFrame(()=>requestAnimationFrame(()=>{el.style.transition=`width ${sec}s linear`;el.style.width='100%';}));
}
// ═══════════════════════════════════════════════════════
// SEARCH helper
// ═══════════════════════════════════════════════════════
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
$('btn-market').addEventListener('click',()=>openUrl('https://dstock.vndirect.com.vn','MARKET'));
$('btn-vnindex').addEventListener('click',()=>openUrl('https://24hmoney.vn/indices/vn-index','VNINDEX'));
$('hmap-simplize-btn').addEventListener('click',function(){ quickSimplize(); this.blur(); });
$('hmap-popout-btn').addEventListener('click',function(){ quickPopout(); this.blur(); });
$('hover-preview-btn').addEventListener('click',()=>toggleHoverPreview());
$('journal-open-btn').addEventListener('click',()=>{
  if(DOM.journalFrame.src==='about:blank')DOM.journalFrame.src='/journal';
  DOM.journalOverlay.classList.add('on');
  document.body.style.overflow='hidden';
});
function closeJournal(){
  DOM.journalOverlay.classList.remove('on');
  if(!DOM.overlay.classList.contains('on')&&!DOM.lb.classList.contains('on'))document.body.style.overflow='';
}
DOM.journalOverlay.addEventListener('click',e=>{if(e.target===DOM.journalOverlay)closeJournal();});
// ═══════════════════════════════════════════════════════
// ALBUM
// ═══════════════════════════════════════════════════════
function _showAlbum(images){
  _albumImages=images;_albumTotal=images.length;_albumIdx=0;
  const mob=IS_MOBILE();
  DOM.albumSlides.innerHTML=images.map((img,i)=>`<div class="album-slide${i===0?' on':''}" data-idx="${i}"><img src="${img.url}" alt="${img.label}" loading="lazy" decoding="async"${mob?' data-lb="1"':''}></div>`).join('');
  DOM.albumDots.innerHTML=images.map((_,i)=>`<div class="album-dot${i===0?' on':''}" data-idx="${i}"></div>`).join('');
  _updateAlbumNav();
  DOM.albumOuter.style.display='flex';DOM.loading.style.display='none';
}
function _appendAlbumImages(images){
  if(!images.length)return;
  const mob=IS_MOBILE(),start=_albumImages.length;
  _albumImages=_albumImages.concat(images);_albumTotal=_albumImages.length;
  DOM.albumSlides.insertAdjacentHTML('beforeend',images.map((img,i)=>{const idx=start+i;return`<div class="album-slide" data-idx="${idx}"><img src="${img.url}" alt="${img.label}" loading="lazy" decoding="async"${mob?' data-lb="1"':''}></div>`;}).join(''));
  DOM.albumDots.insertAdjacentHTML('beforeend',images.map((_,i)=>`<div class="album-dot" data-idx="${start+i}"></div>`).join(''));
  _updateAlbumNav();
}
DOM.albumDots.addEventListener('click',e=>{const d=e.target.closest('.album-dot');if(d)albumGoto(+d.dataset.idx);});
DOM.albumSlides.addEventListener('click',e=>{
  const img=e.target.closest('img');
  if(img&&img.dataset.lb==='1'){const slide=img.closest('.album-slide');if(slide)lbOpen(_albumImages,+slide.dataset.idx);}
});
DOM.btnPrev.addEventListener('click',()=>albumNav(-1));
DOM.btnNext.addEventListener('click',()=>albumNav(1));
function albumGoto(i){
  if(i<0||i>=_albumTotal)return;
  DOM.albumSlides.querySelectorAll('.album-slide').forEach((s,idx)=>s.classList.toggle('on',idx===i));
  DOM.albumDots.querySelectorAll('.album-dot').forEach((d,idx)=>d.classList.toggle('on',idx===i));
  _albumIdx=i;_updateAlbumNav();
}
function albumNav(dir){albumGoto(_albumIdx+dir);}
function _updateAlbumNav(){
  DOM.btnPrev.classList.toggle('disabled',_albumIdx===0);
  DOM.btnNext.classList.toggle('disabled',_albumIdx===_albumTotal-1);
}
let _scanTouchX=0,_scanRaf=null;
$('panel-scanner').addEventListener('touchstart',e=>{_scanTouchX=e.touches[0].clientX;},{passive:true});
$('panel-scanner').addEventListener('touchend',e=>{
  if(_scanRaf)cancelAnimationFrame(_scanRaf);
  _scanRaf=requestAnimationFrame(()=>{const dx=e.changedTouches[0].clientX-_scanTouchX;if(Math.abs(dx)>50)albumNav(dx<0?1:-1);});
},{passive:true});
DOM.btnRef.addEventListener('click',async()=>{
  if(!_sym)return;
  DOM.btnRef.classList.add('spinning');DOM.btnRef.disabled=true;
  try{await fetch('/api/chart_cache_clear/'+_sym,{method:'DELETE'});}catch(e){}
  DOM.btnRef.classList.remove('spinning');DOM.btnRef.disabled=false;
  await loadScannerChart(_sym);
});
async function loadScannerChart(sym){
  DOM.albumOuter.style.display='none';DOM.loading.style.display='flex';
  DOM.loading.innerHTML=`<span>⏳ Đang tạo chart <b>${sym}</b>…</span>`;
  try{
    const r=await fetch('/api/chart_images/'+sym);
    if(!r.ok){const j=await r.json().catch(()=>({}));throw new Error(j.error||'HTTP '+r.status);}
    const j=await r.json();
    if(!j.images?.length)throw new Error('no_images');
    const labels=j.labels||['📊 Daily [D]','📈 Weekly [W]'];
    _showAlbum(j.images.map((b64,i)=>({url:'data:image/png;base64,'+b64,label:labels[i]||'Chart '+(i+1)})));
    const h=DOM.albumOuter.querySelector('.album-hint');
    if(h)h.textContent='Đang tải 15m...';
    loadScannerChart15m(sym);
  }catch(e){
    DOM.loading.innerHTML=`<div style="text-align:center;color:#aaa;padding:24px"><div style="font-size:24px;margin-bottom:10px">⚠️</div><div style="margin-bottom:8px">Không tải được chart <b style="color:#4d9ff5">${sym}</b></div><div style="font-size:11px;color:#666;margin-bottom:16px">${e.message}</div><div style="display:flex;gap:8px;justify-content:center;flex-wrap:wrap"><button onclick="loadScannerChart('${sym}')" style="padding:6px 14px;border-radius:5px;background:#1a56db;color:#fff;border:none;cursor:pointer;font-size:12px">🔄 Thử lại</button><a href="https://ta.vietstock.vn/?stockcode=${sym.toLowerCase()}" target="_blank" style="padding:6px 14px;border-radius:5px;background:#374151;color:#fff;text-decoration:none;font-size:12px">📈 Stockchart</a></div></div>`;
  }
}
async function loadScannerChart15m(sym){
  const s=(sym||'').toUpperCase().trim();
  try{
    const r=await fetch('/api/chart_image_15m/'+s);
    if(!r.ok){const h=DOM.albumOuter.querySelector('.album-hint');if(h)h.textContent='';return;}
    const j=await r.json();
    if((_sym||'').toUpperCase().trim()!==s)return;
    if(!j.images?.length)return;
    const labels=j.labels||['⚡ 15 phút [15m]'];
    _appendAlbumImages(j.images.map((b64,i)=>({url:'data:image/png;base64,'+b64,label:labels[i]||'15m'})));
    const h=DOM.albumOuter.querySelector('.album-hint');
    if(h)h.textContent='';
  }catch(e){
    const h=DOM.albumOuter.querySelector('.album-hint');
    if(h)h.textContent='';
  }
}
// ═══════════════════════════════════════════════════════
// POPUP — tab activation (dùng chung cho cả 3 header)
// ═══════════════════════════════════════════════════════
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
  if(tab==='scanner')loadScannerChart(_sym);
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
// ═══════════════════════════════════════════════════════
// POPUP OPEN / CLOSE
// ═══════════════════════════════════════════════════════
function _updateSymDisplay(sym){
  DOM.ptitle.textContent=sym;
  DOM.mobPtitle.textContent=sym;
  DOM.mobLandSym.textContent=sym;
}
function _resetScannerUI(){
  DOM.albumOuter.style.display='none';
  DOM.loading.style.display='flex';
  DOM.loading.innerHTML='<span>⏳ Đang tạo chart từ scanner...</span>';
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
function openChart(sym){
  _resetPopupChrome();
  _sym=sym.toUpperCase().trim();_tab='vs';
  _updateSymDisplay(_sym);
  DOM.ifVs.src='https://ta.vietstock.vn/?stockcode='+_sym.toLowerCase();
  ['vnd-cs','vnd-news','vnd-sum','24h','url'].forEach(t=>{const f=$('iframe-'+t);if(f)f.src='about:blank';});
  _resetScannerUI();
  _activateTab('vs');
  _openPopup();
  setTimeout(()=>DOM.pbox.focus(),0);
  // Clear search inputs
  DOM.popupSearch.value='';DOM.mobSearch.value='';DOM.mobLandSearch.value='';
}
function openUrl(url,label){
  _resetPopupChrome();
  _sym=label||'WEB';
  _updateSymDisplay(label||'🌐');
  ['vnd-cs','vnd-news','vnd-sum','24h'].forEach(t=>{const f=$('iframe-'+t);if(f)f.src='about:blank';});
  DOM.ifVs.src='https://ta.vietstock.vn/?stockcode=vnindex';
  $('iframe-url').src=url;
  _resetScannerUI();
  TABS_ALL.forEach(t=>{
    const p=document.getElementById('panel-'+t);if(p)p.classList.toggle('on',t==='url');
    DOM.popupCtabs.querySelectorAll('.ctab').forEach(b=>b.classList.toggle('on',b.dataset.tab==='url'));
  });
  _openPopup();
}
function closePopup(){
  const pbox=DOM.pbox;
  _resetPopupChrome();
  pbox.style.visibility='hidden';
  DOM.ifVs.src='about:blank';
  ['vnd-cs','vnd-news','vnd-sum','24h','url'].forEach(t=>{const f=$('iframe-'+t);if(f)f.src='about:blank';});
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
    if(!DOM.overlay.classList.contains('on')||DOM.lb.classList.contains('on'))return;
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
  if(DOM.lb.classList.contains('on'))return;
  if(e.key==='Escape'){if(DOM.overlay.classList.contains('on')){closePopup();return;}}
  if(e.key==='Escape'&&DOM.journalOverlay.classList.contains('on')){closeJournal();return;}
  if(!DOM.overlay.classList.contains('on'))return;
  const activeSearch=[DOM.popupSearch,DOM.mobSearch,DOM.mobLandSearch];
  if(activeSearch.includes(document.activeElement))return;
  if(_tab!=='scanner'||_albumTotal===0)return;
  if(e.key==='ArrowLeft'){e.preventDefault();albumNav(-1);}
  if(e.key==='ArrowRight'){e.preventDefault();albumNav(1);}
});
// ═══════════════════════════════════════════════════════
// LIGHTBOX
// ═══════════════════════════════════════════════════════
const lb=DOM.lb;
const lbS={
  idx:0,W:0,images:[],
  dragging:false,dragDir:'',dragDx:0,dragDy:0,dragStartX:0,dragStartY:0,stripOffset:0,
  scale:1,panX:0,panY:0,
  isPinching:false,pinchStartDist:0,pinchStartScale:1,pinchStartPanX:0,pinchStartPanY:0,
  isPanning:false,panStartX:0,panStartY:0,panStartPanX:0,panStartPanY:0,
  lastTapTime:0,lastTapX:0,lastTapY:0,hintShown:false,pending:false,
};
function _lbImg(){return DOM.lbStrip.querySelectorAll('.lb-slide img')[lbS.idx]||null;}
function _lbResetZoom(){lbS.scale=1;lbS.panX=0;lbS.panY=0;const img=_lbImg();if(img){img.classList.remove('zooming');img.style.transform='';}}
function _lbApplyZoom(){const img=_lbImg();if(!img)return;img.classList.add('zooming');img.style.transform=`translate(${lbS.panX}px,${lbS.panY}px) scale(${lbS.scale})`;}
function _lbClamp(){
  if(lbS.scale<=1){lbS.panX=0;lbS.panY=0;return;}
  const img=_lbImg();if(!img)return;
  const r=img.getBoundingClientRect();
  const mX=Math.max(0,(r.width-window.innerWidth)/2),mY=Math.max(0,(r.height-window.innerHeight)/2);
  lbS.panX=Math.max(-mX,Math.min(mX,lbS.panX));lbS.panY=Math.max(-mY,Math.min(mY,lbS.panY));
}
function lbOpen(images,idx){
  lbS.images=images;lbS.idx=idx;lbS.W=window.innerWidth;
  DOM.lbStrip.innerHTML=images.map(img=>`<div class="lb-slide"><img src="${img.url}" alt="${img.label}" loading="lazy" decoding="async" draggable="false"></div>`).join('');
  DOM.lbStrip.style.width=`${lbS.W*images.length}px`;
  lbS.scale=1;lbS.panX=0;lbS.panY=0;
  _lbSnap(idx,false);_lbMeta();
  lb.classList.add('on');document.body.style.overflow='hidden';
  if(!lbS.hintShown){lbS.hintShown=true;setTimeout(()=>{DOM.lbZoomHint.classList.add('show');setTimeout(()=>DOM.lbZoomHint.classList.remove('show'),2200);},600);}
}
function lbClose(){_lbResetZoom();lb.classList.remove('on');document.body.style.overflow='';}
$('mob-lightbox-close').addEventListener('click',lbClose);
function _lbSnap(idx,animate){
  lbS.scale=1;lbS.panX=0;lbS.panY=0;
  const prev=_lbImg();if(prev){prev.classList.remove('zooming');prev.style.transform='';}
  lbS.idx=Math.max(0,Math.min(idx,lbS.images.length-1));
  const tx=-lbS.idx*lbS.W;lbS.stripOffset=tx;
  DOM.lbStrip.classList.remove('snapping','dragging');
  if(animate){DOM.lbStrip.classList.add('snapping');DOM.lbStrip.style.transform=`translateX(${tx}px)`;setTimeout(()=>DOM.lbStrip.classList.remove('snapping'),350);}
  else DOM.lbStrip.style.transform=`translateX(${tx}px)`;
  _lbMeta();
}
function _lbMeta(){
  if(!lbS.images.length)return;
  DOM.lbLabel.textContent=lbS.images[lbS.idx].label;
  DOM.lbCounter.innerHTML=lbS.images.map((_,i)=>`<div class="mob-lb-dot${i===lbS.idx?' on':''}"></div>`).join('');
}
function _pd(t){const dx=t[0].clientX-t[1].clientX,dy=t[0].clientY-t[1].clientY;return Math.sqrt(dx*dx+dy*dy);}
function _lbTS(e){
  if(e.touches.length===2){e.preventDefault();lbS.isPinching=true;lbS.dragging=false;lbS.pinchStartDist=_pd(e.touches);lbS.pinchStartScale=lbS.scale;lbS.pinchStartPanX=lbS.panX;lbS.pinchStartPanY=lbS.panY;return;}
  if(e.touches.length!==1)return;
  const now=Date.now(),tx=e.touches[0].clientX,ty=e.touches[0].clientY;
  if(now-lbS.lastTapTime<300&&Math.hypot(tx-lbS.lastTapX,ty-lbS.lastTapY)<40){e.preventDefault();_lbDbl(tx,ty);lbS.lastTapTime=0;return;}
  lbS.lastTapTime=now;lbS.lastTapX=tx;lbS.lastTapY=ty;
  if(lbS.scale>1.05){lbS.isPanning=true;lbS.panStartX=tx;lbS.panStartY=ty;lbS.panStartPanX=lbS.panX;lbS.panStartPanY=lbS.panY;lbS.dragging=false;return;}
  lbS.dragging=true;lbS.isPanning=false;lbS.dragDir='';lbS.dragDx=0;lbS.dragDy=0;lbS.dragStartX=tx;lbS.dragStartY=ty;
}
function _lbTM(e){
  if(lbS.isPinching&&e.touches.length===2){
    e.preventDefault();if(lbS.pending)return;lbS.pending=true;
    const ratio=_pd(e.touches)/lbS.pinchStartDist;
    const ns=Math.min(4,Math.max(1,lbS.pinchStartScale*ratio));
    requestAnimationFrame(()=>{lbS.scale=ns;lbS.panX=lbS.pinchStartPanX;lbS.panY=lbS.pinchStartPanY;_lbClamp();_lbApplyZoom();lbS.pending=false;});
    return;
  }
  if(e.touches.length!==1)return;
  const tx=e.touches[0].clientX,ty=e.touches[0].clientY;
  if(lbS.isPanning){
    e.preventDefault();if(lbS.pending)return;lbS.pending=true;
    const nx=lbS.panStartPanX+(tx-lbS.panStartX),ny=lbS.panStartPanY+(ty-lbS.panStartY);
    requestAnimationFrame(()=>{lbS.panX=nx;lbS.panY=ny;_lbClamp();_lbApplyZoom();lbS.pending=false;});
    return;
  }
  if(!lbS.dragging)return;
  const dx=tx-lbS.dragStartX,dy=ty-lbS.dragStartY;
  if(!lbS.dragDir&&(Math.abs(dx)>6||Math.abs(dy)>6))lbS.dragDir=Math.abs(dy)>Math.abs(dx)?'v':'h';
  if(!lbS.dragDir)return;
  e.preventDefault();if(lbS.pending)return;lbS.pending=true;
  if(lbS.dragDir==='v'){
    const pull=Math.max(0,dy);lbS.dragDy=dy;
    requestAnimationFrame(()=>{lb.style.opacity=Math.max(0,1-pull/280);DOM.lbStrip.style.transform=`translateX(${lbS.stripOffset}px) translateY(${pull*.6}px) scale(${Math.max(.85,1-pull/900)})`;DOM.lbStrip.classList.add('dragging');lbS.pending=false;});
    return;
  }
  lbS.dragDx=dx;
  let offset=lbS.stripOffset+dx;
  const maxO=0,minO=-(lbS.images.length-1)*lbS.W;
  if(offset>maxO)offset=dx*.3;if(offset<minO)offset=minO+(offset-minO)*.3;
  requestAnimationFrame(()=>{DOM.lbStrip.classList.add('dragging');DOM.lbStrip.style.transform=`translateX(${offset}px)`;lbS.pending=false;});
}
function _lbTE(e){
  lbS.pending=false;
  if(lbS.isPinching){lbS.isPinching=false;if(lbS.scale<1.1)_lbResetZoom();else{const img=_lbImg();if(img)img.classList.remove('zooming');}return;}
  if(lbS.isPanning){lbS.isPanning=false;return;}
  if(!lbS.dragging)return;
  lbS.dragging=false;DOM.lbStrip.classList.remove('dragging');
  if(lbS.dragDir==='v'){
    const pull=Math.max(0,lbS.dragDy);
    if(pull>80){DOM.lbStrip.style.transition='transform .22s ease';lb.style.transition='opacity .22s ease';DOM.lbStrip.style.transform=`translateX(${lbS.stripOffset}px) translateY(100vh) scale(.9)`;lb.style.opacity='0';setTimeout(()=>{DOM.lbStrip.style.transition='';lb.style.transition='';lb.style.opacity='';lbClose();},230);}
    else{DOM.lbStrip.style.transition='transform .22s ease';lb.style.transition='opacity .15s ease';DOM.lbStrip.style.transform=`translateX(${lbS.stripOffset}px)`;lb.style.opacity='1';setTimeout(()=>{DOM.lbStrip.style.transition='';lb.style.transition='';},230);}
    lbS.dragDy=0;lbS.dragDir='';return;
  }
  const dx=lbS.dragDx,absX=Math.abs(dx),THR=lbS.W*.25;
  let next=lbS.idx;
  if(absX>THR&&dx<0)next=lbS.idx+1;else if(absX>THR&&dx>0)next=lbS.idx-1;
  else if(absX>80&&dx<0&&lbS.idx<lbS.images.length-1)next=lbS.idx+1;
  else if(absX>80&&dx>0&&lbS.idx>0)next=lbS.idx-1;
  _lbSnap(next,true);lbS.dragDx=0;lbS.dragDir='';
}
function _lbDbl(tapX,tapY){
  if(lbS.scale>1.05)_lbResetZoom();
  else{lbS.scale=2.5;lbS.panX=(window.innerWidth/2-tapX)*(lbS.scale-1);lbS.panY=(window.innerHeight/2-tapY)*(lbS.scale-1);_lbClamp();_lbApplyZoom();}
}
const lbVP=$('lb-viewport');
lbVP.addEventListener('touchstart',_lbTS,{passive:false});
lbVP.addEventListener('touchmove',_lbTM,{passive:false});
lbVP.addEventListener('touchend',_lbTE,{passive:false});
lbVP.addEventListener('touchcancel',()=>{lbS.isPinching=false;lbS.isPanning=false;lbS.dragging=false;lbS.pending=false;},{passive:true});
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&lb.classList.contains('on'))lbClose();});
// ═══════════════════════════════════════════════════════
// HOVER PREVIEW
// ═══════════════════════════════════════════════════════
const _hvGroups=[];
(function(){
  _hvGroups.push({name:'TRADING',syms:TS_POOL});
  _hvGroups.push({name:'VN30',syms:HMAP_COLS[0].groups[0].syms});
  HMAP_COLS.forEach(cd=>cd.groups.forEach(g=>{if(g.name!=='VN30')_hvGroups.push({name:g.name,syms:g.syms});}));
})();
function _hvBuildTabs(){
  DOM.hpGrouptabs.innerHTML=_hvGroups.map((g,i)=>`<button class="hv-gtab${i===_hvActiveGroup?' on':''}" data-idx="${i}">${g.name}</button>`).join('');
}
DOM.hpGrouptabs.addEventListener('click',e=>{const btn=e.target.closest('.hv-gtab');if(btn)_hvSelectGroup(+btn.dataset.idx);});
function _hvSelectGroup(idx){
  if(_hvActiveGroup===idx){_hvActiveGroup=-1;DOM.hpGrouptabs.querySelectorAll('.hv-gtab').forEach(b=>b.classList.remove('on'));DOM.hpSymlist.style.display='none';DOM.hpSortBtn.style.display='none';return;}
  _hvActiveGroup=idx;
  DOM.hpGrouptabs.querySelectorAll('.hv-gtab').forEach((b,i)=>b.classList.toggle('on',i===idx));
  DOM.hpSortBtn.style.display='';DOM.hpSymlist.style.display='';_hvRenderSymList();
}
function _hvGetSorted(){
  const g=_hvGroups[_hvActiveGroup];if(!g)return[];
  const d=window._lastHmapData||{};
  if(_hvSortAlpha)return[...g.syms].sort((a,b)=>a.localeCompare(b));
  return[...g.syms].sort((a,b)=>{const pa=d[a]?d[a].pct||0:-999,pb=d[b]?d[b].pct||0:-999;return pb-pa;});
}
DOM.hpSortBtn.addEventListener('click',()=>{_hvSortAlpha=!_hvSortAlpha;DOM.hpSortBtn.textContent=_hvSortAlpha?'%↕':'A↕Z';_hvRenderSymList();});
function _hvRenderSymList(){
  if(_hvActiveGroup===-1)return;
  const d=window._lastHmapData||{};
  DOM.hpSymlist.innerHTML=_hvGetSorted().map(sym=>{
    const entry=d[sym],pct=entry&&typeof entry.pct==='number'?entry.pct:null;
    const price=entry&&typeof entry.price==='number'?fmtP(entry.price):'—';
    const pctStr=pct!==null?(pct>=0?'+':'')+pct.toFixed(1)+'%':'—';
    const color=pct===null?'var(--muted)':pct>0?'var(--green)':pct<0?'var(--red)':'#b45309';
    return`<div class="hv-sym-item${sym===_hoverPreviewCurrent?' on':''}" data-sym="${sym}"><span class="hv-sym-name">${sym}</span><span class="hv-sym-pct" style="color:${color}">${pctStr}</span><span class="hv-sym-price">${price}</span></div>`;
  }).join('');
}
function _hvPatchSymList(newData){
  if(_hvActiveGroup===-1)return;
  DOM.hpSymlist.querySelectorAll('.hv-sym-item').forEach(el=>{
    const sym=el.dataset.sym,d=newData[sym];if(!d)return;
    const pct=typeof d.pct==='number'?d.pct:null;
    const pEl=el.querySelector('.hv-sym-pct'),prEl=el.querySelector('.hv-sym-price');
    if(pEl&&pct!==null){pEl.textContent=(pct>=0?'+':'')+pct.toFixed(1)+'%';pEl.style.color=pct>0?'var(--green)':pct<0?'var(--red)':'#b45309';}
    if(prEl&&typeof d.price==='number')prEl.textContent=fmtP(d.price);
  });
}
function _syncHoverPreview(sym,updateFrame=true){
  _hoverPreviewCurrent=sym;
  if(!_hoverPreviewOn)return;
  DOM.hpSymlist.querySelectorAll('.hv-sym-item').forEach(el=>el.classList.toggle('on',el.dataset.sym===sym));
  if(updateFrame)DOM.hpIframe.src='https://ta.vietstock.vn/?stockcode='+sym.toLowerCase();
}
DOM.hpSymlist.addEventListener('click',e=>{
  const item=e.target.closest('.hv-sym-item');if(!item)return;
  const sym=item.dataset.sym;if(sym===_hoverPreviewCurrent)return;
  _syncHoverPreview(sym);updatePopout(sym);updateSimplize(sym);
});
document.addEventListener('keydown',e=>{
  if(!_hoverPreviewOn||_hvActiveGroup===-1)return;
  if(DOM.overlay.classList.contains('on'))return;
  if(e.key!=='ArrowUp'&&e.key!=='ArrowDown')return;
  e.preventDefault();
  if(_keyThrottle)return;_keyThrottle=true;setTimeout(()=>{_keyThrottle=false;},60);
  const items=[...DOM.hpSymlist.children];if(!items.length)return;
  let cur=items.findIndex(el=>el.classList.contains('on'));
  let next=cur===-1?0:(e.key==='ArrowDown'?cur+1:cur-1);
  next=Math.max(0,Math.min(next,items.length-1));
  if(next===cur&&cur!==-1)return;
  const sym=items[next].dataset.sym;_syncHoverPreview(sym,false);
  if(_iframeDelay)clearTimeout(_iframeDelay);
  _iframeDelay=setTimeout(()=>{_syncHoverPreview(sym);updatePopout(sym);updateSimplize(sym);},300);
  const list=DOM.hpSymlist,el=items[next],relTop=el.offsetTop-list.offsetTop,h=el.offsetHeight;
  if(relTop-h<list.scrollTop)list.scrollTop=Math.max(0,relTop-h);
  else if(relTop+h*2>list.scrollTop+list.clientHeight)list.scrollTop=relTop+h*2-list.clientHeight;
});
function _closeHoverPanel(){
  _hoverPreviewOn=false;
  DOM.hpPanel.style.display='none';DOM.wrap.style.paddingBottom='';
  DOM.hpIframe.src='about:blank';_hoverPreviewCurrent='';
  if(_isPopoutMode){_isPopoutMode=false;if(_popoutWin&&!_popoutWin.closed)try{_popoutWin.close();}catch(e){}_popoutWin=null;}
  _refreshChartModeUI();
}
$('hv-close-btn').addEventListener('click',_closeHoverPanel);
$('hv-full-btn').addEventListener('click',()=>openChart(_hoverPreviewCurrent||'VNINDEX'));
$('hv-pop-btn').addEventListener('click',()=>popOutHover());
function toggleHoverPreview(){
  if(_isPopoutMode){minimizePopout();return;}
  if(_hoverPreviewOn){_closeHoverPanel();return;}
  _hoverPreviewOn=true;
  DOM.hpPanel.style.display='flex';_hvBuildTabs();
  DOM.wrap.style.paddingBottom=DOM.hpPanel.offsetHeight+16+'px';
  _hoverPreviewCurrent='VNINDEX';
  DOM.hpIframe.src='https://ta.vietstock.vn/?stockcode=vnindex';
  if(_hvActiveGroup===-1)_hvSelectGroup(0);else _hvRenderSymList();
  _refreshChartModeUI();
}
(function(){
  const resizer=$('hover-preview-resizer');let drag=false,startY=0,startH=0;
  resizer.addEventListener('mousedown',e=>{drag=true;startY=e.clientY;startH=DOM.hpPanel.offsetHeight;document.body.style.userSelect='none';document.body.style.cursor='ns-resize';e.preventDefault();});
  document.addEventListener('mousemove',e=>{if(!drag)return;const newH=Math.min(window.innerHeight*.9,Math.max(120,startH+(startY-e.clientY)));DOM.hpPanel.style.height=newH+'px';DOM.wrap.style.paddingBottom=newH+16+'px';});
  document.addEventListener('mouseup',()=>{if(!drag)return;drag=false;document.body.style.userSelect='';document.body.style.cursor='';});
})();
function quickPopout(){
  if(_isPopoutMode&&_popoutWin&&!_popoutWin.closed){_popoutWin.focus();return;}
  if(!_hoverPreviewOn){_hoverPreviewOn=true;_hvActiveGroup=0;}
  popOutHover();
}
// ═══════════════════════════════════════════════════════
// POPOUT WINDOW
// ═══════════════════════════════════════════════════════
function popOutHover(){
  const sym=_hoverPreviewCurrent||'VNINDEX';
  if(_isPopoutMode&&_popoutWin&&!_popoutWin.closed){_popoutWin.focus();return;}
  DOM.hpPanel.style.display='none';DOM.wrap.style.paddingBottom='';
  _isPopoutMode=true;_hoverPreviewOn=false;
  _refreshChartModeUI();
  const box=_getPopupViewport();
  const w=Math.min(1600,box.width-40),h=box.height;
  _popoutWin=_openMaximizedWindow('','ScannerPopout',w,h,0,0,'scrollbars=no');
  if(!_popoutWin){alert('Trình duyệt chặn popup!');minimizePopout();return;}
  _popoutWin.document.write(_buildPopoutHTML(sym));
  _popoutWin.document.close();
  const chk=setInterval(()=>{if(_popoutWin&&_popoutWin.closed){clearInterval(chk);if(_isPopoutMode)closePopoutWindow();}},1000);
}
function _buildPopoutHTML(initSym){
  const gJ=JSON.stringify(_hvGroups.map(g=>({name:g.name,syms:g.syms})));
  const dJ=JSON.stringify(window._lastHmapData||{});
  const ig=_hvActiveGroup>=0?_hvActiveGroup:0;
  return '<!DOCTYPE html><html><head>'
    +'<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">'
    +'<title>Chart \u2014 '+initSym+'</title>'
    +'<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600;700&family=Barlow+Condensed:wght@600;700;800&display=swap" rel="stylesheet">'
    +'<style>'
    +'*{margin:0;padding:0;box-sizing:border-box}'
    +':root{--accent:#1a56db;--bg:#f4f6fb;--surface:#fff;--surf2:#f0f3f9;--border:#dde3ee;--green:#0e9f6e;--red:#e02424;--text:#111827;--muted:#6b7280;--font-mono:\'IBM Plex Mono\',monospace;--font-ui:\'Barlow Condensed\',sans-serif}'
    +'body,html{height:100%;overflow:hidden;background:var(--bg);font-family:var(--font-mono);font-size:13px;color:var(--text)}'
    +'#hdr{display:flex;align-items:center;padding:0 10px;background:var(--surf2);height:42px;gap:6px;border-bottom:1px solid var(--border);flex-shrink:0}'
    +'#sym{font-family:var(--font-ui);font-size:18px;font-weight:800;letter-spacing:1.5px;color:var(--accent);flex-shrink:0;white-space:nowrap}'
    +'#sw{position:relative;flex-shrink:0}'
    +'#si-icon{position:absolute;left:7px;top:50%;transform:translateY(-50%);color:var(--muted);font-size:10px;pointer-events:none}'
    +'#si{width:85px;padding:4px 6px 4px 22px;border-radius:14px;border:1px solid var(--border);background:var(--surface);color:var(--text);font-family:var(--font-mono);font-size:10px;outline:none;transition:width .2s,border-color .15s}'
    +'#si::placeholder{color:var(--muted)}'
    +'#si:focus{width:130px;border-color:var(--accent)}'
    +'@media(max-width:768px){'
    +'  #si{width:68px !important; transition:border-color .15s !important}'
    +'  #si:focus{width:68px !important; box-shadow:0 0 0 2px rgba(26,86,219,.12)}'
    +'}'
    +'#gtabs{display:flex;overflow-x:auto;gap:2px;flex:1;min-width:0;scrollbar-width:none;-ms-overflow-style:none}'
    +'#gtabs::-webkit-scrollbar{display:none}'
    +'.gtab{height:28px;line-height:1;display:inline-flex;align-items:center;justify-content:center;padding:0 10px;border-radius:4px;border:1px solid var(--border);background:var(--bg);color:var(--muted);font-size:10px;font-weight:600;cursor:pointer;white-space:nowrap;transition:all .15s;flex-shrink:0;font-family:var(--font-mono)}'
    +'.gtab.on{background:var(--accent);color:#fff;border-color:var(--accent)}'
    +'.gtab:hover:not(.on){background:#eef3ff;color:var(--accent);border-color:var(--accent)}'
    +'#ctrls{display:flex;gap:3px;align-items:center;flex-shrink:0}'
    +'.ctrl{padding:0 10px;height:28px;border-radius:4px;border:1px solid var(--border);background:var(--surface);color:var(--muted);font-size:10px;font-weight:600;cursor:pointer;transition:all .15s;font-family:var(--font-mono);white-space:nowrap;display:inline-flex;align-items:center;justify-content:center}'
    +'.ctrl:hover{background:var(--accent);color:#fff;border-color:var(--accent)}'
    +'.ctrl.close:hover{background:var(--red);color:#fff;border-color:var(--red)}'
    +'#main{display:flex;height:calc(100% - 42px);overflow:hidden}'
    +'#symlist{width:120px;flex-shrink:0;overflow-y:auto;background:var(--bg);border-right:1px solid var(--border);scrollbar-width:thin;scrollbar-color:var(--border) transparent}'
    +'#symlist::-webkit-scrollbar{width:3px}'
    +'#symlist::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}'
    +'.si{display:grid;grid-template-columns:35px 30px 1fr;align-items:center;padding:5px 6px;cursor:pointer;border-bottom:1px solid rgba(0,0,0,.04);transition:background .12s;gap:2px}'
    +'.si:hover,.si.on{background:#dce8ff}'
    +'.si.on .sn{color:#0f3fb3;font-weight:800}'
    +'.sn{font-size:11px;font-weight:700}'
    +'.sp{font-size:10px;text-align:right;font-weight:700}'
    +'.spr{font-size:10px;text-align:right;color:#334155;font-weight:600}'
    +'.pos{color:var(--green)}.neg{color:var(--red)}.zer{color:#b45309}'
    +'#cw{flex:1;overflow:hidden;position:relative;background:#fff}'
    +'#cf{width:100%;height:100%;border:none;display:block}'
    +'#ld{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;background:var(--bg);color:var(--muted);font-size:13px;z-index:2;transition:opacity .3s}'
    +'#ld.hide{opacity:0;pointer-events:none}'
    +'</style></head><body>'
    +'<div id="hdr">'
    +'<span id="sym">'+initSym+'</span>'
    +'<div id="sw"><span id="si-icon">\uD83D\uDD0D</span>'
    +'<input id="si" type="text" placeholder="T\xECm m\xE3" maxlength="10" autocomplete="off" spellcheck="false"></div>'
    +'<div id="gtabs"></div>'
    +'<div id="ctrls">'
    +'<button class="ctrl" id="sort-btn">A↕Z</button>'
    +'<button class="ctrl" id="full-btn"> ⛶ </button>'
    +'<button class="ctrl" id="min-btn"> ❐ </button>'
    +'<button class="ctrl close" id="close-btn"> ✕ </button>'
    +'</div></div>'
    +'<div id="main">'
    +'<div id="symlist"></div>'
    +'<div id="cw"><div id="ld">Đang tải...</div><iframe id="cf" src="about:blank"></iframe></div>'
    +'</div>'
    +'<script>'
    +'"use strict";'
    +'var _$=function(id){return document.getElementById(id);};'
    +'var groups='+gJ+';'
    +'var hdata='+dJ+';'
    +'var ag='+ig+';'
    +'var sa='+(_hvSortAlpha?'true':'false')+';'
    +'var cur="'+initSym+'";'
    +'var full=false;'
    +'function fp(v){return(!v||v<=0)?"--":(v<100?Number(v).toFixed(2):Number(v).toFixed(1));}'
    +'function buildTabs(){'
    +'  var el=_$("gtabs");'
    +'  var html=groups.map(function(g,i){'
    +'    return \'<button class="gtab\'+(i===ag?\' on\':\'\')+\'" data-idx="\'+i+\'">\'+g.name+"</button>";'
    +'  }).join("");'
    +'  el.innerHTML=html;'
    +'}'
    +'function selGroup(idx){'
    +'  ag=idx;'
    +'  document.querySelectorAll(".gtab").forEach(function(b,i){b.classList.toggle("on",i===idx);});'
    +'  render();'
    +'}'
    +'function getSorted(){'
    +'  var g=groups[ag];if(!g)return [];'
    +'  if(sa)return g.syms.slice().sort(function(a,b){return a.localeCompare(b);});'
    +'  return g.syms.slice().sort(function(a,b){'
    +'    var pa=hdata[a]?hdata[a].pct||0:-999;'
    +'    var pb=hdata[b]?hdata[b].pct||0:-999;'
    +'    return pb-pa;'
    +'  });'
    +'}'
    +'function render(){'
    +'  var syms=getSorted();'
    +'  _$("symlist").innerHTML=syms.map(function(sym){'
    +'    var d=hdata[sym];'
    +'    var pct=d&&typeof d.pct==="number"?d.pct:null;'
    +'    var pctStr=pct!==null?((pct>=0?"+":"")+pct.toFixed(1)+"%"):"--";'
    +'    var cls=pct===null?"zer":pct>0?"pos":pct<0?"neg":"zer";'
    +'    return \'<div class="si\'+(sym===cur?\' on\':\'\')+\'" data-sym="\'+sym+\'"><span class="sn">\'+sym+"</span>"'
    +'      +\'<span class="sp \'+cls+\'">\'+pctStr+"</span>"'
    +'      +\'<span class="spr">\'+fp(d&&d.price)+"</span></div>";'
    +'  }).join("");'
    +'}'
    +'function patch(nd){'
    +'  hdata=nd;'
    +'  document.querySelectorAll(".si").forEach(function(el){'
    +'    var sym=el.dataset.sym,d=nd[sym];if(!d)return;'
    +'    var pct=typeof d.pct==="number"?d.pct:null;'
    +'    var sp=el.querySelector(".sp"),spr=el.querySelector(".spr");'
    +'    if(sp&&pct!==null){sp.textContent=(pct>=0?"+":"")+pct.toFixed(1)+"%";sp.className="sp "+(pct>0?"pos":pct<0?"neg":"zer");}'
    +'    if(spr&&typeof d.price==="number")spr.textContent=fp(d.price);'
    +'  });'
    +'}'
    +'function clickSym(sym){'
    +'  if(sym===cur)return;'
    +'  cur=sym;'
    +'  document.querySelectorAll(".si").forEach(function(e){e.classList.toggle("on",e.dataset.sym===sym);});'
    +'  setSym(sym);'
    +'  if(window.opener&&!window.opener.closed)window.opener.postMessage({type:"POPOUT_SYM_SELECT",symbol:sym},"*");'
    +'}'
    +'function setSym(sym){_$("sym").textContent=sym;document.title="Chart "+sym;loadChart(sym);}'
    +'function loadChart(sym){'
    +'  var cf=_$("cf"),ld=_$("ld");'
    +'  var url=full?(window.location.origin+"/popout_full/"+sym):("https://ta.vietstock.vn/?stockcode="+sym.toLowerCase());'
    +'  if(cf.src===url)return;'
    +'  ld.classList.remove("hide");'
    +'  cf.onload=function(){ld.classList.add("hide");};'
    +'  cf.src=url;'
    +'}'
    +'_$("gtabs").addEventListener("click",function(e){'
    +'  var b=e.target.closest(".gtab");if(!b)return;'
    +'  selGroup(parseInt(b.dataset.idx));'
    +'});'
    +'_$("symlist").addEventListener("click",function(e){'
    +'  var item=e.target.closest(".si");if(!item)return;'
    +'  clickSym(item.dataset.sym);'
    +'});'
    +'_$("sort-btn").addEventListener("click",function(){'
    +'  sa=!sa;'
    +'  this.textContent=sa?"%↕":"A↕Z";'
    +'  render();'
    +'});'
    +'_$("full-btn").addEventListener("click",function(){full=true;loadChart(cur);});'
    +'_$("min-btn").addEventListener("click",function(){'
    +'  if(window.opener&&!window.opener.closed)window.opener.postMessage({type:"POPOUT_MINIMIZE"},"*");'
    +'  window.close();'
    +'});'
    +'_$("close-btn").addEventListener("click",function(){'
    +'  if(window.opener&&!window.opener.closed)window.opener.postMessage({type:"POPOUT_CLOSE"},"*");'
    +'  window.close();'
    +'});'
    +'_$("si").addEventListener("keydown",function(e){'
    +'  if(e.key==="Enter"){'
    +'    var s=this.value.trim().toUpperCase();'
    +'    if(s.length>=2){this.value="";this.blur();cur=s;setSym(s);render();'
    +'      if(window.opener&&!window.opener.closed)window.opener.postMessage({type:"POPOUT_SYM_SELECT",symbol:s},"*");}'
    +'  }'
    +'  if(e.key==="Escape"){this.value="";this.blur();}'
    +'});'
    +'var _kt=false,_kd=null;'
    +'document.addEventListener("keydown",function(e){'
    +'  if(document.activeElement===_$("si"))return;'
    +'  if(e.key!=="ArrowUp"&&e.key!=="ArrowDown")return;'
    +'  e.preventDefault();'
    +'  if(_kt)return;_kt=true;setTimeout(function(){_kt=false;},60);'
    +'  var items=[].slice.call(_$("symlist").children);if(!items.length)return;'
    +'  var c=items.findIndex(function(el){return el.classList.contains("on");});'
    +'  var n=c===-1?0:(e.key==="ArrowDown"?c+1:c-1);'
    +'  n=Math.max(0,Math.min(n,items.length-1));'
    +'  if(n===c&&c!==-1)return;'
    +'  items.forEach(function(el){el.classList.remove("on");});items[n].classList.add("on");'
    +'  var sym=items[n].dataset.sym;cur=sym;_$("sym").textContent=sym;document.title="Chart "+sym;'
    +'  if(_kd)clearTimeout(_kd);'
    +'  _kd=setTimeout(function(){'
    +'    loadChart(sym);'
    +'    if(window.opener&&!window.opener.closed)window.opener.postMessage({type:"POPOUT_SYM_SELECT",symbol:sym},"*");'
    +'  },300);'
    +'  var list=_$("symlist"),el=items[n];'
    +'  var rt=el.offsetTop-list.offsetTop,h=el.offsetHeight;'
    +'  if(rt-h<list.scrollTop)list.scrollTop=Math.max(0,rt-h);'
    +'  else if(rt+h*2>list.scrollTop+list.clientHeight)list.scrollTop=rt+h*2-list.clientHeight;'
    +'});'
    +'window.addEventListener("message",function(e){'
    +'  if(e.data.type==="UPDATE_CHART"){cur=e.data.symbol;setSym(cur);render();}'
    +'  if(e.data.type==="UPDATE_HEATMAP"){patch(e.data.data||{});}'
    +'  if(e.data.type==="EMBEDDED_FULL_SYMBOL"){cur=(e.data.symbol||cur).toUpperCase();_$("sym").textContent=cur;render();}'
    +'  if(e.data.type==="EMBEDDED_FULL_CLOSE"){full=false;cur=(e.data.symbol||cur).toUpperCase();setSym(cur);render();}'
    +'});'
    +'buildTabs();render();setSym(cur);'
    +'<\/script></body></html>';
}

function minimizePopout(){
  _isPopoutMode=false;
  if(_popoutWin&&!_popoutWin.closed)try{_popoutWin.close();}catch(e){}
  _popoutWin=null;
  _hoverPreviewOn=true;
  DOM.hpPanel.style.display='flex';_hvBuildTabs();
  if(_hvActiveGroup>=0){
    DOM.hpGrouptabs.querySelectorAll('.hv-gtab').forEach((b,i)=>b.classList.toggle('on',i===_hvActiveGroup));
    DOM.hpSortBtn.style.display='';DOM.hpSymlist.style.display='';_hvRenderSymList();
  }else _hvSelectGroup(0);
  DOM.wrap.style.paddingBottom=DOM.hpPanel.offsetHeight+16+'px';
  if(_hoverPreviewCurrent)DOM.hpIframe.src='https://ta.vietstock.vn/?stockcode='+_hoverPreviewCurrent.toLowerCase();
  _refreshChartModeUI();
}
function closePopoutWindow(){
  _isPopoutMode=false;
  if(_popoutWin&&!_popoutWin.closed)try{_popoutWin.close();}catch(e){}
  _popoutWin=null;
  _refreshChartModeUI();
}
function updatePopout(sym){if(_popoutWin&&!_popoutWin.closed)_popoutWin.postMessage({type:'UPDATE_CHART',symbol:sym},'*');}

window.addEventListener('message',e=>{
  if(e.data.type==='POPOUT_SYM_SELECT'){
    _syncHoverPreview(e.data.symbol);
    updateSimplize(e.data.symbol);
  }else if(e.data.type==='JOURNAL_SYM_CLICK'&&e.data.symbol){
    const sym=String(e.data.symbol).toUpperCase().trim();
    if(!sym)return;
    _hmapDesktopClick(sym);
  }else if(e.data.type==='JOURNAL_SYM_DBLCLICK'&&e.data.symbol){
    const sym=String(e.data.symbol).toUpperCase().trim();
    if(!sym)return;
    if(_hoverPreviewOn)_syncHoverPreview(sym);
    else _syncHoverPreview(sym,false);
    updatePopout(sym);
    updateSimplize(sym);
    openChart(sym);
  }else if(e.data.type==='JOURNAL_CLOSE'){
    closeJournal();
  }else if(e.data.type==='POPOUT_MINIMIZE'){
    minimizePopout();
  }else if(e.data.type==='POPOUT_CLOSE'){
    closePopoutWindow();
  }
});

// ═══════════════════════════════════════════════════════
// INIT
// ═══════════════════════════════════════════════════════
async function init(){
  await loadConfig();
  _refreshChartModeUI();
  bindLiteChartControls();
  startBar(DOM.pbarSig,SIG_TTL);startBar(DOM.pbarHmap,HMAP_TTL);
  await Promise.all([fetchSigs(),fetchHmap()]);
  loadLiteChart(_liteSymbol);
  setInterval(async()=>{startBar(DOM.pbarSig,SIG_TTL);await fetchSigs();},SIG_TTL*1000);
  setInterval(async()=>{startBar(DOM.pbarHmap,HMAP_TTL);await fetchHmap();},HMAP_TTL*1000);
}
init();
</script>
</body>
</html>
"""
