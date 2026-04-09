import React, { useEffect, useRef, useState } from "react";
import useApi from "./hooks/useApi";
import AnalysisScoresPanel from "./components/AnalysisScoresPanel";
import PriceChart, { OHLCVBar, PositionOverlay, SignalOverlay } from "./components/PriceChart";
import SignalHistory, { Signal } from "./components/SignalHistory";
import LogsPanel from "./components/LogsPanel";
import SignalDetailDrawer from "./components/SignalDetailDrawer";
import MarketfeedPanel from "./components/MarketfeedPanel";
import ChatPanel from "./components/ChatPanel";
import AccountPanel from "./components/AccountPanel";
import CampaignsPanel from "./components/CampaignsPanel";
import BinanceMetricsPanel from "./components/BinanceMetricsPanel";
import BinanceProPanel from "./components/BinanceProPanel";
import ConvictionMeter from "./components/ConvictionMeter";
import SynthesisPanel from "./components/SynthesisPanel";
import RiskToolsPanel from "./components/RiskToolsPanel";
import CrossContextPanel from "./components/CrossContextPanel";
import LearningPanel from "./components/LearningPanel";
import LivePriceTicker from "./components/LivePriceTicker";
import ScalpingRangePanel from "./components/ScalpingRangePanel";

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

interface OpenPosition {
  id: number;
  side: "LONG" | "SHORT";
  entry_price: number;
  stop_loss: number | null;
  take_profit: number | null;
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

  // Open positions — for chart overlays (polled every 5s)
  const { data: openPositions } = useApi<OpenPosition[]>(
    "/api/positions?status=open",
    { pollInterval: 5_000 }
  );

  // Timeframe selector — poll faster for lower timeframes
  const [timeframe, setTimeframe] = useState<string>("1min");
  const pollInterval =
    timeframe === "1min" ? 3_000
    : timeframe === "5min" ? 10_000
    : timeframe === "15min" ? 30_000
    : timeframe === "1H" ? 60_000
    : 300_000;

  const { data: ohlcv, loading: ohlcvLoading } = useApi<OHLCVBar[]>(
    `/api/ohlcv?timeframe=${timeframe}&limit=300`,
    { pollInterval }
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

  // Signal detail drawer state
  const [selectedSignalId, setSelectedSignalId] = useState<number | null>(null);

  // Derived overlays for PriceChart
  const positionOverlays: PositionOverlay[] = (openPositions ?? []).map((p) => ({
    id: p.id,
    side: p.side,
    entry_price: p.entry_price,
    stop_loss: p.stop_loss,
    take_profit: p.take_profit,
  }));

  const signalOverlays: SignalOverlay[] = (signals ?? []).map((s) => ({
    id: s.id,
    timestamp: s.timestamp,
    action: s.action,
  }));

  return (
    <div className="min-h-screen bg-gray-950 text-gray-100 p-4 md:p-6">
      {/* Header */}
      <header className="mb-6 grid grid-cols-1 md:grid-cols-3 items-center gap-3">
        <div>
          <h1 className="text-xl font-bold text-white tracking-tight">
            WTI Crude Trading Dashboard
          </h1>
          <p className="text-xs text-gray-500 mt-0.5">
            {lastUpdate
              ? `Last update: ${new Date(lastUpdate).toLocaleTimeString()}`
              : "Connecting…"}
          </p>
        </div>

        {/* Live price ticker — center of header */}
        <div className="flex justify-center">
          <LivePriceTicker />
        </div>

        {/* WebSocket status pill — right side */}
        <div className="flex justify-end">
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
        </div>
      </header>

      {/* Synthesis layer — Now Brief + Signal Confluence + Anomaly Radar */}
      <SynthesisPanel />

      {/* Account Panel */}
      <AccountPanel />

      {/* Scalping Range — 5-min entries for intraday scalping */}
      <ScalpingRangePanel />

      {/* Risk & Scenario Tools — scenario calculator, Monte Carlo, VWAP, calendar */}
      <RiskToolsPanel />

      {/* Cross-Asset + CVD flow */}
      <CrossContextPanel />

      {/* Learning & Feedback Loop — journal, pattern match, smart alerts */}
      <LearningPanel />

      {/* Conviction Meter — composite decision support (own row) */}
      <div className="mb-6 grid grid-cols-1 md:grid-cols-3 gap-3">
        <ConvictionMeter />
      </div>

      {/* Binance Derivatives Metrics — funding, OI, L/S ratios, liquidations */}
      <BinanceMetricsPanel />

      {/* Binance Pro — orderbook, whale trades, volume profile */}
      <BinanceProPanel />

      {/* Analysis Scores — full-width row of 5 bipolar score cards */}
      <AnalysisScoresPanel scores={score as any} loading={scoreLoading} />

      {/* Price Chart */}
      <section className="mb-6">
        {/* Timeframe tabs */}
        <div className="flex items-center gap-1 mb-2">
          {["1min", "5min", "15min", "1H", "1D", "1W"].map((tf) => (
            <button
              key={tf}
              onClick={() => setTimeframe(tf)}
              className={`px-3 py-1 text-xs rounded font-medium transition ${
                timeframe === tf
                  ? "bg-blue-600 text-white"
                  : "bg-gray-800 text-gray-400 hover:bg-gray-700"
              }`}
            >
              {tf}
            </button>
          ))}
          <span className="ml-auto text-[10px] text-gray-600">
            {timeframe === "1min"
              ? "Refreshing every 3s"
              : timeframe === "5min"
              ? "Refreshing every 10s"
              : timeframe === "15min"
              ? "Refreshing every 30s"
              : "Refreshing every 1-5min"}
          </span>
        </div>
        {ohlcvLoading && (!ohlcv || ohlcv.length === 0) ? (
          <div className="bg-gray-900 rounded-xl p-4 h-40 flex items-center justify-center text-gray-600 text-sm">
            Loading chart…
          </div>
        ) : (
          <PriceChart
            key={timeframe}
            bars={ohlcv ?? []}
            timeframe={timeframe}
            positions={positionOverlays}
            signals={signalOverlays}
          />
        )}
      </section>

      {/* Open Campaigns (replaces PositionsPanel) */}
      <CampaignsPanel />

      {/* Signal History */}
      <section className="mb-6">
        <h2 className="text-xs uppercase tracking-widest text-gray-500 mb-3">
          Signal History
        </h2>
        {signalsLoading && (!signals || signals.length === 0) ? (
          <p className="text-gray-600 text-sm">Loading signals…</p>
        ) : (
          <SignalHistory
            signals={signals ?? []}
            onSignalClick={setSelectedSignalId}
          />
        )}
      </section>

      {/* Marketfeed Knowledge */}
      <section className="mb-6">
        <MarketfeedPanel />
      </section>

      {/* Live Logs */}
      <section>
        <LogsPanel />
      </section>

      {/* Signal detail drawer — portaled over layout */}
      <SignalDetailDrawer
        signalId={selectedSignalId}
        onClose={() => setSelectedSignalId(null)}
      />

      {/* Floating chat panel */}
      <ChatPanel />
    </div>
  );
};

export default App;
