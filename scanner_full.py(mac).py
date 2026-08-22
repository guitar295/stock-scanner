"""
=============================================================================
SCANNER TÍN HIỆU MUA CỔ PHIẾU — PHIÊN BẢN TỐI ƯU (CACHE + QUOTE length=2)
Pocket Pivot / Breakout / Pre-Break / BottomFish / BottomBreakP / MA_Cross
Tích hợp: vnstock + Telegram + Chart mplfinance + Chống spam + Nghỉ ngoài giờ
+ HEATMAP BOT (lệnh /h hoặc /heatmap)
+ CHỈ SỐ: VNINDEX, VN30 (lệnh /VNINDEX, /VN30, /c VNINDEX ...)
+ CHART 15 PHÚT: gửi kèm tín hiệu, on-demand, và khi nhấn nút /s
+ PHÂN QUYỀN: VIP (toàn quyền) / Free (tối đa 20 slot đồng thời, TTL 30 phút)
+ DASHBOARD WEB: http://VPS_IP:8888 — Tín hiệu + Heatmap + Scanner Chart
=============================================================================
"""

# =============================================================================
# BƯỚC 0: CÀI ĐẶT THƯ VIỆN (chạy 1 lần nếu chưa có)
# =============================================================================
#!pip install -U vnstock pandas requests mplfinance pytz pillow flask

# =============================================================================
# BƯỚC 1: IMPORT
# =============================================================================
from vnstock import register_user, Listing, Quote, Trading
import pandas as pd
import numpy as np
import requests
import time
import mplfinance as mpf
import sys
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from concurrent.futures import ProcessPoolExecutor
import logging
import os
import re
import tempfile
from io import BytesIO
from datetime import datetime, date
import pytz
import json
import threading
import math
from PIL import Image, ImageDraw, ImageFont
from dashboard_server import (
    start_dashboard,
    get_active_price_alert_rules,
    record_price_alert_event,
    warm_market_health_cache,
    invalidate_rs_cache,
    warm_rs_cache,
    TS_POOL_CONFIG,
    HMAP_COLS_CONFIG,
)

logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)

# =============================================================================
# BƯỚC 2: CẤU HÌNH
# =============================================================================
VNSTOCK_API         = os.environ.get('VNSTOCK_API')
TELEGRAM_BOT_TOKEN  = os.environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID    = os.environ.get('TELEGRAM_CHAT_ID')
MY_PERSONAL_CHAT_ID = os.environ.get('MY_PERSONAL_CHAT_ID')

DATA_SOURCE        = 'KBS'
SCAN_INTERVAL_SEC  = 120
CACHE_CHECK_INTERVAL_SEC = 1800   # nhịp tự dò/sửa history_cache NGOÀI giờ giao dịch — độc lập với SCAN_INTERVAL_SEC
TZ_VN              = pytz.timezone('Asia/Ho_Chi_Minh')

sys.setswitchinterval(0.001)  # nhả GIL thường xuyên hơn (mặc định 5ms), giảm độ trễ chart trên dashboard khi scanner đang tính toán

register_user(VNSTOCK_API)

# =============================================================================
# BƯỚC 2A: PHÂN QUYỀN VIP / FREE SLOT
# =============================================================================
VIP_CHAT_IDS = {
    str(TELEGRAM_CHAT_ID),
    str(MY_PERSONAL_CHAT_ID),
    '1207484510',
}

FREE_CHAT_LIMIT = 10
SESSION_TTL     = 1800
free_sessions: dict = {}
free_lock = threading.Lock()

def is_vip(chat_id: str) -> bool:
    return chat_id in VIP_CHAT_IDS

def is_allowed(chat_id: str) -> tuple[bool, str]:
    if is_vip(chat_id):
        return True, 'vip'
    now = time.time()
    with free_lock:
        expired = [k for k, v in free_sessions.items() if now - v > SESSION_TTL]
        for k in expired:
            del free_sessions[k]
            print(f"  🔄 Free slot hết hạn: {k} → giải phóng ({len(free_sessions)}/{FREE_CHAT_LIMIT})")
        if chat_id in free_sessions:
            free_sessions[chat_id] = now
            return True, 'free_existing'
        if len(free_sessions) < FREE_CHAT_LIMIT:
            free_sessions[chat_id] = now
            print(f"  ✅ Free slot mới: {chat_id} ({len(free_sessions)}/{FREE_CHAT_LIMIT})")
            return True, 'free_new'
        print(f"  🚫 Free slot đầy: {chat_id} bị từ chối ({FREE_CHAT_LIMIT}/{FREE_CHAT_LIMIT})")
        return False, 'full'

# =============================================================================
# BƯỚC 2B: DANH SÁCH CHỈ SỐ HỖ TRỢ
# =============================================================================
INDEX_SYMBOL_MAP = {
    'VNINDEX':   'VNINDEX',
    'VN30':      'VN30',
    'HNX':       'HNX',
    'HNXINDEX':  'HNXINDEX',
    'UPCOM':     'UPCOM',
    'VN100':     'VN100',
    'VN30F1M':   'VN30F1M',
}
INDEX_SYMBOLS = set(INDEX_SYMBOL_MAP.keys())

# =============================================================================
# BƯỚC 2C: CẤU HÌNH HEATMAP
# =============================================================================
TRADING_STOCKS_POOL = TS_POOL_CONFIG

HEATMAP_COLUMNS = [
    {"col": idx + 1, "groups": [{"name": g["name"], "symbols": g["syms"]} for g in col["groups"]]}
    for idx, col in enumerate(HMAP_COLS_CONFIG)
]

_HEATMAP_NEED_SYMBOLS = list(
    {s for col in HEATMAP_COLUMNS for g in col["groups"] for s in g["symbols"]}
    | set(TRADING_STOCKS_POOL)
)

HMAP_POS_COLORS = [
    (235,248,238), (231,247,234), (225,245,228), (220,243,224),
    (215,242,220), (205,238,211), (195,235,200), (186,232,193),
    (178,228,186), (169,224,178), (160,220,170), (154,218,165),
    (148,216,160),
]
HMAP_NEG_COLORS = [
    (255,232,225), (255,224,216), (255,220,210), (254,212,204),
    (253,205,197), (252,195,186), (250,185,175), (248,176,167),
    (246,168,160), (244,163,156), (243,158,152), (242,154,149),
    (240,150,145),
]
HMAP_CEIL_COLOR = (250,170,225)
HMAP_REF_COLOR  = (245,245,200)
HMAP_FLOOR_COLOR = (175,250,255)

def _hmap_cell_color(pct):
    if pct >= 6.5:
        return HMAP_CEIL_COLOR
    if pct >= 0.05:
        return HMAP_POS_COLORS[min(len(HMAP_POS_COLORS) - 1, math.floor(pct * 2))]
    if pct > -0.05:
        return HMAP_REF_COLOR
    if pct >= -6.5:
        return HMAP_NEG_COLORS[min(len(HMAP_NEG_COLORS) - 1, math.floor(abs(pct) * 2))]
    return HMAP_FLOOR_COLOR

def _hmap_fg(bg):
    lum = 0.299*bg[0] + 0.587*bg[1] + 0.114*bg[2]
    return (30,30,30) if lum > 160 else (15,15,15)

HMAP_CELL_W      = 162
HMAP_CELL_H      = 26
HMAP_COL_GAP     = 4
HMAP_COL_W       = HMAP_CELL_W + HMAP_COL_GAP
HMAP_MARGIN      = 5
HMAP_TOP_BAR     = 32
HMAP_RADIUS      = 5
HMAP_BG          = (252,252,252)
HMAP_HDR_FILL    = (220,228,250)
HMAP_HDR_OUTLINE = (160,180,230)
HMAP_HDR_FG      = (25,55,150)
HMAP_SECTOR_FG_P = (30,140,40)
HMAP_SECTOR_FG_N = (190,30,30)
HMAP_SECTOR_FG_0 = (120,120,30)

def _hmap_rounded_rect(draw, x0, y0, x1, y1, r, fill, outline=None, lw=1):
    draw.rectangle([x0+r, y0, x1-r, y1], fill=fill)
    draw.rectangle([x0, y0+r, x1, y1-r], fill=fill)
    draw.pieslice([x0, y0, x0+2*r, y0+2*r], 180, 270, fill=fill)
    draw.pieslice([x1-2*r, y0, x1, y0+2*r], 270, 360, fill=fill)
    draw.pieslice([x0, y1-2*r, x0+2*r, y1], 90,  180, fill=fill)
    draw.pieslice([x1-2*r, y1-2*r, x1, y1], 0,    90, fill=fill)
    if outline:
        draw.arc([x0, y0, x0+2*r, y0+2*r], 180, 270, fill=outline, width=lw)
        draw.arc([x1-2*r, y0, x1, y0+2*r], 270, 360, fill=outline, width=lw)
        draw.arc([x0, y1-2*r, x0+2*r, y1], 90,  180, fill=outline, width=lw)
        draw.arc([x1-2*r, y1-2*r, x1, y1], 0,    90, fill=outline, width=lw)
        draw.line([x0+r, y0, x1-r, y0], fill=outline, width=lw)
        draw.line([x0+r, y1, x1-r, y1], fill=outline, width=lw)
        draw.line([x0, y0+r, x0, y1-r], fill=outline, width=lw)
        draw.line([x1, y0+r, x1, y1-r], fill=outline, width=lw)

def _hmap_load_fonts():
    # Load DejaVu Sans từ matplotlib hoặc font hệ thống
    try:
        _mpl_font_dir = os.path.join(matplotlib.get_data_path(), "fonts", "ttf")
    except Exception:
        _mpl_font_dir = ""
    bold_paths = [
        os.path.join(_mpl_font_dir, "DejaVuSans-Bold.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
    ]
    reg_paths = [
        os.path.join(_mpl_font_dir, "DejaVuSans.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]
    bold = next((p for p in bold_paths if os.path.exists(p)), None)
    reg  = next((p for p in reg_paths  if os.path.exists(p)), None)
    try:
        f_title  = ImageFont.truetype(bold, 13)
        f_hdr    = ImageFont.truetype(bold, 10)
        f_sym    = ImageFont.truetype(bold, 10)
        f_data   = ImageFont.truetype(reg or bold, 9)
        f_sector = ImageFont.truetype(bold, 11)
        return f_title, f_hdr, f_sym, f_data, f_sector
    except Exception:
        d = ImageFont.load_default()
        return d, d, d, d, d

def _hmap_draw_stock_cell(draw, x, y, sym, price, pct, f_sym, f_data):
    bg = _hmap_cell_color(pct)
    fg = _hmap_fg(bg)
    x1, y1 = x + HMAP_CELL_W - 1, y + HMAP_CELL_H - 2
    _hmap_rounded_rect(draw, x, y, x1, y1, HMAP_RADIUS, fill=bg, outline=(200,205,215), lw=1)
    w1 = int(HMAP_CELL_W * 0.35)
    w2 = int(HMAP_CELL_W * 0.30)
    w3 = HMAP_CELL_W - w1 - w2
    ty = y + (HMAP_CELL_H - 2) // 2 - 5

    def dc(txt, fnt, bx, bw):
        bb = draw.textbbox((0, 0), txt, font=fnt)
        draw.text((bx + (bw - (bb[2] - bb[0])) // 2, ty), txt, font=fnt, fill=fg)

    dc(sym,                                                    f_sym,  x,       w1)
    dc(f"{price:,.2f}" if price < 100 else f"{price:,.0f}",   f_data, x + w1,  w2)
    dc(f"{pct:+.1f}%",                                        f_data, x+w1+w2, w3)

def _hmap_draw_group_header(draw, x, y, name, avg_pct, f_hdr, f_sector):
    x1, y1 = x + HMAP_CELL_W - 1, y + HMAP_CELL_H - 2
    _hmap_rounded_rect(draw, x, y, x1, y1, HMAP_RADIUS,
                       fill=HMAP_HDR_FILL, outline=HMAP_HDR_OUTLINE, lw=1)
    w1 = int(HMAP_CELL_W * 0.65)
    w2 = HMAP_CELL_W - w1
    ty = y + (HMAP_CELL_H - 2) // 2 - 5

    def dc(txt, fnt, bx, bw, color):
        bb = draw.textbbox((0, 0), txt, font=fnt)
        draw.text((bx + (bw - (bb[2] - bb[0])) // 2, ty), txt, font=fnt, fill=color)

    dc(name, f_hdr, x, w1, HMAP_HDR_FG)
    fg_s = HMAP_SECTOR_FG_P if avg_pct > 0 else (HMAP_SECTOR_FG_N if avg_pct < 0 else HMAP_SECTOR_FG_0)
    dc(f"{avg_pct:+.1f}%", f_sector, x + w1, w2, fg_s)

def _hmap_avg_pct(syms, data):
    vals = [data[s]["pct"] for s in syms if s in data]
    return round(sum(vals) / len(vals), 1) if vals else 0.0

def _finite_num(value, default=0.0):
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    return num if math.isfinite(num) else default

def _hmap_col_height(groups):
    h = HMAP_TOP_BAR + HMAP_MARGIN
    for g in groups:
        h += (1 + len(g["symbols"])) * HMAP_CELL_H
    return h + HMAP_MARGIN

def fetch_heatmap_data() -> tuple:
    engine = Trading(source=DATA_SOURCE)
    need   = _HEATMAP_NEED_SYMBOLS
    ts_log = datetime.now(TZ_VN).strftime('%H:%M:%S')
    print(f"  [{ts_log}] 🗺  Heatmap: tải {len(need)} mã...")
    result    = {}
    data_time = None
    try:
        df = engine.price_board(need)
        if df is not None and not df.empty:
            time_col = next(
                (c for c in df.columns
                 if c.lower() in ('time', 'trading_date', 'date', 'timestamp', 'last_time')),
                None
            )
            if time_col:
                raw_times = df[time_col].dropna()
                if not raw_times.empty:
                    val = raw_times.iloc[-1]
                    try:
                        val_num = float(val)
                        if val_num > 1_000_000_000_000:
                            data_time = datetime.fromtimestamp(val_num / 1000, tz=TZ_VN)
                        elif val_num > 1_000_000_000:
                            data_time = datetime.fromtimestamp(val_num, tz=TZ_VN)
                        else:
                            data_time = None
                    except (TypeError, ValueError, OSError):
                        data_time = None

            for _, row in df.iterrows():
                sym   = str(row.get("symbol", "")).strip()
                if not sym: continue
                close = _finite_num(row.get("close_price", 0)) / 1000
                ref_p = _finite_num(row.get("reference_price", 0)) / 1000
                total_value = _finite_num(row.get("total_value", 0))
                if close <= 0 and ref_p > 0:
                    close = ref_p
                pct   = round((close - ref_p) / ref_p * 100, 2) if ref_p > 0 else 0.0
                if not math.isfinite(pct):
                    pct = 0.0
                result[sym] = {"price": close, "pct": pct, "total_value": total_value}

    except Exception as e:
        print(f"  [{ts_log}] ❌ Heatmap API lỗi: {e}")

    if data_time is None:
        data_time = datetime.now(TZ_VN)

    ts_str = data_time.strftime("%H:%M  %d/%m/%Y")
    return result, ts_str


def fetch_extra_quotes(syms: list) -> dict:
    """Bù giá on-demand cho MÃ LẺ không nằm trong _HEATMAP_NEED_SYMBOLS (ví dụ mã người dùng
    tự thêm vào FAVORITE trên sidebar CHART, ngoài mọi danh sách quét chung). Dùng lại đúng
    engine + công thức tính pct với fetch_heatmap_data() để 2 nguồn giá luôn khớp nhau, nhưng
    CHỈ tải đúng danh sách mã được truyền vào (không đụng _HEATMAP_NEED_SYMBOLS/HEATMAP_TTL,
    không ảnh hưởng cache heatmap chính) — được gọi từ dashboard_server.api_quote_extra()
    qua start_dashboard(extra_quote_fn=fetch_extra_quotes)."""
    syms = [s for s in dict.fromkeys(s.strip().upper() for s in syms) if s]
    if not syms:
        return {}
    engine = Trading(source=DATA_SOURCE)
    result = {}
    try:
        df = engine.price_board(syms)
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                sym = str(row.get("symbol", "")).strip()
                if not sym:
                    continue
                close = _finite_num(row.get("close_price", 0)) / 1000
                ref_p = _finite_num(row.get("reference_price", 0)) / 1000
                if close <= 0 and ref_p > 0:
                    close = ref_p
                pct = round((close - ref_p) / ref_p * 100, 2) if ref_p > 0 else 0.0
                if not math.isfinite(pct):
                    pct = 0.0
                result[sym] = {"price": close, "pct": pct}
    except Exception as e:
        print(f"  ❌ fetch_extra_quotes lỗi: {e}")
    return result


def build_heatmap_image(data: dict, timestamp: str) -> str:
    f_title, f_hdr, f_sym, f_data, f_sector = _hmap_load_fonts()

    max_rows   = max(sum(len(g["symbols"]) for g in c["groups"]) for c in HEATMAP_COLUMNS)
    ts_display = sorted(
        [s for s in TRADING_STOCKS_POOL if s in data],
        key=lambda s: data[s]["pct"], reverse=True
    )[:max_rows]

    def srt(syms):
        return sorted(syms, key=lambda s: data.get(s, {}).get("pct", 0), reverse=True)

    col0     = {"col": 0, "groups": [{"name": "TRADING STOCKS", "symbols": ts_display}]}
    all_cols = [col0] + HEATMAP_COLUMNS

    all_sorted = []
    for cd in all_cols:
        all_sorted.append([{"name": g["name"], "symbols": srt(g["symbols"])} for g in cd["groups"]])

    IMG_W = len(all_cols) * HMAP_COL_W + HMAP_MARGIN * 2
    IMG_H = max(_hmap_col_height(gs) for gs in all_sorted)

    img  = Image.new("RGB", (IMG_W, IMG_H), HMAP_BG)
    draw = ImageDraw.Draw(img)

    _hmap_rounded_rect(draw, 0, 0, IMG_W - 1, HMAP_TOP_BAR, 0,
                       fill=(238,242,255), outline=(180,195,235), lw=1)
    draw.text((HMAP_MARGIN + 5, 9), f"MARKET MAP   {timestamp}",
              font=f_title, fill=(15,35,115))
    legend = "  Ma | Gia | %Gia"
    bb = draw.textbbox((0, 0), legend, font=f_data)
    draw.text((IMG_W - (bb[2] - bb[0]) - 8, 11), legend, font=f_data, fill=(100,110,140))

    for idx, cd in enumerate(all_cols):
        cx = cd["col"] * HMAP_COL_W + HMAP_MARGIN
        y  = HMAP_TOP_BAR + HMAP_MARGIN
        for g in all_sorted[idx]:
            avg = _hmap_avg_pct(g["symbols"], data)
            _hmap_draw_group_header(draw, cx, y, g["name"], avg, f_hdr, f_sector)
            y += HMAP_CELL_H
            for sym in g["symbols"]:
                info = data.get(sym, {})
                _hmap_draw_stock_cell(draw, cx, y, sym,
                                      info.get("price", 0.0), info.get("pct", 0.0),
                                      f_sym, f_data)
                y += HMAP_CELL_H

    draw.rectangle([0, 0, IMG_W - 1, IMG_H - 1], outline=(200,210,230), width=1)

    fd, path = tempfile.mkstemp(suffix='_heatmap.png')
    os.close(fd)
    img.save(path, "PNG", optimize=True)
    ts_log = datetime.now(TZ_VN).strftime('%H:%M:%S')
    print(f"  [{ts_log}] 🗺  Heatmap: ảnh {IMG_W}x{IMG_H}px → {path}")
    return path


def handle_heatmap_command(chat_id):
    url_msg   = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    url_photo = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    url_act   = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendChatAction"
    try:
        requests.post(url_act, data={"chat_id": chat_id, "action": "upload_photo"}, timeout=5)
        requests.post(url_msg, data={
            "chat_id": chat_id,
            "text": "🗺 Đang tải dữ liệu heatmap, vui lòng chờ 5–10 giây..."
        })
        data, ts_str = fetch_heatmap_data()
        if not data:
            requests.post(url_msg, data={"chat_id": chat_id,
                                         "text": "❌ Không lấy được dữ liệu heatmap. Thử lại sau."})
            return
        path = build_heatmap_image(data, ts_str)
        with open(path, "rb") as f:
            r = requests.post(url_photo, data={
                "chat_id":    chat_id,
                "caption":    f"<b>MARKET MAP</b>  {ts_str}",
                "parse_mode": "HTML",
            }, files={"photo": f}, timeout=60)
        if os.path.exists(path): os.remove(path)
        ts_log = datetime.now(TZ_VN).strftime('%H:%M:%S')
        print(f"  [{ts_log}] 🗺  Heatmap gửi {'OK' if r.status_code == 200 else 'THẤT BẠI'} → chat_id={chat_id}")
    except Exception as e:
        ts_log = datetime.now(TZ_VN).strftime('%H:%M:%S')
        print(f"  [{ts_log}] ❌ handle_heatmap_command lỗi: {e}")
        requests.post(url_msg, data={"chat_id": chat_id, "text": f"❌ Lỗi heatmap: {e}"})

# =============================================================================
# BƯỚC 3: HÀM BỔ TRỢ (TELEGRAM & WEEKLY DATA)
# =============================================================================
def build_weekly_df(df_daily):
    df_w = df_daily[['open','high','low','close','volume']].resample('W-FRI').agg({
        'open':'first','high':'max','low':'min','close':'last','volume':'sum',
    }).dropna()
    return compute_indicators(df_w)

def build_monthly_df(df_daily):
    df_m = df_daily[['open','high','low','close','volume']].resample('ME').agg({
        'open':'first','high':'max','low':'min','close':'last','volume':'sum',
    }).dropna()
    return compute_indicators(df_m)

def send_telegram_signal(msg, image_paths=None, image_path=None, notify_text=None):
    return
    if image_path and not image_paths:
        image_paths = [image_path]

    url_photo = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    url_album = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMediaGroup"
    url_msg   = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    try:
        if notify_text:
            requests.post(url_msg, data={
                'chat_id': TELEGRAM_CHAT_ID, 'text': notify_text, 'parse_mode': 'HTML'
            })

        if image_paths and len(image_paths) == 1:
            with open(image_paths[0], 'rb') as f:
                requests.post(url_photo, data={
                    'chat_id': TELEGRAM_CHAT_ID, 'caption': msg or '',
                    'parse_mode': 'HTML', 'disable_notification': True,
                }, files={'photo': f})
            print(f"  ✅ Đã gửi chart: {image_paths[0]}")

        elif image_paths and len(image_paths) >= 2:
            files, media = {}, []
            for i, path in enumerate(image_paths):
                key = f"photo{i}"; files[key] = open(path, 'rb')
                item = {"type": "photo", "media": f"attach://{key}"}
                if i == 0 and msg:
                    item["caption"] = msg; item["parse_mode"] = "HTML"
                media.append(item)
            try:
                requests.post(url_album, data={
                    'chat_id': TELEGRAM_CHAT_ID, 'media': json.dumps(media),
                    'disable_notification': True,
                }, files=files)
                print(f"  ✅ Đã gửi album: {image_paths[0]}")
            finally:
                for fh in files.values(): fh.close()

        if image_paths:
            for path in image_paths:
                if os.path.exists(path): os.remove(path)

    except Exception as e:
        print(f"  ❌ Lỗi gửi Telegram: {e}")

# =============================================================================
# BƯỚC 4: DANH SÁCH MÃ QUÉT
# =============================================================================
listing    = Listing(source=DATA_SOURCE)
df_listing = None
for _attempt in range(3):
    try:
        df_listing = listing.all_symbols()
        if df_listing is not None and not df_listing.empty:
            break
    except Exception as e:
        print(f"  ⚠️  Lỗi lấy danh sách mã (lần {_attempt+1}/3): {e}")
    df_listing = None
    time.sleep(5)
if df_listing is None:
    raise RuntimeError("Không lấy được danh sách mã niêm yết sau 3 lần thử — kiểm tra kết nối/API rồi chạy lại.")
col_name    = 'symbol' if 'symbol' in df_listing.columns else 'ticker'
all_symbols = df_listing[col_name].dropna().unique().tolist()

vn30_symbols = [
    'AAA','ACB','ANV','BFC','BID','BSR','BVH','BWE','CII','CRE','CTD','CTG','CTI','CTR','CTS',
    'DBC','DCM','DGW','DIG','DPG','DPM','DXG','FCN','FPT','FRT','FTS','GAS','GEG','GEX','GMD',
    'GVR','HAG','HAX','HBC','HCM','HDB','HDC','HDG','HNG','HPG','HSG','HTN','IDC','IJC','KBC',
    'KDH','KSB','LPB','MBB','MBS','MSB','MSN','MWG','NKG','NLG','NTL','NVL','PC1','PET','PLC',
    'PLX','PNJ','POW','PVD','PVS','PVT','REE','SBT','SCR','SHB','SHS','SSI','STB','SZC','TCB',
    'TIG','TNG','TPB','VCB','VCI','VGT','VHC','VHM','VIB','VIC','VJC','VNM','VPB','VRE',
    'MIG','HAH','HHV','BSI','C4G','G36','OIL','VGC','VND','BAF'
]
heatmap_symbols = {
    s
    for col in HEATMAP_COLUMNS
    for group in col["groups"]
    for s in group["symbols"]
}
cache_symbol_set = set(vn30_symbols) | set(TRADING_STOCKS_POOL) | heatmap_symbols
_HEATMAP_NEED_SYMBOLS = list(set(_HEATMAP_NEED_SYMBOLS) | set(vn30_symbols))
symbols_to_scan = [s for s in all_symbols if s in vn30_symbols]
symbols_to_rs = [s for s in all_symbols if s in cache_symbol_set]
symbols_to_cache = list(dict.fromkeys(symbols_to_rs + ["VNINDEX", "VN30"]))
print(f"🚀 Sẵn sàng quét {len(symbols_to_scan)} mã: {', '.join(symbols_to_scan)}")
print(f"📦 Cache lịch sử mở rộng: {len(symbols_to_cache)} mã (gồm cả VNINDEX, VN30)")

# =============================================================================
# BƯỚC 5: HÀM TÍNH CHỈ BÁO
# =============================================================================
def ref(series, n):  return series.shift(n)
def hhv(series, n):  return series.rolling(n).max()
def llv(series, n):  return series.rolling(n).min()

def cross_above(s1, s2):
    return (s1 >= s2) & (s1.shift(1) < s2.shift(1))

def afl_cross(s1, s2):
    if not isinstance(s2, pd.Series):
        s2 = pd.Series(s2, index=s1.index)
    return ((s1 > s2) & (s1.shift(1) <= s2.shift(1))).astype(bool)

def compute_indicators(df):
    df = df.copy()
    for n in [2,3,5,10,15,20,30,50,100,200]:
        df[f'MA{n}']  = df['close'].rolling(n).mean()
    for n in [10,20,30,50,100,200]:
        df[f'EMA{n}'] = df['close'].ewm(span=n, adjust=False).mean()
    for n in [2,3,5,10,15,20,30,50]:
        df[f'VMA{n}'] = df['volume'].rolling(n).mean()

    delta    = df['close'].diff()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    rs       = avg_gain / avg_loss.replace(0, np.nan)
    df['RSI14'] = 100 - (100 / (1 + rs))

    exp12 = df['close'].ewm(span=12, adjust=False).mean()
    exp26 = df['close'].ewm(span=26, adjust=False).mean()
    df['MACD']             = exp12 - exp26
    df['MACD_Signal']      = df['MACD'].ewm(span=9, adjust=False).mean()
    df['MACD_Hist_origin'] = df['MACD'] - df['MACD_Signal']
    df['MACD_Hist']        = df['MACD_Hist_origin'] * 3
    df['A']                = df['close'].pct_change()
    return df


# =============================================================================
# BƯỚC 5A1: VPA FLAG — tô màu Volume (port rút gọn từ AFL "Signal Generation" +
# "Volume ADD"). Với NHÁNH XANH DƯƠNG (cảnh báo suy yếu/phân phối), đã port:
#   - Điều kiện cổng ngoài cùng: upmajor>=0 AND upminor>=0
#   - Nhánh 1 ("biến A"): volume bất thường kèm giá không tăng
#   - Nhánh 2 (VSA): upThrustBar OR upThrustBarTrue OR topRevBar
#     (upThrustBarTrue KHÔNG phải tập con của upThrustBar — nó không đòi
#     upminor>0, chỉ cần upmajor>0 — nên bắt thêm được tín hiệu mà
#     upThrustBar bỏ sót, đã đối chiếu kỹ với AFL gốc và port đủ cả 2)
# CHƯA port: Nhánh 3 (~36 pattern nến bearish kèm điều kiện giá ở đỉnh 120
# phiên) — chi phí port lớn, giá trị tăng thêm nhỏ vì đã có điều kiện phụ rất
# chặt (đỉnh 120 phiên + volume bất thường) mới kích hoạt. Toàn bộ ~65 pattern
# nến (cả bullish lẫn bearish) trong AFL gốc vẫn KHÔNG port, theo đúng thống
# nhất từ đầu.
#
# AFL gốc gọi thẳng AmiBroker built-in RWIHi(min,max)/RWILo(min,max)/RWI(min,max).
# AmiBroker tính RWI bằng ATR(p) ở mẫu số; ATR() của AmiBroker dùng Wilder
# smoothing, không phải MA đơn giản của True Range. Vì vậy phần port dưới đây tự
# tính Wilder ATR trước khi quét p từ min→max để giảm lệch phân loại blue/cyan
# quanh ngưỡng upmajor/upminor.
# =============================================================================
def _true_range(df):
    prev_close = df['close'].shift(1)
    return pd.concat([
        df['high'] - df['low'],
        (df['high'] - prev_close).abs(),
        (df['low']  - prev_close).abs(),
    ], axis=1).max(axis=1)

def _wilder_atr(tr, period):
    """ATR(period) theo Wilder/AmiBroker: seed bằng SMA(period), sau đó recursive."""
    period = int(period)
    if period <= 0:
        raise ValueError("period must be positive")
    arr = pd.to_numeric(tr, errors='coerce').to_numpy(dtype=float)
    out = np.full(len(arr), np.nan, dtype=float)
    valid = np.isfinite(arr)
    if len(arr) < period or not valid[:period].all():
        return pd.Series(out, index=tr.index)
    out[period - 1] = arr[:period].mean()
    for i in range(period, len(arr)):
        if np.isfinite(arr[i]) and np.isfinite(out[i - 1]):
            out[i] = (out[i - 1] * (period - 1) + arr[i]) / period
    return pd.Series(out, index=tr.index)

def _rwi_hi_lo(df, pmin, pmax, tr=None):
    """RWIHi/RWILo: với mỗi p trong [pmin, pmax], dùng ATR(p) Wilder giống AmiBroker."""
    if tr is None:
        tr = _true_range(df)
    hi_max = pd.Series(0.0, index=df.index)
    lo_max = pd.Series(0.0, index=df.index)
    for p in range(pmin, pmax + 1):
        denom = _wilder_atr(tr, p) * math.sqrt(p)
        hi = ((df['high'] - df['low'].shift(p)) / denom).fillna(0)
        lo = ((df['high'].shift(p) - df['low']) / denom).fillna(0)
        hi_max = np.maximum(hi_max, hi)
        lo_max = np.maximum(lo_max, lo)
    return hi_max, lo_max

def calc_vpa_flag(df, rwi_short=(2, 8), rwi_long=(10, 40), min_bars=140):
    """
    Trả về Series int8 cùng index với df:
      0 = trung tính  → giữ màu xanh/đỏ theo close/open như cũ
      1 = cảnh báo suy yếu / phân phối → tô xanh dương
          gate(upmajor>=0 AND upminor>=0) AND
          (Nhánh1-biến A OR upThrustBar OR upThrustBarTrue OR topRevBar)
      2 = tín hiệu tích lũy mạnh (stopVolume OR revUpThrust) → tô tím

    df cần tối thiểu các cột: open, high, low, close, volume.
    Nếu dữ liệu chưa đủ dài (RWI dài hạn cần ~40 phiên lùi + rolling(80) cho
    avg_spread) thì trả về toàn 0 để tránh tín hiệu nhiễu/rác.
    """
    if df is None or len(df) < min_bars:
        return pd.Series(0, index=(df.index if df is not None else []), dtype='int8')

    h, l, c, v = df['high'], df['low'], df['close'], df['volume']
    tr = _true_range(df)

    hi_st, _        = _rwi_hi_lo(df, *rwi_short, tr=tr)   # Ground — dùng cho upimd
    hi_lt, lo_lt     = _rwi_hi_lo(df, *rwi_long,  tr=tr)   # j2/lo_lt — dùng cho upmajor/upminor

    j       = hi_lt - lo_lt                 # RWI dài hạn có dấu (theo cross(j,1)/cross(j,-1) trong AFL)
    upmajor = np.select([j > 1, j < -1], [1, -1], default=0)
    upminor = np.where(hi_lt > 1, 1, -1)
    upimd   = np.where(hi_st > 1, 1, 0)
    gate_not_downtrend = (upmajor >= 0) & (upminor >= 0)   # điều kiện cổng bọc ngoài toàn nhánh xanh dương

    spread     = h - l
    avg_spread = spread.rolling(80).mean()
    wide_range_bar = spread > 1.5 * avg_spread

    maV5, maV20, maV50 = v.rolling(5).mean(), v.rolling(20).mean(), v.rolling(50).mean()
    vol_avg = v.rolling(30).mean()   # = maV30 trong AFL
    up_bar   = c > c.shift(1)
    down_bar = c < c.shift(1)

    # Nhánh 1 (biến "A" trong AFL): giá không tăng, đi kèm volume bất thường
    # theo 1 trong 3 kiểu — báo hiệu lực bán/phân phối đang âm thầm diễn ra.
    dv = v / v.shift(1)
    nhanh1 = (c <= c.shift(1)) & (
        ((dv > 1.06) & ((v > vol_avg) | (v > maV50) | (v > maV20)))
        | ((v > maV5) & (v > 1.2 * maV20) & (v > 1.2 * vol_avg) & (v > 1.2 * maV50))
        | ((v > v.shift(1)) & (v > maV5) & (v > maV20) & (v > vol_avg) & (v > maV50))
    )

    close_pos = np.select(
        [c <= spread * 0.2 + l, c <= spread * 0.4 + l, c <= spread * 0.6 + l, c <= spread * 0.8 + l],
        [1, 2, 3, 4], default=5,
    )
    vol_pos = np.select(
        [v > vol_avg * 2, v > vol_avg * 1.3, v > vol_avg],
        [1, 2, 3],
        default=np.where((v < vol_avg) & (v > vol_avg * 0.7), 4, 5),
    )
    up_close   = c >= spread * 0.7 + l
    down_close = c <= spread * 0.3 + l
    mid_close  = (c > spread * 0.3 + l) & (c < spread * 0.7 + l)

    up_bar_prev1, down_bar_prev1     = up_bar.shift(1, fill_value=False), down_bar.shift(1, fill_value=False)
    wide_bar_prev1                   = wide_range_bar.shift(1, fill_value=False)
    down_close_prev1                 = down_close.shift(1, fill_value=False)

    up_thrust_bar = (
        wide_range_bar & np.isin(close_pos, [1, 2]) & (upminor > 0) &
        (h > h.shift(1)) & ((upimd > 0) | (upmajor > 0)) & (vol_pos < 4)
    )
    up_thrust_bar_true = (
        wide_range_bar & (close_pos == 1) & (upmajor > 0) &
        (h > h.shift(1)) & (vol_pos < 4)
    )
    top_rev_bar = (
        (v.shift(1) > vol_avg) & up_bar_prev1 & wide_bar_prev1 &
        down_bar & down_close & wide_range_bar & (upmajor > 0) & (h == h.rolling(10).max())
    )
    stop_volume = (
        (l == l.rolling(5).min()) & (up_close | mid_close) & (v > 1.5 * vol_avg) & (upmajor < 0)
    )
    rev_up_thrust = (
        (upmajor < 0) & up_bar & up_close & (v > v.shift(1)) & (v > vol_avg) &
        wide_range_bar & down_bar_prev1 & down_close_prev1 & (upminor < 0)
    )

    flag = pd.Series(0, index=df.index, dtype='int8')
    blue_signal = gate_not_downtrend & (nhanh1 | up_thrust_bar | up_thrust_bar_true | top_rev_bar)
    # Gate tím: AFL gốc bọc (upmajor<0 AND upminor<0) ngoài (stopVolume OR revUpThrust)
    # trước khi tô "Super Up Color" — thiếu vế upminor<0 khiến stop_volume tô tím cả khi
    # upminor>=0 (dương tính giả so với bản gốc).
    purple_gate = (upmajor < 0) & (upminor < 0)
    flag[blue_signal.fillna(False).astype(bool)]                                   = 1
    flag[(purple_gate & (stop_volume | rev_up_thrust)).fillna(False).astype(bool)] = 2   # ưu tiên tích lũy nếu trùng cả 2 (hiếm)
    return flag


# =============================================================================
# BƯỚC 5A2: MARKET HEALTH / FEAR-GREED INDEX
# =============================================================================
def _mh_finite_float(value, default=None):
    try:
        value = float(value)
        return value if np.isfinite(value) else default
    except Exception:
        return default


def _mh_score_band(score: float) -> dict:
    if score >= 80:
        return {"key": "euphoria", "label": "Hưng phấn", "tone": "purple"}
    if score >= 60:
        return {"key": "positive", "label": "Lạc quan", "tone": "green"}
    if score >= 40:
        return {"key": "neutral", "label": "Trung tính", "tone": "yellow"}
    if score >= 20:
        return {"key": "negative", "label": "Bi quan", "tone": "red"}
    return {"key": "fear", "label": "Sợ hãi", "tone": "cyan"}


def _mh_percentile_score(series: pd.Series, window: int = 60) -> pd.Series:
    series = pd.to_numeric(series, errors="coerce")

    def _rank(x):
        x = pd.Series(x).dropna()
        if len(x) < 20:
            return np.nan
        return 100.0 * (x <= x.iloc[-1]).mean()

    return series.rolling(window, min_periods=20).apply(_rank, raw=False).clip(0, 100)


def _mh_last_streak(values, pred) -> int:
    n = 0
    for v in reversed(list(values)):
        if pred(v):
            n += 1
        else:
            break
    return n


def _mh_component_phrase(key: str, value) -> str:
    """
    Chuyển điểm số 0-100 của từng thành phần HEALTH thành một câu mô tả bằng
    ngôn ngữ thường, dùng để đưa vào phần NHẬN ĐỊNH thay cho việc hiển thị số
    dạng "X/100" trên thanh ngang — người đọc không cần hiểu ý nghĩa thang điểm
    nội bộ, chỉ cần đọc câu là hiểu ngay tình trạng thị trường.
    """
    v = _mh_finite_float(value)
    if v is None:
        return ""
    if key == "momentum":
        if v >= 80: return "Đà thị trường đang tăng rất mạnh so với giai đoạn gần đây"
        if v >= 60: return "Đà thị trường nghiêng lạc quan"
        if v >= 40: return "Đà thị trường trung tính, chưa có xu hướng rõ ràng"
        if v >= 20: return "Đà thị trường đang suy yếu"
        return "Đà thị trường rất yếu, thấp hơn hẳn xu hướng gần đây"
    if key == "volatility":
        if v >= 80: return "Biến động giá đang rất thấp, thị trường ổn định"
        if v >= 60: return "Biến động giá ở mức thấp"
        if v >= 40: return "Biến động giá ở mức bình thường"
        if v >= 20: return "Biến động giá đang cao hơn bình thường"
        return "Biến động giá đang rất cao, thị trường dao động mạnh bất thường"
    if key == "breadth":
        if v >= 70: return "Phần lớn cổ phiếu trong rổ vẫn giữ vững xu hướng tăng"
        if v >= 50: return "Đa số cổ phiếu còn giữ xu hướng tăng"
        if v >= 30: return "Số cổ phiếu giữ xu hướng tăng và giảm khá cân bằng"
        if v >= 15: return "Phần lớn cổ phiếu đã gãy xu hướng tăng"
        return "Hầu hết cổ phiếu trong rổ đã mất xu hướng tăng"
    if key == "new_high_low":
        if v >= 80: return "Số mã phá đỉnh 52 tuần (trong phiên) áp đảo, không nhiều mã phá đáy"
        if v >= 60: return "Số mã phá đỉnh (trong phiên) nhỉnh hơn số mã phá đáy"
        if v >= 40: return "Chưa có làn sóng phá đỉnh hay phá đáy đáng chú ý"
        if v >= 20: return "Số mã phá đáy 52 tuần (trong phiên) nhỉnh hơn số mã phá đỉnh"
        return "Có mã phá đáy 52 tuần trong khi không có mã nào phá đỉnh"
    if key == "volume":
        if v >= 80: return "Dòng tiền đổ vào rất mạnh, nhiều mã tăng kèm khối lượng đột biến"
        if v >= 60: return "Dòng tiền nghiêng về mua chủ động"
        if v >= 40: return "Dòng tiền theo khối lượng chưa rõ xu hướng"
        if v >= 20: return "Dòng tiền nghiêng về bán, xuất hiện phiên giảm kèm khối lượng lớn"
        return "Áp lực bán rất mạnh, nhiều mã giảm sâu kèm khối lượng đột biến"
    if key == "rsi":
        # RSI dùng ngưỡng quy ước riêng (70/30) thay vì thang 20-40-60-80 chung,
        # vì RSI vốn đã là chỉ báo có ý nghĩa chuẩn hoá quen thuộc với người đọc.
        if v >= 70: return "RSI trung vị đã vào vùng quá mua"
        if v >= 55: return "RSI trung vị nghiêng lạc quan"
        if v >= 45: return "RSI trung vị ở vùng trung tính"
        if v >= 30: return "RSI trung vị nghiêng bi quan"
        return "RSI trung vị đã vào vùng quá bán"
    return ""


def compute_market_health_index(limit: int = 120) -> dict:
    """
    Chỉ số HEALTH/Fear-Greed dùng hoàn toàn dữ liệu đang có trong history_cache.
    Mỗi phiên được chấm 0-100 từ momentum proxy, volatility, breadth, new high/low,
    RSI trung vị và volume stress của rổ HEATMAP/TRADING.
    """
    # NGƯỠNG ĐỘ DÀI TỐI THIỂU: đồng bộ với build_history_cache() (dòng ~848) —
    # nơi coi 1 mã là "đã cache hợp lệ" khi có >= 60 phiên. Trước đây HEALTH tự
    # đòi >= 80 phiên, khiến mọi mã có 60-79 phiên (mã mới niêm yết, mã bị giới
    # hạn lịch sử từ nguồn dữ liệu...) bị coi là "đã cache" ở MỌI nơi khác trong
    # hệ thống nhưng lại bị âm thầm loại khỏi HEALTH — dễ gây hiểu lầm "cache đủ
    # rồi mà vẫn báo thiếu". Với 60 phiên, các chỉ báo HEALTH đang dùng (MA50,
    # RSI14, VMA20, rolling 252 với min_periods=60) vẫn tính đủ giá trị hợp lệ ở
    # phiên gần nhất nên hạ ngưỡng là an toàn, không làm giảm chất lượng tính.
    MH_MIN_ROWS = 60
    with cache_lock:
        # Không .copy() ở đây: chỉ giữ reference để thoát lock nhanh. An toàn vì
        # history_cache chỉ bị thay bằng cách gán lại key (history_cache[sym] = df_moi)
        # ở nơi khác trong file, không có chỗ nào sửa in-place lên DataFrame đang tồn tại
        # — nên object df đang giữ reference ở đây sẽ không bị đổi ngầm sau khi thoát lock.
        raw_cache = {
            sym: df
            for sym, df in history_cache.items()
            if sym in cache_symbol_set and df is not None and len(df) >= MH_MIN_ROWS
        }

    prepared = {}
    for sym, df in raw_cache.items():
        try:
            cols = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
            if "close" not in cols:
                continue
            d = df[cols].copy().sort_index()
            if "volume" not in d.columns:
                d["volume"] = 0
            d.index = pd.to_datetime(d.index).normalize()
            d = d[~d.index.duplicated(keep="last")]
            # Check độ dài NGAY SAU dedup (trước compute_indicators): dedup là bước
            # duy nhất có thể làm giảm số dòng ở đây, nên lọc sớm để khỏi tốn công
            # tính ~20 cột indicator (MA/EMA/RSI/MACD) cho các mã vừa bị loại.
            if len(d) < MH_MIN_ROWS:
                continue
            d = compute_indicators(d)
            prepared[sym] = d
        except Exception:
            continue

    if len(prepared) < 20:
        return {
            "ok": False,
            "error": "not_enough_cache",
            "message": f"Chưa đủ cache để tính Mrk Health ({len(prepared)} mã hợp lệ).",
            "history": [],
            "components": [],
        }

    close_df = pd.concat({sym: d["close"] for sym, d in prepared.items()}, axis=1).sort_index()
    close_df = close_df.dropna(how="all").tail(360)
    returns = close_df.pct_change(fill_method=None)
    eq_ret = returns.mean(axis=1, skipna=True).fillna(0)
    market_proxy = (1 + eq_ret).cumprod() * 100
    momentum_raw = (market_proxy / market_proxy.rolling(20, min_periods=12).mean() - 1) * 100
    volatility_raw = -eq_ret.rolling(10, min_periods=5).std() * np.sqrt(252) * 100
    momentum_score = _mh_percentile_score(momentum_raw)
    volatility_score = _mh_percentile_score(volatility_raw)

    above_ma50, rsi14, new_high, new_low, vol_push, decline_today, hoang_loan, ban_thao = {}, {}, {}, {}, {}, {}, {}, {}
    for sym, d in prepared.items():
        above_ma50[sym] = (d["close"] > d["MA50"]).astype(float)
        rsi14[sym] = d["RSI14"]
        new_high[sym] = (d["close"] >= d["close"].rolling(252, min_periods=60).max()).astype(float)
        new_low[sym] = (d["close"] <= d["close"].rolling(252, min_periods=60).min()).astype(float)
        pct = d["close"].pct_change() * 100
        decline_today[sym] = (pct < 0).astype(float)
        v_ratio = d["volume"] / d["VMA20"].replace(0, np.nan)
        excite = ((pct >= 2.5) & (v_ratio >= 1.5)).astype(float)
        panic = ((pct <= -2.5) & (v_ratio >= 1.5)).astype(float)
        vol_push[sym] = excite - panic
        hoang_loan[sym] = ((pct <= -2.5) & (pct > -4) & (v_ratio >= 1.3)).astype(float)
        ban_thao[sym] = ((pct <= -4) & (v_ratio >= 1.3)).astype(float)

    dates = close_df.index[-max(limit, 30):]
    breadth_s = pd.concat(above_ma50, axis=1).reindex(dates).mean(axis=1, skipna=True) * 100
    decline_s = pd.concat(decline_today, axis=1).reindex(dates).mean(axis=1, skipna=True) * 100
    hoang_loan_s = pd.concat(hoang_loan, axis=1).reindex(dates).mean(axis=1, skipna=True) * 100
    ban_thao_s = pd.concat(ban_thao, axis=1).reindex(dates).mean(axis=1, skipna=True) * 100
    rsi_s = pd.concat(rsi14, axis=1).reindex(dates).median(axis=1, skipna=True).clip(0, 100)
    nh_s = pd.concat(new_high, axis=1).reindex(dates).sum(axis=1, skipna=True)
    nl_s = pd.concat(new_low, axis=1).reindex(dates).sum(axis=1, skipna=True)
    denom = (nh_s + nl_s).replace(0, np.nan)
    newhl_s = (50 + 50 * ((nh_s - nl_s) / denom)).fillna(50).clip(0, 100)
    vol_push_s = (50 + 50 * pd.concat(vol_push, axis=1).reindex(dates).mean(axis=1, skipna=True)).clip(0, 100)

    component_series = {
        "momentum": momentum_score.reindex(dates),
        "volatility": volatility_score.reindex(dates),
        "breadth": breadth_s,
        "new_high_low": newhl_s,
        "rsi": rsi_s,
        "volume": vol_push_s,
    }
    score_df = pd.DataFrame(component_series)
    score = score_df.mean(axis=1, skipna=True).clip(0, 100)

    usable = score.dropna()
    if usable.empty:
        return {
            "ok": False,
            "error": "not_enough_history",
            "message": "Cache chưa đủ lịch sử để tính Mrk Health.",
            "history": [],
            "components": [],
        }

    # VNINDEX không nằm trong cache_symbol_set (rổ dùng để tính HEALTH), nên phải
    # tự đảm bảo có dữ liệu — dùng lại đúng cơ chế ensure-on-demand mà CHART tab
    # đang dùng khi người dùng xem chart VNINDEX, tránh viết thêm luồng fetch riêng.
    try:
        ensure_symbol_live_in_cache("VNINDEX")
    except Exception:
        pass
    with cache_lock:
        _vni_df = history_cache.get("VNINDEX")
    vni_close = None
    if _vni_df is not None and not _vni_df.empty and "close" in _vni_df.columns:
        vni_close = _vni_df["close"].copy()
        vni_close.index = pd.to_datetime(vni_close.index).normalize()
        vni_close = vni_close[~vni_close.index.duplicated(keep="last")]

    last_dates = list(usable.tail(limit).index)
    history = []
    for dt in last_dates:
        entry = {
            "date": pd.Timestamp(dt).strftime("%Y-%m-%d"),
            "score": round(float(score.loc[dt]), 1),
        }
        if vni_close is not None:
            v = vni_close.get(dt)
            entry["vnindex"] = round(float(v), 2) if v is not None and pd.notna(v) else None
        history.append(entry)

    cur_dt = last_dates[-1]
    cur_score = float(score.loc[cur_dt])
    prev_score = float(score.loc[last_dates[-2]]) if len(last_dates) >= 2 else cur_score
    current_components = {
        k: _mh_finite_float(v.loc[cur_dt])
        for k, v in component_series.items()
    }
    component_labels = {
        "momentum": "Đà thị trường",
        "volatility": "Biến động",
        "breadth": "Độ rộng",
        "new_high_low": "Đỉnh/đáy 52 tuần",
        "rsi": "RSI trung vị",
        "volume": "Dòng tiền/volume",
    }
    components = [
        {
            "key": k,
            "label": component_labels[k],
            "score": round(v, 1) if v is not None else None,
        }
        for k, v in current_components.items()
    ]

    recent_scores = [x["score"] for x in history[-10:]]
    high_streak = _mh_last_streak(recent_scores, lambda x: x >= 75)
    low_streak = _mh_last_streak(recent_scores, lambda x: x <= 25)
    rising_3 = len(history) >= 4 and history[-1]["score"] > history[-4]["score"]
    falling_3 = len(history) >= 4 and history[-1]["score"] < history[-4]["score"]

    breadth_now = current_components.get("breadth") or 0
    decline_now = _mh_finite_float(decline_s.loc[cur_dt]) if cur_dt in decline_s.index else None
    hoang_loan_now = _mh_finite_float(hoang_loan_s.loc[cur_dt]) if cur_dt in hoang_loan_s.index else None
    ban_thao_now = _mh_finite_float(ban_thao_s.loc[cur_dt]) if cur_dt in ban_thao_s.index else None

    band = _mh_score_band(cur_score)
    delta = cur_score - prev_score

    # Xu hướng ngắn hạn dùng chung cho các tag "đảo chiều"/"phân phối" bên dưới:
    # reversing_top/recovering_bottom = đã XÁC NHẬN đổi chiều (falling_3/rising_3
    # hoặc delta cùng dấu). Tách bạch với "chưa đổi chiều" để tag cảnh báo sớm
    # (Dấu hiệu phân phối) và tag xác nhận đảo chiều (Điều chỉnh từ đỉnh) không
    # lặp lại cùng một ý nghĩa ở 2 thời điểm khác nhau.
    reversing_top = falling_3 or delta < 0
    recovering_bottom = rising_3 or delta > 0
    max_recent_10 = max(recent_scores)
    min_recent_10 = min(recent_scores)

    tags = []
    if cur_score >= 90:
        tags.append("Cực kỳ hưng phấn")
    if cur_score <= 10:
        tags.append("Cực kỳ sợ hãi")
    # "Dấu hiệu phân phối": cảnh báo SỚM — breadth đã yếu trong khi streak điểm cao
    # vẫn đang giữ, giá CHƯA xác nhận giảm (not reversing_top). Cố ý không giới hạn
    # trần cur_score nên có thể trùng với "Cực kỳ hưng phấn" (kịch bản "blow-off
    # top": giá vẫn tăng rất mạnh nhưng độ rộng đã co hẹp) — đây là chủ đích, không
    # phải lỗi.
    if high_streak >= 3 and breadth_now < 55 and not reversing_top:
        tags.append("Dấu hiệu phân phối")
    # "Điều chỉnh từ đỉnh": gộp 2 tag cũ ("Rủi ro tạo đỉnh" + "Hạ nhiệt từ hưng
    # phấn") vì cùng mô tả 1 quá trình (giá đảo chiều sau giai đoạn hưng phấn),
    # chỉ khác lát cắt thời điểm đo:
    #   - Nhánh A: điểm vẫn ≥75 (đang trong streak cao) nhưng đã bắt đầu giảm.
    #   - Nhánh B: điểm đã rơi xuống band Lạc quan (60-80, hở 2 đầu để không đụng
    #     "Tích lũy cân bằng" ở mốc 60 và không đụng "Cực kỳ hưng phấn" ở mốc 90)
    #     nhưng gần đây (10 phiên) từng đạt Hưng phấn (≥80).
    # Nhánh A không giới hạn trần điểm nên vẫn có thể trùng "Cực kỳ hưng phấn" —
    # chủ đích, giữ nguyên như "Dấu hiệu phân phối" ở trên.
    if (high_streak >= 3 and falling_3) or (
        60 < cur_score < 80 and max_recent_10 >= 80 and reversing_top
    ):
        tags.append("Điều chỉnh từ đỉnh")
    # "Phục hồi từ đáy": đối xứng với "Điều chỉnh từ đỉnh", gộp "Tín hiệu dò đáy" +
    # "Hồi phục từ sợ hãi". Không có tag "cảnh báo sớm" đối xứng với "Dấu hiệu phân
    # phối" ở phía đáy — có chủ đích, vì breadth cải thiện sớm ở vùng đáy không
    # mang tính cảnh báo rủi ro như breadth suy yếu sớm ở vùng đỉnh.
    if (low_streak >= 3 and rising_3) or (
        20 <= cur_score < 40 and min_recent_10 <= 20 and recovering_bottom
    ):
        tags.append("Phục hồi từ đáy")
    # "Hoảng loạn" / "Bán tháo" là sự kiện CẤP TÍNH trong 1 phiên (giảm sâu + volume
    # xác nhận), không lệ thuộc band/xu hướng điểm HEALTH nhiều phiên trước đó — nên
    # có thể xảy ra ngay cả khi thị trường đang uptrend (VD phiên phân phối đỉnh, RSI
    # vẫn >80 là bình thường). Vì vậy KHÔNG dùng cur_score/breadth_now(MA50)/rsi_now ở
    # đây, chỉ dùng % mã thỏa biên độ giảm + volume đột biến ≥1.3x TB 20 phiên trong
    # chính phiên đó. Hai tag loại trừ lẫn nhau ở cấp độ từng mã (2,5%-4% vs ≥4%),
    # nhưng vẫn có thể cùng xuất hiện nếu cả 2 nhóm mã đều đủ % ngưỡng diện rộng.
    MH_PANIC_PCT = 60  # ngưỡng % mã thỏa điều kiện để coi là diện rộng, áp dụng cho cả 2 tag
    if (hoang_loan_now or 0) >= MH_PANIC_PCT:
        tags.append("Hoảng loạn")
    if (ban_thao_now or 0) >= MH_PANIC_PCT:
        tags.append("Bán tháo")
    if 45 <= cur_score <= 60 and abs(delta) <= 4 and 40 <= breadth_now <= 60:
        tags.append("Tích lũy cân bằng")
    if not tags:
        tags.append("Theo dõi xu hướng")

    summary = (
        f"Mrk Health hiện ở vùng {band['label']} ({cur_score:.1f}/100), "
        f"{'tăng' if delta >= 0 else 'giảm'} {abs(delta):.1f} điểm so với phiên trước."
    )
    factors = [
        p for p in (
            _mh_component_phrase("momentum", current_components.get("momentum")),
            _mh_component_phrase("volatility", current_components.get("volatility")),
            _mh_component_phrase("breadth", current_components.get("breadth")),
            _mh_component_phrase("new_high_low", current_components.get("new_high_low")),
            _mh_component_phrase("volume", current_components.get("volume")),
            _mh_component_phrase("rsi", current_components.get("rsi")),
        ) if p
    ]
    factors = [f"{p}." for p in factors]
    if decline_now is not None:
        factors.append(f"Trong phiên hôm nay có khoảng {decline_now:.0f}% cổ phiếu trong rổ giảm giá.")
    has_euphoria = "Cực kỳ hưng phấn" in tags
    has_fear = "Cực kỳ sợ hãi" in tags
    has_correction = "Điều chỉnh từ đỉnh" in tags
    has_distribution = "Dấu hiệu phân phối" in tags
    has_recovery = "Phục hồi từ đáy" in tags

    if has_euphoria and has_correction:
        conclusion = "Kết luận: Chỉ số đang ở vùng cực kỳ hưng phấn (≥90/100) và đã bắt đầu chững/quay đầu — rủi ro đảo chiều ngắn hạn cao; ưu tiên chốt lời/giảm tỷ trọng ở nhóm đã tăng nóng, tránh mua đuổi."
    elif has_euphoria and has_distribution:
        conclusion = "Kết luận: Chỉ số đang ở vùng cực kỳ hưng phấn (≥90/100) nhưng độ rộng thị trường không theo kịp đà tăng giá — dấu hiệu phân phối sớm giữa lúc tăng nóng; rủi ro đảo chiều ngắn hạn cao, ưu tiên chốt lời/giảm tỷ trọng, tránh mua đuổi."
    elif has_euphoria:
        conclusion = "Kết luận: Chỉ số đang ở vùng cực kỳ hưng phấn (≥90/100) — thị trường tăng nóng, rủi ro đảo chiều ngắn hạn gia tăng; ưu tiên chốt lời/giảm tỷ trọng ở nhóm đã tăng mạnh, hạn chế mua đuổi."
    elif has_fear and has_recovery:
        conclusion = "Kết luận: Chỉ số đang ở vùng cực kỳ sợ hãi (≤10/100) và đã có dấu hiệu hồi phục — tâm lý bán tháo đang dịu bớt, có thể là vùng dò đáy tiềm năng nhưng vẫn cần thêm phiên xác nhận độ rộng/RSI trước khi giải ngân, tránh bắt đáy sớm."
    elif has_fear:
        conclusion = "Kết luận: Chỉ số đang ở vùng cực kỳ sợ hãi (≤10/100) — tâm lý bán là chủ đạo, thường là vùng dò đáy tiềm năng, nhưng cần chờ xác nhận độ rộng/RSI cải thiện trước khi giải ngân, tránh bắt đáy sớm."
    elif has_correction:
        conclusion = "Kết luận: Chỉ số đang điều chỉnh từ vùng hưng phấn/lạc quan — đà tăng đã bớt nóng và bắt đầu suy yếu; ưu tiên chốt lời một phần ở nhóm tăng mạnh trước đó, hạn chế mua đuổi."
    elif has_distribution:
        conclusion = "Kết luận: Điểm Mrk Health vẫn ở vùng cao nhưng độ rộng thị trường đang thu hẹp — dấu hiệu phân phối sớm, nền tăng đang mỏng dần dù giá chưa xác nhận giảm; nên bắt đầu thận trọng, hạn chế mua mới, chưa cần bán vội nếu điểm Mrk Health chưa giảm rõ."
    elif has_recovery:
        conclusion = "Kết luận: Chỉ số đang phục hồi từ vùng sợ hãi/bi quan — lực bán đã suy yếu và tâm lý đang cải thiện; theo dõi quá trình tạo đáy, nhưng chưa nên coi là xác nhận đảo chiều nếu độ rộng chưa cải thiện rõ."
    elif "Tích lũy cân bằng" in tags:
        conclusion = "Kết luận: Thị trường nghiêng về tích lũy/cân bằng; chưa có xác nhận đỉnh hoặc đáy rõ ràng."
    else:
        conclusion = "Kết luận: Chưa có tín hiệu cực đoan đủ mạnh theo band điểm Mrk Health để kết luận đỉnh hoặc đáy; tiếp tục theo dõi breadth và volume."
    # Hoảng loạn/Bán tháo là sự kiện CẤP TÍNH trong phiên, có thể trùng với BẤT KỲ tag/
    # band nào ở trên (kể cả giữa uptrend) — thay vì lồng vào từng nhánh phía trên (dễ
    # gây rối, khó bảo trì), luôn thêm 1 câu nhận xét ĐỘC LẬP ở cuối, tách bạch rõ ràng.
    if "Bán tháo" in tags:
        pct_str = f"~{ban_thao_now:.0f}%" if ban_thao_now is not None else "nhiều"
        conclusion += f" Xuất hiện dấu hiệu bán tháo diện rộng ({pct_str} số mã giảm ≥4% kèm volume ≥1,3 lần TB20) — thận trọng khi giao dịch."
    elif "Hoảng loạn" in tags:
        pct_str = f"~{hoang_loan_now:.0f}%" if hoang_loan_now is not None else "nhiều"
        conclusion += f" Xuất hiện dấu hiệu hoảng loạn diện rộng ({pct_str} số mã giảm 2,5%-4% kèm volume ≥1,3 lần TB20) — thận trọng khi giao dịch, tránh phản ứng thái quá."

    return {
        "ok": True,
        "as_of": pd.Timestamp(cur_dt).strftime("%Y-%m-%d"),
        "updated_at": datetime.now(TZ_VN).strftime("%Y-%m-%d %H:%M:%S"),
        "score": round(cur_score, 1),
        "delta": round(delta, 1),
        "band": band,
        "tags": tags,
        "history": history,
        "components": components,
        "vnindex_available": vni_close is not None,
        "analysis": {
            "summary": summary,
            "factors": factors,
            "conclusion": conclusion,
        },
        "meta": {
            "symbols": len(prepared),
            "lookback_sessions": len(last_dates),
            "source": "history_cache",
        },
    }

# =============================================================================
# BƯỚC 5B: CACHE LỊCH SỬ
# =============================================================================
history_cache: dict = {}
cache_lock          = threading.Lock()
last_bar_update: dict = {}   # {symbol: timestamp} - dùng chung cho CẢ scan cycle lẫn chart on-demand
BAR_UPDATE_TTL_SEC = 60

_vndirect_session = requests.Session()
_vndirect_session.headers.update({
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json,*/*",
    "Referer": "https://dstock.vndirect.com.vn/",
})

def _fetch_vnd(symbol: str, limit: int, resolution: str = "D"):
    symbol = symbol.upper().strip()
    to_ts = int(time.time())
    if resolution == "15":
        from_ts = to_ts - int(limit * 3600 * 24 / 20)  # ~20 nến 15m/ngày
    else:
        from_ts = to_ts - int(limit * 1.6 + 30) * 86400
    url = f"https://dchart-api.vndirect.com.vn/dchart/history?resolution={resolution}&symbol={symbol}&from={from_ts}&to={to_ts}"
    res = _vndirect_session.get(url, timeout=10)
    if res.status_code != 200: return None
    data = res.json()
    if not data or data.get("s") != "ok" or not data.get("t"): return None
    times, opens, highs = data.get("t", []), data.get("o", []), data.get("h", [])
    lows, closes, vols = data.get("l", []), data.get("c", []), data.get("v", [])
    bars = []
    for i in range(len(times)):
        try:
            o, h, l, c, v = float(opens[i]), float(highs[i]), float(lows[i]), float(closes[i]), float(vols[i])
            if all(math.isfinite(x) and x > 0 for x in (o, h, l, c)):
                dt = datetime.utcfromtimestamp(times[i] + 25200)
                bars.append({"time": dt, "open": o, "high": h, "low": l, "close": c, "volume": max(0.0, v)})
        except: pass
    if not bars: return None
    df = pd.DataFrame(bars)
    df.set_index('time', inplace=True)
    return df

def load_history_for_symbol(symbol: str):
    for attempt in range(3):
        try:
            df = _fetch_vnd(symbol, limit=1000)
            if df is None or len(df) < 60: return None
            df['vpa_flag'] = calc_vpa_flag(df)
            return df
        except Exception as e:
            if attempt < 2: time.sleep(1)
            else: print(f"    ❌ Load history {symbol}: {e}")
    return None

def build_history_cache(symbols: list, current_date: date):
    ts = datetime.now(TZ_VN).strftime('%H:%M:%S')
    print(f"\n📦 [{ts}] Bắt đầu load cache lịch sử cho {len(symbols)} mã...")
    new_history = {}
    for i, symbol in enumerate(symbols, 1):
        df = load_history_for_symbol(symbol)
        if df is not None and len(df) >= 60:
            new_history[symbol] = df
        if i % 20 == 0:
            ts2 = datetime.now(TZ_VN).strftime('%H:%M:%S')
            print(f"  [{ts2}] Đã load {i}/{len(symbols)} mã...")
        time.sleep(0.05)  # Dùng VNDirect không lo rate limit, chỉ delay nhẹ nhường CPU
    with cache_lock:
        history_cache.clear()
        history_cache.update(new_history)
    invalidate_rs_cache()
    warm_rs_cache()
    ts = datetime.now(TZ_VN).strftime('%H:%M:%S')
    print(f"✅ [{ts}] Cache hoàn tất: {len(new_history)}/{len(symbols)} mã có dữ liệu.")

# =============================================================================
# BƯỚC 5B2: KIỂM TRA CACHE NHANH TRƯỚC KHI QUÉT
# =============================================================================
CACHE_CHECK_SYMBOL = 'HPG'
SESSION_MORNING_START = 85500
SESSION_MORNING_END = 113000
SESSION_AFTERNOON_START = 130000
SESSION_AFTERNOON_END = 150000

def _is_trading_session_time(current_date: date, now_time: int) -> bool:
    if current_date.weekday() >= 5:
        return False
    return (
        SESSION_MORNING_START <= now_time <= SESSION_MORNING_END or
        SESSION_AFTERNOON_START <= now_time <= SESSION_AFTERNOON_END
    )

def _next_trading_session_label(now_time: int) -> str:
    if now_time < SESSION_MORNING_START:
        return "08:55"
    if now_time < SESSION_AFTERNOON_START:
        return "13:00"
    return "08:55 ngày mai"

def _expected_last_session(current_date: date, now_time: int) -> date:
    """Trả về ngày nến kỳ vọng của cache dựa trên mốc 15h00."""
    expected = (pd.Timestamp(current_date) - pd.tseries.offsets.BDay(1)).date()
    if current_date.weekday() < 5 and now_time >= 150000:
        expected = current_date
    return expected

def _cache_is_fresh(df_hist, current_date: date, now_time: int) -> bool:
    if df_hist is None or len(df_hist) == 0:
        return False
    return df_hist.index[-1].date() >= _expected_last_session(current_date, now_time)

def check_and_rebuild_cache_if_stale(symbols: list, current_date: date) -> bool:
    """Kiểm tra cache qua 1 mã mẫu, rebuild nếu lệch phiên."""
    now_obj  = datetime.now(TZ_VN)
    ts       = now_obj.strftime('%H:%M:%S')
    now_time = int(now_obj.strftime("%H%M%S"))

    with cache_lock:
        check_sym = CACHE_CHECK_SYMBOL if CACHE_CHECK_SYMBOL in history_cache else (
            next(iter(history_cache), None)
        )
        sample_df = history_cache.get(check_sym) if check_sym else None

    expected = _expected_last_session(current_date, now_time)
    if _cache_is_fresh(sample_df, current_date, now_time):
        print(f"  [{ts}] ✅ Cache OK [{check_sym}] ({sample_df.index[-1].date()} ≥ {expected})")
        return True

    reason = "không có dữ liệu" if sample_df is None else f"nến cuối = {sample_df.index[-1].date()}"
    print(f"  [{ts}] ⚠️  Cache STALE ({reason}, kỳ vọng ≥ {expected}) → Rebuild ngay...")
    build_history_cache(symbols, current_date)
    with cache_lock:
        sample_df2 = history_cache.get(check_sym) if check_sym else None
    if sample_df2 is not None:
        new_last = sample_df2.index[-1].date()
        ts2 = datetime.now(TZ_VN).strftime('%H:%M:%S')
        print(f"  [{ts2}] ✅ Sau rebuild [{check_sym}]: nến cuối = {new_last}")
    return False

def fetch_today_bar(symbol: str, current_date: date):
    for attempt in range(3):
        try:
            df_raw = _fetch_vnd(symbol, limit=5)
            if df_raw is None or df_raw.empty: return None
            
            today_rows = df_raw[df_raw.index.date == current_date]
            if today_rows.empty: return None

            row    = today_rows.iloc[-1]
            close  = float(row.get('close',  np.nan))
            open_  = float(row.get('open',   close))
            high   = float(row.get('high',   close))
            low    = float(row.get('low',    close))
            volume = float(row.get('volume', np.nan))

            if pd.isna(close) or close <= 0: return None
            if pd.isna(volume) or volume < 100: return None

            prev_rows = df_raw[df_raw.index.date < current_date]
            if not prev_rows.empty:
                prev = prev_rows.iloc[-1]
                prev_close  = float(prev.get('close',  np.nan))
                prev_volume = float(prev.get('volume', np.nan))
                prev_open   = float(prev.get('open',   np.nan))
                prev_high   = float(prev.get('high',   np.nan))
                prev_low    = float(prev.get('low',    np.nan))

                ohlcv_clone = (
                    close  == prev_close  and open_  == prev_open  and
                    high   == prev_high   and low    == prev_low   and
                    volume == prev_volume
                )
                if ohlcv_clone:
                    print(f"    ⚠️  {symbol}: today_bar OHLCV = phiên trước → bỏ qua")
                    return None

                price_vol_clone = (
                    not pd.isna(prev_close)  and close  == prev_close and
                    not pd.isna(prev_volume) and volume == prev_volume
                )
                if price_vol_clone:
                    print(f"    ⚠️  {symbol}: close+volume = phiên trước → bỏ qua")
                    return None

            high = max(high, open_, close)
            low  = min(low,  open_, close)

            return pd.Series(
                {'open': open_, 'high': high, 'low': low, 'close': close, 'volume': volume},
                name=pd.Timestamp(current_date)
            )
        except Exception as e:
            if attempt < 2: time.sleep(1)
            else: print(f"    ❌ fetch_today_bar {symbol}: {e}")
    return None

def upsert_today_bar(df_hist, today_bar):
    bar_date = pd.Timestamp(today_bar.name).date()
    new_row = pd.DataFrame([today_bar], index=[pd.Timestamp(today_bar.name)])
    ohlcv_cols = ['open', 'high', 'low', 'close', 'volume']
    df_hist = df_hist[df_hist.index.date != bar_date][ohlcv_cols]
    merged = pd.concat([df_hist, new_row]).sort_index()
    merged['vpa_flag'] = calc_vpa_flag(merged)
    return merged

def chart_symbol_status(symbol: str) -> dict:
    """Kiểm tra trạng thái cache cho symbol (không gọi mạng)."""
    symbol = symbol.upper().strip()
    now = datetime.now(TZ_VN)
    current_date = now.date()
    now_time = int(now.strftime("%H%M%S"))

    with cache_lock:
        df_hist = history_cache.get(symbol)
    has_cache = df_hist is not None and len(df_hist) >= 60

    if not has_cache:
        return {"symbol": symbol, "cached": False, "need_fetch": True, "reason": "no_cache"}

    if not _cache_is_fresh(df_hist, current_date, now_time):
        return {"symbol": symbol, "cached": True, "need_fetch": True, "reason": "stale_session"}

    if not _is_trading_session_time(current_date, now_time):
        return {"symbol": symbol, "cached": True, "need_fetch": False, "reason": "outside_session"}

    last_touch = last_bar_update.get(symbol, 0)
    if time.time() - last_touch < BAR_UPDATE_TTL_SEC:
        return {"symbol": symbol, "cached": True, "need_fetch": False, "reason": "recently_updated"}

    return {"symbol": symbol, "cached": True, "need_fetch": True, "reason": "live_update_due"}


def _append_chart_action(current: str, action: str) -> str:
    return action if current == "skip" else f"{current}+{action}"


def ensure_symbol_live_in_cache(symbol: str) -> dict:
    symbol = symbol.upper().strip()
    now = datetime.now(TZ_VN)
    current_date = now.date()
    now_time = int(now.strftime("%H%M%S"))
    result = {"vnstock_action": "skip"}

    with cache_lock:
        df_hist = history_cache.get(symbol)

    if df_hist is None or len(df_hist) < 60:
        result["vnstock_action"] = "fetch_full_history"
        df_hist = load_history_for_symbol(symbol)
        if df_hist is None or len(df_hist) < 60:
            return result
        with cache_lock:
            history_cache[symbol] = df_hist
        return result

    if not _cache_is_fresh(df_hist, current_date, now_time):
        result["vnstock_action"] = "fetch_full_history"
        fresh = load_history_for_symbol(symbol)
        if fresh is not None and len(fresh) >= 60:
            with cache_lock:
                history_cache[symbol] = fresh
            last_bar_update[symbol] = time.time()
            return result

    if not _is_trading_session_time(current_date, now_time):
        return result

    last_touch = last_bar_update.get(symbol, 0)
    if time.time() - last_touch < BAR_UPDATE_TTL_SEC:
        return result

    result["vnstock_action"] = _append_chart_action(result["vnstock_action"], "fetch_today_bar")
    today_bar = fetch_today_bar(symbol, current_date)
    last_bar_update[symbol] = time.time()
    if today_bar is None:
        return result

    with cache_lock:
        latest_hist = history_cache.get(symbol)
        if latest_hist is None or len(latest_hist) < 60:
            latest_hist = df_hist
        history_cache[symbol] = upsert_today_bar(latest_hist, today_bar)
    return result

# =============================================================================
# BƯỚC 5C: HÀM TIỆN ÍCH
# =============================================================================
def _date_str_from_df(df: pd.DataFrame) -> str:
    last_ts = pd.Timestamp(df.index[-1])
    if last_ts.tzinfo is None:
        last_ts = last_ts.tz_localize('Asia/Ho_Chi_Minh')
    return last_ts.strftime('%d/%m/%Y')

# =============================================================================
# BƯỚC 5D: HÀM LẤY DỮ LIỆU CHỈ SỐ
# =============================================================================
def fetch_index_history(symbol: str) -> pd.DataFrame | None:
    for attempt in range(3):
        try:
            df_raw = _fetch_vnd(symbol, limit=1000)
            if df_raw is None or df_raw.empty: return None
            df_raw = df_raw.dropna(subset=['close'])
            if len(df_raw) < 10: return None
            return df_raw
        except Exception as e:
            if attempt < 2: time.sleep(1)
            else: print(f"    ❌ fetch_index_history {symbol}: {e}")
    return None

# =============================================================================
# BƯỚC 5E: HÀM LẤY DỮ LIỆU 15 PHÚT
# =============================================================================
def fetch_intraday_15m(symbol: str) -> pd.DataFrame | None:
    for attempt in range(3):
        try:
            df_raw = _fetch_vnd(symbol, limit=200, resolution="15")
            if df_raw is None or df_raw.empty: return None
            df_raw = df_raw.dropna(subset=['close'])
            if len(df_raw) < 10: return None
            return compute_indicators(df_raw)
        except Exception as e:
            if attempt < 2: time.sleep(1)
            else: print(f"    ❌ fetch_intraday_15m {symbol}: {e}")
    return None

# =============================================================================
# BƯỚC 5F: FETCH FRESH HOÀN TOÀN CHO ON-DEMAND CHART (không dùng cache)
# =============================================================================
def fetch_fresh_for_chart(symbol: str, current_date: date) -> pd.DataFrame | None:
    """Fetch dữ liệu tươi từ server (không qua cache)."""
    for attempt in range(3):
        try:
            df_raw = _fetch_vnd(symbol, limit=1000)
            if df_raw is None or len(df_raw) < 60: return None

            today_rows = df_raw[df_raw.index.date == current_date]
            if not today_rows.empty:
                vol_today = float(today_rows.iloc[-1].get('volume', 0) or 0)
                if vol_today < 100:
                    df_raw = df_raw[df_raw.index.date < current_date]

            if len(df_raw) < 60: return None
            return df_raw
        except Exception as e:
            if attempt < 2: time.sleep(1)
            else: print(f"    ❌ fetch_fresh_for_chart {symbol}: {e}")
    return None

# =============================================================================
# BƯỚC 6: CÁC HÀM ĐIỀU KIỆN TÍN HIỆU
# =============================================================================
def calc_pocket_pivot_vol(df):
    V = df['volume']; C = df['close']
    def down_vol(lv, lc, lcp): return ref(V,lv).where(ref(C,lc) <= ref(C,lcp), 0)
    return (
        (V>down_vol(1,1,2))&(V>down_vol(2,2,3))&(V>down_vol(3,3,4))&
        (V>down_vol(4,4,5))&(V>down_vol(5,5,6))&(V>down_vol(6,6,7))&
        ((V>ref(V,2))|(V>ref(V,1)))&(V>0.8*ref(df['VMA3'],1))
    )

def calc_break_vol(df):
    V = df['volume']
    cond_a = (
        ((V>1.10*df['VMA30'])|(V>1.10*df['VMA50'])|(V>1.15*df['VMA20']))&
        ((V>ref(V,2))|(V>ref(V,1)))&(V>0.9*df['VMA5'])&(V>0.9*ref(df['VMA3'],1))
    )
    cond_b = (
        ((V>1.5*df['VMA30'])|(V>1.5*df['VMA50'])|(V>1.5*df['VMA20']))&
        (V>0.8*ref(df['VMA2'],1))
    )
    return cond_a | cond_b

def calc_wedging(df):
    H,L,C,O,A = df['high'],df['low'],df['close'],df['open'],df['A']
    range5       = ref(hhv(H,5),1) - ref(llv(L,5),1)
    llv5_1       = ref(llv(L,5),1)
    range_close5 = ref(hhv(C,5),1) - ref(llv(C,5),1)
    cond_narrow  = range5/llv5_1 < 0.05
    cond_semi    = (range5/llv5_1<0.06)&(range_close5/llv5_1<0.02)
    ma_a3_1 = ref(A.rolling(3).mean(),1)
    ma_a2_1 = ref(A.rolling(2).mean(),1)
    two_green = (
        (ma_a2_1>0.015)&((O-ref(C,1))>=0)&((ref(O,1)-ref(C,2))>=0)&
        ((ref(O,2)-ref(C,3))>=0)&(ref(C,3)>ref(C,4))&(ref(C,1)>ref(O,1))&
        (ref(C,2)>ref(O,2))&((ref(C,1)-ref(C,2))/ref(C,2)>0.015)&
        ((ref(C,2)-ref(C,3))/ref(C,3)>0.015)&(ref(L,1)>=ref(L,2))&(ref(L,2)>=ref(L,3))
    )
    is_wedging_strong = (ma_a3_1>0.037)|(ma_a2_1>0.04)|two_green
    return cond_narrow|cond_semi|(~is_wedging_strong)

def calc_pocket_pivot_price(df):
    C,O,H,L,V = df['close'],df['open'],df['high'],df['low'],df['volume']
    wedging = calc_wedging(df)
    c1  = C >= 1.015*ref(C,1)
    c2  = ((C>df['MA5'])&calc_break_vol(df)&calc_pocket_pivot_vol(df))|(C>df['MA10'])
    c3  = (C>=df['EMA50'])|(C>=df['EMA30'])|(C>=df['EMA20'])
    c4  = ((df['EMA50']>=ref(df['EMA50'],1))|(df['EMA30']>=ref(df['EMA30'],1))|
            (df['EMA20']>=ref(df['EMA20'],1))|(df['EMA10']>=ref(df['EMA10'],1)))
    c5  = C > (ref(L,1)+ref(H,1))/2
    c6  = (((C>(H+L)/2)&(C>=O))|(((O-ref(C,1))/ref(C,1)>0.02)&((C-ref(C,1))/ref(C,1)>0.02)))
    c7  = ((H-C)/C<0.02)|(((H-C)/C>=0.02)&((C-O)/O>=1.1*(H-C)/C))
    c8  = O <= 1.08*df['MA10']
    c9  = (((O<=0.998*ref(hhv(H,6),2))&((O-df['MA10'])/df['MA10']<0.025))|
            ((O<=0.99*ref(hhv(H,6),2))&((O-df['MA10'])/df['MA10']<0.032))|
            ((O<=0.95*ref(hhv(H,6),2))&((O-df['MA10'])/df['MA10']<0.05))|
            ((O-df['MA10'])/df['MA10']<0.012))
    c10 = (O-df['MA10'])/df['MA10'] < 0.05
    c11 = (O-ref(C,2))/ref(C,2) < 0.1
    c12 = (ref(C,1)-ref(C,2))/ref(C,2) > -0.05
    c13 = (ref(C,1)-ref(df['MA10'],1))/ref(df['MA10'],1) < 0.08
    c14 = (ref(L,1)-ref(df['MA10'],1))/ref(df['MA10'],1) < 0.05
    c15 = ~(((ref(C,1)-ref(C,2))/ref(C,2)<-0.025)&
            ((ref(V,1)-ref(df['VMA50'],1))/ref(df['VMA50'],1)>0.5)&
            ((ref(V,1)-ref(df['VMA30'],1))/ref(df['VMA30'],1)>0.5)&
            (V<0.8*ref(V,1)))
    box1 = (ref(hhv(H,3),1)-ref(llv(L,3),1))/ref(llv(L,5),1) < 0.18
    box2 = (ref(hhv(H,2),1)-ref(llv(L,2),1))/ref(llv(L,2),1) < 0.12
    box3 = (ref(hhv(C,2),1)-ref(llv(C,2),1))/ref(llv(L,2),1) < 0.08
    return c1&c2&c3&c4&c5&c6&c7&c8&c9&c10&c11&c12&c13&c14&c15&box1&box2&box3&wedging

def calc_break_price(df):
    C,O,H,L,V = df['close'],df['open'],df['high'],df['low'],df['volume']
    wedging = calc_wedging(df)
    c1  = C >= 1.015*ref(C,1)
    c2  = (C>df['MA5'])&(C>df['MA10'])&((C>=df['EMA50'])|(C>=df['EMA30'])|(C>=df['EMA20']))
    c3  = ((df['EMA50']>=ref(df['EMA50'],1))|(df['EMA30']>=ref(df['EMA30'],1))|
            (df['EMA20']>=ref(df['EMA20'],1))|(df['EMA10']>=ref(df['EMA10'],1)))
    c4  = C > (ref(L,1)+ref(H,1))/2
    c5  = (ref(L,1)-ref(df['MA10'],1))/ref(df['MA10'],1) < 0.0825
    c6  = (ref(C,1)-ref(df['MA10'],1))/ref(df['MA10'],1) < 0.0825
    c7  = (O-df['MA10'])/df['MA10'] < 0.0825
    c8  = (O-ref(C,2))/ref(C,2) < 0.1
    c9  = (((C>(H+L)/2)&(C>=O))|(((O-ref(C,1))/ref(C,1)>0.02)&((C-ref(C,1))/ref(C,1)>0.02)))
    c10 = ((H-C)/C<0.02)|(((H-C)/C>=0.02)&((C-O)/O>=1.1*(H-C)/C))
    c11 = ~(((ref(C,1)-ref(C,2))/ref(C,2)<-0.025)&
            ((ref(V,1)-ref(df['VMA50'],1))/ref(df['VMA50'],1)>0.5)&
            ((ref(V,1)-ref(df['VMA30'],1))/ref(df['VMA30'],1)>0.5)&
            (V<0.8*ref(V,1)))
    box1 = (ref(hhv(H,3),1)-ref(llv(L,3),1))/ref(llv(L,5),1) < 0.18
    box2 = (ref(hhv(H,2),1)-ref(llv(L,2),1))/ref(llv(L,2),1) < 0.12
    box3 = (ref(hhv(C,2),1)-ref(llv(C,2),1))/ref(llv(L,2),1) < 0.08
    return c1&c2&c3&c4&c5&c6&c7&c8&c9&c10&c11&box1&box2&box3&wedging

def calc_prebreak_vol(df, now_time):
    V   = df['volume']
    V1  = ref(V,1); V2 = ref(V,2)
    pct = (df['close']-ref(df['close'],1))/ref(df['close'],1)
    def make_cond(vma_n, v_lo_mult, big_vma, big_v2):
        normal = (
            (pct<0.1)&
            ((V>vma_n*df['VMA30'])|(V>vma_n*df['VMA50'])|(V>vma_n*df['VMA20']))&
            ((V>v_lo_mult*V2)|(V>v_lo_mult*V1))&
            (V>vma_n*0.8*df['VMA5'])&(V>vma_n*0.8*ref(df['VMA3'],1))
        )
        big = (
            ((V>big_vma*df['VMA30'])|(V>big_vma*df['VMA50'])|(V>big_vma*df['VMA20']))&
            (V>big_v2*ref(df['VMA2'],1))
        )
        return normal | big
    if   now_time < 93000:  return make_cond(0.30,0.25,0.40,0.20)
    elif now_time < 100000: return make_cond(0.40,0.35,0.60,0.30)
    elif now_time < 103000: return make_cond(0.50,0.45,0.80,0.40)
    elif now_time < 113000: return make_cond(0.80,0.70,1.10,0.60)
    elif now_time < 133000: return make_cond(0.95,0.80,1.25,0.70)
    else:                   return make_cond(1.05,0.90,1.40,0.75)

def calc_liquidity(df):
    C, V = df['close'], df['volume']
    return (
        (C>=5)&(C*V>2_000_000)&(df['MA10']*df['VMA10']>2_000_000)&
        (df['MA15']*df['VMA15']>2_000_000)&(df['RSI14']>=29)&
        (df['VMA10']>=50_000)&(df['VMA20']>=50_000)&
        (df['VMA30']>=50_000)&(df['VMA50']>=50_000)
    )

def calc_dmbuy(df):
    C, V = df['close'], df['volume']
    return (
        (C > 5) &
        (df['VMA30'] >= 50_000) &
        (df['VMA20'] >= 50_000) &
        (df['VMA10'] >= 50_000) &
        (df['VMA50'] >= 50_000) &
        (V * C > 2_000_000) &
        (df['VMA5'] * df['MA5'] > 2_000_000) &
        (df['VMA10'] * df['MA10'] > 2_000_000) &
        (df['VMA15'] * df['MA15'] > 2_000_000)
    )

def _macd_buy_on_frame(df):
    if df is None or len(df) < 3:
        return pd.Series(False, index=df.index if df is not None else [])
    macd_cross_signal = afl_cross(df['MACD'], df['MACD_Signal'])
    macd_cross_zero = afl_cross(df['MACD'], 0)
    mb = macd_cross_signal | macd_cross_zero
    return mb | mb.shift(1, fill_value=False)

def _expand_signal_to_daily(frame_signal, daily_index, freq):
    if frame_signal is None or frame_signal.empty:
        return pd.Series(False, index=daily_index)
    signal_by_period = {
        period: bool(value)
        for period, value in zip(frame_signal.index.to_period(freq), frame_signal.astype(bool))
    }
    daily_periods = daily_index.to_period(freq)
    return pd.Series([signal_by_period.get(period, False) for period in daily_periods], index=daily_index)

def calc_macdbuy_signals(df_daily):
    df_d = compute_indicators(df_daily)
    dmbuy = calc_dmbuy(df_d).iloc[-1]
    if not dmbuy:
        return []
    df_w = build_weekly_df(df_daily)
    df_m = build_monthly_df(df_daily)
    wmbuy_series = _expand_signal_to_daily(_macd_buy_on_frame(df_w), df_d.index, 'W-FRI')
    mmbuy_series = _expand_signal_to_daily(_macd_buy_on_frame(df_m), df_d.index, 'M')
    wmbuy = bool(wmbuy_series.iloc[-1])
    mmbuy = bool(mmbuy_series.iloc[-1])
    signals = []
    if wmbuy:
        signals.append("MACD_W")
    if mmbuy:
        signals.append("MACD_M")
    return signals

def calc_rtmbuy(df_daily):
    df_d = compute_indicators(df_daily)
    if not bool(calc_dmbuy(df_d).iloc[-1]):
        return False
    df_w = build_weekly_df(df_daily)
    if len(df_w) < 6:
        return False
    C, O, H = df_w['close'], df_w['open'], df_w['high']
    rtm = (
        (ref(C, 1) < ref(hhv(H, 5), 2)) &
        (ref(H, 1) < 1.05 * ref(hhv(H, 5), 2)) &
        (ref(C, 2) < ref(hhv(H, 4), 3)) &
        (ref(C, 3) < ref(hhv(H, 3), 4)) &
        (C > 0.97 * ref(hhv(H, 5), 1)) &
        (C > O) &
        (C > 0.94 * hhv(H, 5)) &
        (C < 1.15 * ref(hhv(H, 5), 1)) &
        (C > df_w['EMA10']) &
        (df_w['EMA10'] > df_w['EMA50']) &
        (df_w['EMA20'] > df_w['EMA50'])
    )
    wrtm = rtm | rtm.shift(1, fill_value=False)
    wrtm_daily = _expand_signal_to_daily(wrtm, df_d.index, 'W-FRI')
    return bool(wrtm_daily.iloc[-1])

def detect_momentum_signals(df_daily):
    signals = []
    signals.extend(calc_macdbuy_signals(df_daily))
    if calc_rtmbuy(df_daily):
        signals.append("RTM")
    return signals

# =============================================================================
# BƯỚC 6B: CÁC TÍN HIỆU MỚI
# =============================================================================
def calc_bottomfish(df):
    C, V = df['close'], df['volume']
    r    = df['RSI14']
    H, L = df['high'], df['low']
    rsi_cross = cross_above(r, pd.Series(29, index=r.index)) | \
                cross_above(r, pd.Series(30, index=r.index))
    range30    = (hhv(H, 30) - llv(L, 30)) / llv(L, 30)
    cond_range = range30 >= 0.2
    liq = (
        (C >= 5) & (C * V > 2_000_000) &
        (df['MA5']  * df['VMA5']  > 3_000_000) &
        (df['MA10'] * df['VMA10'] > 3_000_000) &
        (df['MA15'] * df['VMA15'] > 3_000_000) &
        (df['VMA30'] >= 50_000) & (df['VMA20'] >= 50_000) &
        (df['VMA10'] >= 50_000) & (df['VMA50'] >= 50_000)
    )
    return rsi_cross & cond_range & liq

def calc_bottombreakp(df):
    C, O, H, L, V = df['close'], df['open'], df['high'], df['low'], df['volume']
    r = df['RSI14']
    rsi_cross_today = cross_above(r, pd.Series(29, index=r.index))
    rsi_cross_prev  = cross_above(r.shift(1), pd.Series(29, index=r.index))
    rsi_cond        = rsi_cross_today | rsi_cross_prev
    high_close_bar  = (
        ((C > (H + L) / 2) & (C >= O)) |
        (((O - ref(C, 1)) / ref(C, 1) > 0.02) & ((C - ref(C, 1)) / ref(C, 1) > 0.02))
    )
    short_wick  = (
        ((H - C) / C < 0.02) |
        (((H - C) / C >= 0.02) & ((C - O) / O >= 1.1 * (H - C) / C))
    )
    price_cond  = (C >= 1.015 * ref(C, 1)) & (C > (ref(L, 1) + ref(H, 1)) / 2)
    range30     = (hhv(H, 30) - llv(L, 30)) / llv(L, 30)
    cond_range  = range30 >= 0.2
    bvol        = calc_break_vol(df)
    liq = (
        (C >= 5) & (C * V > 3_000_000) &
        (df['MA5']  * df['VMA5']  > 3_000_000) &
        (df['MA10'] * df['VMA10'] > 3_000_000) &
        (df['MA15'] * df['VMA15'] > 3_000_000) &
        (df['VMA30'] >= 50_000) & (df['VMA20'] >= 50_000) &
        (df['VMA10'] >= 50_000) & (df['VMA50'] >= 50_000)
    )
    return rsi_cond & high_close_bar & short_wick & price_cond & cond_range & bvol & liq

def _session_time_progress(now_time: int) -> float:
    """
    Dịch lại y hệt biến Ti2 trong code AmiBroker gốc: tỉ lệ thời gian giao dịch đã
    trôi qua trong phiên hiện tại (0-1), dùng để chuẩn hoá khối lượng khớp lệnh tính
    đến thời điểm hiện tại về khối lượng kỳ vọng cho cả ngày (tổng 240 phút = phiên
    sáng 9h00-11h30 + phiên chiều 13h00-14h45, quy tròn về mốc gốc của AFL).
    Trả về 1.0 (không điều chỉnh) nếu ngoài giờ giao dịch (trước 9h00 hoặc sau 14h30) —
    giống điều kiện `Now(4)<090000 OR Now(4)>143000` trong AFL.
    """
    if now_time < 90000 or now_time > 143000:
        return 1.0
    hh = now_time // 10000
    mm = (now_time // 100) % 100
    hh_morning = hh if hh < 12 else 11
    morning_minutes_from_hour = (hh_morning - 9) * 60
    morning_minutes = 30 if now_time > 113100 else mm
    total_morning = morning_minutes_from_hour + morning_minutes
    hh_afternoon = 0 if hh < 14 else 1
    afternoon_minutes_from_hour = hh_afternoon * 60
    afternoon_minutes = mm if now_time >= 130000 else 0
    total_afternoon = afternoon_minutes_from_hour + afternoon_minutes
    total_minutes = total_morning + total_afternoon
    # Bảo vệ chia-cho-0 ở đúng mốc 09:00:00 (Tigd=0 trong AFL gốc cũng rơi vào biên này,
    # nhưng AFL không chia cho 0 nhờ may mắn của thứ tự đánh giá; ở đây chặn tường minh).
    return (total_minutes / 240) if total_minutes > 0 else (1.0 / 240)

def calc_attent(df, now_time):
    """
    ATTENT — danh sách "đáng chú ý": thanh khoản đủ tốt, giá tăng >0.5% so với hôm
    trước, và ít nhất 1 trong 2 nhóm điều kiện sau đúng:
      • Xu hướng trung hạn đang đi lên (MA50/MA30/MA20 tăng 2 phiên liên tiếp và nằm
        trên MA200), HOẶC
      • Khối lượng đang khớp nhanh hơn kỳ vọng cùng thời điểm trong ngày (so với
        trung bình 30/50 phiên, đã chuẩn hoá theo % thời gian phiên đã trôi qua) và
        giá đang trên MA10 — tức dòng tiền vào sớm hơn/mạnh hơn bình thường.
    Dịch lại 1:1 biến ATTENT trong code AmiBroker gốc.
    """
    C, V = df['close'], df['volume']
    liq = (
        (C >= 5) &
        (df['VMA30'] >= 50_000) & (df['VMA20'] >= 50_000) &
        (df['VMA10'] >= 50_000) & (df['VMA50'] >= 50_000) &
        (df['MA5']  * df['VMA5']  > 2_000_000) &
        (df['MA10'] * df['VMA10'] > 2_000_000) &
        (df['MA15'] * df['VMA15'] > 2_000_000) &
        (df['MA20'] * df['VMA20'] > 2_000_000)
    )
    price_up = (C / ref(C, 1)) > 1.005
    ti2 = _session_time_progress(now_time)
    vol_pace_50 = ((V / df['VMA50']) / ti2) > 1.2
    vol_pace_30 = ((V / df['VMA30']) / ti2) > 1.2
    mid_term_up = (
        ((df['MA50'] >= ref(df['MA50'], 1)) & (df['MA50'] >= ref(df['MA50'], 2)) & (df['MA50'] >= df['MA200'])) |
        ((df['MA30'] >= ref(df['MA30'], 1)) & (df['MA30'] >= ref(df['MA30'], 2)) & (df['MA30'] >= df['MA200'])) |
        ((df['MA20'] >= ref(df['MA20'], 1)) & (df['MA20'] >= ref(df['MA20'], 2)) & (df['MA20'] >= df['MA200'])) |
        (vol_pace_50 & (C > df['MA10'])) |
        (vol_pace_30 & (C > df['MA10']))
    )
    return liq & price_up & mid_term_up

def calc_breakvol_signal(df, now_time):
    """
    BREAKVOL — cảnh báo khối lượng "nổ" sớm trong phiên: khối lượng khớp tính đến
    thời điểm hiện tại, sau khi chuẩn hoá theo % thời gian phiên đã trôi qua, đang
    vượt 1.2 lần trung bình khối lượng 50 phiên, đồng thời giá đang tăng so với hôm
    trước. Dịch lại 1:1 biến BREAKVOL trong code AmiBroker gốc (không kèm điều kiện
    thanh khoản/liquidity — đúng như bản gốc chỉ xét khối lượng + giá tăng).
    """
    C, V = df['close'], df['volume']
    ti2 = _session_time_progress(now_time)
    vol_pace = ((V / df['VMA50']) / ti2) > 1.2
    return vol_pace & (C > ref(C, 1))

def calc_ma_cross(df):
    C, V = df['close'], df['volume']
    ma_cross_cond = (
        cross_above(df['MA10'], df['MA20']) |
        cross_above(df['MA10'], df['MA30']) |
        cross_above(df['MA10'], df['MA50'])
    )
    ma_above_200 = (
        (df['MA10'] > df['MA200']) &
        (df['MA30'] > df['MA200']) &
        (df['MA50'] > df['MA200'])
    )
    price_cond = (C > df['MA30']) & (C <= 1.07 * df['MA30'])
    liq = (
        (C >= 5) & (C * V > 2_000_000) &
        (df['MA5']  * df['VMA5']  > 3_000_000) &
        (df['MA10'] * df['VMA10'] > 3_000_000) &
        (df['MA15'] * df['VMA15'] > 3_000_000) &
        (df['VMA30'] >= 50_000) & (df['VMA20'] >= 50_000) &
        (df['VMA10'] >= 50_000) & (df['VMA50'] >= 50_000)
    )
    return ma_cross_cond & ma_above_200 & price_cond & liq

# =============================================================================
# BƯỚC 6C: HÀM DETECT_SIGNAL
# =============================================================================
def detect_signal(df, now_time):
    df = compute_indicators(df)
    if len(df) < 60: return None
    liq         = calc_liquidity(df)
    break_price = calc_break_price(df)
    break_vol   = calc_break_vol(df)
    pprice      = calc_pocket_pivot_price(df)
    pvol        = calc_pocket_pivot_vol(df)
    ma10_ok     = df['MA10'] >= 0.8*ref(df['MA10'],1)
    pre_vol     = calc_prebreak_vol(df, now_time)

    is_breakout = (break_price & break_vol & liq).iloc[-1]
    is_pocket   = (pprice & (pvol | break_vol) & liq & ma10_ok).iloc[-1]
    is_prebreak = (
        ((break_price | pprice) & pre_vol & liq).iloc[-1] and
        not is_breakout and not is_pocket and
        (91700 < now_time < 150000)
    )
    is_bottombreakp = calc_bottombreakp(df).iloc[-1]
    is_bottomfish   = calc_bottomfish(df).iloc[-1]
    is_ma_cross     = calc_ma_cross(df).iloc[-1]

    if is_breakout:     return 'BREAKOUT'
    if is_pocket:       return 'POCKET PIVOT'
    if is_prebreak:     return 'PRE-BREAK'
    if is_bottombreakp: return 'BOTTOMBREAKP'
    if is_ma_cross:     return 'MA_CROSS'
    if is_bottomfish:   return 'BOTTOMFISH'
    return None

# =============================================================================
# BƯỚC 7: VẼ BIỂU ĐỒ
# =============================================================================
_draw_pool = ProcessPoolExecutor(max_workers=1)  # vẽ chart (matplotlib) ở process riêng, khỏi tranh GIL với Flask

def draw_chart(df_plot, symbol, signal_type, today, timeframe='Daily', add_arrow=True, date_str=None, as_bytes=False):
    is_daily  = (timeframe == 'Daily')
    is_weekly = (timeframe == 'Weekly')
    is_15m    = (timeframe == '15m')

    if date_str is None:
        date_str = _date_str_from_df(df_plot)

    prev_close = df_plot['close'].iloc[-2]
    pct        = (today['close'] - prev_close) / prev_close * 100

    hist_val    = df_plot['MACD_Hist'].values
    macd_colors = []
    for i, val in enumerate(hist_val):
        prev = hist_val[i-1] if i > 0 else 0
        if val >= 0: macd_colors.append('#26A69A' if val >= prev else '#B2DFDB')
        else:        macd_colors.append('#EF5350' if val <= prev else '#FFCDD2')

    colors_vol = ['#26A69A' if r['close'] >= r['open'] else '#EF5350'
                  for _, r in df_plot.iterrows()]

    apds = [
        mpf.make_addplot(df_plot['EMA10'],       color='red',    width=0.6),
        mpf.make_addplot(df_plot['EMA20'],       color='green',  width=0.6),
        mpf.make_addplot(df_plot['EMA50'],       color='purple', width=0.6),
        mpf.make_addplot(df_plot['volume'],      type='bar', panel=1, color=colors_vol, alpha=1.0),
        mpf.make_addplot(df_plot['MACD_Hist'],   type='bar', panel=2, color=macd_colors, secondary_y=False),
        mpf.make_addplot(df_plot['MACD'],        panel=2, color='blue',   width=0.6, secondary_y=False),
        mpf.make_addplot(df_plot['MACD_Signal'], panel=2, color='orange', width=0.6, secondary_y=False),
    ]

    if is_daily:
        apds.append(mpf.make_addplot(df_plot['MA200'],  color='brown', width=0.6))
    if is_15m:
        apds.append(mpf.make_addplot(df_plot['EMA200'], color='brown', width=0.6))

    mc           = mpf.make_marketcolors(up='#26A69A',down='#EF5350',edge='inherit',wick='inherit',alpha=1.0)
    custom_style = mpf.make_mpf_style(base_mpf_style='charles',marketcolors=mc,gridstyle='',facecolor='white')

    img_name = None
    if not as_bytes:
        fd, img_name = tempfile.mkstemp(suffix=f'_{symbol}_{timeframe.lower()}.png')
        os.close(fd)

    fig, axlist = mpf.plot(
        df_plot, type='candle', volume=False, addplot=apds,
        style=custom_style,
        figratio=(16,9), returnfig=True, show_nontrading=False, tight_layout=True
    )
    ax_price = axlist[0]
    ax_price.yaxis.set_label_position("right"); ax_price.yaxis.tick_right()
    ax_price.set_ylabel(""); ax_price.tick_params(axis='y', labelsize=8)
    y_min, y_max = ax_price.get_ylim()
    ax_price.set_ylim(y_min, y_max + (y_max-y_min)*0.15)

    if is_daily and add_arrow:
        ax_price.annotate(r'$\mathbf{\uparrow}$',
            xy=(len(df_plot)-1, today['low']), xytext=(0,-8), textcoords='offset points',
            ha='center', va='top', color='DeepPink', fontsize=12)

    if is_daily:
        title_str = (
            f"{symbol} [D] {date_str}  |  "
            f"O:{today['open']:.2f}  H:{today['high']:.2f}  "
            f"L:{today['low']:.2f}  C:{today['close']:.2f} ({pct:+.2f}%) \n\n"
            f"{signal_type}"
        )
    elif is_weekly:
        title_str = (
            f"{symbol} [W] {date_str}  | "
            f"O:{today['open']:.2f}  H:{today['high']:.2f}  "
            f"L:{today['low']:.2f}  C:{today['close']:.2f} ({pct:+.2f}%)"
        )
    else:  # 15m
        last_ts = pd.Timestamp(df_plot.index[-1])
        if last_ts.tzinfo is None:
            last_ts = last_ts.tz_localize('Asia/Ho_Chi_Minh')
        date_str_15m = last_ts.strftime('%d/%m/%Y %H:%M')
        title_str = (
            f"{symbol} [15m] {date_str_15m}  | "
            f"O:{today['open']:.2f}  H:{today['high']:.2f}  "
            f"L:{today['low']:.2f}  C:{today['close']:.2f} ({pct:+.2f}%)"
        )

    ax_price.set_title(title_str, loc='left', fontsize=11)

    if len(axlist) > 4:
        ax_macd = axlist[4]
        ax_macd.yaxis.set_ticks([]); ax_macd.yaxis.set_ticklabels([])
        m_vals = pd.concat([df_plot['MACD'],df_plot['MACD_Signal'],df_plot['MACD_Hist']]).dropna()
        if len(m_vals) == 0 or m_vals.empty:
            abs_m = 1
        else:
            try:
                abs_m = max(abs(m_vals.min()), abs(m_vals.max()))
                if abs_m == 0 or np.isnan(abs_m): abs_m = 1
            except Exception:
                abs_m = 1
        ax_macd.set_ylim(-abs_m*0.8, abs_m*1.2)
        for spine in ['top','right','left','bottom']: ax_macd.spines[spine].set_visible(False)

    for i, ax in enumerate(axlist):
        if i not in [0,4]: ax.set_axis_off()
        else: ax.xaxis.set_visible(False); ax.spines['top'].set_visible(False); ax.spines['left'].set_visible(False)

    xlim = ax_price.get_xlim()
    ax_price.set_xlim(xlim[0], xlim[1]+20)
    if as_bytes:
        buf = BytesIO()
        fig.savefig(buf, format='png', bbox_inches='tight', pad_inches=0.15, dpi=150)
        png_bytes = buf.getvalue()
        buf.close()
        plt.close('all')
        return png_bytes
    fig.savefig(img_name, bbox_inches='tight', pad_inches=0.15, dpi=150)
    plt.close('all')
    return img_name


def _build_15m_chart(symbol: str, signal_type: str, via: str = "telegram_15m") -> str | None:
    df_15m = fetch_intraday_15m(symbol)
    if df_15m is None or len(df_15m) < 2:
        print(f"  ⚠️  {symbol}: không có dữ liệu 15m")
        return None
    today_15m    = df_15m.iloc[-1]
    date_str_15m = _date_str_from_df(df_15m)
    print(f"  [Chart] {symbol} | cache_state=intraday_15m | action=fetch_intraday_15m | source=vnstock_15m | last_bar={df_15m.index[-1]} | via={via}")
    return draw_chart(
        df_15m.tail(200).copy(), symbol, signal_type, today_15m,
        timeframe='15m', add_arrow=False, date_str=date_str_15m
    )

# =============================================================================
# BƯỚC 8: HÀM QUÉT 1 CHU KỲ
# =============================================================================
SIGNAL_RANK  = {
    'PRE-BREAK':    1,
    'BOTTOMFISH':   2,
    'MA_CROSS':     3,
    'BOTTOMBREAKP': 4,
    'POCKET PIVOT': 5,
    'BREAKOUT':     6,
}
SIGNAL_EMOJI = {
    'BREAKOUT':     '🟢',
    'POCKET PIVOT': '🟡',
    'PRE-BREAK':    '🟣',
    'BOTTOMFISH':   '🟠',
    'BOTTOMBREAKP': '🔵',
    'MA_CROSS':     '⚪',
}

def run_scan_cycle(symbols: list, now_time: int, alerted_today: dict, momentum_today: dict,
                    attent_today: dict = None, breakvol_today: dict = None):
    new_signals  = []
    current_momentum = {}
    current_attent   = {}
    current_breakvol = {}
    current_date = datetime.now(TZ_VN).date()
    ts           = datetime.now(TZ_VN).strftime('%H:%M:%S')
    print(f"  [{ts}] Bắt đầu update {len(cache_symbol_set)} mã, quét {len(symbols)} mã (VNDirect)...")

    for symbol in cache_symbol_set:
        try:
            with cache_lock: df_hist = history_cache.get(symbol)
            if df_hist is None or len(df_hist) < 60: continue

            today_bar = fetch_today_bar(symbol, current_date)
            if today_bar is None: continue

            with cache_lock:
                latest = upsert_today_bar(history_cache[symbol], today_bar)
                history_cache[symbol] = latest
                df_merged = latest.copy()
            last_bar_update[symbol] = time.time()

            # Chỉ phát tín hiệu cho các mã trong danh sách symbols (symbols_to_scan)
            if symbol not in symbols:
                continue

            try:
                momentum_signals = detect_momentum_signals(df_merged)
                if momentum_signals:
                    # mom_pct chỉ dùng cột 'close' gốc — không cần compute_indicators
                    mom_pct = (df_merged['close'].iloc[-1] - df_merged['close'].iloc[-2]) / df_merged['close'].iloc[-2] * 100
                    current_momentum[symbol] = {"signals": momentum_signals, "pct": round(mom_pct, 1)}
            except Exception as e:
                print(f"    ⚠️  Momentum {symbol}: {e}")

            try:
                # ATTENT/BREAKVOL cần MA/VMA/RSI — tính compute_indicators 1 lần dùng chung cả 2
                df_ind = compute_indicators(df_merged)
                if len(df_ind) >= 60:
                    pct_today = (df_ind['close'].iloc[-1] - df_ind['close'].iloc[-2]) / df_ind['close'].iloc[-2] * 100
                    if bool(calc_attent(df_ind, now_time).iloc[-1]):
                        current_attent[symbol] = {"pct": round(pct_today, 1)}
                    if bool(calc_breakvol_signal(df_ind, now_time).iloc[-1]):
                        current_breakvol[symbol] = {"pct": round(pct_today, 1)}
            except Exception as e:
                print(f"    ⚠️  ATTENT/BREAKVOL {symbol}: {e}")

            signal_type = detect_signal(df_merged, now_time)
            if not signal_type:
                continue

            prev_entry = alerted_today.get(symbol)
            prev_sig   = prev_entry["signal"] if isinstance(prev_entry, dict) else prev_entry
            prev_rank  = SIGNAL_RANK.get(prev_sig, 0)
            current_rank = SIGNAL_RANK.get(signal_type, 0)
            if prev_rank >= current_rank:
                continue

            df_calc      = compute_indicators(df_merged)
            today        = df_calc.iloc[-1]
            date_str     = _date_str_from_df(df_calc)
            pct          = (today['close']-df_calc['close'].iloc[-2])/df_calc['close'].iloc[-2]*100
            change       = today['close'] - df_calc['close'].iloc[-2]
            emoji        = SIGNAL_EMOJI.get(signal_type, '📌')
            vol_vs_prev  = (today['volume']-df_calc['volume'].iloc[-2])/df_calc['volume'].iloc[-2]*100
            vol_vs_vma50 = (today['volume']-today['VMA50'])/today['VMA50']*100 if today['VMA50']>0 else 0

            alerted_today[symbol] = {"signal": signal_type, "pct": round(pct, 1)}
            new_signals.append(symbol)

            link_vnd_detail  = f"https://dstock.vndirect.com.vn/tong-quan/{symbol}/diem-nhan-co-ban-popup"
            link_vnd_news    = f"https://dstock.vndirect.com.vn/tong-quan/{symbol}/tin-tuc-ma-popup?type=dn"
            link_vietstock   = f"https://stockchart.vietstock.vn/?stockcode={symbol}"
            link_vnd_summary = f"https://dstock.vndirect.com.vn/tong-quan/{symbol}"
            link_24h_money   = f"https://24hmoney.vn/stock/{symbol}/news"

            msg = (
                f"{emoji} #{symbol}  {date_str} \n"
                f"Sig: {signal_type} \n"
                f"Clo: <b>{today['close']:.2f}</b> ({change:+.2f} / {pct:+.2f}%)\n"
                f"Vol: {vol_vs_prev:+.1f}% | {vol_vs_vma50:+.1f}% \n"
                f"<a href='{link_vnd_detail}'>⚖️</a> "
                f"<a href='{link_vnd_news}'>🗞️</a> "
                f"<a href='{link_vietstock}'>📈</a> "
                f"<a href='{link_vnd_summary}'>📄</a> "
                f"<a href='{link_24h_money}'>📄</a>"
            )

            df_plot_d  = df_calc.tail(250).copy()
            img_daily  = _draw_pool.submit(draw_chart, df_plot_d, symbol, signal_type, today,
                                            'Daily', True, date_str).result()
            df_weekly  = build_weekly_df(df_merged)
            df_plot_w  = df_weekly.tail(200).copy()
            today_w    = df_plot_w.iloc[-1]
            date_str_w = _date_str_from_df(df_merged)
            img_weekly = _draw_pool.submit(draw_chart, df_plot_w, symbol, signal_type, today_w,
                                            'Weekly', False, date_str_w).result()
            img_15m    = _draw_pool.submit(_build_15m_chart, symbol, signal_type, "scanner_signal_15m").result()

            image_paths = [img_daily, img_weekly]
            if img_15m: image_paths.append(img_15m)

            notify_text = f"{emoji} #{symbol} | {signal_type} | {date_str}"
            send_telegram_signal(msg, image_paths=image_paths, notify_text=notify_text)

        except Exception as e:
            print(f"  ❌ Lỗi mã {symbol}: {e}")

    momentum_today.clear()
    momentum_today.update(current_momentum)
    if attent_today is not None:
        attent_today.clear()
        attent_today.update(current_attent)
    if breakvol_today is not None:
        breakvol_today.clear()
        breakvol_today.update(current_breakvol)
    return new_signals

# =============================================================================
# BƯỚC 8B: PARSE LỆNH CHART
# =============================================================================
_RESERVED_KEYWORDS = {'s','help','h','scan','c','chart','heatmap','start'}

def parse_chart_command(text: str):
    text = text.strip()
    if not text.startswith('/'): return None
    body = text[1:]

    m = re.match(r'^(c|chart)\s+(.+)$', body, re.IGNORECASE)
    if m: return _filter_symbols(m.group(2).split())

    if body.startswith(' '): return _filter_symbols(body.strip().split())

    parts = body.strip().split()
    if len(parts) == 1:
        candidate = parts[0].upper()
        if candidate in INDEX_SYMBOLS:
            return [candidate]
        if candidate.lower() not in _RESERVED_KEYWORDS and _is_valid_symbol(candidate):
            return [candidate]
    return None

def _is_valid_symbol(s: str) -> bool:
    s_upper = s.upper()
    if s_upper in INDEX_SYMBOLS:
        return True
    return bool(re.match(r'^[A-Z0-9]{1,5}$', s_upper)) and s.lower() not in _RESERVED_KEYWORDS

def _filter_symbols(raw_list: list):
    result = [s.upper() for s in raw_list if _is_valid_symbol(s)]
    return result if result else None

# =============================================================================
# BƯỚC 8C: PIPELINE CHART DÙNG CHUNG — DASHBOARD + TELEGRAM
# =============================================================================
def _get_chart_context(symbol: str):
    symbol = symbol.upper().strip()
    now_obj      = datetime.now(TZ_VN)
    current_date = now_obj.date()
    now_time     = int(now_obj.strftime("%H%M%S"))
    is_index = symbol in INDEX_SYMBOLS
    trace = {
        "cache_state": "index" if is_index else "unknown",
        "vnstock_action": "fetch_index_history" if is_index else "skip",
        "source": "index_api" if is_index else "unknown",
    }

    if is_index:
        df_raw = fetch_index_history(symbol)
    else:
        pre_status = chart_symbol_status(symbol)
        trace["cache_state"] = pre_status.get("reason", "unknown")
        # Dùng chung cơ chế "vá nến hôm nay nếu quá BAR_UPDATE_TTL_SEC" với thẻ CHART
        # (ensure_symbol_live_in_cache), thay vì chỉ kiểm tra tươi theo NGÀY như trước.
        # Nhờ vậy mã NGOÀI danh sách quét (không được run_scan_cycle() chạm tới) khi
        # xem qua scanner chart / Telegram cũng được vá giá/khối lượng mới nhất trong
        # phiên, không còn bị coi là "fresh" chỉ vì nến cuối cùng ngày với hôm nay.
        ensure_result = ensure_symbol_live_in_cache(symbol)
        trace["vnstock_action"] = ensure_result.get("vnstock_action", "skip")
        with cache_lock:
            cached = history_cache.get(symbol)
            df_raw = cached.copy() if cached is not None and len(cached) >= 10 else None
        if df_raw is not None:
            trace["source"] = "history_cache"
        if df_raw is None:
            trace["vnstock_action"] = _append_chart_action(trace["vnstock_action"], "fetch_fresh_fallback")
            trace["source"] = "fresh_fetch"
            df_raw = fetch_fresh_for_chart(symbol, current_date)
            # Ghi ngược vào history_cache dùng chung để các nơi khác (thẻ CHART, quét tín
            # hiệu) không phải tự fetch lại từ đầu ở lần gọi kế tiếp.
            if df_raw is not None and len(df_raw) >= 60:
                with cache_lock:
                    history_cache[symbol] = df_raw.copy()

    if df_raw is None or len(df_raw) < 10:
        return None

    df_calc = compute_indicators(df_raw)
    today = df_calc.iloc[-1]
    signal_type = "INDEX" if is_index else (detect_signal(df_raw, now_time) or "ON-DEMAND")
    return {
        "symbol": symbol,
        "is_index": is_index,
        "source": trace["source"],
        "cache_state": trace["cache_state"],
        "vnstock_action": trace["vnstock_action"],
        "df_raw": df_raw,
        "df_calc": df_calc,
        "today": today,
        "signal_type": signal_type,
        "date_str": _date_str_from_df(df_calc),
    }

def _format_chart_trace(ctx):
    return (
        f"cache_state={ctx.get('cache_state', 'unknown')} | "
        f"action={ctx.get('vnstock_action', 'unknown')} | "
        f"source={ctx.get('source', 'unknown')}"
    )

def _build_chart_message(ctx):
    symbol = ctx["symbol"]
    df_calc = ctx["df_calc"]
    today = ctx["today"]
    date_str = ctx["date_str"]
    pct = (today['close'] - df_calc['close'].iloc[-2]) / df_calc['close'].iloc[-2] * 100
    change = today['close'] - df_calc['close'].iloc[-2]
    vol_vs_prev = (today['volume'] - df_calc['volume'].iloc[-2]) / df_calc['volume'].iloc[-2] * 100
    vol_vs_vma50 = (today['volume'] - today['VMA50']) / today['VMA50'] * 100 if today['VMA50'] > 0 else 0

    if ctx["is_index"]:
        return (
            f"#{symbol}  {date_str}\n"
            f"Clo: <b>{today['close']:.2f}</b> ({change:+.2f} / {pct:+.2f}%)\n"
            f"Vol: {vol_vs_prev:+.1f}% | {vol_vs_vma50:+.1f}%"
        )

    link_vnd_detail  = f"https://dstock.vndirect.com.vn/tong-quan/{symbol}/diem-nhan-co-ban-popup"
    link_vnd_news    = f"https://dstock.vndirect.com.vn/tong-quan/{symbol}/tin-tuc-ma-popup?type=dn"
    link_vietstock   = f"https://stockchart.vietstock.vn/?stockcode={symbol}"
    link_vnd_summary = f"https://dstock.vndirect.com.vn/tong-quan/{symbol}"
    link_24h_money   = f"https://24hmoney.vn/stock/{symbol}/news"
    return (
        f"#{symbol}  {date_str}\n"
        f"Sig: {ctx['signal_type']}\n"
        f"Clo: <b>{today['close']:.2f}</b> ({change:+.2f} / {pct:+.2f}%)\n"
        f"Vol: {vol_vs_prev:+.1f}% | {vol_vs_vma50:+.1f}%\n"
        f"<a href='{link_vnd_detail}'>⚖️</a> "
        f"<a href='{link_vnd_news}'>🗞️</a> "
        f"<a href='{link_vietstock}'>📈</a> "
        f"<a href='{link_vnd_summary}'>📄</a> "
        f"<a href='{link_24h_money}'>📄</a>"
    )

def _build_daily_weekly_chart_paths(ctx):
    paths, labels = [], []
    symbol = ctx["symbol"]
    signal_type = ctx["signal_type"]
    try:
        path_d = draw_chart(
            ctx["df_calc"].tail(250).copy(), symbol, signal_type, ctx["today"],
            timeframe='Daily', add_arrow=False, date_str=ctx["date_str"]
        )
        paths.append(path_d)
        labels.append('📊 Daily [D]')
    except Exception as e:
        print(f"  [ChartCore] ❌ Daily {symbol}: {e}")

    try:
        df_weekly = build_weekly_df(ctx["df_raw"])
        df_plot_w = df_weekly.tail(200).copy()
        today_w = df_plot_w.iloc[-1]
        date_str_w = _date_str_from_df(ctx["df_raw"])
        path_w = draw_chart(
            df_plot_w, symbol, signal_type, today_w,
            timeframe='Weekly', add_arrow=False, date_str=date_str_w
        )
        paths.append(path_w)
        labels.append('📈 Weekly [W]')
    except Exception as e:
        print(f"  [ChartCore] ❌ Weekly {symbol}: {e}")
    return paths, labels

def _cleanup_chart_paths(paths):
    for path in paths:
        try:
            if os.path.exists(path): os.remove(path)
        except Exception:
            pass

def fetch_and_send_chart(symbol, chat_id):
    thread_id = threading.current_thread().ident
    symbol    = symbol.upper().strip()
    url_msg   = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    image_paths = []
    print(f"  🧵 [{thread_id}] fetch_and_send_chart BẮT ĐẦU: {symbol}")

    try:
        ctx = _get_chart_context(symbol)
        if ctx is None:
            requests.post(url_msg, data={
                'chat_id': chat_id,
                'text': f"❌ Không tìm thấy dữ liệu cho mã <b>{symbol}</b>",
                'parse_mode': 'HTML'
            })
            return

        print(f"  [Chart] {symbol} | {_format_chart_trace(ctx)} | last_bar={ctx['df_raw'].index[-1].date()} | via=telegram")
        image_paths, _ = _build_daily_weekly_chart_paths(ctx)
        if not ctx["is_index"]:
            img_15m = _build_15m_chart(symbol, ctx["signal_type"], via="telegram_15m")
            if img_15m: image_paths.append(img_15m)

        if not image_paths:
            requests.post(url_msg, data={
                'chat_id': chat_id,
                'text': f"❌ Không tạo được chart cho <b>{symbol}</b>",
                'parse_mode': 'HTML'
            })
            return

        print(f"  🧵 [{thread_id}] {symbol} — chuẩn bị gửi {len(image_paths)} chart")
        _send_chart_to_chat(_build_chart_message(ctx), image_paths, chat_id)
        image_paths = []
        print(f"  🧵 [{thread_id}] {symbol} — đã gửi xong")

    except Exception as e:
        print(f"  🧵 [{thread_id}] {symbol} — LỖI: {e}")
        requests.post(url_msg, data={
            'chat_id': chat_id,
            'text': f"❌ Lỗi lấy dữ liệu <b>{symbol}</b>: {e}",
            'parse_mode': 'HTML'
        })
    finally:
        _cleanup_chart_paths(image_paths)

def _send_chart_to_chat(msg, image_paths, chat_id):
    url_photo = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    url_album = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMediaGroup"
    try:
        if len(image_paths) == 1:
            with open(image_paths[0], 'rb') as f:
                requests.post(url_photo, data={
                    'chat_id': chat_id, 'caption': msg or '', 'parse_mode': 'HTML'
                }, files={'photo': f})
        else:
            files, media = {}, []
            for i, path in enumerate(image_paths):
                key = f"photo{i}"; files[key] = open(path, 'rb')
                item = {"type":"photo","media":f"attach://{key}"}
                if i == 0 and msg:
                    item["caption"] = msg; item["parse_mode"] = "HTML"
                media.append(item)
            try:
                requests.post(url_album, data={
                    'chat_id': chat_id, 'media': json.dumps(media)
                }, files=files)
            finally:
                for fh in files.values(): fh.close()
    except Exception as e:
        print(f"  ❌ Lỗi gửi chart on-demand: {e}")
    finally:
        for path in image_paths:
            if os.path.exists(path): os.remove(path)

# =============================================================================
# BƯỚC 8D0: CẢNH BÁO DAILY CHO DASHBOARD / TELEGRAM
# =============================================================================
def _price_alert_series_value(row, rule: dict, side: str):
    typ = rule.get(f"{side}_type")
    if typ == "price":
        return float(row.get("close", np.nan)), "Giá"
    kind = str(rule.get(f"{side}_ma_kind") or "MA").upper()
    period = int(rule.get(f"{side}_period") or 20)
    col = f"{kind}{period}"
    return float(row.get(col, np.nan)), col


def _price_alert_message(rule: dict, price: float, left_label: str, right_label: str, right_value: float) -> str:
    op = rule.get("operator")
    op_label = {
        "gte": "Tăng lên / Cắt lên",
        "lte": "Giảm về / Cắt xuống",
    }.get(op, op)
    if rule.get("right_type") == "price":
        target = f"{right_value:.2f}"
    else:
        target = right_label
    return f"{rule['symbol']} - {left_label} {op_label} {target} - Close {price:.2f}"


def _price_alert_triggered(rule: dict, prev_row, cur_row):
    left_prev, left_label = _price_alert_series_value(prev_row, rule, "left")
    left_cur, _ = _price_alert_series_value(cur_row, rule, "left")
    if rule.get("right_type") == "price":
        right_prev = right_cur = float(rule.get("right_value") or np.nan)
        right_label = "Mức giá"
    else:
        right_prev, right_label = _price_alert_series_value(prev_row, rule, "right")
        right_cur, _ = _price_alert_series_value(cur_row, rule, "right")
    vals = [left_prev, left_cur, right_prev, right_cur]
    if any(pd.isna(v) or not math.isfinite(v) for v in vals):
        return False, "", ""
    # Công thức tổng quát duy nhất cho mọi tổ hợp nguồn/đối tượng (Giá/MA vs Mức giá/MA):
    # gte: hôm qua bên trái còn thấp hơn bên phải, hôm nay đã bằng/vượt qua -> "tăng lên / cắt lên"
    # lte: hôm qua bên trái còn cao hơn bên phải, hôm nay đã bằng/thấp hơn -> "giảm về / cắt xuống"
    op = rule.get("operator")
    ok = (
        (op == "gte" and left_prev < right_prev and left_cur >= right_cur) or
        (op == "lte" and left_prev > right_prev and left_cur <= right_cur)
    )
    if not ok:
        return False, "", ""
    price = float(cur_row.get("close", np.nan))
    msg = _price_alert_message(rule, price, left_label, right_label, right_cur)
    detail = (
        f"prev_left={left_prev:.4f}; cur_left={left_cur:.4f}; "
        f"prev_right={right_prev:.4f}; cur_right={right_cur:.4f}"
    )
    return True, msg, detail


def send_price_alert_chart_to_telegram(symbol: str, chat_id: str, alert_message: str):
    return
    image_paths = []
    symbol = symbol.upper().strip()
    try:
        ctx = _get_chart_context(symbol)
        if ctx is None:
            return
        image_paths, _ = _build_daily_weekly_chart_paths(ctx)
        if not ctx["is_index"]:
            img_15m = _build_15m_chart(symbol, ctx["signal_type"], via="price_alert_15m")
            if img_15m:
                image_paths.append(img_15m)
        if not image_paths:
            return
        msg = f"<b>CẢNH BÁO #{symbol}</b>\n{alert_message}\n\n{_build_chart_message(ctx)}"
        _send_chart_to_chat(msg, image_paths, chat_id)
        image_paths = []
    except Exception as e:
        print(f"  [Alert] ❌ Gửi Telegram {symbol}: {e}")
    finally:
        _cleanup_chart_paths(image_paths)


def check_price_alerts():
    try:
        rules = get_active_price_alert_rules()
    except Exception as e:
        print(f"  [Alert] ❌ Không đọc được rule cảnh báo: {e}")
        return []
    if not rules:
        return []
    triggered = []
    for rule in rules:
        symbol = str(rule.get("symbol") or "").upper().strip()
        if not symbol:
            continue
        try:
            ensure_symbol_live_in_cache(symbol)
            with cache_lock:
                df_raw = history_cache.get(symbol)
                df_raw = df_raw.copy() if df_raw is not None and len(df_raw) >= 210 else None
            if df_raw is None or len(df_raw) < 2:
                continue
            df_calc = compute_indicators(df_raw)
            prev_row = df_calc.iloc[-2]
            cur_row = df_calc.iloc[-1]
            ok, msg, detail = _price_alert_triggered(rule, prev_row, cur_row)
            if not ok:
                continue
            bar_date = pd.Timestamp(df_calc.index[-1]).strftime("%Y-%m-%d")
            price = float(cur_row.get("close", np.nan))
            event = record_price_alert_event(
                rule["id"], msg, detail, bar_date, price,
                notify_dashboard=bool(rule.get("notify_dashboard", True))
            )
            if not event:
                continue
            triggered.append(symbol)
            print(f"  [Alert] 🔔 {msg}")
            if rule.get("notify_telegram") and rule.get("telegram_chat_id"):
                threading.Thread(
                    target=send_price_alert_chart_to_telegram,
                    args=(symbol, str(rule["telegram_chat_id"]), msg),
                    daemon=True
                ).start()
        except Exception as e:
            print(f"  [Alert] ❌ {symbol}: {e}")
    return triggered

# =============================================================================
# BƯỚC 8D: HÀM DASHBOARD VOL FORECAST
# =============================================================================
def dashboard_vol_forecast_fn(symbol: str):
    """
    Được truyền vào start_dashboard(vol_forecast_fn=...) — NGUỒN DUY NHẤT cho khối
    "Giá phóng to" (bp-price/bp-sub) trên panel CHART của dashboard: dùng lại NGUYÊN
    _get_chart_context() (cùng df_calc/VMA50 đã tính cho tín hiệu ATTENT/BREAKVOL,
    không tính lại riêng) và _session_time_progress() (cùng hàm ti2 dùng cho
    calc_attent()/calc_breakvol_signal()) — tránh có 2 bản logic lệch nhau giữa
    scanner và dashboard (JS phía trước không còn tự đoán múi giờ trình duyệt hay
    tự tính MA50 nữa, chỉ hiển thị số server trả về).
    """
    symbol = symbol.upper().strip()
    try:
        ctx = _get_chart_context(symbol)
        if ctx is None:
            return {"symbol": symbol, "error": "no_data"}
        df_calc = ctx["df_calc"]
        if len(df_calc) < 2:
            return {"symbol": symbol, "error": "not_enough_bars"}
        today = df_calc.iloc[-1]
        prev = df_calc.iloc[-2]
        now_obj = datetime.now(TZ_VN)
        now_time = int(now_obj.strftime("%H%M%S"))
        bar_date = pd.Timestamp(df_calc.index[-1]).strftime("%Y-%m-%d")
        is_today = bar_date == now_obj.strftime("%Y-%m-%d")
        # Giống điều kiện Ti2 trong AFL gốc: chỉ co giãn theo % thời gian phiên khi đang
        # xem ĐÚNG nến của hôm nay; nến quá khứ luôn coi như đã chốt phiên (progress=1).
        progress = _session_time_progress(now_time) if is_today else 1.0
        vol = float(today.get("volume", float("nan")))
        prev_vol = float(prev.get("volume", float("nan")))
        vma50 = float(today.get("VMA50", float("nan")))
        ratio_prev = (vol / prev_vol) if (prev_vol > 0 and vol == vol) else None
        ratio_ma50 = (vol / vma50) if (vma50 > 0 and vol == vol) else None
        # Chỉ trả về đúng những field JS thực sự dùng (symbol/progress/ratio_*) — volume/prev_volume/
        # vma50 giữ lại thêm để tiện đối chiếu qua tab Network khi cần soát lại số, close/open/
        # now_time/is_today thì bỏ hẳn vì client đã có sẵn close/open từ chính dữ liệu nến, và
        # is_today không cần thiết vì progress đã tự phản ánh đúng ý nghĩa đó rồi.
        return {
            "symbol": symbol,
            "bar_date": bar_date,
            "progress": round(progress, 4),
            "volume": vol if vol == vol else None,
            "prev_volume": prev_vol if prev_vol == prev_vol else None,
            "vma50": vma50 if vma50 == vma50 else None,
            "ratio_prev": round(ratio_prev, 4) if ratio_prev is not None else None,
            "ratio_ma50": round(ratio_ma50, 4) if ratio_ma50 is not None else None,
        }
    except Exception as e:
        print(f"  [VolForecast] ❌ {symbol}: {e}")
        return {"symbol": symbol, "error": "exception", "detail": str(e)}

# =============================================================================
# BƯỚC 8E: TELEGRAM LISTENER
# =============================================================================
def telegram_listener(stop_event: threading.Event):
    url_upd = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    url_msg = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    try:
        resp    = requests.get(url_upd, params={'offset':-1,'limit':1}, timeout=10)
        results = resp.json().get('result', [])
        offset  = (results[-1]['update_id']+1) if results else 0
        print(f"🎧 Telegram Listener khởi động — offset={offset}")
    except Exception as e:
        offset = 0
        print(f"🎧 Telegram Listener khởi động — offset=0 (lỗi: {e})")

    processed_ids: dict = {}
    PROCESSED_TTL = 300
    print(f"🎧 Listener sẵn sàng | VIP: {VIP_CHAT_IDS} | Free slot: {FREE_CHAT_LIMIT}")

    while not stop_event.is_set():
        try:
            resp = requests.get(url_upd, params={
                'offset': offset, 'timeout': 30,
                'allowed_updates': ['message', 'callback_query'],
            }, timeout=35)

            if stop_event.is_set(): break

            if resp.status_code == 409:
                print("  ⚠️ HTTP 409 Conflict — có instance khác đang chạy! Đợi 15s...")
                time.sleep(15); continue
            elif resp.status_code != 200:
                print(f"  ⚠️ getUpdates HTTP {resp.status_code} — thử lại sau 5s")
                time.sleep(5); continue

            updates = resp.json().get('result', [])
            if not updates: continue

            now_ts  = time.time()
            expired = [uid for uid, ts in processed_ids.items() if now_ts-ts > PROCESSED_TTL]
            for uid in expired: del processed_ids[uid]

            for update in updates:
                update_id = update['update_id']
                if update_id >= offset: offset = update_id + 1
                if update_id in processed_ids:
                    print(f"  ⚠️ Bỏ qua duplicate update_id={update_id}"); continue
                processed_ids[update_id] = time.time()
                print(f"  📨 Xử lý update_id={update_id} | offset mới={offset}")

                callback = update.get('callback_query', {})
                if callback:
                    cb_id      = callback.get('id')
                    cb_data    = callback.get('data', '')
                    cb_chat_id = str(callback.get('message', {}).get('chat', {}).get('id', ''))
                    requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                        data={'callback_query_id': cb_id}
                    )
                    allowed, reason = is_allowed(cb_chat_id)
                    if allowed and cb_data.startswith('chart_'):
                        sym = cb_data.replace('chart_', '').upper()
                        print(f"  📥 Callback chart {sym} → chat_id={cb_chat_id} ({reason})")
                        threading.Thread(
                            target=fetch_and_send_chart,
                            args=(sym, cb_chat_id),
                            daemon=True
                        ).start()
                    continue

                msg_obj = update.get('message', {})
                text    = msg_obj.get('text', '').strip()
                chat_id = str(msg_obj.get('chat', {}).get('id', ''))
                if not text or not chat_id: continue

                text_lower = text.lower().strip()

                if text_lower == '/start':
                    continue

                allowed, reason = is_allowed(chat_id)
                if not allowed:
                    requests.post(url_msg, data={
                        'chat_id':    chat_id,
                        'parse_mode': 'HTML',
                        'text': (
                            "⚠️ Bot đang phục vụ tối đa <b>20 người</b> cùng lúc.\n"
                            "Hiện tại đã đầy slot. Vui lòng thử lại sau ít phút.\n"
                            "Slot tự động giải phóng sau <b>30 phút</b> không hoạt động."
                        )
                    })
                    continue

                if text_lower == '/s' or text_lower.startswith('/s '):
                    if not is_vip(chat_id):
                        requests.post(url_msg, data={
                            'chat_id':    chat_id,
                            'parse_mode': 'HTML',
                            'text':       '🔒 Lệnh <b>/s</b> chỉ dành cho thành viên VIP.'
                        })
                        continue

                    if alerted_today:
                        buttons = []
                        sorted_signals = sorted(
                            alerted_today.items(),
                            key=lambda x: (
                                -SIGNAL_RANK.get(x[1]["signal"] if isinstance(x[1], dict) else x[1], 0),
                                x[0]
                            )
                        )
                        for k, v in sorted_signals:
                            sig   = v["signal"] if isinstance(v, dict) else v
                            emoji = SIGNAL_EMOJI.get(sig, '📌')
                            buttons.append([{"text": f"{emoji} #{k}: {sig}", "callback_data": f"chart_{k}"}])
                        if signal_session_date == datetime.now(TZ_VN).date():
                            reply = "📋 <b>Tín hiệu hôm nay:</b>"
                        else:
                            reply = f"📋 <b>Tín hiệu phiên gần nhất ({signal_session_date.strftime('%d/%m')}):</b>"
                    else:
                        reply   = "📋 Chưa có tín hiệu nào hôm nay."
                        buttons = []

                    payload = {
                        'chat_id':    chat_id,
                        'text':       reply,
                        'parse_mode': 'HTML',
                    }
                    if buttons:
                        payload['reply_markup'] = json.dumps({"inline_keyboard": buttons})
                    requests.post(url_msg, data=payload)

                elif text_lower in ('/h', '/heatmap', '/ h', '/ heatmap'):
                    print(f"  🗺  Lệnh heatmap từ chat_id={chat_id} ({reason})")
                    threading.Thread(
                        target=handle_heatmap_command,
                        args=(chat_id,),
                        daemon=True
                    ).start()

                elif text_lower == '/help' or text_lower.startswith('/help '):
                    vip_note = "\n\n🔒 <b>Chỉ VIP:</b> /s — Tín hiệu hôm nay" if not is_vip(chat_id) else ""
                    requests.post(url_msg, data={
                        'chat_id': chat_id, 'parse_mode': 'HTML',
                        'text': (
                            "🤖 <b>Lệnh hỗ trợ:</b>\n\n"
                            "<b>Xem chart cổ phiếu:</b>\n"
                            "/c HPG\n/chart HPG\n/HPG\n/ HPG\n"
                            "/c HPG VNM FPT  (nhiều mã, tối đa 5)\n\n"
                            "<b>Xem chart chỉ số:</b>\n"
                            "/VNINDEX  /VN30  /HNX  /UPCOM  /VN100\n\n"
                            "<b>Heatmap thị trường:</b>\n"
                            "/h  hoặc  /heatmap\n\n"
                            "<b>Khác:</b>\n"
                            "/s  — Tín hiệu hôm nay (VIP)\n"
                            "/help  — Trợ giúp\n\n"
                            "<b>Chart gửi kèm:</b> Daily [D] + Weekly [W] + 15 phút [15m]"
                            f"{vip_note}"
                        )
                    })

                else:
                    symbols = parse_chart_command(text)
                    if symbols:
                        print(f"  🔍 {text!r} → {symbols} | update_id={update_id} ({reason})")
                        for sym in symbols[:5]:
                            print(f"  📥 Chart {sym} → chat_id={chat_id}")
                            threading.Thread(
                                target=fetch_and_send_chart,
                                args=(sym, chat_id),
                                daemon=True
                            ).start()
                            time.sleep(0.3)

        except requests.exceptions.Timeout:
            continue
        except requests.exceptions.ConnectionError as e:
            if stop_event.is_set(): break
            print(f"  ❌ Connection error: {e} — thử lại sau 10s"); time.sleep(10)
        except Exception as e:
            if stop_event.is_set(): break
            print(f"  ❌ Listener lỗi: {e}"); time.sleep(5)

    print("🛑 Listener đã dừng.")

# =============================================================================
# BƯỚC 8D: LƯU/ĐỌC TRẠNG THÁI TÍN HIỆU ĐÃ GỬI (PERSIST QUA RESTART)
# =============================================================================
# Mục đích: alerted_today trước đây chỉ sống trong RAM → mỗi lần restart server
# giữa ngày/phiên sẽ mất sạch, khiến các mã đã gửi tin nhắn rồi bị coi là "chưa
# gửi" và bắn lại từ đầu. Giờ ghi xuống đĩa kèm "phiên giao dịch" mà nó thuộc về,
# để khi restart cùng phiên thì đọc lại và KHÔNG gửi trùng; chỉ thực sự xoá khi
# một phiên giao dịch MỚI thực sự bắt đầu (xem đoạn reset trong vòng lặp chính).
# LƯU Ý: đặt trong DASHBOARD_DATA_DIR (mặc định /data/trade-journal) — đây là
# thư mục đã được mount làm volume trong lệnh `docker run` (-v ...:/data/trade-journal),
# giống trade_journal.sqlite/market_warning.txt. Nếu để cạnh source code như trước,
# file sẽ nằm trong writable layer của container và bị xoá mỗi khi tạo lại container
# (docker rm + build lại image), khiến "Tín hiệu hôm nay"/"Động lượng" mất sau restart.
_SIGNAL_STATE_DIR = os.environ.get("DASHBOARD_DATA_DIR", "/data/trade-journal")
if not os.path.isdir(_SIGNAL_STATE_DIR):
    # Fallback khi chạy ngoài Docker (không có /data/trade-journal) để không vỡ local dev.
    _SIGNAL_STATE_DIR = os.path.dirname(os.path.abspath(__file__))
SIGNAL_STATE_FILE = os.path.join(_SIGNAL_STATE_DIR, 'signal_state_cache.json')
_signal_state_lock = threading.Lock()

def _load_signal_state():
    """
    Đọc lại alerted_today + momentum_today + attent_today + breakvol_today + ngày
    phiên giao dịch đã lưu từ lần chạy trước. Trả về
    (alerted_dict, momentum_dict, attent_dict, breakvol_dict, session_date).
    session_date=None nếu chưa từng lưu (lần đầu chạy) hoặc file lỗi — khi đó coi
    như chưa có gì, sẽ tự đồng bộ lại ngay trong lần khởi tạo bên dưới.
    ATTENT/BREAKVOL dùng chung file + cơ chế lưu/đọc với MOMENTUM (key mới, mặc
    định {} nếu đọc từ file cũ chưa có 2 key này → tương thích ngược).
    """
    try:
        with open(SIGNAL_STATE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        session_date_str = data.get('session_date')
        session_date = datetime.strptime(session_date_str, '%Y-%m-%d').date() if session_date_str else None
        alerted  = data.get('alerted', {}) or {}
        momentum = data.get('momentum', {}) or {}
        attent   = data.get('attent', {}) or {}
        breakvol = data.get('breakvol', {}) or {}
        print(f"  💾 Đã đọc trạng thái đã lưu: {len(alerted)} tín hiệu, {len(momentum)} động lượng, "
              f"{len(attent)} ATTENT, {len(breakvol)} BREAKVOL, phiên {session_date_str or '?'}")
        return alerted, momentum, attent, breakvol, session_date
    except FileNotFoundError:
        return {}, {}, {}, {}, None
    except Exception as e:
        print(f"  ⚠️  Lỗi đọc {SIGNAL_STATE_FILE}: {e} → bỏ qua, coi như chưa có dữ liệu lưu.")
        return {}, {}, {}, {}, None

def _save_signal_state(alerted: dict, momentum: dict, session_date: date,
                        attent: dict = None, breakvol: dict = None):
    """Ghi đè toàn bộ trạng thái tín hiệu + động lượng + ATTENT + BREAKVOL của phiên hiện tại xuống đĩa (ghi an toàn qua file tạm)."""
    try:
        with _signal_state_lock:
            tmp_path = SIGNAL_STATE_FILE + '.tmp'
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump({
                    'session_date': session_date.strftime('%Y-%m-%d') if session_date else None,
                    'alerted': alerted,
                    'momentum': momentum,
                    'attent': attent or {},
                    'breakvol': breakvol or {},
                }, f, ensure_ascii=False)
            os.replace(tmp_path, SIGNAL_STATE_FILE)
    except Exception as e:
        print(f"  ⚠️  Lỗi lưu {SIGNAL_STATE_FILE}: {e}")

# =============================================================================
# BƯỚC 9: KHỞI ĐỘNG
# =============================================================================
try:
    _stop_listener.set()
    print("⏹️  Đang dừng listener cũ...")
    if 'listener_thread' in dir() and listener_thread.is_alive():
        listener_thread.join(timeout=8)
        if listener_thread.is_alive():
            print("  ⚠️ Listener cũ chưa dừng hẳn (timeout), tiếp tục...")
        else:
            print("  ✅ Listener cũ đã dừng hẳn.")
    time.sleep(1)
except NameError:
    pass

alerted_today, momentum_today, attent_today, breakvol_today, signal_session_date = _load_signal_state()
last_run_date = datetime.now(TZ_VN).date()
if signal_session_date is None:
    # Lần đầu chạy (chưa từng có file lưu) → coi phiên hiện tại là "phiên của
    # alerted_today/momentum_today/attent_today/breakvol_today" (rỗng). Vòng lặp
    # chính bên dưới sẽ tự reset đúng lúc nếu thời điểm khởi động đã là 1 phiên
    # mới so với lần lưu gần nhất.
    signal_session_date = last_run_date
    _save_signal_state(alerted_today, momentum_today, signal_session_date, attent_today, breakvol_today)
_last_cache_check_ts = 0.0   # cổng nhịp cho check_and_rebuild_cache_if_stale (ngoài giờ, mỗi CACHE_CHECK_INTERVAL_SEC)

_stop_listener  = threading.Event()
listener_thread = threading.Thread(target=telegram_listener, args=(_stop_listener,), daemon=True)
# listener_thread.start()

# ─── (Đã BỎ ý tưởng đảo thứ tự start_dashboard/build_history_cache) ────────
# Ban đầu định đảo build_history_cache() lên trước start_dashboard() để tránh
# HEALTH tính trên cache rỗng lúc khởi động. Nhưng làm vậy khiến Flask KHÔNG
# mở cổng cho tới khi cache load xong (1-3 phút) → HEATMAP/MARKET/CHART cũng
# không truy cập được trong lúc đó, dù các panel này vốn có cơ chế fallback
# "fetch tươi" khi cache chưa có (xem ensure_symbol_live_in_cache, fetch_today_bar)
# và KHÔNG cần cache sẵn để hoạt động. Đánh đổi đó không đáng — nên GIỮ NGUYÊN
# thứ tự gốc: mở dashboard trước để HEATMAP/CHART dùng được ngay (chậm hơn do
# fetch tươi), còn HEALTH tự khắc phục nhờ 2 cơ chế bên dưới:
#   (1) Fix A trong dashboard_server.py: kết quả lỗi "0 mã hợp lệ" không bị
#       đóng dấu "cache mới" → không kẹt 30 phút, tự thử lại ở lần gọi sau.
#   (2) warm_market_health_cache() được gọi CHỦ ĐỘNG ngay khi build_history_cache()
#       xong (xem bên dưới) → HEALTH có dữ liệu đúng chỉ vài giây sau khi cache
#       sẵn sàng, không cần đợi ai request hay đợi TTL.
start_dashboard(
    alerted_today_ref = lambda: alerted_today,
    history_cache_ref = lambda: history_cache,
    cache_lock_ref    = cache_lock,
    fetch_heatmap_fn  = fetch_heatmap_data,
    signal_emoji_ref  = SIGNAL_EMOJI,
    signal_rank_ref   = SIGNAL_RANK,
    vol_forecast_fn   = dashboard_vol_forecast_fn,
    calc_vpa_flag_fn  = calc_vpa_flag,
    momentum_today_ref = lambda: momentum_today,
    attent_today_ref   = lambda: attent_today,
    breakvol_today_ref = lambda: breakvol_today,
    fetch_market_health_fn = compute_market_health_index,
    signal_session_date_ref = lambda: signal_session_date,
    port              = 8888,
    extra_quote_fn    = fetch_extra_quotes,
    rs_universe_ref   = lambda: symbols_to_rs,
)

print("\n🔧 Đang load cache lịch sử lần đầu...")
build_history_cache(symbols_to_cache, last_run_date)

# Cache lịch sử vừa load xong → chủ động tính HEALTH ngay, không đợi client
# đầu tiên tự trigger. Trong khoảng thời gian build_history_cache() đang chạy
# ở trên, dashboard vẫn phản hồi HEATMAP/CHART bình thường (qua fallback fetch
# tươi); nếu có ai request HEALTH đúng lúc đó thì Fix A đảm bảo không bị kẹt.
warm_market_health_cache()

print("\n" + "="*60)
print("⚙️  AUTO-SCANNER + HEATMAP + TELEGRAM LISTENER + DASHBOARD")
print(f"   Danh sách   : {len(symbols_to_scan)} mã")
print(f"   Cache chart : {len(symbols_to_cache)} mã")
print(f"   Chu kỳ quét : {SCAN_INTERVAL_SEC} giây")
print(f"   Tín hiệu    → Channel/Group: {TELEGRAM_CHAT_ID}")
print("   Dashboard   : http://VPS_IP:8888")
print("   Lệnh chart  : /c HPG | /chart HPG | /HPG | / HPG")
print("   Lệnh chỉ số : /VNINDEX | /VN30 | /HNX | /UPCOM | /VN100")
print("   Lệnh heatmap: /h | /heatmap")
print("   Lệnh khác   : /s (VIP) | /help")
print(f"   Phân quyền  : VIP (toàn quyền) | Free (tối đa {FREE_CHAT_LIMIT} slot, TTL 30p)")
print("   Chart gửi   : Daily [D] + Weekly [W] + 15 phút [15m]")
print("   Tín hiệu    : BREAKOUT / POCKET PIVOT / PRE-BREAK")
print("                 BOTTOMBREAKP / MA_CROSS / BOTTOMFISH")
print("   Nhận lệnh   : Group + Private Chat (24/7)")
print("   Cache check : Tự động trước mỗi chu kỳ quét")
print("   On-demand   : Ưu tiên cache, fallback fetch fresh")
print("   Nghỉ quét   : Thứ 7 và Chủ nhật")
print("="*60)

# =============================================================================
# VÒNG LẶP CHÍNH
# =============================================================================
while True:
    try:
        now_obj      = datetime.now(TZ_VN)
        current_date = now_obj.date()
        now_time     = int(now_obj.strftime("%H%M%S"))
        ts           = now_obj.strftime("%H:%M:%S")
        weekday      = now_obj.weekday()  # 0=Thứ 2 ... 4=Thứ 6, 5=Thứ 7, 6=Chủ nhật

        # ── BỎ QUA THỨ 7 VÀ CHỦ NHẬT ────────────────────────────────────────
        if weekday >= 5:
            day_name = "Thứ 7" if weekday == 5 else "Chủ nhật"
            print(f"[{ts}] 📅 {day_name} — không quét. Listener + Dashboard vẫn chạy.")
            time.sleep(SCAN_INTERVAL_SEC)
            continue

        if current_date > last_run_date:
            last_run_date = current_date
            print(f"\n🌅 [{ts}] Ngày mới {current_date.strftime('%d/%m/%Y')} — Reload cache lịch sử.")
            # LƯU Ý: KHÔNG reset alerted_today/momentum_today ở đây. Sang ngày mới
            # nhưng chưa vào giờ giao dịch thì vẫn chưa có dữ liệu phiên mới — danh
            # sách tín hiệu của phiên gần nhất (signal_session_date) vẫn cần giữ
            # nguyên để hiển thị. Việc reset chỉ diễn ra khi phiên giao dịch mới
            # THỰC SỰ bắt đầu — xem đoạn kiểm tra signal_session_date bên dưới.
            build_history_cache(symbols_to_cache, current_date)
            # Cache vừa reload cho ngày mới → warm lại HEALTH ngay, tránh 30 phút
            # đầu ngày dashboard hiển thị HEALTH tính trên dữ liệu của phiên hôm trước.
            warm_market_health_cache()

        if not _is_trading_session_time(current_date, now_time):
            # Tự dò + tự sửa cache lệch phiên — CHỈ chạy ngoài giờ giao dịch, với nhịp
            # riêng CACHE_CHECK_INTERVAL_SEC (30 phút), hoàn toàn tách khỏi SCAN_INTERVAL_SEC
            # (nhịp quét tín hiệu). Vòng lặp vẫn "thức" mỗi SCAN_INTERVAL_SEC để không lỡ
            # thời điểm mở cửa, nhưng chỉ THỰC SỰ gọi kiểm tra cache mỗi 30 phút 1 lần.
            if time.time() - _last_cache_check_ts >= CACHE_CHECK_INTERVAL_SEC:
                _last_cache_check_ts = time.time()
                check_and_rebuild_cache_if_stale(symbols_to_cache, current_date)

            next_open = _next_trading_session_label(now_time)
            if signal_session_date < current_date:
                print(f"[{ts}] ⏸  Ngoài giờ giao dịch → Đợi đến {next_open}. "
                      f"Đang hiển thị dữ liệu phiên {signal_session_date.strftime('%d/%m/%Y')} (chưa có phiên mới). Listener + Dashboard vẫn chạy.")
            else:
                print(f"[{ts}] ⏸  Ngoài giờ giao dịch → Đợi đến {next_open}. Listener + Dashboard vẫn chạy.")
            time.sleep(SCAN_INTERVAL_SEC)
            continue

        # Reset danh sách tín hiệu khi vào phiên mới
        if current_date > signal_session_date:
            alerted_today.clear()
            momentum_today.clear()
            attent_today.clear()
            breakvol_today.clear()
            signal_session_date = current_date
            _save_signal_state(alerted_today, momentum_today, signal_session_date, attent_today, breakvol_today)
            print(f"🌅 [{ts}] Phiên giao dịch mới {current_date.strftime('%d/%m/%Y')} — Reset danh sách tín hiệu đã gửi.")

        with cache_lock:
            cache_empty = len(history_cache) == 0
        if cache_empty:
            print(f"[{ts}] ⚠️  Cache trống — bắt buộc load trước khi quét...")
            build_history_cache(symbols_to_cache, current_date)
            warm_market_health_cache()

        print(f"\n{'='*60}")
        print(f"🔄 [{ts}] BẮT ĐẦU CHU KỲ QUÉT (VNDirect)")
        print(f"{'='*60}")

        new_signals = run_scan_cycle(symbols_to_scan, now_time, alerted_today, momentum_today,
                                      attent_today, breakvol_today)
        triggered_alerts = check_price_alerts()

        if new_signals:
            print(f"✅ [{ts}] {len(new_signals)} tín hiệu MỚI: {', '.join(new_signals)}")
        else:
            print(f"[{ts}] Không có tín hiệu mới.")

        _save_signal_state(alerted_today, momentum_today, signal_session_date, attent_today, breakvol_today)
        if triggered_alerts:
            print(f"🔔 [{ts}] {len(triggered_alerts)} cảnh báo khớp: {', '.join(triggered_alerts)}")

        if alerted_today:
            summary_str = " | ".join([f"{k}:{v['signal']}" for k,v in alerted_today.items()])
            print(f"   📋 Đã báo hôm nay: {summary_str}")
        if momentum_today:
            summary_mom = " | ".join([f"{k}:{'/'.join(v['signals'])}" for k,v in sorted(momentum_today.items())])
            print(f"   ⚡ Động lượng: {summary_mom}")
        if attent_today:
            print(f"   👀 ATTENT ({len(attent_today)}): {', '.join(sorted(attent_today.keys()))}")
        if breakvol_today:
            print(f"   💥 BREAKVOL ({len(breakvol_today)}): {', '.join(sorted(breakvol_today.keys()))}")

        print(f"⏳ Đợi {SCAN_INTERVAL_SEC}s cho chu kỳ tiếp theo...")
        time.sleep(SCAN_INTERVAL_SEC)

    except Exception as e:
        ts = datetime.now(TZ_VN).strftime("%H:%M:%S")
        print(f"[{ts}] ❌ Lỗi vòng lặp chính: {e}")
        time.sleep(10)
