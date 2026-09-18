// A lead's decision on a worker's waiting call gets its own card: the generic approval
// card asked the human to click "Allow" to DENY a command it never showed (owner catch
// 2026-09-17). Payloads come from the card gallery's state files.
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { approvalItemFromPayload } from "../cardPayloads";
import { inboxItemBuilders } from "../gallery/inboxItems";
import { STATES, statePayload } from "../gallery/states";
import { ApprovalCard } from "./ApprovalCard";
import { InboxItemCard } from "./InboxItemCard";

const state = (id: string) => STATES["worker-decision"].find((s) => s.id === id)!;

describe("WorkerDecisionCard", () => {
  afterEach(cleanup);

  it("shows the worker's command and the lead's reason, never the call id", () => {
    render(<ApprovalCard item={approvalItemFromPayload(statePayload("worker-decision", "lead-denies-command"))} onApprove={vi.fn()} />);
    const card = screen.getByTestId("workerdec-card");
    expect(card.textContent).toContain("The lead wants to deny nia’s command");
    expect(screen.getByTestId("workerdec-call").textContent).toContain("issue-1/web && npm run build");
    expect(screen.getByTestId("workerdec-note").textContent).toContain("already done and merged");
    expect(card.textContent).not.toContain("c51dc4bd");
    expect(card.textContent).not.toContain("requires approval");
  });

  it("following the lead approves the lead's decision and touches nothing else", () => {
    const onApprove = vi.fn();
    const onAnswerWorkerCall = vi.fn();
    render(
      <ApprovalCard
        item={approvalItemFromPayload(statePayload("worker-decision", "lead-denies-command"))}
        onApprove={onApprove}
        onAnswerWorkerCall={onAnswerWorkerCall}
      />,
    );
    expect(screen.getByTestId("workerdec-follow").textContent).toBe("Deny it, as the lead suggests");
    fireEvent.click(screen.getByTestId("workerdec-follow"));
    expect(onApprove).toHaveBeenCalledWith("once");
    expect(onAnswerWorkerCall).not.toHaveBeenCalled();
  });

  it("overriding answers the worker's call with the opposite and declines the lead's", () => {
    const onApprove = vi.fn();
    const onAnswerWorkerCall = vi.fn();
    render(
      <ApprovalCard
        item={approvalItemFromPayload(statePayload("worker-decision", "lead-denies-command"))}
        onApprove={onApprove}
        onAnswerWorkerCall={onAnswerWorkerCall}
      />,
    );
    expect(screen.getByTestId("workerdec-override").textContent).toBe("Allow nia’s command instead");
    fireEvent.click(screen.getByTestId("workerdec-override"));
    expect(onAnswerWorkerCall).toHaveBeenCalledWith("c51dc4bd79f74e87b7fc59050dc41c37", "allow");
    expect(onApprove).toHaveBeenCalledWith("deny");
  });

  it("words the buttons the other way round when the lead allows", () => {
    render(<ApprovalCard item={approvalItemFromPayload(statePayload("worker-decision", "lead-allows-command"))} onApprove={vi.fn()} />);
    expect(screen.getByTestId("workerdec-follow").textContent).toBe("Allow it, as the lead suggests");
    expect(screen.getByTestId("workerdec-override").textContent).toBe("Deny it instead");
  });

  it("says so when the call was already answered, and offers nothing to decide", () => {
    render(<ApprovalCard item={approvalItemFromPayload(statePayload("worker-decision", "already-answered"))} onApprove={vi.fn()} />);
    expect(screen.getByTestId("workerdec-answered").textContent).toContain("already denied");
    expect(screen.queryByTestId("workerdec-override")).toBeNull();
  });

  it("explains the gap when an older server sends no worker call", () => {
    render(<ApprovalCard item={approvalItemFromPayload(statePayload("worker-decision", "old-server"))} onApprove={vi.fn()} />);
    expect(screen.getByTestId("workerdec-missing").textContent).toContain("Open nia’s session");
    expect(screen.getByTestId("workerdec-follow")).toBeTruthy();
  });

  it("parked in the Inbox: follow resolves the lead's item; override resolves the worker's first", () => {
    const onResolve = vi.fn();
    render(<InboxItemCard item={inboxItemBuilders["worker-decision"]!(state("lead-denies-command"))} onResolve={onResolve} />);
    expect(screen.getByTestId("workerdec-card").textContent).not.toContain("call_id");
    fireEvent.click(screen.getByTestId("workerdec-override"));
    expect(onResolve.mock.calls).toEqual([
      ["c51dc4bd79f74e87b7fc59050dc41c37", "allow"],
      ["gallery-item", "deny"],
    ]);
    cleanup();
    const again = vi.fn();
    render(<InboxItemCard item={inboxItemBuilders["worker-decision"]!(state("lead-denies-command"))} onResolve={again} />);
    fireEvent.click(screen.getByTestId("workerdec-follow"));
    expect(again.mock.calls).toEqual([["gallery-item", "allow"]]);
  });
});
