"""
=============================================================================
SCANNER TÍN HIỆU MUA CỔ PHIẾU — PHIÊN BẢN TỐI ƯU (CACHE + PRICE_BOARD)
Pocket Pivot / Breakout / Pre-Break
Tích hợp: vnstock + Telegram + Chart mplfinance + Chống spam + Nghỉ ngoài giờ

KIẾN TRÚC 2 BƯỚC:
  Bước 1: Load lịch sử 1 lần → cache vào dict  (trước giờ GD hoặc lúc khởi động)
  Bước 2: Mỗi chu kỳ scan chỉ gọi price_board 1 request → ghép vào cache → detect
=============================================================================
"""

# =============================================================================
# BƯỚC 0: CÀI ĐẶT THƯ VIỆN (chạy 1 lần nếu chưa có)
# =============================================================================
#!pip install -U vnstock pandas requests mplfinance pytz

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
from datetime import datetime, date
import pytz
import json
import threading

# =============================================================================
# BƯỚC 2: CẤU HÌNH
# =============================================================================
VNSTOCK_API         = os.environ.get('VNSTOCK_API')
TELEGRAM_BOT_TOKEN  = os.environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID    = os.environ.get('TELEGRAM_CHAT_ID')
MY_PERSONAL_CHAT_ID = os.environ.get('MY_PERSONAL_CHAT_ID')

DATA_SOURCE = 'VCI'
SCAN_INTERVAL_SEC  = 120
TZ_VN              = pytz.timezone('Asia/Ho_Chi_Minh')

# Nguồn price_board — KBS hỗ trợ tốt nhất cho realtime
PRICE_BOARD_SOURCE = 'KBS'

ALLOWED_CHATS = {str(TELEGRAM_CHAT_ID), str(MY_PERSONAL_CHAT_ID), '1207484510'}

register_user(VNSTOCK_API)

# =============================================================================
# BƯỚC 3: HÀM BỔ TRỢ (TELEGRAM & WEEKLY DATA)
# =============================================================================
def build_weekly_df(df_daily):
    """Resample OHLCV daily → weekly (tuần kết thúc thứ Sáu), tính lại chỉ báo."""
    df_w = df_daily[['open','high','low','close','volume']].resample('W-FRI').agg({
        'open':   'first',
        'high':   'max',
        'low':    'min',
        'close':  'last',
        'volume': 'sum',
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
                'chat_id':    TELEGRAM_CHAT_ID,
                'text':       notify_text,
                'parse_mode': 'HTML'
            })

        if image_paths and len(image_paths) == 1:
            with open(image_paths[0], 'rb') as f:
                requests.post(url_photo, data={
                    'chat_id':              TELEGRAM_CHAT_ID,
                    'caption':              msg or '',
                    'parse_mode':           'HTML',
                    'disable_notification': True,
                }, files={'photo': f})
            print(f"  ✅ Đã gửi chart: {image_paths[0]}")

        elif image_paths and len(image_paths) >= 2:
            files = {}
            media = []
            for i, path in enumerate(image_paths):
                key        = f"photo{i}"
                files[key] = open(path, 'rb')
                item       = {"type": "photo", "media": f"attach://{key}"}
                if i == 0 and msg:
                    item["caption"]    = msg
                    item["parse_mode"] = "HTML"
                media.append(item)
            try:
                requests.post(url_album, data={
                    'chat_id':              TELEGRAM_CHAT_ID,
                    'media':                json.dumps(media),
                    'disable_notification': True,
                }, files=files)
                print(f"  ✅ Đã gửi album: {image_paths[0]}")
            finally:
                for f in files.values(): f.close()

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
def ref(series, n):
    return series.shift(n)

def hhv(series, n):
    return series.rolling(n).max()

def llv(series, n):
    return series.rolling(n).min()

def compute_indicators(df):
    df = df.copy()
    for n in [2, 3, 5, 10, 15, 20, 30, 50, 200]:
        df[f'MA{n}']  = df['close'].rolling(n).mean()
    for n in [10, 20, 30, 50]:
        df[f'EMA{n}'] = df['close'].ewm(span=n, adjust=False).mean()
    for n in [2, 3, 5, 10, 15, 20, 30, 50]:
        df[f'VMA{n}'] = df['volume'].rolling(n).mean()

    delta    = df['close'].diff()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    rs       = avg_gain / avg_loss.replace(0, np.nan)
    df['RSI14'] = 100 - (100 / (1 + rs))

    exp12             = df['close'].ewm(span=12, adjust=False).mean()
    exp26             = df['close'].ewm(span=26, adjust=False).mean()
    df['MACD']        = exp12 - exp26
    df['MACD_Signal'] = df['MACD'].ewm(span=9, adjust=False).mean()
    df['MACD_Hist_origin'] = df['MACD'] - df['MACD_Signal']
    df['MACD_Hist'] = df['MACD_Hist_origin'] * 3
    df['A']           = df['close'].pct_change()
    return df

# =============================================================================
# BƯỚC 5B: ═══ CACHE LỊCH SỬ ══════════════════════════════════════════════
#
#   history_cache[symbol] = DataFrame (cột OHLCV, index datetime, KHÔNG tính
#                           chỉ báo — sẽ tính khi ghép nến realtime vào)
#   cache_date            = ngày load cache (để biết khi nào cần reload)
# =============================================================================
history_cache: dict[str, pd.DataFrame] = {}
cache_date: date | None = None
cache_lock = threading.Lock()          # bảo vệ khi listener và scanner cùng đọc

# ─── CỘT CẦN LẤY TỪ price_board ────────────────────────────────────────────
# Tên cột thực tế của Trading.price_board() — kiểm tra lại nếu vnstock update
PB_SYMBOL   = 'ticker'        # hoặc 'symbol' tuỳ version
PB_OPEN     = 'open'
PB_HIGH     = 'high'
PB_LOW      = 'low'
PB_CLOSE    = 'match_price'   # giá khớp gần nhất / giá hiện tại
PB_VOLUME   = 'total_volume'  # khối lượng tích luỹ trong ngày

def load_history_for_symbol(symbol: str, current_date: date) -> pd.DataFrame | None:
    """Lấy lịch sử 1 mã, trả về DataFrame OHLCV (chưa tính chỉ báo)."""
    for attempt in range(3):
        try:
            quote  = Quote(symbol=symbol, source=DATA_SOURCE)
            df_raw = quote.history(length='1000', interval='1D')
            if df_raw is None or len(df_raw) < 60:
                return None
            df_raw['time'] = pd.to_datetime(df_raw['time'])
            df_raw.set_index('time', inplace=True)
            df_raw.columns = [c.lower() for c in df_raw.columns]
            # Giữ lại đến cuối ngày hôm qua (không lấy nến hôm nay vào cache)
            df_raw = df_raw[df_raw.index.date < current_date]
            return df_raw[['open','high','low','close','volume']].copy()
        except Exception as e:
            if attempt < 2:
                time.sleep(2)
            else:
                print(f"    ❌ Load history {symbol}: {e}")
    return None


def build_history_cache(symbols: list[str], current_date: date):
    """
    Bước 1: Tải lịch sử toàn bộ danh mục và lưu vào history_cache.
    Gọi 1 lần trước giờ GD hoặc khi khởi động.
    """
    global cache_date
    ts = datetime.now(TZ_VN).strftime('%H:%M:%S')
    print(f"\n📦 [{ts}] Bắt đầu load cache lịch sử cho {len(symbols)} mã...")
    loaded = 0
    for i, symbol in enumerate(symbols, 1):
        df = load_history_for_symbol(symbol, current_date)
        if df is not None and len(df) >= 60:
            with cache_lock:
                history_cache[symbol] = df
            loaded += 1
        if i % 20 == 0:
            ts2 = datetime.now(TZ_VN).strftime('%H:%M:%S')
            print(f"  [{ts2}] Đã load {i}/{len(symbols)} mã...")
        time.sleep(0.3)   # lịch sự với server
    with cache_lock:
        cache_date = current_date
    ts = datetime.now(TZ_VN).strftime('%H:%M:%S')
    print(f"✅ [{ts}] Cache hoàn tất: {loaded}/{len(symbols)} mã có dữ liệu.")


def get_price_board(symbols: list[str]) -> pd.DataFrame | None:
    """
    Bước 2a: Lấy snapshot realtime cho toàn bộ danh mục qua price_board.
    Trả về DataFrame với index là ticker, các cột: open/high/low/close/volume.
    """
    try:
        trading = Trading(source=PRICE_BOARD_SOURCE)
        df_pb   = trading.price_board(symbols)
        if df_pb is None or df_pb.empty:
            return None
        df_pb.columns = [c.lower() for c in df_pb.columns]

        # Chuẩn hoá tên cột về open/high/low/close/volume
        rename_map = {}
        if PB_CLOSE   in df_pb.columns: rename_map[PB_CLOSE]   = 'close'
        if PB_VOLUME  in df_pb.columns: rename_map[PB_VOLUME]  = 'volume'
        if PB_OPEN    in df_pb.columns: rename_map[PB_OPEN]    = 'open'
        if PB_HIGH    in df_pb.columns: rename_map[PB_HIGH]    = 'high'
        if PB_LOW     in df_pb.columns: rename_map[PB_LOW]     = 'low'
        if PB_SYMBOL  in df_pb.columns: rename_map[PB_SYMBOL]  = 'symbol'
        df_pb.rename(columns=rename_map, inplace=True)

        # Đặt symbol làm index để tra nhanh
        if 'symbol' in df_pb.columns:
            df_pb.set_index('symbol', inplace=True)
        return df_pb
    except Exception as e:
        print(f"  ❌ price_board lỗi: {e}")
        return None


def merge_realtime_row(df_hist: pd.DataFrame, pb_row: pd.Series,
                       current_date: date) -> pd.DataFrame:
    """
    Ghép 1 nến realtime (từ price_board) vào cuối DataFrame lịch sử.
    Nếu ngày hôm nay đã có nến (do cache cũ) thì thay thế, không thêm trùng.
    """
    today_ts = pd.Timestamp(current_date)

    new_row = pd.DataFrame([{
        'open':   float(pb_row.get('open',   pb_row.get('close', np.nan))),
        'high':   float(pb_row.get('high',   pb_row.get('close', np.nan))),
        'low':    float(pb_row.get('low',    pb_row.get('close', np.nan))),
        'close':  float(pb_row.get('close',  np.nan)),
        'volume': float(pb_row.get('volume', np.nan)),
    }], index=[today_ts])

    # Xoá nến hôm nay nếu đã tồn tại (tránh trùng)
    df_hist = df_hist[df_hist.index.date < current_date]
    return pd.concat([df_hist, new_row])


# =============================================================================
# BƯỚC 6: CÁC HÀM ĐIỀU KIỆN TÍN HIỆU (GIỮ NGUYÊN)
# =============================================================================
def calc_pocket_pivot_vol(df):
    V = df['volume']
    C = df['close']
    def down_vol(lag_v, lag_c, lag_c_prev):
        return ref(V, lag_v).where(ref(C, lag_c) <= ref(C, lag_c_prev), 0)
    return (
        (V > down_vol(1,1,2)) & (V > down_vol(2,2,3)) & (V > down_vol(3,3,4)) &
        (V > down_vol(4,4,5)) & (V > down_vol(5,5,6)) & (V > down_vol(6,6,7)) &
        ((V > ref(V,2)) | (V > ref(V,1))) &
        (V > 0.8 * ref(df['VMA3'], 1))
    )

def calc_break_vol(df):
    V = df['volume']
    cond_a = (
        ((V > 1.10*df['VMA30']) | (V > 1.10*df['VMA50']) | (V > 1.15*df['VMA20'])) &
        ((V > ref(V,2)) | (V > ref(V,1))) &
        (V > 0.9*df['VMA5']) & (V > 0.9*ref(df['VMA3'],1))
    )
    cond_b = (
        ((V > 1.5*df['VMA30']) | (V > 1.5*df['VMA50']) | (V > 1.5*df['VMA20'])) &
        (V > 0.8*ref(df['VMA2'],1))
    )
    return cond_a | cond_b

def calc_wedging(df):
    H, L, C, O, A = df['high'], df['low'], df['close'], df['open'], df['A']
    range5       = ref(hhv(H,5),1) - ref(llv(L,5),1)
    llv5_1       = ref(llv(L,5),1)
    range_close5 = ref(hhv(C,5),1) - ref(llv(C,5),1)
    cond_narrow  = range5 / llv5_1 < 0.05
    cond_semi    = (range5 / llv5_1 < 0.06) & (range_close5 / llv5_1 < 0.02)
    ma_a3_1 = ref(A.rolling(3).mean(), 1)
    ma_a2_1 = ref(A.rolling(2).mean(), 1)
    two_green = (
        (ma_a2_1 > 0.015) &
        ((O - ref(C,1)) >= 0) & ((ref(O,1) - ref(C,2)) >= 0) &
        ((ref(O,2) - ref(C,3)) >= 0) & (ref(C,3) > ref(C,4)) &
        (ref(C,1) > ref(O,1)) & (ref(C,2) > ref(O,2)) &
        ((ref(C,1)-ref(C,2))/ref(C,2) > 0.015) &
        ((ref(C,2)-ref(C,3))/ref(C,3) > 0.015) &
        (ref(L,1) >= ref(L,2)) & (ref(L,2) >= ref(L,3))
    )
    is_wedging_strong = (ma_a3_1 > 0.037) | (ma_a2_1 > 0.04) | two_green
    return cond_narrow | cond_semi | (~is_wedging_strong)

def calc_pocket_pivot_price(df):
    C, O, H, L, V = df['close'], df['open'], df['high'], df['low'], df['volume']
    wedging = calc_wedging(df)
    c1  = C >= 1.015 * ref(C,1)
    c2  = ((C > df['MA5']) & calc_break_vol(df) & calc_pocket_pivot_vol(df)) | (C > df['MA10'])
    c3  = (C >= df['EMA50']) | (C >= df['EMA30']) | (C >= df['EMA20'])
    c4  = ((df['EMA50']>=ref(df['EMA50'],1)) | (df['EMA30']>=ref(df['EMA30'],1)) |
            (df['EMA20']>=ref(df['EMA20'],1)) | (df['EMA10']>=ref(df['EMA10'],1)))
    c5  = C > (ref(L,1) + ref(H,1)) / 2
    c6  = (((C>(H+L)/2) & (C>=O)) |
           (((O-ref(C,1))/ref(C,1)>0.02) & ((C-ref(C,1))/ref(C,1)>0.02)))
    c7  = ((H-C)/C < 0.02) | (((H-C)/C >= 0.02) & ((C-O)/O >= 1.1*(H-C)/C))
    c8  = O <= 1.08 * df['MA10']
    c9  = (((O <= 0.998*ref(hhv(H,6),2)) & ((O-df['MA10'])/df['MA10'] < 0.025)) |
            ((O <= 0.99 *ref(hhv(H,6),2)) & ((O-df['MA10'])/df['MA10'] < 0.032)) |
            ((O <= 0.95 *ref(hhv(H,6),2)) & ((O-df['MA10'])/df['MA10'] < 0.05))  |
            ((O-df['MA10'])/df['MA10'] < 0.012))
    c10 = (O - df['MA10']) / df['MA10'] < 0.05
    c11 = (O - ref(C,2)) / ref(C,2) < 0.1
    c12 = (ref(C,1)-ref(C,2))/ref(C,2) > -0.05
    c13 = (ref(C,1)-ref(df['MA10'],1))/ref(df['MA10'],1) < 0.08
    c14 = (ref(L,1)-ref(df['MA10'],1))/ref(df['MA10'],1) < 0.05
    c15 = ~(((ref(C,1)-ref(C,2))/ref(C,2) < -0.025) &
            ((ref(V,1)-ref(df['VMA50'],1))/ref(df['VMA50'],1) > 0.5) &
            ((ref(V,1)-ref(df['VMA30'],1))/ref(df['VMA30'],1) > 0.5) &
            (V < 0.8*ref(V,1)))
    box1 = (ref(hhv(H,3),1)-ref(llv(L,3),1))/ref(llv(L,5),1) < 0.18
    box2 = (ref(hhv(H,2),1)-ref(llv(L,2),1))/ref(llv(L,2),1) < 0.12
    box3 = (ref(hhv(C,2),1)-ref(llv(C,2),1))/ref(llv(L,2),1) < 0.08
    return (c1&c2&c3&c4&c5&c6&c7&c8&c9&c10&c11&c12&c13&c14&c15&box1&box2&box3&wedging)

def calc_break_price(df):
    C, O, H, L, V = df['close'], df['open'], df['high'], df['low'], df['volume']
    wedging = calc_wedging(df)
    c1  = C >= 1.015 * ref(C,1)
    c2  = (C>df['MA5']) & (C>df['MA10']) & ((C>=df['EMA50'])|(C>=df['EMA30'])|(C>=df['EMA20']))
    c3  = ((df['EMA50']>=ref(df['EMA50'],1)) | (df['EMA30']>=ref(df['EMA30'],1)) |
            (df['EMA20']>=ref(df['EMA20'],1)) | (df['EMA10']>=ref(df['EMA10'],1)))
    c4  = C > (ref(L,1)+ref(H,1))/2
    c5  = (ref(L,1)-ref(df['MA10'],1))/ref(df['MA10'],1) < 0.0825
    c6  = (ref(C,1)-ref(df['MA10'],1))/ref(df['MA10'],1) < 0.0825
    c7  = (O-df['MA10'])/df['MA10'] < 0.0825
    c8  = (O-ref(C,2))/ref(C,2) < 0.1
    c9  = (((C>(H+L)/2) & (C>=O)) |
           (((O-ref(C,1))/ref(C,1)>0.02) & ((C-ref(C,1))/ref(C,1)>0.02)))
    c10 = ((H-C)/C < 0.02) | (((H-C)/C >= 0.02) & ((C-O)/O >= 1.1*(H-C)/C))
    c11 = ~(((ref(C,1)-ref(C,2))/ref(C,2) < -0.025) &
            ((ref(V,1)-ref(df['VMA50'],1))/ref(df['VMA50'],1) > 0.5) &
            ((ref(V,1)-ref(df['VMA30'],1))/ref(df['VMA30'],1) > 0.5) &
            (V < 0.8*ref(V,1)))
    box1 = (ref(hhv(H,3),1)-ref(llv(L,3),1))/ref(llv(L,5),1) < 0.18
    box2 = (ref(hhv(H,2),1)-ref(llv(L,2),1))/ref(llv(L,2),1) < 0.12
    box3 = (ref(hhv(C,2),1)-ref(llv(C,2),1))/ref(llv(L,2),1) < 0.08
    return c1&c2&c3&c4&c5&c6&c7&c8&c9&c10&c11&box1&box2&box3&wedging

def calc_prebreak_vol(df, now_time):
    V   = df['volume']
    V1  = ref(V,1)
    V2  = ref(V,2)
    pct = (df['close'] - ref(df['close'],1)) / ref(df['close'],1)
    def make_cond(vma_n, v_lo_mult, big_vma, big_v2):
        normal = (
            (pct < 0.1) &
            ((V > vma_n*df['VMA30']) | (V > vma_n*df['VMA50']) | (V > vma_n*df['VMA20'])) &
            ((V > v_lo_mult*V2) | (V > v_lo_mult*V1)) &
            (V > vma_n*0.8*df['VMA5']) & (V > vma_n*0.8*ref(df['VMA3'],1))
        )
        big = (
            ((V > big_vma*df['VMA30']) | (V > big_vma*df['VMA50']) | (V > big_vma*df['VMA20'])) &
            (V > big_v2*ref(df['VMA2'],1))
        )
        return normal | big
    if   now_time < 93000:  return make_cond(0.30, 0.25, 0.40, 0.20)
    elif now_time < 100000: return make_cond(0.40, 0.35, 0.60, 0.30)
    elif now_time < 103000: return make_cond(0.50, 0.45, 0.80, 0.40)
    elif now_time < 113000: return make_cond(0.80, 0.70, 1.10, 0.60)
    elif now_time < 133000: return make_cond(0.95, 0.80, 1.25, 0.70)
    else:                   return make_cond(1.05, 0.90, 1.40, 0.75)

def calc_liquidity(df):
    C, V = df['close'], df['volume']
    return (
        (C >= 5) & (C*V > 2_000_000) &
        (df['MA10']*df['VMA10'] > 2_000_000) &
        (df['MA15']*df['VMA15'] > 2_000_000) &
        (df['RSI14'] >= 29) &
        (df['VMA10'] >= 50_000) & (df['VMA20'] >= 50_000) &
        (df['VMA30'] >= 50_000) & (df['VMA50'] >= 50_000)
    )

def detect_signal(df, now_time):
    df = compute_indicators(df)
    if len(df) < 60:
        return None
    liq         = calc_liquidity(df)
    break_price = calc_break_price(df)
    break_vol   = calc_break_vol(df)
    pprice      = calc_pocket_pivot_price(df)
    pvol        = calc_pocket_pivot_vol(df)
    ma10_ok     = df['MA10'] >= 0.8 * ref(df['MA10'], 1)
    pre_vol     = calc_prebreak_vol(df, now_time)

    is_breakout = (break_price & break_vol  & liq).iloc[-1]
    is_pocket   = (pprice & (pvol | break_vol) & liq & ma10_ok).iloc[-1]
    is_prebreak = (
        ((break_price | pprice) & pre_vol & liq).iloc[-1] and
        not is_breakout and not is_pocket and
        (91700 < now_time < 150000)
    )
    if is_breakout: return 'BREAKOUT'
    if is_pocket:   return 'POCKET PIVOT'
    if is_prebreak: return 'PRE-BREAK'
    return None

# =============================================================================
# BƯỚC 7: VẼ BIỂU ĐỒ (GIỮ NGUYÊN)
# =============================================================================
def draw_chart(df_plot, symbol, signal_type, today, timeframe='Daily', add_arrow=True):
    is_daily = (timeframe == 'Daily')
    date_str = datetime.now(TZ_VN).strftime('%d/%m/%Y')

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
        mpf.make_addplot(df_plot['EMA10'],     color='red',    width=0.6),
        mpf.make_addplot(df_plot['EMA20'],     color='green',  width=0.6),
        mpf.make_addplot(df_plot['EMA50'],     color='purple', width=0.6),
        mpf.make_addplot(df_plot['volume'],    type='bar', panel=1, color=colors_vol,  alpha=1.0),
        mpf.make_addplot(df_plot['MACD_Hist'], type='bar', panel=2, color=macd_colors, secondary_y=False),
        mpf.make_addplot(df_plot['MACD'],      panel=2, color='blue',   width=0.6, secondary_y=False),
        mpf.make_addplot(df_plot['MACD_Signal'],panel=2,color='orange', width=0.6, secondary_y=False),
    ]

    if is_daily:
        apds.append(mpf.make_addplot(df_plot['MA200'], color='brown', width=0.6))

    mc           = mpf.make_marketcolors(up='#26A69A', down='#EF5350', edge='inherit', wick='inherit', alpha=1.0)
    custom_style = mpf.make_mpf_style(base_mpf_style='charles', marketcolors=mc, gridstyle='', facecolor='white')
    img_name     = f"{symbol}_{timeframe.lower()}.png"

    fig, axlist = mpf.plot(
        df_plot, type='candle', volume=False, addplot=apds,
        style=custom_style, savefig=dict(fname=img_name, dpi=150),
        figratio=(12,9), returnfig=True, show_nontrading=False, tight_layout=True
    )
    ax_price = axlist[0]
    ax_price.yaxis.set_label_position("right"); ax_price.yaxis.tick_right()
    ax_price.set_ylabel(""); ax_price.tick_params(axis='y', labelsize=8)
    y_min, y_max = ax_price.get_ylim()
    ax_price.set_ylim(y_min, y_max + (y_max - y_min) * 0.15)

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
        m_vals = pd.concat([df_plot['MACD'], df_plot['MACD_Signal'], df_plot['MACD_Hist']]).dropna()
        abs_m  = max(abs(m_vals.min()), abs(m_vals.max())) if len(m_vals) > 0 else 1
        ax_macd.set_ylim(-abs_m*0.8, abs_m*1.2)
        for spine in ['top','right','left','bottom']: ax_macd.spines[spine].set_visible(False)

    for i, ax in enumerate(axlist):
        if i not in [0,4]: ax.set_axis_off()
        else: ax.xaxis.set_visible(False); ax.spines['top'].set_visible(False); ax.spines['left'].set_visible(False)

    xlim = ax_price.get_xlim()
    ax_price.set_xlim(xlim[0], xlim[1] + 20)
    fig.savefig(img_name, bbox_inches='tight', pad_inches=0.15, dpi=150)
    plt.close(fig)
    return img_name

# =============================================================================
# BƯỚC 8: HÀM QUÉT 1 CHU KỲ — ĐÃ TỐI ƯU VỚI PRICE_BOARD
# =============================================================================
SIGNAL_RANK  = {'PRE-BREAK': 1, 'POCKET PIVOT': 2, 'BREAKOUT': 3}
SIGNAL_EMOJI = {'BREAKOUT': '🟢', 'POCKET PIVOT': '🟡', 'PRE-BREAK': '🟣'}

def run_scan_cycle(symbols: list[str], now_time: int, alerted_today: dict):
    """
    Bước 2 của kiến trúc 2 bước:
      1. Gọi price_board 1 lần cho toàn bộ danh mục  (1 request duy nhất)
      2. Với mỗi mã: ghép nến realtime vào cache lịch sử → detect signal → gửi alert
    """
    new_signals  = []
    current_date = datetime.now(TZ_VN).date()
    date_str     = datetime.now(TZ_VN).strftime('%d/%m/%Y')
    ts           = datetime.now(TZ_VN).strftime('%H:%M:%S')

    # ── Lấy snapshot realtime toàn bộ danh mục — 1 request duy nhất ──────────
    print(f"  [{ts}] Gọi price_board cho {len(symbols)} mã...")
    df_pb = get_price_board(symbols)
    if df_pb is None:
        print(f"  ❌ [{ts}] Không lấy được price_board, bỏ qua chu kỳ này.")
        return new_signals

    ts = datetime.now(TZ_VN).strftime('%H:%M:%S')
    print(f"  [{ts}] price_board OK — bắt đầu xử lý từng mã...")

    for symbol in symbols:
        try:
            # Lấy cache lịch sử
            with cache_lock:
                df_hist = history_cache.get(symbol)

            if df_hist is None or len(df_hist) < 60:
                continue

            # Lấy dòng realtime của mã này từ price_board
            if symbol not in df_pb.index:
                continue
            pb_row = df_pb.loc[symbol]

            # Kiểm tra giá hợp lệ
            close_val = pb_row.get('close', np.nan)
            if pd.isna(close_val) or float(close_val) <= 0:
                continue

            # Ghép nến realtime vào lịch sử
            df_merged = merge_realtime_row(df_hist, pb_row, current_date)

            # Giới hạn 1000 nến để không nặng bộ nhớ
            df_merged = df_merged.tail(1000)

            # Detect signal
            signal_type = detect_signal(df_merged, now_time)
            if not signal_type:
                continue

            prev_rank    = SIGNAL_RANK.get(alerted_today.get(symbol), 0)
            current_rank = SIGNAL_RANK.get(signal_type, 0)
            if prev_rank >= current_rank:
                continue

            alerted_today[symbol] = signal_type
            new_signals.append(symbol)

            # Tính chỉ báo đầy đủ cho chart (dùng df đã merged + indicators)
            df_calc     = compute_indicators(df_merged)
            today       = df_calc.iloc[-1]
            pct         = (today['close'] - df_calc['close'].iloc[-2]) / df_calc['close'].iloc[-2] * 100
            change      = today['close'] - df_calc['close'].iloc[-2]
            emoji       = SIGNAL_EMOJI.get(signal_type, '📌')
            vol_vs_prev  = (today['volume'] - df_calc['volume'].iloc[-2]) / df_calc['volume'].iloc[-2] * 100
            vol_vs_vma50 = (today['volume'] - today['VMA50']) / today['VMA50'] * 100 if today['VMA50'] > 0 else 0

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

            df_plot_d = df_calc.tail(250).copy()
            img_daily = draw_chart(df_plot_d, symbol, signal_type, today, timeframe='Daily', add_arrow=True)

            df_weekly  = build_weekly_df(df_merged)
            df_plot_w  = df_weekly.tail(150).copy()
            today_w    = df_plot_w.iloc[-1]
            img_weekly = draw_chart(df_plot_w, symbol, signal_type, today_w, timeframe='Weekly', add_arrow=False)

            notify_text = f"{emoji} #{symbol} | {signal_type} | {date_str}"
            send_telegram_signal(msg, image_paths=[img_daily, img_weekly], notify_text=notify_text)

        except Exception as e:
            print(f"  ❌ Lỗi mã {symbol}: {e}")

    return new_signals

# =============================================================================
# BƯỚC 8B: HÀM PARSE LỆNH CHART (GIỮ NGUYÊN)
# =============================================================================
def parse_chart_command(text):
    text = text.strip()
    if not text.startswith('/'):
        return None
    body  = text[1:].strip()
    parts = body.split()
    if not parts:
        return None
    first = parts[0].lower()
    if first in ('chart', 'c'):
        symbols = parts[1:]
    elif first not in ('scan', 'help', 's', 'h'):
        symbols = parts
    else:
        return None
    symbols = [s.upper() for s in symbols if 1 <= len(s) <= 5 and s.isalnum()]
    return symbols if symbols else None

# =============================================================================
# BƯỚC 8C: HÀM GỬI CHART ON-DEMAND (GIỮ NGUYÊN)
# =============================================================================
def fetch_and_send_chart(symbol, chat_id):
    """Kéo data, vẽ chart Daily + Weekly, gửi về đúng chat_id yêu cầu."""
    symbol  = symbol.upper().strip()
    url_msg = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        quote  = Quote(symbol=symbol, source=DATA_SOURCE)
        df_raw = quote.history(length='1000', interval='1D')

        if df_raw is None or len(df_raw) < 60:
            requests.post(url_msg, data={
                'chat_id':    chat_id,
                'text':       f"❌ Không tìm thấy dữ liệu cho mã <b>{symbol}</b>",
                'parse_mode': 'HTML'
            })
            return

        df_raw['time'] = pd.to_datetime(df_raw['time'])
        df_raw.set_index('time', inplace=True)
        df_raw.columns = [c.lower() for c in df_raw.columns]

        df_calc     = compute_indicators(df_raw)
        today       = df_calc.iloc[-1]
        now_time    = int(datetime.now(TZ_VN).strftime("%H%M%S"))
        signal_type = detect_signal(df_calc, now_time) or "ON-DEMAND"
        date_str    = datetime.now(TZ_VN).strftime('%d/%m/%Y')
        pct         = (today['close'] - df_calc['close'].iloc[-2]) / df_calc['close'].iloc[-2] * 100
        change      = today['close'] - df_calc['close'].iloc[-2]
        vol_vs_prev  = (today['volume'] - df_calc['volume'].iloc[-2]) / df_calc['volume'].iloc[-2] * 100
        vol_vs_vma50 = (today['volume'] - today['VMA50']) / today['VMA50'] * 100 if today['VMA50'] > 0 else 0

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

        df_plot_d = df_calc.tail(250).copy()
        img_daily = draw_chart(df_plot_d, symbol, signal_type, today, timeframe='Daily', add_arrow=False)

        df_weekly  = build_weekly_df(df_raw)
        df_plot_w  = df_weekly.tail(150).copy()
        today_w    = df_plot_w.iloc[-1]
        img_weekly = draw_chart(df_plot_w, symbol, signal_type, today_w, timeframe='Weekly', add_arrow=False)

        _send_chart_to_chat(msg, [img_daily, img_weekly], chat_id)

    except Exception as e:
        requests.post(url_msg, data={
            'chat_id':    chat_id,
            'text':       f"❌ Lỗi lấy dữ liệu <b>{symbol}</b>: {e}",
            'parse_mode': 'HTML'
        })


def _send_chart_to_chat(msg, image_paths, chat_id):
    """Gửi album ảnh kèm caption vào chat_id chỉ định."""
    url_photo = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    url_album = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMediaGroup"
    try:
        if len(image_paths) == 1:
            with open(image_paths[0], 'rb') as f:
                requests.post(url_photo, data={
                    'chat_id': chat_id, 'caption': msg or '', 'parse_mode': 'HTML'
                }, files={'photo': f})
        else:
            files = {}
            media = []
            for i, path in enumerate(image_paths):
                key        = f"photo{i}"
                files[key] = open(path, 'rb')
                item       = {"type": "photo", "media": f"attach://{key}"}
                if i == 0 and msg:
                    item["caption"]    = msg
                    item["parse_mode"] = "HTML"
                media.append(item)
            try:
                requests.post(url_album, data={
                    'chat_id': chat_id,
                    'media':   json.dumps(media),
                }, files=files)
            finally:
                for f in files.values(): f.close()
    except Exception as e:
        print(f"  ❌ Lỗi gửi chart on-demand: {e}")
    finally:
        for path in image_paths:
            if os.path.exists(path): os.remove(path)

# =============================================================================
# BƯỚC 8D: TELEGRAM LISTENER (GIỮ NGUYÊN)
# =============================================================================
def telegram_listener():
    offset  = 0
    url_upd = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    url_msg = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    processed_ids     = set()
    MAX_PROCESSED_IDS = 50

    print("🎧 Telegram Listener đã khởi động — nhận lệnh từ Group & Private Chat 24/7...")

    while True:
        try:
            resp = requests.get(url_upd, params={
                'offset': offset, 'timeout': 30, 'allowed_updates': ['message']
            }, timeout=35)
            data = resp.json()

            for update in data.get('result', []):
                update_id = update['update_id']

                if update_id in processed_ids:
                    offset = update_id + 1
                    continue

                processed_ids.add(update_id)
                offset = update_id + 1

                if len(processed_ids) > MAX_PROCESSED_IDS:
                    processed_ids.clear()

                msg_obj = update.get('message', {})
                text    = msg_obj.get('text', '').strip()
                chat_id = msg_obj.get('chat', {}).get('id')

                if not text or not chat_id:
                    continue

                if str(chat_id) not in ALLOWED_CHATS:
                    continue

                text_lower = text.lower().strip()

                if text_lower in ('/scan', '/s') or text_lower.startswith(('/scan ', '/s ')):
                    if alerted_today:
                        lines = [f"{SIGNAL_EMOJI.get(v,'📌')} #{k}: {v}"
                                 for k, v in alerted_today.items()]
                        reply = "📋 <b>Tín hiệu hôm nay:</b>\n" + "\n".join(lines)
                    else:
                        reply = "📋 Chưa có tín hiệu nào hôm nay."
                    requests.post(url_msg, data={
                        'chat_id': chat_id, 'text': reply, 'parse_mode': 'HTML'
                    })

                elif text_lower in ('/help', '/h') or text_lower.startswith(('/help ', '/h ')):
                    requests.post(url_msg, data={
                        'chat_id':    chat_id,
                        'parse_mode': 'HTML',
                        'text': (
                            "🤖 <b>Lệnh hỗ trợ:</b>\n\n"
                            "<b>Xem chart:</b>\n"
                            "/HPG\n"
                            "/c HPG\n"
                            "/chart HPG\n"
                            "/HPG VNM FPT  (nhiều mã, tối đa 5)\n\n"
                            "<b>Khác:</b>\n"
                            "/scan  hoặc  /s  — Tín hiệu hôm nay\n"
                            "/help  hoặc  /h  — Trợ giúp"
                        )
                    })

                else:
                    symbols = parse_chart_command(text)
                    if symbols:
                        for sym in symbols[:5]:
                            print(f"  📥 Nhận lệnh chart {sym} — chat_id: {chat_id}")
                            threading.Thread(
                                target=fetch_and_send_chart,
                                args=(sym, chat_id),
                                daemon=True
                            ).start()
                            time.sleep(0.5)

        except requests.exceptions.Timeout:
            continue
        except Exception as e:
            print(f"  ❌ Listener lỗi: {e}")
            time.sleep(5)

# =============================================================================
# BƯỚC 9: KHỞI ĐỘNG
# =============================================================================
alerted_today = {}
last_run_date = datetime.now(TZ_VN).date()

# ── Listener chạy 24/7 trong thread riêng ────────────────────────────────────
listener_thread = threading.Thread(target=telegram_listener, daemon=True)
listener_thread.start()

print("\n" + "="*60)
print("⚙️  AUTO-SCANNER + TELEGRAM LISTENER ĐÃ KÍCH HOẠT")
print(f"   Danh sách  : {len(symbols_to_scan)} mã")
print(f"   Chu kỳ quét: {SCAN_INTERVAL_SEC} giây")
print(f"   Tín hiệu   → Channel/Group: {TELEGRAM_CHAT_ID}")
print(f"   Lệnh chart : /HPG | /c HPG | /chart HPG")
print(f"   Lệnh khác  : /scan (/s) | /help (/h)")
print(f"   Nhận lệnh  : Group + Private Chat (24/7)")
print("="*60)

# ── BƯỚC 1: Load cache lịch sử ngay khi khởi động ───────────────────────────
print("\n🔧 Đang load cache lịch sử lần đầu...")
build_history_cache(symbols_to_scan, last_run_date)

# ── Vòng lặp chính ───────────────────────────────────────────────────────────
while True:
    now_obj      = datetime.now(TZ_VN)
    current_date = now_obj.date()
    now_time     = int(now_obj.strftime("%H%M%S"))
    ts           = now_obj.strftime("%H:%M:%S")

    # Ngày mới: reset alert + reload cache lịch sử
    if current_date > last_run_date:
        alerted_today.clear()
        last_run_date = current_date
        print(f"\n🌅 [{ts}] Ngày mới {current_date.strftime('%d/%m/%Y')} — Đã reset bộ nhớ tín hiệu.")
        print("🔧 Reload cache lịch sử cho ngày mới...")
        build_history_cache(symbols_to_scan, current_date)

    # ── Ngoài giờ giao dịch: build/cập nhật cache nếu chưa có ───────────────
    is_morning   = 85000 <= now_time <= 113000
    is_afternoon = 130000 <= now_time <= 150000

    if not (is_morning or is_afternoon):
        # Nếu cache chưa load hôm nay thì load luôn
        with cache_lock:
            cache_ok = (cache_date == current_date and len(history_cache) > 0)
        if not cache_ok:
            print(f"[{ts}] Cache chưa có — tải lịch sử trước giờ giao dịch...")
            build_history_cache(symbols_to_scan, current_date)

        if   now_time < 85000:  next_open = "09:00"
        elif now_time < 130000: next_open = "13:00"
        else:                   next_open = "09:00 ngày mai"
        print(f"[{ts}] ⏸  Ngoài giờ giao dịch → Đợi đến {next_open}. Listener vẫn chạy.")
        time.sleep(SCAN_INTERVAL_SEC)
        continue

    # ── Đảm bảo cache đã load trước khi scan ─────────────────────────────────
    with cache_lock:
        cache_ok = (cache_date == current_date and len(history_cache) > 0)
    if not cache_ok:
        print(f"[{ts}] ⚠️  Cache chưa sẵn sàng — đang load...")
        build_history_cache(symbols_to_scan, current_date)

    print(f"\n{'='*60}")
    print(f"🔄 [{ts}] BẮT ĐẦU CHU KỲ QUÉT (price_board realtime)")
    print(f"{'='*60}")

    new_signals = run_scan_cycle(symbols_to_scan, now_time, alerted_today)

    if new_signals:
        print(f"✅ [{ts}] {len(new_signals)} tín hiệu MỚI: {', '.join(new_signals)}")
    else:
        print(f"[{ts}] Không có tín hiệu mới.")

    if alerted_today:
        summary_str = " | ".join([f"{k}:{v}" for k, v in alerted_today.items()])
        print(f"   📋 Đã báo hôm nay: {summary_str}")

    print(f"⏳ Đợi {SCAN_INTERVAL_SEC}s cho chu kỳ tiếp theo...")
    time.sleep(SCAN_INTERVAL_SEC)
