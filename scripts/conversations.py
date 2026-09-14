"""跨房間查看／搜尋／匯出對話——讀 Hermes 的 state.db 加 router 的 turn envelope。

對話內容的唯一事實來源是每個房間自己的 `data/<room_id>/state.db`（Hermes 寫的，
含 user／assistant／tool 訊息、tool call、reasoning、token 與成本）；router 另外
在 `data/_conversations/<room_id>.jsonl` 記每一輪的 envelope（結果狀態、送達與否、
耗時、session id），補上「沒進到 agent 的那些訊息」——observed／blocked／reset
在 state.db 裡根本不存在。這支腳本把兩邊接起來，全程唯讀
（`file:...?mode=ro`），不需要 docker、也不碰任何容器。詳見
docs/logging-design.md §5.7／§5.8。

使用方式
--------
    uv run python scripts/conversations.py rooms
    uv run python scripts/conversations.py show <room_id> [--session ID] [--with-tools]
    uv run python scripts/conversations.py search "關鍵字" [--room <room_id>]
    uv run python scripts/conversations.py export --room <room_id> --since 7d --format md --out /tmp/x
    uv run python scripts/conversations.py stats [--since 30d]

DATA_DIR 解析順序：`--data-dir` > 環境變數 `DATA_DIR` > `.env` 的 `DATA_DIR` >
`.env` 的 `HOST_DATA_DIR` > repo 的 `./data`。

隱私：`--with-tools`／`--with-reasoning` 預設關閉（工具回傳與 reasoning 可能含
Drive 檔名等資料）；群組發言者 id 預設輸出成短 hash，要原值加 `--raw`。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections.abc import Callable, Iterable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(REPO_ROOT / "src"))

from _env import load_env  # noqa: E402

from alice_office_router.conversation_log import TurnEnvelope  # noqa: E402
from alice_office_router.conversation_store import (  # noqa: E402
    UNRECORDED_OUTCOMES,
    EnvelopeIndex,
    MessageRow,
    check_schema_version,
    connect_state_db,
    format_ts,
    list_room_ids,
    parse_iso,
    parse_since,
    percentile,
    read_envelopes,
    read_messages,
    read_sessions,
    room_summary,
    search_messages,
    short_sender,
    state_db_path,
    summarize_tool_calls,
)

ENV_FILE = REPO_ROOT / ".env"

# 工具回傳可能是幾十 KB 的 JSON（Drive 檔案清單實測 18 KB），逐字印會把終端機
# 或匯出檔灌爆；只留足以辨識「這個工具回了什麼」的長度。
TOOL_RESULT_MAX_CHARS = 500

# search 的每房間上限，避免一個熱門詞把輸出洗掉。
DEFAULT_SEARCH_LIMIT = 20


@dataclass(frozen=True)
class Context:
    """一次執行共用的路徑與輸出選項。"""

    data_dir: Path
    conversations_dir: Path
    raw: bool


def resolve_data_dir(override: str | None) -> Path:
    """決定要讀哪個 DATA_DIR。

    Args:
        override: `--data-dir` 的值，沒給就是 None。

    Returns:
        第一個有值的來源：--data-dir、環境變數 DATA_DIR、.env 的 DATA_DIR、
        .env 的 HOST_DATA_DIR，最後 fallback 到 repo 的 ./data。
    """
    env = load_env(ENV_FILE)
    for candidate in (
        override,
        os.environ.get("DATA_DIR"),
        env.get("DATA_DIR"),
        env.get("HOST_DATA_DIR"),
    ):
        if candidate:
            return Path(candidate).expanduser()
    return REPO_ROOT / "data"


def warn(message: str) -> None:
    """把警告寫到 stderr，讓 stdout 保持可以 pipe 的乾淨輸出。"""
    print(f"warning: {message}", file=sys.stderr)


def _since(args: argparse.Namespace) -> float | None:
    """把 `--since` 解析成 unix timestamp；沒給是 None，格式錯就以 2 結束。

    打錯 `--since` 是使用者輸入錯誤，不是程式壞掉——印一行看得懂的用法，用
    exit code 2（argparse 的用法錯誤慣例）結束，不要吐一整段 traceback。
    """
    value: str | None = getattr(args, "since", None)
    if not value:
        return None
    try:
        return parse_since(value)
    except ValueError:
        warn(f"invalid --since {value!r}: use 7d / 36h / 90m or an absolute YYYY-MM-DD")
        raise SystemExit(2) from None


def _sender_label(envelope: TurnEnvelope, raw: bool) -> str:
    """組出群組發言者標示（1:1 房間回空字串）。"""
    if not envelope.is_group:
        return ""
    who = envelope.sender_id if raw else short_sender(envelope.sender_id)
    name = envelope.sender_name or "?"
    return f" [{name}|{who or '?'}]"


def _turn_tags(envelope: TurnEnvelope | None, raw: bool) -> str:
    """把 envelope 壓成一行標籤：結果狀態、耗時、送達與否、發言者。"""
    if envelope is None:
        return ""
    parts: list[str] = [envelope.outcome]
    if envelope.agent_duration_ms is not None:
        parts.append(f"{envelope.agent_duration_ms:.0f}ms")
    if envelope.rotated:
        parts.append("rotated")
    if envelope.delivered is False:
        parts.append("undelivered")
    if envelope.error:
        parts.append(envelope.error)
    return f"  ({', '.join(parts)}){_sender_label(envelope, raw)}"


def _truncate(text: str, limit: int) -> str:
    """超過 limit 就截斷並標示原長度。"""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}… (+{len(text) - limit} chars)"


def _render_message(
    message: MessageRow, envelope: TurnEnvelope | None, ctx: Context, args: argparse.Namespace
) -> list[str]:
    """把一則 state.db 訊息轉成要印的行；不需要輸出時回空 list。"""
    stamp = format_ts(message.timestamp)
    if message.role == "user":
        return [f"### {stamp} — user{_turn_tags(envelope, ctx.raw)}", message.content, ""]
    if message.role == "assistant":
        return _render_assistant(message, args)
    if message.role == "tool" and args.with_tools:
        body = _truncate(message.content, TOOL_RESULT_MAX_CHARS)
        return [f"**tool** `{message.tool_name or '?'}`", body, ""]
    return []


def _render_assistant(message: MessageRow, args: argparse.Namespace) -> list[str]:
    """assistant 一則可能是「純回覆」或「只發 tool call」，兩者分開呈現。"""
    lines: list[str] = []
    if args.with_reasoning and message.reasoning:
        lines += ["**reasoning**", message.reasoning, ""]
    calls = summarize_tool_calls(message.tool_calls)
    if calls and args.with_tools:
        lines += [f"**assistant → tools** {calls}", ""]
    if message.content:
        lines += ["**assistant**", message.content, ""]
    return lines


def _render_missing_turn(envelope: TurnEnvelope, ctx: Context) -> list[str]:
    """沒進到 agent 的那一輪：state.db 沒有紀錄，只有 envelope 記得原文。"""
    stamp = format_ts(parse_iso(envelope.ts))
    text = envelope.inbound_text or "(no text recorded)"
    return [f"### {stamp} — user{_turn_tags(envelope, ctx.raw)}", text, ""]


def render_transcript(
    messages: Iterable[MessageRow],
    index: EnvelopeIndex,
    ctx: Context,
    args: argparse.Namespace,
    *,
    since: float | None = None,
    session_id: str | None = None,
) -> list[str]:
    """把 state.db 訊息與「沒進 agent」的 envelope 合併成一份時間序逐字稿。"""
    entries: list[tuple[float, int, list[str]]] = []
    bound: set[int] = set()
    for message in messages:
        envelope = index.nearest(message.session_id, message.timestamp)
        if envelope is not None:
            bound.add(id(envelope))
        lines = _render_message(message, envelope, ctx, args)
        if lines:
            entries.append((message.timestamp, message.id, lines))
    for envelope in index.envelopes:
        if not _is_missing_from_state_db(envelope, bound):
            continue
        if session_id is not None and envelope.session_id != session_id:
            continue
        ts = parse_iso(envelope.ts)
        if since is not None and ts < since:
            continue
        entries.append((ts, 0, _render_missing_turn(envelope, ctx)))
    entries.sort(key=lambda entry: (entry[0], entry[1]))
    return [line for _, _, lines in entries for line in lines]


def _is_missing_from_state_db(envelope: TurnEnvelope, bound: set[int]) -> bool:
    """這一輪 state.db 裡有沒有對應紀錄——沒有的話逐字稿要由 envelope 補上。

    兩種情況：`observed`／`reset`／`blocked` 依定義就沒送進 agent；以及
    `agent_failed` 裡「連 Hermes 都沒碰到」的那一半——容器起不來、HTTP 連不上，
    Hermes 因此連 user 訊息都沒寫。後者若不補，一個容器壞掉的房間會印出一份空的
    逐字稿，正好把最需要看的東西藏起來。有碰到 Hermes 的 `agent_failed`（`bound`
    裡有，代表有訊息接到這個 envelope）已經從 state.db 印過了，不能再印一次。
    """
    if envelope.outcome in UNRECORDED_OUTCOMES:
        return True
    return envelope.outcome == "agent_failed" and id(envelope) not in bound


def _read_room[T](ctx: Context, room_id: str, read: Callable[[sqlite3.Connection], T]) -> T | None:
    """唯讀開一個房間的 state.db 讀一次，讀不動就 warn 並回 None。

    回 None 而不是讓例外往上炸，是因為每個呼叫端都在跑跨房間迴圈：一個房間的
    `-shm` 屬於別的 uid、檔案是壞的、或 schema 少了某張表，都不該讓整份輸出中斷。
    """
    path = state_db_path(ctx.data_dir, room_id)
    if not path.exists():
        warn(f"room {room_id} has no state.db at {path}")
        return None
    try:
        with closing(connect_state_db(path)) as connection:
            _, message = check_schema_version(connection)
            if message:
                warn(f"{room_id}: {message}")
            return read(connection)
    except sqlite3.Error as exc:
        warn(f"room {room_id}: unreadable state.db at {path} ({exc}); skipping")
        return None


def cmd_rooms(args: argparse.Namespace, ctx: Context) -> int:
    """列出每個房間的 session 數、訊息數、最後活動、token 與成本。"""
    room_ids = list_room_ids(ctx.data_dir)
    if not room_ids:
        print(f"no rooms with a state.db under {ctx.data_dir}")
        return 0
    header = (
        f"{'room':<40} {'sess':>5} {'msgs':>6} {'env':>5} {'in':>9} {'out':>8} {'usd':>8}  last"
    )
    print(header)
    print("-" * len(header))
    for room_id in room_ids:
        try:
            summary = room_summary(ctx.data_dir, ctx.conversations_dir, room_id)
        except sqlite3.Error as exc:
            warn(f"room {room_id}: unreadable state.db ({exc}); skipping")
            continue
        print(
            f"{summary.room_id:<40} {summary.session_count:>5} {summary.message_count:>6} "
            f"{summary.envelope_count:>5} {summary.input_tokens:>9} {summary.output_tokens:>8} "
            f"{summary.estimated_cost_usd:>8.4f}  {format_ts(summary.last_activity or 0.0)}"
        )
    return 0


def cmd_show(args: argparse.Namespace, ctx: Context) -> int:
    """印出一個房間的逐字稿（預設只有 user／assistant）。"""
    since = _since(args)
    result = _read_room(
        ctx,
        args.room_id,
        lambda connection: (
            read_sessions(connection),
            read_messages(connection, session_id=args.session, since=since),
        ),
    )
    if result is None:
        return 1
    sessions, messages = result

    index = read_envelopes(ctx.conversations_dir, args.room_id)
    print(f"# {args.room_id}")
    print(
        f"sessions: {len(sessions)}  messages: {len(messages)}  envelopes: {len(index.envelopes)}"
    )
    for session in sessions:
        print(
            f"  session {session.id}  {format_ts(session.started_at)}  "
            f"msgs={session.message_count} tools={session.tool_call_count} "
            f"in={session.input_tokens} out={session.output_tokens} "
            f"usd={session.estimated_cost_usd:.4f}"
        )
    print()
    for line in render_transcript(messages, index, ctx, args, since=since, session_id=args.session):
        print(line)
    return 0


def cmd_search(args: argparse.Namespace, ctx: Context) -> int:
    """跨房間全文搜尋（走 messages_fts_trigram，吃得下中文）。"""
    room_ids = [args.room] if args.room else list_room_ids(ctx.data_dir)
    total = 0
    for room_id in room_ids:
        hits = _read_room(
            ctx,
            room_id,
            lambda connection: search_messages(connection, args.term, limit=args.limit),
        )
        if hits is None:
            continue
        for hit in hits:
            total += 1
            body = _truncate(hit.content.replace("\n", " "), 160)
            print(f"{room_id}  {format_ts(hit.timestamp)}  {hit.role:<9} {body}")
    print(f"\n{total} match(es) for {args.term!r} across {len(room_ids)} room(s)", file=sys.stderr)
    return 0


@dataclass(frozen=True)
class RoomExport:
    """一個房間匯出所需的全部資料（只讀一次 state.db）。"""

    room_id: str
    sessions_count: int
    messages: list[MessageRow]
    index: EnvelopeIndex


def _load_export(args: argparse.Namespace, ctx: Context) -> RoomExport | None:
    """把一個房間要匯出的內容一次讀齊；房間不存在回 None。"""
    since = _since(args)
    result = _read_room(
        ctx,
        args.room,
        lambda connection: (
            read_sessions(connection, since=since),
            read_messages(connection, since=since),
        ),
    )
    if result is None:
        return None
    sessions, messages = result
    return RoomExport(
        room_id=args.room,
        sessions_count=len(sessions),
        messages=messages,
        index=read_envelopes(ctx.conversations_dir, args.room),
    )


def _write_markdown(
    data: RoomExport, args: argparse.Namespace, ctx: Context, out_dir: Path
) -> Path:
    """一個房間一個 .md：YAML front matter ＋ 逐輪對話，可直接 @file 進 Claude Code。"""
    since = _since(args)
    lines = [
        "---",
        f"room: {data.room_id}",
        f"exported_at: {datetime.now(tz=UTC).isoformat()}",
        f"session_count: {data.sessions_count}",
        f"message_count: {len(data.messages)}",
        "source: hermes state.db + router turn envelopes",
        "---",
        "",
        *render_transcript(data.messages, data.index, ctx, args, since=since),
    ]
    path = out_dir / f"{data.room_id}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write_jsonl(data: RoomExport, args: argparse.Namespace, ctx: Context, out_dir: Path) -> Path:
    """一個房間一個 .jsonl：每行一則訊息，附上該輪 envelope 的結果狀態。"""
    path = out_dir / f"{data.room_id}.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for message in data.messages:
            if message.role == "tool" and not args.with_tools:
                continue
            envelope = data.index.nearest(message.session_id, message.timestamp)
            record = {
                "room": data.room_id,
                "session_id": message.session_id,
                "ts": format_ts(message.timestamp),
                "role": message.role,
                "content": message.content,
                "tool_name": message.tool_name,
                "tool_calls": summarize_tool_calls(message.tool_calls) if args.with_tools else None,
                "reasoning": message.reasoning if args.with_reasoning else None,
                "outcome": envelope.outcome if envelope else None,
                "agent_duration_ms": envelope.agent_duration_ms if envelope else None,
                "sender": _sender_label(envelope, ctx.raw).strip() if envelope else None,
            }
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


_EXPORT_WRITERS: dict[str, Callable[[RoomExport, argparse.Namespace, Context, Path], Path]] = {
    "md": _write_markdown,
    "jsonl": _write_jsonl,
}


def cmd_export(args: argparse.Namespace, ctx: Context) -> int:
    """把一個房間匯出成可以直接 `@file` 丟進 Claude Code 的檔案。"""
    data = _load_export(args, ctx)
    if data is None:
        return 1
    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    print(_EXPORT_WRITERS[args.format](data, args, ctx, out_dir))
    return 0


def _stats_rows(ctx: Context, since: float | None) -> tuple[list[TurnEnvelope], set[str]]:
    """收齊所有房間的 envelope，順便標出「被擋掉之後又有回覆」的房間。"""
    selected: list[TurnEnvelope] = []
    recovered: set[str] = set()
    for room_id in sorted({*list_room_ids(ctx.data_dir), *_envelope_room_ids(ctx)}):
        index = read_envelopes(ctx.conversations_dir, room_id)
        blocked_at: float | None = None
        for envelope in sorted(index.envelopes, key=lambda item: parse_iso(item.ts)):
            ts = parse_iso(envelope.ts)
            if since is not None and ts < since:
                continue
            selected.append(envelope)
            if envelope.outcome == "blocked":
                blocked_at = ts
            elif envelope.outcome == "replied" and blocked_at is not None:
                recovered.add(room_id)
    return selected, recovered


def _envelope_room_ids(ctx: Context) -> list[str]:
    """從 _conversations/ 的檔名列出房間（有些房間的容器可能還沒建過）。"""
    if not ctx.conversations_dir.is_dir():
        return []
    return [path.stem for path in ctx.conversations_dir.glob("*.jsonl")]


def cmd_stats(args: argparse.Namespace, ctx: Context) -> int:
    """outcome 分布、agent_failed 率、p50/p95 耗時、blocked 後回流房間數。"""
    since = _since(args)
    envelopes, recovered = _stats_rows(ctx, since)
    if not envelopes:
        print(f"no turn envelopes under {ctx.conversations_dir}")
        return 0

    counts: dict[str, int] = {}
    for envelope in envelopes:
        counts[envelope.outcome] = counts.get(envelope.outcome, 0) + 1
    total = len(envelopes)
    print(f"turns: {total}   window: {args.since or 'all'}")
    for outcome, count in sorted(counts.items(), key=lambda item: -item[1]):
        print(f"  {outcome:<13} {count:>6}  {count / total:6.1%}")

    failed = counts.get("agent_failed", 0)
    print(f"\nagent_failed rate: {failed / total:.2%}")
    undelivered = sum(1 for envelope in envelopes if envelope.delivered is False)
    print(f"undelivered replies: {undelivered}")

    durations = [e.agent_duration_ms for e in envelopes if e.agent_duration_ms is not None]
    p50 = percentile(durations, 0.50)
    p95 = percentile(durations, 0.95)
    print(
        "agent latency: "
        + (
            f"p50={p50:.0f}ms p95={p95:.0f}ms (n={len(durations)})"
            if p50 is not None and p95 is not None
            else "no samples"
        )
    )
    print(f"rooms that replied after a blocked turn: {len(recovered)}")
    return 0


_COMMANDS: dict[str, Callable[[argparse.Namespace, Context], int]] = {
    "rooms": cmd_rooms,
    "show": cmd_show,
    "search": cmd_search,
    "export": cmd_export,
    "stats": cmd_stats,
}


def _add_render_flags(parser: argparse.ArgumentParser) -> None:
    """show／export 共用的輸出開關（預設都不含工具與 reasoning，見模組說明）。"""
    parser.add_argument("--with-tools", action="store_true", help="也輸出 tool call 與工具回傳")
    parser.add_argument("--with-reasoning", action="store_true", help="也輸出模型的 reasoning")


def build_parser() -> argparse.ArgumentParser:
    """組出 argparse 的五個子命令。"""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--data-dir", help="覆寫 DATA_DIR（預設讀 .env）")
    parser.add_argument("--raw", action="store_true", help="群組發言者 id 輸出原值，不做 hash")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("rooms", help="列出所有房間與其用量")

    show = sub.add_parser("show", help="印出一個房間的逐字稿")
    show.add_argument("room_id")
    show.add_argument("--session", help="只看這個 session id")
    show.add_argument("--since", help="只看這個時間之後（7d／36h／2026-09-01）")
    _add_render_flags(show)

    search = sub.add_parser("search", help="跨房間全文搜尋")
    search.add_argument("term")
    search.add_argument("--room", help="只搜這個房間")
    search.add_argument("--limit", type=int, default=DEFAULT_SEARCH_LIMIT)

    export = sub.add_parser("export", help="匯出成 md／jsonl 給 Claude Code 讀")
    export.add_argument("--room", required=True)
    export.add_argument("--since", help="只匯出這個時間之後（7d／36h／2026-09-01）")
    export.add_argument("--format", choices=("md", "jsonl"), default="md")
    export.add_argument("--out", default=".", help="輸出目錄（預設當前目錄）")
    _add_render_flags(export)

    stats = sub.add_parser("stats", help="outcome 分布與耗時統計")
    stats.add_argument("--since", help="只算這個時間之後（30d／2026-09-01）")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 進入點。"""
    args = build_parser().parse_args(argv)
    data_dir = resolve_data_dir(args.data_dir)
    if not data_dir.is_dir():
        warn(f"DATA_DIR {data_dir} does not exist")
        return 1
    ctx = Context(data_dir=data_dir, conversations_dir=data_dir / "_conversations", raw=args.raw)
    return _COMMANDS[args.command](args, ctx)


if __name__ == "__main__":
    raise SystemExit(main())
