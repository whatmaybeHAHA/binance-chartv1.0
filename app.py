import math
import re
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

st.set_page_config(layout="wide", page_title="幣安 5 分鐘實證狙擊系統 V3.1 (Parkinson-10)")

# --- 全局常數（純 BTC / ETH 雙核心） ---
AVAILABLE_SYMBOLS = ["BTCUSDT", "ETHUSDT"]
WEEKDAY_MAP = ["一", "二", "三", "四", "五", "六", "日"]

# 依據 14 天真實回測校準之各幣種搶反彈基準勝率
REBOUND_EMPIRICAL_WR = {
    "BTCUSDT": 0.404,
    "ETHUSDT": 0.341,
}

# 依據 93 筆實戰試算表反推之幣安造市商定價參數 (Parkinson-10 = 1.001x)
BINANCE_BASE_FEE = 0.02
BINANCE_SPREAD = 0.005

# 維持與既有 Google Sheets 完全相容的 25 欄結構
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
if "latest_context" not in st.session_state:
    st.session_state.latest_context = None
if "frozen_snapshot" not in st.session_state:
    st.session_state.frozen_snapshot = None
if "last_submit_feedback" not in st.session_state:
    st.session_state.last_submit_feedback = None
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
.freeze-btn-wrap button {
    height: 46px !important;
    font-size: 16px !important;
    font-weight: bold !important;
    border-radius: 8px !important;
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
    session.headers.update({"User-Agent": "BinancePredictV31/3.1.1"})
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


def parse_quote_inputs(up_raw, dn_raw):
    """
    解析報價輸入，支援：
    1) 第一格「單格雙邊快填」：以空白、全形空白、逗號或斜線分隔（例：'6.14 23.33' -> UP=6.14, DOWN=23.33；'- 23.33' -> 僅 DOWN）
    2) 傳統單邊或雙格分別輸入（第一格 UP、第二格 DOWN）
    """
    up_tokens = [t for t in re.split(r"[\s\u3000,/，]+", up_raw.strip()) if t]
    dn_tokens = [t for t in re.split(r"[\s\u3000,/，]+", dn_raw.strip()) if t]

    if not up_tokens and not dn_tokens:
        return None, None, "請至少輸入買漲或買跌其中一側的金額。"

    def _to_float_or_none(tok):
        if tok == "-":
            return None
        return float(tok)

    # 情況 A：第一格輸入兩個數值（雙邊快填：左 UP、右 DOWN）
    if len(up_tokens) == 2 and len(dn_tokens) == 0:
        try:
            u = _to_float_or_none(up_tokens[0])
            d = _to_float_or_none(up_tokens[1])
            if u is None and d is None:
                return None, None, "請輸入有效數字（例：6.14 23.33）。"
            return u, d, None
        except ValueError:
            return None, None, "雙邊快填格式錯誤：請輸入兩個純數字並以空白分隔（例：6.14 23.33 或 0 490）。"

    # 情況 B：第二格不小心輸入兩個數值
    if len(up_tokens) == 0 and len(dn_tokens) == 2:
        try:
            u = _to_float_or_none(dn_tokens[0])
            d = _to_float_or_none(dn_tokens[1])
            if u is None and d is None:
                return None, None, "請輸入有效數字（例：6.14 23.33）。"
            return u, d, None
        except ValueError:
            return None, None, "雙邊快填格式錯誤：請輸入兩個純數字並以空白分隔（例：6.14 23.33）。"

    # 情況 C：單格超過 2 個數字，或雙邊快填又同時填了第二格
    if len(up_tokens) > 1 or len(dn_tokens) > 1:
        return None, None, "格式衝突：雙邊快填請只在第一格輸入兩個數字（例：6.14 23.33），勿同時填寫第二格。"

    # 情況 D：標準單格單值（可填單邊或兩格各填一邊）
    try:
        u = _to_float_or_none(up_tokens[0]) if up_tokens else None
        d = _to_float_or_none(dn_tokens[0]) if dn_tokens else None
        if u is None and d is None:
            return None, None, "請至少輸入一個有效數字。"
        return u, d, None
    except ValueError:
        return None, None, "請輸入純數字（例：6.70、0，或雙邊快填 6.14 23.33）。"


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
    """每 5 分鐘更新一次 1H 大趨勢（供搶反彈防接刀與寫入 Google Sheets）"""
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
    rsi_fallback = pd.Series(np.where(avg_gain > 0, 100.0, 50.0), index=df.index)
    df["RSI"] = (100.0 - (100.0 / (1.0 + rs))).fillna(rsi_fallback)

    high_low = df["High"] - df["Low"]
    high_close = (df["High"] - df["Close"].shift()).abs()
    low_close = (df["Low"] - df["Close"].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df["ATR"] = tr.rolling(window=14).mean()

    # V3.1 核心：Parkinson-10 高低點波動率（實測與幣安造市商定價比值 1.001x）
    hl_ratio = (df["High"] / df["Low"]).clip(lower=1.0)
    hl_sq = (np.log(hl_ratio) ** 2) / (4.0 * math.log(2.0))
    df["Sig_Park10"] = np.sqrt(hl_sq.ewm(span=10, adjust=False).mean())
    return df


# --- 幣安造市商報價預估與鎖單紅線引擎 ---
def estimate_binance_quote(price_diff, base_price, sig_park10, sec_left, side):
    """
    依據 Parkinson-10 + 實測 2% 基礎費率 + 0.5% 單邊點差，推算幣安此刻預估 5U 報價與鎖單紅線
    """
    if base_price <= 0:
        return 9.80, False, 0.0
    t_min = max(sec_left, 1) / 60.0
    sig = max(sig_park10, 1e-5)
    z_up = price_diff / (base_price * sig * math.sqrt(t_min))
    p_up = 0.5 * (1.0 + math.erf(z_up / math.sqrt(2.0)))

    if side == "NEUTRAL":
        return 9.80, False, z_up

    p_side = p_up if side == "UP" else (1.0 - p_up)
    is_lead = (side == "UP" and price_diff > 0) or (side == "DOWN" and price_diff < 0)
    p_ask = p_side + BINANCE_SPREAD

    # 鎖單紅線：領先側預估報價跌破 ~5.03U (p_ask >= 0.975) 或最後 75s 內 |Z_Park10| >= 2.1
    is_lock_zone = is_lead and (p_ask >= 0.975 or (sec_left <= 75 and abs(z_up) >= 2.1))
    p_ask_clamped = float(np.clip(p_ask, 0.01, 0.972))
    est_5u = min((5.0 * (1.0 - BINANCE_BASE_FEE)) / p_ask_clamped, 490.0)
    return round(est_5u, 2), is_lock_zone, z_up


# --- V3.1 核心量化評分（純數值版，儀表板、凍結按鈕與表單送出共用） ---
def evaluate_v31_from_scalars(
    symbol, base_price, current_price, atr_1m, sig_park10,
    vol_ratio, taker_buy_ratio, rsi_1m, bb_upper, bb_lower,
    trend_1h, sec_left, round_5m_open_str, is_trap_hour, slot_label,
    enable_rebound, rsi_long_th, rsi_short_th, only_low_vol_alert
):
    countdown = f"{sec_left // 60:02d}:{sec_left % 60:02d}"
    price_diff = current_price - base_price
    atr_safe = atr_1m if atr_1m > 0 else max(current_price * 0.0005, 0.01)
    moat_ratio = abs(price_diff) / atr_safe

    sig_safe = sig_park10 if sig_park10 > 0 else max((atr_safe / max(current_price, 1.0)) * 0.7, 1e-5)
    t_min = max(sec_left, 1) / 60.0
    z_score = price_diff / (base_price * sig_safe * math.sqrt(t_min)) if base_price > 0 else 0.0
    p_base_lead = 0.5 * (1.0 + math.erf(abs(z_score) / math.sqrt(2.0)))
    is_low_vol = vol_ratio < 0.5

    # 1H 防接刀濾網（僅作用於搶反彈）
    eff_rsi_long = rsi_long_th - 6 if trend_1h == "空頭" else rsi_long_th
    eff_rsi_short = rsi_short_th + 6 if trend_1h == "多頭" else rsi_short_th

    res = {
        "symbol": symbol, "p_est": 0.50, "p_base": p_base_lead, "direction": "NEUTRAL",
        "desc": "", "status_type": "info", "tactic_tag": "觀望",
        "moat_ratio": moat_ratio, "base_price": base_price, "mid_price": current_price,
        "price_diff": price_diff, "atr_1m": atr_safe, "sigma_1m": sig_safe, "z_score": z_score,
        "vol_ratio": vol_ratio, "is_low_vol": is_low_vol, "taker_buy_ratio": taker_buy_ratio,
        "rsi_1m": rsi_1m, "breakeven_5u": 10.0, "target_5u": 10.5, "est_binance_5u": 9.80,
        "is_lock_zone": False, "signal_mode": "trend", "should_alert": False, "is_locked": False,
        "sec_left": sec_left, "countdown": countdown,
        "round_5m_open_str": round_5m_open_str, "trend_1h": trend_1h,
        "is_trap_hour": is_trap_hour, "slot_label": slot_label,
    }

    # 1. 最後 15 秒全面靜音硬鎖
    if sec_left <= 15:
        lead_dir = "UP" if price_diff >= 0 else "DOWN"
        p_est = float(np.clip(p_base_lead, 0.05, 0.96))
        est_5u, lock_z, _ = estimate_binance_quote(price_diff, base_price, sig_safe, sec_left, lead_dir)
        res.update({
            "p_est": p_est, "direction": lead_dir,
            "breakeven_5u": 5.0 / p_est, "target_5u": 5.25 / p_est,
            "est_binance_5u": est_5u, "is_lock_zone": lock_z,
            "desc": f"🔒 【尾盤靜音鎖 {countdown}】 剩餘不足 15 秒，主戰場與雷達全面停止提示",
            "status_type": "info", "tactic_tag": "尾盤靜音鎖",
            "signal_mode": "locked", "should_alert": False, "is_locked": True,
        })
        return res

    # 2. 戰術 B：幣種化極端搶反彈（硬防線：>=90s & <=1.8x ATR & 1H防接刀 + 12U~30U黃金報價區提示）
    if enable_rebound and sec_left >= 90 and moat_ratio <= 1.8:
        emp_wr = REBOUND_EMPIRICAL_WR.get(symbol, 0.35)
        if current_price <= bb_lower and rsi_1m <= eff_rsi_long:
            p_est = max(emp_wr - 0.03, 0.25) if trend_1h == "空頭" else emp_wr
            tgt = 5.25 / p_est
            est_5u, _, _ = estimate_binance_quote(price_diff, base_price, sig_safe, sec_left, "UP")
            warn_tag = " ｜ 🐻 1H空頭警戒(已收緊門檻)" if trend_1h == "空頭" else ""
            res.update({
                "p_est": p_est, "direction": "UP",
                "breakeven_5u": 5.0 / p_est, "target_5u": tgt, "est_binance_5u": est_5u,
                "desc": (
                    f"⚡ 【倒數 {countdown} 極端搶反彈】 買漲 (UP) ｜ 貫穿下軌 + RSI {rsi_1m:.1f} (門檻≤{eff_rsi_long})"
                    f" ｜ 🎯 黃金報價區：App {tgt:.2f} ~ 30.00 U（>30U樂透區跳過）{warn_tag}"
                ),
                "status_type": "success", "tactic_tag": "極端搶反彈",
                "signal_mode": "rebound", "should_alert": True,
            })
            return res

        if current_price >= bb_upper and rsi_1m >= eff_rsi_short:
            p_est = max(emp_wr - 0.03, 0.25) if trend_1h == "多頭" else emp_wr
            tgt = 5.25 / p_est
            est_5u, _, _ = estimate_binance_quote(price_diff, base_price, sig_safe, sec_left, "DOWN")
            warn_tag = " ｜ 🐂 1H多頭警戒(已收緊門檻)" if trend_1h == "多頭" else ""
            res.update({
                "p_est": p_est, "direction": "DOWN",
                "breakeven_5u": 5.0 / p_est, "target_5u": tgt, "est_binance_5u": est_5u,
                "desc": (
                    f"⚡ 【倒數 {countdown} 極端摸頭空】 買跌 (DOWN) ｜ 貫穿上軌 + RSI {rsi_1m:.1f} (門檻≥{eff_rsi_short})"
                    f" ｜ 🎯 黃金報價區：App {tgt:.2f} ~ 30.00 U（>30U樂透區跳過）{warn_tag}"
                ),
                "status_type": "error", "tactic_tag": "極端摸頭空",
                "signal_mode": "rebound", "should_alert": True,
            })
            return res

    # 3. 死亡糾纏區（價差 < 0.2x ATR）
    if abs(price_diff) < (0.2 * atr_safe):
        res.update({
            "p_est": 0.50, "direction": "NEUTRAL",
            "breakeven_5u": 10.0, "target_5u": 10.5, "est_binance_5u": 9.80,
            "desc": f"⚖️ 【倒數 {countdown}】 死亡糾纏區 ｜ 貼近基準水面 ({moat_ratio:.2f}x ATR)，強制觀望",
            "status_type": "info", "tactic_tag": "死亡糾纏區",
            "signal_mode": "trend", "should_alert": False,
        })
        return res

    # 4. V3.1 順勢引擎：Parkinson-10 + 零中心化微調
    direction = "UP" if price_diff >= 0 else "DOWN"
    delta_p = 0.0

    if vol_ratio < 0.5:
        delta_p += 0.020
        vol_tag = f"🌟 縮量安靜 ({vol_ratio:.2f}x)"
    elif vol_ratio < 1.5:
        vol_tag = f"📊 量能正常 ({vol_ratio:.2f}x)"
    else:
        delta_p -= 0.015
        vol_tag = f"⚠️ 爆量波動風險 ({vol_ratio:.2f}x)"

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

    if vol_ratio >= 1.5:
        danger_reasons.append(f"爆量({vol_ratio:.1f}x)")

    delta_p = float(np.clip(delta_p, -0.075, 0.035))
    p_est = float(np.clip(p_base_lead + delta_p, 0.05, 0.96))
    breakeven_5u = 5.0 / p_est
    target_5u = 5.25 / p_est
    est_5u, is_lock_zone, _ = estimate_binance_quote(price_diff, base_price, sig_safe, sec_left, direction)
    action_cn = "買漲 (UP)" if direction == "UP" else "買跌 (DOWN)"

    is_danger = len(danger_reasons) > 0
    in_time_window = (15 < sec_left <= 150)

    if is_danger:
        status_type = "warning"
        tactic_tag = "地雷禁買"
        should_alert = False
        desc = f"🚫 【倒數 {countdown} 地雷禁買】 {action_cn} ｜ 觸發高風險因子：{' + '.join(danger_reasons)}，強制靜音觀望"
    elif is_lock_zone:
        status_type = "warning"
        tactic_tag = "即將鎖單紅線"
        should_alert = False
        desc = (
            f"🔒 【倒數 {countdown} 莊家鎖單紅線】 {action_cn} ｜ 預估幣安報價已壓至 {est_5u:.2f} U "
            f"(|Z|={abs(z_score):.2f})，極可能已鎖單或僅剩超低賠率，系統強制靜音"
        )
    elif not in_time_window:
        status_type = "info"
        tactic_tag = "早盤等待期"
        should_alert = False
        desc = f"⏳ 【倒數 {countdown} 早盤等待】 {action_cn} ｜ 尚餘超過 150 秒 (護城河 {moat_ratio:.2f}x ｜ {vol_tag})，靜待狙擊窗開啟"
    elif p_est > 0.88:
        status_type = "warning"
        tactic_tag = "低賠率黑洞(>88%)"
        should_alert = False
        desc = (
            f"⚠️ 【倒數 {countdown} 低賠率警戒區】 {action_cn} 勝率 {p_est*100:.1f}% (>88%) ｜ "
            f"預估報價僅 {est_5u:.2f} U 易倒吐，系統自動靜音 (除非 App 出現溢價 ≥ {target_5u:.2f} U)"
        )
    elif (0.72 <= p_est <= 0.88) and (moat_ratio >= 0.5):
        if is_low_vol:
            status_type = "success" if direction == "UP" else "error"
            tactic_tag = "縮量黃金狙擊"
            should_alert = True
            desc = (
                f"🌟 【倒數 {countdown} 縮量黃金狙擊】 {action_cn} ｜ 護城河 {moat_ratio:.2f}x ｜ "
                f"{vol_tag} ｜ 預估 App 報價約 {est_5u:.2f} U"
            )
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
        "est_binance_5u": est_5u, "is_lock_zone": is_lock_zone,
        "desc": desc, "status_type": status_type, "tactic_tag": tactic_tag,
        "signal_mode": "trend", "should_alert": should_alert,
    })
    return res


def evaluate_market_v31(
    symbol, df_1m, df_5m, mid_price, trend_1h,
    enable_rebound, rsi_long_th, rsi_short_th, only_low_vol_alert
):
    now_tw, _, sec_left, sec_elapsed_1m, round_5m_open_tw, is_trap_hour, slot_label = get_taiwan_time_info()
    round_str = round_5m_open_tw.strftime("%Y-%m-%d %H:%M")

    if df_1m.empty or df_5m.empty or len(df_1m) < 25:
        ev = evaluate_v31_from_scalars(
            symbol, 0.0, 0.0, 1.0, 0.0005, 1.0, 0.5, 50.0, 0.0, 0.0,
            trend_1h, sec_left, round_str, is_trap_hour, slot_label,
            enable_rebound, rsi_long_th, rsi_short_th, only_low_vol_alert
        )
        ev["desc"] = "資料載入中..."
        ev["tactic_tag"] = "載入中"
        return ev, None

    latest_1m = df_1m.iloc[-1]
    prev_1m = df_1m.iloc[-2]

    match_5m = df_5m[df_5m["Open time"] == pd.Timestamp(round_5m_open_tw)]
    base_price = float(match_5m["Open"].iloc[0]) if not match_5m.empty else float(df_5m.iloc[-1]["Open"])
    current_price = float(mid_price) if mid_price is not None else float(latest_1m["Close"])

    atr_val = prev_1m["ATR"] if not np.isnan(prev_1m["ATR"]) and prev_1m["ATR"] > 0 else latest_1m["ATR"]
    atr_1m = float(atr_val) if not np.isnan(atr_val) and atr_val > 0 else max(current_price * 0.0005, 0.01)

    park_val = prev_1m["Sig_Park10"] if not np.isnan(prev_1m["Sig_Park10"]) and prev_1m["Sig_Park10"] > 0 else latest_1m["Sig_Park10"]
    sig_park10 = float(park_val) if not np.isnan(park_val) and park_val > 0 else (atr_1m / max(current_price, 1.0)) * 0.7

    vol_ma = float(prev_1m["Vol_MA20"]) if not np.isnan(prev_1m["Vol_MA20"]) and prev_1m["Vol_MA20"] > 0 else 1.0
    if sec_elapsed_1m >= 12:
        proj_vol = float(latest_1m["Volume"]) * (60.0 / sec_elapsed_1m)
        vol_ratio = proj_vol / vol_ma
        active_bar = latest_1m
    else:
        vol_ratio = float(prev_1m["Volume"]) / vol_ma
        active_bar = prev_1m

    bar_vol = float(active_bar["Volume"])
    taker_buy_ratio = float(active_bar["Taker buy base asset"]) / bar_vol if bar_vol > 0 else 0.5
    rsi_1m = float(latest_1m["RSI"]) if not np.isnan(latest_1m["RSI"]) else 50.0
    bb_upper = float(latest_1m["BB_Upper"])
    bb_lower = float(latest_1m["BB_Lower"])

    ev = evaluate_v31_from_scalars(
        symbol, base_price, current_price, atr_1m, sig_park10,
        vol_ratio, taker_buy_ratio, rsi_1m, bb_upper, bb_lower,
        trend_1h, sec_left, round_str, is_trap_hour, slot_label,
        enable_rebound, rsi_long_th, rsi_short_th, only_low_vol_alert
    )

    ctx = {
        "symbol": symbol,
        "round_5m_open_str": round_str,
        "base_price": base_price,
        "atr_1m": atr_1m,
        "sig_park10": sig_park10,
        "vol_ratio": vol_ratio,
        "taker_buy_ratio": taker_buy_ratio,
        "rsi_1m": rsi_1m,
        "bb_upper": bb_upper,
        "bb_lower": bb_lower,
        "trend_1h": trend_1h,
        "enable_rebound": enable_rebound,
        "rsi_long_th": rsi_long_th,
        "rsi_short_th": rsi_short_th,
        "only_low_vol_alert": only_low_vol_alert,
    }
    return ev, ctx


def capture_live_snapshot(sym_now):
    """即時抓取當毫秒 Mid 價與剩餘秒數，完整重算並打包為快照（供凍結按鈕與即時送出共用）"""
    now_tw, live_epoch_5m, live_sec_left, _, round_open_tw, live_is_trap, live_slot = get_taiwan_time_info()
    live_round_str = round_open_tw.strftime("%Y-%m-%d %H:%M")
    live_mid = get_mid_price(sym_now)
    ctx = st.session_state.latest_context

    if ctx is None or ctx.get("symbol") != sym_now or ctx.get("round_5m_open_str") != live_round_str:
        d1_live = add_technical_indicators(fetch_1m_klines(sym_now, live_epoch_5m, limit=60))
        d5_live = add_technical_indicators(resample_klines(d1_live, "5min"))
        t1h_live = fetch_1h_trend(sym_now, live_epoch_5m)
        live_ev, new_ctx = evaluate_market_v31(
            sym_now, d1_live, d5_live, live_mid, t1h_live,
            ctx["enable_rebound"] if ctx else True,
            ctx["rsi_long_th"] if ctx else 30,
            ctx["rsi_short_th"] if ctx else 70,
            ctx["only_low_vol_alert"] if ctx else True,
        )
        if new_ctx is not None:
            st.session_state.latest_context = new_ctx
    else:
        mid_use = live_mid if live_mid is not None else ctx["base_price"]
        live_ev = evaluate_v31_from_scalars(
            sym_now, ctx["base_price"], mid_use, ctx["atr_1m"], ctx["sig_park10"],
            ctx["vol_ratio"], ctx["taker_buy_ratio"], ctx["rsi_1m"],
            ctx["bb_upper"], ctx["bb_lower"], ctx["trend_1h"],
            live_sec_left, live_round_str, live_is_trap, live_slot,
            ctx["enable_rebound"], ctx["rsi_long_th"], ctx["rsi_short_th"],
            ctx["only_low_vol_alert"],
        )

    return {
        "now_tw": now_tw,
        "live_round_str": live_round_str,
        "live_sec_left": live_sec_left,
        "live_slot": live_slot,
        "sym_now": sym_now,
        "live_ev": live_ev,
    }


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


# --- 側邊欄：Google Sheets 5U 賠率收集器 (含凍結按鈕 ＋ 單格雙邊快填 ＋ 防誤打防呆) ---
with st.sidebar:
    st.header("📝 5U 真實賠率收集器")
    ws, gs_msg = get_gsheet_worksheet()
    if ws is not None:
        st.caption(f"🟢 {gs_msg}")
    else:
        st.caption(f"🟡 {gs_msg}（先暫存本頁，可下載 CSV）")

    # 📸 鎖定當下瞬間 / 解除按鈕列（徹底解決看雙邊報價與打字的時間飄移）
    f_col1, f_col2 = st.columns([2.2, 1.0])
    with f_col1:
        st.markdown('<div class="freeze-btn-wrap">', unsafe_allow_html=True)
        freeze_clicked = st.button(
            "📸 鎖定當下瞬間",
            key="btn_freeze_moment",
            type="primary" if st.session_state.frozen_snapshot is None else "secondary",
            use_container_width=True,
        )
        st.markdown("</div>", unsafe_allow_html=True)
    with f_col2:
        st.markdown('<div class="freeze-btn-wrap">', unsafe_allow_html=True)
        unfreeze_clicked = st.button(
            "🔓 解除",
            key="btn_unfreeze_moment",
            disabled=(st.session_state.frozen_snapshot is None),
            use_container_width=True,
        )
        st.markdown("</div>", unsafe_allow_html=True)

    if freeze_clicked:
        st.session_state.frozen_snapshot = capture_live_snapshot(st.session_state.selected_symbol)
        st.session_state.last_submit_feedback = None
        st.rerun()

    if unfreeze_clicked:
        st.session_state.frozen_snapshot = None
        st.session_state.last_submit_feedback = None
        st.rerun()

    # 若處於凍結狀態，顯示清晰凍結資訊卡片（並檢查是否已跨局）
    frozen = st.session_state.frozen_snapshot
    if frozen is not None:
        _, _, _, _, cur_round_tw, _, _ = get_taiwan_time_info()
        cur_round_str = cur_round_tw.strftime("%Y-%m-%d %H:%M")
        fev = frozen["live_ev"]
        f_sym = frozen["sym_now"].replace("USDT", "")
        f_hms = frozen["now_tw"].strftime("%H:%M:%S")
        is_stale_round = (frozen["live_round_str"] != cur_round_str)

        stale_warn = (
            f"\n\n⚠️ **注意：此凍結為上一局 ({frozen['live_round_str']})**，若要記本局請重新點擊鎖定或解除！"
            if is_stale_round else ""
        )
        st.info(
            f"📌 **已鎖定瞬間（寫入後自動解除）**\n\n"
            f"**{f_sym}** ｜ `{f_hms}` (剩 **{frozen['live_sec_left']}s**)\n\n"
            f"Mid: `{fev['mid_price']:,.2f}` ｜ 勝率: **{fev['p_est']*100:.1f}% ({fev['direction']})** ｜ 預估: `~{fev['est_binance_5u']:.2f}U`"
            f"{stale_warn}"
        )

    # 單格雙邊快填 ＋ 雙格兼容表單
    with st.form("ev_collector_form", clear_on_submit=True):
        st.markdown("💡 **快填技巧**：第一格直接打 **`UP DOWN`**（空白分隔，如 `6.14 23.33`）按 **Enter** 一次存雙邊；單邊可分格填，鎖單填 `0`。")
        c_up, c_dn = st.columns(2)
        with c_up:
            up_input = st.text_input("🟢 UP｜雙邊快填", placeholder="6.70 或 6.14 23.33")
        with c_dn:
            dn_input = st.text_input("🔴 DOWN (單填)", placeholder="選填或0")
        bypass_guard = st.checkbox("略過欄位防呆（極端行情強制寫入）", value=False)
        submit_lbl = "💾 寫入【凍結瞬間】紀錄 (Enter)" if frozen is not None else "💾 立即寫入紀錄 (Enter)"
        submitted = st.form_submit_button(submit_lbl, use_container_width=True)

    if submitted:
        up_val, dn_val, parse_err = parse_quote_inputs(up_input, dn_input)
        if parse_err is not None:
            st.session_state.last_submit_feedback = ("error", f"⚠️ {parse_err}")
        else:
            # 防呆 1：數值硬範圍檢查（幣安 5U 封頂約 490U）
            range_err_msg = None
            if up_val is not None and (up_val < 0 or up_val > 500):
                range_err_msg = f"🚫 買漲(UP) 金額 `{up_val}` 超出合理範圍 (0 ~ 500 U)，已攔截！"
            elif dn_val is not None and (dn_val < 0 or dn_val > 500):
                range_err_msg = f"🚫 買跌(DOWN) 金額 `{dn_val}` 超出合理範圍 (0 ~ 500 U)，已攔截！"

            if range_err_msg is not None:
                st.session_state.last_submit_feedback = ("error", range_err_msg)
            else:
                # 優先使用「凍結快照」，否則即時重算當毫秒數值
                used_freeze = (st.session_state.frozen_snapshot is not None)
                snap_pkg = (
                    st.session_state.frozen_snapshot
                    if used_freeze
                    else capture_live_snapshot(st.session_state.selected_symbol)
                )

                now_tw = snap_pkg["now_tw"]
                live_round_str = snap_pkg["live_round_str"]
                live_sec_left = snap_pkg["live_sec_left"]
                live_slot = snap_pkg["live_slot"]
                sym_now = snap_pkg["sym_now"]
                live_ev = snap_pkg["live_ev"]

                # 防呆 2：偵測疑似「買漲 / 買跌欄位填反」
                swap_warning = None
                if not bypass_guard:
                    diff_now = live_ev["price_diff"]
                    z_now = live_ev["z_score"]
                    if up_val is not None and up_val > 0:
                        if diff_now > 0 and z_now >= 0.35 and up_val > 12.0:
                            swap_warning = f"鎖定/當下為 UP 領先 (Z=+{z_now:.2f})，UP 報價不應高達 {up_val:.2f} U"
                        elif diff_now < 0 and z_now <= -0.60 and up_val < 6.5:
                            swap_warning = f"鎖定/當下為 DOWN 領先 (Z={z_now:.2f})，落後側 UP 報價不應低至 {up_val:.2f} U"
                    if dn_val is not None and dn_val > 0 and swap_warning is None:
                        if diff_now < 0 and z_now <= -0.35 and dn_val > 12.0:
                            swap_warning = f"鎖定/當下為 DOWN 領先 (Z={z_now:.2f})，DOWN 報價不應高達 {dn_val:.2f} U"
                        elif diff_now > 0 and z_now >= 0.60 and dn_val < 6.5:
                            swap_warning = f"鎖定/當下為 UP 領先 (Z=+{z_now:.2f})，落後側 DOWN 報價不應低至 {dn_val:.2f} U"

                if swap_warning is not None:
                    # 攔截失敗時保留凍結快照，讓使用者修正重送不遺失瞬間
                    st.session_state.last_submit_feedback = (
                        "error",
                        f"⚠️ **疑似買漲/買跌欄位填反，已自動攔截（凍結狀態已保留）！**\n\n"
                        f"{swap_warning}。請確認順序為「左 UP 右 DOWN」後重填，或勾選略過防呆送出。"
                    )
                else:
                    notes = ["V3.1"]
                    if used_freeze:
                        notes.append("凍結快照")
                    if up_val == 0:
                        notes.append("UP鎖單")
                    if dn_val == 0:
                        notes.append("DOWN鎖單")
                    if bypass_guard:
                        notes.append("強制寫入")

                    vig_pct = ""
                    if up_val and dn_val and up_val > 0 and dn_val > 0:
                        vig_pct = round(((5.0 / up_val) + (5.0 / dn_val) - 1.0) * 100.0, 2)

                    time_label = f"{now_tw.strftime('%Y-%m-%d')} ({WEEKDAY_MAP[now_tw.weekday()]}) {now_tw.strftime('%H:%M:%S')}"
                    row_data = [
                        time_label,
                        live_round_str,
                        sym_now,
                        live_sec_left,
                        live_slot,
                        round(live_ev["base_price"], 4),
                        round(live_ev["mid_price"], 4),
                        round(live_ev["price_diff"], 4),
                        round(live_ev["atr_1m"], 4),
                        round(live_ev["moat_ratio"], 2),
                        round(live_ev["sigma_1m"], 6),
                        round(live_ev["z_score"], 2),
                        round(live_ev["p_est"] * 100.0, 2),
                        round(live_ev["target_5u"], 2),
                        live_ev["direction"],
                        live_ev["tactic_tag"],
                        round(live_ev["vol_ratio"], 2),
                        "是" if live_ev["is_low_vol"] else "否",
                        round(live_ev["taker_buy_ratio"] * 100.0, 1),
                        round(live_ev["rsi_1m"], 1),
                        live_ev["trend_1h"],
                        up_val if up_val is not None else "",
                        dn_val if dn_val is not None else "",
                        vig_pct,
                        " / ".join(notes),
                    ]

                    st.session_state.local_records.append(dict(zip(SHEET_HEADERS, row_data)))

                    # 組合回饋摘要（清楚顯示寫入的 UP/DOWN 與抽水率）
                    val_parts = []
                    if up_val is not None:
                        val_parts.append(f"UP:{up_val:g}")
                    if dn_val is not None:
                        val_parts.append(f"DOWN:{dn_val:g}")
                    vig_str = f" ｜ 抽水 {vig_pct:.2f}%" if vig_pct != "" else ""
                    mode_prefix = "【凍結快照】" if used_freeze else ""

                    if ws is not None:
                        try:
                            ws.append_row(row_data, value_input_option="USER_ENTERED")
                            st.session_state.last_submit_feedback = (
                                "success",
                                f"✅ 已寫入{mode_prefix} Google Sheets！"
                                f"({sym_now.replace('USDT', '')} 剩 {live_sec_left}s ｜ {' / '.join(val_parts)}{vig_str})"
                            )
                        except Exception as e:
                            st.session_state.last_submit_feedback = (
                                "error",
                                f"寫入 Google Sheets 失敗，已改存本頁暫存：{e}"
                            )
                    else:
                        st.session_state.last_submit_feedback = (
                            "success",
                            f"✅ 已暫存{mode_prefix} ({len(st.session_state.local_records)} 筆 ｜ {' / '.join(val_parts)}{vig_str})"
                        )

                    # 成功寫入後自動解除凍結並刷新側邊欄狀態
                    st.session_state.frozen_snapshot = None
                    st.rerun()

    # 顯示最新一次送出結果回饋
    if st.session_state.last_submit_feedback is not None:
        fb_level, fb_msg = st.session_state.last_submit_feedback
        if fb_level == "success":
            st.success(fb_msg)
        elif fb_level == "warning":
            st.warning(fb_msg)
        else:
            st.error(fb_msg)

    if st.session_state.local_records:
        df_backup = pd.DataFrame(st.session_state.local_records)
        st.download_button(
            label=f"⬇️ 下載本次暫存紀錄 ({len(df_backup)} 筆 CSV)",
            data=df_backup.to_csv(index=False).encode("utf-8-sig"),
            file_name="binance_5m_v31_backup.csv",
            mime="text/csv",
            use_container_width=True,
        )

    st.divider()
    st.header("⚙️ V3.1 狙擊參數")
    enable_sound = st.checkbox("🔊 啟用提示音效", value=True)
    only_low_vol_alert = st.checkbox("🌟 順勢僅「縮量安靜 (<0.5x)」響鈴", value=True)
    mute_trap_hours = st.checkbox("🛡️ 午間陷阱 (12-15點) 順勢靜音", value=True)
    rsi_long_th = st.slider("⚡ 搶反彈 (買漲) RSI 基準門檻", min_value=15, max_value=45, value=30, step=1)
    rsi_short_th = st.slider("⚡ 摸頭空 (買跌) RSI 基準門檻", min_value=55, max_value=85, value=70, step=1)
    st.info(
        "🎯 **V3.1 精準定價版鐵律：**\n\n"
        "• **Parkinson-10 核心**：與幣安造市商定價 1:1 對齊，螢幕直接顯示「預估幣安此刻報價」。\n"
        "• **順勢甜蜜帶**：剩餘 `150s~16s`、縮量安靜、勝率 `72%~88%`（零鎖單）。\n"
        "• **搶反彈黃金區**：鎖定 App 報價 `建議門檻 ~ 30 U`（`>30U` 樂透區跳過）。"
    )


# --- 主介面頂部：純點擊雙核心按鈕 (絕不彈出鍵盤) + 即時報價 + 搶反彈開關 ---
st.title("🎯 Binance Predict 5分鐘實證狙擊系統 V3.1")


def select_symbol(sym):
    st.session_state.selected_symbol = sym
    st.session_state.frozen_snapshot = None
    st.session_state.last_submit_feedback = None


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
        ev_r, _ = evaluate_market_v31(
            osym, d1, d5, mid_p, t1h, enable_rebound_flag, r_long, r_short, only_low_vol
        )
        short_name = osym.replace("USDT", "")
        if ev_r["is_locked"]:
            return f"[{short_name}] 🔒 尾盤靜音", None, ev_r["signal_mode"]
        if ev_r["is_lock_zone"] and ev_r["signal_mode"] == "trend":
            return f"[{short_name}] 🔒 鎖單紅線", None, "trend"
        if ev_r["should_alert"]:
            if ev_r["signal_mode"] == "rebound":
                act = "💥反彈UP" if ev_r["direction"] == "UP" else "💥摸空DOWN"
                txt = f"**[{short_name}] {act} {ev_r['p_est']*100:.0f}% (5U:{ev_r['target_5u']:.1f}~30)**"
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

    ev, ctx = evaluate_market_v31(
        selected_symbol, df_1m, df_5m, mid_p, trend_1h,
        enable_rebound_flag, r_long, r_short, only_low_vol
    )
    if ctx is not None:
        st.session_state.latest_context = ctx

    if is_trap_hour:
        st.warning("⚠️ **目前為午間陷阱時段 (12:00 ~ 15:00)**：實測順勢交易嚴重負 EV（平日 -0.49U/筆），建議觀望或僅留意 12~30U 極端搶反彈。")

    # 1. 背景雷達列 ＋ 當前時段屬性
    st.markdown(
        f"<div style='text-align:left; font-size:16px; margin-top:4px; margin-bottom:4px; color:#CCCCCC;'>"
        f"📡 <b>背景雷達：</b> {radar_text} &nbsp;｜&nbsp; 🕒 <b>時段：</b> {slot_label}</div>",
        unsafe_allow_html=True,
    )

    # 2. 核心勝率 ＋ 5U 行動線 ＋ 預估幣安此刻報價（一眼抓溢價）
    p_pct = ev["p_est"] * 100.0
    score_color = "#4FC3F7" if ev["should_alert"] else ("#FFD54F" if p_pct >= 72 else "#9E9E9E")
    trend_badge_color = "#26A69A" if trend_1h == "多頭" else ("#EF5350" if trend_1h == "空頭" else "#B0BEC5")
    dir_label = "買漲 UP" if ev["direction"] == "UP" else ("買跌 DOWN" if ev["direction"] == "DOWN" else "觀望")
    vol_badge = f"🌟縮量({ev['vol_ratio']:.2f}x)" if ev["is_low_vol"] else f"量能({ev['vol_ratio']:.2f}x)"

    if ev["is_lock_zone"] and ev["signal_mode"] == "trend":
        est_quote_html = "<span style='color:#EF5350;'>🔒 預估幣安報價：鎖單紅線</span>"
    else:
        est_quote_html = f"<span style='color:#CE93D8;'>🏦 預估幣安報價：~{ev['est_binance_5u']:.2f} U</span>"

    if ev["signal_mode"] == "rebound":
        target_html = f"<span style='color:#81C784;'>✅ 黃金出手區：{ev['target_5u']:.2f} ~ 30.00 U</span>"
    else:
        target_html = f"<span style='color:#81C784;'>✅ 建議出手(+5%)：≥ {ev['target_5u']:.2f} U</span>"

    st.markdown(
        f"<div style='text-align:left; font-size:20px; font-weight:bold; margin-top:0px; margin-bottom:10px; line-height:1.6;'>"
        f"<span style='color:{score_color};'>🎯 V3.1 預估勝率：{p_pct:.1f}% ({dir_label})</span>"
        f" &nbsp;｜&nbsp; "
        f"<span style='color:#FFFFFF;'>💵 5U 回本：<span style='color:#FF8A65;'>{ev['breakeven_5u']:.2f} U</span></span>"
        f" &nbsp;｜&nbsp; "
        f"{target_html}"
        f" &nbsp;｜&nbsp; "
        f"{est_quote_html}"
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

    # 4. 音效觸發控制（最後 15 秒全面靜音 + 午間陷阱順勢靜音）
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

    st.caption(
        "💡 實戰決策口訣：若幣安 App 實際 5U 報價高於【預估幣安報價】且達【建議出手門檻】，代表抓到造市商定價慢半拍的溢價便宜單；"
        "搶反彈請嚴格控制在「建議門檻 ~ 30 U」之間，超過 30 U 為 0 勝率樂透區請直接跳過。"
    )


show_prediction_dashboard(
    st.session_state.selected_symbol,
    enable_rebound,
    rsi_long_th,
    rsi_short_th,
    enable_sound,
    only_low_vol_alert,
    mute_trap_hours,
)

# --- 網頁底部：V3.1 實證邏輯說明 ---
st.divider()
with st.expander("📖 V3.1 Parkinson-10 精準定價版規則說明 (點擊展開)"):
    st.markdown("""
    ### 1. V3.1 核心升級：Parkinson-10 ＋ 零中心化校準
    * **波動率換裝**：經 93 筆實戰試算表反推驗證，`Parkinson-10`（高低點振幅波動率）與幣安造市商隱含波動率比值高達 `1.001x`（相關係數 `0.929`），誤差幾乎砍半。
    * **零中心化校準**：移除正常量能與通道內的免費加分，並將縮量加分收斂至 `+2.0%`，徹底消除先前 `+2.5%` 的樂觀偏移。
    * **預估幣安報價對照**：直接以 `Parkinson-10` ＋ 實測 `2%` 費率 ＋ `0.5%` 單邊點差推算幣安理論報價，方便盤中一眼抓出造市商報價落後的溢價漏洞。

    ### 2. 順勢甜蜜射程 & 鎖單紅線預警
    * **黃金狙擊窗**：剩餘 **`150 秒 ~ 16 秒`**、**縮量安靜 (`< 0.5x`)**、**V3.1 預估勝率 `72% ~ 88%`**（實測零鎖單、雙幣種於 5% 壓力抽水下皆正 EV）。
    * **三大靜音禁區**：過熱／爆量地雷（RSI `>75/<25`、碰布林邊界、量能 `>=1.5x`）、莊家鎖單紅線（剩餘 `<=75s` 且 `|Z|>=2.1` 或預估報價 `<5.03U`）、以及低賠率黑洞（`>88%`）。

    ### 3. 搶反彈黃金報價區 (12 ~ 30 U) 與收集器防呆
    * 搶反彈維持 **`≥ 90 秒`**、**`≤ 1.8x ATR`**、**1H 防接刀**三防線，並限制在 App 報價 **`建議門檻 ~ 30 U`** 內出手（`>30U` 樂透區實測 0 勝率直接跳過）。
    * **凍結快照 ＋ 雙邊快填**：看到雙邊報價先按 **`📸 鎖定當下瞬間`** 凍結價格與剩餘秒數，再於第一格輸入 `UP DOWN`（空白分隔，如 `6.14 23.33`）按 Enter 一次寫入並自動計算造市商抽水率。
    """)
