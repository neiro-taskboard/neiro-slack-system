# neiro-slack-system

Slack Socket Mode を使った、ねいろ（Muse）の自動受信・自動返信システム一式。

## 構成

- `listener/` — Socket Mode リスナー本体
  - `listener.py` — 両ワークスペースに常時接続し、新着を `events/inbox.jsonl` に記録
  - `backfill.py` — 停止中に投稿されたメッセージを履歴から拾い直す
  - `archive.py` — 30日超のメッセージを月次で圧縮アーカイブ
  - `watchdog.sh` — リスナーの監視・復旧（ユニット再インストール、timers.target ガード）
  - `slack-listener.service` — systemd ユニット
- `hooks/` — 新着検知と返信まわり
  - `scripts/slack-poll.py` — イベント状態管理（pending/processing/done/failed/dead、flock、再試行）
  - `scripts/slack-send-once.sh` — 重複ガード付き送信ラッパー（送信台帳、fail-closed、DLQ再送対応）
  - `scripts/slack-event-state.sh` — イベント完了/失敗の記録
  - `scripts/slack-reconcile-done.sh` — done レコードの古いエラー残りを掃除
  - `scripts/slack-new-messages.sh` — ポーリング入口
  - `definitions/slack-new-messages.json` — フック定義
  - `tests/` — テスト（`test_slack_poll.py`、`run_tests.py`）

## 秘密情報について

トークン等の秘密情報はこのリポジトリに含めていません。
アプリレベルトークン（xapp）は実行環境の `config.json`（権限 0600）から読み込む前提で、
`config.json` 自体はコミット対象外です。

## 設計メモ

- 送信前チェックができない場合は送らない（fail-closed）。議論型のチャンネル投稿では重複コストが欠落コストより重いため。
- DLQ（dead letter）に入った返信のうち、送信前チェックの失敗で送れなかった分は、失敗から24時間以内・毎時の照合で1回だけ再送する。再送文の先頭には「遅れての再送」と元の失敗時刻を添える。スレッドで誰かが実質的に答え済みの場合は再送しない。

## 公開版についての注意

公開にあたり、個人を特定しうる情報（ユーザーIDと表示名の対応表、関係者の実名）は伏せてあります。実運用のコードでは実データが入ります。
