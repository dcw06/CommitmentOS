import { useCallback, useState } from "react";

import { ActivityItem, fetchActivity } from "../api";
import { Card, Empty, formatDateTime, usePolling } from "../ui";

function ActivityPayload({ item }: { item: ActivityItem }) {
  const payload = item.payload;
  const order = Array.isArray(payload.commitment_order)
    ? payload.commitment_order.map(String)
    : [];
  const allocations = Array.isArray(payload.allocations)
    ? payload.allocations
    : [];
  const riskArc =
    typeof payload.risk_arc === "object" && payload.risk_arc !== null
      ? payload.risk_arc
      : null;
  const resumeResult =
    typeof payload.resume_revalidation_result === "string"
      ? payload.resume_revalidation_result
      : null;
  const riskBefore = payload.risk_before_repair;
  const riskAfter = payload.risk_after_repair;

  if (Object.keys(payload).length === 0) return null;
  return (
    <div className="audit-payload">
      {order.length > 0 && (
        <div><strong>Stable portfolio order:</strong> {order.join(" → ")}</div>
      )}
      {allocations.length > 0 && (
        <div>
          <strong>Allocation:</strong>{" "}
          {allocations
            .map((value) => {
              const allocation = value as Record<string, unknown>;
              return `${String(allocation.commitment_id ?? "commitment")} ${String(allocation.allocated_work_minutes ?? 0)}m allocated, ${String(allocation.shortfall_minutes ?? 0)}m shortfall, ${String(allocation.shared_buffer_minutes ?? 0)}m buffer`;
            })
            .join("; ")}
        </div>
      )}
      {riskArc && (
        <div><strong>Risk before/after:</strong> {JSON.stringify(riskArc)}</div>
      )}
      {(typeof riskBefore === "string" || typeof riskAfter === "string") && (
        <div>
          <strong>Risk:</strong> {String(riskBefore ?? "unknown").replaceAll("_", " ")}
          {" → "}{String(riskAfter ?? "unknown").replaceAll("_", " ")}
        </div>
      )}
      {resumeResult && (
        <div>
          <strong>Resume revalidation:</strong> {resumeResult.replaceAll("_", " ")}
          {typeof payload.held_actions_scanned === "number" &&
            ` · ${payload.held_actions_scanned} held scanned`}
          {typeof payload.actions_reissued === "number" &&
            ` · ${payload.actions_reissued} reissued`}
          {typeof payload.actions_superseded === "number" &&
            ` · ${payload.actions_superseded} superseded`}
        </div>
      )}
      {(typeof payload.held_action_count === "number" ||
        typeof payload.in_flight_action_count === "number") && (
        <div>
          <strong>Actions at change:</strong>{" "}
          {String(payload.held_action_count ?? 0)} held ·{" "}
          {String(payload.in_flight_action_count ?? 0)} in flight
        </div>
      )}
      <details>
        <summary>Audit payload{item.traceId ? ` · trace ${item.traceId}` : ""}</summary>
        <pre>{JSON.stringify(payload, null, 2)}</pre>
      </details>
    </div>
  );
}

export function ActivityPage() {
  const [older, setOlder] = useState<ActivityItem[]>([]);
  // undefined = pagination untouched (start from the live page's cursor);
  // null = every older page has been loaded, so the button stays hidden.
  const [cursor, setCursor] = useState<string | null | undefined>(undefined);
  const [loadingMore, setLoadingMore] = useState(false);

  const loadLatest = useCallback(() => fetchActivity(null), []);
  const { data, error } = usePolling(loadLatest, 10000);

  if (!data) return <div className="loading">{error ?? "Loading activity…"}</div>;
  const seen = new Set(
    data.items.map((item) => `${item.createdAt}-${item.summary}`),
  );
  const olderVisible = older.filter(
    (item) => !seen.has(`${item.createdAt}-${item.summary}`),
  );
  const nextCursor = cursor === undefined ? data.nextCursor : cursor;

  const loadMore = async () => {
    if (!nextCursor) return;
    setLoadingMore(true);
    try {
      const page = await fetchActivity(nextCursor);
      setOlder((current) => [...current, ...page.items]);
      setCursor(page.nextCursor);
    } finally {
      setLoadingMore(false);
    }
  };

  const items = [...data.items, ...olderVisible];
  return (
    <Card
      title="Activity"
      note="The decision timeline: every observation, interpretation, policy decision, outbox write, executor result, and control change."
    >
      <div className="timeline">
        {items.length === 0 && <Empty>No activity recorded yet.</Empty>}
        {items.map((item, index) => (
          <div className="timeline-item" key={index}>
            <span className="when">{formatDateTime(item.createdAt)}</span>
            <div className="what">
              <div className="type">{item.eventType.replaceAll("_", " ")}</div>
              <div className="summary">{item.summary}</div>
              <div className="activity-actor">Actor: {item.actor}</div>
              <ActivityPayload item={item} />
            </div>
          </div>
        ))}
      </div>
      {nextCursor && (
        <p>
          <button disabled={loadingMore} onClick={() => void loadMore()}>
            {loadingMore ? "Loading…" : "Load older activity"}
          </button>
        </p>
      )}
    </Card>
  );
}
