import streamlit as st
import pandas as pd
import requests
import plotly.graph_objects as go
from streamlit_autorefresh import st_autorefresh

# 設定網頁版面為寬螢幕模式 (適合平板橫向觀看)
st.set_page_config(layout="wide", page_title="幣安多時框看盤系統")

# 設定每 10 秒自動重新整理一次頁面，抓取最新報價
st_autorefresh(interval=10000, limit=None, key="auto_refresh")

def get_binance_klines(symbol, interval, limit=60):
    """
    從幣安公開 API 抓取 K 線資料
    """
    url = "https://api.binance.com/api/v3/klines"
    params = {
        "symbol": symbol,
        "interval": interval,
        "limit": limit
    }
    
    try:
        response = requests.get(url, params=params)
        data = response.json()
        
        # 轉換為 Pandas DataFrame
        df = pd.DataFrame(data, columns=[
            "Open time", "Open", "High", "Low", "Close", "Volume",
            "Close time", "Quote asset volume", "Number of trades",
            "Taker buy base asset", "Taker buy quote asset", "Ignore"
        ])
        
        # 將時間戳轉換為台灣時間 (UTC+8)
        df["Open time"] = pd.to_datetime(df["Open time"], unit='ms') + pd.Timedelta(hours=8)
        
        # 將文字格式的價格轉換為浮點數
        for col in ["Open", "High", "Low", "Close", "Volume"]:
            df[col] = df[col].astype(float)
            
        return df
    except Exception as e:
        st.error(f"獲取 {symbol} {interval} 資料時發生錯誤: {e}")
        return pd.DataFrame()

def plot_candlestick(df, title):
    """
    使用 Plotly 繪製互動式 K 線圖
    """
    fig = go.Figure(data=[go.Candlestick(
        x=df['Open time'],
        open=df['Open'],
        high=df['High'],
        low=df['Low'],
        close=df['Close'],
        name=title,
        increasing_line_color='green', # 幣圈習慣：綠漲
        decreasing_line_color='red'    # 幣圈習慣：紅跌
    )])
    
    # 隱藏底部的範圍選擇器以節省版面，並調整邊距
    fig.update_layout(
        title=dict(text=title, font=dict(size=18)),
        xaxis_rangeslider_visible=False,
        margin=dict(l=10, r=10, t=40, b=10),
        height=450,
        template="plotly_dark" # 預設使用暗黑模式，保護眼睛
    )
    
    # 隱藏非交易時間的空白 (加密貨幣 24 小時交易，其實不加這行也行，但加上更保險)
    fig.update_xaxes(type='category') 
    
    return fig

# --- 網頁介面設計 ---

st.title("📈 超短線交易：多時框即時看盤面板")

# 頂部控制區
col_setting, col_info = st.columns([1, 3])
with col_setting:
    symbol = st.selectbox("請選擇交易對", ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"])

with col_info:
    st.info("🔄 資料每 10 秒自動更新一次。圖表可使用滑鼠/手指縮放平移。")

# 建立三個欄位 (1分、5分、15分)
col1, col2, col3 = st.columns(3)

with col1:
    df_1m = get_binance_klines(symbol, "1m", limit=50)
    if not df_1m.empty:
        fig_1m = plot_candlestick(df_1m, "1 分鐘線 (確認進場點)")
        st.plotly_chart(fig_1m, use_container_width=True)

with col2:
    df_5m = get_binance_klines(symbol, "5m", limit=50)
    if not df_5m.empty:
        fig_5m = plot_candlestick(df_5m, "5 分鐘線 (尋找型態與支撐壓力)")
        st.plotly_chart(fig_5m, use_container_width=True)

with col3:
    df_15m = get_binance_klines(symbol, "15m", limit=50)
    if not df_15m.empty:
        fig_15m = plot_candlestick(df_15m, "15 分鐘線 (確認大趨勢方向)")
        st.plotly_chart(fig_15m, use_container_width=True)
