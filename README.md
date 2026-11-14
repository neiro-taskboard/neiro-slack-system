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
  - `pre-push` — push 前の機械的な担保：push されるコミットにトークン形式・秘密鍵・noreply 以外のメール・電話番号らしい文字列があれば push を止める。ダミーのプレースホルダー（`REPLACE-ME` を含むもの）は許可する（Yanayanaさんの共有 2026-11-14 を取り込み。自分のプレースホルダーで止まる罠を避けるため）。実名のチェックは `.git/hooks/pre-push-names`（ローカルのみ、コミット対象外）に1行1件で書いた名前で行う。`.git/hooks` はクローンには含まれないため、fresh clone したら `sh hooks/install-pre-push.sh` で入れ直すこと

## 秘密情報について

トークン等の秘密情報はこのリポジトリに含めていません。
アプリレベルトークン（xapp）は実行環境の `config.json`（権限 0600）から読み込む前提で、
`config.json` 自体はコミット対象外です。

## 設計メモ

- 送信前チェックができない場合は送らない（fail-closed）。議論型のチャンネル投稿では重複コストが欠落コストより重いため。
- DLQ再送の最終形（2026-11-13の3者合意、2026-11-14に実装）：送信前チェックの失敗で送れなかった分だけを、失敗から24時間以内・毎時の起こし直しで再送する。ポーラーがペイロードに `resend`（元の失敗時刻つき・接頭辞つき）を付けて起こし直し、ワーカーが `slack-answered-check.py` で答え済み確認（元の投稿より後に質問者と自分以外による本文のある投稿があれば再送しない、時刻順序つき）をしてから送る。再送文の先頭には resend.prefix（コード側で固定した「遅れての再送（元の失敗時刻：YYYY-MM-DD HH:MM JST）」、2026-11-14の3者合意。一時は「最初の試行」に寄せる合意だったが、同日のレビューで元の文言に戻すことで3者統一）を付ける。重複チェックでは新旧両方の文言を同一視する（旧文言で送られた再送があっても二重送信しない）。送信自体の失敗は終端させる。参考：Yanayana の `reconcile_ambiguous.py`（https://github.com/yanayana-dev/slack-ata）。
- ワーカーの起動猶予（2026-11-14）：`processing` のイベントは、最初の wake から3時間の間は試行回数を消費しない。ワーカーの起動がポールから40分〜2時間遅れることがあり、従来の「10分陳腐化×3回で dead」では、ワーカーが起動する前に dead になる誤判定が頻発した（実測：dead-letter 165件全件がイベント発生から30〜37分で dead 化し、うち116件は遅れて起動したワーカーが後に done にしていた）。猶予を過ぎたら従来通り10分刻みでリトライし、3回で dead になる。`failed`（ワーカーが起動して失敗を報告した分）には猶予は無い。代償として、本当に起動したワーカーが応答しない場合の検知が最大で約3.5時間まで遅くなる。

## 公開版についての注意

公開にあたり、個人を特定しうる情報（ユーザーIDと表示名の対応表、関係者の実名）は伏せてあります。実運用のコードでは実データが入ります。
