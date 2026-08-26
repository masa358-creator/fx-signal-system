"""
FX 売買タイミング自動判定システム(複数通貨ペア対応版)

- yfinance で複数通貨ペアのレートを取得(無料)
- 移動平均クロス / RSI / MACD / ボリンジャーバンドでスコアリング
- 合計スコアが閾値を超えたら Discord Webhook に通知(無料)
- state.json に通貨ペアごとの前回シグナルを保存し、同じシグナルの連続通知を防ぐ
- 新規にBUY/SELLと判定された通貨ペアだけを actionable_signals.json に書き出し、
  次のステップ(ctrader_trader.py)がそれを読んで発注する

環境変数:
  DISCORD_WEBHOOK_URL   必須。DiscordのWebhook URL
  FX_SYMBOLS            任意。カンマ区切りのyfinanceティッカー。
                         デフォルトは主要18通貨ペア(下記 DEFAULT_SYMBOLS)
  FX_INTERVAL           任意。デフォルト "1h"
  FX_PERIOD             任意。デフォルト "30d"
  SIGNAL_THRESHOLD      任意。デフォルト 2 (この点数以上でシグナル発報)
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

# yfinanceのティッカー("XXXYYY=X")。cTrader側の銘柄名は "=X" を除いたもの。
DEFAULT_SYMBOLS = [
    "USDJPY=X", "EURJPY=X", "GBPJPY=X", "AUDJPY=X", "NZDJPY=X", "CADJPY=X", "CHFJPY=X",
    "EURUSD=X", "GBPUSD=X", "AUDUSD=X", "NZDUSD=X", "USDCAD=X", "USDCHF=X",
    "EURGBP=X", "EURAUD=X", "EURCHF=X", "GBPCHF=X", "AUDNZD=X",
]

# 暗号資産(現時点では通知・一覧表示のみ。発注はロット換算の仕様確認後に対応予定)
DEFAULT_CRYPTO_SYMBOLS = [
    "BTC-USD", "ETH-USD", "LTC-USD", "XRP-USD", "BCH-USD",
    "ADA-USD", "SOL-USD", "DOGE-USD", "DOT-USD", "LINK-USD",
]

SYMBOLS = [s.strip() for s in os.environ.get("FX_SYMBOLS", ",".join(DEFAULT_SYMBOLS)).split(",") if s.strip()]
CRYPTO_SYMBOLS = [s.strip() for s in os.environ.get("CRYPTO_SYMBOLS", ",".join(DEFAULT_CRYPTO_SYMBOLS)).split(",") if s.strip()]
ENTRY_INTERVAL = os.environ.get("FX_INTERVAL", "1h")   # エントリータイミング判定(下位足)
ENTRY_PERIOD = os.environ.get("FX_PERIOD", "30d")
DAILY_PERIOD = os.environ.get("FX_DAILY_PERIOD", "200d")  # 日足トレンド判定用(MA75に必要な日数を確保)
THRESHOLD = int(os.environ.get("SIGNAL_THRESHOLD", "2"))  # 現在未使用(互換性のため残置)
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

RANK_LOTS = {
    "S": float(os.environ.get("LOT_S_RANK", "0.1")),   # 3条件以上一致
    "A": float(os.environ.get("LOT_A_RANK", "0.07")),  # 2条件一致(0.05〜0.1の中間値)
}

STATE_FILE = Path(__file__).parent / "state.json"
ACTIONABLE_FILE = Path(__file__).parent / "actionable_signals.json"
DOCS_DIR = Path(__file__).parent / "docs"
STATUS_PAGE = DOCS_DIR / "index.html"


def display_symbol_name(yf_symbol: str) -> str:
    """'USDJPY=X' -> 'USDJPY'、'BTC-USD' -> 'BTCUSD'"""
    return yf_symbol.replace("=X", "").replace("-", "")


def ctrader_symbol_name(yf_symbol: str) -> str:
    """'USDJPY=X' -> 'USDJPY'"""
    return yf_symbol.replace("=X", "")


def fetch_data(symbol: str, interval: str, period: str) -> pd.DataFrame:
    df = yf.download(
        symbol, interval=interval, period=period, progress=False, auto_adjust=False
    )
    if df.empty:
        raise RuntimeError(f"データが取得できませんでした: {symbol} ({interval})")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


def resample_ohlc(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """1時間足のデータを、より長い足(例:4時間足)にまとめ直す"""
    resampled = df.resample(rule).agg({
        "Open": "first", "High": "max", "Low": "min", "Close": "last",
    }).dropna()
    return resampled


def determine_trend(df: pd.DataFrame) -> str:
    """MA20とMA75の位置関係で、その時間足の大まかなトレンド方向を判定する"""
    if len(df) < 76:
        return "UNKNOWN"
    close = df["Close"]
    ma_short = close.rolling(window=20).mean().iloc[-1]
    ma_long = close.rolling(window=75).mean().iloc[-1]
    if pd.isna(ma_short) or pd.isna(ma_long):
        return "UNKNOWN"
    if ma_short > ma_long:
        return "UP"
    if ma_short < ma_long:
        return "DOWN"
    return "FLAT"


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    close = df["Close"]

    df["MA_short"] = close.rolling(window=20).mean()
    df["MA_long"] = close.rolling(window=75).mean()

    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=14).mean()
    avg_loss = loss.rolling(window=14).mean()
    rs = avg_gain / avg_loss
    df["RSI"] = 100 - (100 / (1 + rs))

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    df["MACD"] = ema12 - ema26
    df["MACD_signal"] = df["MACD"].ewm(span=9, adjust=False).mean()

    mid = close.rolling(window=20).mean()
    std = close.rolling(window=20).std()
    df["BB_upper"] = mid + 2 * std
    df["BB_lower"] = mid - 2 * std

    return df


def score_signal(df: pd.DataFrame) -> dict:
    latest = df.iloc[-1]
    prev = df.iloc[-2]

    buy_conditions = []
    sell_conditions = []

    ma_cross_up = prev["MA_short"] <= prev["MA_long"] and latest["MA_short"] > latest["MA_long"]
    ma_cross_down = prev["MA_short"] >= prev["MA_long"] and latest["MA_short"] < latest["MA_long"]
    if ma_cross_up:
        buy_conditions.append("移動平均ゴールデンクロス")
    if ma_cross_down:
        sell_conditions.append("移動平均デッドクロス")

    if latest["RSI"] < 30:
        buy_conditions.append(f"RSI売られすぎ({latest['RSI']:.1f})")
    if latest["RSI"] > 70:
        sell_conditions.append(f"RSI買われすぎ({latest['RSI']:.1f})")

    macd_cross_up = prev["MACD"] <= prev["MACD_signal"] and latest["MACD"] > latest["MACD_signal"]
    macd_cross_down = prev["MACD"] >= prev["MACD_signal"] and latest["MACD"] < latest["MACD_signal"]
    if macd_cross_up:
        buy_conditions.append("MACDゴールデンクロス")
    if macd_cross_down:
        sell_conditions.append("MACDデッドクロス")

    if latest["Close"] <= latest["BB_lower"]:
        buy_conditions.append("ボリンジャーバンド-2σタッチ")
    if latest["Close"] >= latest["BB_upper"]:
        sell_conditions.append("ボリンジャーバンド+2σタッチ")

    buy_count = len(buy_conditions)
    sell_count = len(sell_conditions)

    if buy_count > sell_count and buy_count >= 1:
        signal = "BUY"
        match_count = buy_count
        reasons = buy_conditions
    elif sell_count > buy_count and sell_count >= 1:
        signal = "SELL"
        match_count = sell_count
        reasons = sell_conditions
    else:
        signal = "NEUTRAL"
        match_count = 0
        reasons = []

    if match_count >= 3:
        rank = "S"
        should_trade = True
        lot = RANK_LOTS["S"]
    elif match_count == 2:
        rank = "A"
        should_trade = True
        lot = RANK_LOTS["A"]
    elif match_count == 1:
        rank = "B"
        should_trade = False  # 1条件のみは見送り(通知のみ)
        lot = None
    else:
        rank = None
        should_trade = False
        lot = None

    return {
        "signal": signal,
        "rank": rank,
        "should_trade": should_trade,
        "lot": lot,
        "match_count": match_count,
        "reasons": reasons,
        "price": float(latest["Close"]),
        "timestamp": str(latest.name),
    }


def apply_trend_filter(result: dict, trend_4h: str, trend_daily: str) -> dict:
    """
    4時間足・日足のトレンドと、1時間足のシグナル方向が一致しているか確認する。
    一致していなければ、たとえS/Aランクでも発注を見送りにする(通知はそのまま行う)。
    """
    result["trend_4h"] = trend_4h
    result["trend_daily"] = trend_daily

    if result["signal"] == "NEUTRAL":
        result["trend_match"] = None
        return result

    required_trend = "UP" if result["signal"] == "BUY" else "DOWN"
    trend_match = trend_4h == required_trend and trend_daily == required_trend
    result["trend_match"] = trend_match

    if not trend_match:
        if result["should_trade"]:
            result["reasons"] = result["reasons"] + [
                f"上位足トレンド不一致のため見送り(4時間足:{trend_4h} / 日足:{trend_daily})"
            ]
        result["should_trade"] = False
        result["lot"] = None

    return result


def load_state() -> dict:
    if STATE_FILE.exists():
        raw = json.loads(STATE_FILE.read_text())
        # 旧形式("last_signal"キーのみ)から新形式(通貨ペアごと)への移行
        if "last_signal" in raw and not any(isinstance(v, str) for k, v in raw.items() if k != "last_signal"):
            return {}
        return raw
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def notify_discord(symbol_display: str, result: dict) -> None:
    if not WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL が未設定のため通知をスキップしました")
        return

    emoji = "🟢" if result["signal"] == "BUY" else "🔴"
    type_tag = "🪙" if result.get("asset_type") == "CRYPTO" else "💱"
    rank_label = {
        "S": "⭐️Sランク(3条件一致)",
        "A": "🅰️Aランク(2条件一致)",
        "B": "🅱️Bランク(1条件のみ)",
    }.get(result["rank"], result["rank"])
    reasons_text = "\n".join(f"- {r}" for r in result["reasons"]) or "- (根拠なし)"
    if result["should_trade"]:
        lot_line = f"発注ロット: {result['lot']}(上位足トレンド一致)\n"
    else:
        lot_line = "発注: 見送り\n"
    trend_line = f"上位足トレンド: 4時間足={result.get('trend_4h', '-')} / 日足={result.get('trend_daily', '-')}\n"
    content = (
        f"{emoji} **{type_tag}{symbol_display} {result['signal']}シグナル({rank_label})**\n"
        f"レート: {result['price']:.4f}\n"
        f"{trend_line}"
        f"{lot_line}"
        f"根拠:\n{reasons_text}\n"
        f"時刻: {result['timestamp']}"
    )
    resp = requests.post(WEBHOOK_URL, json={"content": content}, timeout=10)
    resp.raise_for_status()


def generate_status_page(all_results: list) -> None:
    """全通貨ペアの最新状態を一覧できる、スマホ向けの簡易HTMLページを生成する"""
    DOCS_DIR.mkdir(exist_ok=True)

    def badge(signal: str, rank: str) -> str:
        color = {"BUY": "#16a34a", "SELL": "#dc2626", "NEUTRAL": "#9ca3af"}.get(signal, "#9ca3af")
        label = {"BUY": "🟢 BUY", "SELL": "🔴 SELL", "NEUTRAL": "⚪ NEUTRAL"}.get(signal, signal)
        rank_text = f" [{rank}]" if rank else ""
        return f'<span style="background:{color};color:#fff;padding:4px 10px;border-radius:12px;font-weight:bold;font-size:14px;">{label}{rank_text}</span>'

    rows = []
    # BUY/SELLを上に、NEUTRALを下にソート。同じ中ではランクが高い順
    rank_order = {"S": 0, "A": 1, "B": 2, None: 3}
    sorted_results = sorted(
        all_results,
        key=lambda r: (0 if r["signal"] != "NEUTRAL" else 1, rank_order.get(r["rank"], 3)),
    )
    for r in sorted_results:
        reasons_text = "、".join(r["reasons"]) if r["reasons"] else "-"
        type_tag = "🪙" if r.get("asset_type") == "CRYPTO" else "💱"
        price_str = f"{r['price']:,.4f}" if r["price"] < 1000 else f"{r['price']:,.2f}"
        rows.append(f"""
        <tr>
          <td style="padding:10px;font-weight:bold;">{type_tag} {r['symbol']}</td>
          <td style="padding:10px;">{badge(r['signal'], r['rank'])}</td>
          <td style="padding:10px;">{price_str}</td>
          <td style="padding:10px;font-size:13px;color:#555;">{reasons_text}</td>
        </tr>""")

    updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FX・暗号資産シグナル一覧</title>
<style>
  body {{ font-family: -apple-system, sans-serif; background:#f3f4f6; margin:0; padding:16px; }}
  h1 {{ font-size:20px; }}
  .updated {{ color:#666; font-size:13px; margin-bottom:16px; }}
  table {{ width:100%; border-collapse:collapse; background:#fff; border-radius:8px; overflow:hidden; box-shadow:0 1px 3px rgba(0,0,0,0.1); }}
  th {{ text-align:left; padding:10px; background:#111827; color:#fff; font-size:13px; }}
  tr:nth-child(even) {{ background:#f9fafb; }}
</style>
</head>
<body>
<h1>📊 シグナル一覧(💱FX18ペア + 🪙暗号資産10ペア)</h1>
<div class="updated">最終更新: {updated_at}(30分おきに自動更新)。🪙暗号資産は現在、通知・表示のみ(発注非対応)</div>
<table>
<tr><th>銘柄</th><th>シグナル</th><th>レート</th><th>根拠</th></tr>
{''.join(rows)}
</table>
</body>
</html>"""
    STATUS_PAGE.write_text(html, encoding="utf-8")


def main() -> None:
    state = load_state()
    actionable = []
    all_results = []

    targets = [(s, "FX") for s in SYMBOLS] + [(s, "CRYPTO") for s in CRYPTO_SYMBOLS]

    for yf_symbol, asset_type in targets:
        display = display_symbol_name(yf_symbol)
        try:
            df_1h = fetch_data(yf_symbol, ENTRY_INTERVAL, ENTRY_PERIOD)
            df_1h = add_indicators(df_1h)
            result = score_signal(df_1h)

            # 上位足(4時間足・日足)のトレンドを確認する
            df_4h = resample_ohlc(df_1h[["Open", "High", "Low", "Close"]], "4h")
            trend_4h = determine_trend(df_4h)

            df_daily = fetch_data(yf_symbol, "1d", DAILY_PERIOD)
            trend_daily = determine_trend(df_daily)

            result = apply_trend_filter(result, trend_4h, trend_daily)

            # 暗号資産は現時点では通知・一覧表示のみ(発注のロット換算仕様が未確認のため)
            if asset_type == "CRYPTO":
                if result["should_trade"]:
                    result["reasons"] = result["reasons"] + ["暗号資産は現在発注非対応(通知のみ)"]
                result["should_trade"] = False
                result["lot"] = None
        except Exception as exc:  # 1銘柄の失敗で全体を止めない
            print(f"[{display}] エラーのためスキップ: {exc}")
            continue

        print(f"[{display}] {json.dumps(result, ensure_ascii=False)}")
        result["asset_type"] = asset_type
        all_results.append({"symbol": display, **result})

        # ランクまで含めて前回と比較する(同じBUYでもB→Aに上がったら再通知する)
        current_key = f"{result['signal']}:{result['rank']}" if result["signal"] != "NEUTRAL" else "NEUTRAL"
        last_key = state.get(display, "NEUTRAL")

        if result["signal"] != "NEUTRAL" and current_key != last_key:
            notify_discord(display, result)
            if result["should_trade"]:
                actionable.append({
                    "symbol": display,
                    "signal": result["signal"],
                    "price": result["price"],
                    "lot": result["lot"],
                    "rank": result["rank"],
                })
            state[display] = current_key
        elif result["signal"] == "NEUTRAL":
            state[display] = "NEUTRAL"
        else:
            print(f"[{display}] 前回と同じランクのシグナルのため通知をスキップしました")

        time.sleep(0.5)  # yfinanceへの連続リクエストを緩やかにする

    save_state(state)
    ACTIONABLE_FILE.write_text(json.dumps(actionable, ensure_ascii=False, indent=2))
    generate_status_page(all_results)
    print(f"\n発注対象シグナル数: {len(actionable)}")


if __name__ == "__main__":
    main()
