import { ReactNode, useCallback, useEffect, useRef, useState } from "react";

export function Card({ title, note, children }: {
  title: string;
  note?: string;
  children: ReactNode;
}) {
  return (
    <section className="card">
      <h2>{title}</h2>
      {note ? <p className="section-note">{note}</p> : null}
      {children}
    </section>
  );
}

export function Badge({ value }: { value: string }) {
  const cls = value.replace(/[^a-z_]/gi, "").toLowerCase();
  return <span className={`badge ${cls}`}>{value.replaceAll("_", " ")}</span>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

const timeFormat = new Intl.DateTimeFormat(undefined, {
  hour: "2-digit",
  minute: "2-digit",
});
const dateTimeFormat = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});

export function formatTime(iso: string): string {
  const parsed = new Date(iso);
  return Number.isNaN(parsed.getTime()) ? iso : timeFormat.format(parsed);
}

export function formatDateTime(iso: string | null): string {
  if (!iso) return "";
  const parsed = new Date(iso);
  return Number.isNaN(parsed.getTime()) ? iso : dateTimeFormat.format(parsed);
}

export function minutesLabel(minutes: number | null): string {
  if (minutes === null) return "—";
  if (minutes >= 60 && minutes % 30 === 0) {
    return `${minutes / 60}h${minutes % 60 ? ` ${minutes % 60}m` : ""}`;
  }
  return `${minutes}m`;
}

/** Poll a loader on an interval; exposes a manual refresh for post-mutation
 *  refetches. The interval keeps the dashboard live so autonomous repairs
 *  appear without a reload. */
export function usePolling<T>(
  load: () => Promise<T>,
  intervalMs: number,
): { data: T | null; error: string | null; refresh: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const loadRef = useRef(load);
  loadRef.current = load;
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    const run = async () => {
      try {
        const result = await loadRef.current();
        if (!cancelled) {
          setData(result);
          setError(null);
        }
      } catch (loadError) {
        if (!cancelled) {
          setError(loadError instanceof Error ? loadError.message : "load failed");
        }
      }
    };
    void run();
    const timer = window.setInterval(run, intervalMs);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [intervalMs, tick]);

  const refresh = useCallback(() => setTick((value) => value + 1), []);
  return { data, error, refresh };
}

/** Wrap a mutation with busy/error state and a follow-up refresh. */
export function useAction(refresh: () => void): {
  busy: boolean;
  error: string | null;
  run: (action: () => Promise<void>) => Promise<void>;
} {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = useCallback(
    async (action: () => Promise<void>) => {
      setBusy(true);
      setError(null);
      try {
        await action();
        refresh();
      } catch (actionError) {
        setError(actionError instanceof Error ? actionError.message : "failed");
      } finally {
        setBusy(false);
      }
    },
    [refresh],
  );
  return { busy, error, run };
}
