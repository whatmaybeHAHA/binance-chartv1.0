import streamlit as st
import pandas as pd
import requests
import plotly.graph_objects as go

st.set_page_config(layout="wide", page_title="幣安 5 分鐘預測高勝率看盤系統")

def get_binance_klines(symbol, interval, limit=150):
    """抓取 K 線資料，並將 limit 提高以利計算均線與技術指標"""
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
        st.error(f"獲取資料發生錯誤: {e}")
        return pd.DataFrame()

def add_technical_indicators(df):
    """計算常用技術指標：MA, 布林通道, RSI"""
    if df.empty: return df
    
    # 1. 移動平均線 (MA)
    df['MA5'] = df['Close'].rolling(window=5).mean()
    df['MA20'] = df['Close'].rolling(window=20).mean()
    
    # 2. 布林通道 (Bollinger Bands, 參數 20, 2)
    df['std'] = df['Close'].rolling(window=20).std()
    df['BB_Upper'] = df['MA20'] + (df['std'] * 2)
    df['BB_Lower'] = df['MA20'] - (df['std'] * 2)
    
    # 3. RSI (參數 14)
    delta = df['Close'].diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
    rs = avg_gain / avg_loss
    df['RSI'] = 100 - (100 / (1 + rs))
    
    return df

def generate_prediction_signal(df_1m, df_15m):
    """綜合分析大時框趨勢與小時框極端值，產出預測建議"""
    if df_1m.empty or df_15m.empty:
        return "資料載入中...", "info"
        
    latest_1m = df_1m.iloc[-1]
    latest_15m = df_15m.iloc[-1]
    
    # 判斷 15 分鐘大方向
    trend_up = latest_15m['MA5'] > latest_15m['MA20']
    
    # 判斷 1 分鐘極端值 (尋找 5 分鐘內的轉折點)
    oversold = latest_1m['RSI'] < 35 or latest_1m['Close'] <= latest_1m['BB_Lower']
    overbought = latest_1m['RSI'] > 65 or latest_1m['Close'] >= latest_1m['BB_Upper']
    
    # 策略邏輯判斷
    if trend_up and oversold:
        return "📈 【高勝率機會：買漲 (UP)】 - 15分趨勢向上，且1分線進入超賣區或觸碰布林下軌，即將反彈！", "success"
    elif not trend_up and overbought:
        return "📉 【高勝率機會：買跌 (DOWN)】 - 15分趨勢向下，且1分線進入超買區或觸碰布林上軌，即將回踩！", "error"
    elif oversold:
        return "⚠️ 【逆勢搶反彈：買漲 (UP)】 - 短線急跌觸碰下軌，可能出現極短線反彈 (逆勢操作請縮小部位)。", "warning"
    elif overbought:
        return "⚠️ 【逆勢抓回落：買跌 (DOWN)】 - 短線急漲觸碰上軌，可能出現極短線回落 (逆勢操作請縮小部位)。", "warning"
    else:
        return "⚖️ 【建議：觀望等待】 - 目前價格在布林通道中間，多空隨機性高。進場宛如擲骰子，請耐心等待邊界測試。", "info"

def plot_advanced_candlestick(df, title):
    latest_date = df['Open time'].iloc[-1].strftime('%Y-%m-%d')
    latest_rsi = df['RSI'].iloc[-1]
    
    fig = go.Figure()
    
    # K線
    fig.add_trace(go.Candlestick(
        x=df['Open time'], open=df['Open'], high=df['High'], low=df['Low'], close=df['Close'],
        name="K線", increasing_line_color='#26A69A', decreasing_line_color='#EF5350'
    ))
    
    # 均線
    fig.add_trace(go.Scatter(x=df['Open time'], y=df['MA5'], line=dict(color='#FBC02D', width=1.5), name='5MA'))
    fig.add_trace(go.Scatter(x=df['Open time'], y=df['MA20'], line=dict(color='#AB47BC', width=1.5), name='20MA'))
    
    # 布林通道 (上下軌)
    fig.add_trace(go.Scatter(x=df['Open time'], y=df['BB_Upper'], line=dict(color='rgba(255, 255, 255, 0.3)', width=1, dash='dot'), name='布林上軌'))
    fig.add_trace(go.Scatter(x=df['Open time'], y=df['BB_Lower'], line=dict(color='rgba(255, 255, 255, 0.3)', width=1, dash='dot'), name='布林下軌'))
    
fig.update_layout(
        title=dict(text=f"{title} | RSI: {latest_rsi:.1f}", font=dict(size=16)),
        xaxis_rangeslider_visible=False,
        # 將底部邊距 (b) 從 10 增加到 60，留出空間給圖例
        margin=dict(l=10, r=10, t=40, b=60), 
        height=450,
        template="plotly_dark",
        uirevision="constant",
        annotations=[dict(
            text=f"📅 {latest_date}", xref="paper", yref="paper", x=1.0, y=1.05,
            xanchor="right", yanchor="bottom", showarrow=False, font=dict(size=12, color="#AAAAAA")
        )],
        # 修改這裡：將圖例移到圖表底部 (y=-0.2) 並置中 (x=0.5)
        legend=dict(orientation="h", yanchor="top", y=-0.2, xanchor="center", x=0.5) 
    )
    
    fig.update_xaxes(tickangle=-45, nticks=8, tickformat="%H:%M") 
    # 限制圖表初始只顯示最後 80 根，確保不擁擠
    fig.update_xaxes(range=[df['Open time'].iloc[-80], df['Open time'].iloc[-1]])
    
    return fig

# --- 網頁介面設計 ---
st.title("🎯 Binance Predict 5分鐘勝率提升器")

col_setting, col_price, col_info = st.columns([1.5, 1.5, 2])
with col_setting:
    symbol = st.selectbox("選擇預測交易對", ["BTCUSDT", "ETHUSDT", "BNBUSDT"])

@st.fragment(run_every=1)
def show_realtime_price(selected_symbol):
    url = "https://data-api.binance.vision/api/v3/ticker/price"
    try:
        res = requests.get(url, params={"symbol": selected_symbol}).json()
        current_price = float(res['price'])
        update_time = (pd.Timestamp.utcnow() + pd.Timedelta(hours=8)).strftime('%H:%M:%S')
        st.metric(label=f"💰 {selected_symbol} 即時現價 ｜ 🟢 {update_time}", value=f"{current_price:,.2f}")
    except:
        st.metric(label="連線狀態", value="載入中...")

with col_price:
    show_realtime_price(symbol)
    
with col_info:
    st.info("💡 實戰口訣：順大勢 (15m)，逆小勢 (1m 觸下軌買漲、觸上軌買跌)。")

@st.fragment(run_every=10)
def show_prediction_dashboard(selected_symbol):
    # 取得並計算三個時框的資料
    df_1m = add_technical_indicators(get_binance_klines(selected_symbol, "1m"))
    df_5m = add_technical_indicators(get_binance_klines(selected_symbol, "5m"))
    df_15m = add_technical_indicators(get_binance_klines(selected_symbol, "15m"))
    
    # --- AI 戰術訊號儀表板 ---
    signal_text, signal_type = generate_prediction_signal(df_1m, df_15m)
    if signal_type == "success":
        st.success(signal_text)
    elif signal_type == "error":
        st.error(signal_text)
    elif signal_type == "warning":
        st.warning(signal_text)
    else:
        st.info(signal_text)
    
    # --- 圖表區 ---
    col1, col2, col3 = st.columns(3)
    chart_config = {'displayModeBar': False, 'scrollZoom': True}
    
    with col1:
        if not df_1m.empty:
            st.plotly_chart(plot_advanced_candlestick(df_1m, "1 分鐘線 (尋找超買超賣邊界)"), use_container_width=True, config=chart_config, key="c1")
    with col2:
        if not df_5m.empty:
            st.plotly_chart(plot_advanced_candlestick(df_5m, "5 分鐘線 (預測結算目標區)"), use_container_width=True, config=chart_config, key="c5")
    with col3:
        if not df_15m.empty:
            st.plotly_chart(plot_advanced_candlestick(df_15m, "15 分鐘線 (確認主力大趨勢)"), use_container_width=True, config=chart_config, key="c15")

show_prediction_dashboard(symbol)
