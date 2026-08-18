// @vitest-environment jsdom

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

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
  };
}

describe("ApprovalPanel", () => {
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
