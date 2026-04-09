/**
 * LivePriceTicker — WTI/USD from Twelve Data via /api/ticker poll.
 *
 * Data flow:
 *   backend plugin_live_ticker polls Twelve Data /quote every 3 sec
 *   → frontend polls /api/ticker every 1.5 sec (cheap, cached)
 *   → display tick-colour-coded price against previous value
 *
 * Previously this widget connected directly to Binance WebSocket. We
 * switched because Binance CLUSDT (TRADIFI perpetual) drifts 1-3%
 * from real NYMEX WTI during low-liquidity hours, which is the whole
 * point of paying for Twelve Data.
 */

import React, { useEffect, useRef, useState } from "react";

interface TickerState {
  symbol: string;
  price: number | null;
  change_pct: number | null;
  high_24h: number | null;
  low_24h: number | null;
  is_market_open: boolean | null;
  direction: "up" | "down" | "flat";
  last_quote_at: number | null;
  updated_at: string | null;
  poll_count: number;
  error: string | null;
}

const LivePriceTicker: React.FC = () => {
  const [ticker, setTicker] = useState<TickerState | null>(null);
  const [flash, setFlash] = useState<"up" | "down" | null>(null);
  const lastSeenPrice = useRef<number | null>(null);

  useEffect(() => {
    let cancelled = false;

    const poll = async () => {
      try {
        const res = await fetch("/api/ticker");
        if (!res.ok) return;
        const json = await res.json();
        const data = json.data as TickerState | undefined;
        if (!data || cancelled) return;

        if (data.price != null) {
          if (lastSeenPrice.current != null && data.price !== lastSeenPrice.current) {
            setFlash(data.price > lastSeenPrice.current ? "up" : "down");
            // Clear flash after short pulse
            setTimeout(() => setFlash(null), 400);
          }
          lastSeenPrice.current = data.price;
        }
        setTicker(data);
      } catch {
        // Network blip — try again next tick
      }
    };

    void poll();
    const id = setInterval(poll, 1500);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  if (!ticker || ticker.price == null) {
    return (
      <div className="flex items-center gap-3 px-4 py-1.5 bg-gray-900 border border-gray-800 rounded-lg">
        <div className="w-2 h-2 rounded-full bg-gray-600 animate-pulse" />
        <span className="text-[10px] uppercase tracking-widest text-gray-500 font-medium">
          CONNECTING
        </span>
      </div>
    );
  }

  const direction = ticker.direction;
  const connected = ticker.error == null;

  // Flash the whole card briefly on every tick move
  const flashBg =
    flash === "up"
      ? "bg-green-900/30"
      : flash === "down"
      ? "bg-red-900/30"
      : "bg-gray-900";

  const priceColor =
    direction === "up"
      ? "text-green-400"
      : direction === "down"
      ? "text-red-400"
      : "text-gray-100";

  const changeColor =
    ticker.change_pct != null && ticker.change_pct >= 0 ? "text-green-400" : "text-red-400";

  return (
    <div
      className={`flex items-center gap-4 px-4 py-1.5 border border-gray-800 rounded-lg transition-colors duration-300 ${flashBg}`}
    >
      {/* Live indicator */}
      <div className="flex items-center gap-1.5">
        <div
          className={`w-2 h-2 rounded-full ${
            connected ? "bg-green-400 animate-pulse" : "bg-red-600"
          }`}
        />
        <span className="text-[9px] uppercase tracking-widest text-gray-500 font-medium">
          {connected ? (ticker.is_market_open ? "LIVE" : "CLOSED") : "ERROR"}
        </span>
      </div>

      {/* Symbol */}
      <span className="text-[10px] text-gray-500 uppercase tracking-widest font-semibold">
        {ticker.symbol}
      </span>

      {/* Price */}
      <div className="flex items-baseline gap-1">
        <span
          className={`text-2xl font-bold font-mono tabular-nums leading-none ${priceColor} transition-colors`}
        >
          ${ticker.price.toFixed(3)}
        </span>
        <span className={`text-sm ${priceColor}`}>
          {direction === "up" ? "▲" : direction === "down" ? "▼" : ""}
        </span>
      </div>

      {/* 24h change % */}
      {ticker.change_pct != null && (
        <div className="flex flex-col items-end">
          <span className={`text-sm font-semibold ${changeColor}`}>
            {ticker.change_pct >= 0 ? "+" : ""}
            {ticker.change_pct.toFixed(2)}%
          </span>
          <span className="text-[9px] text-gray-600">24h</span>
        </div>
      )}

      {/* 24h range */}
      {ticker.high_24h != null && ticker.low_24h != null && (
        <div className="hidden lg:flex flex-col items-end text-[10px] font-mono">
          <span className="text-gray-400">H ${ticker.high_24h.toFixed(2)}</span>
          <span className="text-gray-500">L ${ticker.low_24h.toFixed(2)}</span>
        </div>
      )}

      {/* Source badge */}
      <div className="hidden lg:flex flex-col items-end text-[9px] font-mono text-gray-600">
        <span>Twelve Data</span>
        <span>{ticker.poll_count.toLocaleString()} polls</span>
      </div>
    </div>
  );
};

export default LivePriceTicker;
