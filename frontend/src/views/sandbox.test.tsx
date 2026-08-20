// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(cleanup);

import { SandboxApproval, SandboxApprovalDecision } from "../sandboxApi";
import { ApprovalPanel } from "./sandbox";

function missingDeadlineApproval(): SandboxApproval {
  return {
    approvalId: "approval-deadline-1",
    requestType: "deadline_required_confirmation",
    commitmentId: null,
    commitmentTitle: "Prepare the meeting notes",
    revision: 1,
    reason: "missing_deadline",
    previousRejectionReason: null,
    proposedMinutes: null,
    options: [],
    requiresOwnership: false,
    ownershipOptions: [],
    proposedOwnership: "my_commitment",
    proposedOperation: "create",
    normalizedOutcome: "Prepare the meeting notes",
    proposedDeadline: null,
    proposedDeadlineExpression: null,
    proposedBlocks: [],
  };
}

function effortApproval(proposedMinutes: number | null): SandboxApproval {
  return {
    ...missingDeadlineApproval(),
    approvalId: "approval-effort-1",
    requestType: "effort_confirmation",
    reason: null,
    proposedMinutes,
  };
}

describe("ApprovalPanel", () => {
  it("says 'enter your estimate' when the thread proposed no effort", () => {
    render(
      <ApprovalPanel
        approval={effortApproval(null)}
        busy={false}
        onResolve={vi.fn()}
      />,
    );
    expect(
      screen.getByText(/does not state an effort/, { exact: false }),
    ).toBeTruthy();
    expect(screen.queryByText(/proposes what the thread implies/)).toBeNull();
  });

  it("says the agent proposes the estimate when one exists", () => {
    render(
      <ApprovalPanel
        approval={effortApproval(180)}
        busy={false}
        onResolve={vi.fn()}
      />,
    );
    expect(
      screen.getByText(/proposes what the thread implies/, { exact: false }),
    ).toBeTruthy();
    expect(
      (screen.getByLabelText(/Minutes for/) as HTMLInputElement).value,
    ).toBe("180");
  });

  it("enables and submits a valid future missing deadline", () => {
    const onResolve = vi.fn<
      (approvalId: string, decision: SandboxApprovalDecision) => void
    >();
    render(
      <ApprovalPanel
        approval={missingDeadlineApproval()}
        busy={false}
        onResolve={onResolve}
      />,
    );

    const confirm = screen.getByRole("button", { name: "Confirm deadline" });
    expect((confirm as HTMLButtonElement).disabled).toBe(true);

    fireEvent.change(
      screen.getByLabelText("Deadline for Prepare the meeting notes"),
      { target: { value: "2026-09-18T17:00" } },
    );

    expect((confirm as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(confirm);
    expect(onResolve).toHaveBeenCalledWith("approval-deadline-1", {
      decision: "approve",
      deadline: "2026-09-19T00:00:00.000Z",
    });
  });
});
