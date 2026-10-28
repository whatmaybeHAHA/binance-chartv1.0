import streamlit as st
import pandas as pd
import requests
import plotly.graph_objects as go

# 設定網頁版面為寬螢幕模式
st.set_page_config(layout="wide", page_title="幣安多時框看盤系統")

def get_binance_klines(symbol, interval, limit=50):
    """從幣安公開資料節點抓取 K 線資料"""
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
        st.error(f"獲取 {symbol} {interval} 資料時發生錯誤: {e}")
        return pd.DataFrame()

def plot_candlestick(df, title):
    """繪製圖表並設定時間與日期格式"""
    latest_date = df['Open time'].iloc[-1].strftime('%Y-%m-%d')
    df['TimeStr'] = df['Open time'].dt.strftime('%H:%M')

    fig = go.Figure(data=[go.Candlestick(
        x=df['TimeStr'],
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
        uirevision=title,  # ✨ 魔法參數：記住使用者的縮放與平移視角
        annotations=[dict(
            text=f"📅 {latest_date}",
            xref="paper", yref="paper",
            x=1.0, y=1.08,
            xanchor="right", yanchor="bottom",
            showarrow=False,
            font=dict(size=14, color="#AAAAAA")
        )]
    )
    
    fig.update_xaxes(type='category', tickangle=-45, nticks=20) 
    return fig

# --- 網頁介面設計 ---

st.title("📈 超短線交易：多時框即時看盤面板")

# 頂部控制區 (手動切換交易對)
col_setting, col_price, col_info = st.columns([1.5, 1.5, 2])

with col_setting:
    symbol = st.selectbox("請選擇交易對", ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"])

with col_info:
    st.info("🔄 報價每 1 秒更新，圖表每 10 秒更新。可縮放平移。")

# 獨立區塊 1：每 1 秒自動更新的即時報價
@st.fragment(run_every=1)
def show_realtime_price(selected_symbol):
    url = "https://data-api.binance.vision/api/v3/ticker/price"
    params = {"symbol": selected_symbol}
    try:
        res = requests.get(url, params=params).json()
        current_price = float(res['price'])
        
        # 取得系統抓到資料當下的時間，並轉換為台灣時間 (UTC+8)
        update_time = (pd.Timestamp.utcnow() + pd.Timedelta(hours=8)).strftime('%H:%M:%S')
        
        # 顯示價格，並在標題加入綠燈與秒數
        st.metric(
            label=f"💰 {selected_symbol} 即時報價 ｜ 🟢 最新更新: {update_time}", 
            value=f"{current_price:,.2f}"
        )
    except:
        update_time = (pd.Timestamp.utcnow() + pd.Timedelta(hours=8)).strftime('%H:%M:%S')
        # 若抓取失敗則顯示紅燈與失敗時間
        st.metric(
            label=f"💰 {selected_symbol} 即時報價 ｜ 🔴 連線中斷: {update_time}", 
            value="載入中..."
        )

# 獨立區塊 2：每 10 秒自動更新的多時框圖表
@st.fragment(run_every=10)
def show_charts(selected_symbol):
    col1, col2, col3 = st.columns(3)
    
    # 建立圖表專用的設定檔，將語系設定為繁體中文
    chart_config = {'locale': 'zh-TW'}
    
    with col1:
        df_1m = get_binance_klines(selected_symbol, "1m", limit=200)
        if not df_1m.empty:
            st.plotly_chart(
                plot_candlestick(df_1m, "1 分鐘線 (確認進場點)"), 
                use_container_width=True,
                config=chart_config
            )

    with col2:
        df_5m = get_binance_klines(selected_symbol, "5m", limit=200)
        if not df_5m.empty:
            st.plotly_chart(
                plot_candlestick(df_5m, "5 分鐘線 (尋找型態與支撐壓力)"), 
                use_container_width=True,
                config=chart_config
            )

    with col3:
        df_15m = get_binance_klines(selected_symbol, "15m", limit=200)
        if not df_15m.empty:
            st.plotly_chart(
                plot_candlestick(df_15m, "15 分鐘線 (確認大趨勢方向)"), 
                use_container_width=True,
                config=chart_config
            )

# --- 執行區塊 ---
with col_price:
    show_realtime_price(symbol)

# 呼叫並畫出圖表 (原本遺漏的指令)
show_charts(symbol)
