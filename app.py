import math
import concurrent.futures
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

try:
    import gspread
    from google.oauth2.service_account import Credentials
    GSPREAD_AVAILABLE = True
except ImportError:
    GSPREAD_AVAILABLE = False

st.set_page_config(layout="wide", page_title="幣安 5 分鐘實證狙擊系統 V3 (BTC/ETH)")

# --- 全局常數（做法 A：純 BTC / ETH 雙核心） ---
AVAILABLE_SYMBOLS = ["BTCUSDT", "ETHUSDT"]
WEEKDAY_MAP = ["一", "二", "三", "四", "五", "六", "日"]
# 依據 14 天真實回測校準之各幣種搶反彈基準勝率
REBOUND_EMPIRICAL_WR = {
    "BTCUSDT": 0.404,
    "ETHUSDT": 0.341,
}
SHEET_HEADERS = [
    "紀錄時間(UTC+8)", "5分K開盤時間", "幣種", "剩餘秒數", "時段屬性",
    "5分開盤價", "當下Mid價", "價差", "1分ATR", "護城河倍數",
    "1分波動率(sigma)", "Z_Score", "V3預估勝率(%)", "建議5U門檻",
    "系統方向", "戰術標籤", "校正量能比", "是否縮量(<0.5x)",
    "主動買盤比(%)", "1分RSI", "本幣1H趨勢",
    "UP_5U可拿回", "DOWN_5U可拿回", "造市商抽水率(%)", "備註"
]

# --- Session State 初始化 ---
if "selected_symbol" not in st.session_state or st.session_state.selected_symbol not in AVAILABLE_SYMBOLS:
    st.session_state.selected_symbol = "BTCUSDT"
if "latest_snapshot" not in st.session_state:
    st.session_state.latest_snapshot = None
if "local_records" not in st.session_state:
    st.session_state.local_records = []

# --- 注入 CSS：平板觸控友善超大按鈕與放大核取方塊 ---
st.markdown("""
<style>
div[data-testid="stCheckbox"] label span {
    font-size: 16px !important;
    font-weight: bold;
    color: #E0E0E0;
}
div[data-testid="stCheckbox"] label div[role="checkbox"] {
    transform: scale(1.25);
    margin-right: 10px;
}
.symbol-btn-wrap button {
    height: 54px !important;
    font-size: 21px !important;
    font-weight: bold !important;
    border-radius: 10px !important;
}
</style>
""", unsafe_allow_html=True)


# --- 時間、時段與連線工具 ---
def get_taiwan_time_info():
    """取得台灣時間 (UTC+8, tz-naive)、5 分鐘局號、剩餘秒數與時段標籤"""
    now_utc = datetime.now(timezone.utc)
    ts = now_utc.timestamp()
    now_tw = now_utc.replace(tzinfo=None) + timedelta(hours=8)

    epoch_5m = int(ts // 300)
    sec_elapsed_5m = int(ts % 300)
    sec_left = 300 - sec_elapsed_5m
    sec_elapsed_1m = int(ts % 60)

    round_5m_open_tw = datetime.fromtimestamp(
        epoch_5m * 300, tz=timezone.utc
    ).replace(tzinfo=None) + timedelta(hours=8)

    day_type = "假日" if now_tw.weekday() >= 5 else "平日"
    h = now_tw.hour
    is_trap_hour = (12 <= h < 15)
    if h >= 20 or h < 2:
        slot_label = f"{day_type}-黃金時段(20-02)"
    elif is_trap_hour:
        slot_label = f"{day_type}-陷阱時段(12-15)"
    else:
        slot_label = f"{day_type}-一般時段"

    return now_tw, epoch_5m, sec_left, sec_elapsed_1m, round_5m_open_tw, is_trap_hour, slot_label


@st.cache_resource
def get_http_session():
    session = requests.Session()
    session.headers.update({"User-Agent": "BinancePredictV3/3.0"})
    return session


@st.cache_resource
def get_gsheet_worksheet():
    """連線 Google Sheets；若尚未填寫 Secrets 則安全降級回本機暫存"""
    if not GSPREAD_AVAILABLE:
        return None, "尚未安裝 gspread，請確認 requirements.txt 已更新"
    if "gcp_service_account" not in st.secrets or "gsheet_url" not in st.secrets:
        return None, "尚未設定 Streamlit Secrets (gcp_service_account / gsheet_url)"
    try:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds_dict = dict(st.secrets["gcp_service_account"])
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)
        sh = client.open_by_url(st.secrets["gsheet_url"])
        ws = sh.sheet1

        first_row = ws.row_values(1)
        if not first_row:
            ws.append_row(SHEET_HEADERS, value_input_option="USER_ENTERED")
        return ws, "已連線 Google Sheets"
    except Exception as e:
        return None, f"Google Sheets 連線失敗: {e}"


def play_alert_sound(sound_type="main_trend"):
    t = datetime.now(timezone.utc).timestamp()
    sound_urls = {
        "main_trend": f"https://actions.google.com/sounds/v1/alarms/beep_short.ogg?v={t}",
        "main_rebound": f"https://actions.google.com/sounds/v1/alarms/bugle_tune.ogg?v={t}",
        "radar_trend": f"https://actions.google.com/sounds/v1/cartoon/pop.ogg?v={t}",
        "radar_rebound": f"https://actions.google.com/sounds/v1/alarms/digital_watch_alarm_long.ogg?v={t}",
    }
    url = sound_urls.get(sound_type, sound_urls["main_trend"])
    st.markdown(
        f'<audio autoplay style="display:none;"><source src="{url}" type="audio/ogg"></audio>',
        unsafe_allow_html=True,
    )


# --- 幣安公開 API 與本地重採樣 ---
def get_mid_price(symbol):
    """抓取最優買賣中間價 (Top-of-Book Mid-Price)，對齊 Chainlink 結算源"""
    url = "https://data-api.binance.vision/api/v3/ticker/bookTicker"
    try:
        res = get_http_session().get(url, params={"symbol": symbol}, timeout=3).json()
        return (float(res["bidPrice"]) + float(res["askPrice"])) / 2.0
    except Exception:
        return None


@st.cache_data(ttl=4)
def fetch_1m_klines(symbol, epoch_5m, limit=1000):
    """
    單一 API 抓取 1 分 K，綁定 epoch_5m 確保整點換局瞬間強制清快取。
    1000 根可合成 66 根 15m K 線，截取最後 40 根畫圖時布林通道滿版不留白。
    """
    url = "https://data-api.binance.vision/api/v3/klines"
    params = {"symbol": symbol, "interval": "1m", "limit": limit}
    try:
        res = get_http_session().get(url, params=params, timeout=3)
        res.raise_for_status()
        data = res.json()
        df = pd.DataFrame(data, columns=[
            "Open time", "Open", "High", "Low", "Close", "Volume",
            "Close time", "Quote asset volume", "Number of trades",
            "Taker buy base asset", "Taker buy quote asset", "Ignore"
        ])
        df["Open time"] = pd.to_datetime(df["Open time"], unit="ms") + pd.Timedelta(hours=8)
        for col in ["Open", "High", "Low", "Close", "Volume", "Taker buy base asset"]:
            df[col] = df[col].astype(float)
        return df
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=300)
def fetch_1h_trend(symbol, epoch_5m):
    """每 5 分鐘更新一次 1H 大趨勢（僅供搶反彈防接刀與寫入 Google Sheets）"""
    url = "https://data-api.binance.vision/api/v3/klines"
    params = {"symbol": symbol, "interval": "1h", "limit": 80}
    try:
        res = get_http_session().get(url, params=params, timeout=3)
        res.raise_for_status()
        data = res.json()
        closes = pd.Series([float(row[4]) for row in data])
        ema20 = closes.ewm(span=20, adjust=False).mean().iloc[-1]
        ema50 = closes.ewm(span=50, adjust=False).mean().iloc[-1]
        last_close = closes.iloc[-1]
        if last_close > ema20 and ema20 > ema50:
            return "多頭"
        elif last_close < ema20 and ema20 < ema50:
            return "空頭"
        return "震盪"
    except Exception:
        return "震盪"


def resample_klines(df_1m, rule):
    if df_1m.empty:
        return pd.DataFrame()
    return (
        df_1m.set_index("Open time")
        .resample(rule, label="left", closed="left")
        .agg({
            "Open": "first",
            "High": "max",
            "Low": "min",
            "Close": "last",
            "Volume": "sum",
            "Taker buy base asset": "sum",
        })
        .dropna(subset=["Open"])
        .reset_index()
    )


def add_technical_indicators(df):
    if df.empty or len(df) < 21:
        return df
    df = df.copy()
    df["MA5"] = df["Close"].rolling(window=5).mean()
    df["MA20"] = df["Close"].rolling(window=20).mean()
    df["std"] = df["Close"].rolling(window=20).std()
    df["BB_Upper"] = df["MA20"] + (df["std"] * 2.0)
    df["BB_Lower"] = df["MA20"] - (df["std"] * 2.0)
    df["Vol_MA20"] = df["Volume"].rolling(window=20).mean()

    delta = df["Close"].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / 14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / 14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    # 修正新版 Pandas TypeError：以 pd.Series 包裝 fillna 預設值
    rsi_fallback = pd.Series(np.where(avg_gain > 0, 100.0, 50.0), index=df.index)
    df["RSI"] = (100.0 - (100.0 / (1.0 + rs))).fillna(rsi_fallback)

    high_low = df["High"] - df["Low"]
    high_close = (df["High"] - df["Close"].shift()).abs()
    low_close = (df["Low"] - df["Close"].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df["ATR"] = tr.rolling(window=14).mean()
    df["LogRet"] = np.log(df["Close"] / df["Close"].shift(1))
    return df


# --- V3 終極實證量化評分引擎 ---
def evaluate_market_v3(
    symbol, df_1m, df_5m, mid_price, trend_1h,
    enable_rebound, rsi_long_th, rsi_short_th, only_low_vol_alert
):
    now_tw, _, sec_left, sec_elapsed_1m, round_5m_open_tw, is_trap_hour, slot_label = get_taiwan_time_info()
    countdown = f"{sec_left // 60:02d}:{sec_left % 60:02d}"

    base_out = {
        "symbol": symbol, "p_est": 0.50, "p_base": 0.50, "direction": "NEUTRAL",
        "desc": "資料載入中...", "status_type": "info", "tactic_tag": "載入中",
        "moat_ratio": 0.0, "base_price": 0.0, "mid_price": 0.0, "price_diff": 0.0,
        "atr_1m": 0.0, "sigma_1m": 0.0, "z_score": 0.0,
        "vol_ratio": 1.0, "is_low_vol": False, "taker_buy_ratio": 0.5, "rsi_1m": 50.0,
        "breakeven_5u": 10.0, "target_5u": 10.5,
        "signal_mode": "trend", "should_alert": False, "is_locked": False,
        "sec_left": sec_left, "countdown": countdown,
        "round_5m_open_str": round_5m_open_tw.strftime("%Y-%m-%d %H:%M"),
        "trend_1h": trend_1h, "is_trap_hour": is_trap_hour, "slot_label": slot_label,
    }
    if df_1m.empty or df_5m.empty or len(df_1m) < 35:
        return base_out

    latest_1m = df_1m.iloc[-1]
    prev_1m = df_1m.iloc[-2]

    match_5m = df_5m[df_5m["Open time"] == pd.Timestamp(round_5m_open_tw)]
    base_price = float(match_5m["Open"].iloc[0]) if not match_5m.empty else float(df_5m.iloc[-1]["Open"])
    current_price = float(mid_price) if mid_price is not None else float(latest_1m["Close"])
    price_diff = current_price - base_price

    atr_val = prev_1m["ATR"] if not np.isnan(prev_1m["ATR"]) and prev_1m["ATR"] > 0 else latest_1m["ATR"]
    atr_1m = float(atr_val) if not np.isnan(atr_val) and atr_val > 0 else max(current_price * 0.0005, 0.01)
    moat_ratio = abs(price_diff) / atr_1m

    # 第一層：純波動率標準化距離 (Z-Score) 與客觀基準勝率
    sigma_raw = df_1m["LogRet"].iloc[-31:-1].std()
    sigma_1m = float(sigma_raw) if not np.isnan(sigma_raw) and sigma_raw > 0 else (atr_1m / current_price) * 0.7
    t_min = max(sec_left, 1) / 60.0
    z_score = price_diff / (base_price * sigma_1m * math.sqrt(t_min))
    p_base_lead = 0.5 * (1.0 + math.erf(abs(z_score) / math.sqrt(2.0)))

    # 時間投影量能校正（剛換分 < 12 秒時沿用上一根完整 K 線）
    vol_ma = float(prev_1m["Vol_MA20"]) if not np.isnan(prev_1m["Vol_MA20"]) and prev_1m["Vol_MA20"] > 0 else 1.0
    if sec_elapsed_1m >= 12:
        proj_vol = float(latest_1m["Volume"]) * (60.0 / sec_elapsed_1m)
        vol_ratio = proj_vol / vol_ma
        active_bar = latest_1m
    else:
        vol_ratio = float(prev_1m["Volume"]) / vol_ma
        active_bar = prev_1m

    is_low_vol = vol_ratio < 0.5
    bar_vol = float(active_bar["Volume"])
    taker_buy_ratio = float(active_bar["Taker buy base asset"]) / bar_vol if bar_vol > 0 else 0.5

    rsi_1m = float(latest_1m["RSI"]) if not np.isnan(latest_1m["RSI"]) else 50.0
    bb_upper = float(latest_1m["BB_Upper"])
    bb_lower = float(latest_1m["BB_Lower"])

    # 1H 防接刀濾網（僅作用於搶反彈）
    eff_rsi_long = rsi_long_th - 6 if trend_1h == "空頭" else rsi_long_th
    eff_rsi_short = rsi_short_th + 6 if trend_1h == "多頭" else rsi_short_th

    res = base_out.copy()
    res.update({
        "p_base": p_base_lead, "moat_ratio": moat_ratio,
        "base_price": base_price, "mid_price": current_price,
        "price_diff": price_diff, "atr_1m": atr_1m,
        "sigma_1m": sigma_1m, "z_score": z_score,
        "vol_ratio": vol_ratio, "is_low_vol": is_low_vol,
        "taker_buy_ratio": taker_buy_ratio, "rsi_1m": rsi_1m,
    })

    # 1. 最後 15 秒全面靜音硬鎖
    if sec_left <= 15:
        lead_dir = "UP" if price_diff >= 0 else "DOWN"
        p_est = float(np.clip(p_base_lead, 0.05, 0.96))
        res.update({
            "p_est": p_est, "direction": lead_dir,
            "breakeven_5u": 5.0 / p_est, "target_5u": 5.25 / p_est,
            "desc": f"🔒 【尾盤靜音鎖 {countdown}】 剩餘不足 15 秒，主戰場與雷達全面停止提示",
            "status_type": "info", "tactic_tag": "尾盤靜音鎖",
            "signal_mode": "locked", "should_alert": False, "is_locked": True,
        })
        return res

    # 2. 戰術 B：幣種化極端搶反彈（硬防線：剩餘 >= 90s 且 距水面 <= 1.8x ATR 且 1H 防接刀）
    if enable_rebound and sec_left >= 90 and moat_ratio <= 1.8:
        emp_wr = REBOUND_EMPIRICAL_WR.get(symbol, 0.35)
        if current_price <= bb_lower and rsi_1m <= eff_rsi_long:
            p_est = max(emp_wr - 0.03, 0.25) if trend_1h == "空頭" else emp_wr
            warn_tag = " ｜ 🐻 1H空頭警戒(已收緊門檻)" if trend_1h == "空頭" else ""
            res.update({
                "p_est": p_est, "direction": "UP",
                "breakeven_5u": 5.0 / p_est, "target_5u": 5.25 / p_est,
                "desc": f"⚡ 【倒數 {countdown} 極端搶反彈】 買漲 (UP) ｜ 貫穿下軌 + RSI {rsi_1m:.1f} (門檻≤{eff_rsi_long}){warn_tag}",
                "status_type": "success", "tactic_tag": "極端搶反彈",
                "signal_mode": "rebound", "should_alert": True,
            })
            return res

        if current_price >= bb_upper and rsi_1m >= eff_rsi_short:
            p_est = max(emp_wr - 0.03, 0.25) if trend_1h == "多頭" else emp_wr
            warn_tag = " ｜ 🐂 1H多頭警戒(已收緊門檻)" if trend_1h == "多頭" else ""
            res.update({
                "p_est": p_est, "direction": "DOWN",
                "breakeven_5u": 5.0 / p_est, "target_5u": 5.25 / p_est,
                "desc": f"⚡ 【倒數 {countdown} 極端摸頭空】 買跌 (DOWN) ｜ 貫穿上軌 + RSI {rsi_1m:.1f} (門檻≥{eff_rsi_short}){warn_tag}",
                "status_type": "error", "tactic_tag": "極端摸頭空",
                "signal_mode": "rebound", "should_alert": True,
            })
            return res

    # 3. 死亡糾纏區（價差 < 0.2x ATR）
    if abs(price_diff) < (0.2 * atr_1m):
        res.update({
            "p_est": 0.50, "direction": "NEUTRAL",
            "breakeven_5u": 10.0, "target_5u": 10.5,
            "desc": f"⚖️ 【倒數 {countdown}】 死亡糾纏區 ｜ 貼近基準水面 ({moat_ratio:.2f}x ATR)，強制觀望",
            "status_type": "info", "tactic_tag": "死亡糾纏區",
            "signal_mode": "trend", "should_alert": False,
        })
        return res

    # 4. V3 實證順勢引擎（完全依 41,440 筆體檢結果校準）
    direction = "UP" if price_diff >= 0 else "DOWN"
    delta_p = 0.0

    # (a) 量能反轉因子：縮量安靜最強 (+3.0%)，爆量風險 (-1.5%)
    if vol_ratio < 0.5:
        delta_p += 0.030
        vol_tag = f"🌟 縮量安靜 ({vol_ratio:.2f}x)"
    elif vol_ratio < 1.5:
        delta_p += 0.010
        vol_tag = f"📊 量能正常 ({vol_ratio:.2f}x)"
    else:
        delta_p -= 0.015
        vol_tag = f"⚠️ 爆量波動風險 ({vol_ratio:.2f}x)"

    # (b) 過熱與布林地雷防禦
    is_rsi_hot = (direction == "UP" and rsi_1m > 75) or (direction == "DOWN" and rsi_1m < 25)
    is_rsi_cool = (direction == "UP" and rsi_1m < 40) or (direction == "DOWN" and rsi_1m > 60)
    is_bb_touch = (direction == "UP" and current_price >= bb_upper) or (direction == "DOWN" and current_price <= bb_lower)

    danger_reasons = []
    if is_rsi_hot:
        delta_p -= 0.050
        danger_reasons.append(f"RSI過熱({rsi_1m:.1f})")
    elif is_rsi_cool:
        delta_p += 0.015

    if is_bb_touch:
        delta_p -= 0.025
        danger_reasons.append("碰布林邊界")
    else:
        delta_p += 0.010

    if vol_ratio >= 1.5:
        danger_reasons.append(f"爆量({vol_ratio:.1f}x)")

    delta_p = float(np.clip(delta_p, -0.075, 0.055))
    p_est = float(np.clip(p_base_lead + delta_p, 0.05, 0.96))
    breakeven_5u = 5.0 / p_est
    target_5u = 5.25 / p_est
    action_cn = "買漲 (UP)" if direction == "UP" else "買跌 (DOWN)"

    is_danger = len(danger_reasons) > 0
    in_time_window = (15 < sec_left <= 150)

    # 分層輸出與嚴格靜音控制
    if is_danger:
        status_type = "warning"
        tactic_tag = "地雷禁買"
        should_alert = False
        desc = f"🚫 【倒數 {countdown} 地雷禁買】 {action_cn} ｜ 觸發高風險因子：{' + '.join(danger_reasons)}，強制靜音觀望"
    elif not in_time_window:
        status_type = "info"
        tactic_tag = "早盤等待期"
        should_alert = False
        desc = f"⏳ 【倒數 {countdown} 早盤等待】 {action_cn} ｜ 尚餘超過 150 秒 (護城河 {moat_ratio:.2f}x ｜ {vol_tag})，靜待狙擊窗開啟"
    elif p_est > 0.88:
        status_type = "warning"
        tactic_tag = "低賠率黑洞(>88%)"
        should_alert = False
        desc = f"⚠️ 【倒數 {countdown} 低賠率警戒區】 {action_cn} 勝率 {p_est*100:.1f}% (>88%) ｜ 實測賠率過爛易倒吐，系統自動靜音 (除非 App ≥ {target_5u:.2f} U)"
    elif (0.72 <= p_est <= 0.88) and (moat_ratio >= 0.5):
        if is_low_vol:
            status_type = "success" if direction == "UP" else "error"
            tactic_tag = "縮量黃金狙擊"
            should_alert = True
            desc = f"🌟 【倒數 {countdown} 縮量黃金狙擊】 {action_cn} ｜ 護城河 {moat_ratio:.2f}x ｜ {vol_tag} ｜ 甜蜜區間 (72-88%)"
        else:
            status_type = ("success" if direction == "UP" else "error") if not only_low_vol_alert else "warning"
            tactic_tag = "標準順勢狙擊" if not only_low_vol_alert else "正常量觀察"
            should_alert = (not only_low_vol_alert)
            prefix = "🔥 【倒數" if should_alert else "👀 【倒數"
            hint = " ｜ 等待縮量 <0.5x" if only_low_vol_alert else ""
            desc = f"{prefix} {countdown} {tactic_tag}】 {action_cn} ｜ 護城河 {moat_ratio:.2f}x ｜ {vol_tag}{hint}"
    else:
        status_type = "info"
        tactic_tag = "震盪觀望"
        should_alert = False
        desc = f"⏳ 【倒數 {countdown}】 震盪觀望 ｜ 護城河 {moat_ratio:.2f}x ｜ {vol_tag}"

    res.update({
        "p_est": p_est, "direction": direction,
        "breakeven_5u": breakeven_5u, "target_5u": target_5u,
        "desc": desc, "status_type": status_type, "tactic_tag": tactic_tag,
        "signal_mode": "trend", "should_alert": should_alert,
    })
    return res


def plot_advanced_candlestick(df, title, baseline_price=None):
    """完整計算指標後，僅截取最新 40 根畫圖，布林通道滿版無留白"""
    df_plot = df.tail(40).copy()
    if df_plot.empty:
        return go.Figure()

    latest_rsi = df_plot["RSI"].iloc[-1]
    rsi_str = f"{latest_rsi:.1f}" if not np.isnan(latest_rsi) else "--"
    fig = go.Figure()

    fig.add_trace(go.Candlestick(
        x=df_plot["Open time"], open=df_plot["Open"], high=df_plot["High"],
        low=df_plot["Low"], close=df_plot["Close"],
        name="K線", increasing_line_color="#26A69A", decreasing_line_color="#EF5350"
    ))
    fig.add_trace(go.Scatter(x=df_plot["Open time"], y=df_plot["MA5"], line=dict(color="#FBC02D", width=1.5), name="5MA"))
    fig.add_trace(go.Scatter(x=df_plot["Open time"], y=df_plot["MA20"], line=dict(color="#AB47BC", width=1.5), name="20MA"))
    fig.add_trace(go.Scatter(x=df_plot["Open time"], y=df_plot["BB_Upper"], line=dict(color="rgba(33, 150, 243, 0.65)", width=1.5, dash="dot"), name="布林上軌"))
    fig.add_trace(go.Scatter(x=df_plot["Open time"], y=df_plot["BB_Lower"], line=dict(color="rgba(33, 150, 243, 0.65)", width=1.5, dash="dot"), name="布林下軌"))

    if baseline_price is not None and baseline_price > 0:
        fig.add_hline(
            y=baseline_price, line_dash="dash", line_color="#FFFFFF", line_width=2, opacity=0.9,
            annotation_text="🎯 基準水面", annotation_position="bottom right",
            annotation_font_color="white", annotation_font_size=12
        )

    fig.update_layout(
        title=dict(text=f"{title} | RSI: {rsi_str}", font=dict(size=15)),
        xaxis_rangeslider_visible=False,
        margin=dict(l=10, r=10, t=38, b=55),
        height=400, template="plotly_dark", uirevision="constant",
        legend=dict(orientation="h", yanchor="top", y=-0.22, xanchor="center", x=0.5)
    )
    fig.update_xaxes(tickangle=-45, nticks=8, tickformat="%H:%M")
    return fig


# --- 側邊欄：Google Sheets 5U 賠率收集器 (獨立於重整區塊外，打字不被清空) + V3 狙擊參數 ---
with st.sidebar:
    st.header("📝 5U 真實賠率收集器")
    ws, gs_msg = get_gsheet_worksheet()
    if ws is not None:
        st.caption(f"🟢 {gs_msg}")
    else:
        st.caption(f"🟡 {gs_msg}（先暫存本頁，可下載 CSV）")

    with st.form("ev_collector_form", clear_on_submit=True):
        st.markdown("看一眼幣安 App，填單邊或雙邊後按 **Enter** 秒存（鎖單填 `0`）：")
        c_up, c_dn = st.columns(2)
        with c_up:
            up_input = st.text_input("🟢 買漲(UP) 5U拿回", placeholder="例: 6.70")
        with c_dn:
            dn_input = st.text_input("🔴 買跌(DOWN) 5U拿回", placeholder="選填或0")
        submitted = st.form_submit_button("💾 立即寫入紀錄 (Enter)", use_container_width=True)

    if submitted:
        up_str = up_input.strip()
        dn_str = dn_input.strip()
        if not up_str and not dn_str:
            st.warning("請至少輸入買漲或買跌其中一側的金額。")
        else:
            try:
                up_val = float(up_str) if up_str else None
                dn_val = float(dn_str) if dn_str else None

                snap = st.session_state.latest_snapshot
                now_tw, _, live_sec_left, _, round_open_tw, _, slot_label = get_taiwan_time_info()
                sym_now = st.session_state.selected_symbol
                live_mid = get_mid_price(sym_now)

                if snap is not None and snap.get("symbol") == sym_now:
                    base_p = snap["base_price"]
                    mid_p = live_mid if live_mid is not None else snap["mid_price"]
                    diff_p = mid_p - base_p
                    atr_v = snap["atr_1m"] if snap["atr_1m"] > 0 else 1.0
                    moat_v = abs(diff_p) / atr_v
                    sig_v = snap["sigma_1m"] if snap["sigma_1m"] > 0 else 0.0005
                    z_v = diff_p / (base_p * sig_v * math.sqrt(max(live_sec_left, 1) / 60.0)) if base_p > 0 else 0.0
                    p_est_pct = round(snap["p_est"] * 100.0, 2)
                    tgt_5u = round(snap["target_5u"], 2)
                    s_dir = snap["direction"]
                    t_tag = snap["tactic_tag"]
                    vol_r = round(snap["vol_ratio"], 2)
                    low_vol_str = "是" if snap["is_low_vol"] else "否"
                    taker_pct = round(snap["taker_buy_ratio"] * 100.0, 1)
                    rsi_v = round(snap["rsi_1m"], 1)
                    t_1h = snap["trend_1h"]
                else:
                    base_p, mid_p, diff_p, atr_v, moat_v, sig_v, z_v = 0, live_mid or 0, 0, 0, 0, 0, 0
                    p_est_pct, tgt_5u, s_dir, t_tag, vol_r, low_vol_str, taker_pct, rsi_v, t_1h = (
                        50.0, 10.5, "N/A", "N/A", 1.0, "否", 50.0, 50.0, "N/A"
                    )

                notes = []
                if up_val == 0:
                    notes.append("UP鎖單")
                if dn_val == 0:
                    notes.append("DOWN鎖單")
                vig_pct = ""
                if up_val and dn_val and up_val > 0 and dn_val > 0:
                    vig_pct = round(((5.0 / up_val) + (5.0 / dn_val) - 1.0) * 100.0, 2)

                time_label = f"{now_tw.strftime('%Y-%m-%d')} ({WEEKDAY_MAP[now_tw.weekday()]}) {now_tw.strftime('%H:%M:%S')}"
                row_data = [
                    time_label,
                    round_open_tw.strftime("%Y-%m-%d %H:%M"),
                    sym_now,
                    live_sec_left,
                    slot_label,
                    round(base_p, 4),
                    round(mid_p, 4),
                    round(diff_p, 4),
                    round(atr_v, 4),
                    round(moat_v, 2),
                    round(sig_v, 6),
                    round(z_v, 2),
                    p_est_pct,
                    tgt_5u,
                    s_dir,
                    t_tag,
                    vol_r,
                    low_vol_str,
                    taker_pct,
                    rsi_v,
                    t_1h,
                    up_val if up_val is not None else "",
                    dn_val if dn_val is not None else "",
                    vig_pct,
                    " / ".join(notes),
                ]

                st.session_state.local_records.append(dict(zip(SHEET_HEADERS, row_data)))

                if ws is not None:
                    try:
                        ws.append_row(row_data, value_input_option="USER_ENTERED")
                        st.success(f"✅ 已永久寫入 Google Sheets！({sym_now.replace('USDT','')} 剩 {live_sec_left}s)")
                    except Exception as e:
                        st.error(f"寫入失敗，已改存本頁暫存：{e}")
                else:
                    st.success(f"✅ 已暫存 ({len(st.session_state.local_records)} 筆)，可點下方下載 CSV。")
            except ValueError:
                st.error("⚠️ 請輸入純數字（例如 6.70 或 0）")

    if st.session_state.local_records:
        df_backup = pd.DataFrame(st.session_state.local_records)
        st.download_button(
            label=f"⬇️ 下載本次暫存紀錄 ({len(df_backup)} 筆 CSV)",
            data=df_backup.to_csv(index=False).encode("utf-8-sig"),
            file_name="binance_5m_v3_backup.csv",
            mime="text/csv",
            use_container_width=True,
        )

    st.divider()
    st.header("⚙️ V3 狙擊參數")
    enable_sound = st.checkbox("🔊 啟用提示音效", value=True)
    only_low_vol_alert = st.checkbox("🌟 順勢僅「縮量安靜 (<0.5x)」響鈴", value=True)
    mute_trap_hours = st.checkbox("🛡️ 午間陷阱 (12-15點) 順勢靜音", value=True)
    rsi_long_th = st.slider("⚡ 搶反彈 (買漲) RSI 基準門檻", min_value=15, max_value=45, value=30, step=1)
    rsi_short_th = st.slider("⚡ 摸頭空 (買跌) RSI 基準門檻", min_value=55, max_value=85, value=70, step=1)
    st.info(
        "🎯 **V3 實證狙擊鐵律：**\n\n"
        "• **純雙核心**：專注 BTC / ETH（已剔除負 EV 的 BNB）。\n"
        "• **順勢甜蜜帶**：剩餘 `150s~16s`、縮量安靜、勝率 `72%~88%`。\n"
        "• **過熱／爆量／>88%**：一律自動靜音防踩雷。"
    )


# --- 主介面頂部：純點擊雙核心按鈕 (絕不彈出鍵盤) + 即時報價 + 搶反彈開關 ---
st.title("🎯 Binance Predict 5分鐘實證狙擊系統 V3")


def select_symbol(sym):
    st.session_state.selected_symbol = sym


col_btns, col_price, col_toggle = st.columns([1.6, 1.6, 1.9])

with col_btns:
    st.markdown("<div style='font-size:14px; color:#AAAAAA; margin-bottom:4px;'>主戰場雙核心（純點擊切換）</div>", unsafe_allow_html=True)
    b1, b2 = st.columns(2)
    for col_b, sym in zip([b1, b2], AVAILABLE_SYMBOLS):
        with col_b:
            st.markdown('<div class="symbol-btn-wrap">', unsafe_allow_html=True)
            is_active = st.session_state.selected_symbol == sym
            st.button(
                sym.replace("USDT", ""),
                key=f"sym_btn_{sym}",
                type="primary" if is_active else "secondary",
                use_container_width=True,
                on_click=select_symbol,
                args=(sym,),
            )
            st.markdown("</div>", unsafe_allow_html=True)

with col_toggle:
    st.write("")
    enable_rebound = st.checkbox("☑️ 啟用【極端搶反彈】偵測 (≥90s & ≤1.8x ATR)", value=True)


@st.fragment(run_every=1)
def show_realtime_price(selected_symbol):
    _, _, sec_left, _, _, _, _ = get_taiwan_time_info()
    countdown = f"{sec_left // 60:02d}:{sec_left % 60:02d}"
    mid_price = get_mid_price(selected_symbol)
    if mid_price is not None:
        st.metric(
            label=f"💰 {selected_symbol} 中間價 (Mid) ｜ ⏳ 倒數: {countdown}",
            value=f"{mid_price:,.2f}",
        )
    else:
        st.metric(label=f"💰 {selected_symbol} 連線中...", value="--")


with col_price:
    show_realtime_price(st.session_state.selected_symbol)


# --- 主儀表板與三時框圖表 (每 10 秒自動更新) ---
@st.fragment(run_every=10)
def show_prediction_dashboard(
    selected_symbol, enable_rebound_flag, r_long, r_short,
    sound_on, only_low_vol, mute_trap
):
    _, epoch_5m, sec_left, _, _, is_trap_hour, slot_label = get_taiwan_time_info()
    other_symbol = [s for s in AVAILABLE_SYMBOLS if s != selected_symbol][0]

    def fetch_main_bundle(msym):
        d1_raw = fetch_1m_klines(msym, epoch_5m, limit=1000)
        d1 = add_technical_indicators(d1_raw)
        d5 = add_technical_indicators(resample_klines(d1_raw, "5min"))
        d15 = add_technical_indicators(resample_klines(d1_raw, "15min"))
        mid_p = get_mid_price(msym)
        t1h = fetch_1h_trend(msym, epoch_5m)
        return d1, d5, d15, mid_p, t1h

    def fetch_radar_bundle(osym):
        d1_raw = fetch_1m_klines(osym, epoch_5m, limit=200)
        d1 = add_technical_indicators(d1_raw)
        d5 = add_technical_indicators(resample_klines(d1_raw, "5min"))
        mid_p = get_mid_price(osym)
        t1h = fetch_1h_trend(osym, epoch_5m)
        ev_r = evaluate_market_v3(
            osym, d1, d5, mid_p, t1h, enable_rebound_flag, r_long, r_short, only_low_vol
        )
        short_name = osym.replace("USDT", "")
        if ev_r["is_locked"]:
            return f"[{short_name}] 🔒 尾盤靜音", None, ev_r["signal_mode"]
        if ev_r["should_alert"]:
            if ev_r["signal_mode"] == "rebound":
                act = "💥反彈UP" if ev_r["direction"] == "UP" else "💥摸空DOWN"
                txt = f"**[{short_name}] {act} {ev_r['p_est']*100:.0f}% (5U≥{ev_r['target_5u']:.2f})**"
                return txt, "radar_rebound", "rebound"
            else:
                act = "🟢漲" if ev_r["direction"] == "UP" else "🔴跌"
                tag = "🌟縮量" if ev_r["is_low_vol"] else "🔥順勢"
                txt = f"**[{short_name}] {tag}{act} {ev_r['p_est']*100:.0f}% (5U≥{ev_r['target_5u']:.2f})**"
                return txt, "radar_trend", "trend"
        elif ev_r["p_est"] > 0.88:
            return f"[{short_name}] ⚠️低賠率(>88%)", None, "trend"
        elif ev_r["p_est"] >= 0.72:
            return f"[{short_name}] 🟡觀察 ({ev_r['p_est']*100:.0f}%)", None, "trend"
        return f"[{short_name}] ⚪觀望", None, "trend"

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f_main = executor.submit(fetch_main_bundle, selected_symbol)
        f_radar = executor.submit(fetch_radar_bundle, other_symbol)
        df_1m, df_5m, df_15m, mid_p, trend_1h = f_main.result()
        radar_text, radar_sound, radar_mode = f_radar.result()

    ev = evaluate_market_v3(
        selected_symbol, df_1m, df_5m, mid_p, trend_1h,
        enable_rebound_flag, r_long, r_short, only_low_vol
    )
    st.session_state.latest_snapshot = ev.copy()

    # 午間陷阱時段警示橫幅
    if is_trap_hour:
        st.warning("⚠️ **目前為午間陷阱時段 (12:00 ~ 15:00)**：實測順勢交易為顯著負期望值，建議觀望或僅留意高賠率極端搶反彈。")

    # 1. 背景雷達列 ＋ 當前時段屬性
    st.markdown(
        f"<div style='text-align:left; font-size:16px; margin-top:4px; margin-bottom:4px; color:#CCCCCC;'>"
        f"📡 <b>背景雷達：</b> {radar_text} &nbsp;｜&nbsp; 🕒 <b>時段：</b> {slot_label}</div>",
        unsafe_allow_html=True,
    )

    # 2. 核心預估勝率 ＋ 5U 行動線 ＋ 量能/1H 標籤
    p_pct = ev["p_est"] * 100.0
    score_color = "#4FC3F7" if ev["should_alert"] else ("#FFD54F" if p_pct >= 72 else "#9E9E9E")
    trend_badge_color = "#26A69A" if trend_1h == "多頭" else ("#EF5350" if trend_1h == "空頭" else "#B0BEC5")
    dir_label = "買漲 UP" if ev["direction"] == "UP" else ("買跌 DOWN" if ev["direction"] == "DOWN" else "觀望")
    vol_badge = f"🌟縮量({ev['vol_ratio']:.2f}x)" if ev["is_low_vol"] else f"量能({ev['vol_ratio']:.2f}x)"

    st.markdown(
        f"<div style='text-align:left; font-size:21px; font-weight:bold; margin-top:0px; margin-bottom:10px; line-height:1.5;'>"
        f"<span style='color:{score_color};'>🎯 V3 預估勝率：{p_pct:.1f}% ({dir_label})</span>"
        f" &nbsp;｜&nbsp; "
        f"<span style='color:#FFFFFF;'>💵 5U 回本線：<span style='color:#FF8A65;'>{ev['breakeven_5u']:.2f} U</span></span>"
        f" &nbsp;｜&nbsp; "
        f"<span style='color:#81C784;'>✅ 建議出手(+5%邊際)：≥ {ev['target_5u']:.2f} U</span>"
        f" &nbsp;｜&nbsp; "
        f"<span style='font-size:15px; color:#E0E0E0;'>[{vol_badge}]</span> "
        f"<span style='font-size:15px; color:{trend_badge_color};'>[1H: {trend_1h}]</span>"
        f"</div>",
        unsafe_allow_html=True,
    )

    # 3. 戰術訊號橫幅
    if ev["status_type"] == "success":
        st.success(ev["desc"])
    elif ev["status_type"] == "error":
        st.error(ev["desc"])
    elif ev["status_type"] == "warning":
        st.warning(ev["desc"])
    else:
        st.info(ev["desc"])

    # 4. 音效觸發控制（嚴格遵守：最後 15 秒全面靜音 + 午間陷阱順勢靜音）
    if sound_on and (not ev["is_locked"]) and sec_left > 15:
        main_muted_by_trap = (is_trap_hour and mute_trap and ev["signal_mode"] == "trend")
        radar_muted_by_trap = (is_trap_hour and mute_trap and radar_mode == "trend")

        if ev["should_alert"] and not main_muted_by_trap:
            play_alert_sound("main_rebound" if ev["signal_mode"] == "rebound" else "main_trend")
        elif radar_sound is not None and not radar_muted_by_trap:
            play_alert_sound(radar_sound)

    # 5. 三時框 K 線圖（均截取最新 40 根完整指標資料）
    col1, col2, col3 = st.columns(3)
    chart_config = {"displayModeBar": False, "scrollZoom": True}
    with col1:
        if not df_1m.empty:
            st.plotly_chart(
                plot_advanced_candlestick(df_1m, "1 分鐘線 (過熱邊界與反彈點)", baseline_price=ev["base_price"]),
                use_container_width=True, config=chart_config, key="c1",
            )
    with col2:
        if not df_5m.empty:
            st.plotly_chart(
                plot_advanced_candlestick(df_5m, "5 分鐘線 (結算基準水面)", baseline_price=ev["base_price"]),
                use_container_width=True, config=chart_config, key="c5",
            )
    with col3:
        if not df_15m.empty:
            st.plotly_chart(
                plot_advanced_candlestick(df_15m, "15 分鐘線 (短波段參考)", baseline_price=ev["base_price"]),
                use_container_width=True, config=chart_config, key="c15",
            )

    st.caption("💡 實戰決策口訣：只有響鈴訊號出現，且幣安 App 顯示的「5U 可拿回金額」大於等於上方綠色【建議出手門檻】時才進場；低於門檻一律跳過。")


show_prediction_dashboard(
    st.session_state.selected_symbol,
    enable_rebound,
    rsi_long_th,
    rsi_short_th,
    enable_sound,
    only_low_vol_alert,
    mute_trap_hours,
)

# --- 網頁底部：V3 實證邏輯說明 ---
st.divider()
with st.expander("📖 V3 終極實證版量化規則說明 (點擊展開)"):
    st.markdown("""
    ### 1. 為什麼只留 BTC / ETH 雙核心？
    * 經過去 14 天超過 41,000 筆真實 1 分鐘 K 線驗證，BTC 與 ETH 在縮量順勢與回本線過濾下皆呈穩定正期望值，而 BNB 在順勢與搶反彈兩項皆為顯著負期望值，故徹底剔除以聚焦火力。

    ### 2. V3 順勢甜蜜射程（全部經大樣本實測校準）
    * **時間窗鎖死**：僅於 **剩餘 `150 秒 ~ 16 秒`** 開啟順勢狙擊；最後 **15 秒** 主戰場與雷達全面靜音。
    * **量能徹底反轉**：縮量安靜 (`< 0.5x`) 為最強護城河（實測超額勝率最高），爆量 (`>= 1.5x`) 視為波動放大風險並禁買。
    * **過熱地雷封殺**：1 分鐘 RSI `> 75 / < 25` 或碰觸順向布林邊界，實測均呈現顯著負超額勝率，強制靜音禁買。
    * **勝率上限 `88%` 保護**：實測預估勝率超過 `88%` 時，造市商賠率已壓縮至約 `1.03x` 成為負 EV 黑洞，故系統自動轉為黃色警戒並靜音。

    ### 3. 幣種化極端搶反彈
    * 必須同時滿足 **剩餘 `≥ 90 秒`**、**距基準線 `≤ 1.8x ATR`**、且通過 **1H 大趨勢防接刀**（逆 1H 趨勢時 RSI 門檻自動收緊 6 點）。
    * 5U 建議門檻直接依各幣種真實回測勝率換算（BTC 約 `≥ 12.99 U`，ETH 約 `≥ 15.41 U`；逆 1H 趨勢會再自動墊高門檻）。
    """)
