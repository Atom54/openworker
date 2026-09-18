// The team update card (the digest that woke a lead). Owner 2026-09-17: "I had a hard
// time understanding what's there" — so: action first and in words, grouped by work item,
// note previews in place, and `waiting` rows are first-class. Payloads: gallery states.
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { MessageSource } from "../api";
import { statePayload } from "../gallery/states";
import { BoardWakeCard } from "./BoardWakeCard";

const source = (id: string) => statePayload("board-wake", id).source as MessageSource;

describe("BoardWakeCard", () => {
  afterEach(cleanup);

  it("summarises what needs action in words, the rest as a count", () => {
    render(<BoardWakeCard source={source("waiting-and-review")} />);
    expect(screen.getByTestId("boardwake-card").textContent).toContain("Team update");
    expect(screen.getByTestId("boardwake-summary").textContent).toBe(
      "checks is waiting on a decision · #4 is ready for review · 3 more",
    );
    expect(screen.queryByTestId("boardwake-body")).toBeNull(); // collapsed by default
  });

  it("says plain 'N updates' when nothing needs action", () => {
    render(<BoardWakeCard source={source("routine")} />);
    expect(screen.getByTestId("boardwake-summary").textContent).toBe("3 updates");
  });

  it("groups by work item, the pressing item and line first, ids stripped from actors", () => {
    render(<BoardWakeCard source={source("waiting-and-review")} />);
    fireEvent.click(screen.getByTestId("boardwake-toggle"));
    const groups = [...screen.getByTestId("boardwake-body").children].map((g) => g.getAttribute("data-testid"));
    expect(groups).toEqual(["boardwake-group-5", "boardwake-group-4", "boardwake-group-chat"]);
    const five = screen.getByTestId("boardwake-group-5");
    expect(five.textContent).toContain("Waiting on a decision");
    const lines = [...five.querySelectorAll(".boardwake-line-text")].map((l) => l.textContent);
    expect(lines).toEqual(["checks is waiting on a decision to run a command", "nia commented"]);
    // the engine's filler line is dropped from the waiting call's preview
    expect(five.textContent).not.toContain("requires approval");
    expect(five.textContent).toContain("cp src/invoices.ts");
    const four = screen.getByTestId("boardwake-group-4");
    expect(four.textContent).toContain("swe-lead commented");
    expect(four.textContent).not.toContain("3fbd018c");
    expect(four.textContent).toContain("nia handed it off for review");
  });

  it("previews notes in place and expands a long one with More", () => {
    render(<BoardWakeCard source={source("waiting-and-review")} />);
    fireEvent.click(screen.getByTestId("boardwake-toggle"));
    const four = screen.getByTestId("boardwake-group-4");
    const note = [...four.querySelectorAll(".boardwake-note")].find((n) => n.textContent?.includes("Done on branch"))!;
    expect(note.className).toContain("clamped");
    fireEvent.click(note.parentElement!.querySelector("button")!);
    expect(note.className).not.toContain("clamped");
  });

  it("still renders an update without structured rows (old server)", () => {
    render(<BoardWakeCard source={source("minimal")} />);
    fireEvent.click(screen.getByTestId("boardwake-toggle"));
    expect(screen.getByTestId("boardwake-body").textContent).toContain("Board wake");
  });
});
