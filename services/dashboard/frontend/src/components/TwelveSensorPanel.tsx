/**
 * TwelveSensorPanel — Twelve Data value-add widgets:
 *
 *  1. Market Sessions — which exchanges are open right now + sizing regime
 *  2. WTI Indicators  — pre-computed RSI/MACD/ATR/ADX/BBANDS with bias labels
 *  3. Cross-Asset Stress — 1h RSI for SPY, BTC, UUP as macro stress meter
 */

import React from "react";
import useApi from "../hooks/useApi";

// ---------------------------------------------------------------------------
// Shared helpers
// ---------------------------------------------------------------------------

function biasColor(bias: string | undefined): string {
  if (bias === "bullish") return "text-green-400";
  if (bias === "bearish") return "text-red-400";
  return "text-gray-400";
}

function rsiColor(v: number): string {
  if (v >= 70) return "text-red-400";
  if (v >= 60) return "text-green-400";
  if (v <= 30) return "text-green-400";
  if (v <= 40) return "text-red-400";
  return "text-gray-400";
}

const Card: React.FC<{ title: string; subtitle?: string; children: React.ReactNode }> = ({
  title, subtitle, children,
}) => (
  <div className="bg-gray-900 border border-gray-800 rounded-xl p-4 flex flex-col gap-2">
    <div className="flex items-baseline justify-between">
      <h3 className="text-xs uppercase tracking-widest text-gray-500 font-medium">{title}</h3>
      {subtitle && <span className="text-[10px] text-gray-600">{subtitle}</span>}
    </div>
    {children}
  </div>
);

// ---------------------------------------------------------------------------
// 1. Market Sessions
// ---------------------------------------------------------------------------

interface MarketSessions {
  generated_at: string;
  total_exchanges: number;
  open_count: number;
  active_sessions: { us: boolean; london: boolean; asia: boolean };
  active_us_exchanges: string[];
  active_london_exchanges: string[];
  active_asia_exchanges: string[];
  regime: string;
  sizing_multiplier: number;
  error?: string;
}

function regimeColor(regime: string): { bg: string; text: string; label: string } {
  switch (regime) {
    case "us_london_overlap":
      return { bg: "bg-green-950/60 border-green-800", text: "text-green-300", label: "PEAK LIQUIDITY" };
    case "us_only":
      return { bg: "bg-emerald-950/40 border-emerald-800", text: "text-emerald-300", label: "US ACTIVE" };
    case "london_only":
      return { bg: "bg-blue-950/40 border-blue-800", text: "text-blue-300", label: "LONDON ACTIVE" };
    case "asia_only":
      return { bg: "bg-yellow-950/40 border-yellow-800", text: "text-yellow-300", label: "THIN ASIA" };
    case "all_closed":
      return { bg: "bg-red-950/40 border-red-800", text: "text-red-300", label: "ALL CLOSED" };
    default:
      return { bg: "bg-gray-900 border-gray-800", text: "text-gray-400", label: regime.toUpperCase() };
  }
}

const MarketSessionsCard: React.FC = () => {
  const { data } = useApi<MarketSessions>("/api/market-sessions", { pollInterval: 60_000 });

  if (!data) {
    return <Card title="Market Sessions"><div className="text-gray-600 text-xs">loading…</div></Card>;
  }
  if (data.error) {
    return <Card title="Market Sessions"><div className="text-red-400 text-xs">{data.error}</div></Card>;
  }

  const rc = regimeColor(data.regime);

  const Pill: React.FC<{ label: string; open: boolean }> = ({ label, open }) => (
    <span
      className={`text-[10px] px-2 py-0.5 rounded border ${
        open
          ? "bg-green-900/40 border-green-800 text-green-300"
          : "bg-gray-800/40 border-gray-800 text-gray-600"
      }`}
    >
      {open ? "●" : "○"} {label}
    </span>
  );

  return (
    <Card title="Market Sessions" subtitle={`${data.open_count}/${data.total_exchanges} open`}>
      <div className={`border rounded px-3 py-2 ${rc.bg}`}>
        <div className={`text-xs font-bold ${rc.text}`}>{rc.label}</div>
        <div className="text-[10px] text-gray-500">
          Sizing multiplier ×{data.sizing_multiplier}
        </div>
      </div>
      <div className="flex flex-wrap gap-1.5">
        <Pill label="US" open={data.active_sessions.us} />
        <Pill label="London" open={data.active_sessions.london} />
        <Pill label="Asia" open={data.active_sessions.asia} />
      </div>
      {data.active_us_exchanges.length > 0 && (
        <div className="text-[10px] text-gray-500">
          US: <span className="text-gray-300">{data.active_us_exchanges.join(", ")}</span>
        </div>
      )}
      {data.active_london_exchanges.length > 0 && (
        <div className="text-[10px] text-gray-500">
          EU: <span className="text-gray-300">{data.active_london_exchanges.join(", ")}</span>
        </div>
      )}
      {data.active_asia_exchanges.length > 0 && (
        <div className="text-[10px] text-gray-500">
          Asia: <span className="text-gray-300">{data.active_asia_exchanges.join(", ")}</span>
        </div>
      )}
    </Card>
  );
};

// ---------------------------------------------------------------------------
// 2. WTI Indicators
// ---------------------------------------------------------------------------

interface WtiIndicators {
  symbol: string;
  interval: string;
  rsi?: any;
  macd?: any;
  atr?: any;
  adx?: any;
  bbands?: any;
  interpretation?: {
    rsi?: { value: number; label: string; bias: string };
    macd?: { label: string; bias: string };
    atr?: { value: number; label: string };
    adx?: { value: number; label: string };
    bbands?: { upper: number; middle: number; lower: number; width_pct: number };
    overall_bias?: string;
  };
}

const WtiIndicatorsCard: React.FC = () => {
  const { data } = useApi<WtiIndicators>("/api/td-indicators/wti?interval=1h", { pollInterval: 300_000 });

  if (!data) {
    return <Card title="WTI Indicators (1h)"><div className="text-gray-600 text-xs">loading…</div></Card>;
  }

  const i = data.interpretation || {};
  const overall = i.overall_bias ?? "neutral";

  return (
    <Card title="WTI Indicators" subtitle={`${data.symbol} · ${data.interval}`}>
      <div className="flex items-baseline gap-2 mb-1">
        <span className="text-[10px] uppercase text-gray-500">Composite</span>
        <span className={`text-sm font-bold ${biasColor(overall)}`}>{overall.toUpperCase()}</span>
      </div>

      <div className="flex flex-col gap-1 text-[11px] font-mono">
        {i.rsi && (
          <div className="flex justify-between">
            <span className="text-gray-500">RSI14</span>
            <span className={rsiColor(i.rsi.value)}>{i.rsi.value.toFixed(1)}</span>
            <span className={`${biasColor(i.rsi.bias)} text-[9px] w-20 text-right`}>{i.rsi.label}</span>
          </div>
        )}
        {i.macd && (
          <div className="flex justify-between">
            <span className="text-gray-500">MACD</span>
            <span className={biasColor(i.macd.bias)} style={{ textAlign: "right" }}>{i.macd.label}</span>
          </div>
        )}
        {i.adx && (
          <div className="flex justify-between">
            <span className="text-gray-500">ADX14</span>
            <span className="text-gray-200">{i.adx.value.toFixed(1)}</span>
            <span className="text-gray-500 text-[9px] w-24 text-right">{i.adx.label.replace(/^ADX [\d.]+ /, "")}</span>
          </div>
        )}
        {i.atr && (
          <div className="flex justify-between">
            <span className="text-gray-500">ATR14</span>
            <span className="text-gray-200">${i.atr.value.toFixed(3)}</span>
            <span className="text-gray-600 text-[9px] w-24 text-right">per bar</span>
          </div>
        )}
        {i.bbands && (
          <div className="flex justify-between">
            <span className="text-gray-500">BB20/2</span>
            <span className="text-gray-300 text-[10px]">
              ${i.bbands.lower.toFixed(2)} – ${i.bbands.upper.toFixed(2)}
            </span>
            <span className="text-gray-500 text-[9px] w-16 text-right">{i.bbands.width_pct.toFixed(2)}%</span>
          </div>
        )}
      </div>

      <div className="text-[9px] text-gray-600 pt-1 border-t border-gray-800">
        Pre-computed by Twelve Data · 5-min cache · cross-check for analyzer
      </div>
    </Card>
  );
};

// ---------------------------------------------------------------------------
// 3. Cross-Asset Stress
// ---------------------------------------------------------------------------

interface CrossStress {
  symbols: Record<string, { rsi: number; state: string; description: string }>;
}

const CrossStressCard: React.FC = () => {
  const { data } = useApi<CrossStress>("/api/td-indicators/cross-stress", { pollInterval: 300_000 });

  if (!data?.symbols) {
    return <Card title="Cross-Asset Stress"><div className="text-gray-600 text-xs">loading…</div></Card>;
  }

  return (
    <Card title="Cross-Asset Stress" subtitle="1h RSI">
      <div className="flex flex-col gap-2">
        {Object.entries(data.symbols).map(([sym, info]) => (
          <div key={sym} className="border border-gray-800 rounded p-2">
            <div className="flex items-baseline justify-between">
              <span className="text-xs font-bold text-gray-200">{sym}</span>
              <span className={`text-sm font-bold ${rsiColor(info.rsi)}`}>{info.rsi}</span>
            </div>
            <div className="flex items-baseline justify-between text-[9px]">
              <span className="text-gray-500">{info.description}</span>
              <span className={rsiColor(info.rsi)}>{info.state}</span>
            </div>
          </div>
        ))}
      </div>
      <div className="text-[9px] text-gray-600 pt-1 border-t border-gray-800">
        Macro regime snapshot — correlate with WTI for risk-on/off shifts
      </div>
    </Card>
  );
};

// ---------------------------------------------------------------------------
// Main panel
// ---------------------------------------------------------------------------

const TwelveSensorPanel: React.FC = () => (
  <div className="mb-6">
    <h2 className="text-xs uppercase tracking-widest text-gray-500 font-medium mb-3">
      Twelve Data Sensors
    </h2>
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
      <MarketSessionsCard />
      <WtiIndicatorsCard />
      <CrossStressCard />
    </div>
  </div>
);

export default TwelveSensorPanel;
