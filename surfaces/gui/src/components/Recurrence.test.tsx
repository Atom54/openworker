// The recurrence picker's contract: every cron it can emit, it must read back. A shape that
// round-trips wrong doesn't fail loudly — it silently rewrites the user's schedule the next
// time they edit the title, which is the bug this file exists to catch.
import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { ScheduleFields, fromCron, looksLikeCron, toCron } from "./Recurrence";

afterEach(cleanup);

it("round-trips every shape the picker can build", () => {
  for (const cron of [
    "0 9 * * *", // daily
    "30 8 * * 5", // one weekday
    "0 9 * * 1,3,5", // several
    "0 17 * * 2,4",
    "0 9 * * 1,2,3,4,5", // weekdays preset
    "0 9 * * 6,0", // weekend preset, in week order
    "15 7 12 * *", // day of the month
    "0 9 L * *", // last day
    "0 9 * * 5#2", // 2nd Friday
  ]) {
    expect(toCron(fromCron(cron))).toBe(cron);
  }
});

it("normalizes the equivalent spellings a cron can take", () => {
  // Ranges collapse to the day list the checkbox row shows; both weekend orders agree.
  expect(toCron(fromCron("0 9 * * 1-5"))).toBe("0 9 * * 1,2,3,4,5");
  expect(toCron(fromCron("0 9 * * 0,6"))).toBe("0 9 * * 6,0");
  expect(fromCron("0 9 * * 7").days).toEqual(["0"]); // 7 and 0 are both Sunday
  expect(fromCron("5 9 * * *").time).toBe("09:05");
});

it("keeps anything it can't model as an editable custom cron", () => {
  // The killer case: an agent-written cron must survive an edit untouched.
  for (const cron of ["*/15 * * * *", "0 9 1 1 *", "0 9 * * 5#5", "0 */2 * * *", "@daily"]) {
    const r = fromCron(cron);
    expect(r.mode).toBe("custom");
    expect(toCron(r)).toBe(cron);
  }
  expect(fromCron("").mode).toBe("daily"); // a brand-new automation, not "custom of nothing"
});

it("rejects a cron that isn't five fields", () => {
  expect(looksLikeCron("0 9 * *")).toBe(false);
  expect(looksLikeCron("  0   9 * * 1 ")).toBe(true);
});

it("emits a multi-day cron from the day row and never empties it", () => {
  const onCron = vi.fn();
  render(<ScheduleFields initial="0 9 * * 2" onCron={onCron} />);
  const last = () => onCron.mock.calls[onCron.mock.calls.length - 1][0];
  expect(last()).toBe("0 9 * * 2");

  fireEvent.click(screen.getByText("Thu"));
  expect(last()).toBe("0 9 * * 2,4");
  fireEvent.click(screen.getByText("Weekdays"));
  expect(last()).toBe("0 9 * * 1,2,3,4,5");

  // Unticking down to nothing would mean "every day" — the last one standing stays ticked.
  for (const d of ["Mon", "Tue", "Wed", "Thu", "Fri"]) fireEvent.click(screen.getByText(d));
  expect(last()).toBe("0 9 * * 5");
});

it("switches modes and carries the time across", () => {
  const onCron = vi.fn();
  render(<ScheduleFields initial="45 18 * * *" onCron={onCron} />);
  const last = () => onCron.mock.calls[onCron.mock.calls.length - 1][0];

  fireEvent.change(screen.getByTestId("automation-repeat"), { target: { value: "monthly" } });
  fireEvent.change(screen.getByTestId("automation-dom"), { target: { value: "L" } });
  expect(last()).toBe("45 18 L * *");

  fireEvent.change(screen.getByTestId("automation-repeat"), { target: { value: "nth" } });
  fireEvent.change(screen.getByTestId("automation-nth"), { target: { value: "2" } });
  fireEvent.change(screen.getByTestId("automation-nth-day"), { target: { value: "5" } });
  expect(last()).toBe("45 18 * * 5#2");

  // Custom hands the whole field over — including the time, so the "At" input steps aside.
  fireEvent.change(screen.getByTestId("automation-repeat"), { target: { value: "custom" } });
  expect(screen.queryByTestId("automation-time")).toBeNull();
  fireEvent.change(screen.getByTestId("automation-cron"), { target: { value: "*/10 * * * *" } });
  expect(last()).toBe("*/10 * * * *");
});
