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
from dash import dcc, html, Input, Output, State, callback_context

LINE_TOKEN   = os.environ.get("LINE_TOKEN", "BL02SzdP0SeOiz4iRC+fqU8X9hp+zmcejR4i9WGYNg9TFCM/i97k1M8vm8Hki5fM2CWuFEKQlF4vlMnNkVDV+YKVNxtSJxXIl0AYZ8xUVmLmJ6Cyd6qw8iCBY6VekjwyFbrF/ocFfRUymRkkiw9UMQdB04t89/1O/w1cDnyilFU=")
LINE_USER_ID  = os.environ.get("LINE_USER_ID",  "U2af0aa14205601e29e61d548c2f10f5a")
LINE_USER_ID2 = os.environ.get("LINE_USER_ID2", "Ue87afa7142e414833eb570321cea7972")
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
    if LINE_USER_ID2:
        send_line_to(LINE_USER_ID2, message)

def fetch_yahoo_range(ticker, start_dt, end_dt, interval="1d"):
    """
    interval: "1d"=每日, "1h"=每小時, "1m"=每分鐘
    注意：Yahoo Finance 限制：
      - 1m 只能抓最近 7 天
      - 1h 只能抓最近 730 天
    """
    start = int(start_dt.timestamp())
    end   = int(end_dt.timestamp())
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval={interval}&period1={start}&period2={end}"
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
    opens_raw  = quote.get("open",[])
    volumes_raw = quote.get("volume",[])
    dates, closes, opens, volumes = [], [], [], []
    for ts, c, o, v in zip(timestamps, closes_raw, opens_raw, volumes_raw):
        if c is None: continue
        if interval in ("1h", "1m"):
            fmt = "%Y-%m-%d %H:%M"
        else:
            fmt = "%Y-%m-%d"
        dates.append(datetime.datetime.fromtimestamp(ts, tz=timezone.utc)
                     .astimezone(datetime.timezone(datetime.timedelta(hours=8)))
                     .strftime(fmt))
        closes.append(float(c))
        opens.append(float(o) if o is not None else float(c))
        volumes.append(float(v) if v is not None else 0.0)
    if not dates: raise RuntimeError("無資料")
    return dates, closes, volumes, opens

def fetch_vix(start_dt, end_dt):
    try:
        dates, closes, _, _o = fetch_yahoo_range("%5EVIX", start_dt, end_dt)
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

def rolling_annualized_log_slope_safe(closes, window, annualize=252):
    """與 rolling_annualized_log_slope 相同，但跳過含 nan 的視窗"""
    n = len(closes)
    out = [float("nan")] * n
    t = list(range(window))
    t_mean = sum(t) / window
    Sxx = sum((ti - t_mean)**2 for ti in t)
    for i in range(window-1, n):
        sub = closes[i-window+1:i+1]
        if any(math.isnan(v) for v in sub): continue
        if all(v == sub[0] for v in sub): out[i] = 0.0; continue
        y = [math.log(max(v, 1e-12)) for v in sub]
        y_mean = sum(y) / window
        Sxy = sum((t[k]-t_mean)*(y[k]-y_mean) for k in range(window))
        b = Sxy / Sxx
        out[i] = (math.exp(b*annualize)-1)*100
    return out

def rolling_annualized_log_slope(closes, window, annualize=252):
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
        out[i] = (math.exp(b*annualize)-1)*100
    return out

def backtest_direction(closes, dates, window, annualize=252, opens=None):
    """
    驗證斜率方向是否預測正確：
    - 斜率由負轉正 → 用隔天開盤價買入，統計到下一次轉負（隔天開盤賣出）
    - 斜率由正轉負 → 用隔天開盤價賣出，統計到下一次轉正（隔天開盤買入）
    若 opens 為 None 則退回用收盤價
    """
    slopes = rolling_annualized_log_slope(closes, window, annualize)
    N = len(closes)
    signals = []

    i = 1
    while i < N:
        prev, cur = slopes[i-1], slopes[i]
        if math.isnan(prev) or math.isnan(cur):
            i += 1
            continue

        if prev < 0 and cur > 0:
            sig_type = "負轉正"
        elif prev > 0 and cur < 0:
            sig_type = "正轉負"
        else:
            i += 1
            continue

        # 進場：隔天開盤價（i+1）；若沒有隔天則用當天收盤
        entry_idx = i
        signal_date = dates[i]
        if opens is not None and i + 1 < N:
            entry_price = opens[i + 1]
            entry_date  = dates[i + 1]
        else:
            entry_price = closes[i]
            entry_date  = dates[i]

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

        # 出場：反轉那天的隔天開盤（j+1）；若沒有則用收盤
        raw_exit_idx = min(j, N - 1)
        if opens is not None and raw_exit_idx + 1 < N:
            exit_price = opens[raw_exit_idx + 1]
            exit_date  = dates[raw_exit_idx + 1]
            exit_idx   = raw_exit_idx + 1
        else:
            exit_price = closes[raw_exit_idx]
            exit_date  = dates[raw_exit_idx]
            exit_idx   = raw_exit_idx
        duration   = exit_idx - entry_idx
        price_chg  = round((exit_price - entry_price) / entry_price * 100, 2)

        # 出場當日斜率（轉折那天）
        exit_slope      = round(slopes[exit_idx], 1) if exit_idx < N and not math.isnan(slopes[exit_idx]) else 0.0
        exit_prev_slope = round(slopes[exit_idx-1], 1) if exit_idx > 0 and not math.isnan(slopes[exit_idx-1]) else 0.0

        # 判斷預測是否正確
        if sig_type == "負轉正":
            correct = price_chg > 0
        else:
            correct = price_chg < 0

        signals.append({
            "type":             sig_type,
            "entry_date":       entry_date,
            "exit_date":        exit_date,
            "entry_price":      entry_price,
            "exit_price":       exit_price,
            "duration":         duration,
            "price_chg":        price_chg,
            "correct":          correct,
            "prev_slope":       round(prev, 1),
            "cur_slope":        round(cur, 1),
            "exit_prev_slope":  exit_prev_slope,
            "exit_slope":       exit_slope,
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

def backtest_all_windows(closes, dates, annualize=252, opens=None):
    """對 2~20 視窗都跑方向回測"""
    results = {}
    for win in range(2, 21):
        results[win] = backtest_direction(closes, dates, win, annualize, opens)
    return results

def simulate_trading(signals, logic, capital, min_duration, slope_threshold=0):
    """
    slope_threshold:
      - 負轉正：當日斜率 >= +threshold 才買入
      - 正轉負：當日斜率 <= -threshold 才賣出/做空
      - 0 = 不過濾
    """
    cash = float(capital)
    trades = []
    equity_curve = []
    peak = cash
    max_dd = 0.0
    idle_days = 0
    prev_exit_date = signals[0]["entry_date"] if signals else None
    holding = False  # 只做多時追蹤是否持倉中

    for sig in signals:
        dur = sig["duration"]
        if dur < min_duration:
            continue

        cur_slope  = sig.get("cur_slope", 0)
        sig_type   = sig["type"]
        is_n2p     = "負轉正" in sig_type  # 負轉正
        is_p2n     = "正轉負" in sig_type  # 正轉負

        # 斜率門檻過濾
        if slope_threshold > 0:
            if is_n2p and cur_slope < slope_threshold:
                continue   # 轉正但力道不夠，不買入
            if is_p2n and cur_slope > -slope_threshold:
                continue   # 轉負但力道不夠，不賣出（繼續持有）

        try:
            gap = (datetime.datetime.strptime(sig["entry_date"], "%Y-%m-%d") -
                   datetime.datetime.strptime(prev_exit_date, "%Y-%m-%d")).days
            idle_days += max(0, gap)
        except: pass

        chg = sig["price_chg"] / 100.0

        if logic == "long":
            if is_n2p:
                ret = chg
                holding = True
            else:
                # 正轉負：賣出空倉
                equity_curve.append({"date": sig["entry_date"], "val": cash})
                equity_curve.append({"date": sig["exit_date"],  "val": cash})
                prev_exit_date = sig["exit_date"]
                holding = False
                continue
        else:
            # 多空都做
            ret = chg if is_n2p else -chg

        prev_cash = cash
        cash = cash * (1 + ret)
        peak = max(peak, cash)
        dd = (peak - cash) / peak * 100
        max_dd = max(max_dd, dd)
        trades.append({"date": sig["entry_date"], "ret": ret * 100, "win": ret > 0})
        equity_curve.append({"date": sig["entry_date"], "val": prev_cash})
        equity_curve.append({"date": sig["exit_date"],  "val": cash})
        prev_exit_date = sig["exit_date"]

    wins = sum(1 for t in trades if t["win"])
    wr = round(wins / len(trades) * 100) if trades else 0
    return {
        "final_val":   round(cash, 2),
        "total_ret":   round((cash - capital) / capital * 100, 2),
        "trade_count": len(trades),
        "win_rate":    wr,
        "max_dd":      round(max_dd, 1),
        "idle_days":   idle_days,
        "equity":      equity_curve,
    }

def sma(closes, w):
    out = [None] * len(closes)
    for i in range(w-1, len(closes)):
        out[i] = sum(closes[i-w+1:i+1]) / w
    return out

def backtest_dual_confirmation(closes, dates, price_win, ma_win, ma_period):
    """
    雙重確認模式：
    - 收盤價斜率先轉正 AND MA斜率也為正 → 買入訊號（負轉正確認）
    - 收盤價斜率先轉負 AND MA斜率也為負 → 賣出訊號（正轉負確認）
    統計到下一次反轉前股價方向是否正確
    """
    price_slopes = rolling_annualized_log_slope(closes, price_win)
    ma_vals      = sma(closes, ma_period)
    # 對 MA 值算斜率，需要 ma_vals 中非 None 的部分
    ma_closes_filled = [v if v is not None else 0.0 for v in ma_vals]
    ma_slopes        = rolling_annualized_log_slope(ma_closes_filled, ma_win)
    # 把 MA 值為 None 的位置的 ma_slopes 設為 nan
    for i in range(len(ma_vals)):
        if ma_vals[i] is None:
            ma_slopes[i] = float("nan")

    N = len(closes)
    signals = []
    i = 1
    while i < N:
        ps_prev = price_slopes[i-1]; ps_cur = price_slopes[i]
        ms_cur  = ma_slopes[i]
        if math.isnan(ps_prev) or math.isnan(ps_cur) or math.isnan(ms_cur):
            i += 1; continue

        # 收盤斜率剛轉正 + MA斜率也為正 = 雙重確認買入
        if ps_prev < 0 and ps_cur > 0 and ms_cur > 0:
            sig_type = "負轉正（雙確認）"
        # 收盤斜率剛轉負 + MA斜率也為負 = 雙重確認賣出
        elif ps_prev > 0 and ps_cur < 0 and ms_cur < 0:
            sig_type = "正轉負（雙確認）"
        else:
            i += 1; continue

        entry_idx   = i
        entry_price = closes[i]
        entry_date  = dates[i]

        # 找下一個反轉點
        j = i + 1
        while j < N:
            ps2 = price_slopes[j]; ps2p = price_slopes[j-1]
            if math.isnan(ps2) or math.isnan(ps2p):
                j += 1; continue
            if "負轉正" in sig_type and ps2p > 0 and ps2 < 0:
                break
            if "正轉負" in sig_type and ps2p < 0 and ps2 > 0:
                break
            j += 1

        exit_idx   = min(j, N-1)
        exit_price = closes[exit_idx]
        exit_date  = dates[exit_idx]
        duration   = exit_idx - entry_idx
        price_chg  = round((exit_price - entry_price) / entry_price * 100, 2)
        correct    = price_chg > 0 if "負轉正" in sig_type else price_chg < 0

        signals.append({
            "type": sig_type, "entry_date": entry_date, "exit_date": exit_date,
            "entry_price": entry_price, "exit_price": exit_price,
            "duration": duration, "price_chg": price_chg, "correct": correct,
            "prev_slope": round(ps_prev, 1), "cur_slope": round(ps_cur, 1),
        })
        i = j

    neg2pos = [s for s in signals if "負轉正" in s["type"]]
    pos2neg = [s for s in signals if "正轉負" in s["type"]]

    def stats(lst):
        if not lst: return {"count":0,"correct_rate":0,"avg_chg":0,"avg_days":0}
        correct = sum(1 for s in lst if s["correct"])
        return {
            "count":        len(lst),
            "correct_rate": round(correct / len(lst) * 100),
            "avg_chg":      round(sum(s["price_chg"] for s in lst) / len(lst), 2),
            "avg_days":     round(sum(s["duration"]  for s in lst) / len(lst), 1),
        }

    return {"signals": signals, "neg2pos": stats(neg2pos),
            "pos2neg": stats(pos2neg), "total": stats(signals)}

def check_slope_alerts(window=5):
    end_dt = datetime.datetime.now(tz=timezone.utc)
    start_dt = end_dt - datetime.timedelta(days=30)
    alerts = []
    for ticker in WATCH_TICKERS:
        try:
            _, closes, _ = fetch_yahoo_range(ticker, start_dt, end_dt, interval)
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
            html.Label(id="window-label",style={"fontSize":"12px","color":"#888"}),
            dcc.Input(id="window",value=5,type="number",min=2,max=60,
                      style={"width":"70px","padding":"6px 8px","borderRadius":"6px",
                             "border":"1px solid #ddd","fontSize":"14px"}),
        ],style={"display":"flex","flexDirection":"column","gap":"4px"}),
        html.Div([
            html.Label("斜率門檻（回測用）",style={"fontSize":"12px","color":"#888"}),
            dcc.Input(id="bt-slope-thr",value=0,type="number",min=0,max=99999,step=50,
                      style={"width":"90px","padding":"6px 8px","borderRadius":"6px",
                             "border":"1px solid #ddd","fontSize":"14px"}),
            html.Span("% 以上",style={"fontSize":"12px","color":"#888","alignSelf":"flex-end","paddingBottom":"8px"}),
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
             "background":"#f5f5f3","borderRadius":"10px","padding":"14px 16px","marginBottom":"8px"}),

    html.Div([
        html.Label("時間週期",style={"fontSize":"12px","color":"#888","marginRight":"10px","alignSelf":"center"}),
        dcc.RadioItems(
            id="interval-picker",
            options=[
                {"label":"　每分鐘（近7天）", "value":"1m"},
                {"label":"　每小時（近60天）", "value":"1h"},
                {"label":"　每日（可用滑桿）", "value":"1d"},
            ],
            value="1d",
            inline=True,
            style={"fontSize":"13px","color":"#333","gap":"16px"},
            inputStyle={"marginRight":"4px"},
        ),
    ],style={"display":"flex","alignItems":"center","background":"#f0f4ff",
             "borderRadius":"10px","padding":"10px 16px","marginBottom":"12px",
             "border":"0.5px solid #c7d9f5"}),

    html.Div(id="test-msg",style={"fontSize":"13px","color":"#06c755","minHeight":"20px","marginBottom":"2px","fontFamily":"sans-serif"}),
    html.Div(id="slope-msg",style={"fontSize":"13px","color":"#2563eb","minHeight":"20px","marginBottom":"4px","fontFamily":"sans-serif"}),

    html.Div(id="slider-container", children=[
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

    dcc.Loading(
        id="loading-tab",
        type="circle",
        children=html.Div(id="tab-content"),
        color="#2563eb",
    ),

    html.H3("恐懼指標",style={"fontFamily":"sans-serif","marginTop":"24px","marginBottom":"4px","fontSize":"16px"}),
    html.P(id="fear-status",style={"fontSize":"12px","color":"#aaa","marginBottom":"8px","fontFamily":"sans-serif"}),
    html.Div(id="fear-charts"),
    html.P("VIX >30 為高恐懼區。Fear & Greed Index：0=極度恐懼，100=極度貪婪（加密市場版）。",
           style={"fontSize":"12px","color":"#aaa","marginTop":"12px","fontFamily":"sans-serif"}),

],style={"maxWidth":"1000px","margin":"2rem auto","padding":"0 1.5rem","fontFamily":"sans-serif"})

# ── Callbacks ────────────────────────────────────────────────

@app.callback(
    Output("slider-container","style"),
    Output("slider-label","children"),
    Input("interval-picker","value"),
    Input("days-slider","value"),
)
def update_slider(interval, days):
    end_dt = datetime.datetime.now(tz=timezone.utc)
    if interval == "1m":
        style = {"display":"none"}
        label = "每分鐘模式：自動抓最近 7 天"
    elif interval == "1h":
        style = {"display":"none"}
        label = "每小時模式：自動抓最近 60 天"
    else:
        start_dt = end_dt - datetime.timedelta(days=days)
        style = {"background":"#f5f5f3","borderRadius":"10px","padding":"14px 20px 18px","marginBottom":"16px"}
        label = f"資料區間：{start_dt.strftime('%Y-%m-%d')} ～ {end_dt.strftime('%Y-%m-%d')}　（{days} 天）"
    return style, label

@app.callback(
    Output("window-label","children"),
    Input("interval-picker","value"),
)
def update_window_label(interval):
    if interval == "1m":
        return "視窗（分鐘）"
    elif interval == "1h":
        return "視窗（小時）"
    else:
        return "視窗（天）"

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
            _, closes, _ = fetch_yahoo_range(ticker, start_dt, end_dt, interval)
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
    State("interval-picker","value"),
    State("bt-slope-thr","value"),
    prevent_initial_call=False,
)
def update_content(n_clicks, tab, ticker_str, window, show_volume, days, interval, bt_slope_thr):
    tickers  = [t.strip().upper() for t in (ticker_str or "QQQ").split(",") if t.strip()]
    window   = int(window or 5)
    show_vol = "vol" in (show_volume or [])
    days     = int(days or 365)
    interval = interval or "1d"
    end_dt   = datetime.datetime.now(tz=timezone.utc)

    # 根據週期決定時間範圍
    if interval == "1m":
        start_dt         = end_dt - datetime.timedelta(days=6)
        annualize_factor = 1
        slope_label      = f"{window}分鐘斜率(%)"
        date_range_str   = "最近7天（每分鐘）"
    elif interval == "1h":
        start_dt         = end_dt - datetime.timedelta(days=60)
        annualize_factor = 1
        slope_label      = f"{window}小時斜率(%)"
        date_range_str   = "最近60天（每小時）"
    else:
        start_dt         = end_dt - datetime.timedelta(days=days)
        annualize_factor = 252
        slope_label      = f"{window}日年化斜率(%)"
        date_range_str   = f"{start_dt.strftime('%Y-%m-%d')} ～ {end_dt.strftime('%Y-%m-%d')}"

    ticker_data = {}
    messages = []
    for i, ticker in enumerate(tickers[:12]):
        try:
            dates, closes, volumes, opens = fetch_yahoo_range(ticker, start_dt, end_dt, interval)
            ticker_data[ticker] = {"dates":dates,"closes":closes,"volumes":volumes,"opens":opens,"color":COLORS[i%len(COLORS)]}
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
            slopes = rolling_annualized_log_slope(closes, window, annualize_factor)
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
            fig.add_trace(go.Scatter(x=dates,y=slope_line,name=slope_label,mode="lines",
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
            # Y 軸自動縮放：計算斜率的合理範圍
            valid_slopes = [v for v in slope_line if v is not None]
            if valid_slopes and interval in ("1m","1h"):
                s_max = max(abs(v) for v in valid_slopes)
                s_range = [-s_max*1.1, s_max*1.1] if s_max > 0 else [-1, 1]
            else:
                s_range = [None, None]
            y_range_kwargs = {"range": s_range} if s_range[0] is not None else {}
            fig.update_yaxes(title_text=slope_label,secondary_y=False,showgrid=True,gridcolor="#eee",
                             zeroline=False,title_font=dict(size=10),row=1,col=1,
                             **y_range_kwargs)
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
        # 股價圖表頁籤不受此限制，只有回測頁籤才需要按更新
        if (not n_clicks or int(n_clicks) == 0) and tab == "tab-backtest":
            content = html.Div([
                html.Div([
                    html.P("點上方「更新」按鈕開始跑回測分析",
                           style={"fontSize":"15px","color":"#888","textAlign":"center","marginTop":"60px"}),
                    html.P("回測需要重新抓取資料並運算，請先確認股票代號與日期區間後再按更新",
                           style={"fontSize":"13px","color":"#aaa","textAlign":"center","marginTop":"8px"}),
                ], style={"padding":"40px"})
            ])
            vix_data = fetch_vix(start_dt, end_dt)
            fng_data = fetch_fear_greed()
            fng_data = {d:v for d,v in fng_data.items()
                        if start_dt.strftime("%Y-%m-%d") <= d <= end_dt.strftime("%Y-%m-%d")}
            fear_notes2, fear_divs2 = [], []
            has_vix2, has_fng2 = bool(vix_data), bool(fng_data)
            if has_vix2 or has_fng2:
                fear_fig2 = make_subplots(specs=[[{"secondary_y":True}]])
                if has_vix2:
                    vx2=sorted(vix_data.keys()); vy2=[vix_data[d] for d in vx2]
                    fear_fig2.add_trace(go.Scatter(x=vx2,y=vy2,name="VIX",mode="lines",
                        line=dict(color="rgb(220,72,61)",width=1.8),fill="tozeroy",fillcolor="rgba(220,72,61,0.12)",
                        hovertemplate="%{x}<br>VIX: %{y:.2f}<extra></extra>"),secondary_y=False)
                    fear_fig2.add_hline(y=30,line_color="rgba(220,72,61,0.4)",line_dash="dash",line_width=1,
                                       annotation_text="高恐懼 30",annotation_position="right",secondary_y=False)
                    fear_notes2.append("✅ VIX")
                if has_fng2:
                    fg2=sorted(fng_data.keys()); fv2=[fng_data[d] for d in fg2]
                    fear_fig2.add_trace(go.Scatter(x=fg2,y=fv2,name="Fear & Greed",mode="lines",
                        line=dict(color="rgb(214,140,0)",width=1.8),
                        hovertemplate="%{x}<br>F&G: %{y:.0f}<extra></extra>"),secondary_y=True)
                    fear_fig2.add_hline(y=50,line_color="rgba(214,140,0,0.4)",line_dash="dash",line_width=1,
                                       annotation_text="中性 50",annotation_position="right",secondary_y=True)
                    fear_notes2.append("✅ Fear & Greed")
                fear_fig2.update_layout(plot_bgcolor="white",paper_bgcolor="white",hovermode="x unified",
                    legend=dict(orientation="h",yanchor="bottom",y=1.06,xanchor="left",x=0,font=dict(size=11)),
                    margin=dict(l=55,r=65,t=50,b=40),xaxis=dict(showgrid=True,gridcolor="#eee"),
                    font=dict(family="sans-serif",size=12),height=280)
                fear_fig2.update_yaxes(title_text="VIX",secondary_y=False,showgrid=True,gridcolor="#eee",
                    zeroline=False,title_font=dict(size=11,color="rgb(220,72,61)"),tickfont=dict(color="rgb(220,72,61)"))
                fear_fig2.update_yaxes(title_text="Fear & Greed（0–100）",secondary_y=True,showgrid=False,
                    zeroline=False,range=[0,100],title_font=dict(size=11,color="rgb(214,140,0)"),
                    tickfont=dict(color="rgb(214,140,0)"))
                fear_divs2.append(html.Div(dcc.Graph(figure=fear_fig2,config={"displayModeBar":False}),
                    style={"marginBottom":"12px","border":"0.5px solid #e5e5e5",
                           "borderRadius":"10px","overflow":"hidden","background":"white"}))
            return content, fear_divs2, "　".join(messages), "　".join(fear_notes2)

        # 按了更新才跑完整回測
        # 根據週期決定回測資料範圍
        bt_end_dt = datetime.datetime.now(tz=timezone.utc)
        if interval == "1m":
            bt_start_dt  = bt_end_dt - datetime.timedelta(days=6)
            bt_interval  = "1m"
            bt_unit      = "分鐘"
            bt_ann       = 1
        elif interval == "1h":
            bt_start_dt  = bt_end_dt - datetime.timedelta(days=60)
            bt_interval  = "1h"
            bt_unit      = "小時"
            bt_ann       = 1
        else:
            bt_start_dt  = bt_end_dt - datetime.timedelta(days=days)
            bt_interval  = "1d"
            bt_unit      = "天"
            bt_ann       = 252

        bt_ticker_data = {}
        for i2, ticker in enumerate(tickers[:12]):
            try:
                d2, c2, v2, o2 = fetch_yahoo_range(ticker, bt_start_dt, bt_end_dt, bt_interval)
                bt_ticker_data[ticker] = {"dates":d2,"closes":c2,"opens":o2,"color":COLORS[i2%len(COLORS)]}
            except:
                bt_ticker_data[ticker] = None

        # 從 State 取得斜率門檻（預設 0）
        bt_slope_threshold = float(bt_slope_thr or 0)

        backtest_divs = [
            html.P(
                f"用收盤價斜率（2–20{bt_unit}視窗）做方向回測：斜率由負轉正後，統計到下一次轉負前股價是否上漲；斜率由正轉負後，統計到下一次轉正前股價是否下跌。",
                style={"fontSize":"13px","color":"#666","marginBottom":"16px",
                       "background":"#f5f5f3","padding":"10px 14px","borderRadius":"8px"}),
            html.Div([
                html.Span("負轉正斜率門檻：",
                          style={"fontSize":"12px","color":"#888","alignSelf":"center"}),
                html.Span(f"≥ {bt_slope_threshold:.0f}% 才進場" if bt_slope_threshold > 0 else "不過濾（0%）",
                          style={"fontSize":"13px","color":"#2563eb","fontWeight":"500","alignSelf":"center"}),
                html.Span("　（可在上方「斜率門檻（回測用）」輸入後按更新）",
                          style={"fontSize":"11px","color":"#aaa","alignSelf":"center"}),
            ], style={"display":"flex","alignItems":"center","background":"#f0f4ff",
                      "borderRadius":"8px","padding":"10px 14px","marginBottom":"16px",
                      "border":"0.5px solid #c7d9f5","flexWrap":"wrap","gap":"6px"}),
        ]

        th_style = {"padding":"4px 8px","textAlign":"left","fontSize":"12px",
                    "color":"#888","borderBottom":"0.5px solid #eee"}

        def make_sig_table(signals, best_win):
            rows = []
            for s in signals:
                chg_color  = "#0F6E56" if s["price_chg"] >= 0 else "#A32D2D"
                ok_text    = "✅" if s["correct"] else "❌"
                ok_color   = "#0F6E56" if s["correct"] else "#A32D2D"
                type_color = "#0F6E56" if "負轉正" in s["type"] else "#A32D2D"
                prev_s = s.get("prev_slope", 0)
                cur_s  = s.get("cur_slope",  0)
                exit_s      = s.get("exit_slope", 0)
                exit_prev_s = s.get("exit_prev_slope", 0)
                rows.append(html.Tr([
                    html.Td(s["type"],        style={"padding":"4px 8px","color":type_color,"fontWeight":"500"}),
                    html.Td(s["entry_date"],  style={"padding":"4px 8px"}),
                    html.Td(s["exit_date"],   style={"padding":"4px 8px"}),
                    html.Td(f"{s['duration']} 天", style={"padding":"4px 8px"}),
                    html.Td(f"${s['entry_price']:.2f}", style={"padding":"4px 8px"}),
                    html.Td(f"${s['exit_price']:.2f}",  style={"padding":"4px 8px"}),
                    html.Td(f"{'+' if s['price_chg']>=0 else ''}{s['price_chg']}%",
                            style={"padding":"4px 8px","color":chg_color,"fontWeight":"500"}),
                    html.Td(f"{prev_s:.1f}%", style={"padding":"4px 8px","color":"#A32D2D"}),
                    html.Td(f"{'+' if cur_s>=0 else ''}{cur_s:.1f}%",
                            style={"padding":"4px 8px","color":"#0F6E56" if cur_s>=0 else "#A32D2D"}),
                    html.Td(f"{exit_prev_s:.1f}%",
                            style={"padding":"4px 8px","color":"#0F6E56" if exit_prev_s>=0 else "#A32D2D"}),
                    html.Td(f"{'+' if exit_s>=0 else ''}{exit_s:.1f}%",
                            style={"padding":"4px 8px","color":"#A32D2D" if exit_s<0 else "#0F6E56"}),
                    html.Td(ok_text, style={"padding":"4px 8px","color":ok_color,"fontWeight":"500"}),
                ]))
            return html.Div([
                html.P(f"最佳視窗（{best_win}{bt_unit}）訊號明細：",
                       style={"fontSize":"12px","color":"#888","margin":"8px 0 4px","paddingLeft":"12px"}),
                html.Div(html.Table([
                    html.Thead(html.Tr([
                        html.Th("類型",style=th_style), html.Th("進場日",style=th_style),
                        html.Th("出場日",style=th_style), html.Th("持續",style=th_style),
                        html.Th("進場價",style=th_style), html.Th("出場價",style=th_style),
                        html.Th("股價變化",style=th_style),
                        html.Th("進場前日斜率",style=th_style), html.Th("進場當日斜率",style=th_style),
                        html.Th("出場前日斜率",style=th_style), html.Th("出場當日斜率",style=th_style),
                        html.Th("預測",style=th_style),
                    ])),
                    html.Tbody(rows),
                ],style={"width":"100%","borderCollapse":"collapse","fontSize":"12px"}),
                style={"overflowX":"auto","paddingBottom":"8px"}),
            ]) if rows else html.P("無訊號",style={"fontSize":"12px","color":"#aaa","paddingLeft":"12px"})

        for ticker in tickers[:12]:
            d = bt_ticker_data.get(ticker)
            if d is None:
                backtest_divs.append(html.Div(f"❌ {ticker}：資料不足",
                    style={"padding":"10px","color":"#dc2626","fontSize":"13px",
                           "background":"#fff5f5","borderRadius":"8px","marginBottom":"12px"}))
                continue

            dates, closes, color = d["dates"], d["closes"], d["color"]

            # 跑 2–20 日視窗回測
            all_results = backtest_all_windows(closes, dates, bt_ann, d.get("opens"))
            valid_wins  = [w for w in all_results if all_results[w]["neg2pos"]["count"] > 0]

            if not valid_wins:
                backtest_divs.append(html.Div(f"⚪ {ticker}：資料期間內無訊號",
                    style={"padding":"10px","color":"#888","fontSize":"13px","marginBottom":"12px"}))
                continue

            best_win = max(valid_wins,
                key=lambda w: (all_results[w]["neg2pos"]["correct_rate"],
                               all_results[w]["neg2pos"]["avg_chg"]))

            win_list   = list(range(2, 21))
            n2p_rates  = [all_results[w]["neg2pos"]["correct_rate"] for w in win_list]
            p2n_rates  = [all_results[w]["pos2neg"]["correct_rate"] for w in win_list]
            n2p_cnts   = [all_results[w]["neg2pos"]["count"] for w in win_list]
            n2p_chgs   = [all_results[w]["neg2pos"]["avg_chg"] for w in win_list]
            win_labels = [f"{w}{bt_unit}" for w in win_list]
            bar_colors = [color if w==best_win else "rgba(150,150,150,0.3)" for w in win_list]

            fig_bt = go.Figure()
            fig_bt.add_trace(go.Bar(
                x=win_labels, y=n2p_rates, name="負轉正正確率",
                marker_color=bar_colors,
                text=[f"{r}%({c}次)" for r,c in zip(n2p_rates,n2p_cnts)],
                textposition="outside",
                hovertemplate="視窗%{x}<br>負轉正正確率: %{y}%<extra></extra>"))
            fig_bt.add_trace(go.Scatter(
                x=win_labels, y=p2n_rates, name="正轉負正確率",
                line=dict(color="rgba(226,75,74,0.7)",width=2,dash="dot"),
                mode="lines+markers", marker=dict(size=4),
                hovertemplate="視窗%{x}<br>正轉負正確率: %{y}%<extra></extra>"))
            fig_bt.add_trace(go.Scatter(
                x=win_labels, y=n2p_chgs, name="平均漲幅",
                line=dict(color="rgba(37,99,235,0.6)",width=1.5),
                mode="lines+markers", marker=dict(size=4), yaxis="y2",
                hovertemplate="視窗%{x}<br>平均漲幅: %{y:.1f}%<extra></extra>"))
            fig_bt.add_hline(y=50, line_color="#ccc", line_dash="dash", line_width=1,
                             annotation_text="隨機基準 50%", annotation_position="right")

            best_n2p = all_results[best_win]["neg2pos"]
            best_p2n = all_results[best_win]["pos2neg"]
            fig_bt.update_layout(
                title=dict(
                    text=f"<b>{ticker}</b>　斜率方向預測正確率（最佳視窗：{best_win}{bt_unit}，負轉正 {best_n2p['correct_rate']}%）",
                    font=dict(size=13,color="#1a1a1a"),x=0),
                barmode="overlay", hovermode="x unified",
                legend=dict(orientation="h",yanchor="bottom",y=1.06,xanchor="right",x=1,font=dict(size=11)),
                plot_bgcolor="white", paper_bgcolor="white",
                margin=dict(l=55,r=65,t=55,b=40),
                font=dict(family="sans-serif",size=12), height=320,
                yaxis=dict(title="正確率(%)",showgrid=True,gridcolor="#eee",range=[0,115]),
                yaxis2=dict(title="平均漲幅(%)",overlaying="y",side="right",showgrid=False),
                xaxis=dict(showgrid=True,gridcolor="#eee"),
            )

            # 摘要卡
            summary_cards = html.Div([
                html.Div([
                    html.Div([
                        html.Div("最佳視窗",style={"fontSize":"11px","color":"#888","marginBottom":"3px"}),
                        html.Div(f"{best_win} {bt_unit}",style={"fontSize":"20px","fontWeight":"500","color":"#0F6E56"}),
                    ],style={"background":"#f0faf5","borderRadius":"8px","padding":"10px 14px",
                             "border":"1.5px solid #1D9E75"}),
                    html.Div([
                        html.Div("負轉正正確率",style={"fontSize":"11px","color":"#888","marginBottom":"3px"}),
                        html.Div(f"{best_n2p['correct_rate']}%",
                                 style={"fontSize":"20px","fontWeight":"500","color":"#0F6E56"}),
                        html.Div(f"{best_n2p['count']} 次訊號",style={"fontSize":"11px","color":"#888"}),
                    ],style={"background":"#f5f5f3","borderRadius":"8px","padding":"10px 14px"}),
                    html.Div([
                        html.Div("平均漲幅（到下次反轉）",style={"fontSize":"11px","color":"#888","marginBottom":"3px"}),
                        html.Div(f"{'+' if best_n2p['avg_chg']>=0 else ''}{best_n2p['avg_chg']}%",
                                 style={"fontSize":"20px","fontWeight":"500",
                                        "color":"#0F6E56" if best_n2p['avg_chg']>=0 else "#A32D2D"}),
                        html.Div(f"平均持續 {best_n2p['avg_days']} 天",style={"fontSize":"11px","color":"#888"}),
                    ],style={"background":"#f5f5f3","borderRadius":"8px","padding":"10px 14px"}),
                    html.Div([
                        html.Div("正轉負正確率",style={"fontSize":"11px","color":"#888","marginBottom":"3px"}),
                        html.Div(f"{best_p2n['correct_rate']}%",
                                 style={"fontSize":"20px","fontWeight":"500","color":"#A32D2D"}),
                        html.Div(f"{best_p2n['count']} 次訊號",style={"fontSize":"11px","color":"#888"}),
                    ],style={"background":"#f5f5f3","borderRadius":"8px","padding":"10px 14px"}),
                ],style={"display":"grid","gridTemplateColumns":"repeat(4,1fr)","gap":"10px",
                         "padding":"12px","marginTop":"4px"}),
            ])

            # 模擬交易
            best_signals = all_results[best_win]["signals"]
            sim_section  = html.Div()
            if best_signals:
                first_price  = best_signals[0]["entry_price"]
                last_price   = best_signals[-1]["exit_price"]
                bah_ret      = round((last_price - first_price) / first_price * 100, 2)
                init_capital = 100000

                # 以斜率門檻 0~2000% 每 50% 一行做模擬
                sim_thresholds = [0] + list(range(50, 2001, 50))
                sim_rows_long, sim_rows_both = [], []
                best_long, best_both = None, None
                for thr in sim_thresholds:
                    rl = simulate_trading(best_signals, "long", init_capital, 1, thr)
                    rb = simulate_trading(best_signals, "both", init_capital, 1, thr)
                    sim_rows_long.append((thr, rl))
                    sim_rows_both.append((thr, rb))
                    if best_long is None or rl["final_val"] > best_long[1]["final_val"]:
                        best_long = (thr, rl)
                    if best_both is None or rb["final_val"] > best_both[1]["final_val"]:
                        best_both = (thr, rb)

                bah_final = round(init_capital * (1 + bah_ret/100), 0)

                th_s2 = {"padding":"4px 8px","textAlign":"left","fontSize":"12px",
                         "color":"#888","borderBottom":"0.5px solid #eee"}

                def sim_tbl(rows, best_d):
                    def row(min_d, r, is_best):
                        rc = "#0F6E56" if r["total_ret"]>=0 else "#A32D2D"
                        fw = "500" if is_best else "400"
                        return html.Tr([
                            html.Td(f"≥{min_d}天", style={"padding":"4px 8px","fontWeight":fw}),
                            html.Td(f"{r['final_val']/10000:.2f}萬", style={"padding":"4px 8px","color":rc,"fontWeight":fw}),
                            html.Td(f"{'+' if r['total_ret']>=0 else ''}{r['total_ret']}%", style={"padding":"4px 8px","color":rc}),
                            html.Td(str(r['trade_count'])+"筆", style={"padding":"4px 8px"}),
                            html.Td(f"{r['win_rate']}%",  style={"padding":"4px 8px"}),
                            html.Td(f"-{r['max_dd']}%",   style={"padding":"4px 8px","color":"#A32D2D"}),
                        ])
                    return html.Table([
                        html.Thead(html.Tr([
                            html.Th("最小持續",style=th_s2), html.Th("最終資產",style=th_s2),
                            html.Th("總報酬",style=th_s2),  html.Th("交易數",style=th_s2),
                            html.Th("勝率",style=th_s2),    html.Th("最大回撤",style=th_s2),
                        ])),
                        html.Tbody([row(d2, r2, d2==best_d) for d2,r2 in rows]),
                    ], style={"width":"100%","borderCollapse":"collapse","fontSize":"12px"})

                # 買入持有那一行
                bah_color = "#0F6E56" if bah_ret >= 0 else "#A32D2D"
                bah_row = html.Tr([
                    html.Td("買入持有", style={"padding":"4px 8px","fontWeight":"500","color":"#888"}),
                    html.Td(f"{bah_final/10000:.2f}萬", style={"padding":"4px 8px","color":bah_color,"fontWeight":"500"}),
                    html.Td(f"{bah_ret:+.2f}%", style={"padding":"4px 8px","color":bah_color}),
                    html.Td("1筆", style={"padding":"4px 8px"}),
                    html.Td("—", style={"padding":"4px 8px"}),
                    html.Td("—", style={"padding":"4px 8px"}),
                    html.Td("—", style={"padding":"4px 8px"}),
                ], style={"background":"rgba(100,100,100,0.06)"})

                def sim_tbl_with_bah(rows, best_thr):
                    def row(thr, r, is_best):
                        rc = "#0F6E56" if r["total_ret"]>=0 else "#A32D2D"
                        fw = "500" if is_best else "400"
                        beat = "✅" if r["final_val"] > bah_final else "❌"
                        lbl = "不過濾" if thr == 0 else f"≥{thr}%"
                        return html.Tr([
                            html.Td(lbl, style={"padding":"4px 8px","fontWeight":fw}),
                            html.Td(f"{r['final_val']/10000:.2f}萬", style={"padding":"4px 8px","color":rc,"fontWeight":fw}),
                            html.Td(f"{'+' if r['total_ret']>=0 else ''}{r['total_ret']}%", style={"padding":"4px 8px","color":rc}),
                            html.Td(str(r['trade_count'])+"筆", style={"padding":"4px 8px"}),
                            html.Td(f"{r['win_rate']}%",  style={"padding":"4px 8px"}),
                            html.Td(f"-{r['max_dd']}%",   style={"padding":"4px 8px","color":"#A32D2D"}),
                            html.Td(beat, style={"padding":"4px 8px"}),
                        ])
                    return html.Table([
                        html.Thead(html.Tr([
                            html.Th("斜率門檻",style=th_s2), html.Th("最終資產",style=th_s2),
                            html.Th("總報酬",style=th_s2),  html.Th("交易數",style=th_s2),
                            html.Th("勝率",style=th_s2),    html.Th("最大回撤",style=th_s2),
                            html.Th("贏過買持",style=th_s2),
                        ])),
                        html.Tbody([bah_row] + [row(t2, r2, t2==best_thr) for t2,r2 in rows]),
                    ], style={"width":"100%","borderCollapse":"collapse","fontSize":"12px"})

                sim_section = html.Div([
                    html.P("模擬交易（初始 10 萬，按最佳視窗訊號進出場）",
                           style={"fontSize":"12px","color":"#888","margin":"12px 0 8px",
                                  "paddingLeft":"12px","fontWeight":"500"}),
                    html.Div([
                        html.Div([
                            html.P(f"只做多　最佳門檻：{'不過濾' if best_long[0]==0 else f'≥{best_long[0]}%'}，{best_long[1]['final_val']/10000:.2f}萬（{best_long[1]['total_ret']:+.2f}%）",
                                   style={"fontSize":"12px","color":"#0F6E56","margin":"0 0 6px","fontWeight":"500"}),
                            html.Div(sim_tbl_with_bah(sim_rows_long, best_long[0]), style={"overflowX":"auto"}),
                        ], style={"flex":"1","minWidth":"0"}),
                        html.Div([
                            html.P(f"多空都做　最佳門檻：{'不過濾' if best_both[0]==0 else f'≥{best_both[0]}%'}，{best_both[1]['final_val']/10000:.2f}萬（{best_both[1]['total_ret']:+.2f}%）",
                                   style={"fontSize":"12px","color":"#0F6E56","margin":"0 0 6px","fontWeight":"500"}),
                            html.Div(sim_tbl_with_bah(sim_rows_both, best_both[0]), style={"overflowX":"auto"}),
                        ], style={"flex":"1","minWidth":"0"}),
                    ], style={"display":"flex","gap":"20px","padding":"0 12px","flexWrap":"wrap"}),
                ])

            backtest_divs.append(html.Div([
                dcc.Graph(figure=fig_bt, config={"displayModeBar":False}),
                summary_cards,
                sim_section,
                make_sig_table(all_results[best_win]["signals"], best_win),
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
