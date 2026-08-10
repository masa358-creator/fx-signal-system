"""
Access Token に紐づく取引口座一覧(ctidTraderAccountId)を確認するスクリプト
oauth_get_token.py で取得した Access Token を使って実行してください。

使い方:
  python list_accounts.py
"""

from ctrader_open_api import Client, EndPoints, Protobuf, TcpProtocol
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAApplicationAuthReq,
    ProtoOAGetAccountListByAccessTokenReq,
)
from twisted.internet import reactor


def main() -> None:
    client_id = input("Client ID: ").strip()
    client_secret = input("Client Secret: ").strip()
    access_token = input("Access Token: ").strip()
    env = input("環境 (demo/live) [demo]: ").strip().lower() or "demo"

    host = EndPoints.PROTOBUF_DEMO_HOST if env == "demo" else EndPoints.PROTOBUF_LIVE_HOST
    client = Client(host, EndPoints.PROTOBUF_PORT, TcpProtocol)

    def on_account_list(response):
        message = Protobuf.extract(response)
        print("\n=== 取引口座一覧 ===")
        for account in message.ctidTraderAccount:
            print(
                f"ctidTraderAccountId = {account.ctidTraderAccountId}"
                f"  (live: {account.isLive})"
            )
        print("\n該当するIDを CTRADER_ACCOUNT_ID として GitHub Secretsに保存してください。")
        reactor.stop()

    def on_error(failure):
        print("エラー:", failure)
        reactor.stop()

    def on_app_auth(_response):
        request = ProtoOAGetAccountListByAccessTokenReq()
        request.accessToken = access_token
        deferred = client.send(request)
        deferred.addCallbacks(on_account_list, on_error)

    def connected(_client):
        request = ProtoOAApplicationAuthReq()
        request.clientId = client_id
        request.clientSecret = client_secret
        deferred = client.send(request)
        deferred.addCallbacks(on_app_auth, on_error)

    def disconnected(_client, reason):
        print("切断:", reason)

    client.setConnectedCallback(connected)
    client.setDisconnectedCallback(disconnected)
    client.startService()
    reactor.run()


if __name__ == "__main__":
    main()
