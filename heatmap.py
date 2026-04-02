# ╔══════════════════════════════════════════════════════════════╗
# ║         HEATMAP BOT - CHẠY TRÊN GOOGLE COLAB               ║
# ║  Copy từng CELL vào Colab, chạy theo thứ tự từ trên xuống  ║
# ╚══════════════════════════════════════════════════════════════╝
 
 
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CELL 1 — Cài thư viện (chạy 1 lần duy nhất)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

!pip install -q vnstock pillow requests pytz
 
 # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CELL 2 — Load toàn bộ code (chạy 1 lần)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

import os, time, math, requests, pytz
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont
from vnstock import Trading, register_user

# ── SỬA 2 DÒNG NÀY ─────────────────────────────────────────────
VNSTOCK_API_KEY    = "vnstock_a9d67f9dca5fd7d8c9716f4357a33565"
TELEGRAM_BOT_TOKEN = "......"
# ────────────────────────────────────────────────────────────────

TZ_VN = pytz.timezone("Asia/Ho_Chi_Minh")
def _ts(): return datetime.now(TZ_VN).strftime("%H:%M:%S")

# ── DANH SÁCH TRADING STOCKS ────────────────────────────────────
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

# ── CÁC CỘT NGÀNH (col 1–7) ─────────────────────────────────────
OTHER_COLUMNS = [
    {"col": 1, "groups": [
        {"name": "VN30", "symbols": [
            "FPT","GAS","NVL","VNM","VCB","PLX","TCB","MWG","STB","HPG","PNJ",
            "BID","CTG","HDB","VJC","VPB","KDH","MBB","VHM","POW","VRE","MSN",
            "SSI","ACB","BVH","GVR","TPB",
        ]},
    ]},
    {"col": 2, "groups": [
        {"name": "NGAN HANG", "symbols": ["VCB","BID","CTG","MBB","ACB","TCB","TPB","HDB","SHB","STB","VIB","VPB","MSB","ABB","BVB","LPB"]},
        {"name": "DAU KHI",   "symbols": ["GAS","PVD","PVS","BSR","OIL","PVB","PVC","PLX","PET"]},
    ]},
    {"col": 3, "groups": [
        {"name": "CHUNG KHOAN","symbols": ["SSI","VND","CTS","FTS","HCM","MBS","DSE","BSI","SHS","VCI","VCK","ORS"]},
        {"name": "XAY DUNG",   "symbols": ["C47","C32","L14","CII","CTD","CTI","FCN","HBC","HUT","LCG","PC1","DPG","PHC","VCG"]},
    ]},
    {"col": 4, "groups": [
        {"name": "BAT DONG SAN","symbols": ["IJC","LDG","CEO","D2D","DIG","DXG","HDC","HDG","KDH","NLG","NTL","NVL","PDR","SCR","TIG","KBC","SZC"]},
        {"name": "PHAN BON",    "symbols": ["BFC","DCM","DPM"]},
        {"name": "THEP",        "symbols": ["HPG","HSG","NKG"]},
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
        {"name": "DAU TU CONG","symbols": ["FCN","HHV","LCG","VCG","C4G","CTD","HBC","HSG","NKG","HPG","KSB","PLC"]},
    ]},
]

# ── MÀU SẮC (phiên bản nhạt, nền trắng) ────────────────────────
COLORS = {
    "tran":   (250, 170, 225),   # Tím nhạt   >= +6.5%
    "xd":     (160, 220, 170),   # Xanh đậm   >= +4.0%
    "xv":     (195, 235, 200),   # Xanh vừa   >= +2.0%
    "xn":     (225, 245, 228),   # Xanh nhạt  > 0%
    "tc":     (245, 245, 200),   # Vàng nhạt  = 0% (tham chiếu)
    "dn":     (255, 220, 210),   # Đỏ nhạt    >= -2.0%
    "dv":     (250, 185, 175),   # Đỏ vừa     >= -4.0%
    "dd":     (240, 150, 145),   # Đỏ đậm     >= -6.5%
    "san":    (175, 250, 255),   # Xanh da trời sáng < -6.5%
}

def get_cell_color(pct: float) -> tuple:
    if   pct >=  6.5: return COLORS["tran"]
    elif pct >=  4.0: return COLORS["xd"]
    elif pct >=  2.0: return COLORS["xv"]
    elif pct >   0.0: return COLORS["xn"]
    elif pct ==  0.0: return COLORS["tc"]
    elif pct >= -2.0: return COLORS["dn"]
    elif pct >= -4.0: return COLORS["dv"]
    elif pct >= -6.5: return COLORS["dd"]
    else:             return COLORS["san"]

def get_fg(bg: tuple) -> tuple:
    lum = 0.299*bg[0] + 0.587*bg[1] + 0.114*bg[2]
    return (30, 30, 30) if lum > 160 else (15, 15, 15)

# ── LẤY DỮ LIỆU ────────────────────────────────────────────────
def fetch_all_data() -> dict:
    register_user(VNSTOCK_API_KEY)
    engine = Trading(source="KBS")
    need = list({s for col in OTHER_COLUMNS for g in col["groups"] for s in g["symbols"]}
                | set(TRADING_STOCKS_POOL))
    print(f"[{_ts()}] Dang tai {len(need)} ma...")
    result = {}
    try:
        df = engine.price_board(need)
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                sym   = str(row.get("symbol","")).strip()
                close = float(row.get("close_price", 0) or 0)
                ref   = float(row.get("reference_price", 0) or 0)
                pct   = round((close - ref)/ref*100, 2) if ref > 0 else 0.0
                result[sym] = {"price": close, "pct": pct}
            print(f"[{_ts()}] OK {len(result)} ma.")
        else:
            print(f"[{_ts()}] API tra ve rong.")
    except Exception as e:
        print(f"[{_ts()}] Loi API: {e}")
    return result

# ── FONT (ASCII tên ngành để tránh lỗi font tiếng Việt) ─────────
def _load_fonts():
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
    except Exception as e:
        print(f"Font fallback: {e}")
        d = ImageFont.load_default()
        return d, d, d, d, d

# ── BỐ CỤC ──────────────────────────────────────────────────────
CELL_W   = 162   
CELL_H   = 26    
COL_GAP  = 4
COL_W    = CELL_W + COL_GAP
MARGIN   = 5
TOP_BAR  = 32
RADIUS   = 5     

BG           = (252, 252, 252)
HDR_FILL     = (220, 228, 250)
HDR_OUTLINE  = (160, 180, 230)
HDR_FG       = ( 25,  55, 150)
SECTOR_FILL  = (235, 240, 255)
SECTOR_FG_P  = ( 30, 140,  40)
SECTOR_FG_N  = (190,  30,  30)
SECTOR_FG_0  = (120, 120,  30)

# ── VẼ Ô BẰNG ARC+RECT ──────────────────────────────────────────
def _rounded_rect(draw, x0, y0, x1, y1, r, fill, outline=None, lw=1):
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

# ── VẼ 1 Ô CỔ PHIẾU CĂN GIỮA NỘI DUNG ───────────────────────────
def _draw_stock_cell(draw, x, y, sym, price, pct, f_sym, f_data):
    bg  = get_cell_color(pct)
    fg  = get_fg(bg)
    x1  = x + CELL_W - 1
    y1  = y + CELL_H - 2

    _rounded_rect(draw, x, y, x1, y1, RADIUS, fill=bg,
                  outline=(200, 205, 215), lw=1)

    # Chia ô thành 3 phần: Mã (35%) | Giá (30%) | %Giá (35%)
    w1 = int(CELL_W * 0.35)
    w2 = int(CELL_W * 0.30)
    w3 = CELL_W - w1 - w2
    
    # Căn giữa theo trục Y cố định để các dòng luôn thẳng hàng
    ty = y + (CELL_H - 2)//2 - 5 

    def _draw_center(txt, fnt, bx, bw, fill_c):
        bb = draw.textbbox((0,0), txt, font=fnt)
        tw = bb[2] - bb[0]
        draw.text((bx + (bw - tw)//2, ty), txt, font=fnt, fill=fill_c)

    # Căn giữa từng khối
    _draw_center(sym, f_sym, x, w1, fg)
    
    p_str = f"{price:,.1f}" if price < 100 else f"{price:,.0f}"
    _draw_center(p_str, f_data, x + w1, w2, fg)
    
    pct_str = f"{pct:+.1f}%"
    _draw_center(pct_str, f_data, x + w1 + w2, w3, fg)

# ── VẼ Ô HEADER NGÀNH (liền mạch, căn giữa từng khối) ───────────
def _draw_group_header(draw, x, y, name, avg_pct, f_hdr, f_sector):
    x1 = x + CELL_W - 1
    y1 = y + CELL_H - 2

    _rounded_rect(draw, x, y, x1, y1, RADIUS, fill=HDR_FILL,
                  outline=HDR_OUTLINE, lw=1)

    # Chia tỉ lệ header: Tên (65%) | % Ngành (35%)
    w1 = int(CELL_W * 0.65)
    w2 = CELL_W - w1
    ty = y + (CELL_H - 2)//2 - 5

    def _draw_center(txt, fnt, bx, bw, fill_c):
        bb = draw.textbbox((0,0), txt, font=fnt)
        tw = bb[2] - bb[0]
        draw.text((bx + (bw - tw)//2, ty), txt, font=fnt, fill=fill_c)

    _draw_center(name, f_hdr, x, w1, HDR_FG)

    pct_str = f"{avg_pct:+.2f}%"
    fg_s = SECTOR_FG_P if avg_pct > 0 else (SECTOR_FG_N if avg_pct < 0 else SECTOR_FG_0)
    _draw_center(pct_str, f_sector, x + w1, w2, fg_s)

# ── TÍNH % TRUNG BÌNH NGÀNH ─────────────────────────────────────
def _avg_pct(syms, data):
    vals = [data[s]["pct"] for s in syms if s in data]
    return round(sum(vals)/len(vals), 2) if vals else 0.0

def _col_sym_count(col_def):
    return sum(len(g["symbols"]) for g in col_def["groups"])

def _col_height(groups):
    h = TOP_BAR + MARGIN
    for g in groups:
        # Xóa khoảng cách phụ (+2), đảm bảo chiều cao các ô bám sát nhau hoàn toàn
        h += (1 + len(g["symbols"])) * CELL_H
    return h + MARGIN

# ── VẼ HEATMAP CHÍNH ────────────────────────────────────────────
def draw_heatmap(data: dict, timestamp: str) -> str:
    f_title, f_hdr, f_sym, f_data, f_sector = _load_fonts()

    max_rows = max(_col_sym_count(c) for c in OTHER_COLUMNS)
    ts_display = sorted(
        [s for s in TRADING_STOCKS_POOL if s in data],
        key=lambda s: data[s]["pct"], reverse=True
    )[:max_rows]
    print(f"[{_ts()}] Trading Stocks: {len(ts_display)} ma (cat tu {max_rows} dong cao nhat)")

    def srt(syms):
        return sorted(syms, key=lambda s: data.get(s,{}).get("pct",0), reverse=True)

    col0 = {"col": 0, "groups": [{"name": "TRADING STOCKS", "symbols": ts_display}]}
    all_cols = [col0] + OTHER_COLUMNS

    all_sorted = []
    for cd in all_cols:
        all_sorted.append([{"name": g["name"], "symbols": srt(g["symbols"])} for g in cd["groups"]])

    IMG_W = len(all_cols) * COL_W + MARGIN * 2
    IMG_H = max(_col_height(gs) for gs in all_sorted)

    img  = Image.new("RGB", (IMG_W, IMG_H), BG)
    draw = ImageDraw.Draw(img)

    _rounded_rect(draw, 0, 0, IMG_W-1, TOP_BAR, 0,
                  fill=(238, 242, 255), outline=(180, 195, 235), lw=1)
    draw.text((MARGIN + 5, 9),
              f"MARKET MAP   {timestamp}",
              font=f_title, fill=(15, 35, 115))
              
    # Thay đổi thanh tiêu đề: Loại bỏ cột Vol
    legend = "  Ma | Gia | %Gia"
    bb = draw.textbbox((0,0), legend, font=f_data)
    draw.text((IMG_W - (bb[2]-bb[0]) - 8, 11), legend, font=f_data, fill=(100, 110, 140))

    for idx, cd in enumerate(all_cols):
        cx = cd["col"] * COL_W + MARGIN
        y  = TOP_BAR + MARGIN
        groups = all_sorted[idx]

        for g in groups:
            syms    = g["symbols"]
            avg     = _avg_pct(syms, data)

            _draw_group_header(draw, cx, y, g["name"], avg, f_hdr, f_sector)
            y += CELL_H  

            for sym in syms:
                info    = data.get(sym, {})
                price   = info.get("price",   0.0)
                pct     = info.get("pct",     0.0)
                _draw_stock_cell(draw, cx, y, sym, price, pct, f_sym, f_data)
                y += CELL_H
            
            # Đã xóa đoạn cách "y += 3" ở đây để các khối ngành liền sát hoàn toàn.

    draw.rectangle([0, 0, IMG_W-1, IMG_H-1], outline=(200, 210, 230), width=1)

    path = "/tmp/heatmap.png"
    img.save(path, "PNG", optimize=True)
    print(f"[{_ts()}] Anh {IMG_W}x{IMG_H}px -> {path}")
    return path

# ── TELEGRAM HELPERS ────────────────────────────────────────────
def tg_photo(chat_id, path, caption=""):
    with open(path, "rb") as f:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
            data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
            files={"photo": f}, timeout=60)
    return r.status_code == 200

def tg_text(chat_id, text):
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                  data={"chat_id": chat_id, "text": text}, timeout=10)

def tg_action(chat_id):
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendChatAction",
                  data={"chat_id": chat_id, "action": "upload_photo"}, timeout=5)

# ── XỬ LÝ LỆNH /h ──────────────────────────────────────────────
def handle_heatmap(chat_id):
    ts = datetime.now(TZ_VN).strftime("%H:%M  %d/%m/%Y")
    tg_action(chat_id)
    tg_text(chat_id, "Dang tai du lieu, vui long cho 15-30 giay...")
    data = fetch_all_data()
    if not data:
        tg_text(chat_id, "Khong lay duoc du lieu. Thu lai sau.")
        return
    path = draw_heatmap(data, ts)
    ok = tg_photo(chat_id, path,
                  f"<b>MARKET MAP</b>  {ts}")
    print(f"[{_ts()}] {'Gui OK' if ok else 'Gui that bai'} -> {chat_id}")

print("OK - Load xong. Chay CELL 3 (Telegram) hoac CELL 4 (xem anh).")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CELL 3 — Bot Telegram (bo """ de chay)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

print("Bot dang chay... Nhan /h tren Telegram. Nhan Stop de dung.")
last_id = 0
while True:
    try:
        url = (f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
               f"/getUpdates?offset={last_id+1}&timeout=30")
        updates = requests.get(url, timeout=35).json().get("result", [])
        for upd in updates:
            last_id = upd["update_id"]
            msg  = upd.get("message", {})
            text = msg.get("text", "").strip().lower()
            cid  = msg.get("chat", {}).get("id")
            if cid and text in ["/h", "/heatmap"]:
                print(f"[{_ts()}] Lenh '{text}' tu chat_id={cid}")
                handle_heatmap(cid)
    except KeyboardInterrupt:
        print("Dung.")
        break
    except Exception as e:
        if "timed out" not in str(e).lower():
            print(f"[{_ts()}] {e}")
        time.sleep(3)

 
 # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CELL 4 (TÙY CHỌN) — Test ngay không cần Telegram
# Chạy cell này để xem ảnh heatmap ngay trong Colab
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

from IPython.display import display, Image as IPImage
 
ts   = datetime.now(TZ_VN).strftime("%H:%M  %d/%m/%Y")
data = fetch_all_data()
path = draw_heatmap(data, ts)
display(IPImage(path))

 
