"""
バックテスト: fx_signal_checker.py / ctrader_trader.py の現行ロジックを過去データで再現し、
上位足トレンドフィルタ・最低ランクの組み合わせごとに成績を比較する。

使い方:
  pip install -r requirements.txt
  python backtest.py                 # 実データ(yfinance。1時間足は直近約730日まで)
  python backtest.py --selftest      # 合成データで動作確認(ネット不要)
  python backtest.py --symbols BTC-USD,ETH-USD,USDJPY=X --out results

出力: 画面に比較表、--out ディレクトリに summary.csv / trades_<variant>.csv

現行ロジックの再現ポイント:
  - 1時間足でMA(20/75)クロス・RSI(14)・MACD・ボリンジャー(20,2σ)をスコアリング
    (2条件=A、3条件以上=S、1条件=Bは発注しない)
  - 4時間足(1時間足のリサンプル)と日足のMA20/MA75の位置関係がシグナル方向と一致した時のみ発注
  - 同じ「シグナル:ランク」が続く間は再発注しない(state.jsonの挙動。フィルタで見送られた場合も
    stateは更新される=後でトレンドが揃っても同じキーなら発注されない、という現行仕様も再現)
  - SL/TP: FXはSランク20pips・Aランク40pips、TP=SL×2。暗号資産はSL1.5%・TP3%
  - 同時保有3件まで、1日10件まで。同一銘柄は1ポジションまで

保守的な仮定(実際より成績が良く見えないように):
  - エントリーはシグナルが出た足の「次の足の始値」
  - 同じ足でSLとTPの両方に届いた場合はSLを先に判定
  - スプレッド・手数料・スワップは未考慮(結果はその分だけ楽観的)
  - 日次損失上限(円)は未再現(R倍数で評価するため)
損益は「R倍数」(SL=-1R、TP=+TP_MULT R)で評価する。ロット・円換算には依存しない。
"""

import argparse
import itertools
from pathlib import Path

import numpy as np
import pandas as pd

FX_SYMBOLS = [
    "USDJPY=X", "EURJPY=X", "GBPJPY=X", "AUDJPY=X", "NZDJPY=X", "CADJPY=X", "CHFJPY=X",
    "EURUSD=X", "GBPUSD=X", "AUDUSD=X", "NZDUSD=X", "USDCAD=X", "USDCHF=X",
    "EURGBP=X", "EURAUD=X", "EURCHF=X", "GBPCHF=X", "AUDNZD=X",
]
# 発注対象の暗号資産(現行はBTC・ETHのみ発注)
CRYPTO_SYMBOLS = ["BTC-USD", "ETH-USD"]

SL_PIPS = {"S": 20.0, "A": 40.0}
CRYPTO_SL_PCT = 0.015
TP_MULT = 2.0
MAX_CONCURRENT = 3
MAX_PER_DAY = 10
MAX_HOLD_BARS = 24 * 14  # 2週間で未決済なら終値で強制決済(現行は期限なしのため目安)

RANK_ORDER = {"S": 0, "A": 1, "B": 2}


# ---------------------------------------------------------------- indicators
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    close = df["Close"]
    df["MA_short"] = close.rolling(20).mean()
    df["MA_long"] = close.rolling(75).mean()

    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    rs = gain.rolling(14).mean() / loss.rolling(14).mean()
    df["RSI"] = 100 - (100 / (1 + rs))

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    df["MACD"] = ema12 - ema26
    df["MACD_signal"] = df["MACD"].ewm(span=9, adjust=False).mean()

    mid = close.rolling(20).mean()
    std = close.rolling(20).std()
    df["BB_upper"] = mid + 2 * std
    df["BB_lower"] = mid - 2 * std
    return df


def score_all(df: pd.DataFrame) -> pd.DataFrame:
    """全バーについて score_signal と同じ判定をベクトル化して計算する"""
    p = df.shift(1)
    buy = pd.DataFrame(index=df.index)
    sell = pd.DataFrame(index=df.index)

    buy["ma"] = (p["MA_short"] <= p["MA_long"]) & (df["MA_short"] > df["MA_long"])
    sell["ma"] = (p["MA_short"] >= p["MA_long"]) & (df["MA_short"] < df["MA_long"])
    buy["rsi"] = df["RSI"] < 30
    sell["rsi"] = df["RSI"] > 70
    buy["macd"] = (p["MACD"] <= p["MACD_signal"]) & (df["MACD"] > df["MACD_signal"])
    sell["macd"] = (p["MACD"] >= p["MACD_signal"]) & (df["MACD"] < df["MACD_signal"])
    buy["bb"] = df["Close"] <= df["BB_lower"]
    sell["bb"] = df["Close"] >= df["BB_upper"]

    bc = buy.sum(axis=1)
    sc = sell.sum(axis=1)
    signal = np.where((bc > sc) & (bc >= 1), "BUY", np.where((sc > bc) & (sc >= 1), "SELL", "NEUTRAL"))
    match = np.where(signal == "BUY", bc, np.where(signal == "SELL", sc, 0))
    rank = np.where(match >= 3, "S", np.where(match == 2, "A", np.where(match == 1, "B", "")))
    out = pd.DataFrame({"signal": signal, "rank": rank, "match": match}, index=df.index)
    valid = df[["MA_short", "MA_long", "RSI", "MACD", "MACD_signal", "BB_upper"]].notna().all(axis=1)
    p_valid = valid.shift(1, fill_value=False)
    out.loc[~(valid & p_valid), ["signal", "rank", "match"]] = ["NEUTRAL", "", 0]
    return out


def trend_series(df: pd.DataFrame) -> pd.Series:
    """MA20 vs MA75 でUP/DOWN/FLAT/UNKNOWN(determine_trendの全バー版)"""
    close = df["Close"]
    s, l = close.rolling(20).mean(), close.rolling(75).mean()
    t = pd.Series("FLAT", index=df.index)
    t[s > l] = "UP"
    t[s < l] = "DOWN"
    t[s.isna() | l.isna()] = "UNKNOWN"
    return t


def higher_tf_trends(df_1h: pd.DataFrame, df_1d: pd.DataFrame) -> pd.DataFrame:
    """各1時間足の時点で『すでに確定している』4時間足・日足のトレンドを割り当てる(先読み防止)"""
    h4 = df_1h[["Open", "High", "Low", "Close"]].resample("4h").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()
    t4 = trend_series(h4)
    # 4時間足は足の開始時刻で並ぶ。確定は開始+4時間後 → その時刻以降の1時間足から利用可能
    t4.index = t4.index + pd.Timedelta(hours=4)

    td = trend_series(df_1d)
    # 日足は当日終了後に確定 → 翌日0時以降の1時間足から利用可能
    td.index = td.index + pd.Timedelta(days=1)

    idx = df_1h.index
    tz = idx.tz
    if tz is not None:
        t4.index = t4.index.tz_convert(tz) if t4.index.tz is not None else t4.index.tz_localize(tz)
        td.index = td.index.tz_convert(tz) if td.index.tz is not None else td.index.tz_localize(tz)
    a4 = t4.reindex(idx, method="ffill").fillna("UNKNOWN")
    ad = td.reindex(idx, method="ffill").fillna("UNKNOWN")
    return pd.DataFrame({"t4h": a4, "tday": ad}, index=idx)


# ---------------------------------------------------------------- trade sim
def sl_distance(symbol: str, rank: str, price: float, is_crypto: bool) -> float:
    if is_crypto:
        return price * CRYPTO_SL_PCT
    pip = 0.01 if "JPY" in symbol else 0.0001
    return SL_PIPS[rank] * pip


def simulate_exit(df: pd.DataFrame, entry_i: int, side: str, entry: float, risk: float):
    """entry_i(エントリー足)以降でSL/TPを判定。戻り値: (exit_i, R倍数, 理由)"""
    sl = entry - risk if side == "BUY" else entry + risk
    tp = entry + risk * TP_MULT if side == "BUY" else entry - risk * TP_MULT
    hi, lo, cl = df["High"].values, df["Low"].values, df["Close"].values
    end = min(len(df) - 1, entry_i + MAX_HOLD_BARS)
    for j in range(entry_i, end + 1):
        hit_sl = lo[j] <= sl if side == "BUY" else hi[j] >= sl
        hit_tp = hi[j] >= tp if side == "BUY" else lo[j] <= tp
        if hit_sl:  # 同一足で両方到達 → SL優先(保守的)
            return j, -1.0, "SL"
        if hit_tp:
            return j, TP_MULT, "TP"
    r = ((cl[end] - entry) if side == "BUY" else (entry - cl[end])) / risk
    return end, float(r), "TIMEOUT" if end - entry_i >= MAX_HOLD_BARS else "OPEN"


def build_candidates(symbol: str, df_1h: pd.DataFrame, df_1d: pd.DataFrame, is_crypto: bool) -> pd.DataFrame:
    """フィルタ設定に依らず、現行のstate挙動で『通知が出る足』をすべて列挙し、各トレードの結果も計算する"""
    df = add_indicators(df_1h.copy())
    sc = score_all(df)
    tr = higher_tf_trends(df_1h, df_1d)

    # 現行main(): signal != NEUTRAL かつ key が前回と違う時だけ通知/発注判定。NEUTRALでstateリセット。
    rows = []
    last_key = "NEUTRAL"
    sig, rk = sc["signal"].values, sc["rank"].values
    for i in range(len(df)):
        if sig[i] == "NEUTRAL":
            last_key = "NEUTRAL"
            continue
        key = f"{sig[i]}:{rk[i]}"
        if key == last_key:
            continue
        last_key = key  # 発注可否に関わらず更新(現行仕様)
        if rk[i] not in ("S", "A", "B") or i + 1 >= len(df):
            continue
        entry_i = i + 1
        entry = float(df["Open"].iloc[entry_i])
        risk = sl_distance(symbol, rk[i] if rk[i] in SL_PIPS else "A", entry, is_crypto)
        exit_i, r, why = simulate_exit(df, entry_i, sig[i], entry, risk)
        rows.append({
            "symbol": symbol, "signal_time": df.index[i], "entry_time": df.index[entry_i],
            "exit_time": df.index[exit_i], "side": sig[i], "rank": rk[i],
            "t4h": tr["t4h"].iloc[i], "tday": tr["tday"].iloc[i],
            "entry": entry, "risk": risk, "R": r, "exit_reason": why,
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- portfolio
def passes_filter(row, mode: str) -> bool:
    need = "UP" if row["side"] == "BUY" else "DOWN"
    if mode == "both":
        return row["t4h"] == need and row["tday"] == need
    if mode == "daily":
        return row["tday"] == need
    if mode == "h4":
        return row["t4h"] == need
    return True  # none


def run_portfolio(cands: pd.DataFrame, filter_mode: str, min_rank: str) -> pd.DataFrame:
    """銘柄横断で同時保有上限・1日上限を適用して、実際に取引された分を返す"""
    if cands.empty:
        return cands
    c = cands[cands["rank"].map(RANK_ORDER) <= RANK_ORDER[min_rank]]
    if c.empty:
        return c
    mask = [passes_filter(r, filter_mode) for _, r in c.iterrows()]
    c = c[mask].sort_values("entry_time")
    taken, open_pos, per_day = [], [], {}
    for _, row in c.iterrows():
        t = row["entry_time"]
        open_pos = [p for p in open_pos if p["exit_time"] > t]
        day = t.date()
        if len(open_pos) >= MAX_CONCURRENT or per_day.get(day, 0) >= MAX_PER_DAY:
            continue
        if any(p["symbol"] == row["symbol"] for p in open_pos):
            continue
        open_pos.append(row)
        per_day[day] = per_day.get(day, 0) + 1
        taken.append(row)
    return pd.DataFrame(taken)


def summarize(trades: pd.DataFrame, months: float) -> dict:
    if trades.empty:
        return {"trades": 0, "per_month": 0, "win_rate_%": np.nan, "avg_R": np.nan,
                "total_R": 0.0, "profit_factor": np.nan, "max_DD_R": 0.0}
    r = trades.sort_values("exit_time")["R"].values
    wins, losses = r[r > 0].sum(), -r[r < 0].sum()
    equity = np.cumsum(r)
    dd = (np.maximum.accumulate(np.concatenate([[0], equity]))[1:] - equity).max()
    return {
        "trades": len(r), "per_month": round(len(r) / months, 1),
        "win_rate_%": round((r > 0).mean() * 100, 1), "avg_R": round(r.mean(), 3),
        "total_R": round(r.sum(), 1),
        "profit_factor": round(wins / losses, 2) if losses > 0 else np.inf,
        "max_DD_R": round(dd, 1),
    }


# ---------------------------------------------------------------- data
def fetch(symbol: str, interval: str, period: str) -> pd.DataFrame:
    import yfinance as yf
    df = yf.download(symbol, interval=interval, period=period, progress=False, auto_adjust=False)
    if df.empty:
        raise RuntimeError(f"データ取得失敗: {symbol} {interval}")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df[["Open", "High", "Low", "Close"]].dropna()


def synthetic(symbol: str, seed: int, hours: int = 24 * 400):
    """動作確認用の合成データ(トレンド+ノイズ)。成績の意味は無い。"""
    rng = np.random.default_rng(seed)
    base = 150.0 if "JPY" in symbol else (60000.0 if "BTC" in symbol else 1.1)
    drift = np.cumsum(rng.normal(0, 0.0004, hours)) + np.sin(np.arange(hours) / 700) * 0.05
    close = base * np.exp(drift + rng.normal(0, 0.0015, hours).cumsum() * 0.2)
    idx = pd.date_range("2025-01-01", periods=hours, freq="1h", tz="UTC")
    op = np.concatenate([[close[0]], close[:-1]])
    spread = np.abs(rng.normal(0, 0.0012, hours)) * close
    df = pd.DataFrame({"Open": op, "Close": close,
                       "High": np.maximum(op, close) + spread, "Low": np.minimum(op, close) - spread}, index=idx)
    d = df.resample("1D").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()
    return df, d


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=",".join(FX_SYMBOLS + CRYPTO_SYMBOLS))
    ap.add_argument("--period-1h", default="730d")
    ap.add_argument("--period-1d", default="5y")
    ap.add_argument("--out", default="backtest_results")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    symbols = [s.strip() for s in a.symbols.split(",") if s.strip()]
    if a.selftest:
        symbols = ["USDJPY=X", "EURUSD=X", "BTC-USD"]
    cands = []
    start, end = None, None
    for k, s in enumerate(symbols):
        is_crypto = s.endswith("-USD")
        try:
            if a.selftest:
                d1h, d1d = synthetic(s, seed=k)
            else:
                d1h, d1d = fetch(s, "1h", a.period_1h), fetch(s, "1d", a.period_1d)
        except Exception as e:
            print(f"[{s}] スキップ: {e}")
            continue
        c = build_candidates(s, d1h, d1d, is_crypto)
        cands.append(c)
        start = d1h.index[0] if start is None else min(start, d1h.index[0])
        end = d1h.index[-1] if end is None else max(end, d1h.index[-1])
        print(f"[{s}] 候補シグナル {len(c)}件 (S/A/B全て・フィルタ前)")
    cands = pd.concat([c for c in cands if not c.empty], ignore_index=True) if cands else pd.DataFrame()
    if cands.empty:
        print("候補が0件でした")
        return
    months = max((end - start).days / 30.4, 1)
    out = Path(a.out)
    out.mkdir(exist_ok=True)

    rows = []
    for fm, mr in itertools.product(["both", "daily", "h4", "none"], ["S", "A", "B"]):
        t = run_portfolio(cands, fm, mr)
        label = f"filter={fm}/min_rank={mr}"
        if not t.empty:
            t.to_csv(out / f"trades_{fm}_{mr}.csv", index=False)
        rows.append({"variant": label + (" ←現行" if (fm, mr) == ("both", "A") else ""), **summarize(t, months)})
    summary = pd.DataFrame(rows)
    summary.to_csv(out / "summary.csv", index=False)
    print(f"\n期間: {start:%Y-%m-%d} 〜 {end:%Y-%m-%d} ({months:.1f}か月)\n")
    print(summary.to_string(index=False))
    if a.selftest:
        print("\n※--selftest は合成データです。成績の数値に意味はありません(動作確認専用)。")


if __name__ == "__main__":
    main()
