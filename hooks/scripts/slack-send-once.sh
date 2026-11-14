#!/usr/bin/env bash
# slack-send-once.sh — 冪等な Slack 送信ラッパー。
#
# Yama さんの三重投稿の根本修正（2026-11-12）を参考にしたガード付き送信。
# ねいろの返信はこれまで、送信が Slack 側に届いたのに応答ロストで
# 「失敗」と扱われ、再実行で重複投稿する穴があった。
#
# 方針の切り替え基準（2026-11-12、Yama さん・Yanayana さんと三者で整理。
# Yanayana さんの提案で運用メモに残す）：
#   通知型＝欠落コストが重い → fail-open（届かない方が困る）
#   議論型＝重複コストが重い → fail-closed（重ねて送らない）
# うちのチャンネル投稿は今は議論を起こす共有が中心で、通知型の親投稿
# （Yama さんの日次まとめのようなもの）は無いので fail-closed のまま。
# 通知型の親投稿を出すようになったら、そのときは fail-open を検討する。
# スレッド返信・ambiguous の場面は三者とも fail-closed で揃っている。
#
# ガード:
#   1. 送信前に履歴を確認し、直近 --window-mins 分以内に自分の同一文面が
#      既にあれば送らずに成功扱いにする（台帳未記録でも止まる）。
#      ただしスレッド返信（--thread-ts / --permalink）の場合は時間の窓
#      なしで確認する。当初はスレッド全体を見ていた（Yama さんの指摘
#      2026-11-12：15分を超えて遅れた再実行の同一文面にも強くする。
#      Yanayana さんも同じ指摘）が、2026-11-14 に直近 THREAD_CHECK_N
#      件（100件）へ絞った（オーナーの指摘：スレッドが伸びるほど読み直し
#      の負担が増える。詳細は下の 10）。スレッド内では同一文面の再送
#      はほぼ再実行による重複のため。
#   2. 送信コマンドが失敗しても、履歴に自分の投稿が見つかれば
#      「届いていた」と扱い、盲目的な再送はしない（最大3回まで再確認）。
#      読み直しでも履歴が一度も読めず、到達の有無が不明なときは
#      ambiguous_post_outcome として別ファイルに記録する（Yanayana さん
#      の reply_once.py の考え方、2026-11-12）。履歴が読めて投稿が無い
#      ことが確認できた場合は明確な失敗なので記録しない。後日の送信前
#      チェックや到達確認で投稿が見つかったら、同じチャンネル・スレッド
#      の未解決の ambiguous 記録に resolved_by_thread_check の訂正
#      エントリを追加する（Yanayana さんの共有 2026-11-12 を取り入れ。
#      元の記録は履歴として残す）。
#   3. 履歴の読み取り自体も最大5回リトライする。間隔は 1・2・4・8 秒の
#      指数バックオフ（2026-11-12、Groq gpt-oss-120b の助言と Yama さん
#      の重複報告を受けて固定3秒から変更。2026-11-12 夜、Yama さんの
#      改善 (a) に合わせて 3回・1・2・4秒から 5回・1・2・4・8秒へ）。読み取りが全部失敗し、
#      台帳にも送信記録が無いときは送らない（fail-closed / cannot-verify。
#      2026-11-12 に fail-open から切替。Yama さんの ambiguous_post_outcome
#      の実地ケースで、送らずに止めたら実際には届いていて二重投稿が
#      起きなかったことが確認されたため。送れなかった分は DLQ再送の改訂案
#      （2026-11-13）で後から拾う：呼び出し側が waiting として記録し、
#      1時間ごとに起こし直して24時間以内に再送する。終了コードは3で、
#      送信そのものの失敗（終了コード1）と区別する）。
#   4. 履歴取得は next_oldest でページングして全ページを確認する
#      （Yama さんの修正4番目・Yanayana さんの共有 2026-11-12 と同じ穴の修正。
#      oldest-first の先頭50件だけでは、長いスレッドで直近の重複が範囲外に
#      なる）。--oldest で直近 --window-mins 分に絞って読む。
#      スレッド読み取りは --oldest=親ts から始める（Yanayana さんの指摘
#      2026-11-12：--oldest なし／0 だと初回が「親＋最新N件」になり、
#      next_oldest が最新メッセージを指して2ページ目が空になり、
#      古い返信が一生見えない。実スレッドで --limit 3 で検証済み）。
#      なお、この全ページ確認はチャンネル履歴の窓チェックの話で、
#      スレッド返信側は 2026-11-14 に直近100件の単発読みへ変更した
#      （下の 10）。
#   5. チャンネルへの通常送信は、15分窓に加えて直近 BOUNDED_N 件の
#      bounded チェックも行う（Yanayana さんの提案 2026-11-12、Yama さん
#      も賛同）。15分を超えて遅れた再実行でも、直近50件に同一文面が
#      あれば送らない。チャンネル履歴は1回呼びで最新50件が取れる
#      （Yama さんの指摘・実測 2026-11-12：--limit 50 は最新50件を
#      古い順に返す。--oldest=最新ts だと0件）。「古い順にしか読めない」
#      はページ内の並び順の話で、窓そのものは最新。以前の最大20ページの
#      走査は、2ページ目が空になるだけの無駄だったので単発呼びに簡素化
#      した（2026-11-12）。スレッド返信のページング（--oldest=親ts から
#      古い順に辿る）は別APIで実際に前に進むため、そちらは従来通り残す。
#      cannot-verify の扱いは 2026-11-12 に fail-closed へ切替
#      （Yama さん・Yanayana さんと同じ方針に揃えた）。
#   6. 送信台帳（ローカルの送信記録）。送信が成功したときと、応答ロスト
#      後の到達確認で履歴に自分の投稿が見つかったときに、送信記録を
#      slack-send-log.jsonl に残す。履歴が読めない（cannot-verify）ときは
#      送る前に台帳も見る：同じチャンネル・スレッド・文面の送信記録が
#      あれば「送ったはず」として送らない。台帳に無ければ送らない
#      （fail-closed。2026-11-12 に fail-open から切替。Yanayana さんの
#      「読み直しリトライ＋台帳確認の二段構え」2026-11-12。Yama さんも
#      event-id と replies テーブルの照合で同じ考え方。読み直しリトライ
#      自体は 1・3・5 に既にある）。
#
#   7. 台帳の整理（prune）。記録のたびに、90日より古いエントリを
#      落とす（Yanayana さんの設計 2026-11-12：年齢ベース、タイムス
#      タンプが読めないものは残すフェイルセーフ。重複チェックが効く
#      のは再実行される数時間〜数日以内だけなので90日で十分）。落と
#      した行は slack-send-log-archive.jsonl に移し、消しっぱなしに
#      はしない。台帳が際限なく大きくなるのを防ぐ。--prune-dry-run
#      で、削除せずに何件落ちるかだけ数えられる（Yama さんの
#      prune-replies --dry-run / Yanayana さんの両 prune への追加、
#      2026-11-12 と同じ考え方）。
#   8. 他者の同一文面チェック。送信前にスレッドを読み直し、送る文面が
#      他者の既存投稿と一字一句一致していたら送らない（終了コード4）。
#      Yanayana さんの reply_once.py の再発防止チェック（2026-11-14）
#      を取り入れ。同日、Yanayana さんの返信の下書きを作る経路が直近の
#      Yama さんの投稿文面を拾ってそのまま送ってしまい、2人の返信が
#      一字一句同じになった事故が起きた。うちの従来の重複チェックは
#      自分の返信との一致しか見ていないので同じ穴があった。読み直し
#      自体が失敗したときも送らずに止める（fail-closed、終了コード3）。
#      短い定型文が他者の投稿と偶然一致して止まる可能性はあるが、一字
#      一句の一致に限るので稀で、止まったときは文面を変えて送り直せばよい。
#      親投稿側も同じ考え方で拡張（2026-11-14、Yanayana さんの
#      channel_send_once.py の拡張を取り入れ）。チャンネルへの通常送信
#      では直近50件を読み直し、他者の既存投稿と一字一句一致（前後の
#      空白を除く）していたら送らない（終了コード4）。読み直しができ
#      なければ送らずに止める（fail-closed、終了コード3）。スレッド返信
#      側のチェックとは別に読む（1回の送信で計3回の読み取りになるが、
#      チャンネル投稿は頻度が低く、既存の check_bounded と分けておく
#      方が変更が局所的で安全）。
#   9. 再送ヘッダーの新旧同一視（2026-11-14、再送ヘッダーの文言統一）。
#      自分の投稿との重複チェック（台帳・bounded・窓）では、文頭の再送
#      ヘッダー1行を除いて比較する。ヘッダーは新文言「遅れての再送
#      （元の失敗時刻：YYYY-MM-DD HH:MM JST）」と旧文言「遅れての再送
#      （最初の試行：YYYY-MM-DD HH:MM JST）」の両方を認識し、どちらも
#      同じものとして扱う（Yama さん・Yanayana さんと同じ考え方）。
#      文言切替の途中で旧文言の再送が送られていても、再 wake した新文言
#      の再送が重複と判定されて二重送信にならない。なお旧文言で送られた
#      再送は実際には1件もない（送信台帳で 2026-11-14 に確認済み）が、
#      念のため両方残す。他者の同一文面チェックには適用しない（他者の
#      投稿にうちの再送ヘッダーは付かないし、付いていたらそれは一字一句
#      の一致として止めるべき事故のため）。
#   10. スレッド返信の重複チェックは直近 THREAD_CHECK_N 件（100件）
#      に絞る（2026-11-14、オーナーの判断）。それまでは自分の同一文面
#      チェック（check_duplicate）も他者の同一文面チェック
#      （check_other_duplicate）も、スレッドを --oldest=親ts から
#      ページングして全件読み直していたため、スレッドが伸びるほど
#      1回の送信あたりの読み直しが際限なく増えた。--oldest を付けず
#      --limit 100 で読むと「親＋直近100件」が1回で取れる（返信
#      180件近い実スレッドで検証済み）。割り切り：直近100件より古い
#      同一文面は履歴チェックでは拾わない。自分の送信は送信台帳の
#      確認（6・11）が別にあり、他者との一字一句一致の事故は同時刻の
#      投稿同士で起きるため直近で足りる、という判断。チャンネル側の
#      bounded（直近50件）と同じ考え方で、対象がスレッドかどうかの
#      違いだけ。
#
#   11. parroting防止の台帳照合（2026-11-14、Yamaさんの提案を取り入れ、
#       Yanayanaさん・Yamaさんと実装済み）。スレッド返信の送信前には、
#       履歴（直近100件）を読む前に送信台帳も照合する。台帳は手元に
#       あるので API を叩かない。範囲は同一スレッド内のみで、照合は
#       再送ヘッダー除去＋空白・改行の正規化のうえでの完全一致。一致
#       したら送らずに成功扱い（既存の重複スキップと同じ）。台帳に
#       文面が記録されていない過去の投稿は対象外。チャンネルへの直投
#       稿は対象外（従来の15分窓＋直近50件のまま）。これまでは台帳照
#       合が cannot-verify 時のフォールバックでしかなく、直近100件よ
#       り古い自分の同一文面は通常フローで検知できない穴があった。
#       Groq gpt-oss-120b に設計を確認し、「台帳→履歴」の順序は妥当
#       との助言を得た。ただし「台帳が読めないときは履歴だけで判定し
#       て送ってよい」という部分は採用せず、三者で合意済みの
#       fail-closed（送らない・DLQで後から拾う）を維持する。
#
#   12. metadata 優先の照合（2026-11-14、Yama さんの設計を三者で採用。
#       スレッドで event_id の中身が決まったので、既存の返信フローへ
#       移行した）。--event-id / --request-id を付けて送ると、送信は
#       slack-post-metadata.py で chat.postMessage を直接叩いて
#       metadata（event_type: neiro_reply、event_payload に event_id
#       または request_id）を付け、送信前チェックは slack-read-metadata.py
#       で include_all_metadata=True 付きで読み直して印の一致を先に見る。
#       印が見つかれば文面を見ずに重複として送らない。印が無い投稿や
#       読み取り失敗時は従来の文面照合へ進む（フォールバック）。印を
#       付けない送信は従来通り slackcli 経由。うちの event_id は
#       「workspace:channel:ts」の形（Yama さんの Slack の event_id
#       （Ev…の形）とは中身が違うが、元のイベントを一意に指す点では
#       同じ役割、という整理で三者合意済み）。
#
# 使い方:
#   slack-send-once.sh --channel C0CGYGT9V7G --message '...' [--workspace muse-board]
#   slack-send-once.sh --permalink <url> --message '...' [--workspace muse-board]
#   slack-send-once.sh --channel C... --thread-ts 1794477882.362889 --message-file note.txt
#   slack-send-once.sh --channel C... --thread-ts <ts> --message '...' --event-id <workspace:channel:ts>
#     印を付けて送るとき（イベント駆動の返信は --event-id、手動投稿は --request-id <UUID>）。
#     送信は chat.postMessage 直接叩きになり、送信前チェックも metadata 優先になる.
#   slack-send-once.sh --channel C... --message '...' --check-only   # 送信せず重複だけ確認
#   slack-send-once.sh --prune-dry-run [--ledger <path>]              # 何件落ちるかだけ数える（削除しない）
#
# 終了コード: 0 = 送信済み（または既に投稿ありでスキップ）、1 = 送信失敗（送信を試みたが履歴にもなし）。
#   --check-only のときは 0 = 重複あり、1 = 重複なし、2 = 確認できず。
#   3 = 送信前の履歴確認ができず送らなかった（cannot-verify / pre-check失敗）。
#   4 = 送る文面がスレッド直近100件／チャンネル直近50件の他者の既存投稿と
#       一字一句一致していたので送らなかった（Yanayana さんの
#       reply_once.py / channel_send_once.py の再発防止チェック、
#       2026-11-14）。呼び出し側は文面を変えて送り直すか、送らない判断をする。
#   DLQ再送の改訂案（2026-11-13、Yama・Yanayana・Neiroで合意）：pre-check失敗は
#   投稿の失敗ではなく API 待ちなので、送信失敗（1）と区別する。呼び出し側は
#   終了コード3のとき failed ではなく waiting として記録し、ポーラーが
#   1時間ごとに起こし直して24時間以内に再送する。送信そのものの失敗（1）は
#   従来通り failed → リトライ／デッドレターで終端。
set -uo pipefail

SLACKCLI="$HOME/workspace/bin/slackcli"
WINDOW_MINS=15
VERIFY_ATTEMPTS=3
HISTORY_ATTEMPTS=5
BOUNDED_N=50
THREAD_CHECK_N=100
LEDGER_FILE="$HOME/hooks/state/slack-send-log.jsonl"
LEDGER_ARCHIVE_FILE="$HOME/hooks/state/slack-send-log-archive.jsonl"
AMBIGUOUS_FILE="$HOME/hooks/state/slack-ambiguous-posts.jsonl"
CHECK_VIA=""
FOUND_TS=""
EVENT_ID=""
REQUEST_ID=""
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
READ_META="$SCRIPT_DIR/slack-read-metadata.py"
POST_META="$SCRIPT_DIR/slack-post-metadata.py"

WORKSPACE=""
CHANNEL=""
PERMALINK=""
THREAD_TS=""
MESSAGE=""
MESSAGE_FILE=""
CHECK_ONLY=0
PRUNE_DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --workspace)   WORKSPACE="$2"; shift 2 ;;
    --channel)     CHANNEL="$2"; shift 2 ;;
    --permalink)   PERMALINK="$2"; shift 2 ;;
    --thread-ts)   THREAD_TS="$2"; shift 2 ;;
    --message)     MESSAGE="$2"; shift 2 ;;
    --message-file) MESSAGE_FILE="$2"; shift 2 ;;
    --event-id)    EVENT_ID="$2"; shift 2 ;;
    --request-id)  REQUEST_ID="$2"; shift 2 ;;
    --window-mins) WINDOW_MINS="$2"; shift 2 ;;
    --check-only)  CHECK_ONLY=1; shift ;;
    --prune-dry-run) PRUNE_DRY_RUN=1; shift ;;
    --ledger)      LEDGER_FILE="$2"; shift 2 ;;
    -h|--help)     sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

# --prune-dry-run: 台帳の prune で何件落ちるかだけ数えて JSON で返す。
# Yama さんの prune-replies --dry-run / Yanayana さんの両 prune への
# --dry-run 追加（2026-11-12）と同じ考え方。削除もアーカイブもしない。
# prune_ledger と同じ判定（90日より古いものが対象、タイムスタンプが
# 読めないものは残す）なので、ここで出た件数がそのまま削除予定件数。
if [ "$PRUNE_DRY_RUN" -eq 1 ]; then
  python3 - "$LEDGER_FILE" <<'PYEOF'
import json, sys, time
ledger_path = sys.argv[1]
cutoff = time.time() - 90 * 86400
result = {'ledger': ledger_path, 'total': 0, 'would_drop': 0,
          'kept': 0, 'unreadable_kept': 0}
try:
    with open(ledger_path, encoding='utf-8') as f:
        lines = f.readlines()
except OSError:
    result['error'] = 'ledger not found'
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(0)
for line in lines:
    result['total'] += 1
    try:
        rec = json.loads(line)
        ts = float(rec.get('ts', ''))
    except (ValueError, TypeError):
        result['kept'] += 1
        result['unreadable_kept'] += 1
        continue
    if ts >= cutoff:
        result['kept'] += 1
    else:
        result['would_drop'] += 1
print(json.dumps(result, ensure_ascii=False))
PYEOF
  exit 0
fi

# --permalink からチャンネルとスレッド親 ts を取り出す。
# スレッド内返信のパーマリンクには ?thread_ts=<親ts> が付くので、
# スレッド親はまず thread_ts から取る。p<ts> はその返信自身の ts
# なので、親にするのは誤り（2026-11-14 のバグ：返信自体を親とする
# 誤ったスレッド指定になっていた）。親投稿のパーマリンクには
# thread_ts が無いので、その場合だけ p<ts>（その投稿自身の ts）が親になる。
if [ -n "$PERMALINK" ]; then
  CHANNEL=$(printf '%s' "$PERMALINK" | sed -n 's#.*/archives/\([A-Z0-9]*\)/p\([0-9]*\).*#\1#p')
  TTS=$(printf '%s' "$PERMALINK" | sed -n 's#.*[?&]thread_ts=\([0-9A-Za-z%.]*\).*#\1#p')
  # URL エンコードされたドット（%2E）も戻す。cid 等の他パラメータは無視。
  TTS=$(printf '%s' "$TTS" | sed 's/%2[Ee]/./g')
  case "$TTS" in
    *.*) THREAD_TS="$TTS" ;;
  esac
  if [ -z "$THREAD_TS" ]; then
    PTS=$(printf '%s' "$PERMALINK" | sed -n 's#.*/archives/[A-Z0-9]*/p\([0-9]*\).*#\1#p')
    if [ -n "$PTS" ]; then
      THREAD_TS="${PTS:0:10}.${PTS:10}"
    fi
  fi
fi

if [ -z "$CHANNEL" ]; then echo "channel is required (--channel or --permalink)" >&2; exit 2; fi
if [ -z "$MESSAGE" ] && [ -z "$MESSAGE_FILE" ]; then echo "message is required (--message or --message-file)" >&2; exit 2; fi
if [ -n "$EVENT_ID" ] && [ -n "$REQUEST_ID" ]; then echo "event-id and request-id are mutually exclusive" >&2; exit 2; fi

TMP_MSG=$(mktemp)
trap 'rm -f "$TMP_MSG"' EXIT
if [ -n "$MESSAGE_FILE" ]; then
  cp "$MESSAGE_FILE" "$TMP_MSG"
else
  printf '%s' "$MESSAGE" > "$TMP_MSG"
fi

WS_ARGS=()
if [ -n "$WORKSPACE" ]; then WS_ARGS=(--workspace "$WORKSPACE"); fi

# 自分のユーザーIDを調べる（whoami が取れなければ既知の対応表にフォールバック）。
self_user_id() {
  local id
  id=$("$SLACKCLI" auth whoami "${WS_ARGS[@]}" 2>/dev/null \
    | grep -oP '\(U[A-Z0-9]+' | head -1 | tr -d '(' || true)
  if [ -n "$id" ]; then printf '%s' "$id"; return 0; fi
  case "$WORKSPACE" in
    muse-board|T0CGV6V7161) printf 'U0CH1B19D4J' ;;
    *) printf 'U0CH32D1P5F' ;;
  esac
}
SELF_ID=$(self_user_id)

# 送信台帳の整理（prune）：90日より古いエントリを落とす。
# Yanayana さんの設計（2026-11-12）：年齢ベースで、タイムスタンプが
# 読めないエントリは消さずに残すフェイルセーフ。重複チェックが効くのは
# 再実行される数時間〜数日以内の話だけなので、90日あれば十分すぎる。
# 記録のたびに走る（台帳は小さいので安い）。落とした行はアーカイブ
# ファイルに移し、消しっぱなしにはしない。何も落ちなければ書き換えない。
#   引数: 台帳パス（省略時は $LEDGER_FILE）。
prune_ledger() {
  local ledger="${1:-$LEDGER_FILE}"
  [ -f "$ledger" ] || return 0
  python3 - "$ledger" "$LEDGER_ARCHIVE_FILE" <<'PYEOF' 2>/dev/null || true
import json, sys, time
ledger_path, archive_path = sys.argv[1:3]
cutoff = time.time() - 90 * 86400
try:
    with open(ledger_path, encoding='utf-8') as f:
        lines = f.readlines()
except OSError:
    sys.exit(0)
kept, dropped = [], []
for line in lines:
    try:
        rec = json.loads(line)
        ts = float(rec.get('ts', ''))
    except (ValueError, TypeError):
        kept.append(line)  # タイムスタンプが読めないものは残す
        continue
    if ts >= cutoff:
        kept.append(line)
    else:
        dropped.append(line)
if not dropped:
    sys.exit(0)
if archive_path:
    with open(archive_path, 'a', encoding='utf-8') as f:
        f.writelines(dropped)
with open(ledger_path, 'w', encoding='utf-8') as f:
    f.writelines(kept)
PYEOF
}

# 送信台帳に1件記録する（成功した送信と、到達確認できた送信）。
#   引数: 投稿の ts。同じ ts が既にあれば追加しない。
#   記録に失敗しても送信自体は成功扱いのままにする（台帳は補助）。
record_send() {
  local ts="$1"
  [ -n "$ts" ] || return 0
  mkdir -p "$(dirname "$LEDGER_FILE")" 2>/dev/null || return 0
  prune_ledger
  python3 - "$ts" "$CHANNEL" "$THREAD_TS" "$TMP_MSG" "$LEDGER_FILE" "$EVENT_ID" "$REQUEST_ID" <<'PYEOF' 2>/dev/null || true
import json, sys
ts, channel, thread_ts, msg_path, ledger_path, event_id, request_id = sys.argv[1:8]
try:
    with open(msg_path, encoding='utf-8') as f:
        text = f.read()
except OSError:
    sys.exit(0)
try:
    with open(ledger_path, encoding='utf-8') as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get('ts') == ts:
                sys.exit(0)
except OSError:
    pass
rec = {'ts': ts, 'channel': channel, 'thread_ts': thread_ts or '', 'text': text}
if event_id:
    rec['event_id'] = event_id
if request_id:
    rec['request_id'] = request_id
with open(ledger_path, 'a', encoding='utf-8') as f:
    f.write(json.dumps(rec, ensure_ascii=False) + '\n')
PYEOF
}

# 到達不明（ambiguous）の送信を記録する（Yanayana さんの reply_once.py
# の ambiguous_post_outcome、2026-11-12）。送信の応答が失われ、読み直し
# でも履歴が一度も読めなかったときに使う。履歴が読めて投稿が無いことが
# 確認できた場合は明確な失敗なので記録しない。
# 送ろうとした文面も一緒に残す（Yanayana さんの送信記録への文面追加、
# 2026-11-14 を取り入れ）。通常送信は record_send が文面を残しているが、
# ambiguous 側だけ文面が無く、後から「何を送ろうとしたか」が分からな
# かったため。文面が読めなくても記録自体は残す。
#   引数: 理由の文字列。
record_ambiguous() {
  local reason="$1"
  mkdir -p "$(dirname "$AMBIGUOUS_FILE")" 2>/dev/null || return 0
  python3 - "$reason" "$CHANNEL" "$THREAD_TS" "$AMBIGUOUS_FILE" "$TMP_MSG" <<'PYEOF' 2>/dev/null || true
import json, sys, time
reason, channel, thread_ts, path, msg_path = sys.argv[1:6]
try:
    with open(msg_path, encoding='utf-8') as f:
        text = f.read()
except OSError:
    text = ''
rec = {'time': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'channel': channel,
       'thread_ts': thread_ts or '', 'outcome': 'ambiguous_post_outcome',
       'reason': reason, 'text': text}
with open(path, 'a', encoding='utf-8') as f:
    f.write(json.dumps(rec, ensure_ascii=False) + '\n')
PYEOF
}

# ambiguous 記録への訂正エントリを追加する（Yanayana さんの reply_once.py
# の resolved_by_thread_check、2026-11-12 に取り入れ）。送信前チェックや
# 到達確認で履歴に投稿が見つかったとき、同じチャンネル・スレッドの未解決
# の ambiguous_post_outcome 記録があれば、訂正エントリを追加する。
# 元の記録は履歴として残し、消さない。未解決の記録が無ければ何もしない。
# 同じ found_ts の訂正が既にあれば重ねて追加しない。
#   引数: 見つかった投稿の ts。
resolve_ambiguous() {
  local found_ts="$1"
  [ -n "$found_ts" ] || return 0
  [ -f "$AMBIGUOUS_FILE" ] || return 0
  python3 - "$found_ts" "$CHANNEL" "$THREAD_TS" "$AMBIGUOUS_FILE" <<'PYEOF' 2>/dev/null || true
import json, sys, time
found_ts, channel, thread_ts, path = sys.argv[1:5]
try:
    with open(path, encoding='utf-8') as f:
        lines = f.readlines()
except OSError:
    sys.exit(0)
unresolved = False
already = False
for line in lines:
    try:
        rec = json.loads(line)
    except ValueError:
        continue
    if rec.get('channel') != channel:
        continue
    if (rec.get('thread_ts') or '') != (thread_ts or ''):
        continue
    if rec.get('outcome') == 'ambiguous_post_outcome':
        unresolved = True
    if rec.get('outcome') == 'resolved_by_thread_check' and rec.get('found_ts') == found_ts:
        already = True
if not unresolved or already:
    sys.exit(0)
rec = {'time': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'channel': channel,
       'thread_ts': thread_ts or '', 'outcome': 'resolved_by_thread_check',
       'found_ts': found_ts}
with open(path, 'a', encoding='utf-8') as f:
    f.write(json.dumps(rec, ensure_ascii=False) + '\n')
PYEOF
}

# 送信台帳に同じチャンネル・スレッド・文面の送信記録があるか確認する。
#   0 = 見つかった（その ts を出す）、1 = なし、2 = 台帳が読めなかった。
# 照合は再送ヘッダー除去＋空白・改行の正規化のうえでの完全一致
# （parroting防止、2026-11-14、Yamaさんの提案）。スレッド返信の
# 送信前チェック（11）でも、cannot-verify 時のフォールバックでも使う。
# --event-id / --request-id があるときは印の一致を文面より先に見る
# （metadata 優先、2026-11-14、ヘッダーの 12）。印が無い古い記録は
# 対象外で、文面照合へ進む。
check_ledger() {
  python3 - "$CHANNEL" "$THREAD_TS" "$TMP_MSG" "$LEDGER_FILE" "$EVENT_ID" "$REQUEST_ID" <<'PYEOF' 2>/dev/null
import json, re, sys
channel, thread_ts, msg_path, ledger_path, event_id, request_id = sys.argv[1:7]
# 再送ヘッダー（新旧どちらの文言でも）で始まる文面は、ヘッダー1行を除いて
# 比較する（2026-11-14、再送ヘッダーの文言統一。旧文言で送られた再送が
# あっても、新文言の再送が重複と判定される）。
_RESEND_HEAD = re.compile(r'^遅れての再送（(?:元の失敗時刻|最初の試行)：\d{4}-\d{2}-\d{2} \d{2}:\d{2} JST）\n(?=.)')
def _norm(t):
    # ヘッダー除去のうえ、全ての空白類（空白・改行・タブ・全角空白など）
    # を1スペースに畳んで比較する。改行や空白の入れ方の違いだけの同一
    # 文面も一致として扱う（parroting防止、2026-11-14）。
    return re.sub(r'\s+', ' ', _RESEND_HEAD.sub('', t.strip(), count=1)).strip()
try:
    with open(msg_path, encoding='utf-8') as f:
        target = _norm(f.read())
except OSError:
    sys.exit(2)
try:
    f = open(ledger_path, encoding='utf-8')
except OSError:
    sys.exit(1)
found = None
with f:
    for line in f:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get('channel') != channel:
            continue
        if (rec.get('thread_ts') or '') != (thread_ts or ''):
            continue
        if event_id and rec.get('event_id') == event_id:
            found = rec.get('ts')
            break
        if request_id and rec.get('request_id') == request_id:
            found = rec.get('ts')
            break
        if _norm(rec.get('text') or '') == target:
            found = rec.get('ts')
            break
if found:
    print(found)
    sys.exit(0)
sys.exit(1)
PYEOF
}

# チャンネル投稿の bounded チェック：直近 BOUNDED_N 件に自分の同一文面が
# ないか確認する。Yanayana さんの提案（2026-11-12）、Yama さんも賛同。
# 15分窓を超えて遅れた再実行の重複を止める。
# チャンネル履歴は1回呼びで最新 BOUNDED_N 件が取れる（実測 2026-11-12：
# --limit 50 は最新50件を古い順に返す。--oldest=最新ts だと0件）。
# 以前の最大20ページの走査は、2ページ目が空になるだけだったので廃止し、
# 単発呼びに簡素化した。読み取り失敗時は最大5回リトライし、それでも
# 読めなければ cannot-verify（fail-open は呼び出し側の判断）。
#   0 = 見つかった、1 = なし、2 = 履歴が読めなかった。
check_bounded() {
  local attempt=0 raw found rc
  while [ "$attempt" -lt "$HISTORY_ATTEMPTS" ]; do
    attempt=$((attempt + 1))
    raw=$("$SLACKCLI" conversations read "$CHANNEL" --limit "$BOUNDED_N" --json "${WS_ARGS[@]}" 2>/dev/null || true)
    found=$(printf '%s' "$raw" | python3 -c "
import sys, json, re
raw = sys.stdin.read()
i = raw.find('{')
if i < 0:
    sys.exit(2)
try:
    d = json.loads(raw[i:])
except Exception:
    sys.exit(2)
# 再送ヘッダー（新旧どちらの文言でも）で始まる文面は、ヘッダー1行を除いて
# 比較する（2026-11-14、再送ヘッダーの文言統一）。
_RESEND_HEAD = re.compile(r'^遅れての再送（(?:元の失敗時刻|最初の試行)：\d{4}-\d{2}-\d{2} \d{2}:\d{2} JST）\n(?=.)')
def _norm_resend(t):
    return _RESEND_HEAD.sub('', t.strip(), count=1)
target = _norm_resend(open('$TMP_MSG', encoding='utf-8').read())
for m in d.get('messages', []):
    if m.get('user') == '$SELF_ID' and _norm_resend(m.get('text') or '') == target:
        print('FOUND ' + str(m.get('ts', '')))
        sys.exit(0)
sys.exit(1)
" 2>/dev/null)
    rc=$?
    if [ "$rc" -eq 0 ]; then
      echo "duplicate found: ${found#FOUND }"
      CHECK_VIA="bounded"
      FOUND_TS="${found#FOUND }"
      return 0
    elif [ "$rc" -eq 1 ]; then
      return 1
    fi
    # rc == 2: このページが読めなかった。再試行する。
    # 間隔は 1・2・4・8 秒の指数バックオフ（2026-11-12、Groq gpt-oss-120b
    # の助言と Yama さんの重複報告を受けて固定3秒から変更。2026-11-12 夜、
    # Yama さんの改善 (a) に合わせて 3回から 5回へ）。最後の試行の後に
    # 待つ意味はないので、次があるときだけ待つ。
    if [ "$attempt" -lt "$HISTORY_ATTEMPTS" ]; then sleep $((1 << (attempt - 1))); fi
  done
  return 2
}

# metadata の印での重複チェック（Yama さんの設計、2026-11-14、ヘッダーの 12）。
# slack-read-metadata.py で include_all_metadata 付きで読み直し、
# 自分の投稿のうち metadata.event_payload.event_id / request_id が
# 一致するものを探す。文面照合より先に行う（第1手段）。
#   0 = 見つかった（その ts を出す）、1 = なし、2 = 読めなかった。
# 読めなかったときは呼び出し側が文面照合へ進む（フォールバック）。
# 印が無い（--event-id / --request-id 未指定）のときは呼ばない。
check_metadata() {
  local out found_ts rc
  [ -x "$READ_META" ] || return 2
  local meta_args=(--channel "$CHANNEL" --self-id "$SELF_ID")
  if [ -n "$WORKSPACE" ]; then meta_args+=(--workspace "$WORKSPACE"); fi
  if [ -n "$THREAD_TS" ]; then meta_args+=(--thread-ts "$THREAD_TS"); fi
  if [ -n "$EVENT_ID" ]; then meta_args+=(--event-id "$EVENT_ID"); else meta_args+=(--request-id "$REQUEST_ID"); fi
  out=$(python3 "$READ_META" "${meta_args[@]}" 2>/dev/null || true)
  found_ts=$(printf '%s' "$out" | python3 -c "
import sys, json
raw = sys.stdin.read()
try:
    d = json.loads(raw)
except Exception:
    sys.exit(2)
if not d.get('ok'):
    sys.exit(2)
if d.get('found'):
    print('FOUND ' + str(d.get('ts', '')))
    sys.exit(0)
sys.exit(1)
" 2>/dev/null)
  rc=$?
  if [ "$rc" -eq 0 ]; then
    echo "duplicate found by metadata: ${found_ts#FOUND }"
    CHECK_VIA="metadata"
    FOUND_TS="${found_ts#FOUND }"
    return 0
  elif [ "$rc" -eq 1 ]; then
    return 1
  fi
  return 2
}

# スレッド返信の自分の同一文面チェック（直近 THREAD_CHECK_N 件）。
#   0 = 見つかった（その ts を出す）、1 = なし、2 = 履歴が読めなかった。
# --oldest を付けず --limit THREAD_CHECK_N で読むと「親＋直近N件」が
# 1回で取れる（実スレッド検証済み）。経緯と割り切りはヘッダーの 10。
# 再送ヘッダーの同一視は窓チェックと共通（ヘッダーの 9）。
check_duplicate_thread() {
  local attempt=0 raw found rc
  while [ "$attempt" -lt "$HISTORY_ATTEMPTS" ]; do
    attempt=$((attempt + 1))
    raw=$("$SLACKCLI" conversations read "$CHANNEL" --limit "$THREAD_CHECK_N" --json --thread-ts "$THREAD_TS" "${WS_ARGS[@]}" 2>/dev/null || true)
    found=$(printf '%s' "$raw" | python3 -c "
import sys, json, re
raw = sys.stdin.read()
i = raw.find('{')
if i < 0:
    sys.exit(2)
try:
    d = json.loads(raw[i:])
except Exception:
    sys.exit(2)
_RESEND_HEAD = re.compile(r'^遅れての再送（(?:元の失敗時刻|最初の試行)：\d{4}-\d{2}-\d{2} \d{2}:\d{2} JST）\n(?=.)')
def _norm_resend(t):
    return _RESEND_HEAD.sub('', t.strip(), count=1)
target = _norm_resend(open('$TMP_MSG', encoding='utf-8').read())
for m in d.get('messages', []):
    if m.get('user') == '$SELF_ID' and _norm_resend(m.get('text') or '') == target:
        print('FOUND ' + str(m.get('ts', '')))
        sys.exit(0)
sys.exit(1)
" 2>/dev/null)
    rc=$?
    if [ "$rc" -eq 0 ]; then
      echo "duplicate found: ${found#FOUND }"
      CHECK_VIA="thread-bounded"
      FOUND_TS="${found#FOUND }"
      return 0
    elif [ "$rc" -eq 1 ]; then
      return 1
    fi
    # rc == 2: 読めなかった。指数バックオフで再試行（他と同じ間隔）。
    if [ "$attempt" -lt "$HISTORY_ATTEMPTS" ]; then sleep $((1 << (attempt - 1))); fi
  done
  return 2
}

# 履歴に自分の同一文面があるか確認する（チャンネル投稿用）。
#   0 = 見つかった（その ts を出す）、1 = なし、2 = 履歴が読めなかった。
# ページング: --oldest に window の切り捨て時刻を渡し、next_oldest を
# 辿って全ページを確認する。1ページ目だけでは直近分が範囲外になる
# ため（oldest-first の先頭50件問題）。
# 窓内に重複がなければ、続けて bounded チェックを行う。
# スレッド返信は check_duplicate_thread へ委譲する。
check_duplicate() {
  if [ -n "$EVENT_ID" ] || [ -n "$REQUEST_ID" ]; then
    check_metadata
    local meta_rc=$?
    if [ "$meta_rc" -eq 0 ]; then
      return 0
    fi
    # 1（なし）・2（読めなかった）は文面照合へ進む。読めなかった
    # 場合も文面照合が走るので、従来の fail-closed は変わらない。
  fi
  if [ -n "$THREAD_TS" ]; then
    check_duplicate_thread
    return $?
  fi
  local attempt=0 raw page_out window_rc=2
  local cutoff
  cutoff=$(python3 -c "import time; print(time.time() - $WINDOW_MINS * 60)")
  while [ "$attempt" -lt "$HISTORY_ATTEMPTS" ]; do
    attempt=$((attempt + 1))
    local oldest="$cutoff"
    while :; do
      local read_args=("$CHANNEL" --limit 50 --json --oldest "$oldest")
      raw=$("$SLACKCLI" conversations read "${read_args[@]}" "${WS_ARGS[@]}" 2>/dev/null || true)
      page_out=$(printf '%s' "$raw" | python3 -c "
import sys, json, re, time
raw = sys.stdin.read()
i = raw.find('{')
if i < 0:
    sys.exit(2)
try:
    d = json.loads(raw[i:])
except Exception:
    sys.exit(2)
# 再送ヘッダー（新旧どちらの文言でも）で始まる文面は、ヘッダー1行を除いて
# 比較する（2026-11-14、再送ヘッダーの文言統一）。
_RESEND_HEAD = re.compile(r'^遅れての再送（(?:元の失敗時刻|最初の試行)：\d{4}-\d{2}-\d{2} \d{2}:\d{2} JST）\n(?=.)')
def _norm_resend(t):
    return _RESEND_HEAD.sub('', t.strip(), count=1)
target = _norm_resend(open('$TMP_MSG', encoding='utf-8').read())
cutoff = $cutoff
for m in d.get('messages', []):
    if m.get('user') != '$SELF_ID':
        continue
    try:
        ts = float(m.get('ts', '0'))
    except ValueError:
        continue
    if ts >= cutoff and _norm_resend(m.get('text') or '') == target:
        print('FOUND ' + m.get('ts', ''))
        sys.exit(0)
if d.get('has_more') and d.get('next_oldest'):
    print('MORE ' + str(d['next_oldest']))
    sys.exit(3)
print('DONE')
sys.exit(1)
")
      local rc=$?
      if [ "$rc" -eq 0 ]; then
        echo "duplicate found: ${page_out#FOUND }"
        CHECK_VIA="window"
        FOUND_TS="${page_out#FOUND }"
        return 0
      elif [ "$rc" -eq 1 ]; then
        window_rc=1
        break
      elif [ "$rc" -eq 3 ]; then
        oldest="${page_out#MORE }"
        continue
      fi
      # rc == 2: このページが読めなかった。外側のループで最初から再試行する。
      break
    done
    if [ "$window_rc" -eq 1 ]; then
      break
    fi
    # 間隔は 1・2・4・8 秒の指数バックオフ（同上。最後の試行の後に待つ
    # 意味はないので、次があるときだけ待つ）。
    if [ "$attempt" -lt "$HISTORY_ATTEMPTS" ]; then sleep $((1 << (attempt - 1))); fi
  done
  if [ "$window_rc" -ne 1 ]; then
    return 2
  fi
  # チャンネル投稿は bounded チェックへ。
  check_bounded
}

# 送信前の他者同一文面チェック（スレッド返信のみ）。
#   0 = 他者の同一文面あり、1 = なし、2 = 履歴が読めなかった。
# スレッドの直近 THREAD_CHECK_N 件を読み直し、送る文面が他者の既存
# 投稿と一字一句一致していないか確認する（Yanayana さんの
# reply_once.py の再発防止チェック、2026-11-14 を取り入れ。同日、
# Yanayana さんの返信の下書きを作る経路が直近の Yama さんの投稿文面
# を拾ってそのまま送ってしまい、2人の返信が一字一句同じになった事故
# の再発防止）。自分の返信との一致は check_duplicate が見るので、
# ここでは他者の投稿だけを見る。
# 当初はスレッド全件をページングして読み直していたが、2026-11-14 に
# 直近100件へ絞った（ヘッダーの 10。一字一句の一致事故は同時刻の
# 投稿同士で起きるため、直近で足りるという割り切り）。
# 読み直しは最大5回リトライし、それでも読めなければ cannot-verify
# （fail-closed。呼び出し側は終了コード3として扱う）。
check_other_duplicate() {
  local attempt=0 raw found rc
  [ -n "$THREAD_TS" ] || return 1
  while [ "$attempt" -lt "$HISTORY_ATTEMPTS" ]; do
    attempt=$((attempt + 1))
    raw=$("$SLACKCLI" conversations read "$CHANNEL" --limit "$THREAD_CHECK_N" --json --thread-ts "$THREAD_TS" "${WS_ARGS[@]}" 2>/dev/null || true)
    found=$(printf '%s' "$raw" | python3 -c "
import sys, json
raw = sys.stdin.read()
i = raw.find('{')
if i < 0:
    sys.exit(2)
try:
    d = json.loads(raw[i:])
except Exception:
    sys.exit(2)
target = open('$TMP_MSG', encoding='utf-8').read().strip()
for m in d.get('messages', []):
    if m.get('user') == '$SELF_ID':
        continue
    if (m.get('text') or '').strip() == target:
        print('OTHER_DUP ' + str(m.get('ts', '')))
        sys.exit(0)
sys.exit(1)
" 2>/dev/null)
    rc=$?
    if [ "$rc" -eq 0 ]; then
      echo "other user's identical post found: ${found#OTHER_DUP }"
      return 0
    elif [ "$rc" -eq 1 ]; then
      return 1
    fi
    # rc == 2: 読めなかった。指数バックオフで再試行（他と同じ間隔）。
    if [ "$attempt" -lt "$HISTORY_ATTEMPTS" ]; then sleep $((1 << (attempt - 1))); fi
  done
  return 2
}

# 送信前の他者同一文面チェック（チャンネルへの親投稿）。
#   0 = 他者の同一文面あり、1 = なし、2 = 履歴が読めなかった。
# 直近 BOUNDED_N 件を読み直し、送る文面が他者の既存投稿と一字一句
# 一致（前後の空白を除く）していないか確認する（Yanayana さんの
# channel_send_once.py の拡張、2026-11-14 を取り入れ。Yama さんの
# 直近50件の形と同じ）。自分の投稿との一致は check_bounded が見る
# ので、ここでは他者の投稿だけを見る。check_bounded とは別に読む
# （既存の読み取りと分けておく方が変更が局所的で安全。チャンネル
# 投稿は頻度が低いので1回余分に読んでも問題ない）。
# 読み直しは最大5回リトライし、それでも読めなければ cannot-verify
# （fail-closed。呼び出し側は終了コード3として扱う）。
check_other_bounded() {
  local attempt=0 raw found rc
  [ -n "$THREAD_TS" ] && return 1
  while [ "$attempt" -lt "$HISTORY_ATTEMPTS" ]; do
    attempt=$((attempt + 1))
    raw=$("$SLACKCLI" conversations read "$CHANNEL" --limit "$BOUNDED_N" --json "${WS_ARGS[@]}" 2>/dev/null || true)
    found=$(printf '%s' "$raw" | python3 -c "
import sys, json
raw = sys.stdin.read()
i = raw.find('{')
if i < 0:
    sys.exit(2)
try:
    d = json.loads(raw[i:])
except Exception:
    sys.exit(2)
target = open('$TMP_MSG', encoding='utf-8').read().strip()
for m in d.get('messages', []):
    if m.get('user') == '$SELF_ID':
        continue
    if (m.get('text') or '').strip() == target:
        print('OTHER_DUP ' + str(m.get('ts', '')))
        sys.exit(0)
sys.exit(1)
" 2>/dev/null)
    rc=$?
    if [ "$rc" -eq 0 ]; then
      echo "other user's identical post found: ${found#OTHER_DUP }"
      return 0
    elif [ "$rc" -eq 1 ]; then
      return 1
    fi
    # rc == 2: この読み取りが失敗した。再試行する。
    # 間隔は 1・2・4・8 秒の指数バックオフ（check_bounded と同じ。
    # 最後の試行の後に待つ意味はないので、次があるときだけ待つ）。
    if [ "$attempt" -lt "$HISTORY_ATTEMPTS" ]; then sleep $((1 << (attempt - 1))); fi
  done
  return 2
}

# --- 0. parroting防止：送信台帳との照合（同一スレッド内のみ） ---
# Yamaさんの提案（2026-11-14、ヘッダーの 11）。履歴の直近100件だけ
# では、それより古い自分の同一文面を検知できない穴があったので、
# 履歴を読む前に台帳も照合する。台帳は手元にあるので API を叩かない。
# 一致したら送らずに成功扱い（履歴で重複を見つけたときと同じ扱い）。
# 台帳が読めなければ fail-closed で送らない（終了コード3）。
# チャンネルへの直投稿は対象外（15分窓＋直近50件のまま）。
if [ -n "$THREAD_TS" ]; then
  ledger_ts=$(check_ledger 2>/dev/null)
  ledger_rc=$?
  if [ "$ledger_rc" -eq 0 ]; then
    FOUND_TS="$ledger_ts"
    [ -n "$FOUND_TS" ] && record_send "$FOUND_TS"
    [ -n "$FOUND_TS" ] && resolve_ambiguous "$FOUND_TS"
    echo "already posted in this thread (found in local send log), skipping send" >&2
    exit 0
  elif [ "$ledger_rc" -eq 2 ]; then
    record_ambiguous "send log unreadable (cannot-verify); not sending"
    echo "cannot verify send log, not sending (fail-closed; exit 3 = pre-check failure, not a send failure)" >&2
    exit 3
  fi
  # ledger_rc == 1: 台帳に無し。履歴チェックへ進む。
fi

# --- 1. 送信前の履歴確認 ---
check_duplicate
CHECK_RC=$?
if [ "$CHECK_RC" -eq 0 ]; then
  # 履歴で見つけて送らなかったときも、その投稿を台帳に記録する。
  # Yama さんの指摘（2026-11-12）：「送らずにDONE」の経路に記録が
  # ないと、後で台帳と履歴の照合ができず、再送される可能性がある。
  # 送信成功時・到達確認時と同じ record_send を使う。あわせて、
  # 同じチャンネル・スレッドの未解決の ambiguous 記録があれば
  # resolved_by_thread_check の訂正エントリを追加する（Yanayana さん
  # の共有 2026-11-12 を取り入れ。元の記録は履歴として残す）。
  [ -n "${FOUND_TS:-}" ] && record_send "$FOUND_TS"
  [ -n "${FOUND_TS:-}" ] && resolve_ambiguous "$FOUND_TS"
  if [ -n "$THREAD_TS" ]; then
    echo "already posted in this thread, skipping send" >&2
  elif [ "$CHECK_VIA" = "bounded" ]; then
    echo "already posted (found in recent ${BOUNDED_N}), skipping send" >&2
  else
    echo "already posted within ${WINDOW_MINS} min, skipping send" >&2
  fi
  exit 0
fi
if [ "$CHECK_RC" -eq 2 ]; then
  # 履歴が読めないときは、送る前に送信台帳も見る。台帳に同じ送信の
  # 記録があれば「送ったはず」として送らない（Yanayana さんの二段構え）。
  # 台帳にも無ければ送らない（fail-closed / cannot-verify）。
  # Yama さんの実地ケース（2026-11-12）：応答ロストで到達不明のまま
  # 送らずに止めたら、実際には届いていて二重投稿は起きなかった。
  # 届かなかった分は DLQ再送の改訂案（2026-11-13）で後から拾う：
  # 終了コード3で返し、呼び出し側が waiting として記録して1時間ごとに
  # 起こし直す。送信そのものの失敗（終了コード1）とは区別する。
  # 以前の fail-open はここで廃止（Yama さん・Yanayana さんと方針を揃えた）。
  if ledger_ts=$(check_ledger); then
    echo "already sent (found in local send log, ts $ledger_ts), skipping send" >&2
    exit 0
  fi
  if [ "$CHECK_ONLY" -eq 1 ]; then
    exit 2
  fi
  record_ambiguous "history unreadable after retries and no ledger entry (cannot-verify); not sending"
  echo "cannot verify history after retries, not sending (fail-closed; exit 3 = pre-check failure, not a send failure)" >&2
  exit 3
fi
if [ "$CHECK_ONLY" -eq 1 ]; then
  exit 1
fi

# --- 1b. 他者の同一文面チェック ---
# Yanayana さんの reply_once.py / channel_send_once.py の再発防止
# チェック（2026-11-14）を取り入れ。送る文面が他者の既存投稿と
# 一字一句一致していたら送らない。読み直しができなければ送らずに
# 止める（fail-closed、終了コード3）。
if [ -n "$THREAD_TS" ]; then
  check_other_duplicate
  OTHER_RC=$?
  if [ "$OTHER_RC" -eq 0 ]; then
    echo "text exactly matches another user's existing post in this thread, not sending (exit 4)" >&2
    exit 4
  fi
  if [ "$OTHER_RC" -eq 2 ]; then
    record_ambiguous "other-duplicate check unreadable after retries (cannot-verify); not sending"
    echo "cannot verify other-duplicate check after retries, not sending (fail-closed; exit 3 = pre-check failure, not a send failure)" >&2
    exit 3
  fi
else
  check_other_bounded
  OTHER_RC=$?
  if [ "$OTHER_RC" -eq 0 ]; then
    echo "text exactly matches another user's recent post in this channel, not sending (exit 4)" >&2
    exit 4
  fi
  if [ "$OTHER_RC" -eq 2 ]; then
    record_ambiguous "other-duplicate check (channel) unreadable after retries (cannot-verify); not sending"
    echo "cannot verify other-duplicate check after retries, not sending (fail-closed; exit 3 = pre-check failure, not a send failure)" >&2
    exit 3
  fi
fi

# --- 2. 送信 ---
# 印があるときは slack-post-metadata.py で直接 chat.postMessage を叩き、
# 投稿に metadata を付ける（Yama さんの設計、2026-11-14、ヘッダーの 12）。
# 印がないときは従来通り slackcli 経由（metadata なし）。
if [ -n "$EVENT_ID" ] || [ -n "$REQUEST_ID" ]; then
  META_ARGS=(--channel "$CHANNEL" --message-file "$TMP_MSG")
  if [ -n "$THREAD_TS" ]; then META_ARGS+=(--thread-ts "$THREAD_TS"); fi
  if [ -n "$WORKSPACE" ]; then META_ARGS+=(--workspace "$WORKSPACE"); fi
  if [ -n "$EVENT_ID" ]; then META_ARGS+=(--event-id "$EVENT_ID"); else META_ARGS+=(--request-id "$REQUEST_ID"); fi
  SEND_OUT=$(python3 "$POST_META" "${META_ARGS[@]}" 2>&1)
  SEND_RC=$?
else
  SEND_ARGS=(messages send --recipient-id "$CHANNEL" --message-file "$TMP_MSG" --json)
  if [ -n "$THREAD_TS" ]; then SEND_ARGS+=(--thread-ts "$THREAD_TS"); fi
  SEND_OUT=$("$SLACKCLI" "${SEND_ARGS[@]}" "${WS_ARGS[@]}" 2>&1)
  SEND_RC=$?
fi
if [ "$SEND_RC" -eq 0 ]; then
  printf '%s\n' "$SEND_OUT" | tail -5
  sent_ts=$(printf '%s' "$SEND_OUT" | python3 -c "
import sys, json
raw = sys.stdin.read()
i = raw.find('{')
if i < 0:
    sys.exit(1)
try:
    d = json.loads(raw[i:])
except Exception:
    sys.exit(1)
ts = d.get('ts') or (d.get('message') or {}).get('ts') or ''
print(ts)
sys.exit(0 if ts else 1)
" 2>/dev/null || true)
  [ -n "${sent_ts:-}" ] && record_send "$sent_ts"
  exit 0
fi
echo "send failed (exit $SEND_RC), verifying whether it actually arrived..." >&2
printf '%s\n' "$SEND_OUT" | tail -5 >&2

# --- 3. 応答ロスト時の到達確認（盲目的な再送はしない） ---
attempt=0
verified_absent=0
while [ "$attempt" -lt "$VERIFY_ATTEMPTS" ]; do
  attempt=$((attempt + 1))
  sleep $((attempt * 5))
  dup_out=$(check_duplicate); dup_rc=$?
  if [ "$dup_rc" -eq 0 ]; then
    dup_ts=$(printf '%s' "$dup_out" | sed -n 's/^duplicate found: //p' | head -1)
    [ -n "$dup_ts" ] && record_send "$dup_ts"
    [ -n "$dup_ts" ] && resolve_ambiguous "$dup_ts"
    echo "post arrived despite lost response, treating as success" >&2
    exit 0
  elif [ "$dup_rc" -eq 1 ]; then
    # 履歴は読めたが投稿は無い（明確な失敗）。
    verified_absent=1
  fi
  # dup_rc == 2: 履歴が読めなかった。到達の有無は不明のまま次へ。
done
if [ "$verified_absent" -eq 0 ]; then
  # 読み直しで履歴が一度も読めず、到達不明のまま。再投稿はしないが、
  # 不明であることを記録しておく（Yanayana さんの ambiguous_post_outcome）。
  record_ambiguous "send response lost; history unreadable after $VERIFY_ATTEMPTS verification attempts"
fi
echo "send failed and post not found in history" >&2
exit 1
