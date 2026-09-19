// Board wakes: `turn_start` payloads whose `source.connector` is "board". In a reopened
// session the same `source` sits on the stored message.
import type { CardState } from "../types";

const board = (text: string, rows: Record<string, unknown>[]) => ({
  source: {
    connector: "board",
    kind: "channel",
    channel_id: "/Users/test/OpenWorker/launch-note",
    channel_name: "Team board",
    sender_id: "board",
    sender_name: "Board",
    ts: 1789900000,
    text,
    board: { rows },
  },
});

export const boardWakeStates: CardState[] = [
  {
    id: "waiting-and-review",
    title: "A worker waiting, a hand-off to review (real run)",
    note: "From the SpaceSol issue #3 run, 2026-09-17: the row that matters most is checks waiting on the lead's decision. Grouped by work item; chat on its own.",
    payload: board("⏰ Board wake — your team needs decisions", [
      { item: 5, title: "api: prove PDF 403 for other accounts and 200 for owner/staff with tests", actor: "nia", kind: "comment", note: "@checks PDF authz landed on issue-3 (commit fbc2190). GET /v1/invoices/:id/pdf status contract: 401 no token, 403 other account (assertCanRead), 404 unknown id or no pdf_key, 200 owner and staff. Test against that." },
      { item: 4, title: "api + web: enforce owner/staff authorization on invoice PDF download", actor: "swe-lead:3fbd018c", kind: "comment", note: "Received in review. Holding here until checks completes the independent verification in #5 (API gate + 403/owner-200/staff-200 assertions against the real handler)." },
      { item: 4, title: "api + web: enforce owner/staff authorization on invoice PDF download", actor: "nia", kind: "moved", to: "review", note: "Done on branch issue-3 (commit fbc2190). API: GET /:id/pdf now runs assertCanRead(claimsOf(req), invoice.account_id) after loadInvoice, before the S3 read. Web: the Download PDF button renders only for the invoice's owner or staff. Lint and build green in api/ and web/." },
      { item: 5, title: "api: prove PDF 403 for other accounts and 200 for owner/staff with tests", actor: "checks", kind: "waiting", tool: "run_shell", prompt_id: "c6e33538745f4a2d8689da0a4ba436cf", note: "requires approval\ncommand: cd /home/user/work/billing-service/issue-3/api && cp src/invoices.ts /tmp/… · description: Temporarily remove fix, confirm cross-account test fails, then restore" },
      { kind: "chat", actor: "nia", note: "@checks @lead I don't have decide_worker_call (worker, not lead), but for the record I endorse checks' mutation check — back up src/invoices.ts, remove the assert, see the 403 test go red, restore." },
    ]),
  },
  {
    id: "review-and-filed",
    title: "A hand-off to review",
    note: "Collapsed by default; a review tints the line because it needs a decision.",
    payload: board("⏰ Board wake — your team needs decisions:\n- #2 moved to review by webb", [
      {
        kind: "moved",
        item: 2,
        title: "Statements page",
        actor: "webb",
        to: "review",
        note: "Ready for review on feat/customer-statements, commit 029f9f7. Build verified; final verdict stays with the tester.",
      },
      { kind: "filed", item: 5, title: "Follow-up: rate limit", actor: "nia" },
    ]),
  },
  {
    id: "routine",
    title: "Routine updates",
    note: "Claims and comments only: no tint.",
    payload: board("Board wake — updates", [
      { kind: "claimed", item: 1, title: "Statement API endpoint", actor: "nia" },
      { kind: "claimed", item: 3, title: "Statement totals reconcile", actor: "omar" },
      { kind: "comment", item: 1, title: "Statement API endpoint", actor: "nia", note: "Range parsing is done; starting on the totals." },
    ]),
  },
  {
    id: "blocked-and-chat",
    title: "Blocked item and team chat",
    payload: board("⏰ Board wake — your team needs decisions", [
      { kind: "moved", item: 4, title: "Verification pass", actor: "checks", to: "blocked", note: "The seeded database has no invoices for Northgate in March, so the range test cannot pass. Need a seed change or a different range." },
      { kind: "chat", actor: "checks", note: "@lead the March seed data is missing — who owns db/seed.sql?" },
      { kind: "moved", item: 1, title: "Statement API endpoint", actor: "nia", to: "done" },
      { kind: "assigned", item: 6, title: "Seed data for March" },
    ]),
  },
  {
    id: "many-rows",
    title: "A busy wake (twelve rows)",
    note: "What a lead of a large team sees; the summary line carries the counts.",
    payload: board(
      "⏰ Board wake — your team needs decisions",
      Array.from({ length: 12 }, (_, i) =>
        i % 4 === 0
          ? { kind: "moved", item: i + 1, title: `Work item ${i + 1}`, actor: `worker-${i + 1}`, to: "review", note: "Ready for review; tests green." }
          : i % 4 === 1
            ? { kind: "claimed", item: i + 1, title: `Work item ${i + 1}`, actor: `worker-${i + 1}` }
            : i % 4 === 2
              ? { kind: "moved", item: i + 1, title: `Work item ${i + 1}`, actor: `worker-${i + 1}`, to: "done" }
              : { kind: "comment", item: i + 1, title: `Work item ${i + 1}`, actor: `worker-${i + 1}`, note: "Halfway; no blockers." },
      ),
    ),
  },
  {
    id: "minimal",
    title: "No rows (old server)",
    note: "A wake without structured rows still renders.",
    payload: { source: { connector: "board", kind: "channel", channel_id: "board", channel_name: "Team board", sender_id: "board", sender_name: "Board", ts: 1789900000, text: "Board wake" } },
  },
];
