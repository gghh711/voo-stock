"""
股票滾動年化斜率 + 恐懼指標 + LINE 通知 + 回測功能
"""

import datetime, math, os, threading
from datetime import timezone
import requests
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import flask
import dash
from dash import dcc, html, Input, Output, State

LINE_TOKEN   = os.environ.get("LINE_TOKEN", "BL02SzdP0SeOiz4iRC+fqU8X9hp+zmcejR4i9WGYNg9TFCM/i97k1M8vm8Hki5fM2CWuFEKQlF4vlMnNkVDV+YKVNxtSJxXIl0AYZ8xUVmLmJ6Cyd6qw8iCBY6VekjwyFbrF/ocFfRUymRkkiw9UMQdB04t89/1O/w1cDnyilFU=")
LINE_USER_ID = os.environ.get("LINE_USER_ID", "U2af0aa14205601e29e61d548c2f10f5a")
WATCH_TICKERS = ["QQQ", "VOO", "TSM"]

COLORS = ["#2563eb","#16a34a","#dc2626","#d97706","#7c3aed","#0891b2","#db2777","#65a30d","#b45309","#0f766e"]
HEADERS = {"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36","Accept":"application/json","Referer":"https://finance.yahoo.com"}

# ── 工具函數 ─────────────────────────────────────────────────

def send_line_to(to, message):
    try:
        requests.post("https://api.line.me/v2/bot/message/push",
            headers={"Authorization":f"Bearer {LINE_TOKEN}","Content-Type":"application/json"},
            json={"to":to,"messages":[{"type":"text","text":message}]},timeout=10)
    except Exception as e:
        print(f"LINE 發送失敗：{e}")

def send_line(message):
    if LINE_USER_ID:
        send_line_to(LINE_USER_ID, message)

def fetch_yahoo_range(ticker, start_dt, end_dt):
    start = int(start_dt.timestamp())
    end   = int(end_dt.timestamp())
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&period1={start}&period2={end}"
    r = requests.get(url, headers=HEADERS, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"Yahoo Finance 回應 {r.status_code}")
    data = r.json()
    chart = data.get("chart", {})
    if chart.get("error"):
        raise RuntimeError(chart["error"].get("description","未知錯誤"))
    result = chart.get("result")
    if not result: raise RuntimeError("找不到資料")
    result = result[0]
    timestamps = result["timestamp"]
    quote = result["indicators"]["quote"][0]
    closes_raw = quote.get("close",[])
    volumes_raw = quote.get("volume",[])
    dates, closes, volumes = [], [], []
    for ts, c, v in zip(timestamps, closes_raw, volumes_raw):
        if c is None: continue
        dates.append(datetime.datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d"))
        closes.append(float(c))
        volumes.append(float(v) if v is not None else 0.0)
    if not dates: raise RuntimeError("無資料")
    return dates, closes, volumes

def fetch_vix(start_dt, end_dt):
    try:
        dates, closes, _ = fetch_yahoo_range("%5EVIX", start_dt, end_dt)
        return dict(zip(dates, closes))
    except: return {}

def fetch_fear_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=548&format=json", timeout=10)
        if r.status_code != 200: return {}
        result = {}
        for item in r.json().get("data",[]):
            ts = int(item["timestamp"])
            d = datetime.datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            result[d] = int(item["value"])
        return result
    except: return {}

def rolling_annualized_log_slope(closes, window):
    n = len(closes)
    out = [float("nan")] * n
    t = list(range(window))
    t_mean = sum(t) / window
    Sxx = sum((ti - t_mean)**2 for ti in t)
    for i in range(window-1, n):
        sub = closes[i-window+1:i+1]
        if all(v == sub[0] for v in sub): out[i] = 0.0; continue
        y = [math.log(max(v,1e-12)) for v in sub]
        y_mean = sum(y) / window
        Sxy = sum((t[k]-t_mean)*(y[k]-y_mean) for k in range(window))
        b = Sxy / Sxx
        out[i] = (math.exp(b*252)-1)*100
    return out

def backtest_direction(closes, dates, window):
    """
    驗證斜率方向是否預測正確：
    - 斜率由負轉正 → 統計到下一次轉負前，股價是否確實上漲
    - 斜率由正轉負 → 統計到下一次轉正前，股價是否確實下跌
    回傳每個轉折訊號的統計
    """
    slopes = rolling_annualized_log_slope(closes, window)
    N = len(closes)
    signals = []

    i = 1
    while i < N:
        prev, cur = slopes[i-1], slopes[i]
        if math.isnan(prev) or math.isnan(cur):
            i += 1
            continue

        # 找轉折點
        if prev < 0 and cur > 0:
            sig_type = "負轉正"  # 預測：之後股價應上漲
        elif prev > 0 and cur < 0:
            sig_type = "正轉負"  # 預測：之後股價應下跌
        else:
            i += 1
            continue

        entry_idx = i
        entry_price = closes[i]
        entry_date = dates[i]

        # 找下一個反轉點（或到資料末尾）
        j = i + 1
        while j < N:
            s_cur  = slopes[j]
            s_prev = slopes[j-1]
            if math.isnan(s_cur) or math.isnan(s_prev):
                j += 1
                continue
            if sig_type == "負轉正" and s_prev > 0 and s_cur < 0:
                break  # 找到下一個轉負
            if sig_type == "正轉負" and s_prev < 0 and s_cur > 0:
                break  # 找到下一個轉正
            j += 1

        exit_idx   = min(j, N - 1)
        exit_price = closes[exit_idx]
        exit_date  = dates[exit_idx]
        duration   = exit_idx - entry_idx
        price_chg  = round((exit_price - entry_price) / entry_price * 100, 2)

        # 判斷預測是否正確
        if sig_type == "負轉正":
            correct = price_chg > 0
        else:
            correct = price_chg < 0

        signals.append({
            "type":        sig_type,
            "entry_date":  entry_date,
            "exit_date":   exit_date,
            "entry_price": entry_price,
            "exit_price":  exit_price,
            "duration":    duration,
            "price_chg":   price_chg,
            "correct":     correct,
        })
        i = j  # 跳到下一段

    # 彙總統計
    neg2pos = [s for s in signals if s["type"] == "負轉正"]
    pos2neg = [s for s in signals if s["type"] == "正轉負"]

    def stats(lst):
        if not lst: return {"count":0,"correct_rate":0,"avg_chg":0,"avg_days":0}
        correct = sum(1 for s in lst if s["correct"])
        return {
            "count":        len(lst),
            "correct_rate": round(correct / len(lst) * 100),
            "avg_chg":      round(sum(s["price_chg"] for s in lst) / len(lst), 2),
            "avg_days":     round(sum(s["duration"]  for s in lst) / len(lst), 1),
        }

    return {
        "signals":  signals,
        "neg2pos":  stats(neg2pos),
        "pos2neg":  stats(pos2neg),
        "total":    stats(signals),
    }

def backtest_all_windows(closes, dates):
    """對 2~20 日視窗都跑方向回測"""
    results = {}
    for win in range(2, 21):
        results[win] = backtest_direction(closes, dates, win)
    return results

def check_slope_alerts(window=5):
    end_dt = datetime.datetime.now(tz=timezone.utc)
    start_dt = end_dt - datetime.timedelta(days=30)
    alerts = []
    for ticker in WATCH_TICKERS:
        try:
            _, closes, _ = fetch_yahoo_range(ticker, start_dt, end_dt)
            if len(closes) < window+2: continue
            slopes = rolling_annualized_log_slope(closes, window)
            valid = [(i,s) for i,s in enumerate(slopes) if not math.isnan(s)]
            if len(valid) < 2: continue
            prev_slope = valid[-2][1]
            last_slope = valid[-1][1]
            if prev_slope < 0 and last_slope > 0:
                alerts.append(f"📈 {ticker} 斜率由負轉正！\n前一日：{prev_slope:.1f}%\n今日：{last_slope:.1f}%\n現價：${closes[-1]:.2f}")
        except Exception as e:
            print(f"檢查 {ticker} 失敗：{e}")
    if alerts:
        send_line("【股票斜率提醒】\n\n" + "\n\n".join(alerts))
    else:
        print(f"[{datetime.datetime.now()}] 無斜率轉正信號")

def scheduler_loop():
    while True:
        now_utc = datetime.datetime.now(tz=timezone.utc)
        target = now_utc.replace(hour=14, minute=0, second=0, microsecond=0)
        if now_utc >= target:
            target += datetime.timedelta(days=1)
        wait_sec = (target - now_utc).total_seconds()
        print(f"下次檢查：等待 {wait_sec/3600:.1f} 小時")
        threading.Event().wait(wait_sec)
        check_slope_alerts()

# ── Flask + Dash ─────────────────────────────────────────────
server = flask.Flask(__name__)

@server.route("/webhook", methods=["POST"])
def webhook():
    body = flask.request.get_json(silent=True) or {}
    for event in body.get("events", []):
        if event.get("type") == "message":
            user_id = event["source"]["userId"]
            send_line_to(user_id, f"你的 User ID 是：\n{user_id}\n\n請把這串 ID 填入程式的 LINE_USER_ID。")
    return "OK", 200

app = dash.Dash(__name__, server=server)
app.title = "股票滾動年化斜率"

TAB_STYLE = {"padding":"8px 20px","fontSize":"14px","fontFamily":"sans-serif",
             "border":"0.5px solid #ddd","borderBottom":"none","background":"#f5f5f3",
             "cursor":"pointer","borderRadius":"6px 6px 0 0"}
TAB_SEL   = {**TAB_STYLE,"background":"white","fontWeight":"500","borderBottom":"1px solid white"}

app.layout = html.Div([
    html.H2("股票收盤價 × 滾動年化斜率 × 恐懼指標",
            style={"fontFamily":"sans-serif","marginBottom":"4px"}),
    html.P("資料從今天往回抓，可用滑桿調整區間（最長 1.5 年）｜每天 22:00 自動檢查斜率轉正並發 LINE 通知",
           style={"fontFamily":"sans-serif","color":"#888","marginBottom":"16px"}),

    html.Div([
        html.Div([
            html.Label("股票代號（逗號分隔）",style={"fontSize":"12px","color":"#888"}),
            dcc.Input(id="ticker",value="QQQ, VOO, TSM",type="text",
                      style={"width":"300px","textTransform":"uppercase","padding":"6px 8px",
                             "borderRadius":"6px","border":"1px solid #ddd","fontSize":"14px"}),
        ],style={"display":"flex","flexDirection":"column","gap":"4px"}),
        html.Div([
            html.Label("斜率（天）",style={"fontSize":"12px","color":"#888"}),
            dcc.Input(id="window",value=5,type="number",min=2,max=60,
                      style={"width":"70px","padding":"6px 8px","borderRadius":"6px",
                             "border":"1px solid #ddd","fontSize":"14px"}),
        ],style={"display":"flex","flexDirection":"column","gap":"4px"}),
        html.Div([
            html.Label("顯示選項",style={"fontSize":"12px","color":"#888"}),
            dcc.Checklist(id="show-volume",
                          options=[{"label":"　顯示成交量","value":"vol"}],
                          value=["vol"],
                          style={"fontSize":"14px","color":"#333","paddingTop":"6px"}),
        ],style={"display":"flex","flexDirection":"column","gap":"4px"}),
        html.Button("更新",id="run-btn",n_clicks=0,
                    style={"alignSelf":"flex-end","padding":"8px 20px","background":"#1a1a1a",
                           "color":"#fff","border":"none","borderRadius":"6px","cursor":"pointer","fontSize":"14px"}),
        html.Button("立即測試 LINE 通知",id="test-btn",n_clicks=0,
                    style={"alignSelf":"flex-end","padding":"8px 16px","background":"#06c755",
                           "color":"#fff","border":"none","borderRadius":"6px","cursor":"pointer","fontSize":"13px"}),
        html.Button("發送目前斜率",id="slope-btn",n_clicks=0,
                    style={"alignSelf":"flex-end","padding":"8px 16px","background":"#2563eb",
                           "color":"#fff","border":"none","borderRadius":"6px","cursor":"pointer","fontSize":"13px"}),
    ],style={"display":"flex","flexWrap":"wrap","gap":"16px","alignItems":"flex-end",
             "background":"#f5f5f3","borderRadius":"10px","padding":"14px 16px","marginBottom":"12px"}),

    html.Div(id="test-msg",style={"fontSize":"13px","color":"#06c755","minHeight":"20px","marginBottom":"2px","fontFamily":"sans-serif"}),
    html.Div(id="slope-msg",style={"fontSize":"13px","color":"#2563eb","minHeight":"20px","marginBottom":"4px","fontFamily":"sans-serif"}),

    html.Div([
        html.Label(id="slider-label",style={"fontSize":"12px","color":"#888","marginBottom":"6px","display":"block"}),
        dcc.Slider(id="days-slider",min=30,max=548,step=30,value=365,
                   marks={30:"1個月",90:"3個月",180:"6個月",365:"1年",548:"1.5年"},
                   tooltip={"placement":"bottom","always_visible":False}),
    ],style={"background":"#f5f5f3","borderRadius":"10px","padding":"14px 20px 18px","marginBottom":"16px"}),

    html.Div(id="status-msg",style={"fontSize":"13px","color":"#888","minHeight":"20px","marginBottom":"8px","fontFamily":"sans-serif"}),

    dcc.Tabs(id="tabs", value="tab-chart", children=[
        dcc.Tab(label="📈 股價圖表", value="tab-chart", style=TAB_STYLE, selected_style=TAB_SEL),
        dcc.Tab(label="🔬 回測分析", value="tab-backtest", style=TAB_STYLE, selected_style=TAB_SEL),
    ], style={"marginBottom":"0"}),

    html.Div(id="tab-content"),

    html.H3("恐懼指標",style={"fontFamily":"sans-serif","marginTop":"24px","marginBottom":"4px","fontSize":"16px"}),
    html.P(id="fear-status",style={"fontSize":"12px","color":"#aaa","marginBottom":"8px","fontFamily":"sans-serif"}),
    html.Div(id="fear-charts"),
    html.P("VIX >30 為高恐懼區。Fear & Greed Index：0=極度恐懼，100=極度貪婪（加密市場版）。",
           style={"fontSize":"12px","color":"#aaa","marginTop":"12px","fontFamily":"sans-serif"}),

],style={"maxWidth":"1000px","margin":"2rem auto","padding":"0 1.5rem","fontFamily":"sans-serif"})

# ── Callbacks ────────────────────────────────────────────────

@app.callback(Output("slider-label","children"), Input("days-slider","value"))
def update_slider_label(days):
    end_dt = datetime.datetime.now(tz=timezone.utc)
    start_dt = end_dt - datetime.timedelta(days=days)
    return f"資料區間：{start_dt.strftime('%Y-%m-%d')} ～ {end_dt.strftime('%Y-%m-%d')}　（{days} 天）"

@app.callback(Output("test-msg","children"), Input("test-btn","n_clicks"), prevent_initial_call=True)
def test_line(n):
    if not LINE_USER_ID:
        return "❌ 尚未設定 User ID"
    send_line("【測試】股票斜率提醒系統運作正常 ✅\n每天 22:00 會自動檢查 QQQ、VOO、TSM 斜率。")
    return "✅ 測試訊息已發送，請查看 LINE"

@app.callback(Output("slope-msg","children"), Input("slope-btn","n_clicks"), prevent_initial_call=True)
def send_current_slope(n):
    if not LINE_USER_ID:
        return "❌ 尚未設定 User ID"
    end_dt   = datetime.datetime.now(tz=timezone.utc)
    start_dt = end_dt - datetime.timedelta(days=30)
    window   = 5
    tw_time  = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    blocks   = [f"【目前斜率報告】\n{tw_time} 台灣時間"]
    vix_val  = None
    try:
        vd = fetch_vix(start_dt, end_dt)
        if vd: vix_val = vd[sorted(vd.keys())[-1]]
    except: pass
    for ticker in WATCH_TICKERS:
        try:
            _, closes, _ = fetch_yahoo_range(ticker, start_dt, end_dt)
            slopes = rolling_annualized_log_slope(closes, window)
            valid  = [s for s in slopes if not math.isnan(s)]
            if len(valid) < 2:
                blocks.append(f"⚪ {ticker}：資料不足"); continue
            prev_slope, last_slope = valid[-2], valid[-1]
            price = closes[-1]
            if prev_slope < 0 and last_slope > 0:
                block = (f"╔══════════════════╗\n⚡ {ticker} 動能反轉！\n"
                         f"斜率：{prev_slope:.1f}% → +{last_slope:.1f}%\n現價：${price:.2f}\n👉 可考慮買入\n╚══════════════════╝")
            elif prev_slope > 0 and last_slope < 0:
                block = (f"╔══════════════════╗\n🔻 {ticker} 動能反轉↓\n"
                         f"斜率：+{prev_slope:.1f}% → {last_slope:.1f}%\n現價：${price:.2f}\n👉 可考慮減倉\n╚══════════════════╝")
            elif last_slope > 20:
                block = f"📈 {ticker}｜斜率 +{last_slope:.1f}%｜${price:.2f}\n漲勢強勁，持有或加碼"
            elif last_slope > 0:
                block = f"📈 {ticker}｜斜率 +{last_slope:.1f}%｜${price:.2f}\n動能向上，注意變化"
            elif last_slope > -20:
                block = f"📉 {ticker}｜斜率 {last_slope:.1f}%｜${price:.2f}\n動能偏弱，建議觀望"
            else:
                block = f"📉 {ticker}｜斜率 {last_slope:.1f}%｜${price:.2f}\n下跌趨勢，空手觀望"
            blocks.append(block)
        except Exception as e:
            blocks.append(f"⚪ {ticker}：錯誤 {e}")
    if vix_val is not None:
        vix_emoji = "🔴" if vix_val > 30 else "🟡" if vix_val > 20 else "🟢"
        vix_label = "高恐懼" if vix_val > 30 else "中性" if vix_val > 20 else "低恐懼"
        blocks.append(f"────────────────\n{vix_emoji} VIX {vix_val:.1f}｜{vix_label}\n────────────────")
    send_line("\n\n".join(blocks))
    return "✅ 斜率報告已發送到 LINE"


@app.callback(
    Output("tab-content","children"),
    Output("fear-charts","children"),
    Output("status-msg","children"),
    Output("fear-status","children"),
    Input("run-btn","n_clicks"),
    Input("tabs","value"),
    State("ticker","value"), State("window","value"),
    State("show-volume","value"), State("days-slider","value"),
    prevent_initial_call=False,
)
def update_content(n_clicks, tab, ticker_str, window, show_volume, days):
    tickers  = [t.strip().upper() for t in (ticker_str or "QQQ").split(",") if t.strip()]
    window   = int(window or 5)
    show_vol = "vol" in (show_volume or [])
    days     = int(days or 365)
    end_dt   = datetime.datetime.now(tz=timezone.utc)
    start_dt = end_dt - datetime.timedelta(days=days)
    date_range_str = f"{start_dt.strftime('%Y-%m-%d')} ～ {end_dt.strftime('%Y-%m-%d')}"

    ticker_data = {}
    messages = []
    for i, ticker in enumerate(tickers[:12]):
        try:
            dates, closes, volumes = fetch_yahoo_range(ticker, start_dt, end_dt)
            ticker_data[ticker] = {"dates":dates,"closes":closes,"volumes":volumes,"color":COLORS[i%len(COLORS)]}
            messages.append(f"✅ {ticker} {len(dates)}日")
        except Exception as e:
            messages.append(f"❌ {ticker}: {e}")
            ticker_data[ticker] = None

    # ── 頁籤一：股價圖表 ──
    if tab == "tab-chart":
        chart_divs = []
        for ticker in tickers[:12]:
            d = ticker_data.get(ticker)
            if d is None:
                chart_divs.append(html.Div(f"❌ {ticker}：資料錯誤",
                    style={"padding":"12px","color":"#dc2626","fontSize":"13px",
                           "background":"#fff5f5","borderRadius":"8px","marginBottom":"12px"}))
                continue
            dates, closes, volumes, color = d["dates"], d["closes"], d["volumes"], d["color"]
            slopes = rolling_annualized_log_slope(closes, window)
            slope_line = [None if math.isnan(v) else v for v in slopes]
            pos_s = [v if (v is not None and v > 0) else 0 for v in slope_line]
            neg_s = [v if (v is not None and v < 0) else 0 for v in slope_line]
            vol_colors = []
            for k in range(len(closes)):
                if k == 0: vol_colors.append("rgba(150,150,150,0.5)")
                elif closes[k] >= closes[k-1]: vol_colors.append("rgba(34,160,107,0.5)")
                else: vol_colors.append("rgba(226,72,61,0.5)")
            if show_vol:
                fig = make_subplots(rows=2,cols=1,shared_xaxes=True,row_heights=[0.68,0.32],
                                    vertical_spacing=0.04,specs=[[{"secondary_y":True}],[{"secondary_y":False}]])
                chart_height = 420
            else:
                fig = make_subplots(rows=1,cols=1,specs=[[{"secondary_y":True}]])
                chart_height = 300
            fig.add_trace(go.Bar(x=dates,y=pos_s,name="上升",marker_color="rgba(34,160,107,0.35)",showlegend=False,
                hovertemplate="%{x}<br>斜率: %{y:.1f}%<extra></extra>"),row=1,col=1,secondary_y=False)
            fig.add_trace(go.Bar(x=dates,y=neg_s,name="下跌",marker_color="rgba(226,72,61,0.35)",showlegend=False,
                hovertemplate="%{x}<br>斜率: %{y:.1f}%<extra></extra>"),row=1,col=1,secondary_y=False)
            fig.add_trace(go.Scatter(x=dates,y=slope_line,name=f"{window}日年化斜率",mode="lines",
                line=dict(color="#444",width=1.2),hovertemplate="%{x}<br>斜率: %{y:.2f}%<extra></extra>"),
                row=1,col=1,secondary_y=False)
            fig.add_trace(go.Scatter(x=dates,y=closes,name="收盤價",mode="lines",
                line=dict(color=color,width=2),hovertemplate="%{x}<br>收盤: $%{y:.2f}<extra></extra>"),
                row=1,col=1,secondary_y=True)
            if show_vol:
                fig.add_trace(go.Bar(x=dates,y=volumes,name="成交量",marker_color=vol_colors,
                    hovertemplate="%{x}<br>成交量: %{y:,.0f}<extra></extra>"),row=2,col=1)
            fig.add_hline(y=0,line_color="#ccc",line_width=1,row=1,col=1)
            fig.update_layout(
                title=dict(text=f"<b>{ticker}</b>　{window}日斜率　{date_range_str}",
                           font=dict(size=13,color="#1a1a1a"),x=0),
                barmode="overlay",bargap=0,hovermode="x unified",
                legend=dict(orientation="h",yanchor="bottom",y=1.06,xanchor="right",x=1,font=dict(size=11)),
                plot_bgcolor="white",paper_bgcolor="white",
                margin=dict(l=60,r=65,t=50,b=40),
                font=dict(family="sans-serif",size=12),height=chart_height)
            fig.update_xaxes(showgrid=True,gridcolor="#eee")
            fig.update_yaxes(title_text="年化斜率(%)",secondary_y=False,showgrid=True,gridcolor="#eee",
                             zeroline=False,title_font=dict(size=10),row=1,col=1)
            fig.update_yaxes(title_text="收盤價(USD)",secondary_y=True,showgrid=False,zeroline=False,
                             title_font=dict(size=10,color=color),tickfont=dict(color=color),row=1,col=1)
            if show_vol:
                fig.update_yaxes(title_text="成交量",showgrid=True,gridcolor="#eee",zeroline=False,
                                 title_font=dict(size=10),tickformat=".2s",row=2,col=1)
            chart_divs.append(html.Div(dcc.Graph(figure=fig,config={"displayModeBar":False}),
                style={"marginBottom":"16px","border":"0.5px solid #e5e5e5",
                       "borderRadius":"10px","overflow":"hidden","background":"white"}))
        content = html.Div(chart_divs)

    # ── 頁籤二：回測分析 ──
    else:
        backtest_divs = [
            html.P("驗證邏輯：斜率由負轉正後，統計到下一次轉負前股價是否上漲；斜率由正轉負後，統計到下一次轉正前股價是否下跌。視窗 2–20 天全部比較。",
                   style={"fontSize":"13px","color":"#666","marginBottom":"16px",
                          "background":"#f5f5f3","padding":"10px 14px","borderRadius":"8px"}),
        ]

        for ticker in tickers[:12]:
            d = ticker_data.get(ticker)
            if d is None:
                backtest_divs.append(html.Div(f"❌ {ticker}：資料不足",
                    style={"padding":"10px","color":"#dc2626","fontSize":"13px",
                           "background":"#fff5f5","borderRadius":"8px","marginBottom":"12px"}))
                continue

            dates, closes, color = d["dates"], d["closes"], d["color"]
            all_results = backtest_all_windows(closes, dates)

            # 找最佳視窗（負轉正正確率最高）
            best_win = max(
                (w for w in all_results if all_results[w]["neg2pos"]["count"] > 0),
                key=lambda w: (all_results[w]["neg2pos"]["correct_rate"],
                               all_results[w]["neg2pos"]["avg_chg"]),
                default=5
            )

            # 長條圖：各視窗正確率
            win_list   = list(range(2, 21))
            n2p_rates  = [all_results[w]["neg2pos"]["correct_rate"] for w in win_list]
            p2n_rates  = [all_results[w]["pos2neg"]["correct_rate"] for w in win_list]
            n2p_chg    = [all_results[w]["neg2pos"]["avg_chg"] for w in win_list]
            p2n_chg    = [all_results[w]["pos2neg"]["avg_chg"] for w in win_list]
            win_labels = [f"{w}日" for w in win_list]
            bar_colors = [color if w == best_win else "rgba(150,150,150,0.3)" for w in win_list]

            fig_bt = go.Figure()
            fig_bt.add_trace(go.Bar(x=win_labels, y=n2p_rates, name="負轉正正確率",
                marker_color=bar_colors,
                hovertemplate="視窗%{x}<br>負轉正正確率: %{y}%<extra></extra>"))
            fig_bt.add_trace(go.Scatter(x=win_labels, y=p2n_rates, name="正轉負正確率",
                line=dict(color="rgba(226,75,74,0.7)", width=2, dash="dot"), mode="lines+markers",
                marker=dict(size=4),
                hovertemplate="視窗%{x}<br>正轉負正確率: %{y}%<extra></extra>"))
            fig_bt.add_hline(y=50, line_color="#ccc", line_dash="dash", line_width=1,
                             annotation_text="50%隨機線", annotation_position="right")

            best_n2p = all_results[best_win]["neg2pos"]
            fig_bt.update_layout(
                title=dict(
                    text=(f"<b>{ticker}</b>　方向預測正確率｜"
                          f"最佳視窗：{best_win}天（負轉正正確率 {best_n2p['correct_rate']}%，"
                          f"平均漲幅 {'+' if best_n2p['avg_chg']>=0 else ''}{best_n2p['avg_chg']}%，"
                          f"平均持續 {best_n2p['avg_days']} 天）"),
                    font=dict(size=12, color="#1a1a1a"), x=0),
                barmode="overlay", hovermode="x unified",
                legend=dict(orientation="h",yanchor="bottom",y=1.06,xanchor="right",x=1,font=dict(size=11)),
                plot_bgcolor="white", paper_bgcolor="white",
                margin=dict(l=55,r=20,t=60,b=40),
                font=dict(family="sans-serif",size=12), height=300,
                yaxis=dict(title="正確率(%)",showgrid=True,gridcolor="#eee",range=[0,105]),
                xaxis=dict(showgrid=True,gridcolor="#eee"),
            )

            # 最佳視窗的訊號明細
            best_signals = all_results[best_win]["signals"]
            sig_rows = []
            for s in best_signals:
                chg_color = "#0F6E56" if s["price_chg"] >= 0 else "#A32D2D"
                ok_text   = "✅ 正確" if s["correct"] else "❌ 錯誤"
                ok_color  = "#0F6E56" if s["correct"] else "#A32D2D"
                sig_rows.append(html.Tr([
                    html.Td(s["type"],
                            style={"padding":"4px 8px",
                                   "color":"#0F6E56" if s["type"]=="負轉正" else "#A32D2D",
                                   "fontWeight":"500"}),
                    html.Td(s["entry_date"],style={"padding":"4px 8px"}),
                    html.Td(s["exit_date"], style={"padding":"4px 8px"}),
                    html.Td(f"{s['duration']} 天",style={"padding":"4px 8px"}),
                    html.Td(f"${s['entry_price']:.2f}",style={"padding":"4px 8px"}),
                    html.Td(f"${s['exit_price']:.2f}", style={"padding":"4px 8px"}),
                    html.Td(f"{'+' if s['price_chg']>=0 else ''}{s['price_chg']}%",
                            style={"padding":"4px 8px","color":chg_color,"fontWeight":"500"}),
                    html.Td(ok_text, style={"padding":"4px 8px","color":ok_color,"fontWeight":"500"}),
                ]))

            th_style = {"padding":"4px 8px","textAlign":"left","fontSize":"12px",
                        "color":"#888","borderBottom":"0.5px solid #eee"}
            detail_table = html.Div([
                html.P(f"最佳視窗（{best_win}天）訊號明細：",
                       style={"fontSize":"12px","color":"#888","margin":"8px 0 4px","paddingLeft":"12px"}),
                html.Div(html.Table([
                    html.Thead(html.Tr([
                        html.Th("訊號類型",style=th_style),
                        html.Th("進場日",style=th_style),
                        html.Th("出場日",style=th_style),
                        html.Th("持續天數",style=th_style),
                        html.Th("進場價",style=th_style),
                        html.Th("出場價",style=th_style),
                        html.Th("股價變化",style=th_style),
                        html.Th("預測",style=th_style),
                    ])),
                    html.Tbody(sig_rows),
                ],style={"width":"100%","borderCollapse":"collapse","fontSize":"12px"}),
                style={"overflowX":"auto","paddingBottom":"8px"}),
            ])

            backtest_divs.append(html.Div([
                dcc.Graph(figure=fig_bt, config={"displayModeBar":False}),
                detail_table,
            ],style={"marginBottom":"20px","border":"0.5px solid #e5e5e5",
                     "borderRadius":"10px","overflow":"hidden","background":"white","paddingBottom":"12px"}))

        content = html.Div(backtest_divs)

    # ── 恐懼指標 ──
    vix_data = fetch_vix(start_dt, end_dt)
    fng_data = fetch_fear_greed()
    fng_data = {d:v for d,v in fng_data.items()
                if start_dt.strftime("%Y-%m-%d") <= d <= end_dt.strftime("%Y-%m-%d")}
    fear_notes, fear_divs = [], []
    has_vix, has_fng = bool(vix_data), bool(fng_data)
    if has_vix or has_fng:
        fear_fig = make_subplots(specs=[[{"secondary_y":True}]])
        if has_vix:
            vx = sorted(vix_data.keys()); vy = [vix_data[d] for d in vx]
            fear_fig.add_trace(go.Scatter(x=vx,y=vy,name="VIX",mode="lines",
                line=dict(color="rgb(220,72,61)",width=1.8),fill="tozeroy",fillcolor="rgba(220,72,61,0.12)",
                hovertemplate="%{x}<br>VIX: %{y:.2f}<extra></extra>"),secondary_y=False)
            fear_fig.add_hline(y=30,line_color="rgba(220,72,61,0.4)",line_dash="dash",line_width=1,
                               annotation_text="高恐懼 30",annotation_position="right",secondary_y=False)
            fear_notes.append("✅ VIX")
        else:
            fear_notes.append("❌ VIX 無資料")
        if has_fng:
            fg = sorted(fng_data.keys()); fv = [fng_data[d] for d in fg]
            fear_fig.add_trace(go.Scatter(x=fg,y=fv,name="Fear & Greed",mode="lines",
                line=dict(color="rgb(214,140,0)",width=1.8),
                hovertemplate="%{x}<br>F&G: %{y:.0f}<extra></extra>"),secondary_y=True)
            fear_fig.add_hline(y=50,line_color="rgba(214,140,0,0.4)",line_dash="dash",line_width=1,
                               annotation_text="中性 50",annotation_position="right",secondary_y=True)
            fear_notes.append("✅ Fear & Greed")
        else:
            fear_notes.append("❌ Fear & Greed 無資料")
        fear_fig.update_layout(
            title=dict(text=f"<b>VIX & Fear and Greed Index</b>　{date_range_str}",
                       font=dict(size=13,color="#1a1a1a"),x=0),
            plot_bgcolor="white",paper_bgcolor="white",hovermode="x unified",
            legend=dict(orientation="h",yanchor="bottom",y=1.06,xanchor="left",x=0,font=dict(size=11)),
            margin=dict(l=55,r=65,t=50,b=40),xaxis=dict(showgrid=True,gridcolor="#eee"),
            font=dict(family="sans-serif",size=12),height=280)
        fear_fig.update_yaxes(title_text="VIX",secondary_y=False,showgrid=True,gridcolor="#eee",
                              zeroline=False,title_font=dict(size=11,color="rgb(220,72,61)"),
                              tickfont=dict(color="rgb(220,72,61)"))
        fear_fig.update_yaxes(title_text="Fear & Greed（0–100）",secondary_y=True,showgrid=False,
                              zeroline=False,range=[0,100],title_font=dict(size=11,color="rgb(214,140,0)"),
                              tickfont=dict(color="rgb(214,140,0)"))
        fear_divs.append(html.Div(dcc.Graph(figure=fear_fig,config={"displayModeBar":False}),
            style={"marginBottom":"12px","border":"0.5px solid #e5e5e5",
                   "borderRadius":"10px","overflow":"hidden","background":"white"}))

    return content, fear_divs, "　".join(messages), "　".join(fear_notes)


if __name__ == "__main__":
    t = threading.Thread(target=scheduler_loop, daemon=True)
    t.start()
    print("啟動中，請用瀏覽器開啟 http://localhost:8050")
    app.run(debug=False, host="0.0.0.0", port=8050)
