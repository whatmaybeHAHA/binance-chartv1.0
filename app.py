import streamlit as st
import pandas as pd
import requests
import plotly.graph_objects as go

st.set_page_config(layout="wide", page_title="幣安多時框看盤系統")

def get_binance_klines(symbol, interval, limit=80):
    """抓取 K 線資料，將 limit 下調至 80 讓畫面適中不擁擠"""
    url = "https://data-api.binance.vision/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    try:
        response = requests.get(url, params=params)
        data = response.json()
        df = pd.DataFrame(data, columns=[
            "Open time", "Open", "High", "Low", "Close", "Volume",
            "Close time", "Quote asset volume", "Number of trades",
            "Taker buy base asset", "Taker buy quote asset", "Ignore"
        ])
        df["Open time"] = pd.to_datetime(df["Open time"], unit='ms') + pd.Timedelta(hours=8)
        for col in ["Open", "High", "Low", "Close", "Volume"]:
            df[col] = df[col].astype(float)
        return df
    except Exception as e:
        st.error(f"獲取 {symbol} {interval} 資料發生錯誤: {e}")
        return pd.DataFrame()

def plot_candlestick(df, title):
    latest_date = df['Open time'].iloc[-1].strftime('%Y-%m-%d')
    
    # 解決重疊問題：直接使用真實的 datetime 物件，不轉成純字串
    fig = go.Figure(data=[go.Candlestick(
        x=df['Open time'], 
        open=df['Open'],
        high=df['High'],
        low=df['Low'],
        close=df['Close'],
        name=title,
        increasing_line_color='#26A69A',
        decreasing_line_color='#EF5350'
    )])
    
    fig.update_layout(
        title=dict(text=title, font=dict(size=18)),
        xaxis_rangeslider_visible=False,
        margin=dict(l=10, r=10, t=55, b=10),
        height=450,
        template="plotly_dark",
        uirevision="constant",  # 保持縮放視角
        annotations=[dict(
            text=f"📅 {latest_date}",
            xref="paper", yref="paper",
            x=1.0, y=1.08,
            xanchor="right", yanchor="bottom",
            showarrow=False,
            font=dict(size=14, color="#AAAAAA")
        )]
    )
    
    # 讓 Plotly 自動處理連續時間，並設定標籤格式為「時:分」
    fig.update_xaxes(
        tickangle=-45, 
        nticks=10, 
        tickformat="%H:%M" 
    ) 
    return fig

# --- 網頁介面設計 ---
st.title("📈 超短線交易：多時框即時看盤面板")

col_setting, col_price, col_info = st.columns([1.5, 1.5, 2])

with col_setting:
    symbol = st.selectbox("請選擇交易對", ["BTCUSDT", "ETHUSDT", "BNBUSDT"])

with col_info:
    st.info("🔄 報價每 1 秒更新，圖表每 10 秒更新。可縮放平移。")

@st.fragment(run_every=1)
def show_realtime_price(selected_symbol):
    url = "https://data-api.binance.vision/api/v3/ticker/price"
    params = {"symbol": selected_symbol}
    try:
        res = requests.get(url, params=params).json()
        current_price = float(res['price'])
        update_time = (pd.Timestamp.utcnow() + pd.Timedelta(hours=8)).strftime('%H:%M:%S')
        st.metric(
            label=f"💰 {selected_symbol} 即時報價 ｜ 🟢 最新更新: {update_time}", 
            value=f"{current_price:,.2f}"
        )
    except:
        update_time = (pd.Timestamp.utcnow() + pd.Timedelta(hours=8)).strftime('%H:%M:%S')
        st.metric(
            label=f"💰 {selected_symbol} 即時報價 ｜ 🔴 連線中斷: {update_time}", 
            value="載入中..."
        )

@st.fragment(run_every=10)
def show_charts(selected_symbol):
    col1, col2, col3 = st.columns(3)
    chart_config = {'displayModeBar': False, 'scrollZoom': True}
    
    with col1:
        df_1m = get_binance_klines(selected_symbol, "1m", limit=80)
        if not df_1m.empty:
            st.plotly_chart(
                plot_candlestick(df_1m, "1 分鐘線 (確認進場點)"), 
                use_container_width=True,
                config=chart_config,
                key="chart_1m" # 🌟 加入專屬 Key，讓系統記住縮放狀態
            )

    with col2:
        df_5m = get_binance_klines(selected_symbol, "5m", limit=80)
        if not df_5m.empty:
            st.plotly_chart(
                plot_candlestick(df_5m, "5 分鐘線 (尋找型態與支撐壓力)"), 
                use_container_width=True,
                config=chart_config,
                key="chart_5m" # 🌟 加入專屬 Key
            )

    with col3:
        df_15m = get_binance_klines(selected_symbol, "15m", limit=80)
        if not df_15m.empty:
            st.plotly_chart(
                plot_candlestick(df_15m, "15 分鐘線 (確認大趨勢方向)"), 
                use_container_width=True,
                config=chart_config,
                key="chart_15m" # 🌟 加入專屬 Key
            )

with col_price:
    show_realtime_price(symbol)

show_charts(symbol)
