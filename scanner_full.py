"""
=============================================================================
SCANNER TÍN HIỆU MUA CỔ PHIẾU — PHIÊN BẢN TỐI ƯU (CACHE + QUOTE length=2)
Pocket Pivot / Breakout / Pre-Break / BottomFish / BottomBreakP / MA_Cross
Tích hợp: vnstock + Telegram + Chart mplfinance + Chống spam + Nghỉ ngoài giờ
+ HEATMAP BOT (lệnh /h hoặc /heatmap)
+ CHỈ SỐ: VNINDEX, VN30 (lệnh /VNINDEX, /VN30, /c VNINDEX ...)

KIẾN TRÚC 2 BƯỚC:
  Bước 1: Load lịch sử 1 lần → cache vào dict  (trước giờ GD hoặc lúc khởi động)
  Bước 2: Mỗi chu kỳ scan chỉ gọi Quote.history(length=2) → ghép vào cache → detect

LỆNH TELEGRAM HỖ TRỢ:
  /c HPG        → chart HPG
  /c HPG VNM    → chart nhiều mã (tối đa 5)
  /chart HPG    → như /c
  /HPG          → chart HPG (không dấu cách)
  / HPG         → chart HPG (có dấu cách)
  /VNINDEX      → chart chỉ số VNINDEX
  /VN30         → chart chỉ số VN30
  /s            → tín hiệu hôm nay
  /h            → heatmap thị trường
  /heatmap      → heatmap thị trường
  /help         → trợ giúp

THAY ĐỔI SO VỚI PHIÊN BẢN CŨ:
  - Thêm hỗ trợ chart chỉ số: VNINDEX, VN30, HNX, UPCOM, VN100, HNXINDEX
  - Thêm 3 tín hiệu mới: BOTTOMFISH, BOTTOMBREAKP, MA_CROSS
  - draw_chart: date_str lấy từ index cây nến cuối của df_plot (không dùng datetime.now)
  - fetch_and_send_chart: date_str lấy từ index cây nến cuối của df_calc
  - fetch_heatmap_data: timestamp parse bằng float() thay vì isinstance check,
    tránh lỗi 01/01/1970 khi val là string dạng "1768552349519"
  - handle_heatmap_command: nhận (data, ts_str) tuple từ fetch_heatmap_data
=============================================================================
"""

# =============================================================================
# BƯỚC 0: CÀI ĐẶT THƯ VIỆN (chạy 1 lần nếu chưa có)
# =============================================================================
#!pip install -U vnstock pandas requests mplfinance pytz pillow

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
import os
import re
import tempfile
from datetime import datetime, date
import pytz
import json
import threading
import math
from PIL import Image, ImageDraw, ImageFont

# =============================================================================
# BƯỚC 2: CẤU HÌNH (dùng chung cho cả scanner và heatmap)
# =============================================================================
VNSTOCK_API         = os.environ.get('VNSTOCK_API')
TELEGRAM_BOT_TOKEN  = os.environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID    = os.environ.get('TELEGRAM_CHAT_ID')
MY_PERSONAL_CHAT_ID = os.environ.get('MY_PERSONAL_CHAT_ID')

DATA_SOURCE        = 'KBS'
SCAN_INTERVAL_SEC  = 120
TZ_VN              = pytz.timezone('Asia/Ho_Chi_Minh')

ALLOWED_CHATS = {str(TELEGRAM_CHAT_ID), str(MY_PERSONAL_CHAT_ID), '1207484510'}

register_user(VNSTOCK_API)

# =============================================================================
# BƯỚC 2A: DANH SÁCH CHỈ SỐ HỖ TRỢ
# =============================================================================
# Mapping tên người dùng gõ → symbol thực tế trên vnstock
# Các chỉ số này được xử lý riêng: không quét tín hiệu, chỉ vẽ chart on-demand
INDEX_SYMBOL_MAP = {
    'VNINDEX':   'VNINDEX',
    'VN30':      'VN30',
    'HNX':       'HNX',
    'HNXINDEX':  'HNXINDEX',
    'UPCOM':     'UPCOM',
    'VN100':     'VN100',
    'VN30F1M':   'VN30F1M',   # Hợp đồng tương lai VN30
}
INDEX_SYMBOLS = set(INDEX_SYMBOL_MAP.keys())

# =============================================================================
# BƯỚC 2B: CẤU HÌNH HEATMAP
# =============================================================================
TRADING_STOCKS_POOL = [
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
        {"name": "BAT DONG SAN", "symbols": ["IJC","LDG","CEO","D2D","DIG","DXG","HDC","HDG","KDH","NLG","NTL","NVL","PDR","SCR","TIG","KBC","SZC"]},
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

HMAP_COLORS = {
    "tran": (250,170,225), "xd": (160,220,170), "xv": (195,235,200),
    "xn":   (225,245,228), "tc": (245,245,200),  "dn": (255,220,210),
    "dv":   (250,185,175), "dd": (240,150,145),  "san":(175,250,255),
}

def _hmap_cell_color(pct):
    if   pct >=  6.5: return HMAP_COLORS["tran"]
    elif pct >=  4.0: return HMAP_COLORS["xd"]
    elif pct >=  2.0: return HMAP_COLORS["xv"]
    elif pct >   0.0: return HMAP_COLORS["xn"]
    elif pct ==  0.0: return HMAP_COLORS["tc"]
    elif pct >= -2.0: return HMAP_COLORS["dn"]
    elif pct >= -4.0: return HMAP_COLORS["dv"]
    elif pct >= -6.5: return HMAP_COLORS["dd"]
    else:             return HMAP_COLORS["san"]

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
    dc(f"{avg_pct:+.2f}%", f_sector, x + w1, w2, fg_s)

def _hmap_avg_pct(syms, data):
    vals = [data[s]["pct"] for s in syms if s in data]
    return round(sum(vals) / len(vals), 2) if vals else 0.0

def _hmap_col_height(groups):
    h = HMAP_TOP_BAR + HMAP_MARGIN
    for g in groups:
        h += (1 + len(g["symbols"])) * HMAP_CELL_H
    return h + HMAP_MARGIN

# =============================================================================
# HEATMAP: fetch + build
# FIX: parse timestamp bằng float() thay vì isinstance — tránh lỗi 01/01/1970
#      khi KBS trả val dạng string "1768552349519"
# =============================================================================
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
                            print(f"  [{ts_log}] ⚠️  time value không hợp lệ: {val_num}")
                            data_time = None
                    except (TypeError, ValueError, OSError) as te:
                        print(f"  [{ts_log}] ⚠️  Không parse được time '{val}': {te}")
                        data_time = None

            for _, row in df.iterrows():
                sym   = str(row.get("symbol", "")).strip()
                if not sym: continue
                close = float(row.get("close_price", 0) or 0) / 1000
                ref_p = float(row.get("reference_price", 0) or 0) / 1000
                if close <= 0 and ref_p > 0:
                    close = ref_p
                pct   = round((close - ref_p) / ref_p * 100, 2) if ref_p > 0 else 0.0
                result[sym] = {"price": close, "pct": pct}

    except Exception as e:
        print(f"  [{ts_log}] ❌ Heatmap API lỗi: {e}")

    if data_time is None:
        data_time = datetime.now(TZ_VN)
        print(f"  [{ts_log}] ⚠️  Heatmap: dùng fallback timestamp = now()")

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
            "text": "🗺 Đang tải dữ liệu heatmap, vui lòng chờ 15–30 giây..."
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
symbols_to_scan = [s for s in all_symbols if s in vn30_symbols]
print(f"🚀 Sẵn sàng quét {len(symbols_to_scan)} mã: {', '.join(symbols_to_scan)}")

# =============================================================================
# BƯỚC 5: HÀM TÍNH CHỈ BÁO
# =============================================================================
def ref(series, n):  return series.shift(n)
def hhv(series, n):  return series.rolling(n).max()
def llv(series, n):  return series.rolling(n).min()

def cross_above(s1, s2):
    """Trả về Series bool: s1 cắt lên s2 (hôm nay s1>=s2, hôm qua s1<s2)."""
    return (s1 >= s2) & (s1.shift(1) < s2.shift(1))

def compute_indicators(df):
    df = df.copy()
    for n in [2,3,5,10,15,20,30,50,200]:
        df[f'MA{n}']  = df['close'].rolling(n).mean()
    for n in [10,20,30,50]:
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
cache_date          = None
cache_lock          = threading.Lock()

def load_history_for_symbol(symbol: str, current_date: date):
    for attempt in range(3):
        try:
            quote  = Quote(symbol=symbol, source=DATA_SOURCE)
            df_raw = quote.history(length='1000', interval='1D')
            if df_raw is None or len(df_raw) < 60: return None
            df_raw['time'] = pd.to_datetime(df_raw['time'])
            df_raw.set_index('time', inplace=True)
            df_raw.columns = [c.lower() for c in df_raw.columns]
            df_raw = df_raw[df_raw.index.date < current_date]
            return df_raw[['open','high','low','close','volume']].copy()
        except Exception as e:
            if attempt < 2: time.sleep(2)
            else: print(f"    ❌ Load history {symbol}: {e}")
    return None

def build_history_cache(symbols: list, current_date: date):
    global cache_date
    ts = datetime.now(TZ_VN).strftime('%H:%M:%S')
    print(f"\n📦 [{ts}] Bắt đầu load cache lịch sử cho {len(symbols)} mã...")
    loaded = 0
    for i, symbol in enumerate(symbols, 1):
        df = load_history_for_symbol(symbol, current_date)
        if df is not None and len(df) >= 60:
            with cache_lock: history_cache[symbol] = df
            loaded += 1
        if i % 20 == 0:
            ts2 = datetime.now(TZ_VN).strftime('%H:%M:%S')
            print(f"  [{ts2}] Đã load {i}/{len(symbols)} mã...")
        time.sleep(0.3)
    with cache_lock: cache_date = current_date
    ts = datetime.now(TZ_VN).strftime('%H:%M:%S')
    print(f"✅ [{ts}] Cache hoàn tất: {loaded}/{len(symbols)} mã có dữ liệu.")

def fetch_today_bar(symbol: str, current_date: date):
    """Lấy nến ngày hôm nay. Trả về None nếu dữ liệu rác hoặc chưa có khớp lệnh thực."""
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

            if pd.isna(close) or close <= 0:
                return None

            if pd.isna(volume) or volume < 100:
                return None

            prev_rows = df_raw[df_raw.index.date < current_date]
            if not prev_rows.empty:
                prev = prev_rows.iloc[-1]
                prev_close  = float(prev.get('close',  np.nan))
                prev_volume = float(prev.get('volume', np.nan))
                prev_open   = float(prev.get('open',   np.nan))
                prev_high   = float(prev.get('high',   np.nan))
                prev_low    = float(prev.get('low',    np.nan))

                ohlcv_clone = (
                    close  == prev_close  and
                    open_  == prev_open   and
                    high   == prev_high   and
                    low    == prev_low    and
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

def merge_today_bar(df_hist, today_bar, current_date: date):
    today_ts = pd.Timestamp(current_date)
    new_row  = pd.DataFrame([today_bar], index=[today_ts])
    df_hist  = df_hist[df_hist.index.date < current_date]
    return pd.concat([df_hist, new_row])

# =============================================================================
# BƯỚC 5C: HÀM TIỆN ÍCH — LẤY DATE_STR TỪ INDEX CÂY NẾN CUỐI
# =============================================================================
def _date_str_from_df(df: pd.DataFrame) -> str:
    """Trả về chuỗi 'dd/mm/yyyy' từ index (Timestamp) của cây nến cuối cùng."""
    last_ts = pd.Timestamp(df.index[-1])
    if last_ts.tzinfo is None:
        last_ts = last_ts.tz_localize('Asia/Ho_Chi_Minh')
    return last_ts.strftime('%d/%m/%Y')

# =============================================================================
# BƯỚC 5D: HÀM LẤY DỮ LIỆU CHỈ SỐ (VNINDEX, VN30, ...)
# =============================================================================
def fetch_index_history(symbol: str) -> pd.DataFrame | None:
    """
    Lấy lịch sử chỉ số (VNINDEX, VN30, ...) qua Quote.
    Một số data source trả về điểm số ở cột 'close' — không cần chia 1000.
    Trả về DataFrame chuẩn (open/high/low/close/volume) hoặc None nếu lỗi.
    """
    for attempt in range(3):
        try:
            quote  = Quote(symbol=symbol, source=DATA_SOURCE)
            df_raw = quote.history(length='1000', interval='1D')
            if df_raw is None or df_raw.empty:
                return None
            df_raw['time'] = pd.to_datetime(df_raw['time'])
            df_raw.set_index('time', inplace=True)
            df_raw.columns = [c.lower() for c in df_raw.columns]

            # Đảm bảo có đủ cột OHLCV
            for col in ['open','high','low','close']:
                if col not in df_raw.columns:
                    df_raw[col] = np.nan
            if 'volume' not in df_raw.columns:
                df_raw['volume'] = 0

            df_raw = df_raw[['open','high','low','close','volume']].copy()
            df_raw = df_raw.dropna(subset=['close'])
            if len(df_raw) < 10:
                return None
            return df_raw
        except Exception as e:
            if attempt < 2:
                time.sleep(2)
            else:
                print(f"    ❌ fetch_index_history {symbol}: {e}")
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

# =============================================================================
# BƯỚC 6B: CÁC TÍN HIỆU MỚI
# =============================================================================

def calc_bottomfish(df):
    """
    BOTTOMFISH: RSI(14) cắt lên 29 hoặc 30, kết hợp vùng dao động đủ rộng và thanh khoản.
    Tương đương:
        (Cross(r,29) OR Cross(r,30))
        AND (HHV(H,30)-LLV(L,30))/LLV(L,30) >= 0.2
        AND C >= 5
        AND V*C > 2_000_000
        AND MA(V,5)*MA(C,5) > 3_000_000  ...  MA(V,50) >= 50_000
    """
    C, V = df['close'], df['volume']
    r    = df['RSI14']
    H, L = df['high'], df['low']

    # RSI cắt lên 29 hoặc 30 (cross_above)
    rsi_cross = cross_above(r, pd.Series(29, index=r.index)) | \
                cross_above(r, pd.Series(30, index=r.index))

    # Biên độ vùng 30 phiên >= 20%
    range30 = (hhv(H, 30) - llv(L, 30)) / llv(L, 30)
    cond_range = range30 >= 0.2

    # Thanh khoản
    liq = (
        (C >= 5) &
        (C * V > 2_000_000) &
        (df['MA5']  * df['VMA5']  > 3_000_000) &
        (df['MA10'] * df['VMA10'] > 3_000_000) &
        (df['MA15'] * df['VMA15'] > 3_000_000) &
        (df['VMA30'] >= 50_000) &
        (df['VMA20'] >= 50_000) &
        (df['VMA10'] >= 50_000) &
        (df['VMA50'] >= 50_000)
    )

    return rsi_cross & cond_range & liq


def calc_bottombreakp(df):
    """
    BOTTOMBREAKP: Bứt phá đáy — RSI vừa cắt lên 29 (hôm nay hoặc hôm qua),
    nến tăng mạnh, khối lượng breakout, vùng dao động rộng.
    """
    C, O, H, L, V = df['close'], df['open'], df['high'], df['low'], df['volume']
    r = df['RSI14']

    # RSI cắt lên 29 hôm nay hoặc hôm qua
    rsi_cross_today = cross_above(r, pd.Series(29, index=r.index))
    rsi_cross_prev  = cross_above(r.shift(1), pd.Series(29, index=r.index))
    rsi_cond = rsi_cross_today | rsi_cross_prev

    # Nến tăng mạnh (High close bar)
    high_close_bar = (
        ((C > (H + L) / 2) & (C >= O)) |
        (((O - ref(C, 1)) / ref(C, 1) > 0.02) & ((C - ref(C, 1)) / ref(C, 1) > 0.02))
    )

    # Râu nến không quá dài
    short_wick = (
        ((H - C) / C < 0.02) |
        (((H - C) / C >= 0.02) & ((C - O) / O >= 1.1 * (H - C) / C))
    )

    # Điều kiện giá
    price_cond = (
        (C >= 1.015 * ref(C, 1)) &
        (C > (ref(L, 1) + ref(H, 1)) / 2)
    )

    # Biên độ vùng 30 phiên >= 20%
    range30    = (hhv(H, 30) - llv(L, 30)) / llv(L, 30)
    cond_range = range30 >= 0.2

    # Khối lượng breakout (dùng lại calc_break_vol)
    bvol = calc_break_vol(df)

    # Thanh khoản
    liq = (
        (C >= 5) &
        (C * V > 3_000_000) &
        (df['MA5']  * df['VMA5']  > 3_000_000) &
        (df['MA10'] * df['VMA10'] > 3_000_000) &
        (df['MA15'] * df['VMA15'] > 3_000_000) &
        (df['VMA30'] >= 50_000) &
        (df['VMA20'] >= 50_000) &
        (df['VMA10'] >= 50_000) &
        (df['VMA50'] >= 50_000)
    )

    bottombreakprice = rsi_cond & high_close_bar & short_wick & price_cond & cond_range
    return bottombreakprice & bvol & liq


def calc_ma_cross(df):
    """
    MA_CROSS: MA10 cắt lên MA20/MA30/MA50, tất cả MA trên MA200, giá gần MA30.
    """
    C, V = df['close'], df['volume']

    # MA10 cắt lên MA20, MA30 hoặc MA50
    cross_10_20 = cross_above(df['MA10'], df['MA20'])
    cross_10_30 = cross_above(df['MA10'], df['MA30'])
    cross_10_50 = cross_above(df['MA10'], df['MA50'])
    ma_cross_cond = cross_10_20 | cross_10_30 | cross_10_50

    # MA nằm trên MA200
    ma_above_200 = (
        (df['MA10'] > df['MA200']) &
        (df['MA30'] > df['MA200']) &
        (df['MA50'] > df['MA200'])
    )

    # Giá trong vùng hợp lý so với MA30
    price_cond = (
        (C > df['MA30']) &
        (C <= 1.07 * df['MA30'])
    )

    # Thanh khoản
    liq = (
        (C >= 5) &
        (C * V > 2_000_000) &
        (df['MA5']  * df['VMA5']  > 3_000_000) &
        (df['MA10'] * df['VMA10'] > 3_000_000) &
        (df['MA15'] * df['VMA15'] > 3_000_000) &
        (df['VMA30'] >= 50_000) &
        (df['VMA20'] >= 50_000) &
        (df['VMA10'] >= 50_000) &
        (df['VMA50'] >= 50_000)
    )

    return ma_cross_cond & ma_above_200 & price_cond & liq

# =============================================================================
# BƯỚC 6C: HÀM DETECT_SIGNAL (bổ sung 3 tín hiệu mới)
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

    # Tín hiệu chính (ưu tiên cao nhất)
    is_breakout = (break_price & break_vol & liq).iloc[-1]
    is_pocket   = (pprice & (pvol | break_vol) & liq & ma10_ok).iloc[-1]
    is_prebreak = (
        ((break_price | pprice) & pre_vol & liq).iloc[-1] and
        not is_breakout and not is_pocket and
        (91700 < now_time < 150000)
    )

    # Tín hiệu mới (ưu tiên thấp hơn, chỉ phát khi không có tín hiệu chính)
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
def draw_chart(df_plot, symbol, signal_type, today, timeframe='Daily', add_arrow=True, date_str=None):
    is_daily  = (timeframe == 'Daily')

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
        apds.append(mpf.make_addplot(df_plot['MA200'], color='brown', width=0.6))

    mc           = mpf.make_marketcolors(up='#26A69A',down='#EF5350',edge='inherit',wick='inherit',alpha=1.0)
    custom_style = mpf.make_mpf_style(base_mpf_style='charles',marketcolors=mc,gridstyle='',facecolor='white')

    fd, img_name = tempfile.mkstemp(suffix=f'_{symbol}_{timeframe.lower()}.png')
    os.close(fd)

    fig, axlist = mpf.plot(
        df_plot, type='candle', volume=False, addplot=apds,
        style=custom_style, savefig=dict(fname=img_name, dpi=150),
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
    else:
        title_str = (
            f"{symbol} [W] {date_str}  | "
            f"O:{today['open']:.2f}  H:{today['high']:.2f}  "
            f"L:{today['low']:.2f}  C:{today['close']:.2f} ({pct:+.2f}%)"
        )
    ax_price.set_title(title_str, loc='left', fontsize=11)

    if len(axlist) > 4:
        ax_macd = axlist[4]
        ax_macd.yaxis.set_ticks([]); ax_macd.yaxis.set_ticklabels([])
        m_vals = pd.concat([df_plot['MACD'],df_plot['MACD_Signal'],df_plot['MACD_Hist']]).dropna()
        abs_m  = max(abs(m_vals.min()),abs(m_vals.max())) if len(m_vals) > 0 else 1
        ax_macd.set_ylim(-abs_m*0.8, abs_m*1.2)
        for spine in ['top','right','left','bottom']: ax_macd.spines[spine].set_visible(False)

    for i, ax in enumerate(axlist):
        if i not in [0,4]: ax.set_axis_off()
        else: ax.xaxis.set_visible(False); ax.spines['top'].set_visible(False); ax.spines['left'].set_visible(False)

    xlim = ax_price.get_xlim()
    ax_price.set_xlim(xlim[0], xlim[1]+20)
    fig.savefig(img_name, bbox_inches='tight', pad_inches=0.15, dpi=150)
    plt.close('all')
    return img_name

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

def run_scan_cycle(symbols: list, now_time: int, alerted_today: dict):
    new_signals  = []
    current_date = datetime.now(TZ_VN).date()
    ts           = datetime.now(TZ_VN).strftime('%H:%M:%S')
    print(f"  [{ts}] Bắt đầu quét {len(symbols)} mã (cache + Quote length=2)...")

    for symbol in symbols:
        try:
            with cache_lock: df_hist = history_cache.get(symbol)
            if df_hist is None or len(df_hist) < 60: continue

            today_bar = fetch_today_bar(symbol, current_date)
            if today_bar is None: continue

            df_merged   = merge_today_bar(df_hist, today_bar, current_date)
            signal_type = detect_signal(df_merged, now_time)
            if not signal_type:
                time.sleep(0.3); continue

            prev_rank    = SIGNAL_RANK.get(alerted_today.get(symbol), 0)
            current_rank = SIGNAL_RANK.get(signal_type, 0)
            if prev_rank >= current_rank:
                time.sleep(0.3); continue

            alerted_today[symbol] = signal_type
            new_signals.append(symbol)

            df_calc      = compute_indicators(df_merged)
            today        = df_calc.iloc[-1]
            date_str     = _date_str_from_df(df_calc)
            pct          = (today['close']-df_calc['close'].iloc[-2])/df_calc['close'].iloc[-2]*100
            change       = today['close'] - df_calc['close'].iloc[-2]
            emoji        = SIGNAL_EMOJI.get(signal_type, '📌')
            vol_vs_prev  = (today['volume']-df_calc['volume'].iloc[-2])/df_calc['volume'].iloc[-2]*100
            vol_vs_vma50 = (today['volume']-today['VMA50'])/today['VMA50']*100 if today['VMA50']>0 else 0

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
            date_str_w = _date_str_from_df(df_plot_w)
            img_weekly = draw_chart(df_plot_w, symbol, signal_type, today_w,
                                    timeframe='Weekly', add_arrow=False, date_str=date_str_w)

            notify_text = f"{emoji} #{symbol} | {signal_type} | {date_str}"
            send_telegram_signal(msg, image_paths=[img_daily, img_weekly], notify_text=notify_text)

        except Exception as e:
            print(f"  ❌ Lỗi mã {symbol}: {e}")
        time.sleep(0.3)

    return new_signals

# =============================================================================
# BƯỚC 8B: PARSE LỆNH CHART
# Mở rộng: nhận diện chỉ số (VNINDEX, VN30, ...) dù là reserved keyword
# =============================================================================
_RESERVED_KEYWORDS = {'s','help','h','scan','c','chart','heatmap'}

def parse_chart_command(text: str):
    """
    Phân tích lệnh chart từ tin nhắn Telegram.
    Trả về list symbol (có thể là mã CK lẫn chỉ số) hoặc None nếu không phải lệnh chart.
    """
    text = text.strip()
    if not text.startswith('/'): return None
    body = text[1:]

    # /c VNINDEX hoặc /chart VN30 HPG
    m = re.match(r'^(c|chart)\s+(.+)$', body, re.IGNORECASE)
    if m: return _filter_symbols(m.group(2).split())

    # / VNINDEX (có khoảng trắng sau /)
    if body.startswith(' '): return _filter_symbols(body.strip().split())

    # /VNINDEX hoặc /HPG (không khoảng trắng)
    parts = body.strip().split()
    if len(parts) == 1:
        candidate = parts[0].upper()
        # Chỉ số được ưu tiên nhận diện trước khi kiểm tra reserved keyword
        if candidate in INDEX_SYMBOLS:
            return [candidate]
        if candidate.lower() not in _RESERVED_KEYWORDS and _is_valid_symbol(candidate):
            return [candidate]
    return None

def _is_valid_symbol(s: str) -> bool:
    """Hợp lệ: 1–7 ký tự chữ số/chữ cái, không phải reserved (trừ chỉ số)."""
    s_upper = s.upper()
    if s_upper in INDEX_SYMBOLS:
        return True
    return bool(re.match(r'^[A-Z0-9]{1,5}$', s_upper)) and s.lower() not in _RESERVED_KEYWORDS

def _filter_symbols(raw_list: list):
    result = [s.upper() for s in raw_list if _is_valid_symbol(s)]
    return result if result else None

# =============================================================================
# BƯỚC 8C: HÀM GỬI CHART ON-DEMAND (hỗ trợ cả chỉ số)
# =============================================================================
def fetch_and_send_chart(symbol, chat_id):
    thread_id = threading.current_thread().ident
    symbol    = symbol.upper().strip()
    url_msg   = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    is_index  = symbol in INDEX_SYMBOLS
    print(f"  🧵 [{thread_id}] fetch_and_send_chart BẮT ĐẦU: {symbol} (index={is_index})")

    try:
        current_date = datetime.now(TZ_VN).date()

        # ── Lấy dữ liệu ─────────────────────────────────────────────────
        if is_index:
            # Chỉ số: luôn fetch thẳng, không dùng cache cổ phiếu
            df_raw = fetch_index_history(symbol)
            if df_raw is None or len(df_raw) < 10:
                requests.post(url_msg, data={
                    'chat_id': chat_id,
                    'text': f"❌ Không tìm thấy dữ liệu chỉ số <b>{symbol}</b>. "
                            f"Hãy kiểm tra tên chỉ số (VD: VNINDEX, VN30, HNX, UPCOM).",
                    'parse_mode': 'HTML'
                })
                return
           
        else:
            # Cổ phiếu thường: ưu tiên cache
            with cache_lock: df_hist = history_cache.get(symbol)
            if df_hist is not None and len(df_hist) >= 60:
                today_bar = fetch_today_bar(symbol, current_date)
                if today_bar is not None:
                    df_raw = merge_today_bar(df_hist, today_bar, current_date)
                else:
                    df_raw = df_hist.copy()
                    ts_log = datetime.now(TZ_VN).strftime('%H:%M:%S')
                    print(f"  [{ts_log}] ⚠️  {symbol}: chưa có nến hôm nay → vẽ lịch sử thuần")
            else:
                quote  = Quote(symbol=symbol, source=DATA_SOURCE)
                df_raw = quote.history(length='1000', interval='1D')
                if df_raw is None or len(df_raw) < 60:
                    requests.post(url_msg, data={
                        'chat_id': chat_id,
                        'text': f"❌ Không tìm thấy dữ liệu cho mã <b>{symbol}</b>",
                        'parse_mode': 'HTML'
                    })
                    return
                df_raw['time'] = pd.to_datetime(df_raw['time'])
                df_raw.set_index('time', inplace=True)
                df_raw.columns = [c.lower() for c in df_raw.columns]

        # ── Tính chỉ báo ─────────────────────────────────────────────────
        df_calc     = compute_indicators(df_raw)
        today       = df_calc.iloc[-1]
        now_time    = int(datetime.now(TZ_VN).strftime("%H%M%S"))

        # Chỉ số không detect tín hiệu mua — chỉ vẽ chart
        if is_index:
            signal_type = "INDEX"
        else:
            signal_type = detect_signal(df_raw, now_time) or "ON-DEMAND"

        date_str     = _date_str_from_df(df_calc)
        pct          = (today['close']-df_calc['close'].iloc[-2])/df_calc['close'].iloc[-2]*100
        change       = today['close'] - df_calc['close'].iloc[-2]
        vol_vs_prev  = (today['volume']-df_calc['volume'].iloc[-2])/df_calc['volume'].iloc[-2]*100
        vol_vs_vma50 = (today['volume']-today['VMA50'])/today['VMA50']*100 if today['VMA50']>0 else 0

        # Chỉ số: không thêm link tổng quan CK
        if is_index:
            msg = (
                f"#{symbol}  {date_str}\n"
                f"Clo: <b>{today['close']:.2f}</b> ({change:+.2f} / {pct:+.2f}%)\n"
                f"Vol: {vol_vs_prev:+.1f}% | {vol_vs_vma50:+.1f}%"
            )
        else:
            link_vnd_detail  = f"https://dstock.vndirect.com.vn/tong-quan/{symbol}/diem-nhan-co-ban-popup"
            link_vnd_news    = f"https://dstock.vndirect.com.vn/tong-quan/{symbol}/tin-tuc-ma-popup?type=dn"
            link_vietstock   = f"https://stockchart.vietstock.vn/?stockcode={symbol}"
            link_vnd_summary = f"https://dstock.vndirect.com.vn/tong-quan/{symbol}"
            link_24h_money   = f"https://24hmoney.vn/stock/{symbol}/news"
            msg = (
                f"#{symbol}  {date_str}\n"
                f"Sig: {signal_type}\n"
                f"Clo: <b>{today['close']:.2f}</b> ({change:+.2f} / {pct:+.2f}%)\n"
                f"Vol: {vol_vs_prev:+.1f}% | {vol_vs_vma50:+.1f}%\n"
                f"<a href='{link_vnd_detail}'>⚖️</a> "
                f"<a href='{link_vnd_news}'>🗞️</a> "
                f"<a href='{link_vietstock}'>📈</a> "
                f"<a href='{link_vnd_summary}'>📄</a> "
                f"<a href='{link_24h_money}'>📄</a>"
            )

        df_plot_d  = df_calc.tail(250).copy()
        img_daily  = draw_chart(df_plot_d, symbol, signal_type, today,
                                timeframe='Daily', add_arrow=False, date_str=date_str)
        df_weekly  = build_weekly_df(df_raw)
        df_plot_w  = df_weekly.tail(200).copy()
        today_w    = df_plot_w.iloc[-1]
        date_str_w = _date_str_from_df(df_plot_w)
        img_weekly = draw_chart(df_plot_w, symbol, signal_type, today_w,
                                timeframe='Weekly', add_arrow=False, date_str=date_str_w)

        print(f"  🧵 [{thread_id}] {symbol} — chuẩn bị gửi chart")
        _send_chart_to_chat(msg, [img_daily, img_weekly], chat_id)
        print(f"  🧵 [{thread_id}] {symbol} — đã gửi xong")

    except Exception as e:
        print(f"  🧵 [{thread_id}] {symbol} — LỖI: {e}")
        requests.post(url_msg, data={
            'chat_id': chat_id,
            'text': f"❌ Lỗi lấy dữ liệu <b>{symbol}</b>: {e}",
            'parse_mode': 'HTML'
        })

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
# BƯỚC 8D: TELEGRAM LISTENER
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
    print(f"🎧 Listener sẵn sàng | chat được phép: {ALLOWED_CHATS}")

    while not stop_event.is_set():
        try:
            resp = requests.get(url_upd, params={
                'offset': offset, 'timeout': 30, 'allowed_updates': ['message', 'callback_query'],
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

                # ── Xử lý callback_query (nhấn nút)
                callback = update.get('callback_query', {})
                if callback:
                    cb_id      = callback.get('id')
                    cb_data    = callback.get('data', '')
                    cb_chat_id = callback.get('message', {}).get('chat', {}).get('id')
                    requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                        data={'callback_query_id': cb_id}
                    )
                    if str(cb_chat_id) in ALLOWED_CHATS and cb_data.startswith('chart_'):
                        sym = cb_data.replace('chart_', '').upper()
                        print(f"  📥 Callback chart {sym} → chat_id={cb_chat_id}")
                        threading.Thread(
                            target=fetch_and_send_chart,
                            args=(sym, cb_chat_id),
                            daemon=True
                        ).start()
                    continue  
                
                # ── Xử lý message thông thường
                msg_obj = update.get('message', {})
                text    = msg_obj.get('text', '').strip()
                chat_id = msg_obj.get('chat', {}).get('id')
                if not text or not chat_id: continue
              
                if str(chat_id) not in ALLOWED_CHATS:
                    print(f"  🚫 chat_id {chat_id} không được phép"); continue

                text_lower = text.lower().strip()

               # ── /s — tín hiệu hôm nay ────────────────────────────────
                if text_lower == '/s' or text_lower.startswith('/s '):
                    if alerted_today:
                        buttons = []
                        # Tìm độ dài chuỗi dài nhất
                        max_len = max(len(f"#{k}: {v}") for k, v in alerted_today.items())
                    
                        for k, v in alerted_today.items():
                            emoji   = SIGNAL_EMOJI.get(v, '📌')
                            content = f"#{k}: {v}"
                            padding = "\u00A0" * (max_len - len(content)) * 3  # \u00A0 = non-breaking space
                            buttons.append([
                                {"text": f"{emoji} {content}{padding}", "callback_data": f"chart_{k}"},
                            ])
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

                # ── /h hoặc /heatmap — market heatmap ────────────────────
                elif text_lower in ('/h', '/heatmap'):
                    print(f"  🗺  Lệnh heatmap từ chat_id={chat_id}")
                    threading.Thread(
                        target=handle_heatmap_command,
                        args=(chat_id,),
                        daemon=True
                    ).start()

                # ── /help ─────────────────────────────────────────────────
                elif text_lower == '/help' or text_lower.startswith('/help '):
                    requests.post(url_msg, data={
                        'chat_id': chat_id, 'parse_mode': 'HTML',
                        'text': (
                            "🤖 <b>Lệnh hỗ trợ:</b>\n\n"
                            "<b>Xem chart cổ phiếu:</b>\n"
                            "/c HPG\n/chart HPG\n/HPG\n/ HPG\n"
                            "/c HPG VNM FPT  (nhiều mã, tối đa 5)\n\n"
                            "<b>Xem chart chỉ số:</b>\n"
                            "/VNINDEX  /VN30  /HNX  /UPCOM  /VN100\n"
                            "/c VNINDEX VN30  (nhiều chỉ số)\n\n"
                            "<b>Heatmap thị trường:</b>\n"
                            "/h  hoặc  /heatmap\n\n"
                            "<b>Khác:</b>\n"
                            "/s  — Tín hiệu hôm nay\n"
                            "/help  — Trợ giúp\n\n"
                            "<b>Tín hiệu hỗ trợ:</b>\n"
                            "🟢 BREAKOUT  🟡 POCKET PIVOT  🟣 PRE-BREAK\n"
                            "🔵 BOTTOMBREAKP  🟠 BOTTOMFISH  ⚪ MA_CROSS"
                        )
                    })

                # ── chart on-demand ───────────────────────────────────────
                else:
                    symbols = parse_chart_command(text)
                    if symbols:
                        print(f"  🔍 {text!r} → {symbols} | update_id={update_id}")
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
last_run_date = datetime.now(TZ_VN).date()

_stop_listener  = threading.Event()
listener_thread = threading.Thread(target=telegram_listener, args=(_stop_listener,), daemon=True)
listener_thread.start()

print("\n" + "="*60)
print("⚙️  AUTO-SCANNER + HEATMAP + TELEGRAM LISTENER ĐÃ KÍCH HOẠT")
print(f"   Danh sách   : {len(symbols_to_scan)} mã")
print(f"   Chu kỳ quét : {SCAN_INTERVAL_SEC} giây")
print(f"   Tín hiệu    → Channel/Group: {TELEGRAM_CHAT_ID}")
print(f"   Lệnh chart  : /c HPG | /chart HPG | /HPG | / HPG")
print(f"   Lệnh chỉ số : /VNINDEX | /VN30 | /HNX | /UPCOM | /VN100")
print(f"   Lệnh heatmap: /h | /heatmap")
print(f"   Lệnh khác   : /s | /help")
print(f"   Tín hiệu    : BREAKOUT / POCKET PIVOT / PRE-BREAK")
print(f"                 BOTTOMBREAKP / MA_CROSS / BOTTOMFISH")
print(f"   Nhận lệnh   : Group + Private Chat (24/7)")
print("="*60)

print("\n🔧 Đang load cache lịch sử lần đầu...")
build_history_cache(symbols_to_scan, last_run_date)

# =============================================================================
# VÒNG LẶP CHÍNH
# =============================================================================
while True:
    now_obj      = datetime.now(TZ_VN)
    current_date = now_obj.date()
    now_time     = int(now_obj.strftime("%H%M%S"))
    ts           = now_obj.strftime("%H:%M:%S")

    if current_date > last_run_date:
        alerted_today.clear()
        last_run_date = current_date
        print(f"\n🌅 [{ts}] Ngày mới {current_date.strftime('%d/%m/%Y')} — Reset tín hiệu.")
        print("🔧 Reload cache lịch sử cho ngày mới...")
        build_history_cache(symbols_to_scan, current_date)

    is_morning   = 85000 <= now_time <= 113000
    is_afternoon = 130000 <= now_time <= 150000

    if not (is_morning or is_afternoon):
        with cache_lock: cache_ok = (cache_date == current_date and len(history_cache) > 0)
        if not cache_ok:
            print(f"[{ts}] Cache chưa có — tải lịch sử trước giờ giao dịch...")
            build_history_cache(symbols_to_scan, current_date)

        if   now_time < 85000:  next_open = "09:00"
        elif now_time < 130000: next_open = "13:00"
        else:                   next_open = "09:00 ngày mai"
        print(f"[{ts}] ⏸  Ngoài giờ giao dịch → Đợi đến {next_open}. Listener vẫn chạy.")
        time.sleep(SCAN_INTERVAL_SEC)
        continue

    with cache_lock: cache_ok = (cache_date == current_date and len(history_cache) > 0)
    if not cache_ok:
        print(f"[{ts}] ⚠️  Cache chưa sẵn sàng — đang load...")
        build_history_cache(symbols_to_scan, current_date)

    print(f"\n{'='*60}")
    print(f"🔄 [{ts}] BẮT ĐẦU CHU KỲ QUÉT (cache + Quote length=2)")
    print(f"{'='*60}")

    new_signals = run_scan_cycle(symbols_to_scan, now_time, alerted_today)

    if new_signals:
        print(f"✅ [{ts}] {len(new_signals)} tín hiệu MỚI: {', '.join(new_signals)}")
    else:
        print(f"[{ts}] Không có tín hiệu mới.")

    if alerted_today:
        summary_str = " | ".join([f"{k}:{v}" for k,v in alerted_today.items()])
        print(f"   📋 Đã báo hôm nay: {summary_str}")

    print(f"⏳ Đợi {SCAN_INTERVAL_SEC}s cho chu kỳ tiếp theo...")
    time.sleep(SCAN_INTERVAL_SEC)
