# FX 売買タイミング自動判定システム(無料構成プロトタイプ)

GitHub Actions + Python + Discord Webhook だけで完結する、課金要素なしの構成です。

## 何をするか

30分おきに GitHub Actions がドル円のレートを取得し、以下4つの指標でスコアリングして
「買い」「売り」シグナルが出たら Discord に通知します。

- 移動平均クロス(20期間 / 75期間)
- RSI(14期間、30以下=買われすぎ売り、70以上=売られすぎ買い、の逆)
- MACD クロス(12, 26, 9)
- ボリンジャーバンド(20期間, ±2σ)タッチ

同じシグナルが続く間は再通知しません(`state.json` に前回シグナルを保存)。

## セットアップ手順

1. **GitHubリポジトリを作成**し、このフォルダ一式をpush(publicリポジトリなら
   Actionsの実行時間が実質無制限で完全無料)

2. **Discord Webhook URLを発行**
   - Discordサーバーの「サーバー設定」→「連携サービス」→「Webhook」→「新しいWebhook」
   - Webhook URL をコピー

3. **GitHub Secretsに登録**
   - リポジトリの `Settings` → `Secrets and variables` → `Actions` → `New repository secret`
   - Name: `DISCORD_WEBHOOK_URL`、Value: 発行したWebhook URL

4. **動作確認**
   - `Actions` タブ → `FX signal check` → `Run workflow` で手動実行して確認
   - 以降は30分おきに自動実行されます

## ローカルで試す場合

```bash
pip install -r requirements.txt
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/xxxx"
python fx_signal_checker.py
```

## カスタマイズ

`.github/workflows/fx-signal-check.yml` の `env` を変更することで、通貨ペアや
時間足、閾値を調整できます。

- `FX_SYMBOL`: 例 `EURUSD=X`、`GBPJPY=X` など(Yahoo Financeのティッカー表記)
- `FX_INTERVAL`: `1h`, `30m`, `15m` など(短い足ほど `FX_PERIOD` を短くする必要あり)
- `SIGNAL_THRESHOLD`: スコアの閾値(デフォルト2。上げるとダマシが減るが検出頻度も下がる)

## cTrader Open API で自動発注を追加する(無料・デモ口座)

OANDA証券はデモ口座でのAPI利用ができないため、**現在の推奨構成はこちらです**。
cTraderを採用している海外ブローカー(Pepperstone、IC Markets、FxProなど)のデモ口座で、
無料のままAPI発注を試せます。

### 1. cTrader対応ブローカーでデモ口座を開設する

ブローカーのサイトで無料デモ口座を開設し、cTraderプラットフォームでログインできることを確認します。

### 2. Open APIアプリを登録する

1. https://openapi.ctrader.com/ にアクセスし、cTrader IDでログイン
2. 「Applications」から新しいアプリを登録(Spotwareの承認待ちになる場合があります)
3. アプリ設定で **Redirect URI** に `http://localhost/` を追加
4. 発行された **Client ID** / **Client Secret** を控える

### 3. Access Tokenを取得する(ローカルで一度だけ実行)

```bash
pip install -r requirements.txt
python oauth_get_token.py
```

指示に従いブラウザで認可し、表示されたAccess Token / Refresh Tokenを控えます。

### 4. 取引口座ID(ctidTraderAccountId)を確認する

```bash
python list_accounts.py
```

表示された中から、開設したデモ口座のIDを控えます。

### 5. GitHub Secretsに登録する

| Name | 値 |
|---|---|
| `CTRADER_CLIENT_ID` | アプリのClient ID |
| `CTRADER_CLIENT_SECRET` | アプリのClient Secret |
| `CTRADER_ACCESS_TOKEN` | 取得したAccess Token |
| `CTRADER_ACCOUNT_ID` | 確認したctidTraderAccountId |

### 6. 動作確認

`Actions` タブ → `FX signal check` → `Run workflow` で手動実行します。
`ENABLE_TRADING` が `false` のままなので、シグナルが出てもログに「ドライラン」と表示されるだけで、
実際には発注されません。

### 7. 実際にデモ口座で発注させる

ワークフローファイル内の `ENABLE_TRADING: "false"` を `"true"` に変更してpushします。
`CTRADER_SYMBOL` はブローカー側の銘柄名表記(例: `USDJPY`, `EURUSD`)に、
`ORDER_VOLUME_LOTS` は最初は最小ロット(`0.01`など)のまま検証してください。

### 注意点(cTrader版)

- Access Tokenには有効期限があります。期限切れ時は `CTRADER_REFRESH_TOKEN` を使った更新処理が別途必要です(本構成では未実装)
- volumeの単位換算は一般的な設定(1ロット=100,000通貨)を前提にしています。ブローカーやシンボルによって異なる場合があるため、必ず最小ロットで発注結果を確認してから運用してください
- 損切り・利確ラインの自動設定は本バージョンには含まれていません(必要であれば追加できます)

## OANDA Japan について(参考・現状は利用不可)

OANDA証券のREST APIは本番口座かつゴールド会員・NYサーバー(プロコース)・残高25万円以上が必要で、
無料のデモ環境では使えないことが判明したため、上記のcTrader構成を代わりに採用しています。
将来的に条件を満たす本番口座を用意する場合は、以下の手順が使えます。

### 1. デモ口座を開設する

1. [OANDA Japan](https://www.oanda.jp/) でデモ口座(fxTrade practice)を無料開設
2. マイページにログインし、「API」または「Manage API Access」から **Personal Access Token** を発行
3. 口座一覧から **Account ID**(例: `001-009-1234567-001`)を確認

### 2. GitHub Secretsに登録する

`Settings` → `Secrets and variables` → `Actions` で以下を追加します。

| Name | 値 |
|---|---|
| `OANDA_API_TOKEN` | 発行したPersonal Access Token |
| `OANDA_ACCOUNT_ID` | 確認したAccount ID |

### 3. 動作確認(発注はまだされません)

初期状態では `.github/workflows/fx-signal-check.yml` 内の `ENABLE_TRADING` が `"false"` になっているため、
シグナルが出てもDiscord通知のみで、実際の発注は行われません(ログに「ドライラン」と出力されます)。
Actionsのログでこの挙動を確認してください。

### 4. 実際にデモ口座で発注させる

動作に問題なければ、ワークフローファイルの `ENABLE_TRADING: "false"` を `"true"` に変更してpushします。
以降、BUY/SELLシグナルが出るたびにデモ口座へ成行注文(逆指値・利確ライン付き)が入ります。

### 5. 注文条件を調整する

同じくワークフローファイルの `env` で調整できます。

- `OANDA_INSTRUMENT`: 通貨ペア(OANDA表記、例 `EUR_USD`, `GBP_JPY`)
- `ORDER_UNITS`: 発注する通貨単位数(ロットではない。1000通貨などから開始推奨)
- `STOP_LOSS_PIPS` / `TAKE_PROFIT_PIPS`: 損切り・利確までの値幅(pips)
- `OANDA_ENV`: `practice`(デモ)のままにしておく。live口座に切り替える場合のみ `live` に変更(自己責任)

## 注意点

- yfinance は非公式のデータ取得ライブラリのため、個人の検証用途を想定しています
- 実際の売買判断は自己責任で行ってください。本システムはシグナルの目安を通知するのみです
- 短い時間足(5分足など)を使う場合は `FX_PERIOD` を短縮し、cronの間隔も短くする必要があります
- OANDAでの自動発注は、通信エラーやAPI仕様変更でも発注が失敗・重複する可能性があります。live口座に切り替える前に、必ずデモ口座で数週間以上の実運用検証を行ってください
