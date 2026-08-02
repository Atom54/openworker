import { useEffect, useState } from "react";

// The recurrence picker behind every automation schedule (the create form AND the detail
// editor — one component so the two can't drift). Everything it emits is a plain 5-field
// cron: croniter in the server is the only scheduler, so the picker's whole job is to cover
// the shapes people actually ask for and to READ THEM BACK, so opening the editor on a
// "second Tuesday" automation never silently downgrades it to "every day".

export type Mode = "daily" | "weekly" | "monthly" | "nth" | "custom";

export interface Recurrence {
  mode: Mode;
  time: string; // "HH:MM" — every mode except custom
  days: string[]; // cron day-of-week numbers, weekly mode (always ≥1)
  dom: string; // "1".."31" | "L" (last day of the month), monthly mode
  nth: string; // "1".."4" — the nth <weekday>, nth mode
  nthDay: string; // cron day-of-week number, nth mode
  raw: string; // the cron typed in custom mode
}

// Cron numbers the days 0 = Sunday … 6 = Saturday; a week reads Monday-first.
export const WEEK = [
  { dow: "1", short: "Mon", label: "Monday" },
  { dow: "2", short: "Tue", label: "Tuesday" },
  { dow: "3", short: "Wed", label: "Wednesday" },
  { dow: "4", short: "Thu", label: "Thursday" },
  { dow: "5", short: "Fri", label: "Friday" },
  { dow: "6", short: "Sat", label: "Saturday" },
  { dow: "0", short: "Sun", label: "Sunday" },
];

const WEEKDAYS = ["1", "2", "3", "4", "5"];
const WEEKEND = ["6", "0"];

const DEFAULT: Recurrence = {
  mode: "daily",
  time: "09:00",
  days: ["1"],
  dom: "1",
  nth: "1",
  nthDay: "1",
  raw: "",
};

/** A cron string the server will accept, shape-wise: 5 whitespace-separated fields.
 *  croniter still has the last word (the PATCH/POST both reject what it can't parse). */
export function looksLikeCron(cron: string): boolean {
  return (cron || "").trim().split(/\s+/).filter(Boolean).length === 5;
}

const ordinal = (n: string) => {
  const i = parseInt(n, 10);
  const suffix = i % 100 >= 11 && i % 100 <= 13 ? "th" : ["th", "st", "nd", "rd"][i % 10] || "th";
  return `${i}${suffix}`;
};

const pad = (s: string) => String(Math.max(0, parseInt(s, 10) || 0)).padStart(2, "0");

/** Expand a cron day-of-week field into its individual numbers: "1-5" → 1,2,3,4,5 and
 *  "1,4" → 1,4. Returns null for anything richer (steps, names) — that's custom's job. */
function expandDow(field: string): string[] | null {
  const out: string[] = [];
  for (const token of field.split(",")) {
    const one = /^([0-7])$/.exec(token);
    const range = /^([0-7])-([0-7])$/.exec(token);
    if (one) {
      out.push(String(Number(one[1]) % 7));
    } else if (range) {
      const [a, b] = [Number(range[1]) % 7, Number(range[2]) % 7];
      if (a > b) return null; // cron allows wrap-around; the checkbox row can't say it
      for (let d = a; d <= b; d++) out.push(String(d));
    } else {
      return null;
    }
  }
  return out.length ? Array.from(new Set(out)) : null;
}

/** Read a cron back into the picker's state. Anything the structured modes can't express
 *  lands in `custom` with the cron intact — an agent-written schedule stays editable and,
 *  crucially, survives a save that only meant to change the title. */
export function fromCron(cron?: string | null): Recurrence {
  const raw = (cron || "").trim();
  const base = { ...DEFAULT, raw };
  if (!raw) return base;
  const parts = raw.split(/\s+/);
  const custom = { ...base, mode: "custom" as Mode };
  if (parts.length !== 5) return custom;
  const [m, h, dom, month, dow] = parts;
  // Only a single plain time-of-day maps onto the "At" input; "*/15" and friends are custom.
  if (month !== "*" || !/^\d{1,2}$/.test(m) || !/^\d{1,2}$/.test(h)) return custom;
  if (Number(m) > 59 || Number(h) > 23) return custom;
  const time = `${pad(h)}:${pad(m)}`;

  if (dom === "*" && dow === "*") return { ...base, mode: "daily", time };
  if (dom === "*") {
    const nth = /^([0-7])#([1-4])$/.exec(dow);
    if (nth) {
      return { ...base, mode: "nth", time, nthDay: String(Number(nth[1]) % 7), nth: nth[2] };
    }
    const days = expandDow(dow);
    if (days) return { ...base, mode: "weekly", time, days };
    return custom;
  }
  if (dow === "*" && (dom === "L" || (/^\d{1,2}$/.test(dom) && +dom >= 1 && +dom <= 31))) {
    return { ...base, mode: "monthly", time, dom };
  }
  return custom;
}

/** The picker's state as a 5-field cron. */
export function toCron(r: Recurrence): string {
  if (r.mode === "custom") return r.raw.trim();
  const [h, m] = (r.time || "09:00").split(":");
  const at = `${Number(m) || 0} ${Number(h) || 0}`;
  switch (r.mode) {
    case "weekly": {
      // Emitted in week order so the cron reads the way the row does.
      const days = WEEK.filter((d) => r.days.includes(d.dow)).map((d) => d.dow);
      return `${at} * * ${days.length ? days.join(",") : "*"}`;
    }
    case "monthly":
      return `${at} ${r.dom || "1"} * *`;
    case "nth":
      return `${at} * * ${r.nthDay}#${r.nth}`;
    default:
      return `${at} * * *`;
  }
}

/** Time-of-day + recurrence fields. Owns its own state (seeded from `initial` at mount) and
 *  reports the assembled cron up; the parent stores nothing but that string. */
export function ScheduleFields({
  initial,
  onCron,
}: {
  initial?: string | null;
  onCron: (cron: string) => void;
}) {
  const [r, setR] = useState<Recurrence>(() => fromCron(initial));
  const set = (patch: Partial<Recurrence>) => setR((cur) => ({ ...cur, ...patch }));

  // Report on mount too, so the parent starts holding a real cron rather than a default
  // it had to guess. Same-string updates are a no-op for React.
  useEffect(() => {
    onCron(toCron(r));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [r]);

  const toggleDay = (dow: string) =>
    set({
      days: r.days.includes(dow)
        ? // Never leave the row empty: an empty day list would mean "every day", which is
          // not what unticking your last day asks for.
          r.days.length > 1
          ? r.days.filter((d) => d !== dow)
          : r.days
        : [...r.days, dow],
    });

  return (
    <>
      <div className="tmpl-sched">
        {r.mode !== "custom" && (
          <label className="tmpl-field">
            <span>At</span>
            <input
              type="time"
              className="tmpl-input tmpl-time"
              value={r.time}
              data-testid="automation-time"
              onChange={(e) => set({ time: e.target.value })}
            />
          </label>
        )}
        <label className="tmpl-field">
          <span>Repeat</span>
          <select
            className="tmpl-input tmpl-select"
            value={r.mode}
            data-testid="automation-repeat"
            onChange={(e) => set({ mode: e.target.value as Mode })}
          >
            <option value="daily">Every day</option>
            <option value="weekly">Days of the week…</option>
            <option value="monthly">Day of the month…</option>
            <option value="nth">Nth weekday of the month…</option>
            <option value="custom">Custom cron…</option>
          </select>
        </label>
        {r.mode === "monthly" && (
          <label className="tmpl-field">
            <span>On the</span>
            <select
              className="tmpl-input tmpl-select"
              value={r.dom}
              data-testid="automation-dom"
              onChange={(e) => set({ dom: e.target.value })}
            >
              {Array.from({ length: 31 }, (_, i) => String(i + 1)).map((d) => (
                <option key={d} value={d}>
                  {ordinal(d)}
                </option>
              ))}
              <option value="L">Last day</option>
            </select>
          </label>
        )}
        {r.mode === "nth" && (
          <>
            <label className="tmpl-field">
              <span>On the</span>
              <select
                className="tmpl-input tmpl-select"
                value={r.nth}
                data-testid="automation-nth"
                onChange={(e) => set({ nth: e.target.value })}
              >
                {["1", "2", "3", "4"].map((n) => (
                  <option key={n} value={n}>
                    {ordinal(n)}
                  </option>
                ))}
              </select>
            </label>
            <label className="tmpl-field">
              <span>Weekday</span>
              <select
                className="tmpl-input tmpl-select"
                value={r.nthDay}
                data-testid="automation-nth-day"
                onChange={(e) => set({ nthDay: e.target.value })}
              >
                {WEEK.map((d) => (
                  <option key={d.dow} value={d.dow}>
                    {d.label}
                  </option>
                ))}
              </select>
            </label>
          </>
        )}
      </div>

      {r.mode === "weekly" && (
        <div className="rec-days" data-testid="automation-days">
          {WEEK.map((d) => (
            <button
              key={d.dow}
              type="button"
              className={"rec-day" + (r.days.includes(d.dow) ? " on" : "")}
              aria-pressed={r.days.includes(d.dow)}
              onClick={() => toggleDay(d.dow)}
            >
              {d.short}
            </button>
          ))}
          <button type="button" className="link rec-preset" onClick={() => set({ days: WEEKDAYS })}>
            Weekdays
          </button>
          <button type="button" className="link rec-preset" onClick={() => set({ days: WEEKEND })}>
            Weekend
          </button>
        </div>
      )}

      {r.mode === "custom" && (
        <label className="tmpl-field">
          <span>Cron — minute hour day-of-month month day-of-week</span>
          <input
            className="tmpl-input"
            value={r.raw}
            placeholder="0 9 * * 1,4"
            spellCheck={false}
            data-testid="automation-cron"
            onChange={(e) => set({ raw: e.target.value })}
          />
          <span className={looksLikeCron(r.raw) ? "dim" : "rec-bad"}>
            {looksLikeCron(r.raw)
              ? "Ranges (1-5), lists (1,4), steps (*/15) and L / #n all work."
              : "Needs 5 space-separated fields."}
          </span>
        </label>
      )}
    </>
  );
}
