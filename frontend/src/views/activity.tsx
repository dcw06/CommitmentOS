import { useCallback, useState } from "react";

import { ActivityItem, fetchActivity } from "../api";
import { Card, Empty, formatDateTime, usePolling } from "../ui";

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
