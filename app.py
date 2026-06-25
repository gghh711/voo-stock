"""
股票滾動年化斜率 + 恐懼指標 + LINE 通知 + 回測功能
"""

import datetime, math, os, json, threading
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
FRED_API_KEY  = os.environ.get("FRED_API_KEY",  "cf36f7a356563694d9f5a06b63ad0cae")
WATCH_TICKERS    = ["QQQ", "VOO", "^SOX"]
WATCH_TICKERS_TW = ["^TWII"]
WATCH_TICKERS_ASIA = ["^N225", "^KS11"]
TICKER_NAMES = {
    "^TWII":  "台灣加權",
    "^N225":  "日經225",
    "^KS11":  "韓國KOSPI",
    "^SOX":   "費城半導體",
}
# 定時發送時間（台灣時間，UTC = 台灣時間 - 8）
SCHEDULE_HOURS_TW   = [20, 22]   # 可由使用者更改
SCHEDULE_CONTENT    = ["slope"]  # "slope" / "option" / 兩者
SCHEDULE_OPT_TICKER = "QQQ"      # 期權分析標的

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
    interval: "1d"=每日, "1h"=每小時, "1wk"=每週
    注意：Yahoo Finance 限制：
      - 1wk 可抓多年資料
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
        if interval == "1mo":
            fmt = "%Y-%m"
        elif interval in ("1h",):
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

def fetch_fred_inventory():
    """
    從 FRED 抓取多項美國經濟數據（月度，季調）：
    - MNFCTRIRSA：製造業庫存（百萬美元）
    - RETAILIRSA ：零售業庫存（百萬美元）
    - PCE        ：個人消費支出 PCE（十億美元）
    - RSXFS     ：零售銷售（不含汽油，百萬美元）
    """
    result = {
        "manufacturing": [],
        "retail":        [],
        "pce":           [],
        "retail_sales":  [],
    }
    series = {
        "manufacturing": "MNFCTRIRSA",
        "retail":        "RETAILIRSA",
        "pce":           "PCE",
        "retail_sales":  "RSXFS",
    }
    for key, series_id in series.items():
        try:
            url = (f"https://api.stlouisfed.org/fred/series/observations"
                   f"?series_id={series_id}&api_key={FRED_API_KEY}"
                   f"&file_type=json&sort_order=desc&limit=120")
            r = requests.get(url, timeout=15)
            if r.status_code != 200: continue
            obs = r.json().get("observations", [])
            for o in reversed(obs):  # 反轉讓日期從舊到新
                if o["value"] == ".": continue
                result[key].append((o["date"], float(o["value"])))
        except: continue
    return result

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

def slope_state(cur, prev):
    """六狀態斜率分類"""
    if cur is None or prev is None or math.isnan(cur) or math.isnan(prev):
        return "未知"
    if prev > 0 and cur > 0:
        return "加速上升" if cur > prev else "減速上升"
    elif prev < 0 and cur < 0:
        return "加速下跌" if cur < prev else "減速下跌"
    elif prev < 0 and cur > 0:
        return "由負轉正"
    elif prev > 0 and cur < 0:
        return "由正轉負"
    elif prev == 0 or cur == 0:
        return "減速上升" if cur >= 0 else "減速下跌"
    return "未知"

def analyze_six_states(day_closes, dates_d, w_l1=120, w_l2=40, w_l3=10, forward_days=5):
    """
    分析 L1/L2/L3 六狀態組合：
    - 平均報酬：從進場日持有到今天
    - 短期報酬：進場後 forward_days 天的報酬（用於找出場訊號）
    """
    s_mo  = rolling_annualized_log_slope(day_closes, w_l1, 252)
    s_wk  = rolling_annualized_log_slope(day_closes, w_l2, 252)
    s_day = rolling_annualized_log_slope(day_closes, w_l3, 252)
    N = len(day_closes)
    last_price = day_closes[-1]

    combo_results = {}

    for i in range(1, N - forward_days - 1):
        c1, p1 = s_mo[i],  s_mo[i-1]
        c2, p2 = s_wk[i],  s_wk[i-1]
        c3, p3 = s_day[i], s_day[i-1]
        if any(math.isnan(x) for x in [c1,p1,c2,p2,c3,p3]):
            continue

        st1 = slope_state(c1, p1)
        st2 = slope_state(c2, p2)
        st3 = slope_state(c3, p3)
        if "未知" in (st1, st2, st3):
            continue

        entry_price = day_closes[i]
        # 持有到今天的報酬
        hold_ret  = (last_price - entry_price) / entry_price * 100
        # 未來 N 天短期報酬（用於判斷出場）
        fwd_ret   = (day_closes[i + forward_days] - entry_price) / entry_price * 100

        key = (st1, st2, st3)
        combo_results.setdefault(key, []).append({
            "hold_ret": hold_ret,
            "fwd_ret":  fwd_ret,
            "date":     dates_d[i],
            "entry":    entry_price,
        })

    bah_total = round((last_price - day_closes[0]) / day_closes[0] * 100, 2)

    summary = []
    for key, records in combo_results.items():
        if len(records) < 3:
            continue
        hold_rets = [r["hold_ret"] for r in records]
        fwd_rets  = [r["fwd_ret"]  for r in records]
        avg_hold  = round(sum(hold_rets) / len(hold_rets), 2)
        avg_fwd   = round(sum(fwd_rets)  / len(fwd_rets),  2)
        win_hold  = round(sum(1 for r in hold_rets if r > 0) / len(hold_rets) * 100, 1)
        win_fwd   = round(sum(1 for r in fwd_rets  if r > 0) / len(fwd_rets)  * 100, 1)
        best      = round(max(hold_rets), 1)
        worst     = round(min(hold_rets), 1)
        summary.append({
            "l1": key[0], "l2": key[1], "l3": key[2],
            "avg_ret":  avg_hold,   # 持有到今報酬
            "avg_fwd":  avg_fwd,    # 短期 N 天報酬
            "win_rate": win_hold,
            "win_fwd":  win_fwd,
            "best":     best,
            "worst":    worst,
            "count":    len(records),
        })

    # 進場排名：持有到今報酬由高到低
    summary_entry = sorted(summary, key=lambda x: x["avg_ret"], reverse=True)
    # 出場排名：未來 N 天報酬由低到高（最差的先出）
    summary_exit  = sorted(summary, key=lambda x: x["avg_fwd"])

    return summary_entry, summary_exit, bah_total


def paired_backtest(day_closes, dates_d, entry_combos, exit_combos,
                    w_l1=120, w_l2=40, w_l3=10, capital=100000):
    """
    配對回測：
    - entry_combos：進場組合集合（set of (l1,l2,l3) tuples）
    - exit_combos ：出場組合集合
    從頭到尾掃描，遇到進場訊號就買入，遇到出場訊號就賣出
    回傳每筆交易、最終資產、買入持有對比
    """
    s_mo  = rolling_annualized_log_slope(day_closes, w_l1, 252)
    s_wk  = rolling_annualized_log_slope(day_closes, w_l2, 252)
    s_day = rolling_annualized_log_slope(day_closes, w_l3, 252)
    N = len(day_closes)

    cash   = float(capital)
    shares = 0.0
    trades = []
    equity = []
    peak   = cash
    max_dd = 0.0

    for i in range(1, N):
        c1 = s_mo[i];  p1 = s_mo[i-1]
        c2 = s_wk[i];  p2 = s_wk[i-1]
        c3 = s_day[i]; p3 = s_day[i-1]
        if any(math.isnan(x) for x in [c1,p1,c2,p2,c3,p3]):
            val = cash + shares * day_closes[i]
            equity.append({"date":dates_d[i],"val":val})
            continue

        st1 = slope_state(c1, p1)
        st2 = slope_state(c2, p2)
        st3 = slope_state(c3, p3)
        price = day_closes[i]
        date  = dates_d[i]
        combo = (st1, st2, st3)

        # 出場優先
        if shares > 0 and combo in exit_combos:
            proceeds = shares * price
            cash += proceeds
            trades.append({"action":"賣出","date":date,"price":round(price,2),
                           "combo":f"{st1}+{st2}+{st3}",
                           "amount":round(proceeds,2),"cash_after":round(cash,2)})
            shares = 0.0
        # 進場
        elif shares == 0 and combo in entry_combos and cash > 0.01:
            shares = cash / price
            trades.append({"action":"買入","date":date,"price":round(price,2),
                           "combo":f"{st1}+{st2}+{st3}",
                           "amount":round(cash,2),"cash_after":0})
            cash = 0.0

        val    = cash + shares * price
        peak   = max(peak, val)
        max_dd = max(max_dd, (peak - val) / peak * 100)
        equity.append({"date":date,"val":val})

    final_val = cash + day_closes[-1] * shares
    total_ret = round((final_val - capital) / capital * 100, 2)
    bah_ret   = round((day_closes[-1] - day_closes[0]) / day_closes[0] * 100, 2)
    return {
        "final_val": round(final_val, 2),
        "total_ret": total_ret,
        "bah_ret":   bah_ret,
        "bah_final": round(capital * (1 + bah_ret/100), 2),
        "max_dd":    round(max_dd, 1),
        "trades":    trades,
        "equity":    equity,
        "dates":     dates_d,
        "closes":    day_closes,
    }


def run_mtf_sim(day_closes, dates_d, w_l1, w_l2, w_l3, capital=100000):
    """
    用指定視窗跑三層確認策略回測，回傳績效指標
    w_l1: L1 月線視窗（日數）
    w_l2: L2 週線視窗（日數）
    w_l3: L3 日線視窗（日數）
    """
    if len(day_closes) < max(w_l1, w_l2, w_l3) + 5:
        return None

    s_mo  = rolling_annualized_log_slope(day_closes, w_l1, 252)
    s_wk  = rolling_annualized_log_slope(day_closes, w_l2, 252)
    s_day = rolling_annualized_log_slope(day_closes, w_l3, 252)

    N = len(day_closes)
    cash = float(capital)
    shares = 0.0
    peak = cash
    max_dd = 0.0
    trade_count = 0

    for i in range(2, N):
        d_slope = s_day[i]   if not math.isnan(s_day[i])   else 0
        d_prev  = s_day[i-1] if not math.isnan(s_day[i-1]) else 0
        d_prev2 = s_day[i-2] if not math.isnan(s_day[i-2]) else 0
        w_slope = s_wk[i]    if not math.isnan(s_wk[i])    else 0
        m_slope = s_mo[i]    if not math.isnan(s_mo[i])    else 0
        price   = day_closes[i]

        # 出場：L2 轉負
        if shares > 0 and w_slope < 0:
            cash += shares * price
            shares = 0.0

        elif m_slope > 0 and w_slope > 0:
            flatten      = d_slope < 0 and d_prev < 0 and abs(d_slope) < abs(d_prev)
            was_expanding= d_prev < 0 and d_prev2 < 0 and abs(d_prev) > abs(d_prev2)
            n2p          = d_prev < 0 and d_slope > 0
            best_allin   = flatten and was_expanding

            if best_allin and cash > 0.01:
                shares += cash / price; cash = 0.0; trade_count += 1
            elif flatten and not was_expanding:
                amt = cash * 0.5
                if amt > 0.01:
                    shares += amt / price; cash -= amt; trade_count += 1
            elif n2p and cash > 0.01:
                shares += cash / price; cash = 0.0; trade_count += 1
            elif d_slope > 0 and shares == 0 and cash > 0.01:
                shares += cash / price; cash = 0.0; trade_count += 1

        val = cash + shares * day_closes[i]
        peak = max(peak, val)
        max_dd = max(max_dd, (peak - val) / peak * 100)

    final_val = cash + day_closes[-1] * shares
    total_ret = round((final_val - capital) / capital * 100, 2)
    bah_ret   = round((day_closes[-1] - day_closes[0]) / day_closes[0] * 100, 2)
    return {
        "total_ret": total_ret,
        "final_val": final_val,
        "bah_ret":   bah_ret,
        "max_dd":    round(max_dd, 1),
        "trade_count": trade_count,
        "beat_bah":  final_val > capital * (1 + bah_ret/100),
    }


def multi_timeframe_strategy(ticker_sym, end_dt, capital=100000):
    """
    三層多時間框架確認策略（只抓一次日線，月/週用日線計算）：
    L1 月線方向：用 120 日斜率（約 6 個月）代表月線
    L2 週線方向：用  40 日斜率（約 8 週）代表週線
    L3 日線進場：10 日斜率跌勢趨緩 or 負轉正
    進場：L1>0 且 L2>0 且 L3觸發
    出場：週線方向（40日斜率）轉負
    """
    try:
        start_dt = end_dt - datetime.timedelta(days=365*3)
        dates_d, day_closes, _, _ = fetch_yahoo_range(ticker_sym, start_dt, end_dt, "1d")
    except Exception as e:
        return None, None, None, str(e)

    if len(day_closes) < 130:
        return None, None, None, f"資料不足（只有 {len(day_closes)} 筆，需要至少 130 筆）"

    # 三層斜率
    s_mo  = rolling_annualized_log_slope(day_closes, 120, 252)  # L1 月線方向
    s_wk  = rolling_annualized_log_slope(day_closes,  40, 252)  # L2 週線方向
    s_day = rolling_annualized_log_slope(day_closes,  10, 252)  # L3 日線進場

    # 最新值與前一個有效值
    def latest(slopes):
        return next((s for s in reversed(slopes) if not math.isnan(s)), None)

    def prev_val(slopes):
        # 找最後一個有效值的前一個有效值
        valid_idx = [i for i, s in enumerate(slopes) if not math.isnan(s)]
        if len(valid_idx) < 2:
            return None
        return slopes[valid_idx[-2]]

    mo_cur  = latest(s_mo)
    wk_cur  = latest(s_wk)
    day_cur = latest(s_day)
    day_prev = prev_val(s_day)
    wk_prev  = prev_val(s_wk)

    l1_ok  = mo_cur  is not None and mo_cur  > 0
    l2_ok  = wk_cur  is not None and wk_cur  > 0
    l3_ok  = (day_cur is not None and day_prev is not None and
               day_cur < 0 and day_prev < 0 and abs(day_cur) < abs(day_prev))  # 跌勢趨緩
    l3_n2p = (day_cur is not None and day_prev is not None and
               day_prev < 0 and day_cur > 0)                                    # 負轉正
    l3_pos = (day_cur is not None and day_cur > 0)                              # 正斜率（動能向上）

    # 模擬交易
    N    = len(day_closes)
    cash = float(capital)
    shares = 0.0
    trades = []
    equity = []
    peak   = cash
    max_dd = 0.0

    for i in range(1, N):
        d_slope = s_day[i]  if not math.isnan(s_day[i])  else 0
        d_prev  = s_day[i-1] if not math.isnan(s_day[i-1]) else 0
        w_slope = s_wk[i]   if not math.isnan(s_wk[i])   else 0
        m_slope = s_mo[i]   if not math.isnan(s_mo[i])   else 0
        price   = day_closes[i]
        date    = dates_d[i]

        # 出場：週線轉負
        if shares > 0 and w_slope < 0:
            proceeds = shares * price
            cash    += proceeds
            trades.append({"action":"賣出","date":date,"price":price,
                           "amount":round(proceeds,2),"cash_after":round(cash,2)})
            shares = 0.0

        # 進場：L1>0 且 L2>0 且 L3觸發
        elif m_slope > 0 and w_slope > 0:
            d_prev2 = s_day[i-2] if i >= 2 and not math.isnan(s_day[i-2]) else None

            flatten      = d_slope < 0 and d_prev < 0 and abs(d_slope) < abs(d_prev)
            was_expanding= d_prev2 is not None and d_prev < 0 and d_prev2 < 0 and abs(d_prev) > abs(d_prev2)
            n2p          = d_prev < 0 and d_slope > 0

            # 最佳 ALL IN：前一天跌勢擴大 → 今天跌勢趨緩（轉折點）
            best_allin = flatten and was_expanding

            if best_allin and cash > 0.01:
                amt = cash
                shares += amt / price
                cash    = 0.0
                trades.append({"action":"買入(ALL IN 最佳)","date":date,"price":price,
                               "amount":round(amt,2),"cash_after":round(cash,2)})
            elif flatten and not was_expanding:
                # 跌勢趨緩但前一天不是擴大 → 分批 50%
                amt = cash * 0.5
                if amt > 0.01:
                    shares += amt / price
                    cash   -= amt
                    trades.append({"action":"買入(50%)","date":date,"price":price,
                                   "amount":round(amt,2),"cash_after":round(cash,2)})
            elif n2p and cash > 0.01:
                # 負轉正 → ALL IN
                amt = cash
                shares += amt / price
                cash    = 0.0
                trades.append({"action":"買入(ALL IN)","date":date,"price":price,
                               "amount":round(amt,2),"cash_after":round(cash,2)})
            elif d_slope > 0 and shares == 0 and cash > 0.01:
                # 三層都正但還沒持股 → ALL IN
                amt = cash
                shares += amt / price
                cash    = 0.0
                trades.append({"action":"買入(ALL IN)","date":date,"price":price,
                               "amount":round(amt,2),"cash_after":round(cash,2)})

        val    = cash + shares * price
        peak   = max(peak, val)
        max_dd = max(max_dd, (peak - val) / peak * 100)
        equity.append({"date":date,"val":val})

    final_val = cash + day_closes[-1] * shares
    total_ret = round((final_val - capital) / capital * 100, 2)
    bah_ret   = round((day_closes[-1] - day_closes[0]) / day_closes[0] * 100, 2)
    bah_final = round(capital * (1 + bah_ret/100), 0)

    signals = {
        "mo_cur":   round(mo_cur,   1) if mo_cur   is not None else None,
        "wk_cur":   round(wk_cur,   1) if wk_cur   is not None else None,
        "wk_prev":  round(wk_prev,  1) if wk_prev  is not None else None,
        "day_cur":  round(day_cur,  1) if day_cur  is not None else None,
        "day_prev": round(day_prev, 1) if day_prev is not None else None,
        "l1_ok": l1_ok, "l2_ok": l2_ok,
        "l3_ok": l3_ok, "l3_n2p": l3_n2p, "l3_pos": l3_pos,
    }
    result = {
        "final_val": round(final_val, 2),
        "total_ret": total_ret,
        "bah_ret":   bah_ret,
        "bah_final": bah_final,
        "max_dd":    round(max_dd, 1),
        "trades":    trades,
        "equity":    equity,
        "dates":     dates_d,
        "closes":    day_closes,
    }
    return signals, result, None, None


def moving_average_bands(closes, window):
    """
    均線五線譜：中線（簡單移動平均）± 1標準差 ± 2標準差
    回傳 dict，每個 key 對應一條線（長度與 closes 相同，前面不足視窗的部分為 None）
    """
    n = len(closes)
    ma     = [None] * n
    upper1 = [None] * n
    lower1 = [None] * n
    upper2 = [None] * n
    lower2 = [None] * n
    for i in range(window-1, n):
        sub = closes[i-window+1:i+1]
        m = sum(sub) / window
        var = sum((v-m)**2 for v in sub) / window
        sd = math.sqrt(var)
        ma[i]     = m
        upper1[i] = m + sd
        lower1[i] = m - sd
        upper2[i] = m + 2*sd
        lower2[i] = m - 2*sd
    return {"ma":ma, "upper1":upper1, "lower1":lower1, "upper2":upper2, "lower2":lower2}

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

        # 判斷預測是否正確（原方法：持有到下次反轉，股價方向）
        if sig_type == "負轉正":
            correct = price_chg > 0
        else:
            correct = price_chg < 0

        # 新判斷方法：訊號當天收盤價 vs T+1、T+2 收盤價平均
        # 負轉正：當天價格 < T+1,T+2平均 → 正確（之後漲）
        # 正轉負：當天價格 > T+1,T+2平均 → 正確（之後跌）
        correct_t2 = None
        if i + 2 < N:
            avg_t1t2 = (closes[i+1] + closes[i+2]) / 2
            if sig_type == "負轉正":
                correct_t2 = closes[i] < avg_t1t2
            else:
                correct_t2 = closes[i] > avg_t1t2

        signals.append({
            "type":             sig_type,
            "entry_date":       entry_date,
            "exit_date":        exit_date,
            "entry_price":      entry_price,
            "exit_price":       exit_price,
            "duration":         duration,
            "price_chg":        price_chg,
            "correct":          correct,
            "correct_t2":       correct_t2,
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
        if not lst: return {"count":0,"correct_rate":0,"avg_chg":0,"avg_days":0,"correct_rate_t2":0,"count_t2":0}
        correct = sum(1 for s in lst if s["correct"])
        lst_t2 = [s for s in lst if s.get("correct_t2") is not None]
        correct_t2 = sum(1 for s in lst_t2 if s["correct_t2"])
        return {
            "count":           len(lst),
            "correct_rate":    round(correct / len(lst) * 100),
            "avg_chg":         round(sum(s["price_chg"] for s in lst) / len(lst), 2),
            "avg_days":        round(sum(s["duration"]  for s in lst) / len(lst), 1),
            "correct_rate_t2": round(correct_t2 / len(lst_t2) * 100) if lst_t2 else 0,
            "count_t2":        len(lst_t2),
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

def simulate_trading(signals, logic, capital, min_duration=1):
    cash = float(capital)
    trades = []
    equity_curve = []
    peak = cash
    max_dd = 0.0
    idle_days = 0
    prev_exit_date = signals[0]["entry_date"] if signals else None

    for sig in signals:
        dur = sig["duration"]
        if dur < min_duration:
            continue

        sig_type   = sig["type"]
        is_n2p     = "負轉正" in sig_type
        is_p2n     = "正轉負" in sig_type

        try:
            gap = (datetime.datetime.strptime(sig["entry_date"], "%Y-%m-%d") -
                   datetime.datetime.strptime(prev_exit_date, "%Y-%m-%d")).days
            idle_days += max(0, gap)
        except: pass

        chg = sig["price_chg"] / 100.0

        if logic == "long":
            if is_n2p:
                ret = chg
            else:
                equity_curve.append({"date": sig["entry_date"], "val": cash})
                equity_curve.append({"date": sig["exit_date"],  "val": cash})
                prev_exit_date = sig["exit_date"]
                continue
        else:
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

def moving_average_bands(closes, window):
    """
    均線五線譜：中線（簡單移動平均）± 1標準差 ± 2標準差
    回傳 dict，每個 key 對應一條線（長度與 closes 相同，前面不足視窗的部分為 None）
    """
    n = len(closes)
    ma     = [None] * n
    upper1 = [None] * n
    lower1 = [None] * n
    upper2 = [None] * n
    lower2 = [None] * n
    for i in range(window-1, n):
        sub = closes[i-window+1:i+1]
        m = sum(sub) / window
        var = sum((v-m)**2 for v in sub) / window
        sd = math.sqrt(var)
        ma[i]     = m
        upper1[i] = m + sd
        lower1[i] = m - sd
        upper2[i] = m + 2*sd
        lower2[i] = m - 2*sd
    return {"ma":ma, "upper1":upper1, "lower1":lower1, "upper2":upper2, "lower2":lower2}

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

        # 判斷預測是否正確（原方法：持有到下次反轉，股價方向）
        if sig_type == "負轉正":
            correct = price_chg > 0
        else:
            correct = price_chg < 0

        # 新判斷方法：訊號當天收盤價 vs T+1、T+2 收盤價平均
        # 負轉正：當天價格 < T+1,T+2平均 → 正確（之後漲）
        # 正轉負：當天價格 > T+1,T+2平均 → 正確（之後跌）
        correct_t2 = None
        if i + 2 < N:
            avg_t1t2 = (closes[i+1] + closes[i+2]) / 2
            if sig_type == "負轉正":
                correct_t2 = closes[i] < avg_t1t2
            else:
                correct_t2 = closes[i] > avg_t1t2

        signals.append({
            "type":             sig_type,
            "entry_date":       entry_date,
            "exit_date":        exit_date,
            "entry_price":      entry_price,
            "exit_price":       exit_price,
            "duration":         duration,
            "price_chg":        price_chg,
            "correct":          correct,
            "correct_t2":       correct_t2,
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
        if not lst: return {"count":0,"correct_rate":0,"avg_chg":0,"avg_days":0,"correct_rate_t2":0,"count_t2":0}
        correct = sum(1 for s in lst if s["correct"])
        lst_t2 = [s for s in lst if s.get("correct_t2") is not None]
        correct_t2 = sum(1 for s in lst_t2 if s["correct_t2"])
        return {
            "count":           len(lst),
            "correct_rate":    round(correct / len(lst) * 100),
            "avg_chg":         round(sum(s["price_chg"] for s in lst) / len(lst), 2),
            "avg_days":        round(sum(s["duration"]  for s in lst) / len(lst), 1),
            "correct_rate_t2": round(correct_t2 / len(lst_t2) * 100) if lst_t2 else 0,
            "count_t2":        len(lst_t2),
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

def simulate_trading(signals, logic, capital, min_duration=1):
    cash = float(capital)
    trades = []
    equity_curve = []
    peak = cash
    max_dd = 0.0
    idle_days = 0
    prev_exit_date = signals[0]["entry_date"] if signals else None

    for sig in signals:
        dur = sig["duration"]
        if dur < min_duration:
            continue

        sig_type   = sig["type"]
        is_n2p     = "負轉正" in sig_type
        is_p2n     = "正轉負" in sig_type

        try:
            gap = (datetime.datetime.strptime(sig["entry_date"], "%Y-%m-%d") -
                   datetime.datetime.strptime(prev_exit_date, "%Y-%m-%d")).days
            idle_days += max(0, gap)
        except: pass

        chg = sig["price_chg"] / 100.0

        if logic == "long":
            if is_n2p:
                ret = chg
            else:
                equity_curve.append({"date": sig["entry_date"], "val": cash})
                equity_curve.append({"date": sig["exit_date"],  "val": cash})
                prev_exit_date = sig["exit_date"]
                continue
        else:
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

def moving_average_bands(closes, window):
    """
    均線五線譜：中線（簡單移動平均）± 1標準差 ± 2標準差
    回傳 dict，每個 key 對應一條線（長度與 closes 相同，前面不足視窗的部分為 None）
    """
    n = len(closes)
    ma     = [None] * n
    upper1 = [None] * n
    lower1 = [None] * n
    upper2 = [None] * n
    lower2 = [None] * n
    for i in range(window-1, n):
        sub = closes[i-window+1:i+1]
        m = sum(sub) / window
        var = sum((v-m)**2 for v in sub) / window
        sd = math.sqrt(var)
        ma[i]     = m
        upper1[i] = m + sd
        lower1[i] = m - sd
        upper2[i] = m + 2*sd
        lower2[i] = m - 2*sd
    return {"ma":ma, "upper1":upper1, "lower1":lower1, "upper2":upper2, "lower2":lower2}

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

        # 判斷預測是否正確（原方法：持有到下次反轉，股價方向）
        if sig_type == "負轉正":
            correct = price_chg > 0
        else:
            correct = price_chg < 0

        # 新判斷方法：訊號當天收盤價 vs T+1、T+2 收盤價平均
        # 負轉正：當天價格 < T+1,T+2平均 → 正確（之後漲）
        # 正轉負：當天價格 > T+1,T+2平均 → 正確（之後跌）
        correct_t2 = None
        if i + 2 < N:
            avg_t1t2 = (closes[i+1] + closes[i+2]) / 2
            if sig_type == "負轉正":
                correct_t2 = closes[i] < avg_t1t2
            else:
                correct_t2 = closes[i] > avg_t1t2

        signals.append({
            "type":             sig_type,
            "entry_date":       entry_date,
            "exit_date":        exit_date,
            "entry_price":      entry_price,
            "exit_price":       exit_price,
            "duration":         duration,
            "price_chg":        price_chg,
            "correct":          correct,
            "correct_t2":       correct_t2,
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
        if not lst: return {"count":0,"correct_rate":0,"avg_chg":0,"avg_days":0,"correct_rate_t2":0,"count_t2":0}
        correct = sum(1 for s in lst if s["correct"])
        lst_t2 = [s for s in lst if s.get("correct_t2") is not None]
        correct_t2 = sum(1 for s in lst_t2 if s["correct_t2"])
        return {
            "count":           len(lst),
            "correct_rate":    round(correct / len(lst) * 100),
            "avg_chg":         round(sum(s["price_chg"] for s in lst) / len(lst), 2),
            "avg_days":        round(sum(s["duration"]  for s in lst) / len(lst), 1),
            "correct_rate_t2": round(correct_t2 / len(lst_t2) * 100) if lst_t2 else 0,
            "count_t2":        len(lst_t2),
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

def simulate_trading(signals, logic, capital, min_duration=1):
    cash = float(capital)
    trades = []
    equity_curve = []
    peak = cash
    max_dd = 0.0
    idle_days = 0
    prev_exit_date = signals[0]["entry_date"] if signals else None

    for sig in signals:
        dur = sig["duration"]
        if dur < min_duration:
            continue

        sig_type   = sig["type"]
        is_n2p     = "負轉正" in sig_type
        is_p2n     = "正轉負" in sig_type

        try:
            gap = (datetime.datetime.strptime(sig["entry_date"], "%Y-%m-%d") -
                   datetime.datetime.strptime(prev_exit_date, "%Y-%m-%d")).days
            idle_days += max(0, gap)
        except: pass

        chg = sig["price_chg"] / 100.0

        if logic == "long":
            if is_n2p:
                ret = chg
            else:
                equity_curve.append({"date": sig["entry_date"], "val": cash})
                equity_curve.append({"date": sig["exit_date"],  "val": cash})
                prev_exit_date = sig["exit_date"]
                continue
        else:
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

def moving_average_bands(closes, window):
    """
    均線五線譜：中線（簡單移動平均）± 1標準差 ± 2標準差
    回傳 dict，每個 key 對應一條線（長度與 closes 相同，前面不足視窗的部分為 None）
    """
    n = len(closes)
    ma     = [None] * n
    upper1 = [None] * n
    lower1 = [None] * n
    upper2 = [None] * n
    lower2 = [None] * n
    for i in range(window-1, n):
        sub = closes[i-window+1:i+1]
        m = sum(sub) / window
        var = sum((v-m)**2 for v in sub) / window
        sd = math.sqrt(var)
        ma[i]     = m
        upper1[i] = m + sd
        lower1[i] = m - sd
        upper2[i] = m + 2*sd
        lower2[i] = m - 2*sd
    return {"ma":ma, "upper1":upper1, "lower1":lower1, "upper2":upper2, "lower2":lower2}

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

        # 判斷預測是否正確（原方法：持有到下次反轉，股價方向）
        if sig_type == "負轉正":
            correct = price_chg > 0
        else:
            correct = price_chg < 0

        # 新判斷方法：訊號當天收盤價 vs T+1、T+2 收盤價平均
        # 負轉正：當天價格 < T+1,T+2平均 → 正確（之後漲）
        # 正轉負：當天價格 > T+1,T+2平均 → 正確（之後跌）
        correct_t2 = None
        if i + 2 < N:
            avg_t1t2 = (closes[i+1] + closes[i+2]) / 2
            if sig_type == "負轉正":
                correct_t2 = closes[i] < avg_t1t2
            else:
                correct_t2 = closes[i] > avg_t1t2

        signals.append({
            "type":             sig_type,
            "entry_date":       entry_date,
            "exit_date":        exit_date,
            "entry_price":      entry_price,
            "exit_price":       exit_price,
            "duration":         duration,
            "price_chg":        price_chg,
            "correct":          correct,
            "correct_t2":       correct_t2,
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
        if not lst: return {"count":0,"correct_rate":0,"avg_chg":0,"avg_days":0,"correct_rate_t2":0,"count_t2":0}
        correct = sum(1 for s in lst if s["correct"])
        lst_t2 = [s for s in lst if s.get("correct_t2") is not None]
        correct_t2 = sum(1 for s in lst_t2 if s["correct_t2"])
        return {
            "count":           len(lst),
            "correct_rate":    round(correct / len(lst) * 100),
            "avg_chg":         round(sum(s["price_chg"] for s in lst) / len(lst), 2),
            "avg_days":        round(sum(s["duration"]  for s in lst) / len(lst), 1),
            "correct_rate_t2": round(correct_t2 / len(lst_t2) * 100) if lst_t2 else 0,
            "count_t2":        len(lst_t2),
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

def simulate_trading(signals, logic, capital, min_duration=1):
    cash = float(capital)
    trades = []
    equity_curve = []
    peak = cash
    max_dd = 0.0
    idle_days = 0
    prev_exit_date = signals[0]["entry_date"] if signals else None

    for sig in signals:
        dur = sig["duration"]
        if dur < min_duration:
            continue

        sig_type   = sig["type"]
        is_n2p     = "負轉正" in sig_type
        is_p2n     = "正轉負" in sig_type

        try:
            gap = (datetime.datetime.strptime(sig["entry_date"], "%Y-%m-%d") -
                   datetime.datetime.strptime(prev_exit_date, "%Y-%m-%d")).days
            idle_days += max(0, gap)
        except: pass

        chg = sig["price_chg"] / 100.0

        if logic == "long":
            if is_n2p:
                ret = chg
            else:
                equity_curve.append({"date": sig["entry_date"], "val": cash})
                equity_curve.append({"date": sig["exit_date"],  "val": cash})
                prev_exit_date = sig["exit_date"]
                continue
        else:
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

def check_slope_alerts(window=5):
    end_dt = datetime.datetime.now(tz=timezone.utc)
    start_dt = end_dt - datetime.timedelta(days=30)
    alerts = []
    for ticker in WATCH_TICKERS:
        try:
            _, closes, _, _o = fetch_yahoo_range(ticker, start_dt, end_dt, "1d")
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
    """每次迴圈重新讀取 SCHEDULE_HOURS_TW，支援動態更改發送時間。"""
    while True:
        send_hours_utc = sorted([(h - 8) % 24 for h in SCHEDULE_HOURS_TW])

        now_utc = datetime.datetime.now(tz=timezone.utc)

        next_send = None
        for h in send_hours_utc:
            cand = now_utc.replace(hour=h, minute=0, second=0, microsecond=0)
            if cand > now_utc:
                next_send = cand
                break
        if next_send is None:
            next_send = (now_utc + datetime.timedelta(days=1)).replace(
                hour=send_hours_utc[0], minute=0, second=0, microsecond=0)

        wait_sec = (next_send - now_utc).total_seconds()
        tw_next  = (next_send + datetime.timedelta(hours=8)).strftime("%H:%M")
        tw_hours = " / ".join([f"{(h+8)%24:02d}:00" for h in send_hours_utc])
        print(f"[排程] 發送時間：{tw_hours}（台灣）　下次：{tw_next}　等待 {wait_sec/3600:.1f}h")
        threading.Event().wait(wait_sec)

        try:
            send_current_slope_auto()
        except Exception as e:
            print(f"[排程] 發送失敗：{e}")

        try:
            check_slope_alerts()
            check_asia_signal()
        except Exception as e:
            print(f"[排程] 檢查失敗：{e}")

def slope_block(ticker, closes, window):
    """單一股票斜率分析，回傳 LINE 訊息 block"""
    slopes = rolling_annualized_log_slope(closes, window)
    valid  = [s for s in slopes if not math.isnan(s)]
    if len(valid) < 2:
        return f"⚪ {ticker}：資料不足"
    prev_slope, last_slope = valid[-2], valid[-1]
    price = closes[-1]
    flattening = (last_slope < 0 and prev_slope < 0 and
                  abs(last_slope) < abs(prev_slope))
    if prev_slope < 0 and last_slope > 0:
        return (f"╔══════════════════╗\n⚡ {ticker} 負轉正！\n"
                f"斜率：{prev_slope:.1f}% → +{last_slope:.1f}%\n"
                f"現價：${price:.2f}\n👉 可考慮買入\n╚══════════════════╝")
    elif prev_slope > 0 and last_slope < 0:
        return (f"╔══════════════════╗\n🔻 {ticker} 正轉負！\n"
                f"斜率：+{prev_slope:.1f}% → {last_slope:.1f}%\n"
                f"現價：${price:.2f}\n👉 可考慮減倉\n╚══════════════════╝")
    elif flattening:
        return (f"╔══════════════════╗\n📊 {ticker} 跌勢趨緩！\n"
                f"斜率：{prev_slope:.1f}% → {last_slope:.1f}%\n"
                f"（負斜率縮小，下跌動能減弱）\n"
                f"現價：${price:.2f}\n👉 可考慮分批布局\n╚══════════════════╝")
    elif last_slope > 100:
        return f"🚀 {ticker}｜斜率 +{last_slope:.1f}%｜${price:.2f}\n強勁多頭，持有或加碼"
    elif last_slope > 0:
        return f"📈 {ticker}｜斜率 +{last_slope:.1f}%｜${price:.2f}\n動能向上，持續觀察"
    elif last_slope > -30:
        return f"➡️ {ticker}｜斜率 {last_slope:.1f}%｜${price:.2f}\n盤整偏弱，等待方向確認"
    elif last_slope > -100:
        return f"📉 {ticker}｜斜率 {last_slope:.1f}%｜${price:.2f}\n動能走弱，建議觀望"
    else:
        return f"🔴 {ticker}｜斜率 {last_slope:.1f}%｜${price:.2f}\n下跌趨勢，空手等待"

def asia_slope_block(display, closes, window):
    """亞洲指數用：斜率 + 5日漲幅 + 20日新高"""
    base = slope_block(display, closes, window)
    # 5日漲幅
    ret5 = round((closes[-1] - closes[-6]) / closes[-6] * 100, 1) if len(closes) >= 6 else None
    # 20日新高
    if len(closes) >= 20:
        high20 = max(closes[-20:-1])
        new_high = closes[-1] >= high20
        if new_high:
            nh_str = "20日新高 ✅"
        else:
            gap = round((closes[-1] - high20) / high20 * 100, 1)
            nh_str = f"20日新高 ❌（距高點 {gap:+.1f}%）"
    else:
        nh_str = "20日新高 —"
    ret5_str = f"5日漲幅 {ret5:+.1f}%" if ret5 is not None else "5日漲幅 —"
    extra = f"　{ret5_str}　｜　{nh_str}"
    return base + "\n" + extra

def build_option_line_msg(ticker):
    """產生期權結構的 LINE 訊息文字"""
    try:
        import yfinance as yf
        import math as _math
        t    = yf.Ticker(ticker)
        spot = t.fast_info.last_price
        exps = t.options
        if not exps: return None
        today     = datetime.date.today()
        exp_data  = []
        all_puts  = {}
        all_calls = {}
        # 跳過今天或已過期的到期日，取未來2個有效到期日
        valid_exps = [e for e in exps if datetime.date.fromisoformat(e) > today]
        for e in valid_exps[:2]:
            ed   = datetime.date.fromisoformat(e)
            days = (ed - today).days
            try:
                chain = t.option_chain(e)
                puts  = chain.puts[["strike","openInterest"]].dropna()
                calls = chain.calls[["strike","openInterest"]].dropna()
                for _, row in puts.iterrows():
                    all_puts[row["strike"]]  = all_puts.get(row["strike"],0)  + row["openInterest"]
                for _, row in calls.iterrows():
                    all_calls[row["strike"]] = all_calls.get(row["strike"],0) + row["openInterest"]
                exp_data.append({
                    "exp": e, "days": days,
                    "is_gamma": ed.weekday() in [2,4],
                    "is_opex":  ed.weekday() == 4,
                    "top_puts":  puts.nlargest(3,"openInterest")[["strike","openInterest"]].values.tolist(),
                    "top_calls": calls.nlargest(3,"openInterest")[["strike","openInterest"]].values.tolist(),
                })
            except: continue

        top_put_strikes  = sorted(all_puts,  key=lambda k: all_puts[k],  reverse=True)[:3]
        top_call_strikes = sorted(all_calls, key=lambda k: all_calls[k], reverse=True)[:3]
        support  = [(k, all_puts[k])  for k in top_put_strikes  if k < spot]
        resist   = [(k, all_calls[k]) for k in top_call_strikes if k > spot]

        main_sup = support[0] if support else None
        main_res = resist[0]  if resist  else None

        tw_time = datetime.datetime.now(
            datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")

        # 建議
        sug = ""
        if main_sup and main_res:
            d_sup = (spot - main_sup[0]) / spot * 100
            d_res = (main_res[0] - spot) / spot * 100
            if d_sup <= 2:
                sug = f"🟢 接近支撐 ${main_sup[0]:.0f}（差 {d_sup:.1f}%），可考慮買入"
            elif d_res <= 2:
                sug = f"🔴 接近阻力 ${main_res[0]:.0f}（差 {d_res:.1f}%），可考慮減倉"
            else:
                sug = f"⚪ 區間中段　支撐 ${main_sup[0]:.0f}（-{d_sup:.1f}%）　阻力 ${main_res[0]:.0f}（+{d_res:.1f}%）"
        elif main_sup:
            sug = f"⚪ 支撐 ${main_sup[0]:.0f}，暫無明顯上方阻力"
        elif main_res:
            sug = f"⚪ 阻力 ${main_res[0]:.0f}，暫無明顯下方支撐"
        else:
            sug = "⚪ 期權數據不足，無法判斷支撐阻力"

        # 歷史追蹤
        put_note, call_note = track_option_wall(
            ticker, main_sup[0] if main_sup else 0, main_res[0] if main_res else 0)

        # 跨月雙重確認
        def dual(strike, side):
            key = "top_puts" if side=="put" else "top_calls"
            return sum(1 for ed in exp_data if strike in [r[0] for r in ed[key]]) >= 2

        dual_puts  = [(k,v) for k,v in [(k,all_puts[k])  for k in top_put_strikes  if k<spot]  if dual(k,"put")]  if exp_data else []
        dual_calls = [(k,v) for k,v in [(k,all_calls[k]) for k in top_call_strikes if k>spot] if dual(k,"call")] if exp_data else []

        put_str  = "　".join([f"${k:.0f}(✅雙月)" for k,v in dual_puts[:2]]  or [f"${k:.0f}" for k,v in support[:2]])
        call_str = "　".join([f"${k:.0f}(✅雙月)" for k,v in dual_calls[:2]] or [f"${k:.0f}" for k,v in resist[:2]])

        next_exp = exp_data[0] if exp_data else None
        exp_line = ""
        if next_exp:
            tags = ("⚠️Gamma高峰" if next_exp["is_gamma"] else "") + ("　📅月結算" if next_exp["is_opex"] else "")
            exp_line = f"\n📅 結算日 {next_exp['exp']}（{next_exp['days']}天後）{tags}"

        msg = (f"【⚡ {ticker} 期權結構】\n{tw_time}\n\n"
               f"📍 現價：${spot:.2f}\n"
               f"{sug}\n\n"
               f"🔗 Put牆：{put_str or '—'}\n"
               f"🔗 Call牆：{call_str or '—'}\n")
        if put_note:  msg += f"{put_note}\n"
        if call_note: msg += f"{call_note}\n"
        msg += exp_line
        return msg
    except Exception as e:
        return f"【期權分析失敗】{e}"


def send_current_slope_auto():
    """自動排程呼叫的斜率報告"""
    end_dt   = datetime.datetime.now(tz=timezone.utc)
    start_dt = end_dt - datetime.timedelta(days=30)
    window   = 5
    tw_time  = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")

    if "slope" in SCHEDULE_CONTENT:
        blocks = [f"【定時斜率報告】\n{tw_time} 台灣時間"]
        blocks.append("🇺🇸 ── 美股 ──")
        for ticker in WATCH_TICKERS:
            try:
                _, closes, _, _o = fetch_yahoo_range(ticker, start_dt, end_dt, "1d")
                blocks.append(slope_block(ticker, closes, window))
            except Exception as e:
                blocks.append(f"⚪ {ticker}：錯誤 {e}")
        blocks.append("🇹🇼 ── 亞洲 ──")
        for ticker in WATCH_TICKERS_TW + WATCH_TICKERS_ASIA:
            try:
                _, closes, _, _o = fetch_yahoo_range(ticker, start_dt, end_dt, "1d")
                display = TICKER_NAMES.get(ticker, ticker)
                blocks.append(asia_slope_block(display, closes, window))
            except Exception as e:
                blocks.append(f"⚪ {TICKER_NAMES.get(ticker,ticker)}：錯誤 {e}")
        try:
            vd = fetch_vix(start_dt, end_dt)
            if vd:
                vix_val   = vd[sorted(vd.keys())[-1]]
                vix_emoji = "🔴" if vix_val > 30 else "🟡" if vix_val > 20 else "🟢"
                vix_label = "高恐懼" if vix_val > 30 else "中性" if vix_val > 20 else "低恐懼"
                blocks.append(f"────────────────\n{vix_emoji} VIX {vix_val:.1f}｜{vix_label}\n────────────────")
        except: pass
        send_line("\n\n".join(blocks))
        print(f"[排程] 已發送斜率報告 {tw_time}")

    if "option" in SCHEDULE_CONTENT:
        try:
            msg = build_option_line_msg(SCHEDULE_OPT_TICKER)
            if msg:
                send_line(msg)
                print(f"[排程] 已發送期權報告 {tw_time}")
        except Exception as e:
            print(f"[排程] 期權報告失敗：{e}")


def check_asia_signal(window=20):
    """
    台股創20日新高，但日股+韓股未創20日新高
    → 資金回流美國訊號，發 LINE 通知
    """
    end_dt   = datetime.datetime.now(tz=timezone.utc)
    start_dt = end_dt - datetime.timedelta(days=60)
    indices  = {"台股":("^TWII","🇹🇼"), "日股":("^N225","🇯🇵"), "韓股":("^KS11","🇰🇷")}
    data = {}
    for name, (sym, flag) in indices.items():
        try:
            _, closes, _, _ = fetch_yahoo_range(sym, start_dt, end_dt, "1d")
            if len(closes) >= window:
                data[name] = closes
        except: pass

    if "台股" not in data: return

    tw   = data["台股"]
    tw_high20 = max(tw[-window:-1])  # 前20日最高（不含今日）
    tw_today  = tw[-1]
    tw_new_high = tw_today >= tw_high20

    if not tw_new_high:
        return  # 台股沒創新高，不觸發

    # 檢查日韓
    results = {}
    for name in ["日股", "韓股"]:
        if name not in data:
            results[name] = None
            continue
        closes = data[name]
        high20 = max(closes[-window:-1])
        today  = closes[-1]
        results[name] = today >= high20

    jp_new_high = results.get("日股")
    kr_new_high = results.get("韓股")

    # 台股創高 + 日韓都沒創高 → 資金回流訊號
    if tw_new_high and jp_new_high is False and kr_new_high is False:
        msg = (
            f"🚨 【資金回流美國訊號】\n"
            f"台股今日創 {window} 日新高（{tw_today:.0f}）\n"
            f"🇯🇵 日股未創新高（{data['日股'][-1]:.0f}）\n"
            f"🇰🇷 韓股未創新高（{data['韓股'][-1]:.0f}）\n\n"
            f"⚠️ 資金可能回流美國，QQQ 或有最後一波上漲"
        )
        send_line(msg)
        return msg
    return None

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
            dcc.Input(id="ticker",value="QQQ, VOO, ^SOX",type="text",
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
        html.Button("📤 發送期權結構",id="send-opt-now-btn",n_clicks=0,
                    style={"alignSelf":"flex-end","padding":"8px 16px","background":"#7c3aed",
                           "color":"#fff","border":"none","borderRadius":"6px","cursor":"pointer","fontSize":"13px"}),
    ],style={"display":"flex","flexWrap":"wrap","gap":"16px","alignItems":"flex-end",
             "background":"#f5f5f3","borderRadius":"10px","padding":"14px 16px","marginBottom":"8px"}),
    html.Div(id="send-opt-now-msg",style={"fontSize":"13px","color":"#7c3aed","minHeight":"20px","marginBottom":"2px","fontFamily":"sans-serif"}),

    html.Div([
        html.Label("⏰ 定時發送設定（台灣時間）",
                   style={"fontSize":"12px","color":"#888","marginRight":"12px","alignSelf":"center","whiteSpace":"nowrap"}),
        html.Div([
            html.Label("第一次：",style={"fontSize":"12px","color":"#888","alignSelf":"center"}),
            dcc.Dropdown(
                id="schedule-time-1",
                options=[{"label":f"{h:02d}:00","value":h} for h in range(0,24)],
                value=20, clearable=False,
                style={"width":"90px","fontSize":"13px"},
            ),
        ], style={"display":"flex","alignItems":"center","gap":"6px"}),
        html.Div([
            html.Label("第二次：",style={"fontSize":"12px","color":"#888","alignSelf":"center"}),
            dcc.Dropdown(
                id="schedule-time-2",
                options=[{"label":f"{h:02d}:00","value":h} for h in range(0,24)],
                value=22, clearable=False,
                style={"width":"90px","fontSize":"13px"},
            ),
        ], style={"display":"flex","alignItems":"center","gap":"6px"}),
        html.Div([
            html.Label("發送內容：",style={"fontSize":"12px","color":"#888","alignSelf":"center"}),
            dcc.Checklist(
                id="schedule-content",
                options=[
                    {"label":" 斜率", "value":"slope"},
                    {"label":" 期權", "value":"option"},
                ],
                value=["slope"],
                inline=True,
                style={"fontSize":"12px"},
                inputStyle={"marginRight":"3px","marginLeft":"8px"},
            ),
        ], style={"display":"flex","alignItems":"center","gap":"4px"}),
        html.Div([
            html.Label("期權標的：",style={"fontSize":"12px","color":"#888","alignSelf":"center"}),
            dcc.Input(id="schedule-opt-ticker", value="QQQ",
                      style={"width":"80px","fontSize":"12px","padding":"3px 6px",
                             "border":"1px solid #ddd","borderRadius":"4px"}),
        ], id="schedule-opt-ticker-div",
           style={"display":"flex","alignItems":"center","gap":"4px"}),
        html.Button("✅ 套用",id="schedule-apply-btn",n_clicks=0,
                    style={"padding":"6px 14px","background":"#f5f5f3","border":"1px solid #ddd",
                           "borderRadius":"6px","cursor":"pointer","fontSize":"12px"}),
        html.Span(id="schedule-msg",style={"fontSize":"12px","color":"#0F6E56","alignSelf":"center"}),
    ], style={"display":"flex","alignItems":"center","gap":"10px","background":"#f0f9ff",
              "borderRadius":"10px","padding":"10px 16px","marginBottom":"8px",
              "border":"0.5px solid #bae6fd","flexWrap":"wrap"}),

    html.Div([
        html.Label("時間週期",style={"fontSize":"12px","color":"#888","marginRight":"10px","alignSelf":"center"}),
        dcc.RadioItems(
            id="interval-picker",
            options=[
                {"label":"　每週", "value":"1wk"},
                {"label":"　每月（近5年）", "value":"1mo"},
                {"label":"　每日（可用滑桿）", "value":"1d"},
            ],
            value="1d",
            inline=True,
            style={"fontSize":"13px","color":"#333","gap":"16px"},
            inputStyle={"marginRight":"4px"},
        ),

    ],style={"display":"flex","alignItems":"center","background":"#f0f4ff",
             "borderRadius":"10px","padding":"10px 16px","marginBottom":"12px",
             "border":"0.5px solid #c7d9f5","flexWrap":"wrap","gap":"8px"}),

    html.Div(id="test-msg",style={"fontSize":"13px","color":"#06c755","minHeight":"20px","marginBottom":"2px","fontFamily":"sans-serif"}),
    html.Div(id="slope-msg",style={"fontSize":"13px","color":"#2563eb","minHeight":"20px","marginBottom":"4px","fontFamily":"sans-serif"}),

    html.Div(id="slider-container", children=[
        html.Label(id="slider-label",style={"fontSize":"12px","color":"#888","marginBottom":"6px","display":"block"}),
        dcc.Slider(id="days-slider",min=30,max=548,step=30,value=365,
                   marks={30:"1個月",90:"3個月",180:"6個月",365:"1年",548:"1.5年"},
                   tooltip={"placement":"bottom","always_visible":False}),
    ],style={"background":"#f5f5f3","borderRadius":"10px","padding":"14px 20px 18px","marginBottom":"16px"}),

    html.Div(id="status-msg",style={"fontSize":"13px","color":"#888","minHeight":"20px","marginBottom":"8px","fontFamily":"sans-serif"}),

    dcc.Loading(
        id="loading-tab",
        type="circle",
        children=html.Div(id="tab-content"),
        color="#2563eb",
    ),

    html.H3("📊 六狀態組合分析",style={"fontFamily":"sans-serif","marginTop":"32px","marginBottom":"4px","fontSize":"16px"}),
    html.P("L1（月線）× L2（週線）× L3（日線）六狀態組合，統計歷史上各組合進場後的平均報酬和勝率",
           style={"fontSize":"12px","color":"#aaa","marginBottom":"8px","fontFamily":"sans-serif"}),
    html.Div([
        html.Label("分析股票：",style={"fontSize":"12px","color":"#888","alignSelf":"center"}),
        dcc.Input(id="six-state-ticker", value="QQQ", type="text",
                  style={"width":"100px","fontSize":"13px","padding":"4px 8px",
                         "border":"1px solid #ddd","borderRadius":"6px"}),
        html.Button("🔍 開始分析", id="six-state-btn", n_clicks=0,
                    style={"padding":"6px 16px","background":"#1a1a1a","color":"#fff",
                           "border":"none","borderRadius":"6px","cursor":"pointer",
                           "fontSize":"13px","marginLeft":"8px"}),
    ], style={"display":"flex","gap":"8px","alignItems":"center","marginBottom":"12px","flexWrap":"wrap"}),
    html.Div(id="six-state-div"),

    html.H3("恐懼指標",style={"fontFamily":"sans-serif","marginTop":"32px","marginBottom":"4px","fontSize":"16px"}),
    html.P(id="fear-status",style={"fontSize":"12px","color":"#aaa","marginBottom":"8px","fontFamily":"sans-serif"}),
    html.Div(id="fear-cards"),
    html.P("VIX >30 為高恐懼區。Fear & Greed Index：0=極度恐懼，100=極度貪婪（加密市場版）。",
           style={"fontSize":"12px","color":"#aaa","marginTop":"12px","fontFamily":"sans-serif"}),

    html.H3("🌏 亞洲資金回流訊號",style={"fontFamily":"sans-serif","marginTop":"32px","marginBottom":"4px","fontSize":"16px"}),
    html.P("台股創N日新高，但日股+韓股未創新高 → 資金可能回流美國，QQQ最後一波",
           style={"fontSize":"12px","color":"#aaa","marginBottom":"8px","fontFamily":"sans-serif"}),
    html.Div([
        html.Label("觀察天數：", style={"fontSize":"13px","color":"#888","alignSelf":"center","marginRight":"8px"}),
        dcc.Dropdown(
            id="asia-window",
            options=[{"label":f"{n}日新高","value":n} for n in [5,10,20,60,120,250]],
            value=20,
            clearable=False,
            style={"width":"120px","fontSize":"13px","display":"inline-block"},
        ),
        html.Button("🔍 立即檢查", id="asia-btn",
                    style={"padding":"8px 18px","fontSize":"13px","background":"#f5f5f3",
                           "border":"1px solid #ddd","borderRadius":"8px","cursor":"pointer",
                           "marginLeft":"10px"}),
    ], style={"display":"flex","alignItems":"center","marginBottom":"12px","gap":"4px"}),
    html.Div(id="asia-signal-div"),

    html.H3("⚡ 期權結構分析",style={"fontFamily":"sans-serif","marginTop":"32px","marginBottom":"4px","fontSize":"16px"}),
    html.P("Put/Call牆、Gamma Exposure、結算日分析，協助判斷支撐阻力與買賣建議",
           style={"fontSize":"12px","color":"#aaa","marginBottom":"8px","fontFamily":"sans-serif"}),
    html.Div([
        html.Label("標的：",style={"fontSize":"12px","color":"#888","alignSelf":"center"}),
        dcc.Input(id="opt-ticker", value="QQQ", type="text",
                  style={"width":"90px","fontSize":"13px","padding":"4px 8px",
                         "border":"1px solid #ddd","borderRadius":"6px"}),
        html.Button("📊 分析期權結構", id="opt-btn", n_clicks=0,
                    style={"padding":"8px 16px","background":"#7c3aed","color":"#fff",
                           "border":"none","borderRadius":"6px","cursor":"pointer",
                           "fontSize":"13px","marginLeft":"8px"}),
    ], style={"display":"flex","gap":"8px","alignItems":"center","marginBottom":"12px"}),
    html.Div(id="opt-div"),

    html.H3("🇹🇼 台股月K篩選",style={"fontFamily":"sans-serif","marginTop":"32px","marginBottom":"4px","fontSize":"16px"}),
    html.P("台灣中型100成分股：6月紅K且收高於上月→買入觀察；6月黑K且低於5月均價→避開",
           style={"fontSize":"12px","color":"#aaa","marginBottom":"8px","fontFamily":"sans-serif"}),
    html.Button("🔍 執行台股月K篩選", id="tw-screen-btn",
                style={"padding":"8px 18px","fontSize":"13px","background":"#f5f5f3",
                       "border":"1px solid #ddd","borderRadius":"8px","cursor":"pointer",
                       "marginBottom":"12px","fontFamily":"sans-serif"}),
    html.Div(id="tw-screen-div"),

    html.H3("企業庫存數據",style={"fontFamily":"sans-serif","marginTop":"32px","marginBottom":"4px","fontSize":"16px"}),
    html.P("美國製造業與零售業庫存月度數據（FRED，季調，單位：百萬美元）",
           style={"fontSize":"12px","color":"#aaa","marginBottom":"8px","fontFamily":"sans-serif"}),
    html.Button("📦 顯示庫存數據", id="inventory-btn",
                style={"padding":"8px 18px","fontSize":"13px","background":"#f5f5f3",
                       "border":"1px solid #ddd","borderRadius":"8px","cursor":"pointer",
                       "marginBottom":"12px","fontFamily":"sans-serif"}),
    html.Div(id="inventory-charts"),

    html.H3("📋 指數總覽",style={"fontFamily":"sans-serif","marginTop":"32px","marginBottom":"4px","fontSize":"16px"}),
    html.P("主要指數與板塊 ETF 的高低點、季線、乖離率一覽",
           style={"fontSize":"12px","color":"#aaa","marginBottom":"8px","fontFamily":"sans-serif"}),
    html.Button("📋 載入指數總覽", id="index-btn",
                style={"padding":"8px 18px","fontSize":"13px","background":"#f5f5f3",
                       "border":"1px solid #ddd","borderRadius":"8px","cursor":"pointer",
                       "marginBottom":"12px","fontFamily":"sans-serif"}),
    html.Div(id="index-div"),

],style={"maxWidth":"1200px","margin":"2rem auto","padding":"0 1.5rem","fontFamily":"sans-serif"})

# ── Callbacks ────────────────────────────────────────────────

@app.callback(
    Output("slider-container","style"),
    Output("slider-label","children"),
    Input("interval-picker","value"),
    Input("days-slider","value"),
)
def update_slider(interval, days):
    end_dt = datetime.datetime.now(tz=timezone.utc)
    if interval == "1wk":
        style = {"display":"none"}
        label = "每週模式：自動抓最近 3 年"
    elif interval == "1mo":
        style = {"display":"none"}
        label = "每月模式：自動抓最近 5 年"
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
    if interval == "1wk":
        return "視窗（週）"
    elif interval == "1mo":
        return "視窗（月）"
    else:
        return "視窗（天）"

@app.callback(
    Output("schedule-msg","children"),
    Output("schedule-opt-ticker-div","style"),
    Input("schedule-apply-btn","n_clicks"),
    Input("schedule-content","value"),
    State("schedule-time-1","value"),
    State("schedule-time-2","value"),
    State("schedule-content","value"),
    State("schedule-opt-ticker","value"),
    prevent_initial_call=True,
)
def apply_schedule(n_clicks, content_val, t1, t2, content, opt_ticker):
    global SCHEDULE_HOURS_TW, SCHEDULE_CONTENT, SCHEDULE_OPT_TICKER
    # 顯示/隱藏期權標的欄位
    show_opt = "option" in (content_val or [])
    opt_style = {"display":"flex","alignItems":"center","gap":"4px"} if show_opt else {"display":"none"}

    from dash import ctx
    if not n_clicks or ctx.triggered_id != "schedule-apply-btn":
        return "", opt_style

    h1 = int(t1 or 20)
    h2 = int(t2 or 22)
    if h1 == h2:
        return "❌ 兩個時間不能相同", opt_style

    SCHEDULE_HOURS_TW   = sorted([h1, h2])
    SCHEDULE_CONTENT    = list(content or ["slope"])
    SCHEDULE_OPT_TICKER = (opt_ticker or "QQQ").strip().upper()

    content_label = "+".join(
        "斜率" if c=="slope" else f"期權({SCHEDULE_OPT_TICKER})"
        for c in SCHEDULE_CONTENT)
    return (f"✅ {SCHEDULE_HOURS_TW[0]:02d}:00 / {SCHEDULE_HOURS_TW[1]:02d}:00　"
            f"發送：{content_label}"), opt_style

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

    # 美股
    blocks.append("🇺🇸 ── 美股 ──")
    for ticker in WATCH_TICKERS:
        try:
            _, closes, _, _o = fetch_yahoo_range(ticker, start_dt, end_dt, "1d")
            blocks.append(slope_block(ticker, closes, window))
        except Exception as e:
            blocks.append(f"⚪ {ticker}：錯誤 {e}")

    # 台股 + 亞洲合併
    blocks.append("🇹🇼 ── 亞洲 ──")
    for ticker in WATCH_TICKERS_TW + WATCH_TICKERS_ASIA:
        try:
            _, closes, _, _o = fetch_yahoo_range(ticker, start_dt, end_dt, "1d")
            display = TICKER_NAMES.get(ticker, ticker)
            blocks.append(asia_slope_block(display, closes, window))
        except Exception as e:
            blocks.append(f"⚪ {TICKER_NAMES.get(ticker,ticker)}：錯誤 {e}")

    # VIX
    try:
        vd = fetch_vix(start_dt, end_dt)
        if vd:
            vix_val = vd[sorted(vd.keys())[-1]]
            vix_emoji = "🔴" if vix_val > 30 else "🟡" if vix_val > 20 else "🟢"
            vix_label = "高恐懼" if vix_val > 30 else "中性" if vix_val > 20 else "低恐懼"
            blocks.append(f"────────────────\n{vix_emoji} VIX {vix_val:.1f}｜{vix_label}\n────────────────")
    except: pass

    send_line("\n\n".join(blocks))
    return "✅ 斜率報告已發送到 LINE"


@app.callback(
    Output("send-opt-now-msg","children"),
    Input("send-opt-now-btn","n_clicks"),
    State("schedule-opt-ticker","value"),
    prevent_initial_call=True,
)
def send_opt_now(n_clicks, ticker_input):
    if not n_clicks:
        return ""
    ticker = (ticker_input or SCHEDULE_OPT_TICKER or "QQQ").strip().upper()
    try:
        msg = build_option_line_msg(ticker)
        if msg:
            send_line(msg)
            return f"✅ 已發送 {ticker} 期權結構到 LINE"
        return "❌ 無法產生期權訊息"
    except Exception as e:
        return f"❌ 發送失敗：{e}"


@app.callback(
    Output("tab-content","children"),
    Output("fear-cards","children"),
    Output("status-msg","children"),
    Output("fear-status","children"),
    Input("run-btn","n_clicks"),
    State("ticker","value"), State("window","value"),
    State("show-volume","value"), State("days-slider","value"),
    State("interval-picker","value"),
    prevent_initial_call=False,
)
def update_content(n_clicks, ticker_str, window, show_volume, days, interval):
    tickers  = [t.strip().upper() for t in (ticker_str or "QQQ").split(",") if t.strip()]
    window   = int(window or 5)
    show_vol = "vol" in (show_volume or [])
    days     = int(days or 365)
    interval = interval or "1d"
    end_dt   = datetime.datetime.now(tz=timezone.utc)

    # 根據週期決定時間範圍
    if interval == "1wk":
        start_dt         = end_dt - datetime.timedelta(days=365*3)  # 3年週線
        end_dt_1m        = end_dt
        annualize_factor = 52   # 一年約52週
        slope_label      = f"{window}週斜率(%)"
        date_range_str   = f"近3年（每週）"
    elif interval == "1mo":
        start_dt         = end_dt - datetime.timedelta(days=365*5)
        end_dt_1m        = end_dt
        annualize_factor = 12   # 一年12個月
        slope_label      = f"{window}月斜率(%)"
        date_range_str   = "近5年（每月）"
    else:
        start_dt         = end_dt - datetime.timedelta(days=days)
        end_dt_1m        = end_dt
        annualize_factor = 252
        slope_label      = f"{window}日年化斜率(%)"
        date_range_str   = f"{start_dt.strftime('%Y-%m-%d')} ～ {end_dt.strftime('%Y-%m-%d')}"

    ticker_data = {}
    messages = []
    for i, ticker in enumerate(tickers[:12]):
        try:
            fetch_end = end_dt_1m if interval == "1wk" else end_dt
            dates, closes, volumes, opens = fetch_yahoo_range(ticker, start_dt, fetch_end, interval)
            ticker_data[ticker] = {"dates":dates,"closes":closes,"volumes":volumes,"opens":opens,"color":COLORS[i%len(COLORS)]}
            messages.append(f"✅ {ticker} {len(dates)}日")
        except Exception as e:
            messages.append(f"❌ {ticker}: {e}")
            ticker_data[ticker] = None

    # ── 股價圖表 ──
    chart_divs = []
    if True:
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

            # 建立主圖（月/週/日三層斜率 + 收盤價）
            show_vol = "vol" in (show_volume or [])

            if interval == "1d":
                # 三層斜率
                s_mo  = rolling_annualized_log_slope(closes, min(120, len(closes)-1), 252)
                s_wk  = rolling_annualized_log_slope(closes, min(40,  len(closes)-1), 252)
                s_day = rolling_annualized_log_slope(closes, window, annualize_factor)
                def to_line(s):
                    return [None if math.isnan(v) else v for v in s]
                mo_line  = to_line(s_mo)
                wk_line  = to_line(s_wk)
                day_line = to_line(s_day)

                n_rows = 4 if show_vol else 4
                row_h  = [0.2, 0.2, 0.2, 0.4] if show_vol else [0.2, 0.2, 0.2, 0.4]
                specs  = [[{"secondary_y":False}]]*3 + [[{"secondary_y": True}]]
                titles = ("L1 月線斜率（120日）", "L2 週線斜率（40日）",
                          f"L3 日線斜率（{window}日）", "收盤價")

                fig = make_subplots(rows=4, cols=1, shared_xaxes=True,
                                    row_heights=row_h, vertical_spacing=0.03,
                                    specs=specs, subplot_titles=titles)
                chart_height = 620

                def add_slope_bars(sl, row, up_c, dn_c):
                    ps = [v if (v or 0)>0 else 0 for v in sl]
                    ns = [v if (v or 0)<0 else 0 for v in sl]
                    fig.add_trace(go.Bar(x=dates,y=ps,marker_color=up_c,showlegend=False,
                        hovertemplate="%{x}<br>%{y:.1f}%<extra></extra>"),row=row,col=1)
                    fig.add_trace(go.Bar(x=dates,y=ns,marker_color=dn_c,showlegend=False,
                        hovertemplate="%{x}<br>%{y:.1f}%<extra></extra>"),row=row,col=1)
                    fig.add_trace(go.Scatter(x=dates,y=sl,mode="lines",showlegend=False,
                        line=dict(color="#444",width=1),
                        hovertemplate="%{x}<br>%{y:.2f}%<extra></extra>"),row=row,col=1)
                    fig.add_hline(y=0,line_color="#ccc",line_width=1,row=row,col=1)

                add_slope_bars(mo_line,  1, "rgba(34,160,107,0.3)", "rgba(226,72,61,0.3)")
                add_slope_bars(wk_line,  2, "rgba(34,160,107,0.35)","rgba(226,72,61,0.35)")
                add_slope_bars(day_line, 3, "rgba(34,160,107,0.4)", "rgba(226,72,61,0.4)")

                # 收盤價（第4列）
                fig.add_trace(go.Scatter(x=dates,y=closes,name="收盤價",mode="lines",
                    line=dict(color=color,width=2),
                    hovertemplate="%{x}<br>收盤: $%{y:.2f}<extra></extra>"),
                    row=4,col=1,secondary_y=False)

                fig.update_layout(
                    title=dict(text=f"<b>{ticker}</b>　三層斜率　{date_range_str}",
                               font=dict(size=13,color="#1a1a1a"),x=0),
                    barmode="overlay",bargap=0,hovermode="x unified",
                    legend=dict(orientation="h",yanchor="bottom",y=1.04,xanchor="right",x=1,font=dict(size=11)),
                    plot_bgcolor="white",paper_bgcolor="white",
                    margin=dict(l=60,r=20,t=50,b=40),
                    font=dict(family="sans-serif",size=11),height=chart_height)
                fig.update_xaxes(showgrid=True,gridcolor="#eee")
                for row in [1,2,3]:
                    fig.update_yaxes(showgrid=True,gridcolor="#eee",zeroline=False,
                                     title_font=dict(size=9),row=row,col=1)
                fig.update_yaxes(title_text="收盤價",showgrid=True,gridcolor="#eee",
                                 zeroline=False,row=4,col=1,secondary_y=False)

            else:
                # 非每日模式：只顯示單一斜率圖
                if show_vol:
                    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                                        row_heights=[0.68,0.32], vertical_spacing=0.04,
                                        specs=[[{"secondary_y":True}],[{"secondary_y":False}]])
                    chart_height = 420
                else:
                    fig = make_subplots(rows=1, cols=1, specs=[[{"secondary_y":True}]])
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
                    title=dict(text=f"<b>{ticker}</b>　{slope_label}　{date_range_str}",
                               font=dict(size=13,color="#1a1a1a"),x=0),
                    barmode="overlay",bargap=0,hovermode="x unified",
                    legend=dict(orientation="h",yanchor="bottom",y=1.06,xanchor="right",x=1,font=dict(size=11)),
                    plot_bgcolor="white",paper_bgcolor="white",
                    margin=dict(l=60,r=65,t=50,b=40),
                    font=dict(family="sans-serif",size=12),height=chart_height)
                fig.update_xaxes(showgrid=True,gridcolor="#eee")
                fig.update_yaxes(title_text=slope_label,secondary_y=False,showgrid=True,gridcolor="#eee",
                                 zeroline=False,title_font=dict(size=10),row=1,col=1)
                fig.update_yaxes(title_text="收盤價(USD)",secondary_y=True,showgrid=False,zeroline=False,
                                 title_font=dict(size=10,color=color),tickfont=dict(color=color),row=1,col=1)
                if show_vol:
                    fig.update_yaxes(title_text="成交量",showgrid=True,gridcolor="#eee",zeroline=False,
                                     title_font=dict(size=10),tickformat=".2s",row=2,col=1)

            # 乖離率
            def ma_dev(c, n):
                if len(c) < n: return None
                ma = sum(c[-n:]) / n
                return round((c[-1] - ma) / ma * 100, 2)
            dev_specs = [("5日均",5,"日"),("20日均",20,"日"),("60日均",60,"日"),
                         ("5週均",25,"週"),("10週均",50,"週"),("3月均",60,"月"),("6月均",120,"月")]
            dev_cards = []
            for ma_name, n, period in dev_specs:
                dev = ma_dev(closes, n)
                if dev is None: continue
                dev_color = "#0F6E56" if dev >= 0 else "#A32D2D"
                dev_cards.append(html.Div([
                    html.Div(f"{ma_name}乖離", style={"fontSize":"10px","color":"#aaa"}),
                    html.Div(f"{dev:+.2f}%", style={"fontSize":"13px","fontWeight":"500","color":dev_color}),
                ], style={"textAlign":"center","padding":"6px 10px","background":"#f5f5f3",
                          "borderRadius":"6px","minWidth":"70px"}))

            # ── 三層多時間框架分析 ──
            if interval == "1d":
                try:
                    sig, res, _, err = multi_timeframe_strategy(ticker, end_dt, 100000)
                except Exception as mtf_ex:
                    sig, res, err = None, None, str(mtf_ex)

                # 偵錯：顯示原始結果
                debug_info = html.Div(
                    f"MTF結果 sig={sig is not None} res={res is not None} err={err}"
                    + (f" | 月={sig['mo_cur']} 週={sig['wk_cur']} 日={sig['day_cur']} 日前={sig['day_prev']} L1={sig['l1_ok']} L2={sig['l2_ok']} L3跌緩={sig['l3_ok']} L3負正={sig['l3_n2p']}" if sig else ""),
                    style={"fontSize":"11px","color":"#aaa","padding":"4px 12px"})

                if sig and res:
                    def lamp(ok, val, label):
                        color = "#0F6E56" if ok else "#A32D2D"
                        icon  = "🟢" if ok else "🔴"
                        return html.Div([
                            html.Div(label, style={"fontSize":"10px","color":"#aaa"}),
                            html.Span(icon, style={"fontSize":"18px"}),
                            html.Div(f"{val:+.1f}%" if val else "—",
                                     style={"fontSize":"12px","fontWeight":"500","color":color}),
                        ], style={"textAlign":"center","padding":"8px 12px",
                                  "background":"#f5f5f3","borderRadius":"8px","minWidth":"80px"})

                    # 判斷是否為最佳轉折點（跌勢擴大→趨緩）
                    day_prev2_val = None
                    if sig.get("day_prev") is not None and sig.get("day_cur") is not None:
                        # 用 s_day 最後三個有效值判斷
                        valid_days = [s for s in s_day if not math.isnan(s)] if 'multi_timeframe_strategy' else []

                    d_cur  = sig.get("day_cur")  or 0
                    d_prev = sig.get("day_prev") or 0
                    l3_flatten   = d_cur < 0 and d_prev < 0 and abs(d_cur) < abs(d_prev)
                    l3_expanding = d_cur < 0 and d_prev < 0 and abs(d_cur) > abs(d_prev)

                    l3_label = ("跌勢擴大→趨緩 🎯 最佳ALL IN！" if (sig["l3_ok"] and l3_expanding)
                                else "跌勢趨緩 📊" if sig["l3_ok"]
                                else "負轉正 ⚡" if sig["l3_n2p"]
                                else ("🚀 動能強勁" if d_cur > 100
                                      else "📈 動能向上") if sig.get("l3_pos")
                                else "跌勢擴大 📉")
                    all_ok = sig["l1_ok"] and sig["l2_ok"] and (sig["l3_ok"] or sig["l3_n2p"] or sig.get("l3_pos"))
                    signal_color = "#0F6E56" if all_ok else "#888"
                    ret_color = "#0F6E56" if res["total_ret"] >= 0 else "#A32D2D"
                    bah_color = "#0F6E56" if res["bah_ret"]   >= 0 else "#A32D2D"
                    beat = res["final_val"] > res["bah_final"]

                    eq_dates = [p["date"] for p in res["equity"]]
                    eq_vals  = [p["val"]  for p in res["equity"]]
                    fig_mtf = go.Figure()
                    fig_mtf.add_trace(go.Scatter(x=eq_dates, y=eq_vals, name="三層策略",
                        mode="lines", line=dict(color="#0F6E56", width=2),
                        hovertemplate="%{x}<br>$%{y:,.0f}<extra></extra>"))
                    bah_v = res["closes"][0]
                    date_to_idx = {d: i for i, d in enumerate(res["dates"])}
                    bah_curve = [100000 * (res["closes"][date_to_idx[d]] / bah_v)
                                 if d in date_to_idx else None for d in eq_dates]
                    fig_mtf.add_trace(go.Scatter(x=eq_dates, y=bah_curve, name="買入持有",
                        mode="lines", line=dict(color="rgba(136,136,136,0.6)", width=1.5, dash="dash"),
                        hovertemplate="%{x}<br>$%{y:,.0f}<extra></extra>"))
                    fig_mtf.update_layout(
                        title=dict(text="三層策略 vs 買入持有", font=dict(size=12), x=0),
                        hovermode="x unified", height=200,
                        plot_bgcolor="white", paper_bgcolor="white",
                        legend=dict(orientation="h", y=1.1, x=1, xanchor="right", font=dict(size=11)),
                        margin=dict(l=55,r=20,t=40,b=30),
                        xaxis=dict(showgrid=True,gridcolor="#eee"),
                        yaxis=dict(showgrid=True,gridcolor="#eee",tickformat=",.0f"))

                    mtf_section = html.Div([
                        debug_info,
                        html.Div([
                            html.Div("📊 三層多時間框架確認",
                                     style={"fontSize":"12px","fontWeight":"500","color":"#1a1a1a",
                                            "alignSelf":"center","marginRight":"8px","whiteSpace":"nowrap"}),
                            lamp(sig["l1_ok"], sig["mo_cur"],  "L1 月線"),
                            lamp(sig["l2_ok"], sig["wk_cur"],  "L2 週線"),
                            html.Div([
                                html.Div("L3 日線", style={"fontSize":"10px","color":"#aaa"}),
                                html.Div(l3_label, style={"fontSize":"12px","fontWeight":"500","color":signal_color}),
                                html.Div(f"{sig['day_cur']:+.1f}%" if sig['day_cur'] else "—",
                                         style={"fontSize":"11px","color":"#888"}),
                            ], style={"textAlign":"center","padding":"8px 12px",
                                      "background":"#f5f5f3","borderRadius":"8px","minWidth":"110px"}),
                            html.Div(
                                "✅ 三層確認，可考慮進場" if all_ok else "⏳ 等待訊號對齊",
                                style={"fontSize":"13px","fontWeight":"500","color":signal_color,
                                       "alignSelf":"center","marginLeft":"8px",
                                       "padding":"6px 12px","borderRadius":"6px",
                                       "background":"#f0faf5" if all_ok else "#f5f5f3"}),
                        ], style={"display":"flex","gap":"8px","padding":"10px 12px",
                                  "borderTop":"0.5px solid #f0f0f0","flexWrap":"wrap","alignItems":"center"}),
                        html.Div([
                            html.Div([
                                html.Div("三層策略報酬", style={"fontSize":"10px","color":"#aaa"}),
                                html.Div(f"{res['total_ret']:+.2f}%",
                                         style={"fontSize":"18px","fontWeight":"500","color":ret_color}),
                                html.Div(f"${res['final_val']/10000:.2f}萬",
                                         style={"fontSize":"11px","color":ret_color}),
                            ], style={"background":"#f0faf5" if res['total_ret']>=0 else "#fff5f5",
                                      "borderRadius":"8px","padding":"8px 12px","flex":"1",
                                      "border":f"1.5px solid {'#1D9E75' if res['total_ret']>=0 else '#A32D2D'}"}),
                            html.Div([
                                html.Div("買入持有", style={"fontSize":"10px","color":"#aaa"}),
                                html.Div(f"{res['bah_ret']:+.2f}%",
                                         style={"fontSize":"18px","fontWeight":"500","color":bah_color}),
                                html.Div(f"${res['bah_final']/10000:.2f}萬",
                                         style={"fontSize":"11px","color":bah_color}),
                            ], style={"background":"#f5f5f3","borderRadius":"8px","padding":"8px 12px","flex":"1"}),
                            html.Div([
                                html.Div("最大回撤", style={"fontSize":"10px","color":"#aaa"}),
                                html.Div(f"-{res['max_dd']:.1f}%",
                                         style={"fontSize":"18px","fontWeight":"500","color":"#A32D2D"}),
                                html.Div("✅ 贏過買持" if beat else "❌ 輸給買持",
                                         style={"fontSize":"11px","color":"#0F6E56" if beat else "#A32D2D"}),
                            ], style={"background":"#f5f5f3","borderRadius":"8px","padding":"8px 12px","flex":"1"}),
                        ], style={"display":"flex","gap":"8px","padding":"0 12px 8px","flexWrap":"wrap"}),
                        dcc.Graph(figure=fig_mtf, config={"displayModeBar":False}),
                    ])
                else:
                    mtf_section = html.Div([
                        debug_info,
                        html.P(f"⚠️ 三層分析：{err or '資料不足'}",
                               style={"fontSize":"12px","color":"#dc2626","padding":"8px 12px"}),
                    ])
            else:
                mtf_section = html.Div()

            chart_divs.append(html.Div([
                dcc.Graph(figure=fig,config={"displayModeBar":False}),
                mtf_section,
                html.Div([
                    html.Span(f"{ticker}　乖離率：",
                              style={"fontSize":"12px","color":"#888","alignSelf":"center","marginRight":"8px"}),
                    *dev_cards,
                ], style={"display":"flex","flexWrap":"wrap","gap":"6px","padding":"8px 12px",
                          "borderTop":"0.5px solid #f0f0f0","alignItems":"center"}),
            ], style={"marginBottom":"16px","border":"0.5px solid #e5e5e5",
                      "borderRadius":"10px","overflow":"hidden","background":"white"}))


    # ── 回測分析 ──
    backtest_divs = []
    if not n_clicks or int(n_clicks) == 0:
        backtest_divs.append(html.Div([
            html.P("點上方「更新」按鈕開始跑回測分析",
                   style={"fontSize":"15px","color":"#888","textAlign":"center","marginTop":"30px"}),
            html.P("回測需要重新抓取資料並運算，請先確認股票代號與日期區間後再按更新",
                   style={"fontSize":"13px","color":"#aaa","textAlign":"center","marginTop":"8px"}),
        ], style={"padding":"20px"}))
    else:
        bt_end_dt = datetime.datetime.now(tz=timezone.utc)
        if interval == "1wk":
            bt_start_dt  = bt_end_dt - datetime.timedelta(days=365*3)
            bt_interval  = "1wk"
            bt_unit      = "週"
            bt_ann       = 52
        elif interval == "1mo":
            bt_start_dt  = bt_end_dt - datetime.timedelta(days=365*5)
            bt_interval  = "1mo"
            bt_unit      = "月"
            bt_ann       = 12
        else:
            bt_start_dt  = bt_end_dt - datetime.timedelta(days=days)
            bt_interval  = "1d"
            bt_unit      = "天"
            bt_ann       = 252

        bt_ticker_data = {}
        for i2, ticker in enumerate(tickers[:12]):
            try:
                d2, c2, v2, o2 = fetch_yahoo_range(ticker, bt_start_dt, bt_end_dt, bt_interval)
                bt_ticker_data[ticker] = {"dates":d2,"closes":c2,"opens":o2,"volumes":v2,"color":COLORS[i2%len(COLORS)]}
            except:
                bt_ticker_data[ticker] = None

        backtest_divs = [
            html.P(
                f"用收盤價斜率（2–20{bt_unit}視窗）做方向回測：斜率由負轉正後，統計到下一次轉負前股價是否上漲；斜率由正轉負後，統計到下一次轉正前股價是否下跌。",
                style={"fontSize":"13px","color":"#666","marginBottom":"16px",
                       "background":"#f5f5f3","padding":"10px 14px","borderRadius":"8px"}),
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
                ok_t2 = s.get("correct_t2")
                ok_t2_text  = "✅" if ok_t2 else ("❌" if ok_t2 is not None else "—")
                ok_t2_color = "#0F6E56" if ok_t2 else ("#A32D2D" if ok_t2 is not None else "#888")
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
                    html.Td(ok_t2_text, style={"padding":"4px 8px","color":ok_t2_color,"fontWeight":"500"}),
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
                        html.Th("預測（反轉）",style=th_style),
                        html.Th("預測（T+1,T+2）",style=th_style),
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
                key=lambda w: simulate_trading(all_results[w]["signals"], "long", 100000, 1)["final_val"])

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
            best_win_ret = simulate_trading(all_results[best_win]["signals"], "long", 100000, 1)["total_ret"]
            fig_bt.update_layout(
                title=dict(
                    text=f"<b>{ticker}</b>　斜率方向預測正確率（最佳視窗：{best_win}{bt_unit}，只做多報酬 {best_win_ret:+.2f}%，負轉正正確率 {best_n2p['correct_rate']}%）",
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
                    html.Div([
                        html.Div("負轉正正確率（T+1,T+2平均）",style={"fontSize":"11px","color":"#888","marginBottom":"3px"}),
                        html.Div(f"{best_n2p['correct_rate_t2']}%",
                                 style={"fontSize":"20px","fontWeight":"500","color":"#0F6E56"}),
                        html.Div(f"{best_n2p['count_t2']} 次訊號",style={"fontSize":"11px","color":"#888"}),
                    ],style={"background":"#f5f5f3","borderRadius":"8px","padding":"10px 14px"}),
                    html.Div([
                        html.Div("正轉負正確率（T+1,T+2平均）",style={"fontSize":"11px","color":"#888","marginBottom":"3px"}),
                        html.Div(f"{best_p2n['correct_rate_t2']}%",
                                 style={"fontSize":"20px","fontWeight":"500","color":"#A32D2D"}),
                        html.Div(f"{best_p2n['count_t2']} 次訊號",style={"fontSize":"11px","color":"#888"}),
                    ],style={"background":"#f5f5f3","borderRadius":"8px","padding":"10px 14px"}),
                ],style={"display":"grid","gridTemplateColumns":"repeat(3,1fr)","gap":"10px",
                         "padding":"12px","marginTop":"4px"}),
            ])

            # 猜對/猜錯時的平均斜率比較
            def slope_accuracy_stats(signals, sig_filter):
                lst = [s for s in signals if sig_filter(s)]
                correct_lst = [s for s in lst if s["correct"]]
                wrong_lst   = [s for s in lst if not s["correct"]]
                def avg_abs_slope(items):
                    if not items: return None
                    return sum(abs(s["cur_slope"]) for s in items) / len(items)
                return {
                    "correct_avg": avg_abs_slope(correct_lst),
                    "wrong_avg":   avg_abs_slope(wrong_lst),
                    "correct_n":   len(correct_lst),
                    "wrong_n":     len(wrong_lst),
                }

            n2p_stats = slope_accuracy_stats(all_results[best_win]["signals"], lambda s: "負轉正" in s["type"])
            p2n_stats = slope_accuracy_stats(all_results[best_win]["signals"], lambda s: "正轉負" in s["type"])

            def fmt_avg(v):
                return f"{v:.1f}%" if v is not None else "—"

            slope_compare = html.Div([
                html.P("猜對 vs 猜錯時的平均當日斜率（取絕對值）",
                       style={"fontSize":"12px","color":"#888","margin":"12px 0 6px","paddingLeft":"12px","fontWeight":"500"}),
                html.Div([
                    html.Div([
                        html.Div("負轉正", style={"fontSize":"12px","color":"#0F6E56","fontWeight":"500","marginBottom":"4px"}),
                        html.Div([
                            html.Span(f"✅ 猜對（{n2p_stats['correct_n']}次）：", style={"fontSize":"12px","color":"#888"}),
                            html.Span(fmt_avg(n2p_stats['correct_avg']), style={"fontSize":"14px","fontWeight":"500","color":"#0F6E56","marginLeft":"4px"}),
                        ], style={"marginBottom":"2px"}),
                        html.Div([
                            html.Span(f"❌ 猜錯（{n2p_stats['wrong_n']}次）：", style={"fontSize":"12px","color":"#888"}),
                            html.Span(fmt_avg(n2p_stats['wrong_avg']), style={"fontSize":"14px","fontWeight":"500","color":"#A32D2D","marginLeft":"4px"}),
                        ]),
                    ], style={"background":"#f5f5f3","borderRadius":"8px","padding":"10px 14px","flex":"1"}),
                    html.Div([
                        html.Div("正轉負", style={"fontSize":"12px","color":"#A32D2D","fontWeight":"500","marginBottom":"4px"}),
                        html.Div([
                            html.Span(f"✅ 猜對（{p2n_stats['correct_n']}次）：", style={"fontSize":"12px","color":"#888"}),
                            html.Span(fmt_avg(p2n_stats['correct_avg']), style={"fontSize":"14px","fontWeight":"500","color":"#0F6E56","marginLeft":"4px"}),
                        ], style={"marginBottom":"2px"}),
                        html.Div([
                            html.Span(f"❌ 猜錯（{p2n_stats['wrong_n']}次）：", style={"fontSize":"12px","color":"#888"}),
                            html.Span(fmt_avg(p2n_stats['wrong_avg']), style={"fontSize":"14px","fontWeight":"500","color":"#A32D2D","marginLeft":"4px"}),
                        ]),
                    ], style={"background":"#f5f5f3","borderRadius":"8px","padding":"10px 14px","flex":"1"}),
                ], style={"display":"flex","gap":"10px","padding":"0 12px","flexWrap":"wrap"}),
            ])

            best_signals = all_results[best_win]["signals"]
            sim_section  = html.Div()
            if best_signals:
                first_price  = best_signals[0]["entry_price"]
                last_price   = best_signals[-1]["exit_price"]
                bah_ret      = round((last_price - first_price) / first_price * 100, 2)
                init_capital = 100000
                bah_final    = round(init_capital * (1 + bah_ret/100), 0)

                # 對每個視窗（2~20）各跑一次模擬
                sim_rows_long, sim_rows_both = [], []
                best_long, best_both = None, None
                for w in range(2, 21):
                    w_signals = all_results[w]["signals"]
                    rl = simulate_trading(w_signals, "long", init_capital, 1)
                    rb = simulate_trading(w_signals, "both", init_capital, 1)
                    sim_rows_long.append((w, rl))
                    sim_rows_both.append((w, rb))
                    if best_long is None or rl["final_val"] > best_long[1]["final_val"]:
                        best_long = (w, rl)
                    if best_both is None or rb["final_val"] > best_both[1]["final_val"]:
                        best_both = (w, rb)

                th_s2 = {"padding":"4px 8px","textAlign":"left","fontSize":"12px",
                         "color":"#888","borderBottom":"0.5px solid #eee"}

                bah_color = "#0F6E56" if bah_ret >= 0 else "#A32D2D"

                def sim_tbl_with_bah(rows, best_w):
                    def row(w, r, is_best):
                        rc = "#0F6E56" if r["total_ret"]>=0 else "#A32D2D"
                        fw = "500" if is_best else "400"
                        beat = "✅" if r["final_val"] > bah_final else "❌"
                        return html.Tr([
                            html.Td(f"{w}{bt_unit}", style={"padding":"4px 8px","fontWeight":fw}),
                            html.Td(f"{r['final_val']/10000:.2f}萬", style={"padding":"4px 8px","color":rc,"fontWeight":fw}),
                            html.Td(f"{'+' if r['total_ret']>=0 else ''}{r['total_ret']}%", style={"padding":"4px 8px","color":rc}),
                            html.Td(str(r['trade_count'])+"筆", style={"padding":"4px 8px"}),
                            html.Td(f"{r['win_rate']}%",  style={"padding":"4px 8px"}),
                            html.Td(f"-{r['max_dd']}%",   style={"padding":"4px 8px","color":"#A32D2D"}),
                            html.Td(beat, style={"padding":"4px 8px"}),
                        ])
                    return html.Table([
                        html.Thead(html.Tr([
                            html.Th("視窗",style=th_s2), html.Th("最終資產",style=th_s2),
                            html.Th("總報酬",style=th_s2),  html.Th("交易數",style=th_s2),
                            html.Th("勝率",style=th_s2),    html.Th("最大回撤",style=th_s2),
                            html.Th("贏過買持",style=th_s2),
                        ])),
                        html.Tbody([row(w2, r2, w2==best_w) for w2,r2 in rows]),
                    ], style={"width":"100%","borderCollapse":"collapse","fontSize":"12px"})

                sim_section = html.Div([
                    html.P(f"模擬交易（初始 10 萬，各視窗（2–20{bt_unit}）負轉正/正轉負訊號進出場）",
                           style={"fontSize":"12px","color":"#888","margin":"12px 0 8px",
                                  "paddingLeft":"12px","fontWeight":"500"}),
                    html.Div([
                        html.Div([
                            html.P(f"只做多　最佳：{best_long[0]}{bt_unit}，{best_long[1]['final_val']/10000:.2f}萬（{best_long[1]['total_ret']:+.2f}%）",
                                   style={"fontSize":"12px","color":"#0F6E56","margin":"0 0 6px","fontWeight":"500"}),
                            html.Div(sim_tbl_with_bah(sim_rows_long, best_long[0]), style={"overflowX":"auto"}),
                        ], style={"flex":"1","minWidth":"0"}),
                        html.Div([
                            html.P(f"多空都做　最佳：{best_both[0]}{bt_unit}，{best_both[1]['final_val']/10000:.2f}萬（{best_both[1]['total_ret']:+.2f}%）",
                                   style={"fontSize":"12px","color":"#0F6E56","margin":"0 0 6px","fontWeight":"500"}),
                            html.Div(sim_tbl_with_bah(sim_rows_both, best_both[0]), style={"overflowX":"auto"}),
                        ], style={"flex":"1","minWidth":"0"}),
                    ], style={"display":"flex","gap":"20px","padding":"0 12px","flexWrap":"wrap"}),
                ])


            backtest_divs.append(html.Div([
                dcc.Graph(figure=fig_bt, config={"displayModeBar":False}),
                summary_cards,
                slope_compare,
                sim_section,
                make_sig_table(all_results[best_win]["signals"], best_win),
            ],style={"marginBottom":"20px","border":"0.5px solid #e5e5e5",
                     "borderRadius":"10px","overflow":"hidden","background":"white","paddingBottom":"12px"}))

    # ── 恐懼指標 ──
    vix_data = fetch_vix(start_dt, end_dt)
    fng_data = fetch_fear_greed()
    fear_notes = []

    # VIX 最新值
    vix_val, vix_label, vix_color = None, "—", "#888"
    if vix_data:
        latest_vix = sorted(vix_data.keys())[-1]
        vix_val = vix_data[latest_vix]
        if vix_val >= 40:   vix_label, vix_color = "極度恐懼", "#dc2626"
        elif vix_val >= 30: vix_label, vix_color = "高恐懼",   "#e25b5b"
        elif vix_val >= 20: vix_label, vix_color = "中等恐懼", "#d97706"
        elif vix_val >= 15: vix_label, vix_color = "低恐懼",   "#16a34a"
        else:               vix_label, vix_color = "極度平靜", "#0891b2"
        fear_notes.append("✅ VIX")
    else:
        fear_notes.append("❌ VIX 無資料")

    # Fear & Greed 最新值
    fng_val, fng_label, fng_color = None, "—", "#888"
    if fng_data:
        latest_fng = sorted(fng_data.keys())[-1]
        fng_val = fng_data[latest_fng]
        if fng_val >= 75:   fng_label, fng_color = "極度貪婪", "#dc2626"
        elif fng_val >= 55: fng_label, fng_color = "貪婪",     "#d97706"
        elif fng_val >= 45: fng_label, fng_color = "中性",     "#888"
        elif fng_val >= 25: fng_label, fng_color = "恐懼",     "#2563eb"
        else:               fng_label, fng_color = "極度恐懼", "#1d4ed8"
        fear_notes.append("✅ Fear & Greed")
    else:
        fear_notes.append("❌ Fear & Greed 無資料")

    def fear_card(title, val, label, color, sub=""):
        return html.Div([
            html.Div(title, style={"fontSize":"11px","color":"#888","marginBottom":"4px"}),
            html.Div(f"{val:.1f}" if val is not None else "—",
                     style={"fontSize":"28px","fontWeight":"500","color":color,"lineHeight":"1.1"}),
            html.Div(label, style={"fontSize":"13px","fontWeight":"500","color":color,"marginTop":"3px"}),
            html.Div(sub, style={"fontSize":"11px","color":"#aaa","marginTop":"2px"}),
        ], style={"background":"#f5f5f3","borderRadius":"10px","padding":"14px 18px","flex":"1",
                  "border":f"1.5px solid {color}","minWidth":"140px"})

    fear_cards = html.Div([
        fear_card("VIX 恐懼指數", vix_val, vix_label, vix_color, "＞30=高恐懼，＜15=平靜"),
        fear_card("Fear & Greed Index", fng_val, fng_label, fng_color, "0=極度恐懼，100=極度貪婪"),
    ], style={"display":"flex","gap":"14px","flexWrap":"wrap"})

    content = html.Div(
        chart_divs +
        [html.Div("🔬 回測分析", style={"fontSize":"15px","fontWeight":"500","color":"#1a1a1a",
                                          "margin":"20px 0 12px","paddingTop":"12px",
                                          "borderTop":"1px solid #e5e5e5"})] +
        backtest_divs
    )
    return content, fear_cards, "　".join(messages), "　".join(fear_notes)


@app.callback(
    Output("asia-signal-div","children"),
    Input("asia-btn","n_clicks"),
    State("asia-window","value"),
    prevent_initial_call=True,
)
def update_asia_signal(n_clicks, asia_window):
    if not n_clicks:
        return html.Div()
    window = int(asia_window or 20)
    end_dt   = datetime.datetime.now(tz=timezone.utc)
    # 抓足夠天數（視窗+緩衝）
    lookback = max(window * 2, 60)
    start_dt = end_dt - datetime.timedelta(days=lookback)
    indices  = [
        ("台股", "^TWII", "🇹🇼", "#dc2626"),
        ("日股", "^N225", "🇯🇵", "#2563eb"),
        ("韓股", "^KS11", "🇰🇷", "#16a34a"),
    ]
    results = []
    for name, sym, flag, color in indices:
        try:
            dates, closes, _, _ = fetch_yahoo_range(sym, start_dt, end_dt, "1d")
            if len(closes) < window:
                results.append((name, flag, color, None, None, None, None))
                continue
            high20   = max(closes[-window:-1])
            today    = closes[-1]
            new_high = today >= high20
            chg_pct  = round((closes[-1]-closes[-2])/closes[-2]*100, 2) if len(closes)>=2 else 0
            results.append((name, flag, color, today, high20, new_high, chg_pct))
        except Exception as e:
            results.append((name, flag, color, None, None, None, None))

    # 判斷訊號
    tw   = next((r for r in results if r[0]=="台股"), None)
    jp   = next((r for r in results if r[0]=="日股"), None)
    kr   = next((r for r in results if r[0]=="韓股"), None)

    tw_high = tw[5] if tw else None
    jp_high = jp[5] if jp else None
    kr_high = kr[5] if kr else None

    signal_triggered = tw_high is True and jp_high is False and kr_high is False

    signal_box = html.Div([
        html.Div(
            f"🚨 資金回流美國訊號觸發！台股創{window}日新高，但日韓均未創高" if signal_triggered
            else f"⚪ 目前未觸發訊號（{window}日新高基準）",
            style={"fontSize":"14px","fontWeight":"500","padding":"12px 16px","borderRadius":"8px",
                   "background":"#fff5f0" if signal_triggered else "#f5f5f3",
                   "color":"#dc2626" if signal_triggered else "#888",
                   "border":"1.5px solid #dc2626" if signal_triggered else "1px solid #eee",
                   "marginBottom":"12px"}),
    ])

    cards = html.Div([
        html.Div([
            html.Div(f"{flag} {name}", style={"fontSize":"12px","color":"#888","marginBottom":"4px"}),
            html.Div(f"{today:,.0f}" if today else "—",
                     style={"fontSize":"20px","fontWeight":"500","color":color}),
            html.Div(f"{window}日高點：{high20:,.0f}" if high20 else "資料不足",
                     style={"fontSize":"11px","color":"#aaa"}),
            html.Div(
                ("✅ 創{window}日新高".format(window=window) if new_high else f"❌ 未創{window}日新高") if new_high is not None else "—",
                style={"fontSize":"12px","fontWeight":"500",
                       "color":"#0F6E56" if new_high else "#A32D2D","marginTop":"3px"}),
            html.Div(f"今日漲跌：{chg_pct:+.2f}%" if chg_pct is not None else "",
                     style={"fontSize":"11px","color":"#0F6E56" if (chg_pct or 0)>=0 else "#A32D2D"}),
        ], style={"background":"#f5f5f3","borderRadius":"8px","padding":"12px 14px","flex":"1"})
        for name, flag, color, today, high20, new_high, chg_pct in results
    ], style={"display":"flex","gap":"12px","flexWrap":"wrap"})

    return html.Div([signal_box, cards])


@app.callback(
    Output("inventory-charts","children"),
    Output("inventory-btn","children"),
    Input("inventory-btn","n_clicks"),
    prevent_initial_call=True,
)
def update_inventory(n_clicks):
    if not n_clicks:
        return html.Div(), "📦 顯示庫存數據"
    inv   = fetch_fred_inventory()
    mfg   = inv.get("manufacturing", [])
    ret   = inv.get("retail", [])
    pce   = inv.get("pce", [])
    rsxfs = inv.get("retail_sales", [])

    if not any([mfg, ret, pce, rsxfs]):
        return html.P("無法取得數據，請確認網路連線",
                      style={"fontSize":"13px","color":"#aaa","padding":"12px"}), "📦 顯示庫存數據"

    def yoy_list(data):
        vals = [x[1] for x in data]
        return [None]*12 + [
            round((vals[i]-vals[i-12])/vals[i-12]*100, 1)
            for i in range(12, len(vals))
        ]

    def trend_dir(yoy_vals):
        """最近3個月 YoY 是上升還是下降"""
        recent = [v for v in yoy_vals[-3:] if v is not None]
        if len(recent) < 2: return "持平"
        if recent[-1] > recent[0] + 0.5:  return "上升"
        if recent[-1] < recent[0] - 0.5:  return "下降"
        return "持平"

    def health(key, yoy_val, trend):
        """根據指標類型、YoY 和趨勢，給出健康狀態"""
        if yoy_val is None: return "無資料", "#888", "—"
        if key in ("manufacturing", "retail"):
            # 庫存：YoY 太高是壞事（積壓），下降是去化
            if yoy_val > 8:
                return "⚠️ 積壓過高", "#dc2626", "庫存快速堆積，需求可能走弱"
            elif yoy_val > 3:
                return "🟡 偏高", "#d97706", "庫存偏高，留意去化速度"
            elif yoy_val >= 0:
                return "🟢 健康", "#16a34a", "庫存溫和增加，需求穩定"
            else:
                return "🟢 去化中", "#0891b2", "庫存下降，需求健康消化"
        else:
            # PCE/零售銷售：YoY 正成長是好事
            if yoy_val >= 4:
                return "🟢 強勁", "#16a34a", "消費動能強勁"
            elif yoy_val >= 1:
                return "🟢 健康", "#16a34a", "消費穩定成長"
            elif yoy_val >= 0:
                return "🟡 趨緩", "#d97706", "消費成長放緩，留意趨勢"
            elif yoy_val >= -2:
                return "🟠 走弱", "#dc2626", "消費出現走弱訊號"
            else:
                return "🔴 衰退", "#dc2626", "消費明顯衰退"

    def mini_spark(data, color):
        """12個月迷你折線圖"""
        vals  = [x[1] for x in data[-12:]]
        dates = [x[0] for x in data[-12:]]
        fig   = go.Figure()
        fig.add_trace(go.Scatter(x=dates, y=vals, mode="lines",
                                  line=dict(color=color, width=2)))
        fig.update_layout(margin=dict(l=0,r=0,t=0,b=0), height=50, width=180,
                          plot_bgcolor="white", paper_bgcolor="white",
                          xaxis=dict(visible=False), yaxis=dict(visible=False),
                          showlegend=False)
        return dcc.Graph(figure=fig, config={"displayModeBar":False},
                         style={"height":"50px","width":"180px"})

    datasets = [
        ("manufacturing", "製造業庫存", mfg,   "#2563eb", "百萬美元"),
        ("retail",        "零售業庫存", ret,   "#d97706", "百萬美元"),
        ("pce",           "個人消費PCE", pce,  "#7c3aed", "十億美元"),
        ("retail_sales",  "零售銷售",  rsxfs, "#0891b2", "百萬美元"),
    ]

    cards = []
    for key, name, data, color, unit in datasets:
        if not data:
            continue
        y_list   = yoy_list(data)
        latest   = data[-1]
        latest_y = y_list[-1]
        trend    = trend_dir(y_list)
        status, status_color, note = health(key, latest_y, trend)
        trend_icon = "↑" if trend=="上升" else ("↓" if trend=="下降" else "→")
        trend_tcolor = "#16a34a" if trend=="上升" else ("#dc2626" if trend=="下降" else "#888")

        cards.append(html.Div([
            html.Div([
                html.Div(name, style={"fontSize":"12px","color":"#888","marginBottom":"4px"}),
                html.Div(status, style={"fontSize":"14px","fontWeight":"500","color":status_color}),
            ], style={"marginBottom":"6px"}),
            html.Div([
                html.Div([
                    html.Div(f"最新值", style={"fontSize":"10px","color":"#aaa"}),
                    html.Div(f"{latest[1]:,.0f} {unit}", style={"fontSize":"13px","fontWeight":"500","color":color}),
                    html.Div(f"{latest[0]}", style={"fontSize":"10px","color":"#aaa"}),
                ], style={"flex":"1"}),
                html.Div([
                    html.Div(f"年增率", style={"fontSize":"10px","color":"#aaa"}),
                    html.Div(f"{latest_y:+.1f}%" if latest_y is not None else "—",
                             style={"fontSize":"13px","fontWeight":"500",
                                    "color":"#16a34a" if (latest_y or 0)>=0 else "#dc2626"}),
                    html.Div([
                        html.Span(trend_icon, style={"color":trend_tcolor,"fontWeight":"bold"}),
                        html.Span(f" {trend}", style={"fontSize":"10px","color":"#aaa","marginLeft":"2px"}),
                    ]),
                ], style={"flex":"1"}),
                html.Div(mini_spark(data, color), style={"flex":"2"}),
            ], style={"display":"flex","alignItems":"center","gap":"8px"}),
            html.Div(note, style={"fontSize":"11px","color":"#888","marginTop":"6px",
                                   "borderTop":"0.5px solid #f0f0f0","paddingTop":"5px"}),
        ], style={"background":"white","borderRadius":"10px","padding":"12px 14px",
                  "border":f"1.5px solid {status_color}","flex":"1","minWidth":"220px"}))

    # ── 風險評分 ──
    score = 0
    reasons_green, reasons_yellow, reasons_red = [], [], []

    pce_yoy   = yoy_list(pce)[-1]   if pce   else None
    rsxfs_yoy = yoy_list(rsxfs)[-1] if rsxfs else None
    mfg_yoy   = yoy_list(mfg)[-1]   if mfg   else None

    if pce_yoy is not None:
        if pce_yoy >= 3:   score += 1; reasons_green.append(f"PCE YoY {pce_yoy:+.1f}%（消費強勁）")
        elif pce_yoy >= 0: pass
        elif pce_yoy >= -2: score -= 1; reasons_yellow.append(f"PCE YoY {pce_yoy:+.1f}%（消費走弱）")
        else:               score -= 2; reasons_red.append(f"PCE YoY {pce_yoy:+.1f}%（消費衰退）")

    if rsxfs_yoy is not None:
        if rsxfs_yoy >= 2:  score += 1; reasons_green.append(f"零售銷售 YoY {rsxfs_yoy:+.1f}%（健康）")
        elif rsxfs_yoy >= 0: pass
        elif rsxfs_yoy >= -3: score -= 1; reasons_yellow.append(f"零售銷售 YoY {rsxfs_yoy:+.1f}%（走弱）")
        else:                  score -= 2; reasons_red.append(f"零售銷售 YoY {rsxfs_yoy:+.1f}%（衰退）")

    if mfg_yoy is not None:
        if mfg_yoy > 10:    score -= 2; reasons_red.append(f"製造業庫存 YoY {mfg_yoy:+.1f}%（積壓過高）")
        elif mfg_yoy > 5:   score -= 1; reasons_yellow.append(f"製造業庫存 YoY {mfg_yoy:+.1f}%（庫存偏高）")
        elif mfg_yoy >= 0:  pass
        else:                score += 1; reasons_green.append(f"製造業庫存 YoY {mfg_yoy:+.1f}%（去化中）")

    if score >= 2:
        light, light_text, light_color, light_bg = "🟢", "低風險", "#0F6E56", "#f0faf5"
    elif score >= 0:
        light, light_text, light_color, light_bg = "🟡", "中等風險", "#b45309", "#fffbeb"
    elif score >= -2:
        light, light_text, light_color, light_bg = "🟠", "偏高風險", "#c2410c", "#fff7ed"
    else:
        light, light_text, light_color, light_bg = "🔴", "高風險", "#dc2626", "#fff5f5"

    risk_box = html.Div([
        html.Div([
            html.Span(light, style={"fontSize":"28px","marginRight":"10px"}),
            html.Div([
                html.Div(f"整體經濟風險：{light_text}",
                         style={"fontSize":"15px","fontWeight":"500","color":light_color}),
                html.Div(f"評分 {score:+d}",
                         style={"fontSize":"11px","color":"#888","marginTop":"2px"}),
            ]),
        ], style={"display":"flex","alignItems":"center","marginBottom":"8px"}),
        html.Div([
            html.Div([html.Div("✅ " + r, style={"fontSize":"12px","color":"#555","marginBottom":"2px"})
                      for r in reasons_green] or [], style={"flex":"1"}),
            html.Div([html.Div("⚠️ " + r, style={"fontSize":"12px","color":"#555","marginBottom":"2px"})
                      for r in reasons_yellow] or [], style={"flex":"1"}),
            html.Div([html.Div("🚨 " + r, style={"fontSize":"12px","color":"#555","marginBottom":"2px"})
                      for r in reasons_red] or [], style={"flex":"1"}),
        ], style={"display":"flex","gap":"12px","flexWrap":"wrap"}),
    ], style={"background":light_bg,"borderRadius":"10px","padding":"14px 18px",
              "border":f"1.5px solid {light_color}","marginBottom":"14px"})

    return html.Div([
        risk_box,
        html.Div(cards, style={"display":"flex","gap":"12px","flexWrap":"wrap"}),
    ]), "📦 隱藏庫存數據"


INDEX_LIST = [
    ("DJIA",    "^DJI",   "道瓊"),
    ("NAS",     "^IXIC",  "那斯達克"),
    ("S&P 500", "^GSPC",  "S&P 500"),
    ("SOX",     "^SOX",   "費半"),
    ("RUT",     "^RUT",   "Russell 2000"),
    ("QQQ",     "QQQ",    "那斯達克100 ETF"),
    ("SOXX",    "SOXX",   "半導體 ETF"),
    ("XLK",     "XLK",    "科技 ETF"),
    ("XLF",     "XLF",    "金融 ETF"),
    ("XLI",     "XLI",    "工業 ETF"),
    ("XLE",     "XLE",    "能源 ETF"),
    ("XLV",     "XLV",    "醫療 ETF"),
    ("XLY",     "XLY",    "非必需消費 ETF"),
    ("XLB",     "XLB",    "原物料 ETF"),
    ("GDX",     "GDX",    "黃金礦業 ETF"),
    ("ITA",     "ITA",    "航太國防 ETF"),
]

@app.callback(
    Output("index-div","children"),
    Output("index-btn","children"),
    Input("index-btn","n_clicks"),
    prevent_initial_call=True,
)
def update_index_overview(n_clicks):
    if not n_clicks:
        return html.Div(), "📋 載入指數總覽"

    end_dt   = datetime.datetime.now(tz=timezone.utc)
    start_dt = end_dt - datetime.timedelta(days=400)

    th   = {"padding":"6px 10px","textAlign":"right","fontSize":"11px",
            "color":"#888","borderBottom":"1px solid #eee","whiteSpace":"nowrap"}
    th_l = {**th, "textAlign":"left"}

    rows = []
    for label, sym, desc in INDEX_LIST:
        try:
            dates, closes, _, _ = fetch_yahoo_range(sym, start_dt, end_dt, "1d")
            if len(closes) < 60:
                continue

            now   = closes[-1]
            high  = max(closes[-252:]) if len(closes)>=252 else max(closes)
            low   = min(closes[-252:]) if len(closes)>=252 else min(closes)
            ma60  = round(sum(closes[-60:]) / 60, 2)

            spread    = round(high - low, 2)
            rebound   = round((now - low) / (high - low) * 100) if high != low else 0
            vs_high   = round(now - high, 2)
            vs_high_r = round((now - high) / high * 100, 1)
            ma60_dev  = round((now - ma60) / ma60 * 100, 1)

            now_color = "#dc2626" if now >= high * 0.99 else "#1a1a1a"
            vsh_color = "#0F6E56" if vs_high >= 0 else "#dc2626"
            dev_color = "#0F6E56" if ma60_dev >= 0 else "#dc2626"
            reb_color = "#0F6E56" if rebound >= 50 else ("#d97706" if rebound >= 25 else "#dc2626")

            rows.append(html.Tr([
                html.Td(html.Div([
                    html.Span(label, style={"fontWeight":"500","fontSize":"13px"}),
                    html.Span(f" {desc}", style={"fontSize":"10px","color":"#aaa","marginLeft":"4px"}),
                ]), style={"padding":"6px 10px","textAlign":"left","whiteSpace":"nowrap"}),
                html.Td(f"{high:,.2f}",     style={"padding":"6px 10px","textAlign":"right","fontSize":"12px"}),
                html.Td(f"{low:,.2f}",      style={"padding":"6px 10px","textAlign":"right","fontSize":"12px"}),
                html.Td(f"{ma60:,.2f}",     style={"padding":"6px 10px","textAlign":"right","fontSize":"12px","color":"#7c3aed"}),
                html.Td(f"{now:,.2f}",      style={"padding":"6px 10px","textAlign":"right","fontSize":"12px","fontWeight":"500","color":now_color}),
                html.Td(f"{spread:,.2f}",   style={"padding":"6px 10px","textAlign":"right","fontSize":"12px"}),
                html.Td(f"{rebound}%",      style={"padding":"6px 10px","textAlign":"right","fontSize":"12px","fontWeight":"500","color":reb_color}),
                html.Td(f"{vs_high:+,.2f}", style={"padding":"6px 10px","textAlign":"right","fontSize":"12px","color":vsh_color}),
                html.Td(f"{vs_high_r:+.1f}%", style={"padding":"6px 10px","textAlign":"right","fontSize":"12px","fontWeight":"500","color":vsh_color}),
                html.Td(f"{ma60_dev:+.1f}%",  style={"padding":"6px 10px","textAlign":"right","fontSize":"12px","fontWeight":"500","color":dev_color}),
            ], style={"borderBottom":"0.5px solid #f0f0f0"}))
        except:
            continue

    if not rows:
        return html.P("無法取得資料，請確認網路連線",
                      style={"color":"#aaa","fontSize":"13px"}), "📋 載入指數總覽"

    today = datetime.datetime.now(tz=timezone.utc).astimezone(
        datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d")

    table = html.Div([
        html.P(f"指數總覽 | {today}　（高低點=近252交易日，季線=60日均線）",
               style={"fontSize":"11px","color":"#aaa","margin":"0 0 8px"}),
        html.Div(html.Table([
            html.Thead(html.Tr([
                html.Th("指數",      style=th_l),
                html.Th("高點",      style=th),
                html.Th("低點",      style=th),
                html.Th("季線",      style={**th,"color":"#7c3aed"}),
                html.Th("NOW",       style={**th,"color":"#dc2626"}),
                html.Th("高低點差",  style=th),
                html.Th("反彈幅度",  style=th),
                html.Th("與前高差",  style=th),
                html.Th("前高幅率",  style={**th,"fontWeight":"500"}),
                html.Th("季線乖離率",style={**th,"fontWeight":"500"}),
            ], style={"background":"#f9f9f9"})),
            html.Tbody(rows),
        ], style={"width":"100%","borderCollapse":"collapse","fontSize":"12px"}),
        style={"overflowX":"auto"}),
    ], style={"border":"0.5px solid #e5e5e5","borderRadius":"10px",
              "overflow":"hidden","background":"white","padding":"12px 14px"})

    return table, "📋 重新載入"


@app.callback(
    Output("six-state-div","children"),
    Input("six-state-btn","n_clicks"),
    State("six-state-ticker","value"),
    prevent_initial_call=True,
)
def update_six_state(n_clicks, ticker_input):
    if not n_clicks:
        return html.Div()

    ticker = (ticker_input or "QQQ").strip().upper()
    end_dt   = datetime.datetime.now(tz=timezone.utc)
    start_dt = end_dt - datetime.timedelta(days=365*3)

    try:
        dates_d, day_closes, _, _ = fetch_yahoo_range(ticker, start_dt, end_dt, "1d")
    except Exception as e:
        return html.P(f"資料抓取失敗：{e}", style={"color":"#dc2626","fontSize":"13px"})

    if len(day_closes) < 130:
        return html.P("資料不足", style={"color":"#aaa","fontSize":"13px"})

    summary_entry, summary_exit, bah_total = analyze_six_states(day_closes, dates_d)
    if not summary_entry:
        return html.P("組合樣本不足", style={"color":"#aaa","fontSize":"13px"})

    summary = summary_entry  # 用進場排名顯示完整表格

    # 顏色輔助
    STATE_COLOR = {
        "加速上升": "#0F6E56", "減速上升": "#16a34a",
        "由負轉正": "#0891b2", "由正轉負": "#dc2626",
        "減速下跌": "#d97706", "加速下跌": "#7f1d1d",
    }
    STATE_ICON = {
        "加速上升":"🚀", "減速上升":"📈",
        "由負轉正":"⚡", "由正轉負":"🔻",
        "減速下跌":"📊", "加速下跌":"🔴",
    }

    th = {"padding":"5px 10px","textAlign":"left","fontSize":"11px",
          "color":"#888","borderBottom":"1px solid #eee","whiteSpace":"nowrap"}

    def state_badge(st):
        c = STATE_COLOR.get(st, "#888")
        icon = STATE_ICON.get(st, "")
        return html.Span(f"{icon}{st}",
                         style={"color":c,"fontWeight":"500","fontSize":"12px"})

    rows = []
    for rank, r in enumerate(summary[:27]):
        ret_color = "#0F6E56" if r["avg_ret"] >= 0 else "#A32D2D"
        fwd_color = "#0F6E56" if r["avg_fwd"] >= 0 else "#A32D2D"
        win_color = "#0F6E56" if r["win_rate"] >= 50 else "#A32D2D"
        rows.append(html.Tr([
            html.Td(f"#{rank+1}", style={"padding":"5px 10px","fontSize":"11px","color":"#aaa"}),
            html.Td(state_badge(r["l1"]), style={"padding":"5px 10px"}),
            html.Td(state_badge(r["l2"]), style={"padding":"5px 10px"}),
            html.Td(state_badge(r["l3"]), style={"padding":"5px 10px"}),
            html.Td(f"{r['avg_ret']:+.2f}%",
                    style={"padding":"5px 10px","fontWeight":"500","color":ret_color}),
            html.Td(f"{r['avg_fwd']:+.2f}%",
                    style={"padding":"5px 10px","color":fwd_color}),
            html.Td(f"{r['best']:+.1f}%",
                    style={"padding":"5px 10px","color":"#0F6E56","fontSize":"11px"}),
            html.Td(f"{r['worst']:+.1f}%",
                    style={"padding":"5px 10px","color":"#A32D2D","fontSize":"11px"}),
            html.Td(f"{r['win_rate']}%",
                    style={"padding":"5px 10px","color":win_color}),
            html.Td(f"{r['count']}筆",
                    style={"padding":"5px 10px","color":"#888","fontSize":"11px"}),
        ], style={"borderBottom":"0.5px solid #f5f5f5",
                  "background":"#f0faf5" if rank < 3 else "white"}))

    # 今日狀態
    s1 = rolling_annualized_log_slope(day_closes, 120, 252)
    s2 = rolling_annualized_log_slope(day_closes,  40, 252)
    s3 = rolling_annualized_log_slope(day_closes,  10, 252)
    today_l1 = slope_state(s1[-1] if not math.isnan(s1[-1]) else None,
                            s1[-2] if len(s1)>1 and not math.isnan(s1[-2]) else None)
    today_l2 = slope_state(s2[-1] if not math.isnan(s2[-1]) else None,
                            s2[-2] if len(s2)>1 and not math.isnan(s2[-2]) else None)
    today_l3 = slope_state(s3[-1] if not math.isnan(s3[-1]) else None,
                            s3[-2] if len(s3)>1 and not math.isnan(s3[-2]) else None)

    # 找今日組合的排名
    today_combo = next(
        (r for r in summary if r["l1"]==today_l1 and r["l2"]==today_l2 and r["l3"]==today_l3), None)

    today_box = html.Div([
        html.Div("📍 今日狀態",
                 style={"fontSize":"12px","fontWeight":"500","color":"#1a1a1a","marginBottom":"8px"}),
        html.Div([
            html.Div([
                html.Div("L1 月線", style={"fontSize":"10px","color":"#aaa"}),
                state_badge(today_l1),
            ], style={"flex":"1","textAlign":"center"}),
            html.Div([
                html.Div("L2 週線", style={"fontSize":"10px","color":"#aaa"}),
                state_badge(today_l2),
            ], style={"flex":"1","textAlign":"center"}),
            html.Div([
                html.Div("L3 日線", style={"fontSize":"10px","color":"#aaa"}),
                state_badge(today_l3),
            ], style={"flex":"1","textAlign":"center"}),
        ], style={"display":"flex","gap":"8px","marginBottom":"8px"}),
        html.Div(
            f"歷史排名：#{summary.index(today_combo)+1}　平均持有報酬：{today_combo['avg_ret']:+.2f}%　勝率：{today_combo['win_rate']}%　樣本：{today_combo['count']}筆"
            if today_combo else "此組合歷史樣本不足",
            style={"fontSize":"13px","fontWeight":"500",
                   "color":"#0F6E56" if today_combo and today_combo['avg_ret']>0 else "#888"}),
    ], style={"background":"#f5f5f3","borderRadius":"10px","padding":"12px 16px","marginBottom":"12px"})

    table = html.Div(html.Table([
        html.Thead(html.Tr([
            html.Th("排名",style=th),
            html.Th("L1 月線",style=th), html.Th("L2 週線",style=th), html.Th("L3 日線",style=th),
            html.Th("持有到今報酬",style=th),
            html.Th("5天後報酬",style=th),
            html.Th("最佳",style=th), html.Th("最差",style=th),
            html.Th("勝率",style=th), html.Th("樣本",style=th),
        ], style={"background":"#f9f9f9"})),
        html.Tbody(rows),
    ], style={"width":"100%","borderCollapse":"collapse","fontSize":"12px"}),
    style={"overflowX":"auto","border":"0.5px solid #e5e5e5","borderRadius":"10px","background":"white"})

    # 最佳進場（報酬最高前3）和最佳出場（報酬最低前3）
    best_entry  = summary_entry[:3]
    worst_entry = summary_exit[:3]  # 未來N天報酬最差的組合

    def signal_cards(items, title, color, bg, use_fwd=False):
        cards = []
        for i, r in enumerate(items):
            ret_val  = r["avg_fwd"]  if use_fwd else r["avg_ret"]
            win_val  = r["win_fwd"]  if use_fwd else r["win_rate"]
            ret_label= "5日後報酬" if use_fwd else "持有至今"
            cards.append(html.Div([
                html.Div(f"#{i+1}", style={"fontSize":"10px","color":"#aaa","marginBottom":"2px"}),
                html.Div([state_badge(r["l1"])], style={"marginBottom":"2px"}),
                html.Div([state_badge(r["l2"])], style={"marginBottom":"2px"}),
                html.Div([state_badge(r["l3"])], style={"marginBottom":"4px"}),
                html.Div(f"{ret_val:+.2f}%",
                         style={"fontSize":"13px","fontWeight":"500","color":color}),
                html.Div(f"{ret_label}　勝率 {win_val}%",
                         style={"fontSize":"11px","color":"#888"}),
            ], style={"background":bg,"borderRadius":"8px","padding":"10px 12px",
                      "flex":"1","border":f"1.5px solid {color}"}))
        return html.Div([
            html.Div(title, style={"fontSize":"12px","fontWeight":"500","color":color,
                                    "marginBottom":"8px"}),
            html.Div(cards, style={"display":"flex","gap":"8px"}),
        ], style={"marginBottom":"12px"})

    signals_box = html.Div([
        signal_cards(best_entry,  "✅ 最佳進場訊號（持有到今報酬最高）",    "#0F6E56", "#f0faf5", use_fwd=False),
        signal_cards(worst_entry, "🚪 最佳出場訊號（出現後5天報酬最差）",    "#dc2626", "#fff5f5", use_fwd=True),
    ])

    # ── 配對回測：Top10進場 × Top10出場 ──
    top_entry = set((r["l1"],r["l2"],r["l3"]) for r in summary_entry[:10])
    top_exit  = set((r["l1"],r["l2"],r["l3"]) for r in summary_exit[:10])

    paired = paired_backtest(day_closes, dates_d, top_entry, top_exit)

    # 買入/賣出點
    buy_x  = [t["date"]  for t in paired["trades"] if t["action"]=="買入"]
    buy_y  = [t["price"] for t in paired["trades"] if t["action"]=="買入"]
    sell_x = [t["date"]  for t in paired["trades"] if t["action"]=="賣出"]
    sell_y = [t["price"] for t in paired["trades"] if t["action"]=="賣出"]

    # 資產走勢
    eq_dates = [p["date"] for p in paired["equity"]]
    eq_vals  = [p["val"]  for p in paired["equity"]]
    date_idx = {d:i for i,d in enumerate(paired["dates"])}
    bah_vals = [100000*(paired["closes"][date_idx[d]]/paired["closes"][0])
                if d in date_idx else None for d in eq_dates]

    # 上下雙子圖：股價+買賣點 / 資產走勢
    fig_pair = make_subplots(rows=2, cols=1, shared_xaxes=True,
                              row_heights=[0.55, 0.45], vertical_spacing=0.05,
                              subplot_titles=("股價走勢與買賣點", "資產走勢"))

    # 股價線
    fig_pair.add_trace(go.Scatter(
        x=paired["dates"], y=paired["closes"], name="收盤價", mode="lines",
        line=dict(color="#2563eb", width=1.5),
        hovertemplate="%{x}<br>$%{y:.2f}<extra></extra>"), row=1, col=1)
    # 買入點
    fig_pair.add_trace(go.Scatter(
        x=buy_x, y=buy_y, name="買入", mode="markers",
        marker=dict(color="#0F6E56", size=10, symbol="triangle-up"),
        hovertemplate="買入<br>%{x}<br>$%{y:.2f}<extra></extra>"), row=1, col=1)
    # 賣出點
    fig_pair.add_trace(go.Scatter(
        x=sell_x, y=sell_y, name="賣出", mode="markers",
        marker=dict(color="#dc2626", size=10, symbol="triangle-down"),
        hovertemplate="賣出<br>%{x}<br>$%{y:.2f}<extra></extra>"), row=1, col=1)

    # 資產走勢
    fig_pair.add_trace(go.Scatter(
        x=eq_dates, y=eq_vals, name="配對策略",
        mode="lines", line=dict(color="#0F6E56", width=2),
        hovertemplate="%{x}<br>$%{y:,.0f}<extra></extra>"), row=2, col=1)
    fig_pair.add_trace(go.Scatter(
        x=eq_dates, y=bah_vals, name="買入持有",
        mode="lines", line=dict(color="rgba(136,136,136,0.6)", width=1.5, dash="dash"),
        hovertemplate="%{x}<br>$%{y:,.0f}<extra></extra>"), row=2, col=1)

    fig_pair.update_layout(
        title=dict(text="配對策略（Top10進場×Top10出場）", font=dict(size=12), x=0),
        hovermode="x unified", height=480,
        plot_bgcolor="white", paper_bgcolor="white",
        legend=dict(orientation="h", y=1.06, x=1, xanchor="right", font=dict(size=11)),
        margin=dict(l=55,r=20,t=50,b=30))
    fig_pair.update_xaxes(showgrid=True, gridcolor="#eee")
    fig_pair.update_yaxes(title_text="價格", showgrid=True, gridcolor="#eee",
                          tickformat=",.0f", row=1, col=1)
    fig_pair.update_yaxes(title_text="資產(USD)", showgrid=True, gridcolor="#eee",
                          tickformat=",.0f", row=2, col=1)

    ret_color = "#0F6E56" if paired["total_ret"] >= 0 else "#A32D2D"
    bah_color = "#0F6E56" if paired["bah_ret"]   >= 0 else "#A32D2D"
    beat = paired["final_val"] > paired["bah_final"]

    paired_box = html.Div([
        html.Div("📈 配對回測結果（Top10進場組合 + Top10出場組合）",
                 style={"fontSize":"13px","fontWeight":"500","color":"#1a1a1a",
                        "padding":"10px 12px","borderBottom":"0.5px solid #eee"}),
        html.Div([
            html.Div([
                html.Div("策略報酬", style={"fontSize":"10px","color":"#aaa"}),
                html.Div(f"{paired['total_ret']:+.2f}%",
                         style={"fontSize":"20px","fontWeight":"500","color":ret_color}),
                html.Div(f"${paired['final_val']/10000:.2f}萬",
                         style={"fontSize":"11px","color":ret_color}),
            ], style={"background":"#f0faf5" if beat else "#fff5f5","borderRadius":"8px",
                      "padding":"10px 14px","flex":"1",
                      "border":f"1.5px solid {'#0F6E56' if beat else '#A32D2D'}"}),
            html.Div([
                html.Div("買入持有", style={"fontSize":"10px","color":"#aaa"}),
                html.Div(f"{paired['bah_ret']:+.2f}%",
                         style={"fontSize":"20px","fontWeight":"500","color":bah_color}),
                html.Div(f"${paired['bah_final']/10000:.2f}萬",
                         style={"fontSize":"11px","color":bah_color}),
            ], style={"background":"#f5f5f3","borderRadius":"8px","padding":"10px 14px","flex":"1"}),
            html.Div([
                html.Div("最大回撤", style={"fontSize":"10px","color":"#aaa"}),
                html.Div(f"-{paired['max_dd']:.1f}%",
                         style={"fontSize":"20px","fontWeight":"500","color":"#A32D2D"}),
                html.Div(f"交易 {len(paired['trades'])} 筆",
                         style={"fontSize":"11px","color":"#888"}),
                html.Div("✅ 贏過買持" if beat else "❌ 輸給買持",
                         style={"fontSize":"11px","color":"#0F6E56" if beat else "#A32D2D"}),
            ], style={"background":"#f5f5f3","borderRadius":"8px","padding":"10px 14px","flex":"1"}),
        ], style={"display":"flex","gap":"8px","padding":"10px 12px","flexWrap":"wrap"}),
        dcc.Graph(figure=fig_pair, config={"displayModeBar":False}),
    ], style={"border":"0.5px solid #e5e5e5","borderRadius":"10px",
              "overflow":"hidden","background":"white","marginBottom":"12px"})

    return html.Div([
        html.P(f"分析標的：{ticker}　近3年日線資料 {len(day_closes)} 筆　從頭到尾單筆買入持有報酬：{bah_total:+.2f}%",
               style={"fontSize":"11px","color":"#aaa","margin":"0 0 10px"}),
        today_box,
        signals_box,
        paired_box,
        table,
    ])


def track_option_wall(ticker, put_wall, call_wall):
    """追蹤 Put/Call 牆的歷史移動，回傳變動分析文字"""
    filename = f"option_history_{ticker}.json"
    today    = datetime.date.today().isoformat()

    if os.path.exists(filename):
        with open(filename, "r") as f:
            history = json.load(f)
    else:
        history = {}

    prev_date = list(history.keys())[-1] if history else None
    put_note  = ""
    call_note = ""

    if prev_date and prev_date != today:
        prev_put  = history[prev_date].get("put_wall")
        prev_call = history[prev_date].get("call_wall")
        if prev_put is not None:
            if put_wall > prev_put:
                put_note = f"⚠️ 支撐上移：${prev_put}→${put_wall}（買盤轉強）"
            elif put_wall < prev_put:
                put_note = f"⚠️ 支撐下移：${prev_put}→${put_wall}（防守力轉弱）"
            else:
                put_note = "✅ 支撐穩定"
        if prev_call is not None:
            if call_wall > prev_call:
                call_note = f"⚠️ 阻力上移：${prev_call}→${call_wall}（賣壓減輕）"
            elif call_wall < prev_call:
                call_note = f"⚠️ 阻力下移：${prev_call}→${call_wall}（賣壓增強）"
            else:
                call_note = "✅ 阻力穩定"
    else:
        put_note  = "開始追蹤" if not prev_date else "今日已記錄"
        call_note = ""

    history[today] = {"put_wall": put_wall, "call_wall": call_wall}
    try:
        with open(filename, "w") as f:
            json.dump(history, f, indent=2)
    except: pass

    return put_note, call_note


@app.callback(
    Output("opt-div","children"),
    Input("opt-btn","n_clicks"),
    State("opt-ticker","value"),
    prevent_initial_call=True,
)
def update_options(n_clicks, ticker_input):
    if not n_clicks:
        return html.Div()

    ticker = (ticker_input or "QQQ").strip().upper()

    try:
        import yfinance as yf
        import math as _math

        t       = yf.Ticker(ticker)
        spot    = t.fast_info.get("lastPrice") or t.fast_info.last_price
        exps    = t.options
        if not exps:
            return html.P("無法取得期權數據", style={"color":"#aaa","fontSize":"13px"})

        today   = datetime.date.today()

        # 找最近4個有效到期日（跳過今天到期或已過期）
        exp_dates = []
        for e in exps[:8]:
            ed = datetime.date.fromisoformat(e)
            if ed <= today:
                continue  # 跳過今天及已過期
            exp_dates.append((e, ed, (ed - today).days))

        if not exp_dates:
            return html.P("目前無有效期權到期日（今日結算日，請明天再查）",
                          style={"color":"#d97706","fontSize":"13px"})

        # 抓近月和次月
        near_exps  = exp_dates[:2]

        # 結算日判斷（週五=4，週三=2）
        settle_flags = []
        for e, ed, days in near_exps:
            dow = ed.weekday()
            is_opex = (dow == 4)        # 月度結算通常是週五
            is_gamma = (dow in [2,4])   # 週三/週五 Gamma 影響大
            settle_flags.append((e, ed, days, is_opex, is_gamma))

        # 彙整各到期日的Put/Call牆
        all_puts  = {}  # strike → total OI
        all_calls = {}
        exp_data  = []

        for e, ed, days, is_opex, is_gamma in settle_flags:
            try:
                chain = t.option_chain(e)
                puts  = chain.puts[["strike","openInterest","impliedVolatility"]].dropna()
                calls = chain.calls[["strike","openInterest","impliedVolatility"]].dropna()

                # 累加跨月OI
                for _, row in puts.iterrows():
                    all_puts[row["strike"]]  = all_puts.get(row["strike"], 0)  + row["openInterest"]
                for _, row in calls.iterrows():
                    all_calls[row["strike"]] = all_calls.get(row["strike"], 0) + row["openInterest"]

                # 計算Gamma Exposure（近似：Gamma ≈ OI × 100 × spot × IV / sqrt(T) × 0.01）
                T = max(days/365, 1/365)
                def approx_gamma_exp(df, is_put=False):
                    gex = 0
                    for _, row in df.iterrows():
                        iv = row["impliedVolatility"]
                        oi = row["openInterest"]
                        k  = row["strike"]
                        if iv > 0 and oi > 0:
                            d1 = (_math.log(spot/k) + 0.5*iv*iv*T) / (iv*_math.sqrt(T))
                            gamma = _math.exp(-d1*d1/2) / (spot * iv * _math.sqrt(2*_math.pi*T))
                            g = gamma * oi * 100 * spot * spot * 0.01
                            gex += (-g if is_put else g)
                    return gex

                gex_call = approx_gamma_exp(calls, is_put=False)
                gex_put  = approx_gamma_exp(puts,  is_put=True)

                exp_data.append({
                    "exp": e, "days": days,
                    "is_opex": is_opex, "is_gamma": is_gamma,
                    "top_puts":  puts.nlargest(5,"openInterest")[["strike","openInterest"]].values.tolist(),
                    "top_calls": calls.nlargest(5,"openInterest")[["strike","openInterest"]].values.tolist(),
                    "gex_net": round((gex_call + gex_put)/1e6, 1),
                })
            except:
                continue

        if not exp_data:
            return html.P("期權數據解析失敗", style={"color":"#aaa","fontSize":"13px"})

        # 跨月雙重確認的強支撐/強阻力
        top_put_strikes  = sorted(all_puts,  key=lambda k: all_puts[k],  reverse=True)[:5]
        top_call_strikes = sorted(all_calls, key=lambda k: all_calls[k], reverse=True)[:5]

        # 找最強支撐（最大Put OI且低於現價）和最強阻力（最大Call OI且高於現價）
        support_strikes  = [(k, all_puts[k])  for k in top_put_strikes  if k < spot]
        resist_strikes   = [(k, all_calls[k]) for k in top_call_strikes if k > spot]

        main_support = support_strikes[0]  if support_strikes  else None
        main_resist  = resist_strikes[0]   if resist_strikes   else None

        # 跨月雙重確認
        def dual_confirm(strike, exp_data_list, side="put"):
            count = 0
            for ed in exp_data_list:
                key = "top_puts" if side=="put" else "top_calls"
                strikes_in_exp = [r[0] for r in ed[key]]
                if strike in strikes_in_exp:
                    count += 1
            return count >= 2

        # ── 建議 ──
        suggestions = []
        if main_support and main_resist:
            s_k, s_oi = main_support
            r_k, r_oi = main_resist
            s_dual = dual_confirm(s_k, exp_data, "put")
            r_dual = dual_confirm(r_k, exp_data, "call")

            dist_to_support = round((spot - s_k) / spot * 100, 1)
            dist_to_resist  = round((r_k - spot) / spot * 100, 1)

            if dist_to_support <= 2:
                suggestions.append(("🟢 接近支撐位", f"現價 ${spot:.2f} 距 Put牆 ${s_k} 只有 {dist_to_support}%，可考慮買入", "buy"))
            elif dist_to_resist <= 2:
                suggestions.append(("🔴 接近阻力位", f"現價 ${spot:.2f} 距 Call牆 ${r_k} 只有 {dist_to_resist}%，可考慮減倉", "sell"))
            else:
                suggestions.append(("⚪ 區間中段", f"現價 ${spot:.2f}，支撐 ${s_k}（-{dist_to_support}%），阻力 ${r_k}（+{dist_to_resist}%）", "neutral"))

        # 追蹤 Put/Call 牆歷史移動
        put_note, call_note = track_option_wall(
            ticker,
            main_support[0] if main_support else 0,
            main_resist[0]  if main_resist  else 0)

        # ── LINE 訊息格式 ──
        tw_time = datetime.datetime.now(
            datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")

        sug_text = suggestions[0][1] if suggestions else "區間中段，觀察方向"
        sug_icon = suggestions[0][0] if suggestions else "⚪"

        next_exp = exp_data[0] if exp_data else None
        exp_line = ""
        if next_exp:
            gamma_tag = "⚠️ Gamma高峰" if next_exp["is_gamma"] else ""
            opex_tag  = "📅 月結算" if next_exp["is_opex"] else ""
            exp_line  = (f"📅 結算日 {next_exp['exp']}（{next_exp['days']}天後）"
                        f"{'　'+gamma_tag if gamma_tag else ''}{'　'+opex_tag if opex_tag else ''}")

        # 跨月雙重確認（提前計算，供 LINE 訊息和 UI 使用）
        dual_puts  = [(k, all_puts[k])  for k in top_put_strikes  if dual_confirm(k, exp_data,"put")]
        dual_calls = [(k, all_calls[k]) for k in top_call_strikes if dual_confirm(k, exp_data,"call")]

        top_put_str  = "　".join([f"${k:.0f}(OI {oi/1000:.0f}k)" for k,oi in (dual_puts or support_strikes)[:2]])
        top_call_str = "　".join([f"${k:.0f}(OI {oi/1000:.0f}k)" for k,oi in (dual_calls or resist_strikes)[:2]])

        line_msg = (
            f"【⚡ {ticker} 期權結構】\n{tw_time}\n\n"
            f"📍 現價：${spot:.2f}\n"
            f"{sug_icon} {sug_text}\n\n"
            f"🔗 Put牆（支撐）：{top_put_str or '—'}\n"
            f"🔗 Call牆（阻力）：{top_call_str or '—'}\n"
        )
        if put_note:  line_msg += f"{put_note}\n"
        if call_note: line_msg += f"{call_note}\n"
        if exp_line:  line_msg += f"\n{exp_line}"

        # ── UI ──
        def oi_bar(oi, max_oi):
            pct = min(oi / max_oi * 100, 100) if max_oi > 0 else 0
            return html.Div(style={"background":"#e5e7eb","borderRadius":"3px","height":"6px","width":"80px","display":"inline-block","verticalAlign":"middle","marginLeft":"6px","overflow":"hidden"},
                            children=[html.Div(style={"background":"#7c3aed","width":f"{pct}%","height":"100%","borderRadius":"3px"})])

        # 到期日卡片
        exp_cards = []
        for ed in exp_data:
            gamma_flag = "⚠️ Gamma高峰日" if ed["is_gamma"] else ""
            opex_flag  = "📅 月結算日" if ed["is_opex"] else ""
            exp_cards.append(html.Div([
                html.Div([
                    html.Span(ed["exp"], style={"fontWeight":"500","fontSize":"13px"}),
                    html.Span(f"  {ed['days']}天後", style={"fontSize":"11px","color":"#aaa","marginLeft":"6px"}),
                    html.Span(f"  {opex_flag} {gamma_flag}", style={"fontSize":"11px","color":"#d97706","marginLeft":"6px"}),
                ], style={"marginBottom":"6px"}),
                html.Div([
                    html.Div([
                        html.Div("Put牆（支撐）", style={"fontSize":"10px","color":"#0F6E56","marginBottom":"3px","fontWeight":"500"}),
                        *[html.Div([
                            html.Span(f"${r[0]:.0f}", style={"fontSize":"12px","fontWeight":"500"}),
                            html.Span(f"  OI {r[1]:,.0f}", style={"fontSize":"11px","color":"#888"}),
                            oi_bar(r[1], ed["top_puts"][0][1] if ed["top_puts"] else 1),
                        ], style={"marginBottom":"2px"}) for r in ed["top_puts"][:3]],
                    ], style={"flex":"1"}),
                    html.Div([
                        html.Div("Call牆（阻力）", style={"fontSize":"10px","color":"#A32D2D","marginBottom":"3px","fontWeight":"500"}),
                        *[html.Div([
                            html.Span(f"${r[0]:.0f}", style={"fontSize":"12px","fontWeight":"500"}),
                            html.Span(f"  OI {r[1]:,.0f}", style={"fontSize":"11px","color":"#888"}),
                            oi_bar(r[1], ed["top_calls"][0][1] if ed["top_calls"] else 1),
                        ], style={"marginBottom":"2px"}) for r in ed["top_calls"][:3]],
                    ], style={"flex":"1"}),
                ], style={"display":"flex","gap":"16px"}),
                html.Div(f"Net GEX：{ed['gex_net']:+.1f}M　{'正GEX→做市商賣Gamma，價格被磁吸穩定' if ed['gex_net']>0 else '負GEX→做市商買Gamma，波動放大'}",
                         style={"fontSize":"11px","color":"#7c3aed","marginTop":"6px"}),
            ], style={"background":"white","borderRadius":"10px","padding":"12px 14px",
                      "border":"0.5px solid #e5e5e5","marginBottom":"10px"}))

        # 跨月雙重確認 UI
        max_put_oi  = max(all_puts.values())  if all_puts  else 1
        max_call_oi = max(all_calls.values()) if all_calls else 1

        dual_box = html.Div([
            html.Div("🔗 跨月雙重確認（近月+次月同時存在 → 訊號更強）",
                     style={"fontSize":"12px","fontWeight":"500","color":"#1a1a1a","marginBottom":"8px"}),
            html.Div([
                html.Div([
                    html.Div("✅ 強支撐（雙月Put牆）", style={"fontSize":"11px","color":"#0F6E56","marginBottom":"4px"}),
                ] + ([html.Div(f"${k:.0f}　累計OI {oi:,.0f}", style={"fontSize":"12px","fontWeight":"500","color":"#0F6E56"})
                      for k,oi in dual_puts[:3]] or [html.Div("無", style={"fontSize":"11px","color":"#aaa"})]),
                style={"flex":"1"}),
                html.Div([
                    html.Div("🚫 強阻力（雙月Call牆）", style={"fontSize":"11px","color":"#A32D2D","marginBottom":"4px"}),
                ] + ([html.Div(f"${k:.0f}　累計OI {oi:,.0f}", style={"fontSize":"12px","fontWeight":"500","color":"#A32D2D"})
                      for k,oi in dual_calls[:3]] or [html.Div("無", style={"fontSize":"11px","color":"#aaa"})]),
                style={"flex":"1"}),
            ], style={"display":"flex","gap":"16px"}),
        ], style={"background":"#f5f5f3","borderRadius":"10px","padding":"12px 14px","marginBottom":"10px"})

        # 買賣建議
        sug_color = {"buy":"#0F6E56","sell":"#A32D2D","neutral":"#888"}
        sug_bg    = {"buy":"#f0faf5","sell":"#fff5f5","neutral":"#f5f5f3"}
        sug_box   = html.Div([
            html.Div([
                html.Div(s[0], style={"fontSize":"14px","fontWeight":"500",
                                      "color":sug_color[s[2]]}),
                html.Div(s[1], style={"fontSize":"12px","color":"#555","marginTop":"3px"}),
            ], style={"background":sug_bg[s[2]],"borderRadius":"10px","padding":"12px 16px",
                      "border":f"1.5px solid {sug_color[s[2]]}","marginBottom":"8px"})
            for s in suggestions
        ])

        spot_label = html.Div(
            f"📍 {ticker} 現價：${spot:.2f}",
            style={"fontSize":"14px","fontWeight":"500","color":"#1a1a1a","marginBottom":"12px"})

        line_preview = html.Div([
            html.Div("📱 LINE 訊息預覽", style={"fontSize":"12px","fontWeight":"500",
                     "color":"#1a1a1a","marginBottom":"8px"}),
            html.Pre(line_msg, style={"fontSize":"12px","color":"#333","whiteSpace":"pre-wrap",
                     "background":"#f5f5f3","borderRadius":"8px","padding":"10px 12px",
                     "margin":"0 0 8px","fontFamily":"sans-serif","lineHeight":"1.6"}),
            html.Button("📤 發送到 LINE", id="opt-line-btn", n_clicks=0,
                        **{"data-msg": line_msg},
                        style={"padding":"6px 16px","background":"#06c755","color":"#fff",
                               "border":"none","borderRadius":"6px","cursor":"pointer","fontSize":"13px"}),
            html.Div(id="opt-line-msg", style={"fontSize":"12px","color":"#0F6E56","marginTop":"6px"}),
        ], style={"border":"0.5px solid #e5e5e5","borderRadius":"10px","padding":"12px 14px",
                  "background":"white","marginBottom":"10px"})

        return html.Div([spot_label, sug_box, line_preview, dual_box] + exp_cards)

    except Exception as e:
        return html.P(f"錯誤：{e}", style={"color":"#dc2626","fontSize":"13px"})


@app.callback(
    Output("opt-line-msg","children"),
    Input("opt-line-btn","n_clicks"),
    State("opt-line-btn","data-msg"),
    prevent_initial_call=True,
)
def send_opt_line(n_clicks, msg):
    if not n_clicks or not msg:
        return ""
    try:
        send_line(msg)
        return "✅ 已發送到 LINE"
    except Exception as e:
        return f"❌ 發送失敗：{e}"


if __name__ == "__main__":
    t = threading.Thread(target=scheduler_loop, daemon=True)
    t.start()
    print("啟動中，請用瀏覽器開啟 http://localhost:8050")
    app.run(debug=False, host="0.0.0.0", port=8050)

# ── 台股月K篩選 ───────────────────────────────────────────────

# 台灣中型100成分股（2024年底版本，Yahoo Finance格式需加.TW）
TW_MID100 = {
    "2303":"聯電",    "2308":"台達電",  "2317":"鴻海",    "2324":"仁寶",
    "2327":"國巨",    "2328":"廣宇",    "2330":"台積電",  "2331":"精英",
    "2337":"旺宏",    "2347":"聯強",    "2352":"佳世達",  "2353":"宏碁",
    "2354":"鴻準",    "2356":"英業達",  "2357":"華碩",    "2360":"致茂",
    "2362":"藍天",    "2363":"擎華",    "2368":"金像電",  "2371":"大同",
    "2376":"技嘉",    "2377":"微星",    "2379":"瑞昱",    "2382":"廣達",
    "2383":"台光電",  "2385":"群光",    "2392":"正崴",    "2395":"研華",
    "2408":"南亞科",  "2409":"友達",    "2413":"環科",    "2414":"精技",
    "2415":"今國光",  "2421":"建準",    "2423":"固緯",    "2426":"鑫永銓",
    "2429":"銘旺科",  "2441":"超豐",    "2448":"晶電",    "2449":"京元電子",
    "2450":"神腦",    "2451":"創見",    "2455":"全新",    "2456":"奇力新",
    "2458":"義隆",    "2460":"建通",    "2461":"光群雷",  "2474":"可成",
    "2478":"大毅",    "2481":"強茂",    "2485":"兆赫",    "2488":"漢平",
    "2492":"華新科",  "2496":"卓越",    "2498":"宏達電",  "2501":"國建",
    "2504":"國產",    "2511":"太子",    "2515":"中工",    "2520":"冠德",
    "2548":"華固",    "2603":"長榮",    "2609":"陽明",    "2615":"萬海",
    "2618":"長榮航",  "2633":"台灣高鐵","2634":"漢翔",    "2636":"台驊",
    "2637":"慧洋",    "2641":"正德",    "2642":"宅配通",  "2645":"大榮",
    "2711":"晶華",    "2727":"王品",    "2809":"京城銀",  "2812":"台中銀",
    "2820":"華票",    "2823":"中壽",    "2824":"山富",    "2832":"台產",
    "2834":"臺企銀",  "2836":"遠東銀",  "2838":"聯邦銀",  "2845":"遠銀",
    "2847":"大眾銀",  "2849":"安泰銀",  "2850":"新產",    "2851":"中再保",
    "2852":"第一保",  "2855":"統一證",  "2856":"元富",    "2880":"華南金",
    "2881":"富邦金",  "2882":"國泰金",  "2883":"開發金",  "2884":"玉山金",
    "2885":"元大金",  "2886":"兆豐金",  "2887":"台新金",  "2888":"新光金",
    "2889":"國票金",  "2890":"永豐金",  "2891":"中信金",  "2892":"第一金",
    "2897":"王道銀",  "3034":"聯詠",    "3037":"欣興",    "3044":"健鼎",
    "3045":"台灣大",  "3149":"正達",    "3189":"景碩",    "3653":"健策",
    "4904":"遠傳",    "4938":"和碩",    "4958":"臻鼎-KY","5871":"中租-KY",
    "5876":"上海商銀","5880":"合庫金",  "6176":"瑞儀",    "6214":"精誠",
    "6269":"台郡",    "6278":"台表科",  "6285":"啟碁",    "6414":"樺漢",
    "6446":"藥華藥",  "6669":"緯穎",    "6770":"力積電",  "8088":"品安",
    "8046":"南電",    "9910":"豐泰",
}

@app.callback(
    Output("tw-screen-div","children"),
    Output("tw-screen-btn","children"),
    Input("tw-screen-btn","n_clicks"),
    prevent_initial_call=True,
)
def update_tw_screen(n_clicks):
    if not n_clicks:
        return html.Div(), "🔍 執行台股月K篩選"

    end_dt   = datetime.datetime.now(tz=timezone.utc)
    start_dt = end_dt - datetime.timedelta(days=365)  # 抓1年月K

    # 判斷目前月份
    now_tw = datetime.datetime.now(tz=datetime.timezone(datetime.timedelta(hours=8)))
    this_month = now_tw.strftime("%Y-%m")
    this_year  = now_tw.year
    this_mon   = now_tw.month

    buy_list  = []  # 紅K且收高於上月
    avoid_list= []  # 黑K且低於5月均價

    errors = []
    total = len(TW_MID100)
    processed = 0

    for code in TW_MID100.keys():
        name = TW_MID100.get(code, "")
        sym = f"{code}.TW"
        try:
            dates, closes, _, opens = fetch_yahoo_range(sym, start_dt, end_dt, "1mo")
            if len(closes) < 6:
                continue

            # 月份標籤
            def get_ym(d): return d[:7]

            # 找6月的月K（最新月份）
            paired = list(zip(dates, closes, opens))
            # 最新的一個月
            last_date, last_close, last_open = paired[-1]
            prev_date, prev_close, prev_open = paired[-2] if len(paired)>=2 else (None,None,None)

            last_ym = get_ym(last_date)

            # 只看6月
            if last_ym != this_month and not last_ym.endswith(f"-{this_mon:02d}"):
                # 找明確的6月資料
                jun_data = [(d,c,o) for d,c,o in paired if get_ym(d).endswith(f"-{this_mon:02d}")]
                if not jun_data:
                    continue
                last_date, last_close, last_open = jun_data[-1]
                idx = paired.index(jun_data[-1])
                prev_close = paired[idx-1][1] if idx > 0 else None
            else:
                idx = len(paired) - 1

            if prev_close is None:
                continue

            # 5個月均價（6月之前的5個月）
            prev5 = [c for _,c,_ in paired[max(0,idx-5):idx]]
            ma5 = sum(prev5) / len(prev5) if prev5 else None

            is_red_k   = last_close > last_open          # 紅K
            above_prev = last_close > prev_close          # 收高於上月
            below_ma5  = ma5 and last_close < ma5         # 低於5月均

            chg = round((last_close - prev_close) / prev_close * 100, 1) if prev_close else 0

            row = {
                "code": code, "name": name, "sym": sym,
                "close": last_close, "open": last_open,
                "prev_close": prev_close,
                "ma5": round(ma5, 1) if ma5 else None,
                "chg": chg,
                "is_red": is_red_k,
            }

            if is_red_k and above_prev:
                buy_list.append(row)
            elif not is_red_k and below_ma5:
                avoid_list.append(row)

            processed += 1
        except:
            continue

    # 排序：買入清單依漲幅排序
    buy_list.sort(key=lambda x: x["chg"], reverse=True)
    avoid_list.sort(key=lambda x: x["chg"])

    th = {"padding":"5px 10px","textAlign":"right","fontSize":"11px",
          "color":"#888","borderBottom":"1px solid #eee"}
    th_l = {**th, "textAlign":"left"}

    def make_table(rows, title, color, icon):
        if not rows:
            return html.Div(html.P(f"{icon} {title}：無符合條件的股票",
                            style={"fontSize":"13px","color":"#aaa","padding":"10px"}))
        trs = []
        for r in rows:
            chg_color = "#0F6E56" if r["chg"]>=0 else "#A32D2D"
            trs.append(html.Tr([
                html.Td(html.Div([
                    html.Span(r["code"], style={"fontWeight":"500"}),
                    html.Span(f" {r.get('name','')}", style={"color":"#888","fontSize":"11px","marginLeft":"4px"}),
                ]), style={"padding":"5px 10px","whiteSpace":"nowrap"}),
                html.Td(f"{r['close']:.1f}", style={"padding":"5px 10px","textAlign":"right"}),
                html.Td(f"{r['open']:.1f}",  style={"padding":"5px 10px","textAlign":"right"}),
                html.Td(f"{r['prev_close']:.1f}", style={"padding":"5px 10px","textAlign":"right"}),
                html.Td(f"{r['ma5']:.1f}" if r['ma5'] else "—", style={"padding":"5px 10px","textAlign":"right","color":"#7c3aed"}),
                html.Td(f"{r['chg']:+.1f}%", style={"padding":"5px 10px","textAlign":"right","fontWeight":"500","color":chg_color}),
                html.Td("🔴 黑K" if not r["is_red"] else "🟢 紅K",
                        style={"padding":"5px 10px","textAlign":"center"}),
            ], style={"borderBottom":"0.5px solid #f5f5f5"}))

        return html.Div([
            html.Div(f"{icon} {title}（{len(rows)}支）",
                     style={"fontSize":"13px","fontWeight":"500","color":color,
                            "padding":"10px 12px","borderBottom":"0.5px solid #eee"}),
            html.Div(html.Table([
                html.Thead(html.Tr([
                    html.Th("代號／名稱",style=th_l),
                    html.Th("6月收",style=th), html.Th("6月開",style=th),
                    html.Th("5月收",style=th), html.Th("5月均價",style={**th,"color":"#7c3aed"}),
                    html.Th("月漲幅",style=th), html.Th("K線",style=th),
                ], style={"background":"#f9f9f9"})),
                html.Tbody(trs),
            ], style={"width":"100%","borderCollapse":"collapse","fontSize":"12px"}),
            style={"overflowX":"auto"}),
        ])

    today_str = now_tw.strftime("%Y-%m-%d")
    return html.Div([
        html.P(f"篩選日期：{today_str}　共掃描 {processed} 支中型100成分股",
               style={"fontSize":"11px","color":"#aaa","margin":"0 0 12px"}),
        html.Div([
            html.Div(make_table(buy_list, "✅ 買入觀察（紅K + 收高於上月）", "#0F6E56", "✅"),
                     style={"flex":"1","minWidth":"300px","border":"0.5px solid #e5e5e5",
                            "borderRadius":"10px","overflow":"hidden","background":"white"}),
            html.Div(make_table(avoid_list, "❌ 避開名單（黑K + 低於5月均價）", "#dc2626", "❌"),
                     style={"flex":"1","minWidth":"300px","border":"0.5px solid #e5e5e5",
                            "borderRadius":"10px","overflow":"hidden","background":"white"}),
        ], style={"display":"flex","gap":"14px","flexWrap":"wrap"}),
    ]), "🔍 重新篩選"

