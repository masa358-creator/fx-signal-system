"""
cTrader Open API 経由でシグナルに応じた成行注文を出すスクリプト(デモ口座想定)

fx_signal_checker.py が判定した結果(環境変数 FX_SIGNAL)を受け取って発注します。
GitHub Actionsでは同じジョブの前のステップが FX_SIGNAL を出力する前提です。

環境変数:
  CTRADER_CLIENT_ID       必須。アプリ登録時のClient ID
  CTRADER_CLIENT_SECRET   必須。アプリ登録時のClient Secret
  CTRADER_ACCESS_TOKEN    必須。oauth_get_token.pyで取得したAccess Token
  CTRADER_ACCOUNT_ID      必須。list_accounts.pyで確認したctidTraderAccountId
  CTRADER_ENV             任意。"demo"(デフォルト) または "live"
  CTRADER_SYMBOL          任意。デフォルト "EURUSD"(ブローカーの銘柄名表記に合わせる。例 "USDJPY")
  ORDER_VOLUME_LOTS       任意。デフォルト 0.01(ロット数。最初は最小ロットで検証してください)
  ENABLE_TRADING          任意。"true" にしない限り発注せずログ出力のみ(安全装置)
  FX_SIGNAL               fx_signal_checker.py から渡される "BUY" / "SELL" / "NEUTRAL"

注意:
  volumeの単位換算(1ロット=100,000通貨として計算)は一般的な設定ですが、
  ブローカーやシンボルによってlotSizeが異なる場合があります。
  本番相当の金額で使う前に、必ず最小ロットで発注結果を確認してください。
"""

import os
import sys

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
SYMBOL_NAME = os.environ.get("CTRADER_SYMBOL", "EURUSD")
VOLUME_LOTS = float(os.environ.get("ORDER_VOLUME_LOTS", "0.01"))
ENABLE_TRADING = os.environ.get("ENABLE_TRADING", "false").lower() == "true"
SIGNAL = os.environ.get("FX_SIGNAL", "NEUTRAL")

if SIGNAL not in ("BUY", "SELL"):
    print(f"シグナルが BUY/SELL ではないため終了します(受け取った値: {SIGNAL})")
    sys.exit(0)

host = EndPoints.PROTOBUF_DEMO_HOST if ENV == "demo" else EndPoints.PROTOBUF_LIVE_HOST
client = Client(host, EndPoints.PROTOBUF_PORT, TcpProtocol)


def volume_to_cents(lots: float) -> int:
    units = lots * 100000
    return int(units * 100)


def stop_reactor() -> None:
    if reactor.running:
        reactor.stop()


def on_error(failure) -> None:
    print("エラー:", failure)
    stop_reactor()


def on_order_response(response) -> None:
    print("発注完了:", Protobuf.extract(response))
    stop_reactor()


def send_order(symbol_id: int) -> None:
    if not ENABLE_TRADING:
        print(f"[ドライラン] ENABLE_TRADING=false のため発注をスキップ(判定: {SIGNAL})")
        stop_reactor()
        return

    request = ProtoOANewOrderReq()
    request.ctidTraderAccountId = ACCOUNT_ID
    request.symbolId = symbol_id
    request.orderType = ProtoOAOrderType.MARKET
    request.tradeSide = ProtoOATradeSide.BUY if SIGNAL == "BUY" else ProtoOATradeSide.SELL
    request.volume = volume_to_cents(VOLUME_LOTS)

    deferred = client.send(request)
    deferred.addCallbacks(on_order_response, on_error)


def on_symbols_response(response) -> None:
    message = Protobuf.extract(response)
    symbol_id = None
    for symbol in message.symbol:
        if symbol.symbolName == SYMBOL_NAME:
            symbol_id = symbol.symbolId
            break

    if symbol_id is None:
        print(f"シンボル '{SYMBOL_NAME}' が見つかりません。ブローカー側の銘柄名表記を確認してください。")
        stop_reactor()
        return

    send_order(symbol_id)


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
    print("接続完了")
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
