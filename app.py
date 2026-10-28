import streamlit as st
import pandas as pd
import requests
import plotly.graph_objects as go
import concurrent.futures  # 🌟 引入多執行緒平行處理模組

st.set_page_config(layout="wide", page_title="幣安 5 分鐘預測高勝率看盤系統")

def play_alert_sound():
    """播放高勝率進場提示音 (強制更新時間戳記)"""
    t = pd.Timestamp.now().timestamp()
    audio_html = f"""
        <audio autoplay style="display:none;">
            <source src="https://actions.google.com/sounds/v1/alarms/beep_short.ogg?v={t}" type="audio/ogg">
        </audio>
    """
    st.markdown(audio_html, unsafe_allow_html=True)

@st.cache_data(ttl=5)
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

def generate_radar_signal(df_1m, df_5m, df_15m, symbol_name):
    """專為背景雷達設計的輕量級無聲訊號判斷"""
    if df_1m.empty or df_5m.empty or df_15m.empty:
        return f"[{symbol_name}] ⏳ 載入中..."
        
    latest_1m = df_1m.iloc[-1]
    latest_5m = df_5m.iloc[-1]
    latest_15m = df_15m.iloc[-1]
    
    base_price = latest_5m['Open']
    current_price = latest_1m['Close']
    price_diff = current_price - base_price
    
    now = pd.Timestamp.utcnow() + pd.Timedelta(hours=8)
    next_5m = now.ceil('5min')
    if next_5m == now: next_5m = now + pd.Timedelta(minutes=5)
    sec_left = int((next_5m - now).total_seconds())
    countdown = f"{sec_left // 60:02d}:{sec_left % 60:02d}"
    
    trend_up = latest_15m['MA5'] > latest_15m['MA20']
    rsi_1m = latest_1m['RSI']
    
    if sec_left <= 60:
        if price_diff > 0 and rsi_1m > 40:
            return f"**[{symbol_name}] 🟢 買漲 (剩 {countdown})**"
        elif price_diff < 0 and rsi_1m < 60:
            return f"**[{symbol_name}] 🔴 買跌 (剩 {countdown})**"
    else:
        if price_diff > 0 and trend_up and rsi_1m > 50:
            return f"[{symbol_name}] 📈 順勢買漲"
        elif price_diff < 0 and not trend_up and rsi_1m < 50:
            return f"[{symbol_name}] 📉 順勢買跌"
        elif rsi_1m < 30 and price_diff < 0:
            return f"[{symbol_name}] ⚠️ 潛在反彈"
        elif rsi_1m > 70 and price_diff > 0:
            return f"[{symbol_name}] ⚠️ 潛在回落"
            
    return f"[{symbol_name}] ⚖️ 觀望中"

def generate_prediction_signal(df_1m, df_5m, df_15m):
    """主戰場的 AI 戰術邏輯 (有聲音)"""
    if df_1m.empty or df_5m.empty or df_15m.empty:
        return "資料載入中...", "info"
        
    latest_1m = df_1m.iloc[-1]
    latest_5m = df_5m.iloc[-1]
    latest_15m = df_15m.iloc[-1]
    
    base_price = latest_5m['Open']
    current_price = latest_1m['Close']
    price_diff = current_price - base_price
    
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
    
    if sec_left <= 60:
        if price_diff > 0 and rsi_1m > 40:
            return f"⏳ 【倒數 {countdown}】 📈 買漲 (UP) 極高勝率！價格守在水面 {base_price} 之上，動能穩定。", "success"
        elif price_diff < 0 and rsi_1m < 60:
            return f"⏳ 【倒數 {countdown}】 📉 買跌 (DOWN) 極高勝率！價格壓在水面 {base_price} 之下，反彈無力。", "error"
        else:
            return f"⏳ 【倒數 {countdown}】 ⚖️ 觀望 - 價格緊貼基準線或動能出現背離，最後一秒容易被反殺，放棄此局。", "info"
    else:
        if price_diff > 0 and trend_up and rsi_1m > 50:
            return f"⏱️ 【剩餘 {countdown}】 📈 買漲 (UP) 順勢 - 15分趨勢向上，且目前已取得綠K優勢。", "success"
        elif price_diff < 0 and not trend_up and rsi_1m < 50:
            return f"⏱️ 【剩餘 {countdown}】 📉 買跌 (DOWN) 順勢 - 15分趨勢向下，且目前已取得紅K優勢。", "error"
        elif rsi_1m < 30 and price_diff < 0:
            return f"⏱️ 【剩餘 {countdown}】 ⚠️ 潛在逆轉 (UP) - 極度超賣！有機會向上反彈衝出水面 {base_price}。", "warning"
        elif rsi_1m > 70 and price_diff > 0:
            return f"⏱️ 【剩餘 {countdown}】 ⚠️ 潛在逆轉 (DOWN) - 極度
