"""
cTrader Open API 経由で、複数通貨ペアのシグナルに応じた成行注文を出すスクリプト
ストップロス・テイクプロフィット・1日あたりの発注上限(安全装置)付き。

fx_signal_checker.py が書き出した actionable_signals.json
(例: [{"symbol": "USDJPY", "signal": "BUY", "price": 159.05}, ...])
を読み込み、該当する通貨ペアすべてに、損切り・利確ライン付きの成行注文を出します。

環境変数:
  CTRADER_CLIENT_ID       必須。アプリ登録時のClient ID
  CTRADER_CLIENT_SECRET   必須。アプリ登録時のClient Secret
  CTRADER_ACCESS_TOKEN    必須。取得したAccess Token
  CTRADER_ACCOUNT_ID      必須。ctidTraderAccountId
  CTRADER_ENV             任意。"demo"(デフォルト) または "live"
  ORDER_VOLUME_LOTS       任意。デフォルト 0.01(通貨ペア共通のロット数)
  STOP_LOSS_PIPS          任意。デフォルト 20 (損切りまでの値幅。pips単位)
  TAKE_PROFIT_PIPS        任意。デフォルト 40 (利確までの値幅。pips単位)
  MAX_ORDERS_PER_DAY      任意。デフォルト 10 (1日の発注件数の上限。安全装置)
  ENABLE_TRADING          任意。"true" にしない限り発注せずログ出力のみ(安全装置)

注意:
  - pipsの定義: JPYが絡むペアは1pip=0.01、それ以外は1pip=0.0001という
    一般的な業界慣習で計算しています。
  - volumeの単位換算(1ロット=100,000通貨として計算)は一般的な設定ですが、
    ブローカーやシンボルによってlotSizeが異なる場合があります。
  - 本番相当の金額で使う前に、必ず最小ロットで発注結果を確認してください。
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
    ProtoOASymbolsListReq,
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
VOLUME_LOTS = float(os.environ.get("ORDER_VOLUME_LOTS", "0.01"))
STOP_LOSS_PIPS = float(os.environ.get("STOP_LOSS_PIPS", "20"))
TAKE_PROFIT_PIPS = float(os.environ.get("TAKE_PROFIT_PIPS", "40"))
MAX_ORDERS_PER_DAY = int(os.environ.get("MAX_ORDERS_PER_DAY", "10"))
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
    """JPY絡みのペアは1pip=0.01、それ以外は1pip=0.0001とする業界慣習"""
    return 0.01 if "JPY" in symbol_name else 0.0001


def price_digits(symbol_name: str) -> int:
    return 3 if "JPY" in symbol_name else 5


def load_trade_log() -> dict:
    if TRADE_LOG_FILE.exists():
        data = json.loads(TRADE_LOG_FILE.read_text())
        if data.get("date") == str(date.today()):
            return data
    return {"date": str(date.today()), "count": 0}


def save_trade_log(log: dict) -> None:
    TRADE_LOG_FILE.write_text(json.dumps(log, ensure_ascii=False, indent=2))


trade_log = load_trade_log()
remaining_quota = max(0, MAX_ORDERS_PER_DAY - trade_log["count"])

if remaining_quota <= 0:
    print(f"[安全装置] 本日の発注上限({MAX_ORDERS_PER_DAY}件)に達しているため、これ以上は発注しません")
    raise SystemExit(0)

if len(SIGNALS) > remaining_quota:
    print(f"[安全装置] 本日の残り発注可能件数は{remaining_quota}件です。先頭{remaining_quota}件のみ処理します。")
    SIGNALS = SIGNALS[:remaining_quota]

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
    maybe_finish()


def on_order_response(response) -> None:
    global executed_count
    print("発注完了:", Protobuf.extract(response))
    executed_count += 1
    maybe_finish()


def send_order(symbol_name: str, side: str, entry_price: float, symbol_id: int) -> None:
    if not ENABLE_TRADING:
        print(f"[ドライラン] ENABLE_TRADING=false のため発注をスキップ({symbol_name} {side})")
        maybe_finish()
        return

    pip = pip_size(symbol_name)
    digits = price_digits(symbol_name)

    if side == "BUY":
        stop_loss = round(entry_price - STOP_LOSS_PIPS * pip, digits)
        take_profit = round(entry_price + TAKE_PROFIT_PIPS * pip, digits)
    else:
        stop_loss = round(entry_price + STOP_LOSS_PIPS * pip, digits)
        take_profit = round(entry_price - TAKE_PROFIT_PIPS * pip, digits)

    request = ProtoOANewOrderReq()
    request.ctidTraderAccountId = ACCOUNT_ID
    request.symbolId = symbol_id
    request.orderType = ProtoOAOrderType.MARKET
    request.tradeSide = ProtoOATradeSide.BUY if side == "BUY" else ProtoOATradeSide.SELL
    request.volume = volume_to_cents(VOLUME_LOTS)
    request.stopLoss = stop_loss
    request.takeProfit = take_profit

    print(f"[発注] {symbol_name} {side} volume={VOLUME_LOTS}lot SL={stop_loss} TP={take_profit}")

    deferred = client.send(request)
    deferred.addCallbacks(on_order_response, on_error)


def on_symbols_response(response) -> None:
    global pending_count
    message = Protobuf.extract(response)
    name_to_id = {symbol.symbolName: symbol.symbolId for symbol in message.symbol}

    orders_to_place = []
    for entry in SIGNALS:
        symbol_name = entry["symbol"]
        side = entry["signal"]
        entry_price = entry["price"]
        symbol_id = name_to_id.get(symbol_name)
        if symbol_id is None:
            print(f"シンボル '{symbol_name}' がブローカー側に見つかりません。スキップします。")
            continue
        orders_to_place.append((symbol_name, side, entry_price, symbol_id))

    if not orders_to_place:
        print("発注可能なシンボルがありませんでした")
        stop_reactor()
        return

    pending_count = len(orders_to_place)
    for symbol_name, side, entry_price, symbol_id in orders_to_place:
        send_order(symbol_name, side, entry_price, symbol_id)


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
    print(f"接続完了。対象シグナル({len(SIGNALS)}件、本日残り枠{remaining_quota}件):", SIGNALS)
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
