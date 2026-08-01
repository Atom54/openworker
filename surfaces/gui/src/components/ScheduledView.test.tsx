// Per-automation run settings: the model picker defaults to "App default", and the
// thinking level is only offered on the native OpenAI wire (every other provider spells
// thinking differently and rejects the parameter — see SessionManager._task_model_settings).
import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

vi.mock("../api", () => ({
  createAutomation: vi.fn(async (_payload: Record<string, unknown>) => ({
    ok: true,
    task: undefined,
  })),
  deleteAutomation: vi.fn(async () => ({ ok: true })),
  getAutomation: vi.fn(async () => ({ task: null, runs: [] })),
  getAutomations: vi.fn(async () => []),
  getSettings: vi.fn(async () => ({
    models: ["gpt-5.6-sol", "azure:gpt-5.6-terra", "anthropic:claude-fable-5"],
    model: "gpt-5.6-sol",
    model_labels: {
      "gpt-5.6-sol": "GPT-5.6 Sol · OpenAI",
      "azure:gpt-5.6-terra": "GPT-5.6 Terra · Azure",
      "anthropic:claude-fable-5": "Claude Fable 5 · Anthropic",
    },
  })),
  markAutomationSeen: vi.fn(async () => ({ ok: true })),
  announceAutomationsChanged: vi.fn(),
  updateAutomation: vi.fn(async () => ({ ok: true })),
  // The quickstart renders alongside the form on an empty list.
  getConnectors: vi.fn(async () => []),
  getCloudStatus: vi.fn(async () => ({ signed_in: false })),
  getRecentChannels: vi.fn(async () => []),
  cloudLogin: vi.fn(async () => ({ ok: true })),
  connectManaged: vi.fn(async () => ({ ok: true })),
  waitForCloudSignIn: vi.fn(async () => ({ ok: true })),
}));

import { createAutomation } from "../api";
import { ScheduledView } from "./ScheduledView";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

async function openForm() {
  render(<ScheduledView onOpenRun={() => {}} onRunNow={() => {}} />);
  // The empty state offers its own copy of the button; either opens the same form.
  fireEvent.click(screen.getAllByText("+ New automation")[0]);
  // the selects only render once getSettings resolves
  return await screen.findByTestId("automation-model");
}

it("offers the curated models and defaults to the app default", async () => {
  const select = (await openForm()) as HTMLSelectElement;
  expect(select.value).toBe(""); // "App default (GPT-5.6 Sol · OpenAI)"
  const labels = [...select.options].map((o) => o.textContent);
  expect(labels[0]).toContain("App default");
  expect(labels).toContain("GPT-5.6 Terra · Azure");
});

it("enables the thinking level for the Responses wires and disables it for the others", async () => {
  const model = (await openForm()) as HTMLSelectElement;
  const thinking = screen.getByTestId("automation-thinking") as HTMLSelectElement;

  // app default is a bare OpenAI id → the level is settable
  expect(thinking.disabled).toBe(false);
  fireEvent.change(thinking, { target: { value: "high" } });
  expect(thinking.value).toBe("high");

  // Azure serves /responses too — an all-Azure install must not find this permanently
  // greyed out (owner-hit 2026-07-28).
  fireEvent.change(model, { target: { value: "azure:gpt-5.6-terra" } });
  await waitFor(() => expect(thinking.disabled).toBe(false));

  // a prefixed id routes to another wire → the control locks and shows nothing set
  fireEvent.change(model, { target: { value: "anthropic:claude-fable-5" } });
  await waitFor(() => expect(thinking.disabled).toBe(true));
  expect(thinking.value).toBe("");
  expect(thinking.title).toContain("Claude Fable 5");
});

it("submits the chosen model and level", async () => {
  const model = (await openForm()) as HTMLSelectElement;
  fireEvent.change(screen.getByPlaceholderText(/^Title/), {
    target: { value: "Nightly digest" },
  });
  fireEvent.change(screen.getByPlaceholderText(/What should it do/), {
    target: { value: "Summarize the day." },
  });
  fireEvent.change(model, { target: { value: "gpt-5.6-sol" } });
  fireEvent.change(screen.getByTestId("automation-thinking"), {
    target: { value: "low" },
  });
  fireEvent.click(screen.getByText("Create automation"));

  await waitFor(() => expect(createAutomation).toHaveBeenCalled());
  expect(vi.mocked(createAutomation).mock.calls[0][0]).toMatchObject({
    title: "Nightly digest",
    model: "gpt-5.6-sol",
    thinking: "low",
  });
});
