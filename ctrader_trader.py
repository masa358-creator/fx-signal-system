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
TP_MULTIPLIER = float(os.environ.get("TP_MULTIPLIER", "2"))
MAX_CONCURRENT_POSITIONS = int(os.environ.get("MAX_CONCURRENT_POSITIONS", "3"))
MAX_ORDERS_PER_DAY = int(os.environ.get("MAX_ORDERS_PER_DAY", "10"))
DAILY_LOSS_LIMIT_JPY = float(os.environ.get("DAILY_LOSS_LIMIT_JPY", "1000"))
ENABLE_TRADING = os.environ.get("ENABLE_TRADING", "false").lower() == "true"

ACTIONABLE_FILE = Path(__file__).parent / "actionable_signals.json"
TRADE_LOG_FILE = Path(__file__).parent / "trade_log.json"

if ACTIONABLE_FILE.exists():
    SIGNALS = json.loads(ACTIONABLE_FILE.read_text())
else:
    SIGNALS = []

if not SIGNALS:
    print("発注対象のシグナルがないため終了します")
    raise SystemExit(0)


def pip_size(symbol_name: str) -> float:
    return 0.01 if "JPY" in symbol_name else 0.0001


def price_digits(symbol_name: str) -> int:
    return 3 if "JPY" in symbol_name else 5


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


def send_order(symbol_name: str, side: str, entry_price: float, symbol_id: int, lot: float, rank: str) -> None:
    if not ENABLE_TRADING:
        print(f"[ドライラン] ENABLE_TRADING=false のため発注をスキップ({symbol_name} {side} {rank}ランク {lot}lot)")
        maybe_finish()
        return

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

    request = ProtoOANewOrderReq()
    request.ctidTraderAccountId = ACCOUNT_ID
    request.symbolId = symbol_id
    request.orderType = ProtoOAOrderType.MARKET
    request.tradeSide = ProtoOATradeSide.BUY if side == "BUY" else ProtoOATradeSide.SELL
    request.volume = volume_to_cents(lot)
    request.stopLoss = stop_loss
    request.takeProfit = take_profit

    print(f"[発注] {symbol_name} {side} {rank}ランク volume={lot}lot SL={stop_loss}({sl_pips}pips) TP={take_profit}({tp_pips}pips)")

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
        symbol_id = name_to_id.get(symbol_name)
        if symbol_id is None:
            print(f"シンボル '{symbol_name}' がブローカー側に見つかりません。スキップします。")
            continue
        orders_to_place.append((symbol_name, side, entry_price, symbol_id, lot, rank))

    if not orders_to_place:
        print("発注可能な注文がありませんでした")
        stop_reactor()
        return

    pending_count = len(orders_to_place)
    for symbol_name, side, entry_price, symbol_id, lot, rank in orders_to_place:
        send_order(symbol_name, side, entry_price, symbol_id, lot, rank)


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
