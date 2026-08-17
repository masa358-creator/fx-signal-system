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

SYMBOLS = [s.strip() for s in os.environ.get("FX_SYMBOLS", ",".join(DEFAULT_SYMBOLS)).split(",") if s.strip()]
INTERVAL = os.environ.get("FX_INTERVAL", "1h")
PERIOD = os.environ.get("FX_PERIOD", "30d")
THRESHOLD = int(os.environ.get("SIGNAL_THRESHOLD", "2"))
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

STATE_FILE = Path(__file__).parent / "state.json"
ACTIONABLE_FILE = Path(__file__).parent / "actionable_signals.json"
DOCS_DIR = Path(__file__).parent / "docs"
STATUS_PAGE = DOCS_DIR / "index.html"


def ctrader_symbol_name(yf_symbol: str) -> str:
    """'USDJPY=X' -> 'USDJPY'"""
    return yf_symbol.replace("=X", "")


def fetch_data(symbol: str) -> pd.DataFrame:
    df = yf.download(
        symbol, interval=INTERVAL, period=PERIOD, progress=False, auto_adjust=False
    )
    if df.empty:
        raise RuntimeError(f"データが取得できませんでした: {symbol}")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


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

    buy_score = 0
    sell_score = 0
    reasons = []

    ma_cross_up = prev["MA_short"] <= prev["MA_long"] and latest["MA_short"] > latest["MA_long"]
    ma_cross_down = prev["MA_short"] >= prev["MA_long"] and latest["MA_short"] < latest["MA_long"]
    if ma_cross_up:
        buy_score += 2
        reasons.append("移動平均ゴールデンクロス")
    if ma_cross_down:
        sell_score += 2
        reasons.append("移動平均デッドクロス")

    if latest["RSI"] < 30:
        buy_score += 1
        reasons.append(f"RSI売られすぎ({latest['RSI']:.1f})")
    if latest["RSI"] > 70:
        sell_score += 1
        reasons.append(f"RSI買われすぎ({latest['RSI']:.1f})")

    macd_cross_up = prev["MACD"] <= prev["MACD_signal"] and latest["MACD"] > latest["MACD_signal"]
    macd_cross_down = prev["MACD"] >= prev["MACD_signal"] and latest["MACD"] < latest["MACD_signal"]
    if macd_cross_up:
        buy_score += 1
        reasons.append("MACDゴールデンクロス")
    if macd_cross_down:
        sell_score += 1
        reasons.append("MACDデッドクロス")

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
    reasons_text = "\n".join(f"- {r}" for r in result["reasons"]) or "- (根拠なし)"
    content = (
        f"{emoji} **{symbol_display} {result['signal']}シグナル**\n"
        f"レート: {result['price']:.4f}\n"
        f"買いスコア: {result['buy_score']} / 売りスコア: {result['sell_score']}\n"
        f"根拠:\n{reasons_text}\n"
        f"時刻: {result['timestamp']}"
    )
    resp = requests.post(WEBHOOK_URL, json={"content": content}, timeout=10)
    resp.raise_for_status()


def generate_status_page(all_results: list) -> None:
    """全通貨ペアの最新状態を一覧できる、スマホ向けの簡易HTMLページを生成する"""
    DOCS_DIR.mkdir(exist_ok=True)

    def badge(signal: str) -> str:
        color = {"BUY": "#16a34a", "SELL": "#dc2626", "NEUTRAL": "#9ca3af"}.get(signal, "#9ca3af")
        label = {"BUY": "🟢 BUY", "SELL": "🔴 SELL", "NEUTRAL": "⚪ NEUTRAL"}.get(signal, signal)
        return f'<span style="background:{color};color:#fff;padding:4px 10px;border-radius:12px;font-weight:bold;font-size:14px;">{label}</span>'

    rows = []
    # BUY/SELLを上に、NEUTRALを下にソート
    sorted_results = sorted(all_results, key=lambda r: 0 if r["signal"] != "NEUTRAL" else 1)
    for r in sorted_results:
        reasons_text = "、".join(r["reasons"]) if r["reasons"] else "-"
        rows.append(f"""
        <tr>
          <td style="padding:10px;font-weight:bold;">{r['symbol']}</td>
          <td style="padding:10px;">{badge(r['signal'])}</td>
          <td style="padding:10px;">{r['price']:.4f}</td>
          <td style="padding:10px;font-size:13px;color:#555;">{reasons_text}</td>
        </tr>""")

    updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FXシグナル一覧</title>
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
<h1>📊 FXシグナル一覧(18通貨ペア)</h1>
<div class="updated">最終更新: {updated_at}(30分おきに自動更新)</div>
<table>
<tr><th>通貨ペア</th><th>シグナル</th><th>レート</th><th>根拠</th></tr>
{''.join(rows)}
</table>
</body>
</html>"""
    STATUS_PAGE.write_text(html, encoding="utf-8")


def main() -> None:
    state = load_state()
    actionable = []
    all_results = []

    for yf_symbol in SYMBOLS:
        display = ctrader_symbol_name(yf_symbol)
        try:
            df = fetch_data(yf_symbol)
            df = add_indicators(df)
            result = score_signal(df)
        except Exception as exc:  # 1銘柄の失敗で全体を止めない
            print(f"[{display}] エラーのためスキップ: {exc}")
            continue

        print(f"[{display}] {json.dumps(result, ensure_ascii=False)}")
        all_results.append({"symbol": display, **result})

        last_signal = state.get(display, "NEUTRAL")
        if result["signal"] != "NEUTRAL" and result["signal"] != last_signal:
            notify_discord(display, result)
            actionable.append({
                "symbol": display,
                "signal": result["signal"],
                "price": result["price"],
            })
            state[display] = result["signal"]
        elif result["signal"] == "NEUTRAL":
            state[display] = "NEUTRAL"
        else:
            print(f"[{display}] 前回と同じシグナル({result['signal']})のため通知をスキップしました")

        time.sleep(0.5)  # yfinanceへの連続リクエストを緩やかにする

    save_state(state)
    ACTIONABLE_FILE.write_text(json.dumps(actionable, ensure_ascii=False, indent=2))
    generate_status_page(all_results)
    print(f"\n発注対象シグナル数: {len(actionable)}")


if __name__ == "__main__":
    main()
