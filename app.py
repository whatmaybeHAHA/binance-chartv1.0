import streamlit as st
import pandas as pd
import requests
import plotly.graph_objects as go
from streamlit_autorefresh import st_autorefresh

# 設定網頁版面為寬螢幕模式
st.set_page_config(layout="wide", page_title="幣安多時框看盤系統")

# 設定每 10 秒自動重新整理一次頁面
st_autorefresh(interval=10000, limit=None, key="auto_refresh")

def get_binance_klines(symbol, interval, limit=50):
    """從幣安公開資料節點抓取 K 線資料"""
    url = "https://data-api.binance.vision/api/v3/klines"
    params = {
        "symbol": symbol,
        "interval": interval,
        "limit": limit
    }
    
    try:
        response = requests.get(url, params=params)
        data = response.json()
        
        df = pd.DataFrame(data, columns=[
            "Open time", "Open", "High", "Low", "Close", "Volume",
            "Close time", "Quote asset volume", "Number of trades",
            "Taker buy base asset", "Taker buy quote asset", "Ignore"
        ])
        
        # 轉換為台灣時間 (UTC+8)
        df["Open time"] = pd.to_datetime(df["Open time"], unit='ms') + pd.Timedelta(hours=8)
        
        for col in ["Open", "High", "Low", "Close", "Volume"]:
            df[col] = df[col].astype(float)
            
        return df
    except Exception as e:
        st.error(f"獲取 {symbol} {interval} 資料時發生錯誤: {e}")
        return pd.DataFrame()

def plot_candlestick(df, title):
    """繪製圖表並設定時間與日期格式"""
    # 提取最新一筆的日期 (YYYY-MM-DD)，用來顯示在右上角
    latest_date = df['Open time'].iloc[-1].strftime('%Y-%m-%d')
    
    # 將 X 軸用的時間格式化為只有「時:分」
    df['TimeStr'] = df['Open time'].dt.strftime('%H:%M')

    fig = go.Figure(data=[go.Candlestick(
        x=df['TimeStr'], # 使用簡化後的時間字串
        open=df['Open'],
        high=df['High'],
        low=df['Low'],
        close=df['Close'],
        name=title,
        increasing_line_color='#26A69A', # 優化綠色
        decreasing_line_color='#EF5350'  # 優化紅色
    )])
    
    fig.update_layout(
        title=dict(text=title, font=dict(size=18)),
        xaxis_rangeslider_visible=False,
        margin=dict(l=10, r=10, t=55, b=10), # 增加頂部空間給日期標籤
        height=450,
        template="plotly_dark",
        
        # 在圖表右上角加入日期標籤
        annotations=[dict(
            text=f"📅 {latest_date}",
            xref="paper", yref="paper",
            x=1.0, y=1.08, # 設定在右上角偏上的位置
            xanchor="right", yanchor="bottom",
            showarrow=False,
            font=dict(size=14, color="#AAAAAA")
        )]
    )
    
    # 設定 X 軸標籤傾斜角度，避免時分字串重疊
    fig.update_xaxes(
        type='category',
        tickangle=-45, 
        nticks=20 # 限制顯示的標籤數量，保持畫面乾淨
    ) 
    
    return fig

# --- 網頁介面設計 ---

st.title("📈 超短線交易：多時框即時看盤面板")

# 頂部控制與報價區
col_setting, col_price, col_info = st.columns([1.5, 1.5, 2])

with col_setting:
    symbol = st.selectbox("請選擇交易對", ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"])

# 優先抓取 1 分鐘資料，以便提取最新價格顯示在上方
df_1m = get_binance_klines(symbol, "1m", limit=50)

with col_price:
    if not df_1m.empty:
        # 取最新一筆收盤價作為即時報價
        current_price = df_1m['Close'].iloc[-1]
        st.metric(label=f"💰 {symbol} 最新即時報價", value=f"{current_price:,}")

with col_info:
    st.info("🔄 資料每 10 秒自動更新一次。圖表可使用滑鼠/手指縮放平移。")

# 建立三個欄位繪製圖表
col1, col2, col3 = st.columns(3)

with col1:
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
