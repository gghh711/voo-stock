"""
股票滾動年化斜率互動圖表（含成交量版）
"""

import datetime, math
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

def fetch_yahoo(ticker, year):
    start = int(datetime.datetime(year, 1, 1, tzinfo=timezone.utc).timestamp())
    end   = int(datetime.datetime(year, 12, 31, 23, 59, tzinfo=timezone.utc).timestamp())
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        f"?interval=1d&period1={start}&period2={end}"
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Referer": "https://finance.yahoo.com",
    }
    r = requests.get(url, headers=headers, timeout=15)
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
        raise RuntimeError(f"{ticker} {year} 年無資料")
    return dates, closes, volumes

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

app = dash.Dash(__name__)
server = app.server
app.title = "股票滾動年化斜率"

app.layout = html.Div([
    html.H2("股票收盤價 × 滾動年化斜率 × 成交量",
            style={"fontFamily": "sans-serif", "marginBottom": "4px"}),
    html.P("每支股票獨立一張圖，上方為斜率與股價，下方為成交量",
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
            html.Label("年份", style={"fontSize": "12px", "color": "#888"}),
            dcc.Input(id="year", value=2026, type="number", min=2000, max=2030,
                      style={"width": "80px", "padding": "6px 8px",
                             "borderRadius": "6px", "border": "1px solid #ddd",
                             "fontSize": "14px"}),
        ], style={"display": "flex", "flexDirection": "column", "gap": "4px"}),

        html.Div([
            html.Label("視窗天數", style={"fontSize": "12px", "color": "#888"}),
            dcc.Input(id="window", value=10, type="number", min=2, max=60,
                      style={"width": "80px", "padding": "6px 8px",
                             "borderRadius": "6px", "border": "1px solid #ddd",
                             "fontSize": "14px"}),
        ], style={"display": "flex", "flexDirection": "column", "gap": "4px"}),

        html.Button("更新", id="run-btn", n_clicks=0,
                    style={"alignSelf": "flex-end", "padding": "8px 20px",
                           "background": "#1a1a1a", "color": "#fff",
                           "border": "none", "borderRadius": "6px",
                           "cursor": "pointer", "fontSize": "14px"}),
    ], style={"display": "flex", "flexWrap": "wrap", "gap": "12px",
              "alignItems": "flex-end", "background": "#f5f5f3",
              "borderRadius": "10px", "padding": "14px 16px",
              "marginBottom": "16px"}),

    html.Div(id="status-msg",
             style={"fontSize": "13px", "color": "#888", "minHeight": "20px",
                    "marginBottom": "8px", "fontFamily": "sans-serif"}),

    html.Div(id="charts-container"),

    html.P("斜率計算：對每個視窗內的對數收盤價做線性回歸，將日斜率年化（×252），以百分比顯示。",
           style={"fontSize": "12px", "color": "#aaa", "marginTop": "16px",
                  "fontFamily": "sans-serif"}),
], style={"maxWidth": "1000px", "margin": "2rem auto", "padding": "0 1.5rem",
          "fontFamily": "sans-serif"})


@app.callback(
    Output("charts-container", "children"),
    Output("status-msg", "children"),
    Input("run-btn", "n_clicks"),
    State("ticker", "value"),
    State("year", "value"),
    State("window", "value"),
    prevent_initial_call=False,
)
def update_charts(n_clicks, ticker_str, year, window):
    tickers = [t.strip().upper() for t in (ticker_str or "QQQ").split(",") if t.strip()]
    year   = int(year or 2026)
    window = int(window or 10)

    chart_divs = []
    messages = []

    for i, ticker in enumerate(tickers[:12]):
        color = COLORS[i % len(COLORS)]
        try:
            dates, closes, volumes = fetch_yahoo(ticker, year)
        except Exception as e:
            messages.append(f"❌ {ticker}: {e}")
            chart_divs.append(html.Div(
                f"❌ {ticker}：{e}",
                style={"padding": "12px", "color": "#dc2626", "fontSize": "13px",
                       "background": "#fff5f5", "borderRadius": "8px",
                       "marginBottom": "12px"}
            ))
            continue

        slopes = rolling_annualized_log_slope(closes, window)
        slope_line = [None if math.isnan(v) else v for v in slopes]
        pos_s = [v if (v is not None and v > 0) else 0 for v in slope_line]
        neg_s = [v if (v is not None and v < 0) else 0 for v in slope_line]

        # 成交量顏色：漲綠跌紅
        vol_colors = []
        for k in range(len(closes)):
            if k == 0:
                vol_colors.append("rgba(150,150,150,0.5)")
            elif closes[k] >= closes[k - 1]:
                vol_colors.append("rgba(34,160,107,0.5)")
            else:
                vol_colors.append("rgba(226,72,61,0.5)")

        # 上方斜率+股價，下方成交量，高度比例 7:3
        fig = make_subplots(
            rows=2, cols=1,
            shared_xaxes=True,
            row_heights=[0.68, 0.32],
            vertical_spacing=0.04,
            specs=[[{"secondary_y": True}], [{"secondary_y": False}]],
        )

        # 上方：斜率填色
        fig.add_trace(go.Bar(
            x=dates, y=pos_s, name="上升",
            marker_color="rgba(34,160,107,0.35)", showlegend=False,
            hovertemplate="%{x}<br>斜率: %{y:.1f}%<extra></extra>",
        ), row=1, col=1, secondary_y=False)

        fig.add_trace(go.Bar(
            x=dates, y=neg_s, name="下跌",
            marker_color="rgba(226,72,61,0.35)", showlegend=False,
            hovertemplate="%{x}<br>斜率: %{y:.1f}%<extra></extra>",
        ), row=1, col=1, secondary_y=False)

        # 上方：斜率折線
        fig.add_trace(go.Scatter(
            x=dates, y=slope_line,
            name=f"{window}日年化斜率", mode="lines",
            line=dict(color="#444", width=1.2),
            hovertemplate="%{x}<br>斜率: %{y:.2f}%<extra></extra>",
        ), row=1, col=1, secondary_y=False)

        # 上方：股價折線（右軸）
        fig.add_trace(go.Scatter(
            x=dates, y=closes,
            name="收盤價", mode="lines",
            line=dict(color=color, width=2),
            hovertemplate="%{x}<br>收盤: $%{y:.2f}<extra></extra>",
        ), row=1, col=1, secondary_y=True)

        # 下方：成交量
        fig.add_trace(go.Bar(
            x=dates, y=volumes,
            name="成交量",
            marker_color=vol_colors,
            hovertemplate="%{x}<br>成交量: %{y:,.0f}<extra></extra>",
        ), row=2, col=1)

        fig.add_hline(y=0, line_color="#ccc", line_width=1, row=1, col=1,
                      secondary_y=False)

        fig.update_layout(
            title=dict(
                text=f"<b>{ticker}</b>　{year}年　{window}日滾動年化斜率",
                font=dict(size=14, color="#1a1a1a"), x=0,
            ),
            barmode="overlay", bargap=0,
            hovermode="x unified",
            legend=dict(orientation="h", yanchor="bottom", y=1.06,
                        xanchor="right", x=1, font=dict(size=11)),
            plot_bgcolor="white", paper_bgcolor="white",
            margin=dict(l=60, r=65, t=50, b=40),
            font=dict(family="sans-serif", size=12),
            height=420,
        )

        # 上方 x 軸（隱藏，共用）
        fig.update_xaxes(showgrid=True, gridcolor="#eee", row=1, col=1)
        # 下方 x 軸
        fig.update_xaxes(showgrid=True, gridcolor="#eee", row=2, col=1)

        # 上方左軸：斜率
        fig.update_yaxes(title_text="年化斜率(%)", secondary_y=False,
                         showgrid=True, gridcolor="#eee", zeroline=False,
                         title_font=dict(size=10), row=1, col=1)
        # 上方右軸：股價
        fig.update_yaxes(title_text="收盤價(USD)", secondary_y=True,
                         showgrid=False, zeroline=False,
                         title_font=dict(size=10, color=color),
                         tickfont=dict(color=color), row=1, col=1)
        # 下方左軸：成交量
        fig.update_yaxes(title_text="成交量", showgrid=True, gridcolor="#eee",
                         zeroline=False, title_font=dict(size=10),
                         tickformat=".2s", row=2, col=1)

        chart_divs.append(html.Div(
            dcc.Graph(figure=fig, config={"displayModeBar": False}),
            style={"marginBottom": "16px", "border": "0.5px solid #e5e5e5",
                   "borderRadius": "10px", "overflow": "hidden", "background": "white"}
        ))
        messages.append(f"✅ {ticker} {len(dates)}日")

    status = "　".join(messages)
    return chart_divs, status


if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=8050)
