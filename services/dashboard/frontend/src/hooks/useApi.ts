import { useState, useEffect, useCallback } from "react";

interface UseApiOptions {
  /** Polling interval in milliseconds. Omit to disable polling. */
  pollInterval?: number;
}

interface UseApiResult<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
  refetch: () => void;
}

/**
 * Generic fetch hook with optional polling.
 *
 * @param url     Relative or absolute URL to fetch
 * @param options Configuration options
 */
function useApi<T>(url: string, options: UseApiOptions = {}): UseApiResult<T> {
  const { pollInterval } = options;
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  const fetchData = useCallback(async () => {
    try {
      const res = await fetch(url);
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}: ${res.statusText}`);
      }
      const json = (await res.json()) as { data: T };
      setData(json.data ?? (json as unknown as T));
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [url]);

  useEffect(() => {
    setLoading(true);
    void fetchData();

    if (pollInterval && pollInterval > 0) {
      const id = setInterval(() => void fetchData(), pollInterval);
      return () => clearInterval(id);
    }
    return undefined;
  }, [fetchData, pollInterval]);

  return { data, loading, error, refetch: fetchData };
}

export default useApi;
