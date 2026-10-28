import streamlit as st
import pandas as pd
import requests
import plotly.graph_objects as go

st.set_page_config(layout="wide", page_title="幣安 5 分鐘預測高勝率看盤系統")

def play_alert_sound():
    """播放高勝率進場提示音"""
    audio_html = """
        <audio autoplay>
            <source src="https://actions.google.com/sounds/v1/alarms/beep_short.ogg" type="audio/ogg">
        </audio>
    """
    st.markdown(audio_html, unsafe_allow_html=True)

def get_binance_klines(symbol, interval, limit=150):
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
    if df.empty: return df
    df['MA5'] = df['Close'].rolling(window=5).mean()
    df['MA20'] = df['Close'].rolling(window=20).mean()
    df['std'] = df['Close'].rolling(window=20).std()
    df['BB_Upper'] = df['MA20'] + (df['std'] * 2)
    df['BB_Lower'] = df['MA20'] - (df['std'] * 2)
    delta = df['Close'].diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
    rs = avg_gain / avg_loss
    df['RSI'] = 100 - (100 / (1 + rs))
    return df

def generate_prediction_signal(df_1m, df_5m, df_15m):
    """基於幣安真實規則 (收盤價 > 開盤價) 與倒數時間的 AI 戰術邏輯"""
    if df_1m.empty or df_5m.empty or df_15m.empty:
        return "資料載入中...", "info"
        
    latest_1m = df_1m.iloc[-1]
    latest_5m = df_5m.iloc[-1]
    latest_15m = df_15m.iloc[-1]
    
    # 遊戲真實基準線：當前 5 分鐘 K 線的「開盤價」
    base_price = latest_5m['Open']
    current_price = latest_1m['Close']
    price_diff = current_price - base_price
    
    # 計算距離 5 分鐘結算的倒數時間
    now = pd.Timestamp.utcnow() + pd.Timedelta(hours=8)
    next_5m = now.ceil('5min')
    if next_5m == now: 
        next_5m = now + pd.Timedelta(minutes=5)
    sec_left = int((next_5m - now).total_seconds())
    min_str = sec_left // 60
    sec_str = sec_left % 60
    countdown = f"{min_str:02d}:{sec_str:02d}"
    
    trend_up = latest_15m['MA5'] > latest_15m['MA20']
    rsi_1m = latest_1m['RSI']
    
    # 根據剩餘時間切換戰術
    if sec_left <= 60:
        # 最後 60 秒保衛戰：重點在於有沒有足夠的護城河 (差價) 與穩定的動能
        if price_diff > 0 and rsi_1m > 40:
            return f"⏳ 【倒數 {countdown}】 📈 買漲 (UP) 極高勝率！價格守在基準 {base_price} 之上，動能穩定。", "success"
        elif price_diff < 0 and rsi_1m < 60:
            return f"⏳ 【倒數 {countdown}】 📉 買跌 (DOWN) 極高勝率！價格壓在基準 {base_price} 之下，反彈無力。", "error"
        else:
            return f"⏳ 【倒數 {countdown}】 ⚖️ 觀望 - 價格緊貼基準線或動能出現背離，最後一秒容易被反殺，放棄此局。", "info"
    else:
        # 剩餘時間 > 1 分鐘：看大趨勢與指標乖離
        if price_diff > 0 and trend_up and rsi_1m > 50:
            return f"⏱️ 【剩餘 {countdown}】 📈 買漲 (UP) 順勢 - 15分趨勢向上，且目前已取得綠K優勢。", "success"
        elif price_diff < 0 and not trend_up and rsi_1m < 50:
            return f"⏱️ 【剩餘 {countdown}】 📉 買跌 (DOWN) 順勢 - 15分趨勢向下，且目前已取得紅K優勢。", "error"
        elif rsi_1m < 30 and price_diff < 0:
            return f"⏱️ 【剩餘 {countdown}】 ⚠️ 潛在逆轉 (UP) - 極度超賣！有機會向上反彈越過基準線 {base_price}。", "warning"
        elif rsi_1m > 70 and price_diff > 0:
            return f"⏱️ 【剩餘 {countdown}】 ⚠️ 潛在逆轉 (DOWN) - 極度超買！有機會向下回落跌破基準線 {base_price}。", "warning"
        else:
            return f"⏱️ 【剩餘 {countdown}】 ⚖️ 觀望 - 價格在基準價 {base_price} 附近隨機震盪，等待邊界訊號。", "info"

def plot_advanced_candlestick(df, title):
    latest_date = df['Open time'].iloc[-1].strftime('%Y-%m-%d')
    latest_rsi = df['RSI'].iloc[-1]
    fig = go.Figure()
    
    fig.add_trace(go.Candlestick(
        x=df['Open time'], open=df['Open'], high=df['High'], low=df['Low'], close=df['Close'],
        name="K線", increasing_line_color='#26A69A', decreasing_line_color='#EF5350'
    ))
    
    fig.add_trace(go.Scatter(x=df['Open time'], y=df['MA5'], line=dict(color='#FBC02D', width=1.5), name='5MA'))
    fig.add_trace(go.Scatter(x=df['Open time'], y=df['MA20'], line=dict(color='#AB47BC', width=1.5), name='20MA'))
    fig.add_trace(go.Scatter(x=df['Open time'], y=df['BB_Upper'], line=dict(color='rgba(33, 150, 243, 0.6)', width=1.5, dash='dot'), name='布林上軌'))
    fig.add_trace(go.Scatter(x=df['Open time'], y=df['BB_Lower'], line=dict(color='rgba(33, 150, 243, 0.6)', width=1.5, dash='dot'), name='布林下軌'))
    
    fig.update_layout(
        title=dict(text=f"{title} | RSI: {latest_rsi:.1f}", font=dict(size=16)),
        xaxis_rangeslider_visible=False,
        margin=dict(l=10, r=10, t=40, b=60),
        height=450,
        template="plotly_dark",
        uirevision="constant",
        annotations=[dict(
            text=f"📅 {latest_date}", xref="paper", yref="paper", x=1.0, y=1.05,
            xanchor="right", yanchor="bottom", showarrow=False, font=dict(size=12, color="#AAAAAA")
        )],
        legend=dict(orientation="h", yanchor="top", y=-0.2, xanchor="center", x=0.5)
    )
    
    fig.update_xaxes(tickangle=-45, nticks=8, tickformat="%H:%M") 
    fig.update_xaxes(range=[df['Open time'].iloc[-80], df['Open time'].iloc[-1]])
    return fig

st.title("🎯 Binance Predict 5分鐘勝率提升器")

col_setting, col_price, col_info = st.columns([1.5, 1.5, 2])
with col_setting:
    symbol = st.selectbox("選擇預測交易對", ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"])

@st.fragment(run_every=1)
def show_realtime_price(selected_symbol):
    url = "
