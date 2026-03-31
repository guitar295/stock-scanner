"""
=============================================================================
SCANNER TÍN HIỆU MUA CỔ PHIẾU — PHIÊN BẢN ĐẦY ĐỦ + AUTO-SCANNER
Pocket Pivot / Breakout / Pre-Break
Tích hợp: vnstock + Telegram + Chart mplfinance + Chống spam + Nghỉ ngoài giờ
=============================================================================
"""

# =============================================================================
# BƯỚC 0: CÀI ĐẶT THƯ VIỆN (chạy 1 lần nếu chưa có)
# =============================================================================
#!pip install -U vnstock pandas requests mplfinance pytz

# =============================================================================
# BƯỚC 1: IMPORT
# =============================================================================
from vnstock import register_user, Listing, Quote
import pandas as pd
import numpy as np
import requests
import time
import mplfinance as mpf
import matplotlib.pyplot as plt
import os
from datetime import datetime
import pytz


# =============================================================================
# BƯỚC 2: CẤU HÌNH
# =============================================================================
VNSTOCK_API        = os.environ.get('VNSTOCK_API')
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID   = os.environ.get('TELEGRAM_CHAT_ID')

SCAN_INTERVAL_SEC  = 120
TZ_VN              = pytz.timezone('Asia/Ho_Chi_Minh')

register_user(VNSTOCK_API)

# =============================================================================
# BƯỚC 3: HÀM GỬI TELEGRAM
# =============================================================================
def send_telegram_signal(message, image_path=None):
    try:
        if image_path and os.path.exists(image_path):
            url_photo = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
            with open(image_path, 'rb') as photo:
                payload = {
                    "chat_id": TELEGRAM_CHAT_ID,
                    "caption": message,
                    "parse_mode": "HTML"
                }
                requests.post(url_photo, data=payload, files={"photo": photo})
            print(f"  ✅ Đã gửi chart: {image_path}")
            os.remove(image_path)
        else:
            url_msg = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
            requests.post(url_msg, data=payload)
            print(f"  ✅ Đã gửi tin nhắn văn bản.")
    except Exception as e:
        print(f"  ❌ Lỗi gửi Telegram: {e}")

# =============================================================================
# BƯỚC 4: DANH SÁCH MÃ QUÉT
# =============================================================================
listing     = Listing(source='VCI')
df_listing  = listing.all_symbols()
col_name    = 'symbol' if 'symbol' in df_listing.columns else 'ticker'
all_symbols = df_listing[col_name].dropna().unique().tolist()

vn30_symbols = [
    'AAA','ACB','ANV','BFC','BID','BSR','BVH','BWE','CII','CRE','CTD','CTG','CTI','CTR','CTS','DBC','DCM','DGW','DIG','DPG','DPM','DXG','FCN','FPT','FRT','FTS','GAS','GEG','GEX','GMD','GVR','HAG','HAX','HBC','HCM','HDB','HDC','HDG','HNG','HPG','HSG','HTN','IDC','IJC','KBC','KDH','KSB','LPB','LTG','MBB','MBS','MSB','MSN','MWG','NKG','NLG','NTL','NVL','PC1','PET','PLC','PLX','PNJ','POW','PVD','PVS','PVT','REE','SBT','SCR','SHB','SHS','SSI','STB','SZC','TCB','TIG','TNG','TPB','VCB','VCI','VGT','VHC','VHM','VIB','VIC','VJC','VNM','VPB','VRE','MIG','HAH','HHV','BSI','C4G','G36','OIL','VGC','VND','BAF'
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
    df['MACD_Hist'] = df['MACD_Hist_origin']*3
    df['A']           = df['close'].pct_change()
    return df

# =============================================================================
# BƯỚC 6: CÁC HÀM ĐIỀU KIỆN TÍN HIỆU
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
    """Trả về loại tín hiệu tại phiên cuối: BREAKOUT > POCKET PIVOT > PRE-BREAK > None"""
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
# BƯỚC 7: VẼ BIỂU ĐỒ
# =============================================================================
def draw_chart(df_plot, symbol, signal_type, today):
    pct      = (today['close'] - df_plot['close'].iloc[-2]) / df_plot['close'].iloc[-2] * 100
    hist_val = df_plot['MACD_Hist'].values
    macd_colors = []
    for i, val in enumerate(hist_val):
        prev = hist_val[i-1] if i > 0 else 0
        if val >= 0: macd_colors.append('#26A69A' if val >= prev else '#B2DFDB')
        else:        macd_colors.append('#EF5350' if val <= prev else '#FFCDD2')

    colors_vol = ['#26A69A' if r['close'] >= r['open'] else '#EF5350' for _, r in df_plot.iterrows()]

    apds = [
        mpf.make_addplot(df_plot['EMA10'],        color='red',    width=0.6),
        mpf.make_addplot(df_plot['EMA20'],        color='green',  width=0.6),
        mpf.make_addplot(df_plot['EMA50'],        color='purple', width=0.6),
        mpf.make_addplot(df_plot['MA200'],       color='brown',  width=0.6),
        mpf.make_addplot(df_plot['volume'],      type='bar', panel=1, color=colors_vol,  alpha=1.0),
        mpf.make_addplot(df_plot['MACD_Hist'],   type='bar', panel=2, color=macd_colors, secondary_y=False),
        mpf.make_addplot(df_plot['MACD'],        panel=2, color='blue',   width=0.6, secondary_y=False),
        mpf.make_addplot(df_plot['MACD_Signal'], panel=2, color='orange', width=0.6, secondary_y=False),
    ]
    mc           = mpf.make_marketcolors(up='#26A69A', down='#EF5350', edge='inherit', wick='inherit', alpha=1.0)
    custom_style = mpf.make_mpf_style(base_mpf_style='charles', marketcolors=mc, gridstyle='', facecolor='white')
    img_name     = f"{symbol}.png"
    
    date_str = datetime.now(TZ_VN).strftime('%d/%m/%Y')
    
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
    ax_price.annotate(r'$\mathbf{\uparrow}$',
        xy=(len(df_plot)-1, today['low']), xytext=(0,-8), textcoords='offset points',
        ha='center', va='top', color='DeepPink', fontsize=12)
    ax_price.set_title(
        f"[{signal_type}] {date_str} {symbol} | "
        f"O:{today['open']:.2f}  H:{today['high']:.2f}  "
        f"L:{today['low']:.2f}  C:{today['close']:.2f} ({pct:+.2f}%)",
        loc='left', fontsize=11)

    if len(axlist) > 4:
        ax_macd = axlist[4]
        ax_macd.yaxis.set_ticks([]); ax_macd.yaxis.set_ticklabels([])
        m_vals = pd.concat([df_plot['MACD'], df_plot['MACD_Signal'], df_plot['MACD_Hist']])
        abs_m  = max(abs(m_vals.min()), abs(m_vals.max()))
        ax_macd.set_ylim(-abs_m*1.1, abs_m*1.1)
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
# BƯỚC 8: HÀM QUÉT 1 CHU KỲ
# =============================================================================
# Thứ tự ưu tiên tín hiệu — dùng để xét "leo hạng"
SIGNAL_RANK  = {'PRE-BREAK': 1, 'POCKET PIVOT': 2, 'BREAKOUT': 3}
SIGNAL_EMOJI = {'BREAKOUT': '🟢', 'POCKET PIVOT': '🟡', 'PRE-BREAK': '🟣'}

def run_scan_cycle(symbols, now_time, alerted_today):
    """
    Quét toàn bộ danh sách mã trong 1 chu kỳ.

    Logic chống spam:
    - Nếu đã báo tín hiệu CÙNG LOẠI hoặc MẠNH HƠN rồi → bỏ qua.
    - Nếu tín hiệu LEO HẠNG (VD: PRE-BREAK → BREAKOUT) → báo lại có ghi chú "Nâng cấp".
    - Mỗi mã chỉ được báo tối đa 1 lần mỗi cấp độ trong ngày.

    Trả về danh sách mã phát tín hiệu mới trong chu kỳ này.
    """
    new_signals    = []
    current_date   = datetime.now(TZ_VN).date()
    date_str = datetime.now(TZ_VN).strftime('%d/%m/%Y')

    for symbol in symbols:
        for attempt in range(3):
            try:
                quote  = Quote(symbol=symbol, source='VCI')
                df_raw = quote.history(length='600', interval='1D')

                if df_raw is None or len(df_raw) < 200:
                    break

                df_raw['time'] = pd.to_datetime(df_raw['time'])
                df_raw.set_index('time', inplace=True)
                df_raw.columns = [c.lower() for c in df_raw.columns]

                # Cập nhật nến real-time nếu chưa có dữ liệu hôm nay
                if df_raw.index.max().date() < current_date:
                    try:
                        today_df = quote.history(
                            start_date=current_date.strftime("%Y-%m-%d"),
                            end_date=current_date.strftime("%Y-%m-%d"),
                            interval='1D'
                        )
                        if today_df is not None and not today_df.empty:
                            today_df['time'] = pd.to_datetime(today_df['time'])
                            today_df.set_index('time', inplace=True)
                            today_df.columns = [c.lower() for c in today_df.columns]
                            if today_df.index.max().date() == current_date:
                                df_raw = df_raw[df_raw.index.date != current_date]
                                df_raw = pd.concat([df_raw, today_df]).sort_index().last('600D')
                    except Exception:
                        pass  # Giữ dữ liệu cũ nếu lỗi cập nhật

                # Phát hiện tín hiệu
                df_calc     = compute_indicators(df_raw)
                signal_type = detect_signal(df_calc, now_time)

                if not signal_type:
                    break

                # --- Kiểm tra chống spam ---
                prev_signal      = alerted_today.get(symbol)
                prev_rank        = SIGNAL_RANK.get(prev_signal, 0)
                current_rank     = SIGNAL_RANK.get(signal_type, 0)

                # Bỏ qua nếu tín hiệu hiện tại KHÔNG mạnh hơn tín hiệu đã báo
                if prev_rank >= current_rank:
                    break

                # Tín hiệu mới hoặc leo hạng → cập nhật bộ nhớ & gửi báo
                alerted_today[symbol] = signal_type
                new_signals.append(symbol)

                today        = df_calc.iloc[-1]
                df_plot      = df_calc.tail(250).copy()
                pct          = (today['close'] - df_calc['close'].iloc[-2]) / df_calc['close'].iloc[-2] * 100
                change       = today['close'] - df_calc['close'].iloc[-2]
                emoji        = SIGNAL_EMOJI.get(signal_type, '📌')
                vol_vs_prev  = (today['volume'] - df_calc['volume'].iloc[-2]) / df_calc['volume'].iloc[-2] * 100
                vol_vs_vma50 = (today['volume'] - today['VMA50']) / today['VMA50'] * 100 if today['VMA50'] > 0 else 0

                # Tạo các link hỗ trợ
                link_vnd_detail  = f"https://dstock.vndirect.com.vn/tong-quan/{symbol}/diem-nhan-co-ban-popup"
                link_vnd_news    = f"https://dstock.vndirect.com.vn/tong-quan/{symbol}/tin-tuc-ma-popup?type=dn"
                link_vietstock   = f"https://stockchart.vietstock.vn/?stockcode={symbol}"
                link_vnd_summary = f"https://dstock.vndirect.com.vn/tong-quan/{symbol}"
                link_24h_money   = f"https://24hmoney.vn/stock/{symbol}/news"
                
                # Ghi chú leo hạng
                upgrade_note = (f"\n⬆️ <i>Nâng cấp từ {prev_signal}</i>" if prev_signal else "")

                img_name = draw_chart(df_plot, symbol, signal_type, today)
                msg = (
                    f"{emoji} #{symbol}  {date_str} \n"
                    f"Sig: {signal_type} \n"
                    f"Clo: <b>{today['close']:.2f}</b> ({change:+.2f} / {pct:+.2f}%) \n"
                    f"Vol: {vol_vs_prev:+.1f}% | {vol_vs_vma50:+.1f}% \n"
                    f"<a href='{link_vnd_detail}'>⚖️</a> "
                    f"<a href='{link_vnd_news}'>🗞️</a> "
                    f"<a href='{link_vietstock}'>📈</a> "
                    f"<a href='{link_vnd_summary}'>📄</a> "
                    f"<a href='{link_24h_money}'>📄</a>"
                )
                send_telegram_signal(msg, image_path=img_name)
                break  # Thành công → thoát retry

            except Exception as e:
                if 'timeout' in str(e).lower() and attempt < 2:
                    time.sleep(2.0)
                else:
                    print(f"  ❌ Lỗi mã {symbol}: {e}")
                    break

        time.sleep(1)  # Nghỉ ngắn giữa mã để tránh sập API

    return new_signals

# =============================================================================
# BƯỚC 9: AUTO-SCANNER — VÒNG LẶP CHÍNH
# =============================================================================
# alerted_today lưu tín hiệu cao nhất đã báo trong ngày cho mỗi mã
# VD: {'SSI': 'BREAKOUT', 'VND': 'PRE-BREAK'}
# Reset tự động khi sang ngày mới
alerted_today = {}
last_run_date = datetime.now(TZ_VN).date()

print("\n" + "="*60)
print("⚙️  AUTO-SCANNER ĐÃ KÍCH HOẠT")
print(f"   Danh sách: {len(symbols_to_scan)} mã")
print(f"   Chu kỳ quét: {SCAN_INTERVAL_SEC} giây")
print(f"   Giờ hoạt động: 09:00–11:30 | 13:00–15:00")
print("="*60)

while True:
    now_obj      = datetime.now(TZ_VN)
    current_date = now_obj.date()
    now_time     = int(now_obj.strftime("%H%M%S"))
    ts           = now_obj.strftime("%H:%M:%S")

    # --- Reset bộ nhớ khi sang ngày mới ---
    if current_date > last_run_date:
        alerted_today.clear()
        last_run_date = current_date
        print(f"\n🌅 [{ts}] Ngày mới {current_date.strftime('%d/%m/%Y')} — Đã reset bộ nhớ tín hiệu.")

    # --- Khoá ngoài giờ giao dịch ---
    is_morning   = 85000 <= now_time <= 113500
    is_afternoon = 130000 <= now_time <= 150000

    if not (is_morning or is_afternoon):
        if   now_time < 85000:  next_open = "09:00"
        elif now_time < 130000: next_open = "13:00"
        else:                   next_open = "09:00 ngày mai"
        print(f"[{ts}] ⏸  Ngoài giờ giao dịch → Đợi đến {next_open}. Ngủ {SCAN_INTERVAL_SEC}s...")
        time.sleep(SCAN_INTERVAL_SEC)
        continue

    # --- Bắt đầu chu kỳ quét ---
    print(f"\n{'='*60}")
    print(f"🔄 [{ts}] BẮT ĐẦU CHU KỲ QUÉT")
    print(f"{'='*60}")

    new_signals = run_scan_cycle(symbols_to_scan, now_time, alerted_today)

    # --- Tổng kết chu kỳ ---
    if new_signals:
        print(f"✅ [{ts}] {len(new_signals)} tín hiệu MỚI: {', '.join(new_signals)}")
    else:
        print(f"[{ts}] Không có tín hiệu mới.")

    if alerted_today:
        summary_str = " | ".join([f"{k}:{v}" for k, v in alerted_today.items()])
        print(f"   📋 Đã báo hôm nay: {summary_str}")

    print(f"⏳ Đợi {SCAN_INTERVAL_SEC}s cho chu kỳ tiếp theo...")
    time.sleep(SCAN_INTERVAL_SEC)
