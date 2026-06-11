"""
股票滾動年化斜率 + 恐懼指標互動圖表
- 成交量可勾選顯示/隱藏
- 自動抓最近一年資料（不需選年份）
"""

import datetime, math, csv, io
from datetime import timezone
import requests
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import dash
from dash import dcc, html, Input, Output, State

COLORS = [
    "#2563eb", "#16a34a", "#dc2626", "#d97706",
    "#7c3aed", "#0891b2", "#db2777", "#65a30d",
    "#b45309", "#0f766e",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": "https://finance.yahoo.com",
}

def fetch_yahoo_range(ticker, start_dt, end_dt):
    start = int(start_dt.timestamp())
    end   = int(end_dt.timestamp())
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
           f"?interval=1d&period1={start}&period2={end}")
    r = requests.get(url, headers=HEADERS, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"Yahoo Finance 回應 {r.status_code}")
    data = r.json()
    chart = data.get("chart", {})
    if chart.get("error"):
        raise RuntimeError(chart["error"].get("description", "未知錯誤"))
    result = chart.get("result")
    if not result:
        raise RuntimeError("找不到資料")
    result = result[0]
    timestamps = result["timestamp"]
    quote = result["indicators"]["quote"][0]
    closes_raw = quote.get("close", [])
    volumes_raw = quote.get("volume", [])
    dates, closes, volumes = [], [], []
    for ts, c, v in zip(timestamps, closes_raw, volumes_raw):
        if c is None:
            continue
        dates.append(datetime.datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d"))
        closes.append(float(c))
        volumes.append(float(v) if v is not None else 0.0)
    if not dates:
        raise RuntimeError("無資料")
    return dates, closes, volumes


def fetch_vix(start_dt, end_dt):
    try:
        dates, closes, _ = fetch_yahoo_range("%5EVIX", start_dt, end_dt)
        return dict(zip(dates, closes))
    except Exception:
        return {}


def fetch_fear_greed():
    try:
        url = "https://api.alternative.me/fng/?limit=365&format=json"
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return {}
        data = r.json().get("data", [])
        result = {}
        for item in data:
            ts = int(item["timestamp"])
            d = datetime.datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            result[d] = int(item["value"])
        return result
    except Exception:
        return {}


def fetch_put_call_ratio(start_dt, end_dt):
    try:
        url = "https://www.cboe.com/publish/scheduledtask/mktdata/datahouse/equitypc.csv"
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return {}
        reader = csv.reader(io.StringIO(r.text))
        result = {}
        for row in reader:
            if len(row) < 2:
                continue
            try:
                d = datetime.datetime.strptime(row[0].strip(), "%m/%d/%Y")
                if start_dt.date() <= d.date() <= end_dt.date():
                    result[d.strftime("%Y-%m-%d")] = float(row[-1])
            except Exception:
                continue
        return result
    except Exception:
        return {}


def rolling_annualized_log_slope(closes, window):
    n = len(closes)
    out = [float("nan")] * n
    t = list(range(window))
    t_mean = sum(t) / window
    Sxx = sum((ti - t_mean) ** 2 for ti in t)
    for i in range(window - 1, n):
        sub = closes[i - window + 1 : i + 1]
        if all(v == sub[0] for v in sub):
            out[i] = 0.0
            continue
        y = [math.log(max(v, 1e-12)) for v in sub]
        y_mean = sum(y) / window
        Sxy = sum((t[k] - t_mean) * (y[k] - y_mean) for k in range(window))
        b = Sxy / Sxx
        out[i] = (math.exp(b * 252) - 1) * 100
    return out


# ── Dash 應用程式 ───────────────────────────────────────────

app = dash.Dash(__name__)
server = app.server
app.title = "股票滾動年化斜率"

app.layout = html.Div([
    html.H2("股票收盤價 × 滾動年化斜率 × 恐懼指標",
            style={"fontFamily": "sans-serif", "marginBottom": "4px"}),
    html.P("自動顯示最近一年資料，每支股票獨立一張圖",
           style={"fontFamily": "sans-serif", "color": "#888", "marginBottom": "16px"}),

    html.Div([
        html.Div([
            html.Label("股票代號（逗號分隔）", style={"fontSize": "12px", "color": "#888"}),
            dcc.Input(id="ticker", value="QQQ, VOO, TSM", type="text",
                      style={"width": "320px", "textTransform": "uppercase",
                             "padding": "6px 8px", "borderRadius": "6px",
                             "border": "1px solid #ddd", "fontSize": "14px"}),
        ], style={"display": "flex", "flexDirection": "column", "gap": "4px"}),

        html.Div([
            html.Label("視窗天數", style={"fontSize": "12px", "color": "#888"}),
            dcc.Input(id="window", value=10, type="number", min=2, max=60,
                      style={"width": "80px", "padding": "6px 8px",
                             "borderRadius": "6px", "border": "1px solid #ddd",
                             "fontSize": "14px"}),
        ], style={"display": "flex", "flexDirection": "column", "gap": "4px"}),

        html.Div([
            html.Label("顯示選項", style={"fontSize": "12px", "color": "#888"}),
            dcc.Checklist(
                id="show-volume",
                options=[{"label": "　顯示成交量", "value": "vol"}],
                value=["vol"],
                style={"fontSize": "14px", "color": "#333",
                       "paddingTop": "6px"},
            ),
        ], style={"display": "flex", "flexDirection": "column", "gap": "4px"}),

        html.Button("更新", id="run-btn", n_clicks=0,
                    style={"alignSelf": "flex-end", "padding": "8px 20px",
                           "background": "#1a1a1a", "color": "#fff",
                           "border": "none", "borderRadius": "6px",
                           "cursor": "pointer", "fontSize": "14px"}),
    ], style={"display": "flex", "flexWrap": "wrap", "gap": "16px",
              "alignItems": "flex-end", "background": "#f5f5f3",
              "borderRadius": "10px", "padding": "14px 16px",
              "marginBottom": "16px"}),

    html.Div(id="status-msg",
             style={"fontSize": "13px", "color": "#888", "minHeight": "20px",
                    "marginBottom": "8px", "fontFamily": "sans-serif"}),

    html.Div(id="charts-container"),

    html.H3("恐懼指標", style={"fontFamily": "sans-serif", "marginTop": "24px",
                               "marginBottom": "4px", "fontSize": "16px"}),
    html.P(id="fear-status",
           style={"fontSize": "12px", "color": "#aaa", "marginBottom": "8px",
                  "fontFamily": "sans-serif"}),
    html.Div(id="fear-charts"),

    html.P("VIX >30 為高恐懼。Fear & Greed：0=極度恐懼，100=極度貪婪（加密市場版）。Put/Call Ratio >1 偏恐懼。",
           style={"fontSize": "12px", "color": "#aaa", "marginTop": "12px",
                  "fontFamily": "sans-serif"}),
], style={"maxWidth": "1000px", "margin": "2rem auto", "padding": "0 1.5rem",
          "fontFamily": "sans-serif"})


@app.callback(
    Output("charts-container", "children"),
    Output("fear-charts", "children"),
    Output("status-msg", "children"),
    Output("fear-status", "children"),
    Input("run-btn", "n_clicks"),
    State("ticker", "value"),
    State("window", "value"),
    State("show-volume", "value"),
    prevent_initial_call=False,
)
def update_charts(n_clicks, ticker_str, window, show_volume):
    tickers = [t.strip().upper() for t in (ticker_str or "QQQ").split(",") if t.strip()]
    window = int(window or 10)
    show_vol = "vol" in (show_volume or [])

    # 最近一年時間範圍
    end_dt   = datetime.datetime.now(tz=timezone.utc)
    start_dt = end_dt - datetime.timedelta(days=365)
    date_range_str = f"{start_dt.strftime('%Y-%m-%d')} ～ {end_dt.strftime('%Y-%m-%d')}"

    # ── 個股圖表 ──
    chart_divs = []
    messages = []

    for i, ticker in enumerate(tickers[:12]):
        color = COLORS[i % len(COLORS)]
        try:
            dates, closes, volumes = fetch_yahoo_range(ticker, start_dt, end_dt)
        except Exception as e:
            messages.append(f"❌ {ticker}: {e}")
            chart_divs.append(html.Div(f"❌ {ticker}：{e}",
                style={"padding": "12px", "color": "#dc2626", "fontSize": "13px",
                       "background": "#fff5f5", "borderRadius": "8px",
                       "marginBottom": "12px"}))
            continue

        slopes = rolling_annualized_log_slope(closes, window)
        slope_line = [None if math.isnan(v) else v for v in slopes]
        pos_s = [v if (v is not None and v > 0) else 0 for v in slope_line]
        neg_s = [v if (v is not None and v < 0) else 0 for v in slope_line]

        vol_colors = []
        for k in range(len(closes)):
            if k == 0:
                vol_colors.append("rgba(150,150,150,0.5)")
            elif closes[k] >= closes[k-1]:
                vol_colors.append("rgba(34,160,107,0.5)")
            else:
                vol_colors.append("rgba(226,72,61,0.5)")

        if show_vol:
            fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                                row_heights=[0.68, 0.32], vertical_spacing=0.04,
                                specs=[[{"secondary_y": True}], [{"secondary_y": False}]])
            chart_height = 420
        else:
            fig = make_subplots(rows=1, cols=1,
                                specs=[[{"secondary_y": True}]])
            chart_height = 300

        # 斜率填色
        fig.add_trace(go.Bar(x=dates, y=pos_s, name="上升",
            marker_color="rgba(34,160,107,0.35)", showlegend=False,
            hovertemplate="%{x}<br>斜率: %{y:.1f}%<extra></extra>"),
            row=1, col=1, secondary_y=False)
        fig.add_trace(go.Bar(x=dates, y=neg_s, name="下跌",
            marker_color="rgba(226,72,61,0.35)", showlegend=False,
            hovertemplate="%{x}<br>斜率: %{y:.1f}%<extra></extra>"),
            row=1, col=1, secondary_y=False)

        # 斜率折線
        fig.add_trace(go.Scatter(x=dates, y=slope_line,
            name=f"{window}日年化斜率", mode="lines",
            line=dict(color="#444", width=1.2),
            hovertemplate="%{x}<br>斜率: %{y:.2f}%<extra></extra>"),
            row=1, col=1, secondary_y=False)

        # 股價折線
        fig.add_trace(go.Scatter(x=dates, y=closes,
            name="收盤價", mode="lines",
            line=dict(color=color, width=2),
            hovertemplate="%{x}<br>收盤: $%{y:.2f}<extra></extra>"),
            row=1, col=1, secondary_y=True)

        # 成交量（可選）
        if show_vol:
            fig.add_trace(go.Bar(x=dates, y=volumes, name="成交量",
                marker_color=vol_colors,
                hovertemplate="%{x}<br>成交量: %{y:,.0f}<extra></extra>"),
                row=2, col=1)

        fig.add_hline(y=0, line_color="#ccc", line_width=1, row=1, col=1)

        fig.update_layout(
            title=dict(text=f"<b>{ticker}</b>　{window}日滾動年化斜率　（{date_range_str}）",
                       font=dict(size=13, color="#1a1a1a"), x=0),
            barmode="overlay", bargap=0, hovermode="x unified",
            legend=dict(orientation="h", yanchor="bottom", y=1.06,
                        xanchor="right", x=1, font=dict(size=11)),
            plot_bgcolor="white", paper_bgcolor="white",
            margin=dict(l=60, r=65, t=50, b=40),
            font=dict(family="sans-serif", size=12), height=chart_height,
        )
        fig.update_xaxes(showgrid=True, gridcolor="#eee")
        fig.update_yaxes(title_text="年化斜率(%)", secondary_y=False,
                         showgrid=True, gridcolor="#eee", zeroline=False,
                         title_font=dict(size=10), row=1, col=1)
        fig.update_yaxes(title_text="收盤價(USD)", secondary_y=True,
                         showgrid=False, zeroline=False,
                         title_font=dict(size=10, color=color),
                         tickfont=dict(color=color), row=1, col=1)
        if show_vol:
            fig.update_yaxes(title_text="成交量", showgrid=True, gridcolor="#eee",
                             zeroline=False, title_font=dict(size=10),
                             tickformat=".2s", row=2, col=1)

        chart_divs.append(html.Div(
            dcc.Graph(figure=fig, config={"displayModeBar": False}),
            style={"marginBottom": "16px", "border": "0.5px solid #e5e5e5",
                   "borderRadius": "10px", "overflow": "hidden", "background": "white"}
        ))
        messages.append(f"✅ {ticker} {len(dates)}日")

    # ── 恐懼指標 ──
    vix_data = fetch_vix(start_dt, end_dt)
    fng_data = fetch_fear_greed()
    pcr_data = fetch_put_call_ratio(start_dt, end_dt)

    # 過濾 Fear & Greed 到時間範圍內
    fng_data = {d: v for d, v in fng_data.items()
                if start_dt.strftime("%Y-%m-%d") <= d <= end_dt.strftime("%Y-%m-%d")}

    fear_notes = []
    fear_divs  = []

    def make_fear_fig(x, y, title, color, y_label, ref_line=None, ref_label=None, fill=False):
        fig = go.Figure()
        fill_arg = "tozeroy" if fill else None
        fill_color = color.replace("rgb(", "rgba(").replace(")", ",0.15)") if fill else None
        fig.add_trace(go.Scatter(
            x=x, y=y, mode="lines", name=title,
            line=dict(color=color, width=1.5),
            fill=fill_arg, fillcolor=fill_color,
            hovertemplate="%{x}<br>" + y_label + ": %{y:.2f}<extra></extra>",
        ))
        if ref_line is not None:
            fig.add_hline(y=ref_line, line_color="#aaa", line_dash="dash",
                          line_width=1, annotation_text=ref_label,
                          annotation_position="right")
        fig.update_layout(
            title=dict(text=f"<b>{title}</b>　（{date_range_str}）",
                       font=dict(size=13, color="#1a1a1a"), x=0),
            plot_bgcolor="white", paper_bgcolor="white",
            hovermode="x unified", showlegend=False,
            margin=dict(l=55, r=20, t=45, b=40),
            xaxis=dict(showgrid=True, gridcolor="#eee"),
            yaxis=dict(title_text=y_label, showgrid=True, gridcolor="#eee",
                       zeroline=False, title_font=dict(size=11)),
            font=dict(family="sans-serif", size=12), height=240,
        )
        return fig

    if vix_data:
        vx = sorted(vix_data.keys())
        vy = [vix_data[d] for d in vx]
        fear_divs.append(html.Div(
            dcc.Graph(figure=make_fear_fig(vx, vy, "VIX 市場恐懼指數",
                      "rgb(220,72,61)", "VIX", 30, "高恐懼 30", fill=True),
                      config={"displayModeBar": False}),
            style={"marginBottom": "12px", "border": "0.5px solid #e5e5e5",
                   "borderRadius": "10px", "overflow": "hidden", "background": "white"}
        ))
        fear_notes.append("✅ VIX")
    else:
        fear_notes.append("❌ VIX 無資料")

    if fng_data:
        fg = sorted(fng_data.keys())
        fv = [fng_data[d] for d in fg]
        fear_divs.append(html.Div(
            dcc.Graph(figure=make_fear_fig(fg, fv, "Fear & Greed Index（加密市場）",
                      "rgb(214,140,0)", "指數（0=極恐懼 100=極貪婪）", 50, "中性 50"),
                      config={"displayModeBar": False}),
            style={"marginBottom": "12px", "border": "0.5px solid #e5e5e5",
                   "borderRadius": "10px", "overflow": "hidden", "background": "white"}
        ))
        fear_notes.append("✅ Fear & Greed")
    else:
        fear_notes.append("❌ Fear & Greed 無資料")

    if pcr_data:
        px_ = sorted(pcr_data.keys())
        py_ = [pcr_data[d] for d in px_]
        fear_divs.append(html.Div(
            dcc.Graph(figure=make_fear_fig(px_, py_, "Put/Call Ratio（CBOE）",
                      "rgb(124,58,237)", "Put/Call Ratio", 1.0, "恐懼線 1.0"),
                      config={"displayModeBar": False}),
            style={"marginBottom": "12px", "border": "0.5px solid #e5e5e5",
                   "borderRadius": "10px", "overflow": "hidden", "background": "white"}
        ))
        fear_notes.append("✅ Put/Call Ratio")
    else:
        fear_notes.append("❌ Put/Call Ratio 無資料")

    return (chart_divs, fear_divs,
            "　".join(messages),
            "　".join(fear_notes))


if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=8050)
