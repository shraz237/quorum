import React, { useEffect, useRef, useState } from "react";
import useApi from "./hooks/useApi";
import ScoreGauge from "./components/ScoreGauge";
import PriceChart, { OHLCVBar } from "./components/PriceChart";
import SignalHistory, { Signal } from "./components/SignalHistory";

// ---------------------------------------------------------------------------
// Types matching the backend JSON
// ---------------------------------------------------------------------------

interface AnalysisScore {
  id: number;
  timestamp: string;
  technical_score: number | null;
  fundamental_score: number | null;
  sentiment_score: number | null;
  shipping_score: number | null;
  unified_score: number | null;
}

interface WsUpdate {
  type: string;
  timestamp: string;
  latest_score: AnalysisScore | null;
  latest_signal: Signal | null;
}

// ---------------------------------------------------------------------------
// WebSocket hook — receives live updates from /ws
// ---------------------------------------------------------------------------

function useWebSocket(url: string, onMessage: (msg: WsUpdate) => void) {
  const wsRef = useRef<WebSocket | null>(null);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    let retryTimer: ReturnType<typeof setTimeout> | null = null;

    function connect() {
      const ws = new WebSocket(url);
      wsRef.current = ws;

      ws.onopen = () => setConnected(true);
      ws.onclose = () => {
        setConnected(false);
        retryTimer = setTimeout(connect, 5000);
      };
      ws.onerror = () => ws.close();
      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data as string) as WsUpdate;
          onMessage(data);
        } catch {
          // ignore parse errors
        }
      };
    }

    connect();

    return () => {
      if (retryTimer) clearTimeout(retryTimer);
      wsRef.current?.close();
    };
  }, [url, onMessage]);

  return connected;
}

// ---------------------------------------------------------------------------
// App
// ---------------------------------------------------------------------------

const WS_URL =
  typeof window !== "undefined"
    ? `${window.location.protocol === "https:" ? "wss" : "ws"}://${window.location.host}/ws`
    : "ws://localhost:8000/ws";

const App: React.FC = () => {
  // REST data — polled every 30 s
  const { data: latestScore, loading: scoreLoading } =
    useApi<AnalysisScore>("/api/scores/latest", { pollInterval: 30_000 });

  const { data: signals, loading: signalsLoading } = useApi<Signal[]>(
    "/api/signals?limit=20",
    { pollInterval: 30_000 }
  );

  const { data: ohlcv, loading: ohlcvLoading } = useApi<OHLCVBar[]>(
    "/api/ohlcv?timeframe=1H&limit=200",
    { pollInterval: 60_000 }
  );

  // Live score override from WebSocket
  const [liveScore, setLiveScore] = useState<AnalysisScore | null>(null);
  const [lastUpdate, setLastUpdate] = useState<string | null>(null);

  const handleWsMessage = React.useCallback((msg: WsUpdate) => {
    if (msg.latest_score) setLiveScore(msg.latest_score);
    setLastUpdate(msg.timestamp);
  }, []);

  const wsConnected = useWebSocket(WS_URL, handleWsMessage);

  const score = liveScore ?? latestScore;

  return (
    <div className="min-h-screen bg-gray-950 text-gray-100 p-4 md:p-6">
      {/* Header */}
      <header className="mb-6 flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-white tracking-tight">
            Brent Crude Trading Dashboard
          </h1>
          <p className="text-xs text-gray-500 mt-0.5">
            {lastUpdate
              ? `Last update: ${new Date(lastUpdate).toLocaleTimeString()}`
              : "Connecting…"}
          </p>
        </div>

        {/* WebSocket status pill */}
        <span
          className={`flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-medium ${
            wsConnected
              ? "bg-green-900 text-green-300"
              : "bg-red-900 text-red-300"
          }`}
        >
          <span
            className={`w-2 h-2 rounded-full ${
              wsConnected ? "bg-green-400 animate-pulse" : "bg-red-400"
            }`}
          />
          {wsConnected ? "Live" : "Disconnected"}
        </span>
      </header>

      {/* Score Gauges */}
      <section className="mb-6">
        <h2 className="text-xs uppercase tracking-widest text-gray-500 mb-3">
          Analysis Scores
        </h2>
        {scoreLoading && !score ? (
          <p className="text-gray-600 text-sm">Loading scores…</p>
        ) : (
          <div className="flex flex-wrap gap-3">
            <ScoreGauge label="Technical" value={score?.technical_score} />
            <ScoreGauge label="Fundamental" value={score?.fundamental_score} />
            <ScoreGauge label="Sentiment" value={score?.sentiment_score} />
            <ScoreGauge label="Shipping" value={score?.shipping_score} />
            <ScoreGauge label="Unified" value={score?.unified_score} />
          </div>
        )}
      </section>

      {/* Price Chart */}
      <section className="mb-6">
        {ohlcvLoading && (!ohlcv || ohlcv.length === 0) ? (
          <div className="bg-gray-900 rounded-xl p-4 h-40 flex items-center justify-center text-gray-600 text-sm">
            Loading chart…
          </div>
        ) : (
          <PriceChart bars={ohlcv ?? []} timeframe="1H" />
        )}
      </section>

      {/* Signal History */}
      <section>
        <h2 className="text-xs uppercase tracking-widest text-gray-500 mb-3">
          Signal History
        </h2>
        {signalsLoading && (!signals || signals.length === 0) ? (
          <p className="text-gray-600 text-sm">Loading signals…</p>
        ) : (
          <SignalHistory signals={signals ?? []} />
        )}
      </section>
    </div>
  );
};

export default App;
