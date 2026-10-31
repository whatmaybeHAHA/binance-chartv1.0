import streamlit as st
import pandas as pd
import numpy as np
import requests
import plotly.graph_objects as go
import concurrent.futures

st.set_page_config(layout="wide", page_title="幣安 5 分鐘預測高勝率看盤系統 - 終極戰術版")

# 🌟 注入 CSS 強制放大 Checkbox 按鈕與文字
st.markdown("""
<style>
div[data-testid="stCheckbox"] label span {
    font-size: 18px !important;
    font-weight: bold;
    color: #E0E0E0;
}
div[data-testid="stCheckbox"] label div[role="checkbox"] {
    transform: scale(1.3);
    margin-right: 10px;
}
</style>
""", unsafe_allow_html=True)

# --- 側邊欄：戰術參數微調 ---
with st.sidebar:
    st.header("⚙️ 戰術參數微調")
    st.markdown("針對不同的市場波動情緒，你可以隨時微調極端反彈的觸發門檻：")
    rsi_long_th = st.slider("⚡ 搶反彈 (買漲) RSI 觸發門檻", min_value=15, max_value=45, value=30, step=1, help="當價格跌穿下軌，且 RSI 低於此數值時觸發買漲。")
    rsi_short_th = st.slider("⚡ 摸頭空 (買跌) RSI 觸發門檻", min_value=55, max_value=85, value=70, step=1, help="當價格突破上軌，且 RSI 高於此數值時觸發買跌。")
    st.divider()
    st.info("""
    💡 **實戰參數調整提示：**
    
    🐂 **狂暴牛市**：
    建議將「摸頭空」門檻調高至 80 以上，避免被強勢多頭嘎空。
    
    🐻 **絕望熊市**：
    建議將「搶反彈(買漲)」門檻調低至 20 以下，避免在無量暴跌時接刀破底。
    """)

def play_alert_sound(signal_mode="trend"):
    """根據戰術模式播放不同的提示音"""
    t = pd.Timestamp.now().timestamp()
    if signal_mode == "rebound":
        audio_url = f"https://actions.google.com/sounds/v1/alarms/bugle_tune.ogg?v={t}"
    else:
        audio_url = f"https://actions.google.com/sounds/v1/alarms/beep_short.ogg?v={t}"
        
    audio_html = f"""
        <audio autoplay style="display:none;">
            <source src="{audio_url}" type="audio/ogg">
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
    
    delta = df['Close'].diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
    rs = avg_gain / avg_loss
    df['RSI'] = 100 - (100 / (1 + rs))
    
    high_low = df['High'] - df['Low']
    high_close = (df['High'] - df['Close'].shift()).abs()
    low_close = (df['Low'] - df['Close'].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df['ATR'] = tr.rolling(window=14).mean()
    return df

def calculate_confidence_score(df_1m, df_5m, df_15m, enable_rebound, rsi_long_th, rsi_short_th):
    if df_1m.empty or df_5m.empty or df_15m.empty:
        return 0, "無資料", "info", 0.0, 0.0, "trend"

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
    
    # 🌟 判斷是否落入「死亡糾纏區」 (價差極小，不到 0.2 倍 ATR)
    in_dead_zone = abs(price_diff) < (0.2 * atr_1m)
    
    # ⚡ 戰術 B：極端搶反彈 (最高優先級)
    if enable_rebound:
        if current_price <= bb_lower and rsi_1m <= rsi_long_th:
            score = min(85.0 + (rsi_long_th - rsi_1m), 99.0)
            desc = f"⚡ 【極端反彈】 買漲 (UP) ｜ 貫穿下軌 ＋ RSI極度恐慌 ({rsi_1m:.1f})"
            return score, desc, "success", moat_ratio, base_price, "rebound"
            
        if current_price >= bb_upper and rsi_1m >= rsi_short_th:
            score = min(85.0 + (rsi_1m - rsi_short_th), 99.0)
            desc = f"⚡ 【極端回落】 買跌 (DOWN) ｜ 貫穿上軌 ＋ RSI極度貪婪 ({rsi_1m:.1f})"
            return score, desc, "error", moat_ratio, base_price, "rebound"

    # 🛑 戰術 C：死亡糾纏區處理 (打破邊界僵局)
    if in_dead_zone:
        if trend_up and rsi_1m > 55:
            score = 62.0
            desc = f"📈 【倒數 {countdown}】 蓄勢買漲 (UP) ｜ 糾纏區待突破 ({moat_ratio:.2f}x ATR)"
            return score, desc, "warning", moat_ratio, base_price, "trend"
        elif not trend_up and rsi_1m < 45:
            score = 62.0
            desc = f"📉 【倒數 {countdown}】 蓄勢買跌 (DOWN) ｜ 糾纏區待跌破 ({moat_ratio:.2f}x ATR)"
            return score, desc, "warning", moat_ratio, base_price, "trend"
        else:
            score = 45.0
            desc = f"⚖️ 【倒數 {countdown}】 無方向震盪 ｜ 死亡糾纏區，指標混亂強制觀望"
            return score, desc, "info", moat_ratio, base_price, "trend"

    # 🛡️ 戰術 A：常規順勢與保衛戰
    score = 50.0 
    direction = "UP" if price_diff >= 0 else "DOWN"
    
    if sec_left <= 60:
        score += 15.0
        if sec_left <= 30: score += 10.0
        
        if moat_ratio >= 1.5: score += 25.0
        elif moat_ratio >= 1.0: score += 18.0
        elif moat_ratio >= 0.5: score += 8.0
        else: score -= 20.0 
    else:
        if (direction == "UP" and trend_up) or (direction == "DOWN" and not trend_up): score += 15.0
        if moat_ratio >= 1.0: score += 10.0

    if direction == "UP":
        if 50 <= rsi_1m <= 68: score += 10.0
        elif rsi_1m > 75: score -= 15.0  
        elif rsi_1m < 40: score -= 15.0
    else:
        if 32 <= rsi_1m <= 50: score += 10.0
        elif rsi_1m < 25: score -= 15.0  
        elif rsi_1m > 60: score -= 15.0

    # 🛡️ 動態時間防禦機制 (布林邊界修正)
    is_touch_upper = current_price >= bb_upper
    is_touch_lower = current_price <= bb_lower
    
    if sec_left <= 60:
        # 倒數 60 秒生死戰：零容錯率，只要觸碰邊界一律重罰
        if direction == "UP" and is_touch_upper: score -= 12.0
        elif direction == "DOWN" and is_touch_lower: score -= 12.0
    else:
        # 前 4 分鐘佈局期：僅在 RSI 過熱時才視為假突破重罰，容許良性突破
        if direction == "UP" and is_touch_upper and rsi_1m > 75: score -= 12.0
        elif direction == "DOWN" and is_touch_lower and rsi_1m < 25: score -= 12.0

    score = float(np.clip(score, 5.0, 98.0))
    
    if score >= 80:
        status_type = "success" if direction == "UP" else "error"
        action = "🔥 買漲 (UP)" if direction == "UP" else "🔥 買跌 (DOWN)"
        desc = f"【倒數 {countdown}】 訊號：{action} ｜ 護城河倍數: {moat_ratio:.2f}x ATR"
    elif score >= 65:
        status_type = "warning"
        action = "買漲 (UP)" if direction == "UP" else "買跌 (DOWN)"
        desc = f"⚠️ 【倒數 {countdown}】 訊號：{action} 順勢觀察 ｜ 護城河薄弱 ({moat_ratio:.2f}x ATR)"
    else:
        status_type = "info"
        desc = f"⏳ 【倒數 {countdown}】 震盪觀望 ｜ 距水面過近或指標背離"

    return score, desc, status_type, moat_ratio, base_price, "trend"

def generate_radar_signal(df_1m, df_5m, df_15m, symbol_name, enable_rebound, rsi_long_th, rsi_short_th):
    score, _, _, _, _, signal_mode = calculate_confidence_score(df_1m, df_5m, df_15m, enable_rebound, rsi_long_th, rsi_short_th)
    if score >= 80:
        latest_1m = df_1m.iloc[-1]
        latest_5m = df_5m.iloc[-1]
        if signal_mode == "rebound":
            action = "💥 反彈UP" if latest_1m['Close'] <= latest_1m['BB_Lower'] else "💥 回落DOWN"
            return f"**[{symbol_name}] {action} ({score:.0f}%)**"
        else:
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
            y=baseline_price, line_dash="dash", line_color="#FFFFFF", line_width=2, opacity=0.9,
            annotation_text=f"🎯 基準水面", annotation_position="bottom right",
            annotation_font_color="white", annotation_font_size=13
        )
    
    fig.update_layout(
        title=dict(text=f"{title} | RSI: {latest_rsi:.1f}", font=dict(size=16)),
        xaxis_rangeslider_visible=False, margin=dict(l=10, r=10, t=40, b=60),
        height=420, template="plotly_dark", uirevision="constant",
        legend=dict(orientation="h", yanchor="top", y=-0.2, xanchor="center", x=0.5)
    )
    fig.update_xaxes(tickangle=-45, nticks=8, tickformat="%H:%M") 
    fig.update_xaxes(range=[df['Open time'].iloc[-80], df['Open time'].iloc[-1]])
    return fig

# --- 主介面 ---
st.title("🎯 Binance Predict 5分鐘勝率提升器")

available_symbols = ["BTCUSDT", "ETHUSDT", "BNBUSDT"]

col_setting, col_price, col_toggle = st.columns([1.5, 1.5, 2])
with col_setting:
    symbol = st.selectbox("選擇主戰場 (發出音效)", available_symbols)
with col_toggle:
    st.write("") 
    enable_rebound = st.checkbox("☑️ 啟用【極端搶反彈】偵測 (貫穿布林邊界 + RSI過熱)", value=True)

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

@st.fragment(run_every=10)
def show_prediction_dashboard(selected_symbol, all_symbols, enable_rebound_flag, r_long, r_short):
    other_symbols = [s for s in all_symbols if s != selected_symbol]
    def fetch_radar(osym):
        d1 = add_technical_indicators(get_binance_klines(osym, "1m", limit=80))
        d5 = add_technical_indicators(get_binance_klines(osym, "5m", limit=80))
        d15 = add_technical_indicators(get_binance_klines(osym, "15m", limit=80))
        return generate_radar_signal(d1, d5, d15, osym.replace("USDT", ""), enable_rebound_flag, r_long, r_short)

    with concurrent.futures.ThreadPoolExecutor() as executor:
        radar_messages = list(executor.map(fetch_radar, other_symbols))
    
    st.markdown(f"<div style='text-align: left; font-size: 16px; margin-top: 10px; margin-bottom: 2px; color: #CCCCCC;'>📡 <b>背景靜音雷達：</b> {' ｜ '.join(radar_messages)}</div>", unsafe_allow_html=True)
    
    df_1m = add_technical_indicators(get_binance_klines(selected_symbol, "1m"))
    df_5m = add_technical_indicators(get_binance_klines(selected_symbol, "5m"))
    df_15m = add_technical_indicators(get_binance_klines(selected_symbol, "15m"))
    
    score, desc_text, status_type, moat_ratio, base_price, signal_mode = calculate_confidence_score(df_1m, df_5m, df_15m, enable_rebound_flag, r_long, r_short)
    
    score_color = "#4FC3F7" if score >= 80 else ("#FFD54F" if score >= 65 else "#9E9E9E")
    
    st.markdown(f"<div style='text-align: left; font-size: 24px; font-weight: bold; color: {score_color}; margin-top: 0px; margin-bottom: 15px;'>🎯 勝率信心指數：{score:.1f}%</div>", unsafe_allow_html=True)
    
    if status_type == "success":
        st.success(desc_text)
        if score >= 80: play_alert_sound(signal_mode)
    elif status_type == "error":
        st.error(desc_text)
        if score >= 80: play_alert_sound(signal_mode)
    elif status_type == "warning":
        st.warning(desc_text)
    else:
        st.info(desc_text)
    
    col1, col2, col3 = st.columns(3)
    chart_config = {'displayModeBar': False, 'scrollZoom': True}
    with col1:
        if not df_1m.empty: st.plotly_chart(plot_advanced_candlestick(df_1m, "1 分鐘線 (尋找超買超賣邊界)", baseline_price=base_price), use_container_width=True, config=chart_config, key="c1")
    with col2:
        if not df_5m.empty: st.plotly_chart(plot_advanced_candlestick(df_5m, "5 分鐘線 (預測結算目標區)", baseline_price=base_price), use_container_width=True, config=chart_config, key="c5")
    with col3:
        if not df_15m.empty: st.plotly_chart(plot_advanced_candlestick(df_15m, "15 分鐘線 (確認主力大趨勢)"), use_container_width=True, config=chart_config, key="c15")

show_prediction_dashboard(symbol, available_symbols, enable_rebound, rsi_long_th, rsi_short_th)

# --- 網頁底部：系統說明書 ---
st.divider()
with st.expander("📖 系統戰術與量化判斷邏輯 (點擊展開)"):
    st.markdown("""
    ### 🔥 戰術 A：順勢與保衛戰 (基礎 50 分)
    *   **時間權重：** 進入最後 1 分鐘 `+15分`，最後 30 秒再 `+10分`。
    *   **護城河權重 (價差 ÷ 1分ATR)：** 大於 1.5 倍 ATR `+25分`；大於 1.0 倍 ATR `+18分`；大於 0.5 倍 ATR `+8分`；貼近基準線或低於水面 `重罰 -20分`。
    
    ### ⏳ 動態時間防禦機制 (布林邊界)
    *   **前 4 分鐘佈局期：** 觸碰布林邊界時，僅在 RSI 過熱 (>75 或 <25) 時扣 12 分，容許良性突破。
    *   **最後 60 秒保衛戰：** 零容錯率，只要觸碰布林邊界，無視 RSI 無條件扣 12 分。

    ### 🛡️ 戰術 C：死亡糾纏區 (邊界僵局處理)
    *   **觸發條件：** 當前價差小於 0.2 倍 ATR (極度貼近基準線)。
    *   **判定邏輯：** 暫停計算傳統護城河，改看短線 RSI 推力與大趨勢方向。
    *   **給分：** 動能一致時給予 62 分 (蓄勢黃燈)，動能混亂時給予 45 分 (無方向藍燈強制觀望)。

    ### ⚡ 戰術 B：極端搶反彈 (獨立高分通道)
    *   **觸發條件：** 價格貫穿布林上下軌 ＋ RSI 超越側邊欄設定門檻。
    *   **給分邏輯：** 無視護城河深度，觸發基礎即給予 85 分，情緒越極端分數越高 (最高 99%)。

    ### 🎯 燈號對照表
    *   🟢 **80% ~ 100% (高勝率)：** 觸發音效，建議進場主力倉位。
    *   🟡 **65% ~ 79% (順勢觀察)：** 不觸發音效，建議輕倉跟隨或觀望最後 30 秒變化。
    *   ⚪ **低於 65% (震盪觀望)：** 強制觀望，嚴禁下單。
    """)
