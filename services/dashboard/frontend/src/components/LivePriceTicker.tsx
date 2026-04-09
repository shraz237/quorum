/**
 * LivePriceTicker — real-time CLUSDT price via Binance aggTrade WebSocket.
 *
 * Subscribes directly from the browser to wss://fstream.binance.com/ws/
 * clusdt@aggTrade — Binance public market data streams don't need auth,
 * so we can skip any backend proxy and get tick-by-tick updates at
 * ~1-5 Hz directly from the exchange.
 *
 * Displays:
 *   - current last price (big, colour-coded by direction of last change)
 *   - 24h change % (pulled from /api/cross-assets or similar — here we use
 *     Binance 24hr ticker REST endpoint for simplicity, polled every 10s)
 *   - small flashing dot indicating the connection is live
 */

import React, { useEffect, useRef, useState } from "react";

const WS_URL = "wss://fstream.binance.com/ws/clusdt@aggTrade";
const TICKER_URL = "https://fapi.binance.com/fapi/v1/ticker/24hr?symbol=CLUSDT";

interface TickerStats {
  priceChangePercent: number;
  highPrice: number;
  lowPrice: number;
  volume: number;
}

const LivePriceTicker: React.FC = () => {
  const [price, setPrice] = useState<number | null>(null);
  const [prevPrice, setPrevPrice] = useState<number | null>(null);
  const [stats, setStats] = useState<TickerStats | null>(null);
  const [connected, setConnected] = useState<boolean>(false);
  const [tickCount, setTickCount] = useState<number>(0);

  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimeout = useRef<number | null>(null);

  // Poll 24h ticker stats every 10s from Binance REST
  useEffect(() => {
    let cancelled = false;
    const fetchStats = async () => {
      try {
        const res = await fetch(TICKER_URL);
        if (!res.ok) return;
        const j = await res.json();
        if (cancelled) return;
        setStats({
          priceChangePercent: parseFloat(j.priceChangePercent),
          highPrice: parseFloat(j.highPrice),
          lowPrice: parseFloat(j.lowPrice),
          volume: parseFloat(j.volume),
        });
      } catch {
        // ignore
      }
    };
    void fetchStats();
    const id = setInterval(fetchStats, 10_000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  // WebSocket connection for tick-by-tick price
  useEffect(() => {
    let closed = false;

    const connect = () => {
      if (closed) return;
      try {
        const ws = new WebSocket(WS_URL);
        wsRef.current = ws;

        ws.onopen = () => {
          if (closed) {
            ws.close();
            return;
          }
          setConnected(true);
        };

        ws.onmessage = (ev) => {
          try {
            const msg = JSON.parse(ev.data);
            if (msg.e === "aggTrade" && msg.p) {
              const p = parseFloat(msg.p);
              setPrice((cur) => {
                if (cur !== null) setPrevPrice(cur);
                return p;
              });
              setTickCount((c) => c + 1);
            }
          } catch {
            // ignore malformed frame
          }
        };

        ws.onclose = () => {
          setConnected(false);
          if (closed) return;
          // Reconnect after 2s
          reconnectTimeout.current = window.setTimeout(connect, 2000);
        };

        ws.onerror = () => {
          try {
            ws.close();
          } catch {
            // ignore
          }
        };
      } catch {
        if (!closed) {
          reconnectTimeout.current = window.setTimeout(connect, 2000);
        }
      }
    };

    connect();

    return () => {
      closed = true;
      if (reconnectTimeout.current !== null) {
        clearTimeout(reconnectTimeout.current);
      }
      if (wsRef.current) {
        try {
          wsRef.current.close();
        } catch {
          // ignore
        }
      }
    };
  }, []);

  const direction =
    price !== null && prevPrice !== null
      ? price > prevPrice
        ? "up"
        : price < prevPrice
        ? "down"
        : "flat"
      : "flat";

  const priceColor =
    direction === "up" ? "text-green-400" : direction === "down" ? "text-red-400" : "text-gray-100";

  const changeColor =
    stats && stats.priceChangePercent >= 0 ? "text-green-400" : "text-red-400";

  return (
    <div className="flex items-center gap-4 px-4 py-1.5 bg-gray-900 border border-gray-800 rounded-lg">
      {/* Live indicator */}
      <div className="flex items-center gap-1.5">
        <div
          className={`w-2 h-2 rounded-full ${
            connected ? "bg-green-400 animate-pulse" : "bg-gray-600"
          }`}
        />
        <span className="text-[9px] uppercase tracking-widest text-gray-500 font-medium">
          {connected ? "LIVE" : "OFFLINE"}
        </span>
      </div>

      {/* Symbol */}
      <span className="text-[10px] text-gray-500 uppercase tracking-widest font-semibold">
        CLUSDT
      </span>

      {/* Price — big, colour-coded */}
      <div className="flex items-baseline gap-1">
        <span className={`text-2xl font-bold font-mono tabular-nums leading-none ${priceColor} transition-colors`}>
          ${price !== null ? price.toFixed(3) : "—"}
        </span>
        <span className={`text-sm ${priceColor}`}>
          {direction === "up" ? "▲" : direction === "down" ? "▼" : ""}
        </span>
      </div>

      {/* 24h change */}
      {stats && (
        <>
          <div className="flex flex-col items-end">
            <span className={`text-sm font-semibold ${changeColor}`}>
              {stats.priceChangePercent >= 0 ? "+" : ""}
              {stats.priceChangePercent.toFixed(2)}%
            </span>
            <span className="text-[9px] text-gray-600">24h</span>
          </div>

          {/* 24h range */}
          <div className="hidden lg:flex flex-col items-end text-[10px] font-mono">
            <span className="text-gray-400">
              H ${stats.highPrice.toFixed(2)}
            </span>
            <span className="text-gray-500">
              L ${stats.lowPrice.toFixed(2)}
            </span>
          </div>
        </>
      )}

      {/* Tick counter — shows how active the feed is */}
      <div className="hidden lg:flex flex-col items-end text-[9px] font-mono text-gray-600">
        <span>{tickCount.toLocaleString()} ticks</span>
        <span>since open</span>
      </div>
    </div>
  );
};

export default LivePriceTicker;
