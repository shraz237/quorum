import React, { useEffect, useRef, useState } from "react";
import useApi from "./hooks/useApi";
import { OHLCVBar, PositionOverlay, SignalOverlay } from "./components/PriceChart";
import { Signal } from "./components/SignalHistory";
import SignalDetailDrawer from "./components/SignalDetailDrawer";
import ChatPanel from "./components/ChatPanel";
import CockpitBar, { TabKey, TABS } from "./components/CockpitBar";
import TradeNowTab from "./components/tabs/TradeNowTab";
import PositionsTab from "./components/tabs/PositionsTab";
import MarketTab from "./components/tabs/MarketTab";
import ThesesTab from "./components/tabs/ThesesTab";
import InvestigateTab from "./components/tabs/InvestigateTab";
import SystemTab from "./components/tabs/SystemTab";

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
// Tab persistence
// ---------------------------------------------------------------------------

const TAB_STORAGE_KEY = "dashboard:active_tab";

const loadInitialTab = (): TabKey => {
  if (typeof window === "undefined") return "trade_now";
  try {
    const stored = window.localStorage.getItem(TAB_STORAGE_KEY);
    if (stored && TABS.some((t) => t.key === stored)) {
      return stored as TabKey;
    }
  } catch {
    // localStorage blocked — fall through to default
  }
  return "trade_now";
};

// ---------------------------------------------------------------------------
// App
// ---------------------------------------------------------------------------

const WS_URL =
  typeof window !== "undefined"
    ? `${window.location.protocol === "https:" ? "wss" : "ws"}://${window.location.host}/ws`
    : "ws://localhost:8000/ws";

const App: React.FC = () => {
  // REST data polled at App level so the cockpit (and whichever tab is
  // mounted) can read it. Heavy per-tab data is polled inside each panel.
  const { data: latestScore, loading: scoreLoading } =
    useApi<AnalysisScore>("/api/scores/latest", { pollInterval: 30_000 });

  const { data: signals, loading: signalsLoading } = useApi<Signal[]>(
    "/api/signals?limit=20",
    { pollInterval: 30_000 }
  );

  const { data: openPositions } = useApi<OpenPosition[]>(
    "/api/positions?status=open",
    { pollInterval: 5_000 }
  );

  // Chart timeframe state — lives here so switching tabs doesn't reset it
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

  // Signal detail drawer state — modal works from any tab
  const [selectedSignalId, setSelectedSignalId] = useState<number | null>(null);

  // Tab state + persistence
  const [activeTab, setActiveTab] = useState<TabKey>(loadInitialTab);

  useEffect(() => {
    try {
      window.localStorage.setItem(TAB_STORAGE_KEY, activeTab);
    } catch {
      // ignore
    }
  }, [activeTab]);

  // Keyboard shortcuts 1-5 for tab switching
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      // Ignore when typing in an input / textarea / contentEditable
      const target = e.target as HTMLElement | null;
      if (target) {
        const tag = target.tagName;
        if (
          tag === "INPUT" ||
          tag === "TEXTAREA" ||
          tag === "SELECT" ||
          target.isContentEditable
        ) {
          return;
        }
      }
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const tab = TABS.find((t) => t.shortcut === e.key);
      if (tab) {
        setActiveTab(tab.key);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);

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
      <CockpitBar
        activeTab={activeTab}
        onTabChange={setActiveTab}
        wsConnected={wsConnected}
        lastUpdate={lastUpdate}
      />

      {/* Tab content — lazy: only the active tab mounts */}
      <main className="mt-6">
        {activeTab === "trade_now" && (
          <TradeNowTab
            timeframe={timeframe}
            setTimeframe={setTimeframe}
            ohlcv={ohlcv ?? []}
            ohlcvLoading={ohlcvLoading}
            positionOverlays={positionOverlays}
            signalOverlays={signalOverlays}
          />
        )}
        {activeTab === "positions" && <PositionsTab />}
        {activeTab === "market" && <MarketTab />}
        {activeTab === "theses" && <ThesesTab />}
        {activeTab === "investigate" && (
          <InvestigateTab
            score={score as any}
            scoreLoading={scoreLoading}
            signals={signals ?? []}
            signalsLoading={signalsLoading}
            onSignalClick={setSelectedSignalId}
          />
        )}
        {activeTab === "system" && <SystemTab />}
      </main>

      {/* Global overlays — accessible from any tab */}
      <SignalDetailDrawer
        signalId={selectedSignalId}
        onClose={() => setSelectedSignalId(null)}
      />
      <ChatPanel />
    </div>
  );
};

export default App;
