"""
DASHBOARD SERVER
"""

from flask import Flask, jsonify, Response, request, session, send_from_directory
from functools import wraps
from pathlib import Path
import hmac
import json
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
_get_history_cache = None
_cache_lock = None
_fetch_heatmap_fn = None
_fetch_chart_fn = None
_signal_emoji = {}
_signal_rank = {}

_heatmap_cache = {"data": {}, "ts": "", "updated_at": 0}
_heatmap_lock = threading.Lock()
HEATMAP_TTL_SEC = 120
SIGNAL_TTL_SEC = 10

_chart_cache: dict = {}
_chart_lock = threading.Lock()
CHART_TTL_SEC = 0

JOURNAL_DATA_DIR = Path(os.environ.get("DASHBOARD_DATA_DIR", "/data/trade-journal")).expanduser()
JOURNAL_UPLOAD_DIR = JOURNAL_DATA_DIR / "uploads"
JOURNAL_DB_PATH = JOURNAL_DATA_DIR / "trade_journal.sqlite"
JOURNAL_WARNING_PATH = JOURNAL_DATA_DIR / "market_warning.txt"
JOURNAL_ALLOWED_EXT = {"png", "jpg", "jpeg", "webp", "gif"}
_journal_lock = threading.Lock()


def _now_vn_iso():
    return datetime.now(TZ_VN).strftime("%Y-%m-%d %H:%M:%S")


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
@app.route("/api/signals")
def api_signals():
    alerted = _get_alerted_today() if _get_alerted_today else {}
    result = []
    for sym, entry in alerted.items():
        sig = entry["signal"] if isinstance(entry, dict) else entry
        pct = entry.get("pct") if isinstance(entry, dict) else None
        emoji = _signal_emoji.get(sig, "📌")
        rank  = _signal_rank.get(sig, 0)
        result.append({"symbol": sym, "signal": sig, "emoji": emoji,
                        "rank": rank, "pct": pct})
    result.sort(key=lambda x: x["rank"], reverse=True)
    return jsonify({
        "signals": result,
        "count":   len(result),
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
        "data":      _heatmap_cache["data"],
        "timestamp": _heatmap_cache["ts"],
        "cached_age": int(now - snap_time),
    })

@app.route("/api/chart_images/<symbol>")
def api_chart_images(symbol):
    import base64
    symbol = symbol.upper().strip()
    now = time.time()
    with _chart_lock:
        cached = _chart_cache.get(symbol)
        if cached and (now - cached["updated_at"]) < CHART_TTL_SEC:
            return jsonify({"symbol": symbol, "images": cached["images"],
                            "labels": cached["labels"], "cached": True})
    if not _fetch_chart_fn:
        return jsonify({"error": "chart_fn_not_registered"}), 503
    try:
        ts = datetime.now(TZ_VN).strftime('%H:%M:%S')
        print(f"  [Dashboard] 📊 Tạo chart {symbol}...")
        png_list, labels = _fetch_chart_fn(symbol)
        if not png_list:
            return jsonify({"error": "no_data"}), 404
        b64_list = [base64.b64encode(b).decode() for b in png_list]
        fetch_time = time.time()
        with _chart_lock:
            _chart_cache[symbol] = {"images": b64_list, "labels": labels,
                                    "updated_at": fetch_time}
        print(f"  [Dashboard] ✅ Chart {symbol}: {len(b64_list)} ảnh ({ts}→{datetime.now(TZ_VN).strftime('%H:%M:%S')})")
        return jsonify({"symbol": symbol, "images": b64_list,
                        "labels": labels, "cached": False})
    except Exception as e:
        print(f"  [Dashboard] ❌ Chart {symbol} lỗi: {e}")
        return jsonify({"error": str(e)}), 500

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
        removed = symbol in _chart_cache
        _chart_cache.pop(symbol, None)
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

@app.route("/sankey")
def sankey_view():
    return Response(SANKEY_HTML, mimetype="text/html")

@app.route("/")
def index():
    return Response(DASHBOARD_HTML, mimetype="text/html")

# =============================================================================
# START
# =============================================================================
def start_dashboard(alerted_today_ref, history_cache_ref, cache_lock_ref,
                    fetch_heatmap_fn, signal_emoji_ref, signal_rank_ref,
                    fetch_chart_fn=None, port=8888):
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
.phdr-left{display:flex;align-items:center;gap:8px}
.phdr-center{display:flex;align-items:flex-end;justify-content:center}
.phdr-right{display:flex;align-items:center;justify-content:flex-end}
.ptitle{font-family:var(--font-ui);font-size:17px;font-weight:800;color:var(--accent);letter-spacing:1.4px;white-space:nowrap}
.search-wrap{position:relative;display:flex;align-items:center}
.s-icon{position:absolute;left:10px;top:50%;transform:translateY(-50%);color:var(--muted);font-size:12px;pointer-events:none}
.search-input{width:108px;padding:5px 10px 5px 28px;border-radius:20px;border:1px solid var(--border);background:var(--surface);color:var(--text);font-family:var(--font-mono);font-size:11px;outline:none;transition:border-color .15s,width .2s}
.search-input:focus{width:180px;border-color:var(--accent);box-shadow:0 0 0 2px rgba(26,86,219,.12)}
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
  .phdr-left,.phdr-center,.phdr-right{justify-content:center}
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
    const labels=j.labels||['📊 Daily [D]','📈 Weekly [W]','⚡ 15m'];
    _showAlbum(j.images.map((b64,i)=>({url:'data:image/png;base64,'+b64,label:labels[i]||'Chart '+(i+1)})));
    if(j.cached){const h=DOM.outer.querySelector('.album-hint');if(h)h.textContent='♻️ Dùng cache';}
  }catch(e){
    DOM.loading.innerHTML=`<div style="text-align:center;color:#aaa;padding:24px"><div style="font-size:24px;margin-bottom:10px">⚠️</div><div style="margin-bottom:8px">Không tải được chart <b style="color:#4d9ff5">${sym}</b></div><div style="font-size:11px;color:#666;margin-bottom:16px">${e.message}</div><div style="display:flex;gap:8px;justify-content:center"><button onclick="loadScannerChart('${sym}')" style="padding:6px 14px;border-radius:5px;background:#1a56db;color:#fff;border:none;cursor:pointer;font-size:12px">🔄 Thử lại</button><a href="https://ta.vietstock.vn/?stockcode=${sym.toLowerCase()}" target="_blank" style="padding:6px 14px;border-radius:5px;background:#374151;color:#fff;text-decoration:none;font-size:12px">📈 Stockchart</a></div></div>`;
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
<title>Nhật ký</title>
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
  <h1>★ Nhật ký</h1>
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
function render(){const box=$('list');if(!S.entries.length){box.innerHTML='<div class="empty">Chưa có nhật ký nào</div>';return;}box.innerHTML=S.entries.map(e=>`
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
$('list').addEventListener('click',async e=>{const imgDel=e.target.closest('[data-img]');if(imgDel){if(confirm('Xóa ảnh này?')){try{await deleteJournalImage(imgDel.dataset.img);}catch(err){alert('Không xóa ảnh được: '+err.message);}}return;}const symBtn=e.target.closest('[data-journal-sym]');if(symBtn){const sym=symBtn.dataset.journalSym;if(S.symTimer)clearTimeout(S.symTimer);S.symTimer=setTimeout(()=>postSym(sym,'JOURNAL_SYM_CLICK'),220);return;}const img=e.target.closest('img[data-entry]');if(img){openViewer(img.dataset.entry,Number(img.dataset.imgIdx)||0);return;}const edit=e.target.closest('[data-edit]');if(edit){const found=S.entries.find(x=>String(x.id)===String(edit.dataset.edit));if(found)showForm(found);return;}const del=e.target.closest('[data-del]');if(del&&confirm('Xóa nhật ký này?')){try{await api('/api/journal/entries/'+del.dataset.del,{method:'DELETE'});if(String(S.editingId||'')===String(del.dataset.del))hideForm();await loadEntries();}catch(err){alert('Không xóa được: '+err.message);}}});
$('list').addEventListener('dblclick',e=>{const symBtn=e.target.closest('[data-journal-sym]');if(!symBtn)return;if(S.symTimer)clearTimeout(S.symTimer);postSym(symBtn.dataset.journalSym,'JOURNAL_SYM_DBLCLICK');});
$('list').addEventListener('change',async e=>{const up=e.target.closest('[data-upload]');if(!up||!S.admin)return;try{await uploadImages(up.dataset.upload,up.files);up.value='';await loadEntries();}catch(err){alert('Không upload được: '+err.message);}});
$('f-symbol').addEventListener('input',()=>{clearTimeout(window._flt);window._flt=setTimeout(loadEntries,250);});
$('f-status').addEventListener('change',loadEntries);
$('viewer').addEventListener('click',e=>{if(e.target.id==='viewer'||e.target.id==='viewer-close')closeViewer();});
$('viewer-prev').addEventListener('click',e=>{e.stopPropagation();viewerNav(-1);});
$('viewer-next').addEventListener('click',e=>{e.stopPropagation();viewerNav(1);});
document.addEventListener('keydown',e=>{if(!$('viewer').classList.contains('on'))return;if(e.key==='Escape')closeViewer();else if(e.key==='ArrowLeft')viewerNav(-1);else if(e.key==='ArrowRight')viewerNav(1);});
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
  position:sticky;top:0;z-index:100;box-shadow:0 1px 6px var(--shadow);
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
.hmap-link-btn{display:inline-flex;align-items:center;padding:4px 11px;border-radius:5px;border:1px solid var(--border);background:var(--surface);color:var(--accent);font-family:var(--font-mono);font-size:10px;font-weight:600;cursor:pointer;text-decoration:none;white-space:nowrap;transition:all .15s}
.hmap-link-btn:hover{background:var(--accent);color:#fff;border-color:var(--accent)}
.hmap-link-btn.on{background:var(--accent);color:#fff;border-color:var(--accent)}
#hmap-simplize-btn{color:var(--muted)}
.hmap-search-wrap{position:relative;display:flex;align-items:center}
.hmap-search-wrap .s-icon{position:absolute;left:11px;top:50%;transform:translateY(-50%);color:var(--muted);font-size:13px;pointer-events:none}
.hmap-search-input{width:100px;padding:5px 10px 5px 30px;border-radius:20px;border:1px solid var(--border);background:var(--surface);color:var(--text);font-family:var(--font-mono);font-size:11px;outline:none;transition:border-color .15s,width .2s}
.hmap-search-input:focus{border-color:var(--accent);box-shadow:0 0 0 2px rgba(26,86,219,.12);width:120px}

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
.hmap-cell:hover{filter:brightness(.88);transform:scale(1.035);z-index:2;box-shadow:0 2px 8px rgba(0,0,0,.18)}
.hmap-cell>span{display:flex;align-items:center;justify-content:center;height:100%;overflow:hidden;white-space:nowrap;font-family:var(--font-mono)}
.hc-sym{font-size:10px}.hc-price{font-size:8.5px;opacity:.82}.hc-pct{font-size:9.5px}
.hmap-sector-group{width:130px;margin:26px auto 0}
.hmap-sector-cell{display:grid;grid-template-columns:1fr auto;align-items:center;height:24px;border-radius:4px;border:1px solid rgba(0,0,0,.1);padding:0 8px;gap:2px;overflow:hidden;transition:filter .12s}
.hmap-sector-cell:hover{filter:brightness(.9)}
.hsc-name{font-family:var(--font-ui);font-size:9px;text-transform:uppercase;letter-spacing:.3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hsc-pct{font-family:var(--font-mono);font-size:9px;text-align:right;flex-shrink:0}
.sankey-frame{width:calc(100% - 24px);aspect-ratio:16/9;height:auto;margin-left:24px;border:none;display:block;background:#fff}
.market-frame{width:100%;height:720px;border:none;display:block;background:#fff}

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
#hover-preview-btn:hover,#hover-preview-btn.on{background:var(--accent);color:#fff;border-color:var(--accent)}

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
  .sankey-frame{
    width:100% !important;
    margin-left:0 !important;
    aspect-ratio:16/9;
    height:auto;
  }
  .market-frame{height:70vh}
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
    <div class="panel-hdr">
      <div class="panel-hdr-left">
        <span class="panel-title">Tín hiệu hôm nay</span>
        <button class="journal-star-btn" id="journal-open-btn" title="Mở nhật ký mua">★</button>
      </div>
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
        <button class="hmap-link-btn" id="btn-market">MARKET</button>
        <button class="hmap-link-btn" id="btn-vnindex">VNINDEX</button>
        <div class="hmap-search-wrap">
          <span class="s-icon">🔍</span>
          <input class="hmap-search-input" id="hmap-search" type="text" placeholder="Tìm mã" maxlength="10" autocomplete="off" spellcheck="false">
        </div>
        <button id="hover-preview-btn">Chart: OFF</button>
        <button class="hmap-link-btn" id="hmap-popout-btn" style="color:var(--muted)">⧉</button>
        <button class="hmap-link-btn" id="hmap-simplize-btn">SZ</button>
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

  <!-- SANKEY -->
  <div class="panel">
    <div class="panel-hdr">
      <span class="panel-title">Sankey</span>
    </div>
    <iframe class="sankey-frame" id="sankey-frame" src="/sankey?embedded=1"></iframe>
  </div>

  <!-- MARKET -->
  <div class="panel">
    <div class="panel-hdr">
      <span class="panel-title">MARKET</span>
    </div>
    <iframe class="market-frame" id="market-frame" src="https://fireant.vn/dashboard" allowfullscreen></iframe>
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
  <div class="pbox" id="pbox">
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

<script>
'use strict';
// ═══════════════════════════════════════════════════════
// DOM CACHE
// ═══════════════════════════════════════════════════════
const $=id=>document.getElementById(id);
const DOM={
  clock:$('clock'),sigMeta:$('sig-meta'),sigList:$('sig-list'),
  hmapTs:$('hmap-ts'),hmapGrid:$('hmap-grid'),hmapSearch:$('hmap-search'),
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
const MOB_TABS=[
  ['vs','📈 Vietstock'],['scanner','🖼 Scanner'],['vnd-cs','⚖️ Cơ bản'],
  ['vnd-news','🗞️ Tin tức'],['vnd-sum','📄 Tổng quan'],['24h','💬 Fireant'],
];
let SIG_TTL=30,HMAP_TTL=120;
let _sym='',_tab='vs';
let _albumIdx=0,_albumTotal=0,_albumImages=[];
let _hoverPreviewOn=false,_hoverPreviewCurrent='';
let _hvActiveGroup=-1,_hvSortAlpha=false;
let _isPopoutMode=false,_popoutWin=null;
let _isSimplizeMode=false,_simplizeWin=null,_simplizeWatch=null;
let _iframeDelay=null,_keyThrottle=false;
const SIMPLIZE_ORIGIN='https://simplize.vn';
function simplizeUrl(sym){return `${SIMPLIZE_ORIGIN}/chart?ticker=${encodeURIComponent((sym||'VNINDEX').toUpperCase())}`;}
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
const HMAP_COLS=[
  {groups:[{name:"VN30",syms:["FPT","GAS","NVL","VNM","VCB","PLX","TCB","MWG","STB","HPG","PNJ","BID","CTG","HDB","VJC","VPB","KDH","MBB","VHM","POW","VRE","MSN","SSI","ACB","BVH","GVR","TPB"]}]},
  {groups:[{name:"NGAN HANG",syms:["VCB","BID","CTG","MBB","ACB","TCB","TPB","HDB","SHB","STB","VIB","VPB","MSB","ABB","BVB","LPB"]},{name:"DAU KHI",syms:["GAS","PVD","PVS","BSR","OIL","PVB","PVC","PLX","PET","PVT"]}]},
  {groups:[{name:"CHUNG KHOAN",syms:["SSI","VND","CTS","FTS","HCM","MBS","DSE","BSI","SHS","VCI","VCK","ORS"]},{name:"XAY DUNG",syms:["C47","C32","L14","CII","CTD","CTI","FCN","HBC","HUT","LCG","PC1","DPG","PHC","VCG"]}]},
  {groups:[{name:"BAT DONG SAN",syms:["VHM","AGG","IJC","LDG","CEO","D2D","DIG","DXG","HDC","HDG","KDH","NLG","NTL","NVL","PDR","SCR","TIG","KBC","SZC"]},{name:"PHAN BON",syms:["BFC","DCM","DPM"]},{name:"THEP",syms:["HPG","HSG","NKG"]}]},
  {groups:[{name:"BAN LE",syms:["MSN","FPT","FRT","MWG","PNJ","DGW"]},{name:"THUY SAN",syms:["ANV","FMC","CMX","VHC","IDI"]},{name:"CANG BIEN",syms:["HAH","GMD","SGP","VSC"]},{name:"CAO SU",syms:["GVR","DPR","DRI","PHR","DRC"]},{name:"NHUA",syms:["AAA","BMP","NTP"]}]},
  {groups:[{name:"DIEN NUOC",syms:["NT2","PC1","GEG","GEX","POW","TDM","BWE"]},{name:"DET MAY",syms:["TCM","TNG","VGT","MSH"]},{name:"HANG KHONG",syms:["NCT","ACV","AST","HVN","SCS","VJC"]},{name:"BAO HIEM",syms:["BMI","MIG","BVH"]},{name:"MIA DUONG",syms:["LSS","SBT","QNS"]}]},
  {groups:[{name:"DAU TU CONG",syms:["FCN","HHV","LCG","VCG","C4G","CTD","HBC","HSG","NKG","HPG","KSB","PLC"]}]},
];
const TS_POOL=["AAA","ACB","AGG","ANV","BFC","BID","BMI","BSR","BVB","BVH","BWE","CII","CKG","CRE","CTD","CTG","CTI","CTR","CTS","D2D","DBC","DCM","DSE","DGW","DIG","DPG","DPM","DRC","DRH","DXG","FCN","FMC","FPT","FRT","FTS","GAS","GEG","GEX","GMD","GVR","HAG","HAX","HBC","HCM","HDB","HDC","VCK","HDG","HNG","HPG","HSG","HTN","HVN","IDC","IJC","KBC","KDH","KSB","LCG","LDG","LPB","LTG","MBB","MBS","MSB","MSN","MWG","NKG","NLG","NTL","NVL","PC1","PDR","PET","PHR","PLC","PLX","PNJ","POW","PTB","PVD","PVS","PVT","QNS","REE","SBT","SCR","SHB","SHS","SSI","STB","SZC","TCB","TDM","TIG","TNG","TPB","TV2","VCB","VCI","VCS","VGT","VHC","VHM","VIB","VIC","VJC","VNM","VPB","VRE"];
// ═══════════════════════════════════════════════════════
// HEATMAP RENDER
// ═══════════════════════════════════════════════════════
function cellStyle(pct){
  let r,g,b;
  if(pct>=6.5){r=250;g=170;b=225}else if(pct>=4){r=160;g=220;b=170}
  else if(pct>=2){r=195;g=235;b=200}else if(pct>0){r=225;g=245;b=228}
  else if(pct===0){r=245;g=245;b=200}else if(pct>=-2){r=255;g=220;b=210}
  else if(pct>=-4){r=250;g=185;b=175}else if(pct>=-6.5){r=240;g=150;b=145}
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
  return`<div class="hmap-group hmap-sector-group"><div class="hmap-ghdr"><span class="hmap-gname">NGANH NGHE</span></div>${groups.slice(0,10).map(g=>{const{bg,fg}=cellStyle(g.avg),sign=g.avg>=0?'+':'';return`<div class="hmap-sector-cell" style="background:${bg};color:${fg}"><span class="hsc-name">${g.name}</span><span class="hsc-pct">${sign}${g.avg.toFixed(1)}%</span></div>`;}).join('')}</div>`;
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
  openChart(cell.dataset.sym);
});
let _hmapClickTimer=null;
function _hmapDesktopClick(sym){
  if(_hmapClickTimer)clearTimeout(_hmapClickTimer);
  _hmapClickTimer=setTimeout(()=>{
    _syncHoverPreview(sym);
    updatePopout(sym);
    updateSimplize(sym);
    if(_isPopoutMode)return;
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
  openChart(row.dataset.sym);
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
    DOM.sigMeta.textContent=`Cập nhật ${j.updated_at} • ${j.count} tín hiệu`;
    if(!j.signals.length){DOM.sigList.innerHTML='<div class="empty"><div class="big">💤</div><div>Chưa có tín hiệu nào hôm nay</div></div>';return;}
    DOM.sigList.innerHTML=j.signals.map(s=>`<div class="sig-row" data-sym="${s.symbol}"><span class="s-emoji">${s.emoji}</span><span class="s-sym">${s.symbol}</span><span class="s-type" style="color:${s.pct>=0?'#0e9f6e':'#e02424'}">${s.pct!=null?(s.pct>=0?'+':'')+Number(s.pct).toFixed(1)+'%':'—'}</span><span class="s-badge ${BADGE_MAP[s.signal]||'b-MACROSS'}">${s.signal.replace('POCKET PIVOT','PIVOT').replace('PRE-BREAK','PRE')}</span></div>`).join('');
  }catch(e){console.error('fetchSigs:',e);}
}
async function fetchHmap(){
  try{
    const j=await fetch('/api/heatmap').then(r=>r.json());
    const now=new Date().toLocaleTimeString('vi-VN',{hour12:false});
    DOM.hmapTs.textContent=`Data: ${j.timestamp||'--'} • Cập nhật: ${now}`;
    window._lastHmapData=j.data||{};
    renderHeatmap(j.data||{});
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
  DOM.loading.innerHTML=`<span>⏳ Đang tạo chart <b>${sym}</b>… (5–10 giây)</span>`;
  try{
    const r=await fetch('/api/chart_images/'+sym);
    if(!r.ok){const j=await r.json().catch(()=>({}));throw new Error(j.error||'HTTP '+r.status);}
    const j=await r.json();
    if(!j.images?.length)throw new Error('no_images');
    const labels=j.labels||['📊 Daily [D]','📈 Weekly [W]','⚡ 15m'];
    _showAlbum(j.images.map((b64,i)=>({url:'data:image/png;base64,'+b64,label:labels[i]||'Chart '+(i+1)})));
    if(j.cached){const h=DOM.albumOuter.querySelector('.album-hint');if(h)h.textContent='♻️ Dùng cache';}
  }catch(e){
    DOM.loading.innerHTML=`<div style="text-align:center;color:#aaa;padding:24px"><div style="font-size:24px;margin-bottom:10px">⚠️</div><div style="margin-bottom:8px">Không tải được chart <b style="color:#4d9ff5">${sym}</b></div><div style="font-size:11px;color:#666;margin-bottom:16px">${e.message}</div><div style="display:flex;gap:8px;justify-content:center;flex-wrap:wrap"><button onclick="loadScannerChart('${sym}')" style="padding:6px 14px;border-radius:5px;background:#1a56db;color:#fff;border:none;cursor:pointer;font-size:12px">🔄 Thử lại</button><a href="https://ta.vietstock.vn/?stockcode=${sym.toLowerCase()}" target="_blank" style="padding:6px 14px;border-radius:5px;background:#374151;color:#fff;text-decoration:none;font-size:12px">📈 Stockchart</a></div></div>`;
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
  if(e.key==='Escape'&&DOM.journalOverlay.classList.contains('on')){closeJournal();return;}
  if(e.key==='Escape'){if(DOM.overlay.classList.contains('on')){closePopup();return;}}
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
  }else if(e.data.type==='SANKEY_SYM_SELECT'&&e.data.symbol){
    if(_hoverPreviewOn)_syncHoverPreview(e.data.symbol);
    else _syncHoverPreview(e.data.symbol,false);
    updatePopout(e.data.symbol);
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
  }else if(e.data.type==='SANKEY_CLOSE'){
    closePopup();
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
  startBar(DOM.pbarSig,SIG_TTL);startBar(DOM.pbarHmap,HMAP_TTL);
  await Promise.all([fetchSigs(),fetchHmap()]);
  setInterval(async()=>{startBar(DOM.pbarSig,SIG_TTL);await fetchSigs();},SIG_TTL*1000);
  setInterval(async()=>{startBar(DOM.pbarHmap,HMAP_TTL);await fetchHmap();},HMAP_TTL*1000);
}
init();
</script>
</body>
</html>
"""

SANKEY_HTML = r"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Sankey Heatmap</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600;700&family=Barlow+Condensed:wght@600;700;800&display=swap" rel="stylesheet">
<script>
try{
  if(new URLSearchParams(window.location.search).get('embedded')==='1')
    document.documentElement.classList.add('embedded-sankey');
}catch(e){}
</script>
<style>
:root{--bg:#f4f6fb;--surface:#fff;--surf2:#f0f3f9;--border:#dde3ee;--accent:#1a56db;--text:#111827;--muted:#6b7280;--green:#0e9f6e;--red:#e02424;--yellow:#b45309;--font-mono:'IBM Plex Mono',monospace;--font-ui:'Barlow Condensed',sans-serif}
*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%}
body{background:var(--surface);color:var(--text);font-family:var(--font-mono);font-size:12px;overflow:hidden}
.page{height:100vh;display:flex;flex-direction:column}
.hdr{display:flex;align-items:center;gap:10px;padding:8px 14px;background:var(--surf2);border-bottom:1px solid var(--border)}
html.embedded-sankey .hdr{display:none}
.title{font-family:var(--font-ui);font-size:18px;font-weight:800;letter-spacing:1.4px;color:var(--accent);white-space:nowrap}
.hdr-spacer{flex:1}
.btn{height:30px;padding:0 12px;border-radius:5px;border:1px solid var(--border);background:var(--surface);color:var(--muted);font-family:var(--font-mono);font-size:11px;font-weight:600;cursor:pointer}
.btn:hover{background:var(--accent);color:#fff;border-color:var(--accent)}
.btn-close{width:30px;padding:0;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;font-size:15px}
.btn-close:hover{background:var(--red);color:#fff;border-color:var(--red)}
.main{flex:1;min-height:0;display:flex;flex-direction:column}
#wrap{flex:1;min-height:0;padding:0}
#svg{width:100%;height:100%;display:block;background:var(--surface);border:none;border-radius:0}
.empty{display:flex;align-items:center;justify-content:center;height:100%;color:var(--muted);font-size:13px}
@media(max-width:900px){
  .hdr{flex-wrap:nowrap}
}
</style>
</head>
<body>
<div class="page">
  <div class="hdr">
    <span class="title">Sankey Diagram</span>
    <span class="hdr-spacer"></span>
    <button class="btn btn-close" id="btn-close">✕</button>
  </div>
  <div class="main">
    <div id="wrap"><svg id="svg" viewBox="0 0 1600 900" preserveAspectRatio="xMidYMid meet"></svg></div>
  </div>
</div>
<script>
'use strict';
const $=id=>document.getElementById(id);
const HMAP_COLS=[
  {groups:[{name:"VN30",syms:["FPT","GAS","NVL","VNM","VCB","PLX","TCB","MWG","STB","HPG","PNJ","BID","CTG","HDB","VJC","VPB","KDH","MBB","VHM","POW","VRE","MSN","SSI","ACB","BVH","GVR","TPB"]}]},
  {groups:[{name:"NGAN HANG",syms:["VCB","BID","CTG","MBB","ACB","TCB","TPB","HDB","SHB","STB","VIB","VPB","MSB","ABB","BVB","LPB"]},{name:"DAU KHI",syms:["GAS","PVD","PVS","BSR","OIL","PVB","PVC","PLX","PET","PVT"]}]},
  {groups:[{name:"CHUNG KHOAN",syms:["SSI","VND","CTS","FTS","HCM","MBS","DSE","BSI","SHS","VCI","VCK","ORS"]},{name:"XAY DUNG",syms:["C47","C32","L14","CII","CTD","CTI","FCN","HBC","HUT","LCG","PC1","DPG","PHC","VCG"]}]},
  {groups:[{name:"BAT DONG SAN",syms:["VHM","AGG","IJC","LDG","CEO","D2D","DIG","DXG","HDC","HDG","KDH","NLG","NTL","NVL","PDR","SCR","TIG","KBC","SZC"]},{name:"PHAN BON",syms:["BFC","DCM","DPM"]},{name:"THEP",syms:["HPG","HSG","NKG"]}]},
  {groups:[{name:"BAN LE",syms:["MSN","FPT","FRT","MWG","PNJ","DGW"]},{name:"THUY SAN",syms:["ANV","FMC","CMX","VHC","IDI"]},{name:"CANG BIEN",syms:["HAH","GMD","SGP","VSC"]},{name:"CAO SU",syms:["GVR","DPR","DRI","PHR","DRC"]},{name:"NHUA",syms:["AAA","BMP","NTP"]}]},
  {groups:[{name:"DIEN NUOC",syms:["NT2","PC1","GEG","GEX","POW","TDM","BWE"]},{name:"DET MAY",syms:["TCM","TNG","VGT","MSH"]},{name:"HANG KHONG",syms:["NCT","ACV","AST","HVN","SCS","VJC"]},{name:"BAO HIEM",syms:["BMI","MIG","BVH"]},{name:"MIA DUONG",syms:["LSS","SBT","QNS"]}]},
  {groups:[{name:"DAU TU CONG",syms:["FCN","HHV","LCG","VCG","C4G","CTD","HBC","HSG","NKG","HPG","KSB","PLC"]}]},
];
const SECTOR_ORDER=[];
HMAP_COLS.forEach(col=>col.groups.forEach(g=>{if(g.name!=="VN30")SECTOR_ORDER.push(g);}));
const SVG_NS='http://www.w3.org/2000/svg';
const COLORS=['#ec8784','#a378e0','#da9672','#d5cc71','#72dacd','#a1e078','#7882e0','#e0b478','#78e0b4','#e078c8','#96c8fa','#b5d67a'];
const MIN_WEIGHT=10000000;
let _ttlMs=120000;
let _timer=null;

function fmtNum(v){
  if(!Number.isFinite(v)||v<=0)return '--';
  return (v/1e9).toFixed(1)+'B';
}
function fmtPct(v){
  if(!Number.isFinite(v))return '--';
  return (v>=0?'+':'')+v.toFixed(2)+'%';
}
function badgeColor(pct){
  if(pct>0)return {fill:'#0e9f6e',text:'#fff'};
  if(pct<0)return {fill:'#e02424',text:'#fff'};
  return {fill:'#d4a017',text:'#fff'};
}
function resolveWeight(entry){
  if(!entry||typeof entry!=='object')return 0;
  const totalValue=Number(entry.total_value);
  return Number.isFinite(totalValue)&&totalValue>0 ? totalValue : 0;
}
function ribbonPath(x1,y1t,y1b,x2,y2t,y2b){
  const c1=x1+(x2-x1)*0.45;
  const c2=x1+(x2-x1)*0.55;
  return `M ${x1} ${y1t} C ${c1} ${y1t}, ${c2} ${y2t}, ${x2} ${y2t} L ${x2} ${y2b} C ${c2} ${y2b}, ${c1} ${y1b}, ${x1} ${y1b} Z`;
}
function makeEl(tag,attrs={},text=''){
  const el=document.createElementNS(SVG_NS,tag);
  Object.entries(attrs).forEach(([k,v])=>el.setAttribute(k,String(v)));
  if(text)el.textContent=text;
  return el;
}
function sectorLimit(rank){
  if(rank<=1)return 10;
  if(rank<=4)return 6;
  if(rank<=7)return 4;
  if(rank<=11)return 2;
  return 1;
}
function notifyHost(sym){
  const payload={type:'SANKEY_SYM_SELECT',symbol:sym};
  try{
    if(window.self!==window.top)window.parent.postMessage(payload,'*');
    if(window.opener&&!window.opener.closed)window.opener.postMessage(payload,'*');
  }catch(e){}
}
function notifyClose(){
  try{
    if(window.self!==window.top)window.parent.postMessage({type:'SANKEY_CLOSE'},'*');
    else window.close();
  }catch(e){}
}
function buildDataset(data){
  const sectors=SECTOR_ORDER.map(g=>{
    const stocks=g.syms.map(sym=>{
      const entry=data[sym];
      const weight=resolveWeight(entry);
      return {
        id:`${g.name}::${sym}`,
        sym,entry,
        pct:Number(entry?.pct),
        price:Number(entry?.price),
        weight,
        sector:g.name,
      };
    }).filter(x=>x.entry&&x.weight>MIN_WEIGHT);
    return {name:g.name,stocks,weight:stocks.reduce((sum,s)=>sum+s.weight,0)};
  }).filter(sec=>sec.weight>0);
  sectors.sort((a,b)=>b.weight-a.weight);
  sectors.forEach((sec,idx)=>{
    sec.rank=idx;
    sec.limit=sectorLimit(idx);
    sec.color=COLORS[idx%COLORS.length];
  });
  const globalStocks=sectors.flatMap(sec=>sec.stocks).sort((a,b)=>b.weight-a.weight);
  sectors.forEach(sec=>{
    let drawn=0;
    sec.visibleStocks=[];
    for(const stock of globalStocks){
      if(stock.sector!==sec.name)continue;
      sec.visibleStocks.push(stock);
      drawn+=1;
      if(drawn>=sec.limit)break;
    }
  });
  return {
    sectors,
    globalStocks,
    total:sectors.reduce((sum,sec)=>sum+sec.weight,0),
  };
}
function render(data,ts){
  const svg=$('svg');
  svg.innerHTML='';
  const dataset=buildDataset(data);
  const sectors=dataset.sectors;
  if(!sectors.length||dataset.total<=0){
    const fo=document.createElementNS(SVG_NS,'foreignObject');
    fo.setAttribute('x','0'); fo.setAttribute('y','0'); fo.setAttribute('width','1600'); fo.setAttribute('height','900');
    const div=document.createElement('div');
    div.className='empty';
    div.textContent='Chưa có dữ liệu heatmap để dựng Sankey';
    fo.appendChild(div);
    svg.appendChild(fo);
    return;
  }
  const total=dataset.total;
  const chart={w:1600,h:900,yStart:120,drawH:540,marketX:130,sectorX:555,stockX:1285,marketW:6,barW:10};
  const gapSector=5;
  const marketH=chart.drawH*0.5;
  const marketY=chart.yStart+(chart.drawH-marketH)/2+30;
  const marketRect=makeEl('rect',{x:chart.marketX,y:marketY,width:chart.marketW,height:marketH,rx:2,fill:'#b496fa'});
  svg.appendChild(marketRect);
  svg.appendChild(makeEl('text',{x:chart.marketX-10,y:marketY+marketH/2-4,'text-anchor':'end',fill:'#6b7280','font-family':'IBM Plex Mono, monospace','font-size':'14','font-weight':'700'},'MARKET'));
  let ySector=chart.yStart;
  let yMarket=marketY;
  const stockLayouts=[];
  sectors.forEach(sec=>{
    sec.h = chart.drawH*(sec.weight/total);
    sec.y = ySector;
    sec.marketH = marketH*(sec.weight/total);
    sec.marketY = yMarket;
    ySector += sec.h + gapSector;
    yMarket += sec.marketH;
    sec.visibleStocks.forEach(stock=>{
      stockLayouts.push({sec,stock});
    });
  });
  const stockDest=new Map();
  stockLayouts.forEach(({stock})=>{
    let dest=stockDest.get(stock.sym);
    if(!dest){
      dest={...stock,flows:[],flowWeight:0,destWeight:stock.weight};
      stockDest.set(stock.sym,dest);
    }
    dest.flows.push(stock);
    dest.flowWeight+=stock.weight;
    if(stock.weight>dest.weight){
      Object.assign(dest,{entry:stock.entry,pct:stock.pct,price:stock.price,weight:stock.weight,destWeight:stock.weight,sector:stock.sector});
    }
  });
  let stockY=chart.yStart-60, stockGap=3;
  const stockNodes=[...stockDest.values()].sort((a,b)=>b.flowWeight-a.flowWeight);
  stockNodes.forEach(stock=>{
    stock.nodeH=Math.max(6,chart.drawH*(stock.destWeight/total)*1.6-6);
    stock.destY=stockY;
    stockY+=stock.nodeH+stockGap;
  });
  sectors.forEach(sec=>{
    svg.appendChild(makeEl('path',{d:ribbonPath(chart.marketX+chart.marketW,sec.marketY,sec.marketY+sec.marketH,chart.sectorX,sec.y,sec.y+sec.h),fill:sec.color,'fill-opacity':'0.48',stroke:'none'}));
  });
  const sectorSourceY=new Map(sectors.map(sec=>[sec.name,sec.y]));
  stockLayouts.forEach(({sec,stock})=>{
    const dest=stockDest.get(stock.sym);
    if(!dest)return;
    const flowH=chart.drawH*(stock.weight/total);
    const sourceY=sectorSourceY.get(sec.name)||sec.y;
    sectorSourceY.set(sec.name,sourceY+flowH);
    svg.appendChild(makeEl('path',{d:ribbonPath(chart.sectorX+chart.barW,sourceY,sourceY+flowH,chart.stockX,dest.destY,dest.destY+dest.nodeH),fill:sec.color,'fill-opacity':'0.62',stroke:'none'}));
  });
  stockNodes.forEach(stock=>{
    const h2=stock.nodeH, flows=stock.flows.length?stock.flows:[stock];
    let segY=stock.destY;
    flows.forEach((flow,idx)=>{
      const sec=sectors.find(s=>s.name===flow.sector);
      const remaining=stock.destY+h2-segY;
      const segH=idx===flows.length-1 ? remaining : Math.max(1,h2*(flow.weight/stock.flowWeight));
      svg.appendChild(makeEl('rect',{x:chart.stockX,y:segY,width:chart.barW,height:segH,rx:2,fill:sec?sec.color:'#94a3b8'}));
      segY+=segH;
    });
    if(h2>6){
      const b=badgeColor(stock.pct);
      const badgeX=chart.stockX+chart.barW+8;
      const badgeY=stock.destY+h2/2-10;
      const badgeW=152;
      const grp=makeEl('g',{'data-sym':stock.sym,style:'cursor:pointer'});
      grp.appendChild(makeEl('rect',{x:badgeX,y:badgeY,width:badgeW,height:20,rx:5,fill:b.fill}));
      const label=`${stock.sym} (${fmtNum(stock.weight)}, ${fmtPct(stock.pct)})`;
      grp.appendChild(makeEl('text',{x:badgeX+6,y:badgeY+14,fill:b.text,'font-family':'IBM Plex Mono, monospace','font-size':'11','font-weight':'600'},label));
      svg.appendChild(grp);
    }
  });
  sectors.forEach(sec=>{
    svg.appendChild(makeEl('rect',{x:chart.sectorX,y:sec.y,width:chart.barW,height:sec.h,rx:2,fill:sec.color}));
    if(sec.h>16){
      svg.appendChild(makeEl('text',{x:chart.sectorX+chart.barW+8,y:sec.y+sec.h/2-2,fill:'#6b7280','font-family':'IBM Plex Mono, monospace','font-size':'12','font-weight':'700'},sec.name));
      svg.appendChild(makeEl('text',{x:chart.sectorX+chart.barW+8,y:sec.y+sec.h/2+14,fill:'#6b7280','font-family':'IBM Plex Mono, monospace','font-size':'10'},fmtNum(sec.weight)));
    }
  });
}
async function fetchConfig(){
  try{
    const cfg=await fetch('/api/config').then(r=>r.json());
    _ttlMs=(Number(cfg.heatmap_ttl_sec)||120)*1000;
  }catch(e){}
}
async function fetchAndRender(){
  try{
    const j=await fetch('/api/heatmap').then(r=>r.json());
    render(j.data||{},j.timestamp||'');
  }catch(e){}
}
function startAuto(){
  if(_timer)clearInterval(_timer);
  _timer=setInterval(fetchAndRender,_ttlMs);
}
$('btn-close').addEventListener('click',notifyClose);
$('svg').addEventListener('click',e=>{
  const target=e.target.closest('[data-sym]');
  if(!target)return;
  notifyHost(target.dataset.sym);
});
(async function init(){
  await fetchConfig();
  await fetchAndRender();
  startAuto();
})();
</script>
</body>
</html>
"""
