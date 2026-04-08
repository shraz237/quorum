import React, { useEffect, useRef } from "react";
import {
  createChart,
  IChartApi,
  ISeriesApi,
  CandlestickData,
  ColorType,
  IPriceLine,
} from "lightweight-charts";

export interface OHLCVBar {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number | null;
}

export interface PositionOverlay {
  id: number;
  side: "LONG" | "SHORT";
  entry_price: number;
  stop_loss: number | null;
  take_profit: number | null;
}

export interface SignalOverlay {
  id: number;
  timestamp: string;
  action: string;
}

interface PriceChartProps {
  bars: OHLCVBar[];
  timeframe?: string;
  positions?: PositionOverlay[];
  signals?: SignalOverlay[];
}

/**
 * TradingView Lightweight Charts candlestick chart.
 * Re-renders whenever `bars` reference changes.
 */
const PriceChart: React.FC<PriceChartProps> = ({
  bars,
  timeframe = "1H",
  positions = [],
  signals = [],
}) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const priceLineRefs = useRef<IPriceLine[]>([]);

  // Create chart once
  useEffect(() => {
    if (!containerRef.current) return;

    const chart = createChart(containerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: "#111827" },
        textColor: "#9CA3AF",
      },
      grid: {
        vertLines: { color: "#1F2937" },
        horzLines: { color: "#1F2937" },
      },
      crosshair: { mode: 1 },
      rightPriceScale: { borderColor: "#374151" },
      timeScale: {
        borderColor: "#374151",
        timeVisible: true,
        secondsVisible: true,
      },
      width: containerRef.current.clientWidth,
      height: 380,
    });

    const series = chart.addCandlestickSeries({
      upColor: "#22C55E",
      downColor: "#EF4444",
      borderUpColor: "#22C55E",
      borderDownColor: "#EF4444",
      wickUpColor: "#22C55E",
      wickDownColor: "#EF4444",
    });

    chartRef.current = chart;
    seriesRef.current = series;

    const handleResize = () => {
      if (containerRef.current) {
        chart.applyOptions({ width: containerRef.current.clientWidth });
      }
    };
    window.addEventListener("resize", handleResize);

    return () => {
      window.removeEventListener("resize", handleResize);
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
    };
  }, []);

  // Update data whenever bars change
  useEffect(() => {
    if (!seriesRef.current || bars.length === 0) return;

    const chartData: CandlestickData[] = bars.map((b) => ({
      time: b.time as unknown as CandlestickData["time"],
      open: b.open,
      high: b.high,
      low: b.low,
      close: b.close,
    }));

    seriesRef.current.setData(chartData);
    chartRef.current?.timeScale().fitContent();
  }, [bars]);

  // Update position price lines whenever positions change
  useEffect(() => {
    const series = seriesRef.current;
    if (!series) return;

    // Remove old price lines
    priceLineRefs.current.forEach((pl) => {
      try {
        series.removePriceLine(pl);
      } catch {
        // line may already be gone
      }
    });
    priceLineRefs.current = [];

    // Add new price lines for each position
    positions.forEach((pos) => {
      const entryLine = series.createPriceLine({
        price: pos.entry_price,
        color: "#3B82F6",
        lineWidth: 1,
        lineStyle: 2, // dashed
        axisLabelVisible: true,
        title: `#${pos.id} Entry (${pos.side})`,
      });
      priceLineRefs.current.push(entryLine);

      if (pos.stop_loss != null) {
        const slLine = series.createPriceLine({
          price: pos.stop_loss,
          color: "#EF4444",
          lineWidth: 1,
          lineStyle: 2,
          axisLabelVisible: true,
          title: `#${pos.id} SL`,
        });
        priceLineRefs.current.push(slLine);
      }

      if (pos.take_profit != null) {
        const tpLine = series.createPriceLine({
          price: pos.take_profit,
          color: "#22C55E",
          lineWidth: 1,
          lineStyle: 2,
          axisLabelVisible: true,
          title: `#${pos.id} TP`,
        });
        priceLineRefs.current.push(tpLine);
      }
    });
  }, [positions]);

  // Update signal markers whenever signals or bars change
  useEffect(() => {
    const series = seriesRef.current;
    if (!series || bars.length === 0) return;

    const markers = signals
      .filter((s) => {
        const t = Math.floor(new Date(s.timestamp).getTime() / 1000);
        return bars.some((b) => b.time === t);
      })
      .map((s) => {
        const t = Math.floor(new Date(s.timestamp).getTime() / 1000);
        const upper = s.action.toUpperCase();
        return {
          time: t as unknown as CandlestickData["time"],
          position: upper === "BUY" ? ("belowBar" as const) : ("aboveBar" as const),
          color: upper === "BUY" ? "#22C55E" : upper === "SELL" ? "#EF4444" : "#FACC15",
          shape: upper === "BUY" ? ("arrowUp" as const) : ("arrowDown" as const),
          text: upper,
          size: 1,
        };
      })
      .sort((a, b) => {
        const ta = typeof a.time === "number" ? a.time : 0;
        const tb = typeof b.time === "number" ? b.time : 0;
        return ta - tb;
      });

    series.setMarkers(markers);
  }, [signals, bars]);

  return (
    <div className="bg-gray-900 rounded-xl p-4">
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-sm font-semibold text-gray-300">
          WTI Crude (CL=F) — {timeframe}
        </h2>
        {bars.length > 0 && (
          <span className="text-xs text-gray-500">
            {bars.length} candles
          </span>
        )}
      </div>
      <div ref={containerRef} className="w-full" />
      {bars.length === 0 && (
        <div className="h-96 flex items-center justify-center text-gray-500 text-sm">
          No price data available
        </div>
      )}
    </div>
  );
};

export default PriceChart;
