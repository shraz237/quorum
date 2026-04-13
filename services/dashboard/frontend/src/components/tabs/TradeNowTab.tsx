/**
 * TradeNowTab — the zero-scroll scalp cockpit.
 *
 * Ordered top to bottom for a 3-second glance:
 *   1. SynthesisPanel (NowBrief headline + confluence + anomalies)
 *   2. ScalpBrainPanel (LONG NOW / SHORT NOW verdict + levels)
 *   3. ScalpingRangePanel (5-min range + realtime 30m + per-side setups)
 *   4. PriceChart (compact, with position + signal overlays)
 */

import React from "react";
import useApi from "../../hooks/useApi";
import SynthesisPanel from "../SynthesisPanel";
import MainBrainPanel from "../MainBrainPanel";
import ScalpBrainPanel from "../ScalpBrainPanel";
import ScalpingRangePanel from "../ScalpingRangePanel";
import PriceChart, { OHLCVBar, PositionOverlay, SignalOverlay, LiveTick } from "../PriceChart";

interface TickerData {
  price: number | null;
  last_quote_at: number | null;
}

interface Props {
  timeframe: string;
  setTimeframe: (tf: string) => void;
  ohlcv: OHLCVBar[];
  ohlcvLoading: boolean;
  positionOverlays: PositionOverlay[];
  signalOverlays: SignalOverlay[];
}

const TIMEFRAMES = ["1min", "5min", "15min", "1H", "1D", "1W"];

const TradeNowTab: React.FC<Props> = ({
  timeframe,
  setTimeframe,
  ohlcv,
  ohlcvLoading,
  positionOverlays,
  signalOverlays,
}) => {
  // Live ticker — polls Twelve Data /quote every 3s via the backend cache.
  // The price is merged into the last candle of the chart so it "breathes"
  // between the slower DB-backed OHLCV polls (which only update once per
  // minute when the data-collector runs). This is how TradingView does it.
  const { data: ticker } = useApi<TickerData>("/api/ticker", {
    pollInterval: 3_000,
  });

  const liveTick: LiveTick | null =
    ticker?.price != null
      ? { price: ticker.price, timestamp: ticker.last_quote_at ?? undefined }
      : null;

  const refreshHint =
    timeframe === "1min" ? "live"
    : timeframe === "5min" ? "10s"
    : timeframe === "15min" ? "30s"
    : "1-5min";

  return (
    <>
      <SynthesisPanel />
      <MainBrainPanel />
      <ScalpBrainPanel />
      <ScalpingRangePanel />
      <section className="mb-6">
        <div className="flex items-center gap-1 mb-2">
          {TIMEFRAMES.map((tf) => (
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
            Refreshing every {refreshHint}
          </span>
        </div>
        {ohlcvLoading && ohlcv.length === 0 ? (
          <div className="bg-gray-900 rounded-xl p-4 h-40 flex items-center justify-center text-gray-600 text-sm">
            Loading chart…
          </div>
        ) : (
          <PriceChart
            key={timeframe}
            bars={ohlcv}
            timeframe={timeframe}
            positions={positionOverlays}
            signals={signalOverlays}
            liveTick={liveTick}
          />
        )}
      </section>
    </>
  );
};

export default TradeNowTab;
