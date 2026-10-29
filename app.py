import streamlit as st
import pandas as pd
import numpy as np
import requests
import plotly.graph_objects as go
import concurrent.futures

st.set_page_config(layout="wide", page_title="幣安 5 分鐘預測高勝率看盤系統 - 信心評分版")

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
    except Exception:
        return pd.DataFrame()

def add_technical_indicators(df):
    if df.empty: return df
    df['MA5'] = df['Close'].rolling(window=5).mean()
    df['MA20'] = df['Close'].rolling(window=20).mean()
    df['std'] = df['Close'].rolling(window=20).std()
    df['BB_Upper'] = df['MA20'] + (df['std'] * 2)
    df['BB_Lower'] = df['MA20'] - (df['std'] * 2)
    
    # RSI
    delta = df['Close'].diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
    rs = avg_gain / avg_loss
    df['RSI'] = 100 - (100 / (1 + rs))
    
    # ATR (14 期真實波動區間)
    high_low = df['High'] - df['Low']
    high_close = (df['High'] - df['Close'].shift()).abs()
    low_close = (df['Low'] - df['Close'].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df['ATR'] = tr.rolling(window=14).mean()
    return df

def calculate_confidence_score(df_1m, df_5m, df_15m):
    """
    結合三大維度：
    1. 護城河深度 vs 1分鐘 ATR
    2. 布林通道突破/壓制防禦
    3. 時間與 RSI 動能權重
    """
    if df_1m.empty or df_5m.empty or df_15m.empty:
        return 0, "無資料", "info", 0.0, 0.0

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
    
    atr_1m = latest_1m['ATR'] if not np.isnan(latest_1m['ATR']) and latest_1m['ATR'] > 0 else 0.5
    moat_ratio = abs(price_diff) / atr_1m
    
    trend_up = latest_15m['MA5'] > latest_15m['MA20']
    rsi_1m = latest_1m['RSI']
    bb_upper = latest_1m['BB_Upper']
    bb_lower = latest_1m['BB_Lower']
    
    score = 50.0  # 基礎分
    direction = "UP" if price_diff >= 0 else "DOWN"
    
    # --- 維度一：時間與護城河深度加成 ---
    if sec_left <= 60:
        score += 15.0  # 進入最後保衛戰
        if sec_left <= 30:
            score += 10.0
        
        # 護城河厚度評估
        if moat_ratio >= 1.5:
            score += 25.0
        elif moat_ratio >= 1.0:
            score += 18.0
        elif moat_ratio >= 0.5:
            score += 8.0
        else:
            score -= 20.0  # 緊貼基準線，極度危險
    else:
        # 前 4 分鐘佈局
        if (direction == "UP" and trend_up) or (direction == "DOWN" and not trend_up):
            score += 15.0
        if moat_ratio >= 1.0:
            score += 10.0

    # --- 維度二：動能加成 (RSI) ---
    if direction == "UP":
        if 50 <= rsi_1m <= 68:
            score += 10.0
        elif rsi_1m > 75:
            score -= 15.0  # 超買嚴重，提防回調
        elif rsi_1m < 40:
            score -= 15.0
    else:
        if 32 <= rsi_1m <= 50:
            score += 10.0
        elif rsi_1m < 25:
            score -= 15.0  # 超賣嚴重，提防反彈
        elif rsi_1m > 60:
            score -= 15.0

    # --- 維度三：布林通道邊界煞車機制 ---
    if direction == "UP" and current_price >= bb_upper:
        score -= 12.0  # 觸碰頂部阻力
    elif direction == "DOWN" and current_price <= bb_lower:
        score -= 12.0  # 觸碰底部支撐

    # 分數範圍正規化至 0 ~ 99%
    score = float(np.clip(score, 5.0, 98.0))
    
    # 決定訊號等級與文字
    if score >= 80:
        status_type = "success" if direction == "UP" else "error"
        action = "買漲 (UP)" if direction == "UP" else "買跌 (DOWN)"
        desc = f"🔥 【倒數 {countdown}】 訊號：{action} ｜ 護城河倍數: {moat_ratio:.2f}x ATR"
    elif score >= 65:
        status_type = "warning"
        action = "買漲 (UP)" if direction == "UP" else "買跌 (DOWN)"
        desc = f"⚠️ 【倒數 {countdown}】 訊號：{action} 順勢觀察 ｜ 護城河薄弱 ({moat_ratio:.2f}x ATR)"
    else:
        status_type = "info"
        desc = f"⏳ 【倒數 {countdown}】 震盪觀望 ｜ 距水面過近或指標背離"

    return score, desc, status_type, moat_ratio, base_price

def generate_radar_signal(df_1m, df_5m, df_15m, symbol_name):
    """輕量背景雷達"""
    score, _, _, moat_ratio, _ = calculate_confidence_score(df_1m, df_5m, df_15m)
    if score >= 80:
        latest_1m = df_1m.iloc[-1]
        latest_5m = df_5m.iloc[-1]
        action = "🟢 漲" if latest_1m['Close'] >= latest_5m['Open'] else "🔴 跌"
        return f"**[{symbol_name}] {action} ({score:.0f}%)**"
    elif score >= 65:
        return f"[{symbol_name}] 🟡 關注 ({score:.0f}%)"
    return f"[{symbol_name}] ⚪ 觀望"

def plot_advanced_candlestick(df, title, baseline_price=None):
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
    
    if baseline_price is not None:
        fig.add_hline(
            y=baseline_price, 
            line_dash="dash", 
            line_color="#FFFFFF", 
            line_width=2,
            opacity=0.9,
            annotation_text=f"🎯 基準水面", 
            annotation_position="bottom right",
            annotation_font_color="white",
            annotation_font_size=13
        )
    
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

# --- 介面呈現 ---
st.title("🎯 Binance Predict 5分鐘勝率提升器 (量化評分版)")

available_symbols = ["BTCUSDT", "ETHUSDT", "BNBUSDT"]

col_setting, col_price, col_info = st.columns([1.5, 1.5, 2])
with col_setting:
    symbol = st.selectbox("選擇主戰場 (發出音效)", available_symbols)

@st.fragment(run_every=1)
def show_realtime_price(selected_symbol):
    url = "https://data-api.binance.vision/api/v3/ticker/price"
    try:
        res = requests.get(url, params={"symbol": selected_symbol}).json()
        current_price = float(res['price'])
        
        now = pd.Timestamp.utcnow() + pd.Timedelta(hours=8)
        next_5m = now.ceil('5min')
        if next_5m == now: next_5m = now + pd.Timedelta(minutes=5)
        sec_left = int((next_5m - now).total_seconds())
        countdown = f"{sec_left // 60:02d}:{sec_left % 60:02d}"
        
        st.metric(label=f"💰 {selected_symbol} 即時現價 ｜ ⏳ 距離結算倒數: {countdown}", value=f"{current_price:,.2f}")
    except Exception:
        st.metric(label="連線狀態", value="載入中...")

with col_price:
    show_realtime_price(symbol)
    
with col_info:
    st.info("💡 信心指標：大於 80% 觸發進場提示音；重點留意護城河是否超過 1.0x ATR。")

@st.fragment(run_every=10)
def show_prediction_dashboard(selected_symbol, all_symbols):
    # 1. 平行背景雷達
    other_symbols = [s for s in all_symbols if s != selected_symbol]
    
    def fetch_radar(osym):
        d1 = add_technical_indicators(get_binance_klines(osym, "1m", limit=80))
        d5 = add_technical_indicators(get_binance_klines(osym, "5m", limit=80))
        d15 = add_technical_indicators(get_binance_klines(osym, "15m", limit=80))
        return generate_radar_signal(d1, d5, d15, osym.replace("USDT", ""))

    with concurrent.futures.ThreadPoolExecutor() as executor:
        radar_messages = list(executor.map(fetch_radar, other_symbols))
    
    st.markdown(f"📡 **背景靜音雷達：** {' ｜ '.join(radar_messages)}")
    st.divider()
    
    # 2. 主戰場資料計算
    df_1m = add_technical_indicators(get_binance_klines(selected_symbol, "1m"))
    df_5m = add_technical_indicators(get_binance_klines(selected_symbol, "5m"))
    df_15m = add_technical_indicators(get_binance_klines(selected_symbol, "15m"))
    
    score, desc_text, status_type, moat_ratio, base_price = calculate_confidence_score(df_1m, df_5m, df_15m)
    
    # 顯示綜合信心進度條
    col_bar, col_val = st.columns([4, 1])
    with col_bar:
        st.progress(score / 100.0)
    with col_val:
        st.markdown(f"**勝率信心指數：`{score:.1f}%`**")
    
    if status_type == "success":
        st.success(desc_text)
        play_alert_sound()
    elif status_type == "error":
        st.error(desc_text)
        play_alert_sound()
    elif status_type == "warning":
        st.warning(desc_text)
    else:
        st.info(desc_text)
    
    col1, col2, col3 = st.columns(3)
    chart_config = {'displayModeBar': False, 'scrollZoom': True}
    
    with col1:
        if not df_1m.empty:
            st.plotly_chart(plot_advanced_candlestick(df_1m, "1 分鐘線 (尋找超買超賣邊界)", baseline_price=base_price), use_container_width=True, config=chart_config, key="c1")
    with col2:
        if not df_5m.empty:
            st.plotly_chart(plot_advanced_candlestick(df_5m, "5 分鐘線 (預測結算目標區)", baseline_price=base_price), use_container_width=True, config=chart_config, key="c5")
    with col3:
        if not df_15m.empty:
            st.plotly_chart(plot_advanced_candlestick(df_15m, "15 分鐘線 (確認主力大趨勢)"), use_container_width=True, config=chart_config, key="c15")

show_prediction_dashboard(symbol, available_symbols)
