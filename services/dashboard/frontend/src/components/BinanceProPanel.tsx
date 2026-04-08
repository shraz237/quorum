/**
 * BinanceProPanel — three advanced Binance widgets in a grid:
 *   1. Order Book Heatmap — bid/ask walls with imbalance indicator
 *   2. Whale Trade Feed — aggregated trades >= $50k quote volume
 *   3. Volume Profile — horizontal histogram with POC + value area
 */

import React from "react";
import useApi from "../hooks/useApi";

// ---------------------------------------------------------------------------
// Shared formatters (local to avoid cross-file coupling)
// ---------------------------------------------------------------------------

function fmtUsdCompact(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "—";
  const abs = Math.abs(v);
  if (abs >= 1e9) return `$${(v / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `$${(v / 1e6).toFixed(2)}M`;
  if (abs >= 1e3) return `$${(v / 1e3).toFixed(1)}K`;
  return `$${v.toFixed(0)}`;
}

function fmtHM(timestampSec: number): string {
  return new Date(timestampSec * 1000).toLocaleTimeString("en-US", {
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  });
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
// 1. Order Book Heatmap
// ---------------------------------------------------------------------------

interface BookLevel { price: number; qty: number; }
interface OrderBook {
  symbol: string;
  mid: number | null;
  best_bid: number | null;
  best_ask: number | null;
  spread: number | null;
  total_bid_volume: number;
  total_ask_volume: number;
  imbalance: number;
  bids: BookLevel[];
  asks: BookLevel[];
}

const OrderBookCard: React.FC = () => {
  const { data } = useApi<OrderBook>("/api/orderbook?depth=100", {
    pollInterval: 5_000,
  });

  if (!data || !data.mid) {
    return <Card title="Order Book"><div className="text-gray-600 text-xs">loading…</div></Card>;
  }

  // Aggregate bids and asks into top 8 levels for the bar visualisation
  const topBids = data.bids.slice(0, 8);
  const topAsks = data.asks.slice(0, 8);
  const maxQty = Math.max(
    ...topBids.map(b => b.qty),
    ...topAsks.map(a => a.qty),
    1,
  );

  const imbPct = data.imbalance * 100;
  const imbColor = imbPct > 20 ? "text-green-400" : imbPct < -20 ? "text-red-400" : "text-gray-400";
  const imbLabel = imbPct > 20 ? "bid wall" : imbPct < -20 ? "ask wall" : "balanced";

  return (
    <Card title="Order Book" subtitle={`${data.symbol} · 100 levels`}>
      {/* Mid + spread header */}
      <div className="flex items-baseline justify-between">
        <span className="text-xl font-bold text-gray-100">${data.mid.toFixed(3)}</span>
        <span className="text-[10px] text-gray-500">
          spread ${data.spread?.toFixed(3) ?? "—"}
        </span>
      </div>

      {/* Imbalance indicator */}
      <div className="flex items-center gap-2 text-[11px]">
        <span className="text-gray-500">Imbalance:</span>
        <span className={`font-bold ${imbColor}`}>
          {imbPct >= 0 ? "+" : ""}{imbPct.toFixed(1)}% ({imbLabel})
        </span>
      </div>

      {/* Ask walls (top 8, reversed so highest price is on top) */}
      <div className="flex flex-col gap-0.5">
        {[...topAsks].reverse().map((a, i) => {
          const w = (a.qty / maxQty) * 100;
          return (
            <div key={`a${i}`} className="flex items-center text-[10px] font-mono">
              <span className="w-16 text-red-400">${a.price.toFixed(3)}</span>
              <div className="flex-1 h-3 bg-gray-900 relative">
                <div
                  className="absolute inset-y-0 left-0 bg-red-900/60"
                  style={{ width: `${w}%` }}
                />
              </div>
              <span className="w-14 text-right text-gray-400">{a.qty.toFixed(1)}</span>
            </div>
          );
        })}
        {/* Mid line */}
        <div className="text-center text-[10px] text-yellow-500 font-bold py-0.5 border-y border-gray-800 my-0.5">
          ── ${data.mid.toFixed(3)} mid ──
        </div>
        {/* Bid walls */}
        {topBids.map((b, i) => {
          const w = (b.qty / maxQty) * 100;
          return (
            <div key={`b${i}`} className="flex items-center text-[10px] font-mono">
              <span className="w-16 text-green-400">${b.price.toFixed(3)}</span>
              <div className="flex-1 h-3 bg-gray-900 relative">
                <div
                  className="absolute inset-y-0 left-0 bg-green-900/60"
                  style={{ width: `${w}%` }}
                />
              </div>
              <span className="w-14 text-right text-gray-400">{b.qty.toFixed(1)}</span>
            </div>
          );
        })}
      </div>

      <div className="text-[9px] text-gray-600 pt-1 border-t border-gray-800">
        Total: {data.total_bid_volume.toFixed(0)} bid · {data.total_ask_volume.toFixed(0)} ask
      </div>
    </Card>
  );
};

// ---------------------------------------------------------------------------
// 2. Whale Trade Feed
// ---------------------------------------------------------------------------

interface WhaleTrade {
  time: number;
  price: number;
  qty: number;
  quote_usd: number;
  side: "BUY" | "SELL";
}

interface WhaleResponse {
  symbol: string;
  min_usd: number;
  count: number;
  buy_volume_usd: number;
  sell_volume_usd: number;
  delta_usd: number;
  trades: WhaleTrade[];
}

const WhaleFeedCard: React.FC = () => {
  const { data } = useApi<WhaleResponse>(
    "/api/whale-trades?limit=1000&min_usd=10000",
    { pollInterval: 10_000 },
  );

  if (!data) {
    return <Card title="Whale Trades"><div className="text-gray-600 text-xs">loading…</div></Card>;
  }

  const delta = data.delta_usd;
  const deltaColor = delta > 0 ? "text-green-400" : delta < 0 ? "text-red-400" : "text-gray-400";

  return (
    <Card title="Whale Trades" subtitle={`${data.symbol} · ≥$${(data.min_usd / 1000).toFixed(0)}K`}>
      <div className="flex gap-3 text-xs">
        <div className="flex-1">
          <div className="text-[10px] text-green-400 uppercase">Buys</div>
          <div className="text-sm font-bold text-green-300">{fmtUsdCompact(data.buy_volume_usd)}</div>
        </div>
        <div className="flex-1">
          <div className="text-[10px] text-red-400 uppercase">Sells</div>
          <div className="text-sm font-bold text-red-300">{fmtUsdCompact(data.sell_volume_usd)}</div>
        </div>
        <div className="flex-1">
          <div className="text-[10px] text-gray-500 uppercase">Delta</div>
          <div className={`text-sm font-bold ${deltaColor}`}>
            {delta >= 0 ? "+" : ""}{fmtUsdCompact(delta)}
          </div>
        </div>
      </div>
      <div className="text-[10px] text-gray-500">{data.count} trades · updates 10s</div>

      <div className="max-h-48 overflow-y-auto pr-1 flex flex-col gap-0.5">
        {data.trades.length === 0 ? (
          <div className="text-[10px] text-gray-600 italic py-2">No whale activity in window</div>
        ) : (
          data.trades.slice(0, 30).map((t, i) => {
            const color = t.side === "BUY" ? "text-green-400" : "text-red-400";
            return (
              <div key={i} className={`flex items-center gap-2 text-[10px] font-mono ${color}`}>
                <span className="w-16 text-gray-600">{fmtHM(t.time)}</span>
                <span className="w-10 font-bold">{t.side}</span>
                <span className="w-16">${t.price.toFixed(3)}</span>
                <span className="ml-auto font-semibold">{fmtUsdCompact(t.quote_usd)}</span>
              </div>
            );
          })
        )}
      </div>
    </Card>
  );
};

// ---------------------------------------------------------------------------
// 3. Volume Profile
// ---------------------------------------------------------------------------

interface VolumeBucket {
  price_lo: number;
  price_hi: number;
  volume: number;
}

interface VolumeProfile {
  symbol: string;
  timeframe: string;
  hours: number;
  total_volume: number;
  poc_price: number | null;
  value_area_low: number;
  value_area_high: number;
  buckets: VolumeBucket[];
}

const VolumeProfileCard: React.FC = () => {
  const { data } = useApi<VolumeProfile>(
    "/api/volume-profile?timeframe=5min&hours=24&buckets=30",
    { pollInterval: 60_000 },
  );

  if (!data || data.buckets.length === 0 || !data.poc_price) {
    return <Card title="Volume Profile"><div className="text-gray-600 text-xs">loading…</div></Card>;
  }

  const maxVol = Math.max(...data.buckets.map(b => b.volume), 1);
  // Sort descending by price for display (high at top)
  const rows = [...data.buckets].sort((a, b) => b.price_lo - a.price_lo);

  return (
    <Card title="Volume Profile" subtitle={`${data.symbol} · ${data.hours}h · ${data.timeframe}`}>
      <div className="flex items-baseline justify-between">
        <div>
          <span className="text-[10px] text-gray-500">POC </span>
          <span className="text-sm font-bold text-yellow-400">${data.poc_price.toFixed(3)}</span>
        </div>
        <div className="text-[10px] text-gray-500">
          Value area ${data.value_area_low.toFixed(2)} – ${data.value_area_high.toFixed(2)}
        </div>
      </div>

      <div className="flex flex-col gap-0 max-h-64 overflow-y-auto pr-1">
        {rows.map((b, i) => {
          const w = (b.volume / maxVol) * 100;
          const inVA = b.price_lo >= data.value_area_low && b.price_hi <= data.value_area_high;
          const isPoc = data.poc_price !== null && b.price_lo <= data.poc_price && b.price_hi >= data.poc_price;
          const barColor = isPoc
            ? "bg-yellow-500/70"
            : inVA
            ? "bg-blue-500/60"
            : "bg-gray-700/60";
          return (
            <div key={i} className="flex items-center text-[9px] font-mono">
              <span className="w-14 text-gray-500 truncate">${b.price_lo.toFixed(2)}</span>
              <div className="flex-1 h-2 bg-gray-900/80">
                <div
                  className={`h-full ${barColor} transition-all duration-200`}
                  style={{ width: `${w}%` }}
                />
              </div>
              <span className="w-10 text-right text-gray-500">
                {b.volume >= 1000 ? `${(b.volume / 1000).toFixed(0)}k` : b.volume.toFixed(0)}
              </span>
            </div>
          );
        })}
      </div>

      <div className="text-[9px] text-gray-600 pt-1 border-t border-gray-800 flex justify-between">
        <span>🟨 POC</span>
        <span>🟦 Value Area (70%)</span>
        <span>Total: {fmtUsdCompact(data.total_volume)}</span>
      </div>
    </Card>
  );
};

// ---------------------------------------------------------------------------
// Main panel — 3-card grid
// ---------------------------------------------------------------------------

const BinanceProPanel: React.FC = () => (
  <div className="mb-6">
    <h2 className="text-xs uppercase tracking-widest text-gray-500 font-medium mb-3">
      Market Microstructure (Binance Pro)
    </h2>
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
      <OrderBookCard />
      <WhaleFeedCard />
      <VolumeProfileCard />
    </div>
  </div>
);

export default BinanceProPanel;
