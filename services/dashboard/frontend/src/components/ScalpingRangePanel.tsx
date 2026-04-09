/**
 * ScalpingRangePanel — short-timeframe intraday scalping levels.
 *
 * Reads /api/scalping-range which computes a 5-minute range analysis
 * with suggested LONG and SHORT entries (entry/SL/TP1/TP2), current
 * zone (lower/middle/upper), volatility regime, VWAP bias, and a
 * recommended side. Refreshes every 10 seconds.
 */

import React from "react";
import useApi from "../hooks/useApi";

interface Setup {
  entry: number;
  stop_loss: number;
  take_profit_1: number;
  take_profit_2: number;
  rr_tp1: number | null;
  rr_tp2: number | null;
  distance_from_current_pct: number;
}

interface ScalpingRange {
  symbol: string;
  timeframe: string;
  lookback_hours: number;
  bar_count: number;
  current_price: number;
  range: {
    low: number;
    mid: number;
    high: number;
    width: number;
    width_pct: number;
  };
  atr_5m: number;
  atr_pct: number;
  volatility_regime: "tight" | "normal" | "wide" | "unknown";
  vwap: number | null;
  vwap_bias: string | null;
  zone: "lower" | "middle" | "upper";
  prefer_side: "LONG" | "SHORT" | "WAIT" | "CAUTION";
  prefer_reason: string;
  long_setup: Setup;
  short_setup: Setup;
  funding_rate_pct: number | null;
  active_anomalies: number;
  warnings: string[];
  generated_at: string;
  error?: string;
}

function sideColor(side: string): { bg: string; text: string; border: string } {
  switch (side) {
    case "LONG":
      return { bg: "bg-green-950/60", text: "text-green-300", border: "border-green-800" };
    case "SHORT":
      return { bg: "bg-red-950/60", text: "text-red-300", border: "border-red-800" };
    case "WAIT":
      return { bg: "bg-gray-900", text: "text-gray-400", border: "border-gray-800" };
    default:
      return { bg: "bg-yellow-950/40", text: "text-yellow-300", border: "border-yellow-800" };
  }
}

function volColor(regime: string): string {
  if (regime === "wide") return "text-red-400";
  if (regime === "normal") return "text-emerald-400";
  if (regime === "tight") return "text-yellow-400";
  return "text-gray-500";
}

const SetupCard: React.FC<{
  side: "LONG" | "SHORT";
  setup: Setup;
  preferred: boolean;
}> = ({ side, setup, preferred }) => {
  const tint = side === "LONG" ? "text-green-300 border-green-900/60" : "text-red-300 border-red-900/60";
  const entryColor = side === "LONG" ? "text-green-400" : "text-red-400";
  const ring = preferred ? "ring-2 ring-blue-500/40" : "";

  return (
    <div className={`border rounded p-2 ${tint} ${ring}`}>
      <div className="flex items-center justify-between mb-1">
        <span className="text-xs font-bold">{side} setup</span>
        {preferred && <span className="text-[9px] text-blue-300 font-semibold">★ PREFERRED</span>}
      </div>
      <div className="grid grid-cols-2 gap-x-3 gap-y-0.5 text-[10px] font-mono">
        <span className="text-gray-500">Entry</span>
        <span className={`text-right font-bold ${entryColor}`}>${setup.entry.toFixed(3)}</span>

        <span className="text-gray-500">Stop-loss</span>
        <span className="text-right text-red-400">${setup.stop_loss.toFixed(3)}</span>

        <span className="text-gray-500">TP1 (mid)</span>
        <span className="text-right text-emerald-400">${setup.take_profit_1.toFixed(3)}</span>

        <span className="text-gray-500">TP2 (full)</span>
        <span className="text-right text-emerald-400">${setup.take_profit_2.toFixed(3)}</span>

        <span className="text-gray-500">R:R (TP1 / TP2)</span>
        <span className="text-right text-gray-300">
          {setup.rr_tp1 != null ? `1:${setup.rr_tp1.toFixed(2)}` : "—"} / {setup.rr_tp2 != null ? `1:${setup.rr_tp2.toFixed(2)}` : "—"}
        </span>

        <span className="text-gray-500">Distance now</span>
        <span className={`text-right ${Math.abs(setup.distance_from_current_pct) < 0.1 ? "text-yellow-400 font-bold" : "text-gray-300"}`}>
          {setup.distance_from_current_pct >= 0 ? "+" : ""}{setup.distance_from_current_pct.toFixed(3)}%
        </span>
      </div>
    </div>
  );
};

const ScalpingRangePanel: React.FC = () => {
  const { data } = useApi<ScalpingRange>("/api/scalping-range?timeframe=5min&lookback_hours=4", {
    pollInterval: 10_000,
  });

  if (!data) {
    return (
      <div className="mb-6 bg-gray-900 border border-gray-800 rounded-xl p-4 animate-pulse h-48" />
    );
  }
  if (data.error) {
    return (
      <div className="mb-6 bg-gray-900 border border-red-900 rounded-xl p-4 text-red-400 text-xs">
        Scalping analyzer error: {data.error}
      </div>
    );
  }

  const pref = sideColor(data.prefer_side);

  // Range visualisation — bar with current price marker
  const rangePct = (p: number) =>
    Math.max(0, Math.min(100, ((p - data.range.low) / (data.range.high - data.range.low)) * 100));
  const currentMarker = rangePct(data.current_price);
  const vwapMarker = data.vwap != null ? rangePct(data.vwap) : null;

  return (
    <div className="mb-6">
      <h2 className="text-xs uppercase tracking-widest text-gray-500 font-medium mb-3">
        Scalping Range (5-min)
      </h2>
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-4 flex flex-col gap-3">
        {/* Top row: preferred side + meta */}
        <div className="flex items-start justify-between gap-3">
          <div className={`flex-1 border rounded px-3 py-2 ${pref.bg} ${pref.border}`}>
            <div className="text-[10px] text-gray-500 uppercase">Recommended action</div>
            <div className={`text-2xl font-bold ${pref.text}`}>{data.prefer_side}</div>
            <div className="text-[10px] text-gray-400 mt-0.5">{data.prefer_reason}</div>
          </div>
          <div className="grid grid-cols-2 gap-2 text-[10px] font-mono min-w-[200px]">
            <div className="bg-gray-800/60 rounded px-2 py-1">
              <div className="text-gray-500 uppercase">ATR 5m</div>
              <div className="text-gray-100 font-bold">${data.atr_5m.toFixed(3)}</div>
              <div className={volColor(data.volatility_regime)}>{data.atr_pct.toFixed(2)}% · {data.volatility_regime}</div>
            </div>
            <div className="bg-gray-800/60 rounded px-2 py-1">
              <div className="text-gray-500 uppercase">VWAP 4h</div>
              <div className="text-gray-100 font-bold">${data.vwap?.toFixed(3) ?? "—"}</div>
              <div className="text-gray-400">{data.vwap_bias?.replace(/_/g, " ") ?? "—"}</div>
            </div>
          </div>
        </div>

        {/* Range bar */}
        <div>
          <div className="flex justify-between text-[10px] font-mono text-gray-500 mb-1">
            <span>LOW ${data.range.low.toFixed(3)}</span>
            <span>MID ${data.range.mid.toFixed(3)}</span>
            <span>HIGH ${data.range.high.toFixed(3)}</span>
          </div>
          <div className="relative h-6 bg-gray-800 rounded overflow-hidden">
            {/* Zone bands */}
            <div className="absolute inset-y-0 left-0 w-1/3 bg-green-900/30" />
            <div className="absolute inset-y-0 left-1/3 w-1/3 bg-yellow-900/20" />
            <div className="absolute inset-y-0 right-0 w-1/3 bg-red-900/30" />
            {/* Mid line */}
            <div className="absolute inset-y-0 left-1/2 w-px bg-gray-600" />
            {/* VWAP marker */}
            {vwapMarker !== null && (
              <div
                className="absolute top-0 bottom-0 w-0.5 bg-blue-400"
                style={{ left: `${vwapMarker}%` }}
                title={`VWAP $${data.vwap?.toFixed(3)}`}
              />
            )}
            {/* Current price marker */}
            <div
              className="absolute top-0 bottom-0 w-1 bg-yellow-300 shadow-lg"
              style={{ left: `${currentMarker}%` }}
              title={`Current $${data.current_price.toFixed(3)}`}
            />
          </div>
          <div className="flex justify-between text-[9px] text-gray-600 mt-0.5">
            <span>lower zone</span>
            <span>middle</span>
            <span>upper zone</span>
          </div>
          <div className="flex items-center justify-between text-[10px] mt-1">
            <span className="text-yellow-300 font-bold">● Current ${data.current_price.toFixed(3)}</span>
            <span className="text-gray-500">
              in <span className="text-gray-300 font-semibold">{data.zone.toUpperCase()}</span> zone ·
              range {data.range.width_pct.toFixed(2)}% wide
            </span>
          </div>
        </div>

        {/* Both setups side by side */}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <SetupCard side="LONG" setup={data.long_setup} preferred={data.prefer_side === "LONG"} />
          <SetupCard side="SHORT" setup={data.short_setup} preferred={data.prefer_side === "SHORT"} />
        </div>

        {/* Warnings */}
        {data.warnings.length > 0 && (
          <div className="bg-orange-950/40 border border-orange-900/60 rounded px-2 py-1.5">
            <div className="text-[10px] text-orange-400 uppercase font-semibold mb-0.5">Warnings</div>
            <ul className="text-[10px] text-orange-200 space-y-0.5">
              {data.warnings.map((w, i) => (
                <li key={i}>⚠ {w}</li>
              ))}
            </ul>
          </div>
        )}

        {/* Footer meta */}
        <div className="text-[9px] text-gray-600 flex justify-between">
          <span>{data.symbol} · {data.timeframe} · {data.bar_count} bars over {data.lookback_hours}h</span>
          <span>funding {data.funding_rate_pct?.toFixed(4) ?? "—"}% · {data.active_anomalies} active anomalies</span>
        </div>
      </div>
    </div>
  );
};

export default ScalpingRangePanel;
