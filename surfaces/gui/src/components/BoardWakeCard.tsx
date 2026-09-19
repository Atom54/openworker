// A team update: the digest that woke a lead (the board's events since its last turn),
// shown in the transcript. Collapsed by default — it is a report, not a message to read
// (owner ask 2026-08-16).
//
// Redesigned 2026-09-17 (owner: "I had a hard time understanding what's there"):
//  · named "Team update", not the internal "board wake";
//  · the summary says what needs ACTION, in words ("checks is waiting on a decision ·
//    #4 is ready for review · 3 more"), not a count per event type;
//  · expanded, events are grouped by work item with a status label, each line a plain
//    sentence with a two-line preview of its note (no click per row);
//  · `waiting` rows — a worker waiting on the lead's decision — are first-class (they
//    used to render as a bare title);
//  · neutral border; actor ids lose their ":session" suffix.
import { useState } from "react";
import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";
import type { BoardWakeRow, MessageSource } from "../api";
import { Icon } from "./Icon";

// A note past this length (or with a line break) clamps to two lines with More/Less.
const NOTE_CLAMP_CHARS = 170;

const actorName = (actor: string | undefined, t: TFunction) =>
  (actor || t("teamupdate.someone")).split(":")[0];

// "<who> is waiting on a decision to <do what>" — one whole sentence per kind of call.
function toolPhrase(who: string, tool: string | undefined, t: TFunction): string {
  if (tool === "run_shell") return t("teamupdate.waiting_run_command", { who });
  if (tool && /^(write_file|replace_in_file|apply_patch|apply_unified_diff)$/.test(tool))
    return t("teamupdate.waiting_change_file", { who });
  return tool ? t("teamupdate.waiting_use_tool", { who, tool }) : t("teamupdate.waiting_act", { who });
}

// What a row says, as a sentence about its item.
function rowSentence(row: BoardWakeRow, t: TFunction): string {
  const who = actorName(row.actor, t);
  switch (row.kind) {
    case "moved":
      if (row.to === "review") return t("teamupdate.moved_review", { who });
      if (row.to === "blocked") return t("teamupdate.moved_blocked", { who });
      if (row.to === "done") return t("teamupdate.moved_done", { who });
      if (row.to === "in_progress") return t("teamupdate.moved_in_progress", { who });
      return t("teamupdate.moved_to", { who, state: String(row.to || "").replace(/_/g, " ") });
    case "filed":
      return t("teamupdate.filed", { who });
    case "claimed":
      return t("teamupdate.claimed", { who });
    case "assigned":
      return t("teamupdate.assigned");
    case "comment":
      return t("teamupdate.commented", { who });
    case "waiting":
      return toolPhrase(who, row.tool, t);
    case "chat":
      return who;
    default:
      return `${who}: ${row.kind}`;
  }
}

// The engine's boilerplate first line says nothing; the rest is the call itself.
const cleanNote = (row: BoardWakeRow) =>
  (row.note || "").replace(/^requires approval\n?/, "").trim();

// Status of an item as far as this update tells: the most pressing thing wins.
// `label` is an i18n KEY, resolved where it renders ("" = no label).
type Status = { rank: number; label: string; dot: string };
const STATUS: Record<string, Status> = {
  waiting: { rank: 0, label: "teamupdate.status_waiting", dot: "board-dot blocked" },
  blocked: { rank: 1, label: "board.state_blocked", dot: "board-dot blocked" },
  review: { rank: 2, label: "teamupdate.status_review", dot: "board-dot review" },
  in_progress: { rank: 3, label: "board.state_in_progress", dot: "board-dot work" },
  claimed: { rank: 3, label: "teamupdate.status_claimed", dot: "board-dot work" },
  assigned: { rank: 3, label: "teamupdate.status_assigned", dot: "board-dot work" },
  done: { rank: 4, label: "board.state_done", dot: "board-dot done" },
  filed: { rank: 5, label: "teamupdate.status_new", dot: "board-dot idle" },
  other: { rank: 6, label: "", dot: "board-dot idle" },
};

function statusOf(row: BoardWakeRow): Status {
  if (row.kind === "waiting") return STATUS.waiting;
  if (row.kind === "moved") return STATUS[String(row.to)] ?? STATUS.other;
  return STATUS[row.kind] ?? STATUS.other;
}

interface Group {
  key: string;
  item?: number | null;
  title: string;
  status: Status;
  rows: BoardWakeRow[];
}

function groupRows(rows: BoardWakeRow[]): { items: Group[]; chat: BoardWakeRow[] } {
  const items: Group[] = [];
  const chat: BoardWakeRow[] = [];
  for (const row of rows) {
    if (row.kind === "chat") {
      chat.push(row);
      continue;
    }
    const key = row.item != null ? `#${row.item}` : `?${row.title || ""}`;
    let g = items.find((x) => x.key === key);
    if (!g) {
      g = { key, item: row.item, title: row.title || "", status: STATUS.other, rows: [] };
      items.push(g);
    }
    g.rows.push(row);
    if (!g.title && row.title) g.title = row.title;
    const s = statusOf(row);
    if (s.rank < g.status.rank) g.status = s;
  }
  // Inside an item too: the line that needs action leads, the rest keep their order.
  for (const g of items) {
    const at = new Map(g.rows.map((r, i) => [r, i]));
    g.rows.sort((a, b) => Math.min(statusOf(a).rank, 3) - Math.min(statusOf(b).rank, 3) || at.get(a)! - at.get(b)!);
  }
  // What needs action first; otherwise the order things happened in.
  const order = new Map(items.map((g, i) => [g, i]));
  items.sort((a, b) => a.status.rank - b.status.rank || order.get(a)! - order.get(b)!);
  return { items, chat };
}

// The collapsed line: what needs action, in words; everything else as a count.
function summarize(rows: BoardWakeRow[], t: TFunction): { phrases: string[]; attention: boolean } {
  const waiting = rows.filter((r) => r.kind === "waiting");
  const blocked = rows.filter((r) => r.kind === "moved" && r.to === "blocked");
  const review = rows.filter((r) => r.kind === "moved" && r.to === "review");
  const phrases: string[] = [];
  if (waiting.length === 1) phrases.push(t("teamupdate.sum_waiting_who", { who: actorName(waiting[0].actor, t) }));
  else if (waiting.length > 1) phrases.push(t("teamupdate.sum_waiting_workers", { n: waiting.length }));
  const items = (rs: BoardWakeRow[]) => [...new Set(rs.map((r) => r.item).filter((i) => i != null))];
  const b = items(blocked);
  if (b.length === 1) phrases.push(t("teamupdate.sum_blocked_item", { item: b[0] }));
  else if (b.length > 1) phrases.push(t("teamupdate.sum_blocked_items", { n: b.length }));
  const v = items(review);
  if (v.length === 1) phrases.push(t("teamupdate.sum_review_item", { item: v[0] }));
  else if (v.length > 1) phrases.push(t("teamupdate.sum_review_items", { n: v.length }));
  const attention = phrases.length > 0;
  const rest = rows.length - waiting.length - blocked.length - review.length;
  if (rest > 0)
    phrases.push(attention ? t("teamupdate.sum_more", { n: rest }) : t("teamupdate.sum_updates", { count: rest }));
  return { phrases: phrases.length ? phrases : [t("teamupdate.sum_fallback")], attention };
}

function Note({ text }: { text: string }) {
  const { t } = useTranslation();
  const [all, setAll] = useState(false);
  if (!text) return null;
  const long = text.length > NOTE_CLAMP_CHARS || text.includes("\n");
  return (
    <span className="boardwake-note-wrap">
      <span className={"boardwake-note" + (long && !all ? " clamped" : "")}>{text}</span>
      {long && (
        <button className="boardwake-note-toggle" onClick={() => setAll((v) => !v)}>
          {all ? t("teamupdate.less") : t("teamupdate.more")}
        </button>
      )}
    </span>
  );
}

export function BoardWakeCard({ source }: { source: MessageSource }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const rows = source.board?.rows || [];
  const { phrases, attention } = summarize(rows, t);
  const { items, chat } = groupRows(rows);
  return (
    <div className="boardwake" data-testid="boardwake-card">
      <button
        className="boardwake-head"
        data-testid="boardwake-toggle"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <Icon name="table" size={14} />
        <span className="boardwake-title">{t("teamupdate.title")}</span>
        <span className="boardwake-summary" data-testid="boardwake-summary">
          {attention && <span className="board-dot review boardwake-attn" />}
          {phrases.join(" · ")}
        </span>
        <span className="spacer" />
        <span className={"boardwake-chevron" + (open ? " open" : "")}>
          <Icon name="chevronDown" size={13} />
        </span>
      </button>
      {open && (
        <div className="boardwake-body" data-testid="boardwake-body">
          {items.map((g) => (
            <div className="boardwake-group" key={g.key} data-testid={`boardwake-group-${g.item ?? "x"}`}>
              <div className="boardwake-group-head">
                <span className={g.status.dot} />
                <span className="boardwake-group-title">
                  {g.item != null && <b>#{g.item} </b>}
                  {g.title}
                </span>
                {g.status.label && <span className="boardwake-status">{t(g.status.label)}</span>}
              </div>
              {g.rows.map((row, i) => (
                <div className="boardwake-line" key={i}>
                  <span className="boardwake-line-text">{rowSentence(row, t)}</span>
                  <Note text={cleanNote(row)} />
                </div>
              ))}
            </div>
          ))}
          {chat.length > 0 && (
            <div className="boardwake-group" data-testid="boardwake-group-chat">
              <div className="boardwake-group-head">
                <span className="board-dot idle" />
                <span className="boardwake-group-title">
                  <b>{t("teamupdate.team_chat")}</b>
                </span>
              </div>
              {chat.map((row, i) => (
                <div className="boardwake-line" key={i}>
                  <span className="boardwake-line-text">{rowSentence(row, t)}</span>
                  <Note text={cleanNote(row)} />
                </div>
              ))}
            </div>
          )}
          {rows.length === 0 && <div className="boardwake-note">{source.text}</div>}
        </div>
      )}
    </div>
  );
}
