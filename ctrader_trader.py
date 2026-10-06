"""
cTrader Open API 経由で、複数通貨ペアのシグナルに応じた成行注文を出すスクリプト
ストップロス・テイクプロフィット・同時保有ポジション上限・1日の最大損失(円)による
安全装置つき。JPY建て口座を前提としています。

fx_signal_checker.py が書き出した actionable_signals.json
(例: [{"symbol": "USDJPY", "signal": "BUY", "price": 159.05, "lot": 0.01, "rank": "A"}, ...])
を読み込み、リスク管理のチェックを通過したものだけ発注します。

環境変数:
  CTRADER_CLIENT_ID          必須。アプリ登録時のClient ID
  CTRADER_CLIENT_SECRET      必須。アプリ登録時のClient Secret
  CTRADER_ACCESS_TOKEN       必須。取得したAccess Token
  CTRADER_ACCOUNT_ID         必須。ctidTraderAccountId
  CTRADER_ENV                任意。"demo"(デフォルト) または "live"
  STOP_LOSS_PIPS_S           任意。Sランク(0.02lot)の損切り値幅。デフォルト20pips(≈400円)
  STOP_LOSS_PIPS_A           任意。Aランク(0.01lot)の損切り値幅。デフォルト40pips(≈400円)
  TP_MULTIPLIER              任意。利確 = 損切り幅 × この倍率。デフォルト2
  MAX_CONCURRENT_POSITIONS   任意。同時保有ポジション数の上限。デフォルト3
  MAX_ORDERS_PER_DAY         任意。1日の発注件数の上限。デフォルト10
  DAILY_LOSS_LIMIT_JPY       任意。1日の最大損失(円)。デフォルト1000
  ENABLE_TRADING             任意。"true" にしない限り発注せずログ出力のみ(安全装置)

複数口座(ブローカー)で動かすための任意設定(未設定なら従来どおりの動作):
  ACCOUNT_LABEL              ログ表示用の口座名。デフォルト "main"
  TRADE_LOG_FILE_NAME        口座ごとの発注ログのファイル名。デフォルト "trade_log.json"
  ALLOWED_RANKS              発注するランク(カンマ区切り)。デフォルト "S,A"
  ALLOWED_ASSET_TYPES        発注する資産種別(カンマ区切り)。デフォルト "FX,CRYPTO"
  FORCE_LOT                  設定すると、ランクに関係なくFXのロットをこの値に固定(小資金口座用)
  VERIFY_CONNECTION          "true" の場合、シグナルが無くても接続して残高と銘柄名の有無を確認して終了

注意:
  - pips→円の換算は、JPY絡みの通貨ペアは正確ですが、それ以外は概算です
    (1pip ≈ 10円 × (lot÷0.01) という業界の目安値で計算しています)。
  - 1日の最大損失は「口座残高(確定損益)」の変化で判定するため、
    保有中の含み損はリアルタイムには反映されません。
  - volumeの単位換算(1ロット=100,000通貨)は一般的な設定ですが、
    ブローカーやシンボルによってlotSizeが異なる場合があります。
"""

import json
import os
from datetime import date
from pathlib import Path

from ctrader_open_api import Client, EndPoints, Protobuf, TcpProtocol
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAAccountAuthReq,
    ProtoOAApplicationAuthReq,
    ProtoOANewOrderReq,
    ProtoOAReconcileReq,
    ProtoOASymbolsListReq,
    ProtoOATraderReq,
)
from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
    ProtoOAOrderType,
    ProtoOATradeSide,
)
from twisted.internet import reactor

CLIENT_ID = os.environ["CTRADER_CLIENT_ID"]
CLIENT_SECRET = os.environ["CTRADER_CLIENT_SECRET"]
ACCESS_TOKEN = os.environ["CTRADER_ACCESS_TOKEN"]
ACCOUNT_ID = int(os.environ["CTRADER_ACCOUNT_ID"])
ENV = os.environ.get("CTRADER_ENV", "demo")

STOP_LOSS_PIPS_S = float(os.environ.get("STOP_LOSS_PIPS_S", "20"))
STOP_LOSS_PIPS_A = float(os.environ.get("STOP_LOSS_PIPS_A", "40"))
CRYPTO_STOP_LOSS_PCT = float(os.environ.get("CRYPTO_STOP_LOSS_PCT", "1.5")) / 100  # 暗号資産の損切り(%)
TP_MULTIPLIER = float(os.environ.get("TP_MULTIPLIER", "2"))
MAX_CONCURRENT_POSITIONS = int(os.environ.get("MAX_CONCURRENT_POSITIONS", "3"))
MAX_ORDERS_PER_DAY = int(os.environ.get("MAX_ORDERS_PER_DAY", "10"))
DAILY_LOSS_LIMIT_JPY = float(os.environ.get("DAILY_LOSS_LIMIT_JPY", "1000"))
ENABLE_TRADING = os.environ.get("ENABLE_TRADING", "false").lower() == "true"

ACCOUNT_LABEL = os.environ.get("ACCOUNT_LABEL", "main")
ALLOWED_RANKS = {r.strip() for r in os.environ.get("ALLOWED_RANKS", "S,A").split(",") if r.strip()}
ALLOWED_ASSET_TYPES = {t.strip() for t in os.environ.get("ALLOWED_ASSET_TYPES", "FX,CRYPTO").split(",") if t.strip()}
FORCE_LOT = float(os.environ["FORCE_LOT"]) if os.environ.get("FORCE_LOT") else None
VERIFY_CONNECTION = os.environ.get("VERIFY_CONNECTION", "false").lower() == "true"

# 接続確認モードで、ブローカー側に銘柄があるかを表示するための一覧
VERIFY_SYMBOLS = [
    "USDJPY", "EURJPY", "GBPJPY", "AUDJPY", "NZDJPY", "CADJPY", "CHFJPY",
    "EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDCAD", "USDCHF",
    "EURGBP", "EURAUD", "EURCHF", "GBPCHF", "AUDNZD", "BTCUSD", "ETHUSD",
]

ACTIONABLE_FILE = Path(__file__).parent / "actionable_signals.json"
TRADE_LOG_FILE = Path(__file__).parent / os.environ.get("TRADE_LOG_FILE_NAME", "trade_log.json")

print(f"[{ACCOUNT_LABEL}] 口座ID={ACCOUNT_ID} 環境={ENV} 発注={'有効' if ENABLE_TRADING else '無効(ドライラン)'}"
      f" 対象ランク={sorted(ALLOWED_RANKS)} 対象資産={sorted(ALLOWED_ASSET_TYPES)}"
      f" 固定ロット={FORCE_LOT if FORCE_LOT is not None else 'なし'}")

if ACTIONABLE_FILE.exists():
    ALL_SIGNALS = json.loads(ACTIONABLE_FILE.read_text())
else:
    ALL_SIGNALS = []

# この口座で発注対象にするランク・資産種別だけに絞る
SIGNALS = [
    s for s in ALL_SIGNALS
    if s.get("rank", "?") in ALLOWED_RANKS and s.get("asset_type", "FX") in ALLOWED_ASSET_TYPES
]
if len(SIGNALS) != len(ALL_SIGNALS):
    print(f"[{ACCOUNT_LABEL}] この口座の対象外のシグナルを除外しました({len(ALL_SIGNALS)}件 → {len(SIGNALS)}件)")

if not SIGNALS and not VERIFY_CONNECTION:
    print("発注対象のシグナルがないため終了します")
    raise SystemExit(0)


def pip_size(symbol_name: str) -> float:
    return 0.01 if "JPY" in symbol_name else 0.0001


def price_digits(symbol_name: str) -> int:
    return 3 if "JPY" in symbol_name else 5


def crypto_volume_to_cents(lots: float) -> int:
    """暗号資産は『1ロット=1コイン』(BTCUSD等で確認済み)。FXの10万通貨換算は使わない。"""
    return int(lots * 100)


def stop_loss_pips_for_rank(rank: str) -> float:
    return STOP_LOSS_PIPS_S if rank == "S" else STOP_LOSS_PIPS_A


def load_trade_log() -> dict:
    if TRADE_LOG_FILE.exists():
        data = json.loads(TRADE_LOG_FILE.read_text())
        if data.get("date") == str(date.today()):
            return data
    return {"date": str(date.today()), "count": 0, "start_balance_jpy": None}


def save_trade_log(log: dict) -> None:
    TRADE_LOG_FILE.write_text(json.dumps(log, ensure_ascii=False, indent=2))


trade_log = load_trade_log()

host = EndPoints.PROTOBUF_DEMO_HOST if ENV == "demo" else EndPoints.PROTOBUF_LIVE_HOST
client = Client(host, EndPoints.PROTOBUF_PORT, TcpProtocol)

pending_count = 0
executed_count = 0


def volume_to_cents(lots: float) -> int:
    units = lots * 100000
    return int(units * 100)


def stop_reactor() -> None:
    if reactor.running:
        reactor.stop()


def maybe_finish() -> None:
    global pending_count
    pending_count -= 1
    if pending_count <= 0:
        trade_log["count"] += executed_count
        save_trade_log(trade_log)
        stop_reactor()


def on_error(failure) -> None:
    print("エラー:", failure)
    stop_reactor()


def on_order_response(response) -> None:
    global executed_count
    print("発注完了:", Protobuf.extract(response))
    executed_count += 1
    maybe_finish()


def send_order(symbol_name: str, side: str, entry_price: float, symbol_id: int, lot: float, rank: str, asset_type: str) -> None:
    if not ENABLE_TRADING:
        print(f"[ドライラン] ENABLE_TRADING=false のため発注をスキップ({symbol_name} {side} {rank}ランク {lot}lot)")
        maybe_finish()
        return

    if asset_type == "CRYPTO":
        # 暗号資産は「1ロット=1コイン」。損切り・利確は価格に対する%で計算する。
        sl_pct = CRYPTO_STOP_LOSS_PCT
        tp_pct = sl_pct * TP_MULTIPLIER
        if side == "BUY":
            stop_loss = round(entry_price * (1 - sl_pct), 2)
            take_profit = round(entry_price * (1 + tp_pct), 2)
        else:
            stop_loss = round(entry_price * (1 + sl_pct), 2)
            take_profit = round(entry_price * (1 - tp_pct), 2)
        volume = crypto_volume_to_cents(lot)
        sl_label = f"{sl_pct*100:.1f}%"
        tp_label = f"{tp_pct*100:.1f}%"
    else:
        pip = pip_size(symbol_name)
        digits = price_digits(symbol_name)
        sl_pips = stop_loss_pips_for_rank(rank)
        tp_pips = sl_pips * TP_MULTIPLIER
        if side == "BUY":
            stop_loss = round(entry_price - sl_pips * pip, digits)
            take_profit = round(entry_price + tp_pips * pip, digits)
        else:
            stop_loss = round(entry_price + sl_pips * pip, digits)
            take_profit = round(entry_price - tp_pips * pip, digits)
        volume = volume_to_cents(lot)
        sl_label = f"{sl_pips}pips"
        tp_label = f"{tp_pips}pips"

    request = ProtoOANewOrderReq()
    request.ctidTraderAccountId = ACCOUNT_ID
    request.symbolId = symbol_id
    request.orderType = ProtoOAOrderType.MARKET
    request.tradeSide = ProtoOATradeSide.BUY if side == "BUY" else ProtoOATradeSide.SELL
    request.volume = volume
    request.stopLoss = stop_loss
    request.takeProfit = take_profit

    print(f"[発注] {symbol_name}({asset_type}) {side} {rank}ランク volume={lot}lot SL={stop_loss}({sl_label}) TP={take_profit}({tp_label})")

    deferred = client.send(request)
    deferred.addCallbacks(on_order_response, on_error)


def place_orders(name_to_id: dict, allowed_slots: int) -> None:
    global pending_count

    orders_to_place = []
    for entry in SIGNALS:
        if len(orders_to_place) >= allowed_slots:
            print(f"[安全装置] 発注可能枠({allowed_slots}件)に達したため、残りのシグナルはスキップします")
            break
        symbol_name = entry["symbol"]
        side = entry["signal"]
        entry_price = entry["price"]
        lot = entry.get("lot") or 0.01
        rank = entry.get("rank", "?")
        asset_type = entry.get("asset_type", "FX")
        if FORCE_LOT is not None and asset_type != "CRYPTO":
            lot = FORCE_LOT  # 小資金口座用: ランクに関係なくロットを固定
        symbol_id = name_to_id.get(symbol_name)
        if symbol_id is None:
            print(f"シンボル '{symbol_name}' がブローカー側に見つかりません。スキップします。")
            continue
        orders_to_place.append((symbol_name, side, entry_price, symbol_id, lot, rank, asset_type))

    if not orders_to_place:
        print("発注可能な注文がありませんでした")
        stop_reactor()
        return

    pending_count = len(orders_to_place)
    for symbol_name, side, entry_price, symbol_id, lot, rank, asset_type in orders_to_place:
        send_order(symbol_name, side, entry_price, symbol_id, lot, rank, asset_type)


def on_reconcile_response(response, name_to_id: dict) -> None:
    message = Protobuf.extract(response)
    open_position_count = len(message.position)
    print(f"現在の保有ポジション数: {open_position_count} / 上限{MAX_CONCURRENT_POSITIONS}")

    position_slots = max(0, MAX_CONCURRENT_POSITIONS - open_position_count)
    daily_order_slots = max(0, MAX_ORDERS_PER_DAY - trade_log["count"])
    allowed_slots = min(position_slots, daily_order_slots, len(SIGNALS))

    if position_slots <= 0:
        print(f"[安全装置] 同時保有ポジション数が上限({MAX_CONCURRENT_POSITIONS}件)に達しているため発注しません")
    if daily_order_slots <= 0:
        print(f"[安全装置] 本日の発注上限({MAX_ORDERS_PER_DAY}件)に達しているため発注しません")

    if allowed_slots <= 0:
        stop_reactor()
        return

    place_orders(name_to_id, allowed_slots)


def on_trader_response(response, name_to_id: dict) -> None:
    message = Protobuf.extract(response)
    current_balance_jpy = message.trader.balance / 100.0  # cTraderは残高をセント単位相当で返す
    print(f"現在の口座残高: {current_balance_jpy:.0f}円")

    if trade_log.get("start_balance_jpy") is None:
        trade_log["start_balance_jpy"] = current_balance_jpy
        print(f"本日の開始時点残高として記録: {current_balance_jpy:.0f}円")

    daily_loss = trade_log["start_balance_jpy"] - current_balance_jpy
    print(f"本日の損益: {-daily_loss:.0f}円(マイナスが損失)")

    if not SIGNALS:
        # 接続確認モード(VERIFY_CONNECTION)でシグナルが無い場合は、ここで確認だけして終了
        print(f"[{ACCOUNT_LABEL}] 接続確認のみ: 発注対象のシグナルが無いため、ここで終了します")
        save_trade_log(trade_log)
        stop_reactor()
        return

    if daily_loss >= DAILY_LOSS_LIMIT_JPY:
        print(f"[安全装置] 本日の最大損失({DAILY_LOSS_LIMIT_JPY:.0f}円)に達しているため、本日はこれ以上発注しません")
        save_trade_log(trade_log)
        stop_reactor()
        return

    save_trade_log(trade_log)

    request = ProtoOAReconcileReq()
    request.ctidTraderAccountId = ACCOUNT_ID
    deferred = client.send(request)
    deferred.addCallbacks(lambda r: on_reconcile_response(r, name_to_id), on_error)


def on_symbols_response(response) -> None:
    message = Protobuf.extract(response)
    name_to_id = {symbol.symbolName: symbol.symbolId for symbol in message.symbol}

    if VERIFY_CONNECTION:
        found = [n for n in VERIFY_SYMBOLS if n in name_to_id]
        missing = [n for n in VERIFY_SYMBOLS if n not in name_to_id]
        print(f"[{ACCOUNT_LABEL}] 銘柄の照合: 全{len(name_to_id)}銘柄中、監視対象{len(found)}/{len(VERIFY_SYMBOLS)}が一致")
        if missing:
            print(f"[{ACCOUNT_LABEL}] ブローカー側に見つからない銘柄(銘柄名の表記違いの可能性): {missing}")

    request = ProtoOATraderReq()
    request.ctidTraderAccountId = ACCOUNT_ID
    deferred = client.send(request)
    deferred.addCallbacks(lambda r: on_trader_response(r, name_to_id), on_error)


def on_account_auth_response(_response) -> None:
    print("口座認証成功")
    request = ProtoOASymbolsListReq()
    request.ctidTraderAccountId = ACCOUNT_ID
    deferred = client.send(request)
    deferred.addCallbacks(on_symbols_response, on_error)


def on_app_auth_response(_response) -> None:
    print("アプリ認証成功")
    request = ProtoOAAccountAuthReq()
    request.ctidTraderAccountId = ACCOUNT_ID
    request.accessToken = ACCESS_TOKEN
    deferred = client.send(request)
    deferred.addCallbacks(on_account_auth_response, on_error)


def connected(_client) -> None:
    print(f"接続完了。対象シグナル({len(SIGNALS)}件):", SIGNALS)
    request = ProtoOAApplicationAuthReq()
    request.clientId = CLIENT_ID
    request.clientSecret = CLIENT_SECRET
    deferred = client.send(request)
    deferred.addCallbacks(on_app_auth_response, on_error)


def disconnected(_client, reason) -> None:
    print("切断:", reason)


client.setConnectedCallback(connected)
client.setDisconnectedCallback(disconnected)
client.startService()
reactor.run()
