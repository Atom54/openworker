import { describe, it, expect } from "vitest";

import {
  shouldNotify,
  DEFAULT_NOTIFICATION_PREFS,
  type AttentionEvent,
  type NotificationPrefs,
} from "./notifications";

const on: NotificationPrefs = { ...DEFAULT_NOTIFICATION_PREFS, enabled: true };
const away = { focused: false, currentSessionId: "other" };

const ev = (over: Partial<AttentionEvent> = {}): AttentionEvent => ({
  reason: "inbox",
  session_id: "s1",
  title: "Run `write_file`?",
  body: "path: /tmp/x",
  kind: "approval",
  workspace: "/ws",
  agent: "code",
  ...over,
});

describe("shouldNotify", () => {
  it("notifies on a pending prompt when the app is away", () => {
    const got = shouldNotify(ev(), on, away);
    expect(got).not.toBeNull();
    expect(got!.title).toBe("Run `write_file`?");
    expect(got!.target).toEqual({ session_id: "s1", workspace: "/ws", agent: "code" });
  });

  it("stays silent while the master switch is off", () => {
    expect(shouldNotify(ev(), DEFAULT_NOTIFICATION_PREFS, away)).toBeNull();
  });

  it("honours each category switch independently", () => {
    expect(shouldNotify(ev(), { ...on, attention: false }, away)).toBeNull();
    expect(
      shouldNotify(ev({ reason: "turn_done" }), { ...on, turn_done: false }, away),
    ).toBeNull();
    expect(
      shouldNotify(
        ev({ reason: "task_done", status: "ok" }),
        { ...on, automation_done: false },
        away,
      ),
    ).toBeNull();
    expect(
      shouldNotify(
        ev({ reason: "task_done", status: "error" }),
        { ...on, errors: false },
        away,
      ),
    ).toBeNull();
  });

  it("separates failed runs from successful ones", () => {
    // Silencing routine completions must not silence breakage.
    const quietSuccess = { ...on, automation_done: false };
    expect(
      shouldNotify(ev({ reason: "task_done", status: "ok" }), quietSuccess, away),
    ).toBeNull();
    const failed = shouldNotify(
      ev({ reason: "task_done", status: "error", title: "Daily brief" }),
      quietSuccess,
      away,
    );
    expect(failed).not.toBeNull();
    expect(failed!.title).toBe("✗ Daily brief");
  });

  it("marks a successful run with a check", () => {
    const got = shouldNotify(
      ev({ reason: "task_done", status: "ok", title: "Daily brief" }),
      on,
      away,
    );
    expect(got!.title).toBe("✓ Daily brief");
  });

  it("stays silent when the user is already looking at that session", () => {
    expect(
      shouldNotify(ev(), on, { focused: true, currentSessionId: "s1" }),
    ).toBeNull();
  });

  it("still notifies for another session while focused", () => {
    // The case the focus rule must not swallow: an ask parked on a session the user
    // isn't watching would otherwise be invisible.
    expect(
      shouldNotify(ev(), on, { focused: true, currentSessionId: "s2" }),
    ).not.toBeNull();
  });

  it("notifies for the same session when the window is in the background", () => {
    expect(
      shouldNotify(ev(), on, { focused: false, currentSessionId: "s1" }),
    ).not.toBeNull();
  });

  it("falls back to a usable title and empty body", () => {
    const got = shouldNotify(ev({ title: "", body: "" }), on, away);
    expect(got!.title).toBe("OpenWorker");
    expect(got!.body).toBe("");
  });
});
