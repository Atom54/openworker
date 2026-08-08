// Desktop system notifications for anything wanting the user's attention.
//
// The server emits one `attention` event on /ws/events for all of it, discriminated by
// `reason`; this file decides whether it deserves a notification and hands it to the shell.
// The decision is a pure function so the rules are testable without a webview.

import { sendNotification, type NotificationTarget } from "./tauri";

export type AttentionReason = "inbox" | "task_done" | "turn_done";

export type AttentionEvent = {
  reason: AttentionReason;
  session_id: string;
  title: string;
  body: string;
  kind?: string;
  status?: "ok" | "error";
  task_id?: string;
  run_id?: string;
  workspace?: string;
  agent?: string;
};

/** Server-side prefs (Settings ▸ Notifications), mirrored from getSettings(). */
export type NotificationPrefs = {
  enabled: boolean;
  automation_done: boolean;
  turn_done: boolean;
  attention: boolean;
  errors: boolean;
};

export const DEFAULT_NOTIFICATION_PREFS: NotificationPrefs = {
  enabled: false,
  automation_done: true,
  turn_done: true,
  attention: true,
  errors: true,
};

/** What the app is showing right now — the other half of the focus rule. */
export type NotificationContext = {
  focused: boolean;
  currentSessionId: string;
};

/** Which switch governs this event. A failed run is its own category: people who silence
 * routine "done" notifications still want to hear that something broke. */
function prefKeyFor(ev: AttentionEvent): keyof NotificationPrefs {
  if (ev.reason === "inbox") return "attention";
  if (ev.reason === "turn_done") return "turn_done";
  return ev.status === "error" ? "errors" : "automation_done";
}

/**
 * The notification for this event, or null to stay quiet.
 *
 * Order matters: the master switch wins over everything, then the per-category switch,
 * then the focus rule. Focus only suppresses when the user is looking at THIS session —
 * an ask on another session is exactly what they'd otherwise miss.
 */
export function shouldNotify(
  ev: AttentionEvent,
  prefs: NotificationPrefs,
  ctx: NotificationContext,
): { title: string; body: string; target: NotificationTarget } | null {
  if (!prefs.enabled) return null;
  if (!prefs[prefKeyFor(ev)]) return null;
  if (ctx.focused && ev.session_id === ctx.currentSessionId) return null;

  const prefix =
    ev.reason === "task_done" ? (ev.status === "error" ? "✗ " : "✓ ") : "";
  return {
    title: `${prefix}${ev.title || "OpenWorker"}`,
    body: ev.body || "",
    target: {
      session_id: ev.session_id,
      workspace: ev.workspace || "",
      agent: ev.agent || "",
    },
  };
}

/**
 * Handle one frame off the app-wide event stream. Deliberately NOT its own
 * `connectEvents` subscription: that opened a second /ws/events socket alongside the
 * automation toast's, and only one of the two would receive a given push. The caller
 * feeds this from the app's single existing stream.
 */
export function handleAttentionEvent(
  msg: { type: string; data?: Record<string, unknown> },
  state: { prefs: NotificationPrefs; ctx: NotificationContext },
): void {
  if (msg.type !== "attention") return;
  const decided = shouldNotify(
    msg.data as unknown as AttentionEvent,
    state.prefs,
    state.ctx,
  );
  if (decided) sendNotification(decided.title, decided.body, decided.target);
}
