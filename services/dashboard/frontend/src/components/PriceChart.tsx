import React, { useEffect, useRef } from "react";
import {
  createChart,
  IChartApi,
  ISeriesApi,
  CandlestickData,
  ColorType,
} from "lightweight-charts";

export interface OHLCVBar {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number | null;
}

interface PriceChartProps {
  bars: OHLCVBar[];
  timeframe?: string;
}

/**
 * TradingView Lightweight Charts candlestick chart.
 * Re-renders whenever `bars` reference changes.
 */
const PriceChart: React.FC<PriceChartProps> = ({ bars, timeframe = "1H" }) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);

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
        secondsVisible: false,
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

  return (
    <div className="bg-gray-900 rounded-xl p-4">
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-sm font-semibold text-gray-300">
          Brent Crude — {timeframe}
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
