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
#      なしでスレッド全体を確認する（Yama さんの指摘 2026-11-12：15分を
#      超えて遅れた再実行の同一文面にも強くする。Yanayana さんも同じ指摘）。
#      スレッド内では同一文面の再送はほぼ再実行による重複のため。
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
#   8. 他者の同一文面チェック（スレッド返信のみ）。送信前にスレッドを
#      読み直し、送る文面が他者の既存投稿と一字一句一致していたら送ら
#      ない（終了コード4）。Yanayana さんの reply_once.py の再発防止
#      チェック（2026-11-14）を取り入れ。同日、Yanayana さんの返信の
#      下書きを作る経路が直近の Yama さんの投稿文面を拾ってそのまま
#      送ってしまい、2人の返信が一字一句同じになった事故が起きた。
#      うちの従来の重複チェックは自分の返信との一致しか見ていないので
#      同じ穴があった。読み直し自体が失敗したときも送らずに止める
#      （fail-closed、終了コード3）。短い定型文が他者の投稿と偶然一致
#      して止まる可能性はあるが、一字一句の一致に限るので稀で、止まった
#      ときは文面を変えて送り直せばよい。
#
# 使い方:
#   slack-send-once.sh --channel C0CGYGT9V7G --message '...' [--workspace muse-board]
#   slack-send-once.sh --permalink <url> --message '...' [--workspace muse-board]
#   slack-send-once.sh --channel C... --thread-ts 1794477882.362889 --message-file note.txt
#   slack-send-once.sh --channel C... --message '...' --check-only   # 送信せず重複だけ確認
#   slack-send-once.sh --prune-dry-run [--ledger <path>]              # 何件落ちるかだけ数える（削除しない）
#
# 終了コード: 0 = 送信済み（または既に投稿ありでスキップ）、1 = 送信失敗（送信を試みたが履歴にもなし）。
#   --check-only のときは 0 = 重複あり、1 = 重複なし、2 = 確認できず。
#   3 = 送信前の履歴確認ができず送らなかった（cannot-verify / pre-check失敗）。
#   4 = 送る文面がスレッド内の他者の既存投稿と一字一句一致していたので
#       送らなかった（Yanayana さんの reply_once.py の再発防止チェック、
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
LEDGER_FILE="$HOME/hooks/state/slack-send-log.jsonl"
LEDGER_ARCHIVE_FILE="$HOME/hooks/state/slack-send-log-archive.jsonl"
AMBIGUOUS_FILE="$HOME/hooks/state/slack-ambiguous-posts.jsonl"
CHECK_VIA=""
FOUND_TS=""

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
if [ -n "$PERMALINK" ]; then
  CHANNEL=$(printf '%s' "$PERMALINK" | sed -n 's#.*/archives/\([A-Z0-9]*\)/p\([0-9]*\).*#\1#p')
  PTS=$(printf '%s' "$PERMALINK" | sed -n 's#.*/archives/[A-Z0-9]*/p\([0-9]*\).*#\1#p')
  if [ -n "$PTS" ]; then
    THREAD_TS="${PTS:0:10}.${PTS:10}"
  fi
fi

if [ -z "$CHANNEL" ]; then echo "channel is required (--channel or --permalink)" >&2; exit 2; fi
if [ -z "$MESSAGE" ] && [ -z "$MESSAGE_FILE" ]; then echo "message is required (--message or --message-file)" >&2; exit 2; fi

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
  python3 - "$ts" "$CHANNEL" "$THREAD_TS" "$TMP_MSG" "$LEDGER_FILE" <<'PYEOF' 2>/dev/null || true
import json, sys
ts, channel, thread_ts, msg_path, ledger_path = sys.argv[1:6]
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
check_ledger() {
  python3 - "$CHANNEL" "$THREAD_TS" "$TMP_MSG" "$LEDGER_FILE" <<'PYEOF' 2>/dev/null
import json, sys
channel, thread_ts, msg_path, ledger_path = sys.argv[1:5]
try:
    with open(msg_path, encoding='utf-8') as f:
        target = f.read().strip()
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
        if (rec.get('text') or '').strip() == target:
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
    if m.get('user') == '$SELF_ID' and (m.get('text') or '').strip() == target:
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

# 履歴に自分の同一文面があるか確認する。
#   0 = 見つかった（その ts を出す）、1 = なし、2 = 履歴が読めなかった。
# ページング: --oldest に window の切り捨て時刻を渡し、next_oldest を
# 辿って全ページを確認する。1ページ目だけでは長いスレッドの直近分が
# 範囲外になるため（oldest-first の先頭50件問題）。
# チャンネル投稿で窓内に重複がなければ、続けて bounded チェックを行う。
check_duplicate() {
  local attempt=0 raw page_out window_rc=2
  local cutoff
  if [ -n "$THREAD_TS" ]; then
    # スレッド返信は時間の窓なしでスレッド全体を確認する。
    # Yama さんの指摘（2026-11-12）：15分を超えて遅れた再実行の同一文面
    # にも強くする。Yanayana さんも同じ指摘。
    cutoff=0
  else
    cutoff=$(python3 -c "import time; print(time.time() - $WINDOW_MINS * 60)")
  fi
  while [ "$attempt" -lt "$HISTORY_ATTEMPTS" ]; do
    attempt=$((attempt + 1))
    local oldest="$cutoff"
    if [ -n "$THREAD_TS" ]; then
      # スレッドは --oldest=親ts から古い順に辿る。--oldest=0 だと
      # 付けないのと同じで初回が「親＋最新N件」になり、next_oldest が
      # 最新メッセージを指して2ページ目が空になり、古い返信が見えない
      # （Yanayana さんの指摘・実スレッド検証 2026-11-12）。
      oldest="$THREAD_TS"
    fi
    while :; do
      local read_args=("$CHANNEL" --limit 50 --json --oldest "$oldest")
      if [ -n "$THREAD_TS" ]; then read_args+=(--thread-ts "$THREAD_TS"); fi
      raw=$("$SLACKCLI" conversations read "${read_args[@]}" "${WS_ARGS[@]}" 2>/dev/null || true)
      page_out=$(printf '%s' "$raw" | python3 -c "
import sys, json, time
raw = sys.stdin.read()
i = raw.find('{')
if i < 0:
    sys.exit(2)
try:
    d = json.loads(raw[i:])
except Exception:
    sys.exit(2)
target = open('$TMP_MSG', encoding='utf-8').read().strip()
cutoff = $cutoff
for m in d.get('messages', []):
    if m.get('user') != '$SELF_ID':
        continue
    try:
        ts = float(m.get('ts', '0'))
    except ValueError:
        continue
    if ts >= cutoff and (m.get('text') or '').strip() == target:
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
  # スレッド返信は窓チェック（＝スレッド全体チェック）で確定。
  if [ -n "$THREAD_TS" ]; then
    return 1
  fi
  # チャンネル投稿は bounded チェックへ。
  check_bounded
}

# 送信前の他者同一文面チェック（スレッド返信のみ）。
#   0 = 他者の同一文面あり、1 = なし、2 = 履歴が読めなかった。
# スレッド全件を読み直し、送る文面が他者の既存投稿と一字一句一致して
# いないか確認する（Yanayana さんの reply_once.py の再発防止チェック、
# 2026-11-14 を取り入れ。同日、Yanayana さんの返信の下書きを作る経路が
# 直近の Yama さんの投稿文面を拾ってそのまま送ってしまい、2人の返信が
# 一字一句同じになった事故の再発防止）。自分の返信との一致は
# check_duplicate が見るので、ここでは他者の投稿だけを見る。
# 読み直しは最大5回リトライし、それでも読めなければ cannot-verify
# （fail-closed。呼び出し側は終了コード3として扱う）。
check_other_duplicate() {
  local attempt=0 raw page_out oldest
  [ -n "$THREAD_TS" ] || return 1
  while [ "$attempt" -lt "$HISTORY_ATTEMPTS" ]; do
    attempt=$((attempt + 1))
    oldest="$THREAD_TS"
    while :; do
      raw=$("$SLACKCLI" conversations read "$CHANNEL" --limit 50 --json --oldest "$oldest" --thread-ts "$THREAD_TS" "${WS_ARGS[@]}" 2>/dev/null || true)
      page_out=$(printf '%s' "$raw" | python3 -c "
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
if d.get('has_more') and d.get('next_oldest'):
    print('MORE ' + str(d['next_oldest']))
    sys.exit(3)
print('DONE')
sys.exit(1)
")
      local rc=$?
      if [ "$rc" -eq 0 ]; then
        echo "other user's identical post found: ${page_out#OTHER_DUP }"
        return 0
      elif [ "$rc" -eq 1 ]; then
        return 1
      elif [ "$rc" -eq 3 ]; then
        oldest="${page_out#MORE }"
        continue
      fi
      break
    done
    if [ "$attempt" -lt "$HISTORY_ATTEMPTS" ]; then sleep $((1 << (attempt - 1))); fi
  done
  return 2
}

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

# --- 1b. 他者の同一文面チェック（スレッド返信のみ） ---
# Yanayana さんの reply_once.py の再発防止チェック（2026-11-14）を取り入れ。
# 送る文面がスレッド内の他者の既存投稿と一字一句一致していたら送らない。
# 読み直しができなければ送らずに止める（fail-closed、終了コード3）。
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
fi

# --- 2. 送信 ---
SEND_ARGS=(messages send --recipient-id "$CHANNEL" --message-file "$TMP_MSG" --json)
if [ -n "$THREAD_TS" ]; then SEND_ARGS+=(--thread-ts "$THREAD_TS"); fi
SEND_OUT=$("$SLACKCLI" "${SEND_ARGS[@]}" "${WS_ARGS[@]}" 2>&1)
SEND_RC=$?
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
