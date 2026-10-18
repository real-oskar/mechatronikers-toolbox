import { useEffect, useState } from "react";

import { useRunApi } from "../api/client";
import type { StoredEvent } from "../api/types";

export interface SessionEvents {
  events: StoredEvent[];
  loaded: boolean; // the stored history arrived (the run kind is known from here on)
  connected: boolean | null; // null until the stream opened once
  error: string | null;
}

/** History first, then the live stream from the last id, so reconnects add nothing twice. */
export function useSessionEvents(): SessionEvents {
  const api = useRunApi();
  const [events, setEvents] = useState<StoredEvent[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [connected, setConnected] = useState<boolean | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let stop: (() => void) | undefined;
    let cancelled = false;
    const seen = new Set<number>();
    api
      .history()
      .then((history) => {
        if (cancelled) return;
        history.forEach((e) => seen.add(e.id));
        setEvents(history);
        setLoaded(true);
        const after = history.length ? history[history.length - 1]!.id : 0;
        stop = api.follow(
          after,
          (event) => {
            if (seen.has(event.id)) return;
            seen.add(event.id);
            setEvents((current) => [...current, event]);
          },
          setConnected,
        );
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
    return () => {
      cancelled = true;
      stop?.();
    };
  }, [api]);

  return { events, loaded, connected, error };
}
