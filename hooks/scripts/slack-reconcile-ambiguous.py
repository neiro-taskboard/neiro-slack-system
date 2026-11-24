#!/usr/bin/env python3
"""Periodic reconciliation of ambiguous (arrival-unknown) send records.

Yanayana さんの reconcile_ambiguous.py の取り込み（2026-11-14）。
うちにはこれまで、送信前チェックや到達確認で投稿が見つかったときに
ambiguous 記録へ resolved_by_thread_check の訂正を入れる「発見時の訂正」
（slack-send-once.sh の resolve_ambiguous）しかなかった。その後の送信
チェックが二度と起きない対象は拾われないままになるので、1時間に1回まで
定期的にライブのスレッド／チャンネル履歴と照合し、届いていたことが
分かった ambiguous 記録に後から訂正を入れる。

照合のやり方（Yanayana さんの共有 2026-11-14 に合わせる）:
  - 照合の対象は record_ambiguous に文面（text）が残っている
    ambiguous_post_outcome 記録（2026-11-14 の取り込みで文面を残す
    ようにしたので、それ以降の記録が対象）。
  - 文面のない記録は文面照合では原理的に解決できないので、
    resolved とは別の終端 closed_no_text で閉じる（Yanayana さんの
    案の取り込み、2026-11-14）。元の記録は残し、(channel, thread_ts)
    ごとに closed_no_text エントリを1行追加する。閉じた後に別の
    pre-send チェックで返信が見つかれば resolved_by_thread_check が
    別途つく（終端は未解決のまま放置しないためのもので、記録自体は
    消えない）。closed_no_text より後に出た文面なし記録は、新しい
    グループとして改めて閉じる。
  - 同じ (channel, thread_ts, text, 印) は1つの対象としてまとめる。
    元の記録は履歴として残し、消さない。印（event_id / request_id）
    が違う記録は文面が同じでも別の対象にする（同じ文面の別イベント
    の送信を1つにまとめてしまうと、印ガードの意味がなくなるため）。
    印のない記録は従来どおり (channel, thread_ts, text) でまとまる。
  - 一致は正規化して比べる。Slack から返される text は HTML エスケープ
    （&amp; など）されていたり、送信時と改行・空白の入り方が違ったり
    することがあるため、比較の前に揃える（Yama さんの共有 2026-11-14、
    Yanayana さんも採用）。完全一致（==）のままだと、届いているのに
    照合できない取りこぼしになる。グルーピングのキーには使わず、
    比較のときだけ使う。
  - 印ガード（2026-11-15、muse取締役会 #slack-へようこそ
    1794621025.648949）。送信時経路（slack-send-once.sh の台帳照合と
    スレッド文面チェック）に入れたのと同じ形を、定期照合にも入れる。
    照合対象の記録と履歴の投稿の両方に印（event_id / request_id）が
    あり、かつ違う場合は、その投稿を文面一致の候補から外す。どちらか
    に印がなければ従来どおり文面で照合する（後方互換：既存の印なし
    記録も壊さない）。記録の印は record_ambiguous() が残すもの
    （2026-11-15 から。印のない送信の記録は従来どおり印なし）。
    履歴の印は slack-read-metadata.py --dump（include_all_metadata
    付き）で取る。slackcli の conversations read では metadata が
    返ってこないため、印の違いが見えない。dump の窓は直近100件
    （スレッドは親＋直近100件）で、窓に入らない古い候補は印が
    分からないので文面で拾う（従来どおり）。dump が読めなければ
    その対象は cannot_verify（fail-closed：次の定期実行で拾い直す）。
    印ガードで外れた候補の ts は使用済みにしない（解決に使って
    いないため）。
  - 照合対象は自分（ねいろ）の投稿だけ。他者の同一文面は拾わない。
  - 見つかった返信の ts は、対象スレッドの親 ts より後、かつ記録時刻の
    1時間前より後であること。対象より前に出た同一文面は誤照合しない
    （2026-11-14、Yama さんの監査指摘から Yanayana さんが採用した絞り込み）。
  - 解決に使った reply ts は使い回さない。同じ文面の対象が2つあっても
    1つの返信に2重に解決されない（Yanayana さんの残る穴への対応案を
    こちらでは最初から入れる）。既存の resolved_by_thread_check の
    found_ts も使用済みとして扱う。
  - ページングは oldest から順に全件読む。最初に見つかったもの＝最も
    古い該当返信を取る。

頻度（Yanayana さんの形）:
  - state ファイルで「前回から1時間以内ならスキップ」と自己ゲートする。
    ポーリングフック（slack-new-messages.sh）から毎回呼ばれても、走るのは
    1時間に1回まで。手動なら --force で即実行できる。
  - 履歴が読めなかった対象は訂正しない（fail-closed）。次の定期実行で
    拾い直す。

flock（Yanayana さんの形、2026-11-14 の取り込み）:
  - 照合スクリプト自体が slack-reconcile-ambiguous.lock に
    LOCK_EX|LOCK_NB の flock を取り、スイープ全体を直列化する。
    ポーリングフック全体の flock（slack-events.lock）の中から呼ばれる
    通常の経路ではそもそも重ならないが、手動で直接走らせた場合は
    その flock の外になる。スクリプト側にも flock を入れることで、
    手動実行の経路もカバーする（Yanayana さんの整理）。
  - ロックが取れなければ gated として抜ける（終了コード 0）。
    自己ゲートのスキップと同じ扱いで、ポーリングフック側は
    '"gated": true' を見てログに残さないが、実行ログには
    {"event": "gated", "reason": "lock_busy"} の1行を残す
    （Yanayana さんの修正 2026-11-14 の取り込み。gated の回が
    痕跡なしだと、走らなかった回と区別がつかないため）。
  - ロックはプロセス終了まで保持し、終了時に OS が解放する。
    last_run の読み書き（チェックと書き込みの間）もロックの中に
    入るので、手動実行とポール経由の実行が同時に走っても
    ゲートのレースは起きない。

実行ログ（Yama さんの形、2026-11-14 の取り込み）:
  - 実行のたびに、開始1行と終了1行（終了コード付き）を
    slack-reconcile-ambiguous-runs.jsonl へ追記する。途中で切られた
    回は「開始だけあって終了がない」形で残るので、切れたことが後から
    分かる。ポーリングフックからの呼び出しは 120秒の timeout 付きで、
    切られた回はそれまで何も残らない形だった。
  - その回のサマリ（標準出力と同じ JSON）は
    slack-reconcile-ambiguous-last-run.json に上書き保存する。
  - 対象ごとの進捗追記は入れない。結果は元々 ambiguous 台帳に
    書かれているので、ログでも持つと二重管理になる（Yama さんの
    検討と同じ結論）。
  - ログ書き込みの失敗で本体が止まらないよう、失敗しても飲み込む。
  - ゲートでスキップされた回は実行ではないので、開始・終了は残さない
    が、{"event": "gated", "reason": ...} の1行だけ残す（Yanayana さんの
    修正 2026-11-14 の取り込み）。理由は interval_gate（前回から1時間
    以内）と lock_busy（別の照合プロセスがロックを持っている）の2種。
    ポーリングフックは gated の出力を破棄するので、この1行が唯一の
    痕跡になる。

cannot_verify の終端（closed_unverifiable、中-3、2026-11-24）:
  - cannot_verify に終端状態がなく、履歴が読めない対象は毎時
    拾い直されて永久に残る（2026-11-14 のあた宛 DM 記録が実例）。
    連続24回（約1日）cannot_verify が続いた対象は、記録を残した
    まま closed_unverifiable として照合対象から外す。fail-closed
    の趣旨は保ちつつ永久リトライだけ止める（Yama さん・Yanayana
    さんと合意した本命案）。
  - 連続回数は状態ファイル
    slack-reconcile-ambiguous-cannot-verify.json に、対象キー
    （channel, thread_ts, 正規化文面, 印）のダイジェストで保存する。
    履歴が読めた回（解決でも不一致でも）は 0 にリセットする。
    対象が照合対象から消えたらカウントも消す。台帳への対象ごとの
    進捗追記は入れない（既存方針：二重管理を避ける）。
  - 1回の照合で大部分が失敗している回は、数え上げを進めない。
    cannot_verify が2件以上かつ試した対象の半数超のとき、その回は
    誰のカウントも進めない（障害が全体的なのか個別なのか区別でき
    ないため）。対象が1件だけのときは 2件以上にならないのでカウント
    は進む（1件だけでは全体か個別か分からず、進めないと永久に閉じ
    なくなるため）。
  - 24回到達時は台帳に closed_unverifiable エントリを1行追記する
    （channel, thread_ts, text, 印, closed_at, attempts=24,
    last_error, error_kind, reason）。元の記録は残す。error_kind は
    理由からの簡易分類（self_id_unknown / dump_unreadable /
    history_unreadable / other）。outcome が resolved とは別なので、
    「検証できたもの」とは混ざらない（Yanayana さんの条件2）。
  - 手動の戻す経路（Yanayana さんの条件3）：台帳に reopened
    エントリを1行追記すると、その (channel, thread_ts) の閉鎖が
    解除され、カウントが 0 から数え直しになる。reopened に text や
    印があれば、それに一致するグループだけを戻す。reopened より後に
    closed_unverifiable があればそちらが優先（最新の状態が勝つ）。
    自動の戻り（発見時の訂正 resolve_ambiguous、新しい記録での
    数え直し）は従来通り動く。reopened は --reopen-channel /
    --reopen-thread-ts（任意で --reopen-text）で追記する。台帳への
    直接追記はロック外になるので、このコマンドを使うこと。
  - 移行の通知（Yanayana さんの条件1）：closed_unverifiable や
    closed_invalid_channel への移行が起きた回は、その回のサマリに
    詳細を残すだけでなく、#slack-へようこそ の監査スレッドへ1行の
    通知を送る（slack-send-locked.sh 経由、--request-id 付き）。
    通知は台帳への追記の後に送る（送ったのに記録がない二重通知を
    防ぐ）。通知の失敗はスクリプトの終了コードに影響しない
    （best-effort）。--no-notify か環境変数 SLACK_RECONCILE_NOTIFY=0
    で止められる（テスト用）。
  - 閉じる記録に元の識別子と理由を残す（Yama さんの点）：channel・
    thread_ts・text・印をエントリにそのまま残す。

入口除外（closed_invalid_channel、Yanayana さんの補足A）:
  - channel が会話 ID の形状（C/D/G...）でない記録は、履歴を読まず
    に closed_invalid_channel として即終端する。実例は channel が
    ユーザーID（U...）のあた宛 DM 記録で、会話読み取りにユーザーID
    は渡せず、履歴は原理的に読めない。reason の "not sending" だけ
    を鍵にしないこと。2026-11-18 に同じ reason の記録が後に
    resolved_by_thread_check が付いた反例がある（別経路で送られて
    いた）。文面のない記録でも channel が無効ならこちらが優先する
    （closed_no_text より構造的な理由のため）。
  - closed_invalid_channel エントリは closed_no_text と同じ形
    （channel, thread_ts, count, reason, closed_at）。エントリより
    後の記録は新しいグループになる。

1回あたりの時間予算（Yanayana さんの補足B）:
  - 照合ループは開始からの経過時間を測り、90秒を超えたら残りの
    対象はその回は試さない。呼び出し側の timeout は120秒で、
    1対象の最悪コスト（2ワークスペース×5回リトライのバックオフ
    約30秒）を見込んで 90秒で切る。予算切れの対象は
    budget_skipped としてサマリに残し、cannot_verify にはせず、
    カウントも進めない（次回も対象として残る）。件数キャップは
    入れない（対象は少なく、時間が律速のため）。

使い方:
  slack-reconcile-ambiguous.py [--ambiguous-file PATH]
      [--last-run-file PATH] [--runs-file PATH]
      [--last-summary-file PATH] [--lock-file PATH]
      [--counts-file PATH] [--notify-cmd PATH] [--no-notify]
      [--force] [--dry-run] [--slackcli PATH] [--read-meta PATH]
  slack-reconcile-ambiguous.py --reopen-channel C... --reopen-thread-ts TS
      [--reopen-text TEXT] [--ambiguous-file PATH] [--lock-file PATH]
出力: JSON 1行 {"resolved": N, "targets": N, "closed_no_text": N,
  "skipped_no_text": N, "closed_invalid_channel": N,
  "closed_unverifiable": N, "budget_skipped": [...],
  "counts_suppressed": false, "notified": false,
  "cannot_verify": [...], "gated": false}
終了コード: 0 = 照合できた（解決なし・スキップも含む）。2 = 一部または
  全部の対象の履歴が読めなかった（cannot_verify）。訂正は送らないので
  安全に再実行できる。--reopen-* は reopened エントリの追記のみで、
  終了コード 0 = 追記した（対象が無いときも 0 だが reopened: false）。
"""
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX 環境では常に使える
    fcntl = None

SLACKCLI = "/home/hatch/workspace/bin/slackcli"
READ_META = "/home/hatch/hooks/scripts/slack-read-metadata.py"
IDS_PATH_ENV = "NEIRO_SLACK_IDS_PATH"
DEFAULT_IDS_PATH = "~/.config/neiro-slack/ids.json"
ID_SHAPE = re.compile(r"[UWBCTGDA]0[A-Z0-9]{8,}")
# メッセージの user フィールドに入るのはユーザーIDだけ（U... / Grid の
# W...）。Bot ID（B...）が self_id に入ると自分の投稿と一致せず、
# 常に照合できないので、ユーザーID専用の形状で検証する
# （2026-11-19、answered-check 第3本の agyレビュー指摘と同じ考え）。
USER_ID_SHAPE = re.compile(r"[UW]0[A-Z0-9]{8,}")
WORKSPACES = ("muse-board", "")  # チャンネルIDはワークスペースごとに
# 一意なので、読めた方が正しいワークスペース。muse-board から試す。
HISTORY_ATTEMPTS = 5  # slack-send-once.sh / slack-answered-check.py と同じ回数
BACKOFF_SECS = (1, 2, 4, 8)  # 指数バックオフ（同上）
MAX_PAGES = 20
PAGE_LIMIT = 50
GATE_SECS = 3600  # 1時間に1回まで（Yanayana の slack-reconcile.timer 相当）
GRACE_SECS = 3600  # 記録時刻の1時間前までを照合の下限とする猶予

AMBIGUOUS_FILE = "/home/hatch/hooks/state/slack-ambiguous-posts.jsonl"
LAST_RUN_FILE = "/home/hatch/hooks/state/slack-reconcile-ambiguous-last.json"
RUNS_FILE = "/home/hatch/hooks/state/slack-reconcile-ambiguous-runs.jsonl"
LAST_SUMMARY_FILE = "/home/hatch/hooks/state/slack-reconcile-ambiguous-last-run.json"
LOCK_FILE = "/home/hatch/hooks/state/slack-reconcile-ambiguous.lock"
COUNTS_FILE = "/home/hatch/hooks/state/slack-reconcile-ambiguous-cannot-verify.json"
CLOSE_AFTER_CONSECUTIVE = 24  # cannot_verify がこの回数連続したら終端
BUDGET_SECS = 90  # 照合1回あたりの時間予算（呼び出し側 timeout 120秒）
# 会話読み取りに渡せるチャンネルIDの形状。C...（チャンネル）、
# D...（DM）、G...（グループDM・旧プライベートチャンネル）のみ。
CHANNEL_ID_SHAPE = re.compile(r"[CDG]0[A-Z0-9]{8,}")
NOTIFY_CMD = "/home/hatch/hooks/scripts/slack-send-locked.sh"
NOTIFY_WORKSPACE = "muse-board"
# 移行通知の宛先スレッド（中-3の議論スレッドの親 ts）。
# チャンネルIDは ids.json（channel_welcome_muse_torishimariyakai）
# から読むので、ここに実IDは書かない。親 ts はタイムスタンプのため
# 直書きする。
NOTIFY_THREAD_TS = "1795265680.893129"


class LockBusy(Exception):
    """別の照合プロセスがロックを持っている（2つ目は gated で抜ける）。"""


_LOCK_FP = None  # プロセス終了まで保持し、flock を維持する


def acquire_lock(path):
    """照合スクリプト専用の flock を取る。取れなければ LockBusy。

    LOCK_EX|LOCK_NB でスイープ全体を直列化する（Yanayana さんの
    reconcile_ambiguous.py と同じ形）。ファイルはプロセス終了まで
    開いたままにし、終了時に OS がロックを解放する。fcntl が無い環境
    ではロックを取れないので OSError とする（fail-closed：直列化
    できないまま走らせない）。
    """
    global _LOCK_FP
    if fcntl is None:
        raise OSError("fcntl is not available; cannot serialize runs")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fp = open(path, "a+", encoding="utf-8")
    try:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fp.close()
        raise LockBusy(path)
    _LOCK_FP = fp
    return fp


def normalize_text(t):
    """照合用の文面正規化（Yama さんの共有 2026-11-14、Yanayana さんも採用）。

    空白・改行・エスケープ違いを吸収する。HTML エンティティを戻し、
    改行を \n に揃え、空白の連続を1文字にまとめて前後を除く。
    グルーピングのキーには使わず、比較のときだけ使う。
    """
    if not t:
        return ""
    s = html.unescape(t)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def parse_slack_json(raw):
    """slackcli の出力から JSON 部分だけ抜いて読む。読めなければ None。"""
    i = raw.find("{")
    if i < 0:
        return None
    try:
        return json.loads(raw[i:])
    except json.JSONDecodeError:
        return None


def load_ids():
    """ids.json を読む。読めなければ None（fail-closed）。

    slack-ids.sh と answered-check.py と同じ前提：ファイルが無い・
    JSON が壊れている・オブジェクトでない・他ユーザーに読める権限
    （0600 でない）のいずれも None を返す。パスは環境変数
    NEIRO_SLACK_IDS_PATH で差し替えられる（テスト用）。
    """
    path = os.path.expanduser(
        os.environ.get(IDS_PATH_ENV) or DEFAULT_IDS_PATH)
    try:
        if os.stat(path).st_mode & 0o077:
            return None
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def ids_value(ids, key, shape=ID_SHAPE):
    """ids から ID 形状の値だけを取り出す。無ければ None。

    Bot のユーザーIDを読むときは shape=USER_ID_SHAPE を渡すこと
    （B... の Bot ID が誤登録されていても使わない）。
    """
    value = ids.get(key) if ids else None
    if isinstance(value, str) and shape.fullmatch(value):
        return value
    return None


def self_user_id(workspace, slackcli=SLACKCLI):
    """自分のユーザーID。whoami が取れなければ ids.json にフォールバック。

    ワークスペースは名前（muse-board）でもIDでも渡されることがある
    ので、IDの方は ids.json の値と突き合わせて判別する。未知の
    ワークスペース名・IDや、ids.json が読めない場合は None を返し、
    呼び出し側が fail-closed で止める（既定の Bot ID への
    フォールスルーは誤照合のもとになる。2026-11-19、第2本の
    agyレビュー指摘と同じ考え）。
    """
    try:
        args = [slackcli, "auth", "whoami"]
        if workspace:
            args += ["--workspace", workspace]
        proc = subprocess.run(args, capture_output=True, text=True,
                              timeout=30)
        # 終了コードも抽出結果の形状も確認する（2026-11-19、agyレビュー
        # 指摘）。whoami が失敗時に "(UNAUTHORIZED)" のような文字列を
        # stdout に出しても、正規表現が U 始まりに前方一致して誤った
        # ID として返さないようにする。
        if proc.returncode == 0:
            m = re.search(r"\([UW][A-Z0-9]+", proc.stdout)
            if m:
                candidate = m.group(0)[1:]
                if USER_ID_SHAPE.fullmatch(candidate):
                    return candidate
    except Exception:
        pass
    ids = load_ids()
    if ids is None:
        return None
    ws = workspace or ""
    if ws == "":
        key = "bot_neiro_neiro_task_board"
    elif ws == "muse-board":
        key = "bot_neiro_muse_torishimariyakai"
    elif ws == ids_value(ids, "workspace_muse_torishimariyakai"):
        key = "bot_neiro_muse_torishimariyakai"
    elif ws == ids_value(ids, "workspace_neiro_task_board"):
        key = "bot_neiro_neiro_task_board"
    else:
        return None
    return ids_value(ids, key, USER_ID_SHAPE)


def record_mark(rec):
    """ambiguous 記録の印を (kind, value) で取る。無ければ None。

    event_id と request_id は送信時に相互排他（slack-send-once.sh が
    両方指定を拒否する）なので、両方載っている記録は event_id を優先
    する。印ガードの比較はこのタプル同士で行う（台帳照合・スレッド
    文面チェックと同じ形）。
    """
    if rec.get("event_id"):
        return ("event_id", str(rec["event_id"]))
    if rec.get("request_id"):
        return ("request_id", str(rec["request_id"]))
    return None


def classify_error(reason):
    """cannot_verify の理由を簡易分類する（closed_unverifiable 用）。

    判定は部分文字列で行い、どれにも当たらなければ other（将来
    新しい理由が増えても other にフォールバックする）。順序に注意：
    self user id unknown の失敗は "history unreadable in all
    workspaces: ..." で包まれて返るので、先に見る。
    """
    r = reason or ""
    if "self user id unknown" in r:
        return "self_id_unknown"
    if "dump unreadable" in r:
        return "dump_unreadable"
    if "history unreadable" in r:
        return "history_unreadable"
    return "other"


def target_key(channel, thread_ts, text, mark):
    """対象キーのダイジェスト。counts ファイルのキーにする。

    (channel, thread_ts, 正規化文面, 印) を JSON にして sha256。
    文面そのものをキーにしない（長い文面がキーになるとファイルが
    読みにくくなるため）。表示用の情報は counts の値側に残す。
    """
    payload = json.dumps(
        [channel, thread_ts, normalize_text(text),
         list(mark) if mark else None],
        ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def is_valid_channel(channel):
    """会話読み取りに渡せるチャンネルIDか。

    C...（チャンネル）、D...（DM）、G...（グループDM・旧プライベート
    チャンネル）のみ。ユーザーID（U...）などは会話読み取りに渡せず、
    履歴は原理的に読めないので無効とする。
    """
    return bool(channel) and CHANNEL_ID_SHAPE.fullmatch(channel) is not None


def _earlier(a, b):
    """2つの epoch 秒のうち早い方。None は「不明」として遅い扱い。"""
    if a is None:
        return b
    if b is None:
        return a
    return a if a < b else b


def parse_record_time(value):
    """record_ambiguous の time（例 2026-11-12T11:30:25+0000）を epoch 秒に。"""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z").timestamp()
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


def is_reply_after(msg_ts, lower_ts):
    """返信 ts が下限 ts より後か（数値比較、だめなら文字列比較）。"""
    if msg_ts is None or lower_ts is None:
        return False
    try:
        return float(msg_ts) > float(lower_ts)
    except (TypeError, ValueError):
        return str(msg_ts) > str(lower_ts)


def read_page(slackcli, channel, oldest, thread_ts, workspace):
    """slackcli で履歴1ページを読む。読めなければ RuntimeError。"""
    args = [slackcli, "conversations", "read", channel,
            "--limit", str(PAGE_LIMIT), "--json", "--oldest", str(oldest)]
    if thread_ts:
        args += ["--thread-ts", thread_ts]
    if workspace:
        args += ["--workspace", workspace]
    r = subprocess.run(args, capture_output=True, text=True, timeout=60)
    data = parse_slack_json(r.stdout or "")
    if data is None or not isinstance(data.get("messages"), list):
        raise RuntimeError(f"slackcli read failed (exit {r.returncode})")
    return data


def read_page_retry(slackcli, channel, oldest, thread_ts, workspace):
    """1ページの読み取りを最大5回リトライする（間隔 1・2・4・8 秒）。"""
    last_exc = None
    for attempt in range(HISTORY_ATTEMPTS):
        try:
            return read_page(slackcli, channel, oldest, thread_ts, workspace)
        except Exception as exc:  # noqa: BLE001 - リトライ対象は広く取る
            last_exc = exc
            if attempt < HISTORY_ATTEMPTS - 1:
                time.sleep(BACKOFF_SECS[min(attempt, len(BACKOFF_SECS) - 1)])
    raise RuntimeError(f"history unreadable after retries: {last_exc}")


def read_all(page_reader, channel, thread_ts, oldest):
    """履歴の全メッセージを古い順に集める（oldest からページング）。"""
    messages = []
    cur = oldest
    for _ in range(MAX_PAGES):
        data = page_reader(channel, thread_ts, cur)
        messages.extend({"ts": m.get("ts"), "user": m.get("user"),
                         "text": m.get("text") or ""}
                        for m in data.get("messages", []))
        if not data.get("has_more"):
            break
        nxt = data.get("next_oldest")
        if not nxt or nxt == cur:
            break
        cur = nxt
    return messages


def load_records(path):
    """ambiguous ファイルを読む。読めなければ空リスト（初回は記録なし）。"""
    records = []
    try:
        with open(path, encoding="utf-8") as f:
            for lineno, raw in enumerate(f, 1):
                line = raw.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    # 壊れた行は飛ばすが、黙って飛ばさない。台帳の欠損に
                    # 気づけるよう stderr へ警告を出す（2026-11-22、Yama
                    # さんの指摘を Yanayana さんが直したのと同じ形）。
                    print("warning: skipping broken ambiguous record "
                          "%s:%d: %s" % (path, lineno, line[:120]),
                          file=sys.stderr)
                    continue
    except OSError:
        pass
    return records


def build_targets(records):
    """照合対象を作る。

    (channel, thread_ts, text, 印) ごとに1対象。文面のない記録は飛ばす
    （空白だけの文面も文面なし扱い）。channel が無効（会話 ID の形状
    でない）な記録も飛ばす（closed_invalid_channel 側で扱う）。
    印が違う記録は文面が同じでも別の対象にする（同じ文面の別イベント
    の送信を1つにまとめてしまうと、印ガードの意味がなくなるため）。
    印のない記録は従来どおり (channel, thread_ts, text) でまとまる。
    closed_unverifiable エントリがあるグループは対象から外す。
    エントリより前の記録は閉じた分として控え、エントリより後に出た
    記録は新しいグループになる（closed_no_text と同じ形）。
    reopened エントリがあるグループは、閉じた分を戻して改めて対象に
    する（その対象は reopened=True になり、reconcile() 側でカウント
    を 0 から数え直す）。reopened に text や印があれば、それに一致
    するグループだけを戻す。
    戻り値: (targets, used_found_ts)
      targets: [{channel, thread_ts, text, mark, earliest_time,
                 key, reopened}]
        key は counts ファイル用のダイジェスト。reopened はこの対象が
        reopened で戻ったものかどうか。
      used_found_ts: {(channel, thread_ts): {found_ts, ...}} — 既存の
        resolved_by_thread_check の found_ts は使用済みとして扱う。
    """
    targets = {}
    used = {}
    closed_seg = {}  # key -> earliest_time（閉じた分の記録）
    for rec in records:
        channel = rec.get("channel") or ""
        thread_ts = rec.get("thread_ts") or ""
        outcome = rec.get("outcome")
        if outcome == "resolved_by_thread_check":
            found = rec.get("found_ts")
            if found:
                used.setdefault((channel, thread_ts), set()).add(found)
            continue
        if outcome == "closed_unverifiable":
            key = (channel, thread_ts, rec.get("text") or "",
                   record_mark(rec))
            if key in targets:
                closed_seg[key] = _earlier(closed_seg.get(key),
                                           targets[key]["earliest_time"])
                del targets[key]
            continue
        if outcome == "reopened":
            rtext = rec.get("text")
            rmark = record_mark(rec)
            for key in list(closed_seg):
                if key[0] != channel or key[1] != thread_ts:
                    continue
                if rtext is not None \
                        and normalize_text(rtext) != normalize_text(key[2]):
                    continue
                if rmark is not None and rmark != key[3]:
                    continue
                seg_time = closed_seg.pop(key)
                if key in targets:
                    targets[key]["earliest_time"] = _earlier(
                        targets[key]["earliest_time"], seg_time)
                    targets[key]["reopened"] = True
                else:
                    targets[key] = {
                        "channel": key[0], "thread_ts": key[1],
                        "text": key[2], "mark": key[3],
                        "earliest_time": seg_time,
                        "key": target_key(key[0], key[1], key[2], key[3]),
                        "reopened": True}
            continue
        if outcome != "ambiguous_post_outcome":
            continue
        if not is_valid_channel(channel):
            continue
        text = rec.get("text") or ""
        if not text.strip():
            # 空白だけの文面も文面なし扱い（Yamaさんの指摘 2026-11-14：
            # " " は not text では弾かれずキーになりうる）。
            continue
        mark = record_mark(rec)
        key = (channel, thread_ts, text, mark)
        rec_time = parse_record_time(rec.get("time"))
        if key not in targets:
            targets[key] = {"channel": channel, "thread_ts": thread_ts,
                            "text": text, "mark": mark,
                            "earliest_time": rec_time,
                            "key": target_key(channel, thread_ts, text,
                                              mark),
                            "reopened": False}
        elif rec_time is not None:
            cur = targets[key]["earliest_time"]
            if cur is None or rec_time < cur:
                targets[key]["earliest_time"] = rec_time
    ordered = sorted(targets.values(),
                     key=lambda t: (t["earliest_time"] is None,
                                    t["earliest_time"] or 0))
    return ordered, used


def build_no_text_groups(records):
    """文面のない ambiguous 記録のうち、まだ閉じていないグループを返す。

    (channel, thread_ts) ごとに数え、closed_no_text エントリが出たら
    それより前の文面なし記録は終端済みとして数え直す（エントリより
    後に出た文面なし記録は新しいグループになる）。
    戻り値: [{channel, thread_ts, count, earliest_time}]（出現順）。
    """
    groups = {}
    order = []
    for rec in records:
        channel = rec.get("channel") or ""
        thread_ts = rec.get("thread_ts") or ""
        key = (channel, thread_ts)
        outcome = rec.get("outcome")
        if outcome == "closed_no_text":
            if key in groups:
                groups[key]["count"] = 0
                groups[key]["earliest_time"] = None
            continue
        if outcome != "ambiguous_post_outcome":
            continue
        if not is_valid_channel(channel):
            # channel が無効な記録は closed_invalid_channel 側で扱う。
            continue
        if (rec.get("text") or "").strip():
            continue
        if key not in groups:
            groups[key] = {"channel": channel, "thread_ts": thread_ts,
                           "count": 0, "earliest_time": None}
            order.append(key)
        g = groups[key]
        g["count"] += 1
        t = parse_record_time(rec.get("time"))
        if t is not None and (g["earliest_time"] is None
                              or t < g["earliest_time"]):
            g["earliest_time"] = t
    return [groups[k] for k in order if groups[k]["count"] > 0]


def build_invalid_channel_groups(records):
    """channel が無効な ambiguous 記録のうち、まだ閉じていないグループを返す。

    (channel, thread_ts) ごとに数え、closed_invalid_channel エントリが
    出たらそれより前の記録は終端済みとして数え直す（エントリより後に
    出た記録は新しいグループになる）。文面の有無は問わない（文面なし
    でも channel が無効ならこちらが優先する。構造的な理由のため）。
    戻り値: [{channel, thread_ts, count, earliest_time}]（出現順）。
    """
    groups = {}
    order = []
    for rec in records:
        channel = rec.get("channel") or ""
        thread_ts = rec.get("thread_ts") or ""
        key = (channel, thread_ts)
        outcome = rec.get("outcome")
        if outcome == "closed_invalid_channel":
            if key in groups:
                groups[key]["count"] = 0
                groups[key]["earliest_time"] = None
            continue
        if outcome != "ambiguous_post_outcome":
            continue
        if is_valid_channel(channel):
            continue
        if key not in groups:
            groups[key] = {"channel": channel, "thread_ts": thread_ts,
                           "count": 0, "earliest_time": None}
            order.append(key)
        g = groups[key]
        g["count"] += 1
        t = parse_record_time(rec.get("time"))
        if t is not None and (g["earliest_time"] is None
                              or t < g["earliest_time"]):
            g["earliest_time"] = t
    return [groups[k] for k in order if groups[k]["count"] > 0]


def reconcile(records, history_reader, mark_lookup=None, counts=None,
              budget_secs=None, monotonic_fn=None):
    """照合の本体。history_reader はテストで差し替えられる。

    history_reader(channel, thread_ts, oldest) -> (messages, self_id)
    読めなければ例外を投げる。
    mark_lookup(channel, thread_ts) -> {ts: (kind, value) | None}：
    そのチャンネル／スレッドの投稿の印を返す。None のときは印が
    分からないものとして従来どおり文面で照合する（後方互換）。
    対象に印があり、文面一致の候補があるときだけ呼ぶ。読めなければ
    例外を投げ、その対象は cannot_verify（fail-closed）。
    counts: 対象キーごとの連続 cannot_verify 回数
      {key: {"consecutive": N, "last_error": ..., "error_kind": ...,
             "last_seen": ..., "channel": ..., "thread_ts": ...,
             "text_preview": ..., "mark": ...}}。None なら空扱い。
      呼び出し側が永続化する（main() が counts ファイルに保存する）。
    budget_secs: 照合1回あたりの時間予算（秒）。None なら無制限。
      開始からの経過時間がこれを超えたら、残りの対象はその回は
      試さない（budget_skipped）。呼び出し側の timeout（120秒）に
      当たらないための予算。
    monotonic_fn: 経過時間の取得（テストで差し替えられる）。
    戻り値: {"resolved": [...], "closed_no_text": [...],
             "skipped_no_text": N,
             "closed_invalid_channel": [...],
             "closed_unverifiable": [...],
             "cannot_verify": [(channel, thread_ts, reason)],
             "targets": N, "budget_skipped": [{channel, thread_ts}],
             "counts_suppressed": bool, "counts": {...}}
    resolved の各要素は {"channel", "thread_ts", "text", "found_ts",
    "mark"}（mark は対象の印か None）。
    closed_no_text の各要素は build_no_text_groups のグループで、
    今回 closed_no_text として閉じる対象。skipped_no_text は文面なし
    記録のうち終端済み（過去に閉じた）ものの件数。
    closed_invalid_channel の各要素は build_invalid_channel_groups
    のグループで、今回閉じる対象。
    closed_unverifiable の各要素は {"channel", "thread_ts", "text",
    "mark", "attempts", "last_error", "error_kind", "closed_at"} で、
    今回 24回連続 cannot_verify に達して閉じる対象。
    counts_suppressed は、大部分失敗の回で数え上げを進めなかった
    かどうか。counts は更新後の連続回数（呼び出し側が保存する）。
    カウントの扱い：
      - cannot_verify の回は +1。24回に達したら閉じてカウントを消す。
      - 履歴が読めた回（解決でも不一致でも）は 0 にリセットする
        （検証できたので、読めない streak は切れた）。
      - 予算切れで試さなかった対象は、増やしもリセットもしない。
      - 1回の照合で cannot_verify が2件以上かつ試した対象の半数超
        のときは、その回は誰のカウントも進めない。
    """
    if counts is None:
        counts = {}
    else:
        counts = dict(counts)
    if monotonic_fn is None:
        monotonic_fn = time.monotonic
    targets, used = build_targets(records)
    no_text_groups = build_no_text_groups(records)
    invalid_groups = build_invalid_channel_groups(records)
    total_no_text = sum(
        1 for r in records
        if r.get("outcome") == "ambiguous_post_outcome"
        and is_valid_channel(r.get("channel") or "")
        and not (r.get("text") or "").strip())
    skipped_no_text = total_no_text - sum(g["count"] for g in no_text_groups)
    target_keys = {t["key"] for t in targets}
    # counts は今の対象だけ残す（解決済み・終端済みのキーは消す）。
    counts = {k: v for k, v in counts.items() if k in target_keys}
    # reopened で戻った対象は数え直し（カウントを 0 から）。
    for t in targets:
        if t.get("reopened"):
            counts.pop(t["key"], None)
    resolved = []
    cannot_verify = []
    failed = []  # (target, reason) — 数え上げはループ後にまとめて行う
    budget_skipped = []
    deadline = None
    if budget_secs is not None:
        deadline = monotonic_fn() + budget_secs
    for target in targets:
        if deadline is not None and monotonic_fn() > deadline:
            budget_skipped.append({"channel": target["channel"],
                                   "thread_ts": target["thread_ts"]})
            continue
        channel = target["channel"]
        thread_ts = target["thread_ts"]
        text = target["text"]
        mark = target["mark"]
        rec_time = target["earliest_time"]
        # 照合の下限: スレッドなら親 ts、かつ記録時刻の1時間前より後。
        # チャンネル投稿なら記録時刻の1時間前より後（親がないため）。
        lower = None
        if thread_ts:
            lower = thread_ts
        if rec_time is not None:
            grace = str(rec_time - GRACE_SECS)
            if lower is None:
                lower = grace
            else:
                try:
                    lower = grace if float(grace) > float(lower) else lower
                except (TypeError, ValueError):
                    pass
        oldest = thread_ts if thread_ts else (
            str(rec_time - GRACE_SECS) if rec_time is not None else "0")
        try:
            messages, self_id = history_reader(channel, thread_ts, oldest)
        except Exception as exc:  # noqa: BLE001 - 読めない対象は飛ばして次へ
            reason = str(exc)[:200]
            cannot_verify.append((channel, thread_ts, reason))
            failed.append((target, reason))
            continue
        spent = used.setdefault((channel, thread_ts), set())
        target_norm = normalize_text(text)
        candidates = [m for m in messages
                      if m.get("user") == self_id
                      and normalize_text(m.get("text") or "") == target_norm
                      and m.get("ts") not in spent
                      and (lower is None
                           or is_reply_after(m.get("ts"), lower))]
        candidates.sort(key=lambda m: (m.get("ts") is None, m.get("ts") or ""))
        if not candidates:
            # 履歴が読めた（検証できた）が一致なし。streak をリセット。
            counts.pop(target["key"], None)
            continue
        if mark is not None and mark_lookup is not None:
            # 印ガード：対象の印と違う印の付いた候補を外す。印のない
            # 候補・dump の窓に入らず印が分からない候補は従来どおり
            # 文面で拾う。dump が読めなければこの対象は cannot_verify
            # （fail-closed：次の定期実行で拾い直す）。
            try:
                marks = mark_lookup(channel, thread_ts)
            except Exception as exc:  # noqa: BLE001 - 読めない対象は飛ばして次へ
                reason = str(exc)[:200]
                cannot_verify.append((channel, thread_ts, reason))
                failed.append((target, reason))
                continue
            kept = []
            for m in candidates:
                m_mark = marks.get(m.get("ts"))
                if m_mark is not None and tuple(m_mark) != tuple(mark):
                    continue
                kept.append(m)
            candidates = kept
            if not candidates:
                # 履歴も印も読めたが一致なし。streak をリセット。
                counts.pop(target["key"], None)
                continue
        found_ts = candidates[0].get("ts")
        spent.add(found_ts)
        # 履歴が読めたので streak をリセット（解決時も）。
        counts.pop(target["key"], None)
        resolved.append({"channel": channel, "thread_ts": thread_ts,
                         "text": text, "found_ts": found_ts,
                         "mark": mark})
    # 大部分失敗の回は数え上げを進めない（障害が全体的なのか個別な
    # のか区別できないため）。分母は試した対象（予算切れは除く）。
    attempted = len(targets) - len(budget_skipped)
    suppressed = (len(failed) >= 2 and attempted > 0
                  and len(failed) * 2 > attempted)
    closed = []
    if not suppressed:
        for target, reason in failed:
            key = target["key"]
            prev = counts.get(key) or {}
            consecutive = (prev.get("consecutive") or 0) + 1
            if consecutive >= CLOSE_AFTER_CONSECUTIVE:
                closed.append({
                    "channel": target["channel"],
                    "thread_ts": target["thread_ts"],
                    "text": target["text"], "mark": target["mark"],
                    "attempts": consecutive,
                    "last_error": reason[:200],
                    "error_kind": classify_error(reason),
                    "closed_at": utc_stamp()})
                counts.pop(key, None)
            else:
                counts[key] = {
                    "consecutive": consecutive,
                    "last_error": reason[:200],
                    "error_kind": classify_error(reason),
                    "last_seen": utc_stamp(),
                    "channel": target["channel"],
                    "thread_ts": target["thread_ts"],
                    "text_preview": (target["text"] or "")[:60],
                    "mark": list(target["mark"])
                    if target["mark"] else None}
    return {"resolved": resolved, "closed_no_text": no_text_groups,
            "skipped_no_text": skipped_no_text,
            "closed_invalid_channel": invalid_groups,
            "closed_unverifiable": closed,
            "cannot_verify": cannot_verify, "targets": len(targets),
            "budget_skipped": budget_skipped,
            "counts_suppressed": suppressed, "counts": counts}


def append_resolved(path, resolved, now=None):
    """訂正エントリを ambiguous ファイルへ追加する（元の記録は残す）。

    対象に印があった場合は訂正エントリにも印を残す（どの印の送信が
    届いたと確認したか、後から分かるように）。
    """
    if not resolved:
        return
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%S%z")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for r in resolved:
            rec = {"time": stamp, "channel": r["channel"],
                   "thread_ts": r["thread_ts"],
                   "outcome": "resolved_by_thread_check",
                   "found_ts": r["found_ts"], "text": r["text"]}
            mark = r.get("mark")
            if mark:
                rec[mark[0]] = mark[1]
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def append_closed_no_text(path, groups, now=None):
    """文面のない記録のグループを closed_no_text として終端する。

    元の記録は残し、終端エントリを1グループ1行で追加する。
    """
    if not groups:
        return
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%S%z")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for g in groups:
            rec = {"time": stamp, "channel": g["channel"],
                   "thread_ts": g["thread_ts"],
                   "outcome": "closed_no_text",
                   "reason": "no text recorded; cannot verify by text "
                             "match (closed_no_text)",
                   "count": g["count"]}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def append_closed_invalid_channel(path, groups, now=None):
    """channel が無効な記録のグループを closed_invalid_channel で終端する。

    closed_no_text と同じ形（1グループ1行、元の記録は残す）。
    """
    if not groups:
        return
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%S%z")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for g in groups:
            rec = {"time": stamp, "channel": g["channel"],
                   "thread_ts": g["thread_ts"],
                   "outcome": "closed_invalid_channel",
                   "closed_at": stamp,
                   "reason": "channel is not a readable conversation id "
                             "(closed_invalid_channel)",
                   "count": g["count"]}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def append_closed_unverifiable(path, closed, now=None):
    """closed_unverifiable エントリを台帳へ追加する（元の記録は残す）。

    closed の各要素は {"channel", "thread_ts", "text", "mark",
    "attempts", "last_error", "error_kind", "closed_at"}。
    対象に印があった場合はエントリにも印を残す（どの印の送信が
    検証できなかったか、後から分かるように）。
    """
    if not closed:
        return
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%S%z")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for c in closed:
            rec = {"time": stamp, "channel": c["channel"],
                   "thread_ts": c["thread_ts"],
                   "outcome": "closed_unverifiable",
                   "closed_at": c.get("closed_at") or stamp,
                   "attempts": c["attempts"],
                   "last_error": c["last_error"],
                   "error_kind": c["error_kind"],
                   "reason": "cannot_verify %d consecutive runs; "
                             "excluded from reconcile (closed_unverifiable)"
                             % c["attempts"],
                   "text": c["text"]}
            mark = c.get("mark")
            if mark:
                rec[mark[0]] = mark[1]
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def read_last_run(path):
    """前回の実行時刻（epoch 秒）。なければ None。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return float(data.get("last_run"))
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def write_last_run(path, epoch):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"last_run": epoch}, f)
    os.replace(tmp, path)


def load_counts(path):
    """連続 cannot_verify 回数の状態ファイルを読む。

    読めなければ空 dict（初回・壊れている場合）。壊れているときは
    警告を stderr に出し、保存もしない（壊れたファイルを空で上書き
    しないため）。戻り値は (counts, ok) で、ok が False のときは
    呼び出し側が保存を控える。
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}, True
    except (OSError, ValueError) as exc:
        print("warning: cannot-verify counts unreadable "
              "%s: %s" % (path, exc), file=sys.stderr)
        return {}, False
    if not isinstance(data, dict):
        print("warning: cannot-verify counts not an object: %s" % path,
              file=sys.stderr)
        return {}, False
    return {k: v for k, v in data.items() if isinstance(v, dict)}, True


def save_counts(path, counts):
    """連続回数の状態ファイルを原子的に保存する。失敗しても飲み込む。"""
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(counts, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as exc:
        print("warning: cannot-verify counts not saved "
              "%s: %s" % (path, exc), file=sys.stderr)


def utc_stamp(epoch=None):
    """UTC の ISO 時刻（例 2026-11-14T07:30:00+0000）。"""
    return datetime.fromtimestamp(
        time.time() if epoch is None else epoch,
        timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")


def append_run_line(path, event, fields):
    """実行ログへ開始・終了の1行を追記する。

    失敗しても飲み込んで本体を止めない（Yama さんの形と同じ）。
    """
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        rec = {"event": event, "time": utc_stamp()}
        rec.update(fields)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def write_last_summary(path, summary):
    """その回のサマリを上書き保存する。失敗しても飲み込む。"""
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def make_mark_lookup(read_meta, workspaces=WORKSPACES):
    """--dump で投稿の印を取る照会を作る。

    slack-read-metadata.py --dump（include_all_metadata 付き）を
    ワークスペースごとに試し、読めた方の結果を使う（history_reader
    のフォールバックと同じ形。チャンネルIDはワークスペースごとに
    一意なので、誤ったワークスペースの結果が混ざることはない）。
    呼び出しは (channel, thread_ts) -> {ts: (kind, value) | None}。
    dump の窓（直近100件）に入らない投稿は辞書に出てこないので、
    呼び出し側は .get(ts) が None のものを「印が分からない」として
    文面で拾う。同じ (channel, thread_ts) は1回の照合で何度も
    呼ばれないようキャッシュする（同じスレッドに複数の対象がある
    場合のため）。どのワークスペースでも読めなければ RuntimeError。
    """
    cache = {}

    def lookup(channel, thread_ts):
        key = (channel, thread_ts)
        if key in cache:
            return cache[key]
        last_exc = None
        for workspace in workspaces:
            args = [sys.executable, read_meta, "--channel", channel,
                    "--dump"]
            if thread_ts:
                args += ["--thread-ts", thread_ts]
            if workspace:
                args += ["--workspace", workspace]
            try:
                r = subprocess.run(args, capture_output=True, text=True,
                                   timeout=60)
                data = parse_slack_json(r.stdout or "")
            except Exception as exc:  # noqa: BLE001 - 次のワークスペースを試す
                last_exc = exc
                continue
            if not data or not data.get("ok"):
                last_exc = RuntimeError(
                    "dump unreadable: %s" % (data or {}).get("error", "no json"))
                continue
            marks = {}
            for m in data.get("messages", []):
                mk = m.get("mark")
                marks[str(m.get("ts", ""))] = (
                    (mk.get("kind"), mk.get("value")) if mk else None)
            cache[key] = marks
            return marks
        raise RuntimeError(f"dump unreadable in all workspaces: {last_exc}")

    return lookup


def notify_transitions(notify_cmd, closed_unverifiable, closed_invalid):
    """終端移行を #slack-へようこそ の監査スレッドへ1行で知らせる。

    通知は best-effort：送信の失敗は False を返すだけで、呼び出し側
    の終了コードには影響しない。チャンネルIDは ids.json から読む
    （実IDをスクリプトに直書きしないため）。ids.json が読めなければ
    通知しない（サマリと台帳には移行が残っている）。
    戻り値: 送信できたかどうか。
    """
    lines = ["照合の終端移行のお知らせです。"]
    for c in closed_unverifiable:
        preview = (c.get("text") or "")[:40]
        lines.append(
            "・closed_unverifiable：%s / %s「%s」（試行%d回、最後のエラー：%s）"
            % (c["channel"], c["thread_ts"] or "チャンネル直下", preview,
               c["attempts"], c["error_kind"]))
    for g in closed_invalid:
        lines.append(
            "・closed_invalid_channel：%s / %s（%d件、履歴を読めないチャンネルIDのため）"
            % (g["channel"], g["thread_ts"] or "チャンネル直下",
               g["count"]))
    lines.append(
        "詳細は照合サマリ（slack-reconcile-ambiguous-last-run.json）をご覧ください。")
    ids = load_ids()
    channel = ids_value(ids, "channel_welcome_muse_torishimariyakai") \
        if ids else None
    if channel is None:
        print("warning: notify skipped (channel id unreadable)",
              file=sys.stderr)
        return False
    try:
        proc = subprocess.run(
            [notify_cmd, "--channel", channel,
             "--thread-ts", NOTIFY_THREAD_TS,
             "--message", "\n".join(lines),
             "--workspace", NOTIFY_WORKSPACE,
             "--request-id", str(uuid.uuid4())],
            capture_output=True, text=True, timeout=30)
    except Exception as exc:  # noqa: BLE001 - 通知の失敗は本体を止めない
        print("warning: notify failed: %s" % exc, file=sys.stderr)
        return False
    if proc.returncode != 0:
        print("warning: notify failed (exit %d): %s" % (
            proc.returncode,
            (proc.stderr or proc.stdout or "").strip()[:200]),
            file=sys.stderr)
        return False
    return True


def reopen_entry(ambiguous_file, lock_file, channel, thread_ts, text=None):
    """reopened エントリを台帳へ追記する（手動の戻す経路）。

    照合スクリプトのロックを取ってから追記する（照合中の追記と
    並ばないように）。閉じた対象があるかは台帳を読んで確認し、
    無ければ追記せずにその旨を返す（無意味なエントリを残さない）。
    戻り値: 終了コード（0 = 正常。対象が無いときも 0 だが
    reopened: false）。
    """
    try:
        acquire_lock(lock_file)
    except LockBusy:
        print(json.dumps({"reopened": False, "reason": "lock_busy"},
                         ensure_ascii=False))
        return 0
    except OSError as exc:
        print(f"cannot acquire reconcile lock: {exc}", file=sys.stderr)
        return 2
    records = load_records(ambiguous_file)
    matched = False
    for rec in records:
        if rec.get("outcome") != "closed_unverifiable":
            continue
        if (rec.get("channel") or "") != channel:
            continue
        if (rec.get("thread_ts") or "") != thread_ts:
            continue
        if text is not None and normalize_text(rec.get("text") or "") \
                != normalize_text(text):
            continue
        matched = True
        break
    if not matched:
        print(json.dumps({"reopened": False, "reason": "no closed target",
                          "channel": channel, "thread_ts": thread_ts},
                         ensure_ascii=False))
        return 0
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")
    os.makedirs(os.path.dirname(ambiguous_file) or ".", exist_ok=True)
    entry = {"time": stamp, "channel": channel, "thread_ts": thread_ts,
             "outcome": "reopened",
             "reason": "manual reopen; closed_unverifiable lifted "
                       "(reopened)"}
    if text is not None:
        entry["text"] = text
    with open(ambiguous_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(json.dumps({"reopened": True, "channel": channel,
                      "thread_ts": thread_ts}, ensure_ascii=False))
    return 0


def main(argv):
    opts = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--ambiguous-file", "--last-run-file", "--runs-file",
                 "--last-summary-file", "--lock-file", "--slackcli",
                 "--read-meta", "--counts-file", "--notify-cmd",
                 "--reopen-channel", "--reopen-thread-ts",
                 "--reopen-text") \
                and i + 1 < len(argv):
            opts[a[2:]] = argv[i + 1]
            i += 2
        elif a in ("--force", "--dry-run", "--no-notify"):
            opts[a[2:]] = True
            i += 1
        else:
            print(f"unknown argument: {a}", file=sys.stderr)
            return 2
    ambiguous_file = opts.get("ambiguous-file") or AMBIGUOUS_FILE
    last_run_file = opts.get("last-run-file") or LAST_RUN_FILE
    runs_file = opts.get("runs-file") or RUNS_FILE
    last_summary_file = opts.get("last-summary-file") or LAST_SUMMARY_FILE
    lock_file = opts.get("lock-file") or LOCK_FILE
    slackcli = opts.get("slackcli") or SLACKCLI
    read_meta = opts.get("read-meta") or READ_META
    counts_file = opts.get("counts-file") or COUNTS_FILE
    notify_cmd = opts.get("notify-cmd") or os.environ.get(
        "SLACK_RECONCILE_NOTIFY_CMD") or NOTIFY_CMD
    notify_disabled = bool(opts.get("no-notify")) or \
        os.environ.get("SLACK_RECONCILE_NOTIFY") == "0"
    force = bool(opts.get("force"))
    dry_run = bool(opts.get("dry-run"))

    # 手動の戻す経路：reopened エントリの追記だけを行い、照合はしない。
    if opts.get("reopen-channel") is not None:
        return reopen_entry(ambiguous_file, lock_file,
                            opts["reopen-channel"],
                            opts.get("reopen-thread-ts") or "",
                            opts.get("reopen-text"))

    # スクリプト側の flock を先に取る。ゲートの読み書きもロックの中に
    # 入るので、手動実行とポール経由の実行のレースを防ぐ。取れなければ
    # gated として抜け、実行ログも残さない（自己ゲートと同じ扱い）。
    try:
        acquire_lock(lock_file)
    except LockBusy:
        # gated の回も実行ログに1行だけ残す（Yanayana さんの修正の
        # 取り込み）。ポーリングフックは gated の stdout を破棄するので、
        # この行が唯一の痕跡になる。書き込み失敗は飲み込む。
        append_run_line(runs_file, "gated",
                        {"reason": "lock_busy", "ts": time.time()})
        print(json.dumps({"resolved": 0, "targets": 0,
                          "closed_no_text": 0,
                          "skipped_no_text": 0,
                          "closed_invalid_channel": 0,
                          "closed_unverifiable": 0,
                          "budget_skipped": [],
                          "counts_suppressed": False,
                          "cannot_verify": [],
                          "gated": True, "lock_busy": True},
                         ensure_ascii=False))
        return 0
    except OSError as exc:
        print(f"cannot acquire reconcile lock: {exc}", file=sys.stderr)
        return 2

    now = time.time()
    if not force:
        last = read_last_run(last_run_file)
        if last is not None and now - last < GATE_SECS:
            append_run_line(runs_file, "gated",
                            {"reason": "interval_gate", "ts": now,
                             "last_run": last})
            print(json.dumps({"resolved": 0, "targets": 0,
                              "closed_no_text": 0,
                              "skipped_no_text": 0,
                              "closed_invalid_channel": 0,
                              "closed_unverifiable": 0,
                              "budget_skipped": [],
                              "counts_suppressed": False,
                              "cannot_verify": [],
                              "gated": True,
                              "last_run": last}, ensure_ascii=False))
            return 0

    records = load_records(ambiguous_file)
    append_run_line(runs_file, "start", {"ts": now, "dry_run": dry_run})

    def history_reader(channel, thread_ts, oldest):
        last_exc = None
        for workspace in WORKSPACES:
            try:
                def page_reader(ch, th, cur, ws=workspace):
                    return read_page_retry(slackcli, ch, cur, th, ws)
                messages = read_all(page_reader, channel, thread_ts, oldest)
                self_id = self_user_id(workspace, slackcli)
                if self_id is None:
                    # 自分のIDが特定できないまま照合すると、自分の投稿
                    # を1件も拾えず「届いていない」と誤って残してしまう
                    # ので、履歴が読めなかったのと同じ扱いにする
                    # （fail-closed：その対象は cannot_verify になり、
                    # 次の定期実行で拾い直す）。
                    raise RuntimeError(
                        "self user id unknown for workspace %r "
                        "(whoami and ids.json both failed)" % (workspace,))
                return messages, self_id
            except Exception as exc:  # noqa: BLE001 - 次のワークスペースを試す
                last_exc = exc
        raise RuntimeError(f"history unreadable in all workspaces: {last_exc}")

    counts, counts_ok = load_counts(counts_file)

    try:
        result = reconcile(records, history_reader,
                           make_mark_lookup(read_meta),
                           counts=counts, budget_secs=BUDGET_SECS)
        if not dry_run:
            append_resolved(ambiguous_file, result["resolved"])
            append_closed_no_text(ambiguous_file, result["closed_no_text"])
            append_closed_invalid_channel(
                ambiguous_file, result["closed_invalid_channel"])
            append_closed_unverifiable(
                ambiguous_file, result["closed_unverifiable"])
            if counts_ok:
                save_counts(counts_file, result["counts"])
            write_last_run(last_run_file, now)
    except Exception as exc:  # noqa: BLE001 - 予期せぬ失敗も終了行に残す
        print(f"reconcile failed: {exc}", file=sys.stderr)
        append_run_line(runs_file, "end",
                        {"ts": time.time(), "ok": False, "exit": 2,
                         "error": str(exc)[:200], "dry_run": dry_run})
        return 2
    # 移行の通知は台帳への追記の後に送る（送ったのに記録がない二重
    # 通知を防ぐ）。失敗しても終了コードには影響しない。
    notified = False
    if (not dry_run and not notify_disabled
            and (result["closed_unverifiable"]
                 or result["closed_invalid_channel"])):
        notified = notify_transitions(
            notify_cmd, result["closed_unverifiable"],
            result["closed_invalid_channel"])
    out = {"resolved": len(result["resolved"]),
           "resolved_detail": result["resolved"],
           "targets": result["targets"],
           "closed_no_text": sum(g["count"] for g in result["closed_no_text"]),
           "closed_no_text_detail": result["closed_no_text"],
           "skipped_no_text": result["skipped_no_text"],
           "closed_invalid_channel": sum(
               g["count"] for g in result["closed_invalid_channel"]),
           "closed_invalid_channel_detail": result["closed_invalid_channel"],
           "closed_unverifiable": len(result["closed_unverifiable"]),
           "closed_unverifiable_detail": result["closed_unverifiable"],
           "budget_skipped": result["budget_skipped"],
           "counts_suppressed": result["counts_suppressed"],
           "notified": notified,
           "cannot_verify": [{"channel": c, "thread_ts": t, "reason": r}
                             for c, t, r in result["cannot_verify"]],
           "gated": False, "dry_run": dry_run}
    exit_code = 0 if not result["cannot_verify"] else 2
    append_run_line(runs_file, "end",
                    {"ts": time.time(), "ok": exit_code == 0,
                     "exit": exit_code, "resolved": out["resolved"],
                     "targets": out["targets"],
                     "closed_no_text": out["closed_no_text"],
                     "closed_invalid_channel": out["closed_invalid_channel"],
                     "closed_unverifiable": out["closed_unverifiable"],
                     "budget_skipped": len(out["budget_skipped"]),
                     "cannot_verify": len(out["cannot_verify"]),
                     "dry_run": dry_run})
    write_last_summary(last_summary_file, out)
    print(json.dumps(out, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
