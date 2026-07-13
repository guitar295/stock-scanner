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
import matplotlib.pyplot as plt
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
from dashboard_server import start_dashboard

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
TRADING_STOCKS_POOL = [
    "AAA","ACB","AGG","ANV","BFC","BID","BSR","BVB","BVH","BWE","VCK","CII",
    "CKG","CRE","CTD","CTG","CTI","CTR","CTS","ORS","DBC","DCM","DGW","DIG",
    "DPG","DPM","DRC","DRH","DXG","FCN","FPT","FRT","FTS","GAS","GEG","GEX",
    "GMD","GVR","HAG","HAX","HBC","HCM","HDB","HDC","HDG","HNG","HPG","HSG",
    "HTN","HVN","IDC","IJC","DSE","KBC","KDH","KSB","LDG","LPB","D2D","LTG",
    "MBB","MBS","MSB","MSN","MWG","NKG","NLG","NTL","NVL","PC1","PDR","PET",
    "PHR","PLC","PLX","PNJ","POW","PTB","PVD","PVS","PVT","QNS","REE","SBT",
    "SCR","SHB","SHS","SSI","STB","SZC","TCB","TDM","TIG","TNG","TPB","TV2",
    "VCB","VCI","VCS","VGT","VHC","VHM","VIB","VIC","VJC","VNM","VPB","VRE",
]

HEATMAP_COLUMNS = [
    {"col": 1, "groups": [
        {"name": "VN30", "symbols": [
            "FPT","GAS","NVL","VNM","VCB","PLX","TCB","MWG","STB","HPG","PNJ",
            "BID","CTG","HDB","VJC","VPB","KDH","MBB","VHM","POW","VRE","MSN",
            "SSI","ACB","BVH","GVR","TPB",
        ]},
    ]},
    {"col": 2, "groups": [
        {"name": "NGAN HANG", "symbols": ["VCB","BID","CTG","MBB","ACB","TCB","TPB","HDB","SHB","STB","VIB","VPB","MSB","ABB","BVB","LPB"]},
        {"name": "DAU KHI",   "symbols": ["GAS","PVD","PVS","BSR","OIL","PVB","PVC","PLX","PET","PVT"]},
    ]},
    {"col": 3, "groups": [
        {"name": "CHUNG KHOAN", "symbols": ["SSI","VND","CTS","FTS","HCM","MBS","DSE","BSI","SHS","VCI","VCK","ORS"]},
        {"name": "XAY DUNG",    "symbols": ["C47","C32","L14","CII","CTD","CTI","FCN","HBC","HUT","LCG","PC1","DPG","PHC","VCG"]},
    ]},
    {"col": 4, "groups": [
        {"name": "BAT DONG SAN", "symbols": ["VHM","AGG","IJC","LDG","CEO","D2D","DIG","DXG","HDC","HDG","KDH","NLG","NTL","NVL","PDR","SCR","TIG","KBC","SZC"]},
        {"name": "PHAN BON",     "symbols": ["BFC","DCM","DPM"]},
        {"name": "THEP",         "symbols": ["HPG","HSG","NKG"]},
    ]},
    {"col": 5, "groups": [
        {"name": "BAN LE",    "symbols": ["MSN","FPT","FRT","MWG","PNJ","DGW"]},
        {"name": "THUY SAN",  "symbols": ["ANV","FMC","CMX","VHC","IDI"]},
        {"name": "CANG BIEN", "symbols": ["HAH","GMD","SGP","VSC"]},
        {"name": "CAO SU",    "symbols": ["GVR","DPR","DRI","PHR","DRC"]},
        {"name": "NHUA",      "symbols": ["AAA","BMP","NTP"]},
    ]},
    {"col": 6, "groups": [
        {"name": "DIEN NUOC",  "symbols": ["NT2","PC1","GEG","GEX","POW","TDM","BWE"]},
        {"name": "DET MAY",    "symbols": ["TCM","TNG","VGT","MSH"]},
        {"name": "HANG KHONG", "symbols": ["NCT","ACV","AST","HVN","SCS","VJC"]},
        {"name": "BAO HIEM",   "symbols": ["BMI","MIG","BVH"]},
        {"name": "MIA DUONG",  "symbols": ["LSS","SBT","QNS"]},
    ]},
    {"col": 7, "groups": [
        {"name": "DAU TU CONG", "symbols": ["FCN","HHV","LCG","VCG","C4G","CTD","HBC","HSG","NKG","HPG","KSB","PLC"]},
    ]},
]

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
    bold_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
    ]
    reg_paths = [
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
    need   = list({s for col in HEATMAP_COLUMNS for g in col["groups"] for s in g["symbols"]}
                  | set(TRADING_STOCKS_POOL))
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
listing     = Listing(source=DATA_SOURCE)
df_listing  = listing.all_symbols()
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
symbols_to_scan = [s for s in all_symbols if s in vn30_symbols]
symbols_to_cache = [s for s in all_symbols if s in cache_symbol_set]
print(f"🚀 Sẵn sàng quét {len(symbols_to_scan)} mã: {', '.join(symbols_to_scan)}")
print(f"📦 Cache lịch sử mở rộng: {len(symbols_to_cache)} mã")

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
    for n in [2,3,5,10,15,20,30,50,200]:
        df[f'MA{n}']  = df['close'].rolling(n).mean()
    for n in [10,20,30,50,200]:
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
# BƯỚC 5B: CACHE LỊCH SỬ
# =============================================================================
history_cache: dict = {}
cache_lock          = threading.Lock()
last_bar_update: dict = {}   # {symbol: timestamp} - dùng chung cho CẢ scan cycle lẫn chart on-demand
BAR_UPDATE_TTL_SEC = 60

def load_history_for_symbol(symbol: str, current_date: date):
    for attempt in range(3):
        try:
            quote  = Quote(symbol=symbol, source=DATA_SOURCE)
            df_raw = quote.history(length='1000', interval='1D')
            if df_raw is None or len(df_raw) < 60: return None
            df_raw['time'] = pd.to_datetime(df_raw['time'])
            df_raw.set_index('time', inplace=True)
            df_raw.columns = [c.lower() for c in df_raw.columns]
            return df_raw[['open','high','low','close','volume']].copy()
        except Exception as e:
            if attempt < 2: time.sleep(2)
            else: print(f"    ❌ Load history {symbol}: {e}")
    return None

def build_history_cache(symbols: list, current_date: date):
    ts = datetime.now(TZ_VN).strftime('%H:%M:%S')
    print(f"\n📦 [{ts}] Bắt đầu load cache lịch sử cho {len(symbols)} mã...")
    new_history = {}
    for i, symbol in enumerate(symbols, 1):
        df = load_history_for_symbol(symbol, current_date)
        if df is not None and len(df) >= 60:
            new_history[symbol] = df
        if i % 20 == 0:
            ts2 = datetime.now(TZ_VN).strftime('%H:%M:%S')
            print(f"  [{ts2}] Đã load {i}/{len(symbols)} mã...")
        time.sleep(0.3)
    with cache_lock:
        history_cache.clear()
        history_cache.update(new_history)
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
    """
    Ngày nến cuối cùng mà cache/lịch sử BẮT BUỘC phải có tính tới thời điểm hiện tại.
    Dùng chung cho mọi nơi cần biết "cache đã đủ mới chưa" (build cache theo ngày,
    ensure-on-demand cho lite chart, chart on-demand cho Telegram/scanner).
    - Ngày thường, trước 15:00 (chưa chốt phiên) → kỳ vọng = phiên liền trước.
    - Ngày thường, từ 15:00 (đã chốt phiên) → kỳ vọng = chính hôm nay.
    """
    expected = (pd.Timestamp(current_date) - pd.tseries.offsets.BDay(1)).date()
    if current_date.weekday() < 5 and now_time >= 150000:
        expected = current_date
    return expected

def _cache_is_fresh(df_hist, current_date: date, now_time: int) -> bool:
    """True nếu nến cuối của df_hist đã đạt tới _expected_last_session()."""
    if df_hist is None or len(df_hist) == 0:
        return False
    return df_hist.index[-1].date() >= _expected_last_session(current_date, now_time)

def check_and_rebuild_cache_if_stale(symbols: list, current_date: date) -> bool:
    """
    Kiểm tra 1 mã mẫu đại diện cho cả `history_cache`; nếu lệch phiên kỳ vọng thì
    rebuild lại toàn bộ. Hàm này KHÔNG tự giới hạn tần suất gọi — việc gọi hàm này
    cách nhau bao lâu (30 phút, ngoài giờ giao dịch) do nơi gọi (vòng lặp chính,
    qua CACHE_CHECK_INTERVAL_SEC) quyết định, để tách bạch rõ "khi nào kiểm tra"
    khỏi "kiểm tra làm gì".
    """
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
            quote  = Quote(symbol=symbol, source=DATA_SOURCE)
            df_raw = quote.history(length='2', interval='1D')
            if df_raw is None or df_raw.empty: return None
            df_raw['time'] = pd.to_datetime(df_raw['time'])
            df_raw.set_index('time', inplace=True)
            df_raw.columns = [c.lower() for c in df_raw.columns]

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
            if attempt < 2: time.sleep(2)
            else: print(f"    ❌ fetch_today_bar {symbol}: {e}")
    return None

def upsert_today_bar(df_hist, today_bar):
    bar_date = pd.Timestamp(today_bar.name).date()
    new_row = pd.DataFrame([today_bar], index=[pd.Timestamp(today_bar.name)])
    df_hist = df_hist[df_hist.index.date != bar_date]
    return pd.concat([df_hist, new_row]).sort_index()

def chart_symbol_status(symbol: str) -> dict:
    """
    Kiểm tra nhanh (KHÔNG gọi mạng/vnstock) xem việc tải chart cho `symbol`
    sẽ được phục vụ từ cache có sẵn hay sẽ cần ensure_symbol_live_in_cache()
    thực hiện một lượt gọi mạng (tải mới toàn bộ lịch sử, hoặc cập nhật nến
    hôm nay từ vnstock). Dùng để hiển thị trạng thái "Đang tải cache" /
    "Đang update chart" ở giao diện TRƯỚC khi gọi endpoint tải dữ liệu thật.
    """
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


def ensure_symbol_live_in_cache(symbol: str) -> bool:
    symbol = symbol.upper().strip()
    now = datetime.now(TZ_VN)
    current_date = now.date()
    now_time = int(now.strftime("%H%M%S"))

    with cache_lock:
        df_hist = history_cache.get(symbol)

    if df_hist is None or len(df_hist) < 60:
        df_hist = load_history_for_symbol(symbol, current_date)
        if df_hist is None or len(df_hist) < 60:
            return False
        with cache_lock:
            history_cache[symbol] = df_hist
        return True

    # Cache đã có nhưng lệch hơn 1 phiên so với kỳ vọng (ví dụ bị bỏ lỡ phiên gần nhất
    # do không ai xem chart lúc phiên đó diễn ra) → nạp lại toàn bộ và ghi đè cache dùng
    # chung, thay vì chỉ cố vá đúng bar "hôm nay" như nhánh live-update bên dưới.
    if not _cache_is_fresh(df_hist, current_date, now_time):
        fresh = load_history_for_symbol(symbol, current_date)
        if fresh is not None and len(fresh) >= 60:
            with cache_lock:
                history_cache[symbol] = fresh
            last_bar_update[symbol] = time.time()
            return True
        # Fetch fresh lỗi → rơi xuống logic live-update bên dưới để vẫn thử vá tạm

    if not _is_trading_session_time(current_date, now_time):
        return True

    last_touch = last_bar_update.get(symbol, 0)
    if time.time() - last_touch < BAR_UPDATE_TTL_SEC:
        return True

    today_bar = fetch_today_bar(symbol, current_date)
    last_bar_update[symbol] = time.time()
    if today_bar is None:
        return True

    with cache_lock:
        latest_hist = history_cache.get(symbol)
        if latest_hist is None or len(latest_hist) < 60:
            latest_hist = df_hist
        history_cache[symbol] = upsert_today_bar(latest_hist, today_bar)
    return True

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
            quote  = Quote(symbol=symbol, source=DATA_SOURCE)
            df_raw = quote.history(length='1000', interval='1D')
            if df_raw is None or df_raw.empty: return None
            df_raw['time'] = pd.to_datetime(df_raw['time'])
            df_raw.set_index('time', inplace=True)
            df_raw.columns = [c.lower() for c in df_raw.columns]
            for col in ['open','high','low','close']:
                if col not in df_raw.columns: df_raw[col] = np.nan
            if 'volume' not in df_raw.columns: df_raw['volume'] = 0
            df_raw = df_raw[['open','high','low','close','volume']].copy()
            df_raw = df_raw.dropna(subset=['close'])
            if len(df_raw) < 10: return None
            return df_raw
        except Exception as e:
            if attempt < 2: time.sleep(2)
            else: print(f"    ❌ fetch_index_history {symbol}: {e}")
    return None

# =============================================================================
# BƯỚC 5E: HÀM LẤY DỮ LIỆU 15 PHÚT
# =============================================================================
def fetch_intraday_15m(symbol: str) -> pd.DataFrame | None:
    for attempt in range(3):
        try:
            quote  = Quote(symbol=symbol, source=DATA_SOURCE)
            df_raw = quote.history(length='200', interval='15m')
            if df_raw is None or df_raw.empty: return None
            df_raw['time'] = pd.to_datetime(df_raw['time'])
            df_raw.set_index('time', inplace=True)
            df_raw.columns = [c.lower() for c in df_raw.columns]
            for col in ['open','high','low','close']:
                if col not in df_raw.columns: df_raw[col] = np.nan
            if 'volume' not in df_raw.columns: df_raw['volume'] = 0
            df_raw = df_raw[['open','high','low','close','volume']].copy()
            df_raw = df_raw.dropna(subset=['close'])
            if len(df_raw) < 10: return None
            return compute_indicators(df_raw)
        except Exception as e:
            if attempt < 2: time.sleep(2)
            else: print(f"    ❌ fetch_intraday_15m {symbol}: {e}")
    return None

# =============================================================================
# BƯỚC 5F: FETCH FRESH HOÀN TOÀN CHO ON-DEMAND CHART (không dùng cache)
# =============================================================================
def fetch_fresh_for_chart(symbol: str, current_date: date) -> pd.DataFrame | None:
    """
    Fetch dữ liệu mới hoàn toàn từ server, không phụ thuộc cache.
    Dùng cho on-demand chart và button tín hiệu hôm nay.
    - Lấy toàn bộ lịch sử (length=1000)
    - Không filter ngày → lấy nến mới nhất server có
    - Bỏ nến hôm nay nếu volume < 100 (chưa giao dịch / ngày nghỉ)
    """
    for attempt in range(3):
        try:
            quote  = Quote(symbol=symbol, source=DATA_SOURCE)
            df_raw = quote.history(length='1000', interval='1D')
            if df_raw is None or len(df_raw) < 60: return None
            df_raw['time'] = pd.to_datetime(df_raw['time'])
            df_raw.set_index('time', inplace=True)
            df_raw.columns = [c.lower() for c in df_raw.columns]
            df_raw = df_raw[['open','high','low','close','volume']].copy()

            today_rows = df_raw[df_raw.index.date == current_date]
            if not today_rows.empty:
                vol_today = float(today_rows.iloc[-1].get('volume', 0) or 0)
                if vol_today < 100:
                    df_raw = df_raw[df_raw.index.date < current_date]

            if len(df_raw) < 60: return None
            return df_raw
        except Exception as e:
            if attempt < 2: time.sleep(2)
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


def _build_15m_chart(symbol: str, signal_type: str) -> str | None:
    df_15m = fetch_intraday_15m(symbol)
    if df_15m is None or len(df_15m) < 2:
        print(f"  ⚠️  {symbol}: không có dữ liệu 15m")
        return None
    today_15m    = df_15m.iloc[-1]
    date_str_15m = _date_str_from_df(df_15m)
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

def run_scan_cycle(symbols: list, now_time: int, alerted_today: dict, momentum_today: dict):
    new_signals  = []
    current_momentum = {}
    current_date = datetime.now(TZ_VN).date()
    ts           = datetime.now(TZ_VN).strftime('%H:%M:%S')
    print(f"  [{ts}] Bắt đầu quét {len(symbols)} mã (cache + Quote length=2)...")

    for symbol in symbols:
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
            try:
                momentum_signals = detect_momentum_signals(df_merged)
                if momentum_signals:
                    df_mom = compute_indicators(df_merged)
                    mom_pct = (df_mom['close'].iloc[-1] - df_mom['close'].iloc[-2]) / df_mom['close'].iloc[-2] * 100
                    current_momentum[symbol] = {"signals": momentum_signals, "pct": round(mom_pct, 1)}
            except Exception as e:
                print(f"    ⚠️  Momentum {symbol}: {e}")

            signal_type = detect_signal(df_merged, now_time)
            if not signal_type:
                time.sleep(0.3); continue

            prev_entry = alerted_today.get(symbol)
            prev_sig   = prev_entry["signal"] if isinstance(prev_entry, dict) else prev_entry
            prev_rank  = SIGNAL_RANK.get(prev_sig, 0)
            current_rank = SIGNAL_RANK.get(signal_type, 0)
            if prev_rank >= current_rank:
                time.sleep(0.3); continue

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
            img_daily  = draw_chart(df_plot_d, symbol, signal_type, today,
                                    timeframe='Daily', add_arrow=True, date_str=date_str)
            df_weekly  = build_weekly_df(df_merged)
            df_plot_w  = df_weekly.tail(200).copy()
            today_w    = df_plot_w.iloc[-1]
            date_str_w = _date_str_from_df(df_merged)
            img_weekly = draw_chart(df_plot_w, symbol, signal_type, today_w,
                                    timeframe='Weekly', add_arrow=False, date_str=date_str_w)
            img_15m    = _build_15m_chart(symbol, signal_type)

            image_paths = [img_daily, img_weekly]
            if img_15m: image_paths.append(img_15m)

            notify_text = f"{emoji} #{symbol} | {signal_type} | {date_str}"
            send_telegram_signal(msg, image_paths=image_paths, notify_text=notify_text)

        except Exception as e:
            print(f"  ❌ Lỗi mã {symbol}: {e}")
        time.sleep(0.3)

    momentum_today.clear()
    momentum_today.update(current_momentum)
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
    source = "index"

    if is_index:
        df_raw = fetch_index_history(symbol)
    else:
        with cache_lock:
            cached = history_cache.get(symbol)
            df_raw = cached.copy() if cached is not None and len(cached) >= 10 else None
        if df_raw is not None:
            if _cache_is_fresh(df_raw, current_date, now_time):
                source = "history_cache"
            else:
                print(f"  [ChartCore] {symbol}: cache stale ({df_raw.index[-1].date()} < "
                      f"{_expected_last_session(current_date, now_time)}) → fresh")
                df_raw = None
        if df_raw is None:
            source = "fresh"
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
        "source": source,
        "df_raw": df_raw,
        "df_calc": df_calc,
        "today": today,
        "signal_type": signal_type,
        "date_str": _date_str_from_df(df_calc),
    }

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

def _build_daily_weekly_chart_bytes(ctx):
    png_bytes, labels = [], []
    symbol = ctx["symbol"]
    signal_type = ctx["signal_type"]
    try:
        png_bytes.append(draw_chart(
            ctx["df_calc"].tail(250).copy(), symbol, signal_type, ctx["today"],
            timeframe='Daily', add_arrow=False, date_str=ctx["date_str"], as_bytes=True
        ))
        labels.append('📊 Daily [D]')
    except Exception as e:
        print(f"  [ChartCore] ❌ Daily bytes {symbol}: {e}")

    try:
        df_weekly = build_weekly_df(ctx["df_raw"])
        df_plot_w = df_weekly.tail(200).copy()
        today_w = df_plot_w.iloc[-1]
        date_str_w = _date_str_from_df(ctx["df_raw"])
        png_bytes.append(draw_chart(
            df_plot_w, symbol, signal_type, today_w,
            timeframe='Weekly', add_arrow=False, date_str=date_str_w, as_bytes=True
        ))
        labels.append('📈 Weekly [W]')
    except Exception as e:
        print(f"  [ChartCore] ❌ Weekly bytes {symbol}: {e}")
    return png_bytes, labels

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

        print(f"  🧵 [{thread_id}] {symbol}: dùng nguồn {ctx['source']} (nến cuối: {ctx['df_raw'].index[-1].date()})")
        image_paths, _ = _build_daily_weekly_chart_paths(ctx)
        if not ctx["is_index"]:
            img_15m = _build_15m_chart(symbol, ctx["signal_type"])
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
# BƯỚC 8D: HÀM DASHBOARD CHART — trả về PNG bytes cho web dashboard
# =============================================================================
def dashboard_chart_fn(symbol: str):
    """
    Được truyền vào start_dashboard(fetch_chart_fn=...).
    Tạo nhanh Daily + Weekly từ history_cache nếu có, trả về (list[bytes], list[str]).
    Không gửi Telegram.
    """
    symbol = symbol.upper().strip()
    try:
        ctx = _get_chart_context(symbol)
        if ctx is None:
            return [], []
        print(f"  [DashChart] {symbol}: Daily/Weekly từ {ctx['source']}")
        return _build_daily_weekly_chart_bytes(ctx)

    except Exception as e:
        print(f"  [DashChart] ❌ {symbol}: {e}")
        return [], []

def dashboard_chart_15m_fn(symbol: str):
    """
    Được truyền vào start_dashboard(fetch_chart_15m_fn=...).
    Tạo riêng chart 15m để dashboard tải sau Daily/Weekly.
    """
    symbol = symbol.upper().strip()
    try:
        ctx = _get_chart_context(symbol)
        if ctx is None or ctx["is_index"]:
            return [], []
        df_15m = fetch_intraday_15m(symbol)
        if df_15m is None or len(df_15m) < 2:
            print(f"  ⚠️  {symbol}: không có dữ liệu 15m")
            return [], []
        today_15m = df_15m.iloc[-1]
        date_str_15m = _date_str_from_df(df_15m)
        png_15m = draw_chart(
            df_15m.tail(200).copy(), symbol, ctx["signal_type"], today_15m,
            timeframe='15m', add_arrow=False, date_str=date_str_15m, as_bytes=True
        )
        return [png_15m], ['⚡ 15 phút [15m]']
    except Exception as e:
        print(f"  [DashChart] ❌ 15m {symbol}: {e}")
        return [], []

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
                        reply = "📋 <b>Tín hiệu hôm nay:</b>"
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

alerted_today = {}
momentum_today = {}
last_run_date = datetime.now(TZ_VN).date()
_last_cache_check_ts = 0.0   # cổng nhịp cho check_and_rebuild_cache_if_stale (ngoài giờ, mỗi CACHE_CHECK_INTERVAL_SEC)

_stop_listener  = threading.Event()
listener_thread = threading.Thread(target=telegram_listener, args=(_stop_listener,), daemon=True)
listener_thread.start()

start_dashboard(
    alerted_today_ref = lambda: alerted_today,
    history_cache_ref = lambda: history_cache,
    cache_lock_ref    = cache_lock,
    fetch_heatmap_fn  = fetch_heatmap_data,
    signal_emoji_ref  = SIGNAL_EMOJI,
    signal_rank_ref   = SIGNAL_RANK,
    fetch_chart_fn    = dashboard_chart_fn,
    fetch_chart_15m_fn = dashboard_chart_15m_fn,
    ensure_chart_symbol_fn = ensure_symbol_live_in_cache,
    chart_symbol_status_fn = chart_symbol_status,
    momentum_today_ref = lambda: momentum_today,
    port              = 8888,
)

print("\n" + "="*60)
print("⚙️  AUTO-SCANNER + HEATMAP + TELEGRAM LISTENER + DASHBOARD")
print(f"   Danh sách   : {len(symbols_to_scan)} mã")
print(f"   Cache chart : {len(symbols_to_cache)} mã")
print(f"   Chu kỳ quét : {SCAN_INTERVAL_SEC} giây")
print(f"   Tín hiệu    → Channel/Group: {TELEGRAM_CHAT_ID}")
print(f"   Dashboard   : http://VPS_IP:8888")
print(f"   Lệnh chart  : /c HPG | /chart HPG | /HPG | / HPG")
print(f"   Lệnh chỉ số : /VNINDEX | /VN30 | /HNX | /UPCOM | /VN100")
print(f"   Lệnh heatmap: /h | /heatmap")
print(f"   Lệnh khác   : /s (VIP) | /help")
print(f"   Phân quyền  : VIP (toàn quyền) | Free (tối đa {FREE_CHAT_LIMIT} slot, TTL 30p)")
print(f"   Chart gửi   : Daily [D] + Weekly [W] + 15 phút [15m]")
print(f"   Tín hiệu    : BREAKOUT / POCKET PIVOT / PRE-BREAK")
print(f"                 BOTTOMBREAKP / MA_CROSS / BOTTOMFISH")
print(f"   Nhận lệnh   : Group + Private Chat (24/7)")
print(f"   Cache check : Tự động trước mỗi chu kỳ quét")
print(f"   On-demand   : Ưu tiên cache, fallback fetch fresh")
print(f"   Nghỉ quét   : Thứ 7 và Chủ nhật")
print("="*60)

print("\n🔧 Đang load cache lịch sử lần đầu...")
build_history_cache(symbols_to_cache, last_run_date)

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
            alerted_today.clear()
            momentum_today.clear()
            last_run_date = current_date
            print(f"\n🌅 [{ts}] Ngày mới {current_date.strftime('%d/%m/%Y')} — Reset tín hiệu.")
            print("🔧 Reload cache lịch sử cho ngày mới...")
            build_history_cache(symbols_to_cache, current_date)

        if not _is_trading_session_time(current_date, now_time):
            # Tự dò + tự sửa cache lệch phiên — CHỈ chạy ngoài giờ giao dịch, với nhịp
            # riêng CACHE_CHECK_INTERVAL_SEC (30 phút), hoàn toàn tách khỏi SCAN_INTERVAL_SEC
            # (nhịp quét tín hiệu). Vòng lặp vẫn "thức" mỗi SCAN_INTERVAL_SEC để không lỡ
            # thời điểm mở cửa, nhưng chỉ THỰC SỰ gọi kiểm tra cache mỗi 30 phút 1 lần.
            if time.time() - _last_cache_check_ts >= CACHE_CHECK_INTERVAL_SEC:
                _last_cache_check_ts = time.time()
                check_and_rebuild_cache_if_stale(symbols_to_cache, current_date)

            next_open = _next_trading_session_label(now_time)
            print(f"[{ts}] ⏸  Ngoài giờ giao dịch → Đợi đến {next_open}. Listener + Dashboard vẫn chạy.")
            time.sleep(SCAN_INTERVAL_SEC)
            continue

        # Trong giờ giao dịch: KHÔNG chạy check_and_rebuild_cache_if_stale ở đây nữa
        # (rebuild toàn bộ có thể tốn 45s-vài phút, làm chậm/nghẽn chu kỳ quét tín hiệu).
        # Việc tự dò + tự sửa cache lệch phiên chỉ chạy ở nhánh ngoài giờ giao dịch phía
        # trên (trước 09:00, nghỉ trưa 11:30-13:00, sau 15:00). Ở đây chỉ giữ lại 1 lớp
        # bảo hiểm rẻ: nếu cache trống hoàn toàn (sự cố nghiêm trọng, gần như không xảy ra
        # trong vận hành bình thường) thì vẫn bắt buộc load để tránh quét trên cache rỗng.
        with cache_lock:
            cache_empty = len(history_cache) == 0
        if cache_empty:
            print(f"[{ts}] ⚠️  Cache trống — bắt buộc load trước khi quét...")
            build_history_cache(symbols_to_cache, current_date)

        print(f"\n{'='*60}")
        print(f"🔄 [{ts}] BẮT ĐẦU CHU KỲ QUÉT (cache + Quote length=2)")
        print(f"{'='*60}")

        new_signals = run_scan_cycle(symbols_to_scan, now_time, alerted_today, momentum_today)

        if new_signals:
            print(f"✅ [{ts}] {len(new_signals)} tín hiệu MỚI: {', '.join(new_signals)}")
        else:
            print(f"[{ts}] Không có tín hiệu mới.")

        if alerted_today:
            summary_str = " | ".join([f"{k}:{v['signal']}" for k,v in alerted_today.items()])
            print(f"   📋 Đã báo hôm nay: {summary_str}")
        if momentum_today:
            summary_mom = " | ".join([f"{k}:{'/'.join(v['signals'])}" for k,v in sorted(momentum_today.items())])
            print(f"   ⚡ Động lượng: {summary_mom}")

        print(f"⏳ Đợi {SCAN_INTERVAL_SEC}s cho chu kỳ tiếp theo...")
        time.sleep(SCAN_INTERVAL_SEC)

    except Exception as e:
        ts = datetime.now(TZ_VN).strftime("%H:%M:%S")
        print(f"[{ts}] ❌ Lỗi vòng lặp chính: {e}")
        time.sleep(10)
