/**
 * MainBrainPanel — what is the main trader thinking right now?
 *
 * Same concept as ScalpBrainPanel but for the main persona:
 * shows the latest Opus recommendation, entry gate status,
 * range bias, scores, and if managing an open campaign — the
 * last heartbeat decision.
 */

import React from "react";
import useApi from "../hooks/useApi";

type Verdict = "BUY" | "SELL" | "HOLD" | "WAIT" | "MANAGING" | "BLOCKED";

interface Gate {
  name: string;
  ok: boolean;
  detail: string;
  position_pct?: number;
  bias?: string;
}

interface Recommendation {
  action: string;
  confidence: number;
  unified_score: number | null;
  analysis_text: string;
  base_scenario: string | null;
  alt_scenario: string | null;
  risk_factors: string | string[] | null;
  entry_price: number | null;
  stop_loss: number | null;
  take_profit: number | null;
  timestamp: string | null;
}

interface Scores {
  unified: number | null;
  technical: number | null;
  fundamental: number | null;
  sentiment: number | null;
  shipping: number | null;
}

interface Heartbeat {
  decision: string;
  reason: string;
  campaign_id: number | null;
  ran_at: string | null;
}

interface MainBrainData {
  generated_at: string;
  current_price: number | null;
  verdict: Verdict;
  verdict_detail: string;
  recommendation: Recommendation | null;
  scores: Scores | null;
  gates: Gate[];
  gates_passed: number;
  gates_total: number;
  has_open_campaign: boolean;
  open_campaigns_count: number;
  last_heartbeat: Heartbeat | null;
  account: { equity: number | null; drawdown_pct: number | null; free_margin: number | null };
  cache_age_seconds?: number;
}

function verdictStyle(v: Verdict): { bg: string; text: string; border: string; label: string } {
  switch (v) {
    case "BUY":
      return { bg: "bg-green-600", text: "text-white", border: "border-green-400", label: "WANTS TO BUY" };
    case "SELL":
      return { bg: "bg-red-600", text: "text-white", border: "border-red-400", label: "WANTS TO SELL" };
    case "MANAGING":
      return { bg: "bg-blue-600", text: "text-white", border: "border-blue-400", label: "MANAGING POSITION" };
    case "BLOCKED":
      return { bg: "bg-amber-700", text: "text-white", border: "border-amber-400", label: "BLOCKED" };
    case "HOLD":
      return { bg: "bg-gray-700", text: "text-gray-200", border: "border-gray-600", label: "HOLD" };
    default:
      return { bg: "bg-gray-800", text: "text-gray-400", border: "border-gray-700", label: "WAITING" };
  }
}

function scoreColor(v: number | null): string {
  if (v == null) return "text-gray-500 bg-gray-800";
  if (v > 15) return "text-green-300 bg-green-900/40";
  if (v < -15) return "text-red-300 bg-red-900/40";
  return "text-gray-300 bg-gray-800";
}

const MainBrainPanel: React.FC = () => {
  const { data } = useApi<MainBrainData>("/api/main-brain", { pollInterval: 15_000 });

  if (!data) {
    return <div className="mb-6 bg-gray-900 border border-gray-800 rounded-xl p-4 animate-pulse h-48" />;
  }

  const style = verdictStyle(data.verdict);
  const rec = data.recommendation;
  const hb = data.last_heartbeat;

  return (
    <div className="mb-6">
      <h2 className="text-xs uppercase tracking-widest text-gray-500 font-medium mb-3 flex items-center gap-2">
        <span>Main Trader Brain</span>
        <span className="text-[9px] text-gray-600">what is the main trader thinking</span>
      </h2>
      <div className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
        {/* Verdict banner */}
        <div className={`${style.bg} ${style.text} px-5 py-3 flex items-center justify-between border-b-2 ${style.border}`}>
          <div>
            <div className="text-[10px] uppercase tracking-widest opacity-70">Main Trader</div>
            <div className="text-2xl font-black tracking-tight">{style.label}</div>
            <div className="text-[11px] opacity-80 mt-0.5">{data.verdict_detail}</div>
          </div>
          <div className="text-right">
            {rec && (
              <>
                <div className="text-[10px] uppercase tracking-widest opacity-70">Opus says</div>
                <div className="text-2xl font-black">{rec.action}</div>
                <div className="text-[11px] opacity-70">{((rec.confidence ?? 0) * 100).toFixed(0)}% confidence</div>
              </>
            )}
          </div>
        </div>

        {/* Scores row */}
        {data.scores && (
          <div className="px-5 py-2 border-b border-gray-800 flex flex-wrap gap-2">
            {Object.entries(data.scores).filter(([k]) => k !== "timestamp").map(([k, v]) => (
              <span key={k} className={`px-2 py-0.5 rounded text-[10px] font-mono ${scoreColor(v as number | null)}`}>
                {k}: {v != null ? (v as number).toFixed(1) : "—"}
              </span>
            ))}
          </div>
        )}

        {/* Gates */}
        <div className="px-5 py-2 border-b border-gray-800">
          <div className="flex items-center gap-3 text-[10px] font-mono flex-wrap">
            <span className="text-gray-500 uppercase tracking-wider">Entry Gates</span>
            {data.gates.map((g) => (
              <span key={g.name} className={g.ok ? "text-green-300" : "text-red-300"} title={g.detail}>
                {g.ok ? "✅" : "❌"} {g.name}
              </span>
            ))}
            <span className="text-gray-500 ml-auto">{data.gates_passed}/{data.gates_total}</span>
          </div>
        </div>

        {/* Opus reasoning (collapsible) */}
        {rec && rec.analysis_text && (
          <details className="px-5 py-2 border-b border-gray-800">
            <summary className="text-[10px] uppercase tracking-wider text-blue-400 cursor-pointer hover:text-blue-300">
              📋 Opus Reasoning
            </summary>
            <div className="mt-2 text-[11px] text-gray-300 leading-relaxed whitespace-pre-line max-h-60 overflow-y-auto">
              {rec.analysis_text}
            </div>
            {rec.risk_factors && (
              <div className="mt-2 text-[10px]">
                <span className="text-gray-500">Risk factors: </span>
                <span className="text-red-300">
                  {Array.isArray(rec.risk_factors) ? rec.risk_factors.join(" · ") : rec.risk_factors}
                </span>
              </div>
            )}
            {rec.entry_price != null && (
              <div className="mt-1 text-[10px] text-gray-500 font-mono">
                Suggested: entry ${(rec.entry_price ?? 0).toFixed(2)} · SL ${(rec.stop_loss ?? 0).toFixed(2)} · TP ${(rec.take_profit ?? 0).toFixed(2)}
              </div>
            )}
          </details>
        )}

        {/* Last heartbeat (if managing) */}
        {data.has_open_campaign && hb && (
          <div className="px-5 py-2 border-b border-gray-800">
            <div className="text-[10px] uppercase tracking-wider text-gray-500 mb-1">
              🫀 Last Heartbeat: <span className={hb.decision === "hold" ? "text-gray-300" : hb.decision === "close" ? "text-red-300" : "text-blue-300"}>{hb.decision}</span>
              {hb.ran_at && <span className="text-gray-600 ml-2">{new Date(hb.ran_at).toLocaleTimeString()}</span>}
            </div>
            <div className="text-[11px] text-gray-400 leading-relaxed line-clamp-3">{hb.reason}</div>
          </div>
        )}

        {/* Footer */}
        <div className="px-5 py-1 bg-gray-950/40 flex items-center justify-between text-[9px] text-gray-600 font-mono">
          <span>
            price {data.current_price != null ? `$${(data.current_price).toFixed(3)}` : "stale"} ·
            equity ${(data.account.equity ?? 0).toFixed(0)} ·
            dd {(data.account.drawdown_pct ?? 0).toFixed(1)}%
          </span>
          <span>cached {data.cache_age_seconds ?? 0}s · {new Date(data.generated_at).toLocaleTimeString()}</span>
        </div>
      </div>
    </div>
  );
};

export default MainBrainPanel;
