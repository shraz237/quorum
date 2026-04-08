/**
 * BinanceMetricsPanel — four Binance-only widgets side by side.
 *
 *   1. Funding Rate gauge + sparkline (contrarian sentiment extreme)
 *   2. Open Interest line (confirmation of momentum)
 *   3. Long/Short ratios (smart money vs retail)
 *   4. Liquidation feed + 24h totals
 */

import React from "react";
import useApi from "../hooks/useApi";

// ---------------------------------------------------------------------------
// Shared types
// ---------------------------------------------------------------------------

interface FundingRateState {
  symbol: string;
  latest: { time: number; rate: number; rate_pct: number; mark_price: number | null } | null;
  series: { time: number; rate: number; rate_pct: number; mark_price: number | null }[];
}

interface OpenInterestState {
  symbol: string;
  latest: { time: number; open_interest: number; open_interest_value_usd: number | null } | null;
  change_pct_over_window: number | null;
  series: { time: number; open_interest: number; open_interest_value_usd: number | null }[];
}

interface LongShortPoint {
  time: number;
  long_pct: number | null;
  short_pct: number | null;
  ratio: number;
  buy_volume: number | null;
  sell_volume: number | null;
}

interface LongShortState {
  symbol: string;
  latest: {
    top_position: LongShortPoint | null;
    global_account: LongShortPoint | null;
    taker: LongShortPoint | null;
  };
  series: {
    top_position: LongShortPoint[];
    global_account: LongShortPoint[];
    taker: LongShortPoint[];
  };
}

interface LiquidationEvent {
  time: number;
  side: string;
  price: number;
  executed_qty: number | null;
  quote_qty_usd: number | null;
  order_status: string | null;
}

interface LiquidationsState {
  symbol: string;
  window_hours: number;
  count: number;
  buy_volume_usd: number;   // shorts liquidated
  sell_volume_usd: number;  // longs liquidated
  events: LiquidationEvent[];
}

// ---------------------------------------------------------------------------
// Shared formatters
// ---------------------------------------------------------------------------

function fmtUsdCompact(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "—";
  const abs = Math.abs(v);
  if (abs >= 1e9) return `$${(v / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `$${(v / 1e6).toFixed(2)}M`;
  if (abs >= 1e3) return `$${(v / 1e3).toFixed(1)}K`;
  return `$${v.toFixed(0)}`;
}

function fmtInt(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "—";
  return v.toLocaleString("en-US", { maximumFractionDigits: 0 });
}

function fmtHM(timestampSec: number): string {
  const d = new Date(timestampSec * 1000);
  return d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: false });
}

// ---------------------------------------------------------------------------
// Sparkline — tiny inline SVG line chart with colored fill
// ---------------------------------------------------------------------------

interface SparklineProps {
  points: number[];
  width?: number;
  height?: number;
  colorPos?: string;
  colorNeg?: string;
  zeroLine?: boolean;
}

const Sparkline: React.FC<SparklineProps> = ({
  points,
  width = 140,
  height = 32,
  colorPos = "#10b981",
  colorNeg = "#ef4444",
  zeroLine = false,
}) => {
  if (points.length < 2) {
    return <div className="text-[10px] text-gray-600 italic">no data</div>;
  }

  const min = Math.min(...points, zeroLine ? 0 : Infinity);
  const max = Math.max(...points, zeroLine ? 0 : -Infinity);
  const range = max - min || 1;
  const step = width / (points.length - 1);
  const y = (v: number) => height - ((v - min) / range) * height;

  const path = points
    .map((v, i) => `${i === 0 ? "M" : "L"} ${(i * step).toFixed(2)} ${y(v).toFixed(2)}`)
    .join(" ");

  const lastVal = points[points.length - 1];
  const stroke = lastVal >= 0 ? colorPos : colorNeg;
  const zeroY = zeroLine ? y(0) : null;

  return (
    <svg width={width} height={height} className="overflow-visible">
      {zeroY !== null && (
        <line
          x1="0" y1={zeroY} x2={width} y2={zeroY}
          stroke="#4b5563" strokeWidth="0.5" strokeDasharray="2 2"
        />
      )}
      <path d={path} fill="none" stroke={stroke} strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
};

// ---------------------------------------------------------------------------
// Widget cards
// ---------------------------------------------------------------------------

const Card: React.FC<{ title: string; children: React.ReactNode; subtitle?: string }> = ({
  title,
  children,
  subtitle,
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
// 1. Funding rate gauge
// ---------------------------------------------------------------------------

const FundingRateCard: React.FC = () => {
  const { data } = useApi<FundingRateState>("/api/funding-rate?hours=168", {
    pollInterval: 60_000,
  });

  if (!data?.latest) {
    return <Card title="Funding Rate"><div className="text-gray-600 text-xs">loading…</div></Card>;
  }

  const pct = data.latest.rate_pct;
  const absPct = Math.abs(pct);
  let color = "text-gray-300";
  let sentiment = "neutral";
  if (pct >= 0.03) { color = "text-red-400"; sentiment = "FROTHY LONGS — short squeeze risk"; }
  else if (pct >= 0.01) { color = "text-yellow-400"; sentiment = "mild bullish"; }
  else if (pct <= -0.03) { color = "text-green-400"; sentiment = "SHORTS CROWDED — long squeeze setup"; }
  else if (pct <= -0.01) { color = "text-emerald-500"; sentiment = "mild bearish"; }

  const points = data.series.map(s => s.rate_pct);

  return (
    <Card title="Funding Rate" subtitle={`${data.symbol} · 7d`}>
      <div className="flex items-baseline gap-2">
        <span className={`text-2xl font-bold ${color}`}>
          {pct >= 0 ? "+" : ""}{pct.toFixed(4)}%
        </span>
        <span className="text-[10px] text-gray-500">/ 8h</span>
      </div>
      <span className={`text-[10px] ${absPct >= 0.03 ? "font-semibold" : ""} ${color}`}>{sentiment}</span>
      <Sparkline points={points} zeroLine width={260} height={40} />
      <div className="flex justify-between text-[10px] text-gray-600">
        <span>7d avg: {(points.reduce((a, b) => a + b, 0) / points.length).toFixed(4)}%</span>
        <span>{points.length} entries</span>
      </div>
    </Card>
  );
};

// ---------------------------------------------------------------------------
// 2. Open interest tracker
// ---------------------------------------------------------------------------

const OpenInterestCard: React.FC = () => {
  const { data } = useApi<OpenInterestState>("/api/open-interest?hours=24", {
    pollInterval: 60_000,
  });

  if (!data?.latest) {
    return <Card title="Open Interest"><div className="text-gray-600 text-xs">loading…</div></Card>;
  }

  const change = data.change_pct_over_window ?? 0;
  const changeColor = change >= 0 ? "text-green-400" : "text-red-400";
  const points = data.series.map(s => s.open_interest);

  return (
    <Card title="Open Interest" subtitle={`${data.symbol} · 24h`}>
      <div className="flex items-baseline gap-3">
        <span className="text-xl font-bold text-gray-100">
          {fmtInt(data.latest.open_interest)}
        </span>
        <span className={`text-sm font-medium ${changeColor}`}>
          {change >= 0 ? "+" : ""}{change.toFixed(2)}%
        </span>
      </div>
      <span className="text-[10px] text-gray-500">
        {fmtUsdCompact(data.latest.open_interest_value_usd)} notional
      </span>
      <Sparkline points={points} width={260} height={40} colorPos="#60a5fa" colorNeg="#60a5fa" />
      <div className="text-[10px] text-gray-600">
        {data.series.length} samples · 5-min interval
      </div>
    </Card>
  );
};

// ---------------------------------------------------------------------------
// 3. Long/short ratios
// ---------------------------------------------------------------------------

const LongShortCard: React.FC = () => {
  const { data } = useApi<LongShortState>("/api/long-short-ratio?hours=24", {
    pollInterval: 60_000,
  });

  if (!data?.latest) {
    return <Card title="Long/Short Ratios"><div className="text-gray-600 text-xs">loading…</div></Card>;
  }

  const top = data.latest.top_position;
  const global = data.latest.global_account;
  const taker = data.latest.taker;

  const Bar: React.FC<{ label: string; longPct: number | null; sub?: string }> = ({
    label,
    longPct,
    sub,
  }) => {
    if (longPct == null) return <div className="text-[10px] text-gray-600">{label}: —</div>;
    const pct = longPct * 100;
    const color =
      pct > 65 ? "bg-green-500" : pct > 55 ? "bg-emerald-500" : pct > 45 ? "bg-gray-500" : pct > 35 ? "bg-orange-500" : "bg-red-500";
    return (
      <div className="text-xs">
        <div className="flex justify-between mb-0.5">
          <span className="text-gray-400">{label}</span>
          <span className="font-semibold text-gray-100">{pct.toFixed(1)}% long</span>
        </div>
        <div className="h-1.5 bg-gray-800 rounded-full overflow-hidden">
          <div className={`h-full ${color}`} style={{ width: `${pct}%` }} />
        </div>
        {sub && <div className="text-[10px] text-gray-600 mt-0.5">{sub}</div>}
      </div>
    );
  };

  const delta =
    top?.long_pct != null && global?.long_pct != null
      ? (global.long_pct - top.long_pct) * 100
      : null;

  const takerRatio = taker?.ratio ?? null;
  const takerFlow =
    takerRatio == null
      ? null
      : takerRatio > 1.1
      ? { color: "text-green-400", label: "buyers dominating" }
      : takerRatio < 0.9
      ? { color: "text-red-400", label: "sellers dominating" }
      : { color: "text-gray-400", label: "balanced" };

  return (
    <Card title="Long/Short Ratios" subtitle={`${data.symbol} · 24h`}>
      <div className="flex flex-col gap-2">
        <Bar label="Top traders" longPct={top?.long_pct ?? null} sub="position-weighted (smart money)" />
        <Bar label="Global retail" longPct={global?.long_pct ?? null} sub="account count (crowd)" />
        {delta !== null && (
          <div className="text-[10px] text-gray-500 pt-1 border-t border-gray-800">
            Retail is <span className={delta > 0 ? "text-red-400" : "text-green-400"}>
              {delta > 0 ? "+" : ""}{delta.toFixed(1)}%
            </span> more long than smart money
            {Math.abs(delta) > 15 && (
              <span className="text-yellow-500 ml-1 font-semibold">⚠ contrarian</span>
            )}
          </div>
        )}
        {takerFlow && (
          <div className="text-[10px] text-gray-500">
            Taker flow (5m):{" "}
            <span className={`font-semibold ${takerFlow.color}`}>
              {takerRatio?.toFixed(2)} · {takerFlow.label}
            </span>
          </div>
        )}
      </div>
    </Card>
  );
};

// ---------------------------------------------------------------------------
// 4. Liquidations feed
// ---------------------------------------------------------------------------

const LiquidationsCard: React.FC = () => {
  const { data } = useApi<LiquidationsState>("/api/liquidations?hours=24&limit=100", {
    pollInterval: 15_000,
  });

  if (!data) {
    return <Card title="Liquidations"><div className="text-gray-600 text-xs">loading…</div></Card>;
  }

  // On CLUSDT perpetual, SELL-side force orders = longs being liquidated, BUY-side = shorts liquidated
  return (
    <Card title="Liquidations" subtitle={`${data.symbol} · ${data.window_hours}h`}>
      <div className="flex gap-3 text-xs">
        <div className="flex-1 bg-red-950/40 border border-red-900 rounded px-2 py-1">
          <div className="text-[10px] text-red-400 uppercase">Longs liq'd</div>
          <div className="text-sm font-bold text-red-300">{fmtUsdCompact(data.sell_volume_usd)}</div>
        </div>
        <div className="flex-1 bg-green-950/40 border border-green-900 rounded px-2 py-1">
          <div className="text-[10px] text-green-400 uppercase">Shorts liq'd</div>
          <div className="text-sm font-bold text-green-300">{fmtUsdCompact(data.buy_volume_usd)}</div>
        </div>
      </div>
      <div className="text-[10px] text-gray-500">
        {data.count} events · updates every 15s
      </div>
      <div className="max-h-32 overflow-y-auto pr-1 flex flex-col gap-0.5">
        {data.events.length === 0 ? (
          <div className="text-[10px] text-gray-600 italic py-2">No liquidations in window</div>
        ) : (
          data.events.slice(0, 20).map((e, i) => {
            const isLong = e.side === "SELL"; // sell order = long liquidated
            return (
              <div
                key={i}
                className={`flex items-center gap-2 text-[10px] ${isLong ? "text-red-400" : "text-green-400"}`}
              >
                <span className="w-10 text-gray-600">{fmtHM(e.time)}</span>
                <span className="w-10 font-semibold">{isLong ? "LONG" : "SHORT"}</span>
                <span className="w-16">${e.price.toFixed(2)}</span>
                <span className="ml-auto">{fmtUsdCompact(e.quote_qty_usd)}</span>
              </div>
            );
          })
        )}
      </div>
    </Card>
  );
};

// ---------------------------------------------------------------------------
// Main export — grid of all four cards
// ---------------------------------------------------------------------------

const BinanceMetricsPanel: React.FC = () => {
  return (
    <div className="mb-6">
      <h2 className="text-xs uppercase tracking-widest text-gray-500 font-medium mb-3">
        Binance Derivatives Metrics
      </h2>
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-3">
        <FundingRateCard />
        <OpenInterestCard />
        <LongShortCard />
        <LiquidationsCard />
      </div>
    </div>
  );
};

export default BinanceMetricsPanel;
