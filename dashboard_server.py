"""
PATCH CHO dashboard_server.py
==============================
Thay thế toàn bộ phần HTML/JS liên quan đến TV Chart.

Cách dùng:
    python3 dashboard_patch.py /path/to/dashboard_server.py

Script sẽ tạo file backup .bak rồi ghi đè file gốc.
"""

import sys, re, shutil, os

# ─────────────────────────────────────────────────────────────────────────────
# ĐOẠN HTML MỚI — thay thế toàn bộ block <!-- ██ TAB TRADINGVIEW ... ██ -->
# ─────────────────────────────────────────────────────────────────────────────
NEW_TV_HTML = r"""      <!-- ██ TAB TRADINGVIEW LIGHTWEIGHT CHART ██ -->
      <div class="tpanel on" id="panel-tv">
        <div id="tv-loading">⏳ Đang tải dữ liệu...</div>
        <div id="tv-chart-wrap" style="display:none;flex-direction:column;height:100%;">
          <!-- Toolbar: timeframe + inline search -->
          <div id="tv-toolbar" style="display:flex;align-items:center;gap:8px;padding:5px 10px;background:#fafafa;border-bottom:1px solid #eeeeee;flex-shrink:0;">
            <span id="tv-sym-label" style="font-family:'Barlow Condensed',sans-serif;font-size:14px;font-weight:800;color:#1a56db;letter-spacing:1px;min-width:52px;">—</span>
            <!-- Timeframe buttons -->
            <div style="display:flex;gap:3px;">
              <button id="tf-btn-D" onclick="tvSetTF('D')" class="tf-btn tf-on">D</button>
              <button id="tf-btn-W" onclick="tvSetTF('W')" class="tf-btn">W</button>
            </div>
            <!-- OHLC mini -->
            <span style="font-family:'IBM Plex Mono',monospace;font-size:10px;color:#6b7280;margin-left:4px;">
              O<span id="leg-open" style="color:#374151;margin-left:2px;">—</span>
              &nbsp;H<span id="leg-high" style="color:#0e9f6e;margin-left:2px;">—</span>
              &nbsp;L<span id="leg-low" style="color:#e02424;margin-left:2px;">—</span>
              &nbsp;C<span id="leg-close" style="color:#374151;margin-left:2px;font-weight:700;">—</span>
            </span>
            <!-- Inline search -->
            <div style="margin-left:auto;position:relative;display:flex;align-items:center;">
              <span style="position:absolute;left:9px;color:#9ca3af;font-size:11px;pointer-events:none;">🔍</span>
              <input id="tv-sym-input" type="text" placeholder="Nhập mã..." maxlength="10" autocomplete="off" spellcheck="false"
                style="width:110px;padding:4px 8px 4px 26px;border-radius:16px;border:1px solid #dde3ee;background:#fff;color:#111827;font-family:'IBM Plex Mono',monospace;font-size:11px;outline:none;transition:border-color .15s,box-shadow .15s,width .2s;"
                onfocus="this.select();this.style.borderColor='#1a56db';this.style.boxShadow='0 0 0 2px rgba(26,86,219,.12)';this.style.width='140px';"
                onblur="this.style.borderColor='#dde3ee';this.style.boxShadow='none';this.style.width='110px';">
            </div>
          </div>
          <!-- Main chart -->
          <div id="tv-chart-container" style="flex:1;position:relative;background:#fff;min-height:0;"></div>
          <!-- MACD panel -->
          <div id="tv-macd-container" style="height:110px;flex-shrink:0;background:#fff;border-top:1px solid #f0f0f0;position:relative;">
            <div id="tv-macd-legend" style="position:absolute;top:3px;left:8px;z-index:10;display:flex;align-items:center;gap:8px;font-family:'IBM Plex Mono',monospace;font-size:9px;pointer-events:none;">
              <span style="font-weight:700;color:#374151;">MACD(12,26,9)</span>
              <span style="color:#9ca3af;">MACD</span><span id="leg-macd" style="color:#2563eb;font-weight:600;">—</span>
              <span style="color:#9ca3af;">Signal</span><span id="leg-signal" style="color:#f97316;font-weight:600;">—</span>
              <span style="color:#9ca3af;">Hist</span><span id="leg-hist" style="font-weight:600;">—</span>
            </div>
          </div>
        </div>
      </div>"""

# ─────────────────────────────────────────────────────────────────────────────
# CSS MỚI — thêm vào trong <style>
# ─────────────────────────────────────────────────────────────────────────────
NEW_CSS_INJECT = r"""
/* ── TIMEFRAME BUTTONS ── */
.tf-btn{font-family:'IBM Plex Mono',monospace;font-size:11px;font-weight:600;padding:3px 10px;border-radius:4px;border:1px solid #dde3ee;background:#f4f6fb;color:#6b7280;cursor:pointer;transition:all .15s;line-height:1.4;}
.tf-btn:hover{background:#eef3ff;color:#1a56db;border-color:#1a56db;}
.tf-btn.tf-on{background:#1a56db;color:#fff;border-color:#1a56db;}
"""

# ─────────────────────────────────────────────────────────────────────────────
# JS MỚI — thay thế toàn bộ block // ═══ TRADINGVIEW LIGHTWEIGHT CHART ═══
# ─────────────────────────────────────────────────────────────────────────────
NEW_TV_JS = r"""// ═══════════════════════════════════════════════════════
// TRADINGVIEW LIGHTWEIGHT CHART
// ═══════════════════════════════════════════════════════
let _tvChart=null, _tvMainSeries=null, _tvMacdChart=null;
let _tvHistSeries=null, _tvMacdLine=null, _tvSignalLine=null;
let _tvVolSeries=null;
let _tvLastSym='', _tvTF='D';

// Màu đúng theo ảnh mẫu
const TV_COLORS={
  ema10:  '#26a69a',   // xanh lá
  ema20:  '#ef5350',   // đỏ
  ema50:  '#9c27b0',   // tím
  ma200:  '#795548',   // nâu đỏ
  macdLine:   '#1565c0',
  macdSignal: '#f57c00',
  macdHistUp: 'rgba(38,166,154,0.65)',
  macdHistDn: 'rgba(239,83,80,0.65)',
  volUp:  'rgba(38,166,154,0.5)',
  volDn:  'rgba(239,83,80,0.5)',
};

function _tvDestroy(){
  if(_tvChart)    {try{_tvChart.remove();}   catch(e){} _tvChart=null;}
  if(_tvMacdChart){try{_tvMacdChart.remove();}catch(e){} _tvMacdChart=null;}
  _tvMainSeries=null;_tvVolSeries=null;
  _tvHistSeries=null;_tvMacdLine=null;_tvSignalLine=null;
}

function _tvResize(){
  const mainEl=document.getElementById('tv-chart-container');
  const macdEl=document.getElementById('tv-macd-container');
  if(mainEl&&_tvChart)     _tvChart.applyOptions({width:mainEl.clientWidth,height:mainEl.clientHeight});
  if(macdEl&&_tvMacdChart) _tvMacdChart.applyOptions({width:macdEl.clientWidth,height:macdEl.clientHeight});
}
const _tvResizeObs=new ResizeObserver(()=>_tvResize());

// Resample daily → weekly (lấy nến tuần từ dữ liệu ngày)
function _toWeekly(candles){
  if(!candles||!candles.length) return candles;
  const weeks={};
  candles.forEach(c=>{
    const d=new Date(c.time*1000);
    // ISO week start = Monday
    const day=d.getUTCDay()||7; // 1=Mon..7=Sun
    const monday=new Date(d);monday.setUTCDate(d.getUTCDate()-(day-1));monday.setUTCHours(0,0,0,0);
    const key=Math.floor(monday.getTime()/1000);
    if(!weeks[key]){weeks[key]={time:key,open:c.open,high:c.high,low:c.low,close:c.close};}
    else{
      weeks[key].high=Math.max(weeks[key].high,c.high);
      weeks[key].low=Math.min(weeks[key].low,c.low);
      weeks[key].close=c.close;
    }
  });
  return Object.values(weeks).sort((a,b)=>a.time-b.time);
}
function _toWeeklyLine(arr){
  if(!arr||!arr.length) return arr;
  const weeks={};
  arr.forEach(p=>{
    const d=new Date(p.time*1000);const day=d.getUTCDay()||7;
    const monday=new Date(d);monday.setUTCDate(d.getUTCDate()-(day-1));monday.setUTCHours(0,0,0,0);
    const key=Math.floor(monday.getTime()/1000);
    weeks[key]={time:key,value:p.value};
  });
  return Object.values(weeks).sort((a,b)=>a.time-b.time);
}
function _toWeeklyHist(arr){
  if(!arr||!arr.length) return arr;
  const weeks={};
  arr.forEach(p=>{
    const d=new Date(p.time*1000);const day=d.getUTCDay()||7;
    const monday=new Date(d);monday.setUTCDate(d.getUTCDate()-(day-1));monday.setUTCHours(0,0,0,0);
    const key=Math.floor(monday.getTime()/1000);
    weeks[key]={time:key,value:p.value,color:p.color};
  });
  return Object.values(weeks).sort((a,b)=>a.time-b.time);
}
function _toWeeklyVol(arr){
  if(!arr||!arr.length) return arr;
  const weeks={};
  arr.forEach(p=>{
    const d=new Date(p.time*1000);const day=d.getUTCDay()||7;
    const monday=new Date(d);monday.setUTCDate(d.getUTCDate()-(day-1));monday.setUTCHours(0,0,0,0);
    const key=Math.floor(monday.getTime()/1000);
    if(!weeks[key]) weeks[key]={time:key,value:0,color:p.color};
    else { weeks[key].value+=p.value; weeks[key].color=p.color; }
  });
  return Object.values(weeks).sort((a,b)=>a.time-b.time);
}

function tvSetTF(tf){
  _tvTF=tf;
  document.querySelectorAll('.tf-btn').forEach(b=>b.classList.toggle('tf-on',b.id==='tf-btn-'+tf));
  if(_tvLastSym) _tvBuildChart(_tvLastSym, window._tvRawData);
}

// Cache raw data để switch TF không gọi API lại
window._tvRawData=null;

async function loadTVChart(sym){
  sym=sym.toUpperCase().trim();
  _tvLastSym=sym;
  window._tvRawData=null;
  const loading=document.getElementById('tv-loading');
  const wrap=document.getElementById('tv-chart-wrap');
  loading.style.display='flex'; wrap.style.display='none';
  loading.innerHTML=`⏳ Đang tải dữ liệu <b>${sym}</b>...`;
  document.getElementById('tv-sym-label').textContent=sym;

  try{
    const r=await fetch(`/api/ohlcv/${sym}`);
    if(!r.ok){const j=await r.json().catch(()=>({}));throw new Error(j.error||`HTTP ${r.status}`);}
    const d=await r.json();
    if(!d.candles||d.candles.length<2) throw new Error('Không đủ dữ liệu');
    window._tvRawData=d;
    _tvBuildChart(sym,d);
  }catch(err){
    loading.innerHTML=`<div style="text-align:center;color:#9ca3af;padding:24px">
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

function _tvBuildChart(sym, d){
  const loading=document.getElementById('tv-loading');
  const wrap=document.getElementById('tv-chart-wrap');
  const isWeekly=(_tvTF==='W');

  // Chuyển đổi dữ liệu theo TF
  const candles = isWeekly ? _toWeekly(d.candles) : d.candles;
  const ema10   = isWeekly ? _toWeeklyLine(d.ema10)   : d.ema10;
  const ema20   = isWeekly ? _toWeeklyLine(d.ema20)   : d.ema20;
  const ema50   = isWeekly ? _toWeeklyLine(d.ema50)   : d.ema50;
  const ma200   = isWeekly ? _toWeeklyLine(d.ma200)   : d.ma200;
  const volume  = isWeekly ? _toWeeklyVol(d.volume)   : d.volume;
  const macdLine   = isWeekly ? _toWeeklyLine(d.macd_line)   : d.macd_line;
  const macdSignal = isWeekly ? _toWeeklyLine(d.macd_signal) : d.macd_signal;
  const macdHist   = isWeekly ? _toWeeklyHist(d.macd_hist)   : d.macd_hist;

  _tvDestroy();

  const mainEl=document.getElementById('tv-chart-container');
  const macdEl=document.getElementById('tv-macd-container');

  // ── CHART CHÍNH ──
  _tvChart=LightweightCharts.createChart(mainEl,{
    width:mainEl.clientWidth,
    height:mainEl.clientHeight,
    layout:{
      background:{type:'solid',color:'#ffffff'},
      textColor:'#9ca3af',
      fontSize:10,
      fontFamily:"'IBM Plex Mono',monospace",
    },
    grid:{
      vertLines:{color:'#f5f5f5',style:0},
      horzLines:{color:'#f5f5f5',style:0},
    },
    crosshair:{
      mode:LightweightCharts.CrosshairMode.Normal,
      vertLine:{color:'#bbbbbb',width:1,style:2,labelBackgroundColor:'#374151'},
      horzLine:{color:'#bbbbbb',width:1,style:2,labelBackgroundColor:'#374151'},
    },
    rightPriceScale:{
      borderColor:'#eeeeee',
      scaleMarginTop:0.06,
      scaleMarginBottom:0.22,  // chừa chỗ cho volume phía dưới
      textColor:'#9ca3af',
    },
    timeScale:{
      borderColor:'#eeeeee',
      rightOffset:30,
      fixLeftEdge:false,
      fixRightEdge:false,
      timeVisible:true,
      secondsVisible:false,
      tickMarkFormatter:(time)=>{
        const d=new Date(time*1000);
        const dd=String(d.getUTCDate()).padStart(2,'0');
        const mm=String(d.getUTCMonth()+1).padStart(2,'0');
        return `${dd}/${mm}`;
      },
    },
    handleScroll:true,
    handleScale:true,
  });

  // Candlestick — nến nhỏ gọn
  _tvMainSeries=_tvChart.addCandlestickSeries({
    upColor:'#26a69a', downColor:'#ef5350',
    borderUpColor:'#26a69a', borderDownColor:'#ef5350',
    wickUpColor:'#26a69a', wickDownColor:'#ef5350',
    priceLineVisible:false,
    lastValueVisible:true,
  });
  _tvMainSeries.setData(candles);

  // Volume histogram (price scale riêng, nằm dưới)
  _tvVolSeries=_tvChart.addHistogramSeries({
    priceFormat:{type:'volume'},
    priceScaleId:'vol',
    lastValueVisible:false,
    priceLineVisible:false,
  });
  _tvChart.priceScale('vol').applyOptions({
    scaleMarginTop:0.84,
    scaleMarginBottom:0.0,
    drawTicks:false,
    borderVisible:false,
    visible:false,
  });
  _tvVolSeries.setData(volume||[]);

  // EMA10 — xanh lá
  if(ema10&&ema10.length){
    const s=_tvChart.addLineSeries({color:TV_COLORS.ema10,lineWidth:1,priceLineVisible:false,lastValueVisible:false,crosshairMarkerVisible:false});
    s.setData(ema10);
  }
  // EMA20 — đỏ
  if(ema20&&ema20.length){
    const s=_tvChart.addLineSeries({color:TV_COLORS.ema20,lineWidth:1,priceLineVisible:false,lastValueVisible:false,crosshairMarkerVisible:false});
    s.setData(ema20);
  }
  // EMA50 — tím
  if(ema50&&ema50.length){
    const s=_tvChart.addLineSeries({color:TV_COLORS.ema50,lineWidth:1.5,priceLineVisible:false,lastValueVisible:false,crosshairMarkerVisible:false});
    s.setData(ema50);
  }
  // MA200 — nâu đỏ, dashed
  if(ma200&&ma200.length){
    const s=_tvChart.addLineSeries({
      color:TV_COLORS.ma200,lineWidth:1.5,
      lineStyle:LightweightCharts.LineStyle.Dashed,
      priceLineVisible:false,lastValueVisible:false,crosshairMarkerVisible:false,
    });
    s.setData(ma200);
  }

  // ── MACD CHART ──
  _tvMacdChart=LightweightCharts.createChart(macdEl,{
    width:macdEl.clientWidth,
    height:macdEl.clientHeight,
    layout:{background:{type:'solid',color:'#ffffff'},textColor:'#9ca3af',fontSize:9,fontFamily:"'IBM Plex Mono',monospace"},
    grid:{vertLines:{color:'#f8f8f8'},horzLines:{color:'#f8f8f8'}},
    crosshair:{
      mode:LightweightCharts.CrosshairMode.Normal,
      vertLine:{color:'#cccccc',width:1,style:2,labelVisible:false},
      horzLine:{color:'#cccccc',width:1,style:2,labelVisible:false},
    },
    rightPriceScale:{borderColor:'#eeeeee',scaleMarginTop:0.1,scaleMarginBottom:0.1,textColor:'#9ca3af'},
    timeScale:{borderColor:'#eeeeee',rightOffset:8,timeVisible:false,visible:false},
    handleScroll:true,
    handleScale:true,
  });

  _tvHistSeries=_tvMacdChart.addHistogramSeries({
    color:TV_COLORS.macdHistUp,
    priceLineVisible:false,lastValueVisible:false,
  });
  if(macdHist&&macdHist.length) _tvHistSeries.setData(macdHist);

  _tvMacdLine=_tvMacdChart.addLineSeries({color:TV_COLORS.macdLine,lineWidth:1.5,priceLineVisible:false,lastValueVisible:false,crosshairMarkerVisible:false});
  if(macdLine&&macdLine.length) _tvMacdLine.setData(macdLine);

  _tvSignalLine=_tvMacdChart.addLineSeries({color:TV_COLORS.macdSignal,lineWidth:1,priceLineVisible:false,lastValueVisible:false,crosshairMarkerVisible:false});
  if(macdSignal&&macdSignal.length) _tvSignalLine.setData(macdSignal);

  // Sync timescale
  _tvChart.timeScale().subscribeVisibleLogicalRangeChange(range=>{
    if(range&&_tvMacdChart) _tvMacdChart.timeScale().setVisibleLogicalRange(range);
  });
  _tvMacdChart.timeScale().subscribeVisibleLogicalRangeChange(range=>{
    if(range&&_tvChart) _tvChart.timeScale().setVisibleLogicalRange(range);
  });

  // Crosshair sync + legend update
  _tvChart.subscribeCrosshairMove(param=>{
    if(!param||!_tvMacdChart) return;
    if(param.time) _tvMacdChart.setCrosshairPosition(0,0,_tvHistSeries);
    if(!param.time||!param.seriesData) return;
    const c=param.seriesData.get(_tvMainSeries);
    if(c){
      document.getElementById('leg-open').textContent=c.open?.toFixed(2)||'—';
      document.getElementById('leg-high').textContent=c.high?.toFixed(2)||'—';
      document.getElementById('leg-low').textContent=c.low?.toFixed(2)||'—';
      document.getElementById('leg-close').textContent=c.close?.toFixed(2)||'—';
    }
    // MACD legend
    const mh=_tvHistSeries&&param.seriesData.get(_tvHistSeries);
  });
  _tvMacdChart.subscribeCrosshairMove(param=>{
    if(!param||!param.time||!param.seriesData) return;
    const ml=param.seriesData.get(_tvMacdLine);
    const ms=param.seriesData.get(_tvSignalLine);
    const mh=param.seriesData.get(_tvHistSeries);
    if(ml) document.getElementById('leg-macd').textContent=ml.value?.toFixed(4)||'—';
    if(ms) document.getElementById('leg-signal').textContent=ms.value?.toFixed(4)||'—';
    if(mh){
      const hEl=document.getElementById('leg-hist');
      hEl.textContent=mh.value?.toFixed(4)||'—';
      hEl.style.color=mh.value>=0?TV_COLORS.macdHistUp.replace('0.65','1'):TV_COLORS.macdHistDn.replace('0.65','1');
    }
  });

  // Hiển thị giá trị cuối ở legend
  const last=candles[candles.length-1];
  document.getElementById('tv-sym-label').textContent=sym+(isWeekly?' [W]':' [D]');
  document.getElementById('leg-open').textContent=last?.open?.toFixed(2)||'—';
  document.getElementById('leg-high').textContent=last?.high?.toFixed(2)||'—';
  document.getElementById('leg-low').textContent=last?.low?.toFixed(2)||'—';
  document.getElementById('leg-close').textContent=last?.close?.toFixed(2)||'—';
  const lastMacd=macdLine?.at(-1);if(lastMacd) document.getElementById('leg-macd').textContent=lastMacd.value?.toFixed(4)||'—';
  const lastSig=macdSignal?.at(-1);if(lastSig) document.getElementById('leg-signal').textContent=lastSig.value?.toFixed(4)||'—';
  const lastHist=macdHist?.at(-1);
  if(lastHist){
    const hEl=document.getElementById('leg-hist');
    hEl.textContent=lastHist.value?.toFixed(4)||'—';
    hEl.style.color=lastHist.value>=0?TV_COLORS.macdHistUp.replace('0.65','1'):TV_COLORS.macdHistDn.replace('0.65','1');
  }

  // Observer resize
  _tvResizeObs.disconnect();
  _tvResizeObs.observe(mainEl);
  _tvResizeObs.observe(macdEl);

  loading.style.display='none';
  wrap.style.display='flex';
}

// Inline search trên TV Chart
(function(){
  function _initTVInlineSearch(){
    const inp=document.getElementById('tv-sym-input');
    if(!inp) return;
    inp.addEventListener('keydown',function(e){
      if(e.key==='Enter'){
        const sym=this.value.trim().toUpperCase();
        if(sym.length>=2){ this.value=''; this.blur(); openChart(sym); }
      }
      if(e.key==='Escape'){ this.value=''; this.blur(); }
    });
  }
  // Gọi ngay và cả khi rebuild mobile header
  document.addEventListener('DOMContentLoaded',_initTVInlineSearch);
  setTimeout(_initTVInlineSearch,500);
  window._initTVInlineSearch=_initTVInlineSearch;
})();"""

# ─────────────────────────────────────────────────────────────────────────────
# MAIN PATCH
# ─────────────────────────────────────────────────────────────────────────────
def patch(filepath):
    if not os.path.exists(filepath):
        print(f"❌ Không tìm thấy file: {filepath}")
        sys.exit(1)

    # Backup
    bak = filepath + '.bak'
    shutil.copy2(filepath, bak)
    print(f"✅ Backup → {bak}")

    with open(filepath, 'r', encoding='utf-8') as f:
        src = f.read()

    # 1) Thêm CSS vào cuối </style>
    if 'tf-btn' not in src:
        src = src.replace('</style>', NEW_CSS_INJECT + '\n</style>', 1)
        print("✅ CSS timeframe buttons đã được thêm")
    else:
        print("ℹ️  CSS tf-btn đã tồn tại, bỏ qua")

    # 2) Thay block HTML TV Chart
    # Tìm từ "<!-- ██ TAB TRADINGVIEW" đến "</div>" đóng của panel-tv
    html_pat = re.compile(
        r'<!-- ██ TAB TRADINGVIEW LIGHTWEIGHT CHART ██ -->.*?(?=\n      <div class="tpanel" id="panel-vs")',
        re.DOTALL
    )
    if html_pat.search(src):
        src = html_pat.sub(NEW_TV_HTML + '\n', src)
        print("✅ HTML TV Chart đã được thay thế")
    else:
        print("⚠️  Không tìm thấy block HTML TV Chart — kiểm tra lại pattern")

    # 3) Thay block JS TV Chart
    js_pat = re.compile(
        r'// ═+\n// TRADINGVIEW LIGHTWEIGHT CHART\n// ═+.*?(?=\n// ═+\n// MOBILE LIGHTBOX)',
        re.DOTALL
    )
    if js_pat.search(src):
        src = js_pat.sub(NEW_TV_JS + '\n\n', src)
        print("✅ JS TV Chart đã được thay thế")
    else:
        print("⚠️  Không tìm thấy block JS TV Chart — kiểm tra lại pattern")

    # 4) Xóa legend cũ (tv-legend div) nếu còn sót
    src = re.sub(
        r'\n\s+<!-- Legend -->\n\s+<div id="tv-legend">.*?</div>\n',
        '\n',
        src,
        flags=re.DOTALL
    )

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(src)

    print(f"\n🎉 Patch hoàn tất! File đã cập nhật: {filepath}")
    print("   Khởi động lại bot để áp dụng thay đổi.")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Cách dùng: python3 dashboard_patch.py /path/to/dashboard_server.py")
        sys.exit(1)
    patch(sys.argv[1])
