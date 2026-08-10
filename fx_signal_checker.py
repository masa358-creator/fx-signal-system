"""
FX 売買タイミング自動判定システム(プロトタイプ)

- yfinance で為替レートを取得(無料)
- 移動平均クロス / RSI / MACD / ボリンジャーバンドでスコアリング
- 合計スコアが閾値を超えたら Discord Webhook に通知(無料)
- state.json に前回のシグナルを保存し、同じシグナルの連続通知を防ぐ

環境変数:
  DISCORD_WEBHOOK_URL   必須。DiscordのWebhook URL
  FX_SYMBOL             任意。デフォルト "USDJPY=X"
  FX_INTERVAL           任意。デフォルト "1h"
  FX_PERIOD             任意。デフォルト "30d"
  SIGNAL_THRESHOLD      任意。デフォルト 2 (この点数以上でシグナル発報)
"""

import json
import os
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

SYMBOL = os.environ.get("FX_SYMBOL", "USDJPY=X")
INTERVAL = os.environ.get("FX_INTERVAL", "1h")
PERIOD = os.environ.get("FX_PERIOD", "30d")
THRESHOLD = int(os.environ.get("SIGNAL_THRESHOLD", "2"))
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

STATE_FILE = Path(__file__).parent / "state.json"


def fetch_data() -> pd.DataFrame:
    df = yf.download(
        SYMBOL, interval=INTERVAL, period=PERIOD, progress=False, auto_adjust=False
    )
    if df.empty:
        raise RuntimeError(f"データが取得できませんでした: {SYMBOL}")
    # yfinance が MultiIndex 列を返すケースに対応
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    close = df["Close"]

    # 移動平均線
    df["MA_short"] = close.rolling(window=20).mean()
    df["MA_long"] = close.rolling(window=75).mean()

    # RSI (14期間)
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=14).mean()
    avg_loss = loss.rolling(window=14).mean()
    rs = avg_gain / avg_loss
    df["RSI"] = 100 - (100 / (1 + rs))

    # MACD (12, 26, 9)
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    df["MACD"] = ema12 - ema26
    df["MACD_signal"] = df["MACD"].ewm(span=9, adjust=False).mean()

    # ボリンジャーバンド (20期間, ±2σ)
    mid = close.rolling(window=20).mean()
    std = close.rolling(window=20).std()
    df["BB_upper"] = mid + 2 * std
    df["BB_lower"] = mid - 2 * std

    return df


def score_signal(df: pd.DataFrame) -> dict:
    """直近2本を比較してクロス/極値を判定し、買い・売りスコアを算出する"""
    latest = df.iloc[-1]
    prev = df.iloc[-2]

    buy_score = 0
    sell_score = 0
    reasons = []

    # 1. 移動平均クロス (ゴールデンクロス / デッドクロス) -> 重み2
    ma_cross_up = prev["MA_short"] <= prev["MA_long"] and latest["MA_short"] > latest["MA_long"]
    ma_cross_down = prev["MA_short"] >= prev["MA_long"] and latest["MA_short"] < latest["MA_long"]
    if ma_cross_up:
        buy_score += 2
        reasons.append("移動平均ゴールデンクロス")
    if ma_cross_down:
        sell_score += 2
        reasons.append("移動平均デッドクロス")

    # 2. RSI の極値 -> 重み1
    if latest["RSI"] < 30:
        buy_score += 1
        reasons.append(f"RSI売られすぎ({latest['RSI']:.1f})")
    if latest["RSI"] > 70:
        sell_score += 1
        reasons.append(f"RSI買われすぎ({latest['RSI']:.1f})")

    # 3. MACD クロス -> 重み1
    macd_cross_up = prev["MACD"] <= prev["MACD_signal"] and latest["MACD"] > latest["MACD_signal"]
    macd_cross_down = prev["MACD"] >= prev["MACD_signal"] and latest["MACD"] < latest["MACD_signal"]
    if macd_cross_up:
        buy_score += 1
        reasons.append("MACDゴールデンクロス")
    if macd_cross_down:
        sell_score += 1
        reasons.append("MACDデッドクロス")

    # 4. ボリンジャーバンド タッチ -> 重み1
    if latest["Close"] <= latest["BB_lower"]:
        buy_score += 1
        reasons.append("ボリンジャーバンド-2σタッチ")
    if latest["Close"] >= latest["BB_upper"]:
        sell_score += 1
        reasons.append("ボリンジャーバンド+2σタッチ")

    if buy_score >= THRESHOLD and buy_score > sell_score:
        signal = "BUY"
    elif sell_score >= THRESHOLD and sell_score > buy_score:
        signal = "SELL"
    else:
        signal = "NEUTRAL"

    return {
        "signal": signal,
        "buy_score": buy_score,
        "sell_score": sell_score,
        "reasons": reasons,
        "price": float(latest["Close"]),
        "timestamp": str(latest.name),
    }


def load_last_signal() -> str:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text()).get("last_signal", "NEUTRAL")
    return "NEUTRAL"


def save_last_signal(signal: str) -> None:
    STATE_FILE.write_text(json.dumps({"last_signal": signal}, ensure_ascii=False))


def export_to_github_env(result: dict) -> None:
    """GitHub Actions内で実行されている場合、次のステップにFX_SIGNAL/FX_PRICEを渡す"""
    github_env = os.environ.get("GITHUB_ENV")
    if not github_env:
        return
    with open(github_env, "a") as f:
        f.write(f"FX_SIGNAL={result['signal']}\n")
        f.write(f"FX_PRICE={result['price']}\n")


def notify_discord(result: dict) -> None:
    if not WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL が未設定のため通知をスキップしました")
        return

    emoji = "🟢" if result["signal"] == "BUY" else "🔴"
    reasons_text = "\n".join(f"- {r}" for r in result["reasons"]) or "- (根拠なし)"
    content = (
        f"{emoji} **{SYMBOL} {result['signal']}シグナル**\n"
        f"レート: {result['price']:.3f}\n"
        f"買いスコア: {result['buy_score']} / 売りスコア: {result['sell_score']}\n"
        f"根拠:\n{reasons_text}\n"
        f"時刻: {result['timestamp']}"
    )
    resp = requests.post(WEBHOOK_URL, json={"content": content}, timeout=10)
    resp.raise_for_status()


def main() -> None:
    df = fetch_data()
    df = add_indicators(df)
    result = score_signal(df)

    print(json.dumps(result, ensure_ascii=False, indent=2))

    last_signal = load_last_signal()
    if result["signal"] != "NEUTRAL" and result["signal"] != last_signal:
        notify_discord(result)
        export_to_github_env(result)
        save_last_signal(result["signal"])
    elif result["signal"] == "NEUTRAL":
        # NEUTRALに戻ったら次回の非NEUTRALシグナルを再通知できるようにリセット
        save_last_signal("NEUTRAL")
    else:
        print(f"前回と同じシグナル({result['signal']})のため通知をスキップしました")


if __name__ == "__main__":
    main()
