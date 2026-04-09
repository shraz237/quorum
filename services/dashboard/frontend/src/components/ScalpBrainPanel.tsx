/**
 * ScalpBrainPanel — the "ultimate scalper" one-panel verdict.
 *
 * Reads /api/scalp-brain which stitches multi-TF RSI, VWAP bands, session
 * range position, opening-range breakout, CVD, orderbook imbalance,
 * whale bias, conviction trend, cross-asset stress, session regime and
 * BBands squeeze into a single LONG NOW / SHORT NOW / LEAN / WAIT
 * verdict with deterministic entry/SL/TP1/TP2/R:R from ATR.
 *
 * The panel is designed to be glanceable: big colored verdict at the
 * top, entry levels in the middle, signal grid + gatekeepers at the
 * bottom.
 */

import React from "react";
import useApi from "../hooks/useApi";

type Verdict = "LONG" | "SHORT" | "LEAN_LONG" | "LEAN_SHORT" | "WAIT";
type Bias = "bullish" | "bearish" | "neutral";

interface SignalEntry {
  bias: Bias;
  weight: number;
  detail: Record<string, unknown> & { reason?: string };
}

interface Gatekeeper {
  ok: boolean;
  message: string;
}

interface TradeLevels {
  entry: number;
  stop_loss: number;
  take_profit_1: number;
  take_profit_2: number;
  rr_tp1: number;
  rr_tp2: number;
  risk_per_contract: number;
}

interface ScalpBrain {
  generated_at: string;
  current_price: number;
  verdict: Verdict;
  preliminary_verdict: Verdict;
  downgrade_reason: string | null;
  intended_side: "LONG" | "SHORT" | null;
  conviction_pct: number;
  bias_pct: number;
  long_pct: number;
  short_pct: number;
  atr_5m: number;
  structural: { atr: number; session_high: number; session_low: number };
  signals: Record<string, SignalEntry>;
  gatekeepers: Record<string, Gatekeeper>;
  gates_passed: number;
  gates_total: number;
  trade_levels: TradeLevels | null;
  why: string;
  cache_age_seconds?: number;
  error?: string;
}

const SIGNAL_LABELS: Record<string, string> = {
  multi_tf_rsi: "Multi-TF RSI",
  vwap_bands: "VWAP bands",
  session_range_pos: "Session range",
  opening_range_bo: "Opening-range BO",
  cvd: "CVD flow",
  orderbook_imbalance: "Book imbalance",
  whale_bias: "Whale bias",
  conviction_trend: "Conviction",
  cross_asset_stress: "Cross-asset",
  session_regime: "Session regime",
  bbands_squeeze: "BB squeeze",
};

function verdictStyle(v: Verdict): { bg: string; text: string; border: string; label: string } {
  switch (v) {
    case "LONG":
      return { bg: "bg-green-600", text: "text-white", border: "border-green-400", label: "LONG NOW" };
    case "SHORT":
      return { bg: "bg-red-600", text: "text-white", border: "border-red-400", label: "SHORT NOW" };
    case "LEAN_LONG":
      return { bg: "bg-green-900/60", text: "text-green-200", border: "border-green-700", label: "LEAN LONG" };
    case "LEAN_SHORT":
      return { bg: "bg-red-900/60", text: "text-red-200", border: "border-red-700", label: "LEAN SHORT" };
    default:
      return { bg: "bg-gray-800", text: "text-gray-400", border: "border-gray-700", label: "WAIT" };
  }
}

function biasDot(bias: Bias, intendedSide: "LONG" | "SHORT" | null): string {
  if (bias === "neutral") return "bg-gray-600";
  if (intendedSide === null) {
    return bias === "bullish" ? "bg-green-400" : "bg-red-400";
  }
  const matches =
    (intendedSide === "LONG" && bias === "bullish") ||
    (intendedSide === "SHORT" && bias === "bearish");
  return matches ? "bg-green-400" : "bg-red-400";
}

const ScalpBrainPanel: React.FC = () => {
  const { data } = useApi<ScalpBrain>("/api/scalp-brain", { pollInterval: 5_000 });

  if (!data) {
    return (
      <div className="mb-6 bg-gray-900 border border-gray-800 rounded-xl p-4 animate-pulse h-56" />
    );
  }
  if (data.error) {
    return (
      <div className="mb-6 bg-gray-900 border border-red-900 rounded-xl p-4 text-red-400 text-xs">
        Scalp Brain error: {data.error}
      </div>
    );
  }

  const style = verdictStyle(data.verdict);
  const levels = data.trade_levels;

  return (
    <div className="mb-6">
      <h2 className="text-xs uppercase tracking-widest text-gray-500 font-medium mb-3 flex items-center gap-2">
        <span>Scalp Brain</span>
        <span className="text-[9px] text-gray-600">ultimate scalper verdict</span>
      </h2>
      <div className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
        {/* Verdict banner */}
        <div className={`${style.bg} ${style.text} px-5 py-4 flex items-center justify-between border-b-2 ${style.border}`}>
          <div>
            <div className="text-[10px] uppercase tracking-widest opacity-70">Verdict</div>
            <div className="text-3xl font-black tracking-tight leading-tight">{style.label}</div>
          </div>
          <div className="text-right">
            <div className="text-[10px] uppercase tracking-widest opacity-70">Conviction</div>
            <div className="text-3xl font-black tracking-tight leading-tight">
              {Math.round(data.conviction_pct)}%
            </div>
            <div className="text-[10px] opacity-70 mt-0.5">
              L {Math.round(data.long_pct * 100)}% · S {Math.round(data.short_pct * 100)}%
            </div>
          </div>
        </div>

        {/* Trade levels (only when a side is intended) */}
        {levels && data.intended_side && (
          <div className="px-5 py-3 bg-gray-950/40 border-b border-gray-800">
            <div className="grid grid-cols-5 gap-3 text-[11px] font-mono">
              <div>
                <div className="text-gray-500 uppercase text-[9px]">Entry</div>
                <div className="text-gray-100 font-bold">${levels.entry.toFixed(3)}</div>
              </div>
              <div>
                <div className="text-gray-500 uppercase text-[9px]">Stop-loss</div>
                <div className="text-red-400 font-bold">${levels.stop_loss.toFixed(3)}</div>
              </div>
              <div>
                <div className="text-gray-500 uppercase text-[9px]">TP1</div>
                <div className="text-emerald-400 font-bold">${levels.take_profit_1.toFixed(3)}</div>
              </div>
              <div>
                <div className="text-gray-500 uppercase text-[9px]">TP2</div>
                <div className="text-emerald-400 font-bold">${levels.take_profit_2.toFixed(3)}</div>
              </div>
              <div>
                <div className="text-gray-500 uppercase text-[9px]">R:R</div>
                <div className="text-gray-100 font-bold">
                  {levels.rr_tp1.toFixed(2)} / {levels.rr_tp2.toFixed(2)}
                </div>
              </div>
            </div>
          </div>
        )}

        {/* Signal grid */}
        <div className="px-5 py-3 border-b border-gray-800">
          <div className="grid grid-cols-2 md:grid-cols-4 gap-x-4 gap-y-1.5 text-[10px]">
            {Object.entries(data.signals).map(([key, sig]) => (
              <div key={key} className="flex items-center gap-2">
                <span className={`w-2 h-2 rounded-full ${biasDot(sig.bias, data.intended_side)}`} />
                <span className="text-gray-300 font-medium truncate">
                  {SIGNAL_LABELS[key] ?? key}
                </span>
                <span className="text-gray-600 ml-auto">w{sig.weight}</span>
              </div>
            ))}
          </div>
        </div>

        {/* Gatekeepers */}
        <div className="px-5 py-2 border-b border-gray-800">
          <div className="flex items-center gap-3 text-[10px] font-mono flex-wrap">
            <span className="text-gray-500 uppercase tracking-wider">Gatekeepers</span>
            {Object.entries(data.gatekeepers).map(([key, gate]) => (
              <span
                key={key}
                className={gate.ok ? "text-green-300" : "text-red-300"}
                title={gate.message}
              >
                {gate.ok ? "✅" : "❌"} {key} {gate.message ? `(${gate.message})` : ""}
              </span>
            ))}
            <span className="text-gray-500 ml-auto">
              {data.gates_passed}/{data.gates_total} passing
            </span>
          </div>
        </div>

        {/* Why */}
        <div className="px-5 py-3">
          <div className="text-[10px] text-gray-500 uppercase tracking-wider mb-1">Why</div>
          <div className="text-xs text-gray-300 leading-relaxed">{data.why}</div>
          {data.downgrade_reason && (
            <div className="text-[10px] text-yellow-400 mt-1">⚠ {data.downgrade_reason}</div>
          )}
        </div>

        {/* Footer */}
        <div className="px-5 py-1 bg-gray-950/40 flex items-center justify-between text-[9px] text-gray-600 font-mono">
          <span>ATR 5m ${data.atr_5m.toFixed(3)} · session {data.structural.session_low.toFixed(2)}-{data.structural.session_high.toFixed(2)}</span>
          <span>cached {data.cache_age_seconds ?? 0}s · {new Date(data.generated_at).toLocaleTimeString()}</span>
        </div>
      </div>
    </div>
  );
};

export default ScalpBrainPanel;
