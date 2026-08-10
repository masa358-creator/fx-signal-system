"""
cTrader Open API の OAuth 認可コードを Access Token / Refresh Token に交換する
一回限りのセットアップ用スクリプト(ローカルで実行してください)

事前準備:
  1. https://openapi.ctrader.com/ で "Applications" からアプリを登録
     (Spotwareによる承認が入るため、即時には使えない場合があります)
  2. アプリ設定で Redirect URI に http://localhost/ を追加登録
  3. Client ID / Client Secret を控える

使い方:
  python oauth_get_token.py
  1. 表示されたURLをブラウザで開き、cTrader IDでログインして権限を許可
  2. リダイレクト先URL(http://localhost/?code=xxxxx のようなURL、
     ページは表示されなくてOK)をブラウザのアドレスバーからコピー
  3. ターミナルに貼り付けてEnter
  4. Access Token / Refresh Token が表示されるので、GitHub Secretsに保存
"""

from urllib.parse import parse_qs, urlparse

import requests

AUTH_BASE_URL = "https://connect.spotware.com/apps/auth"
TOKEN_URL = "https://openapi.ctrader.com/apps/token"


def main() -> None:
    client_id = input("Client ID: ").strip()
    client_secret = input("Client Secret: ").strip()
    redirect_uri = input("Redirect URI (例 http://localhost/): ").strip()

    auth_url = (
        f"{AUTH_BASE_URL}?client_id={client_id}"
        f"&redirect_uri={redirect_uri}&scope=trading"
    )
    print("\n以下のURLをブラウザで開いてログイン・認可してください:\n")
    print(auth_url)
    print(
        "\n認可後にリダイレクトされたURL(ページが表示されなくてもOK)を"
        "アドレスバーからコピーして貼り付けてください。"
    )

    redirected_url = input("\nリダイレクト後のURL: ").strip()
    query = parse_qs(urlparse(redirected_url).query)
    if "code" not in query:
        print("URLに code パラメータが見つかりませんでした。もう一度確認してください。")
        return
    code = query["code"][0]

    resp = requests.get(
        TOKEN_URL,
        params={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        headers={"Accept": "application/json"},
        timeout=10,
    )
    resp.raise_for_status()
    tokens = resp.json()

    print("\n=== 取得結果(この値をGitHub Secretsに保存してください) ===")
    print(f"CTRADER_ACCESS_TOKEN = {tokens.get('accessToken')}")
    print(f"CTRADER_REFRESH_TOKEN = {tokens.get('refreshToken')}")
    print(
        "\n続けて list_accounts.py を実行すると、取引口座ID(ctidTraderAccountId)を確認できます。"
    )


if __name__ == "__main__":
    main()
