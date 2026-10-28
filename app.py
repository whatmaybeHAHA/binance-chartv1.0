import streamlit as st
import pandas as pd
import requests
import plotly.graph_objects as go

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

# 🌟 加入 @st.cache_data(ttl=5)，快取 5 秒內的 API 資料，大幅提升切換速度！
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
            return f"⏱️ 【剩餘 {countdown}】 ⚠️ 潛在逆轉 (DOWN) - 極度超買！有機會向下回落跌破水面 {base_price}。", "warning"
        else:
            return f"⏱️ 【剩餘 {countdown}】 ⚖️ 觀望 - 價格在基準水面 {base_price} 附近隨機震盪，等待邊界訊號。", "info"

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

# --- 網頁介面設計 ---
st.title("🎯 Binance Predict 5分鐘勝率提升器")

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
    except:
        st.metric(label="連線狀態", value="載入中...")

with col_price:
    show_realtime_price(symbol)
    
with col_info:
    st.info("💡 實戰口訣：重點關注【倒數 1 分鐘】內的保衛戰訊號，勝率最高！")

@st.fragment(run_every=10)
def show_prediction_dashboard(selected_symbol, all_symbols):
    # --- 1. 背景靜音雷達 (掃描非選取中的幣種) ---
    other_symbols = [s for s in all_symbols if s != selected_symbol]
    radar_messages = []
    
    for osym in other_symbols:
        d1 = add_technical_indicators(get_binance_klines(osym, "1m", limit=100))
        d5 = add_technical_indicators(get_binance_klines(osym, "5m", limit=100))
        d15 = add_technical_indicators(get_binance_klines(osym, "15m", limit=100))
        clean_name = osym.replace("USDT", "") 
        radar_msg = generate_radar_signal(d1, d5, d15, clean_name)
        radar_messages.append(radar_msg)
    
    st.markdown(f"📡 **背景靜音雷達：** {' ｜ '.join(radar_messages)}")
    st.divider()
    
    # --- 2. 主戰場資料抓取與訊號判定 (有聲音) ---
    df_1m = add_technical_indicators(get_binance_klines(selected_symbol, "1m"))
    df_5m = add_technical_indicators(get_binance_klines(selected_symbol, "5m"))
    df_15m = add_technical_indicators(get_binance_klines(selected_symbol, "15m"))
    
    signal_text, signal_type = generate_prediction_signal(df_1m, df_5m, df_15m)
    
    if signal_type == "success":
        st.success(signal_text)
        play_alert_sound()
    elif signal_type == "error":
        st.error(signal_text)
        play_alert_sound()
    elif signal_type == "warning":
        st.warning(signal_text)
    else:
        st.info(signal_text)
    
    current_base_price = df_5m.iloc[-1]['Open'] if not df_5m.empty else None
    
    col1, col2, col3 = st.columns(3)
    chart_config = {'displayModeBar': False, 'scrollZoom': True}
    
    with col1:
        if not df_1m.empty:
            st.plotly_chart(plot_advanced_candlestick(df_1m, "1 分鐘線 (尋找超買超賣邊界)", baseline_price=current_base_price), use_container_width=True, config=chart_config, key="c1")
    with col2:
        if not df_5m.empty:
            st.plotly_chart(plot_advanced_candlestick(df_5m, "5 分鐘線 (預測結算目標區)", baseline_price=current_base_price), use_container_width=True, config=chart_config, key="c5")
    with col3:
        if not df_15m.empty:
            st.plotly_chart(plot_advanced_candlestick(df_15m, "15 分鐘線 (確認主力大趨勢)"), use_container_width=True, config=chart_config, key="c15")

show_prediction_dashboard(symbol, available_symbols)
