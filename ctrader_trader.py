"""
cTrader Open API 経由で、複数通貨ペアのシグナルに応じた成行注文を出すスクリプト(デモ口座想定)

fx_signal_checker.py が書き出した actionable_signals.json
(例: [{"symbol": "USDJPY", "signal": "BUY", "price": 159.05}, ...])
を読み込み、該当する通貨ペアすべてに成行注文を出します。

環境変数:
  CTRADER_CLIENT_ID       必須。アプリ登録時のClient ID
  CTRADER_CLIENT_SECRET   必須。アプリ登録時のClient Secret
  CTRADER_ACCESS_TOKEN    必須。取得したAccess Token
  CTRADER_ACCOUNT_ID      必須。ctidTraderAccountId
  CTRADER_ENV             任意。"demo"(デフォルト) または "live"
  ORDER_VOLUME_LOTS       任意。デフォルト 0.01(通貨ペア共通のロット数)
  ENABLE_TRADING          任意。"true" にしない限り発注せずログ出力のみ(安全装置)

注意:
  volumeの単位換算(1ロット=100,000通貨として計算)は一般的な設定ですが、
  ブローカーやシンボルによってlotSizeが異なる場合があります。
  本番相当の金額で使う前に、必ず最小ロットで発注結果を確認してください。
"""

import json
import os
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
ENABLE_TRADING = os.environ.get("ENABLE_TRADING", "false").lower() == "true"

ACTIONABLE_FILE = Path(__file__).parent / "actionable_signals.json"

if ACTIONABLE_FILE.exists():
    SIGNALS = json.loads(ACTIONABLE_FILE.read_text())
else:
    SIGNALS = []

if not SIGNALS:
    print("発注対象のシグナルがないため終了します")
    raise SystemExit(0)

host = EndPoints.PROTOBUF_DEMO_HOST if ENV == "demo" else EndPoints.PROTOBUF_LIVE_HOST
client = Client(host, EndPoints.PROTOBUF_PORT, TcpProtocol)

pending_count = 0


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
        stop_reactor()


def on_error(failure) -> None:
    print("エラー:", failure)
    maybe_finish()


def on_order_response(response) -> None:
    print("発注完了:", Protobuf.extract(response))
    maybe_finish()


def send_order(symbol_name: str, side: str, symbol_id: int) -> None:
    if not ENABLE_TRADING:
        print(f"[ドライラン] ENABLE_TRADING=false のため発注をスキップ({symbol_name} {side})")
        maybe_finish()
        return

    request = ProtoOANewOrderReq()
    request.ctidTraderAccountId = ACCOUNT_ID
    request.symbolId = symbol_id
    request.orderType = ProtoOAOrderType.MARKET
    request.tradeSide = ProtoOATradeSide.BUY if side == "BUY" else ProtoOATradeSide.SELL
    request.volume = volume_to_cents(VOLUME_LOTS)

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
        symbol_id = name_to_id.get(symbol_name)
        if symbol_id is None:
            print(f"シンボル '{symbol_name}' がブローカー側に見つかりません。スキップします。")
            continue
        orders_to_place.append((symbol_name, side, symbol_id))

    if not orders_to_place:
        print("発注可能なシンボルがありませんでした")
        stop_reactor()
        return

    pending_count = len(orders_to_place)
    for symbol_name, side, symbol_id in orders_to_place:
        send_order(symbol_name, side, symbol_id)


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
    print("接続完了。対象シグナル:", SIGNALS)
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
