/**
 * SynthesisPanel — three top-of-dashboard widgets that turn the firehose
 * of raw numbers into something a human can read in 5 seconds:
 *
 *   1. NowBrief         — AI-generated structured brief (Haiku)
 *   2. SignalConfluence — bull/bear/neutral matrix of all current signals
 *   3. AnomalyAlerts    — current extreme/rare events + history ticker
 */

import React, { useState } from "react";
import useApi from "../hooks/useApi";

// ---------------------------------------------------------------------------
// 1. NOW BRIEF
// ---------------------------------------------------------------------------

interface NowBrief {
  headline: string;
  market_state: string;
  your_position: string;
  next_action: string;
  watch_for: string;
  risk_level: number;
  risk_reason: string;
  generated_at: string;
  cache_age_seconds: number;
  error?: string;
}

function riskColor(level: number): { bg: string; text: string; bar: string } {
  if (level >= 8) return { bg: "bg-red-950/60 border-red-900", text: "text-red-300", bar: "bg-red-500" };
  if (level >= 6) return { bg: "bg-orange-950/60 border-orange-900", text: "text-orange-300", bar: "bg-orange-500" };
  if (level >= 4) return { bg: "bg-yellow-950/60 border-yellow-900", text: "text-yellow-300", bar: "bg-yellow-400" };
  return { bg: "bg-gray-900 border-gray-800", text: "text-emerald-300", bar: "bg-emerald-500" };
}

const NowBriefCard: React.FC = () => {
  const { data, refetch } = useApi<NowBrief>("/api/now-brief", {
    pollInterval: 30_000,
  });

  const forceRefresh = async () => {
    await fetch("/api/now-brief?force=true");
    refetch();
  };

  if (!data) {
    return (
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-4 h-60 animate-pulse" />
    );
  }
  if (data.error) {
    return (
      <div className="bg-gray-900 border border-red-900 rounded-xl p-4 text-red-400 text-xs">
        Brief unavailable: {data.error}
      </div>
    );
  }

  const rc = riskColor(data.risk_level);
  const riskPct = (data.risk_level / 10) * 100;

  return (
    <div className={`border rounded-xl p-4 flex flex-col gap-2 ${rc.bg}`}>
      <div className="flex items-baseline justify-between">
        <h3 className="text-xs uppercase tracking-widest text-gray-500 font-medium">
          Now Brief
        </h3>
        <div className="flex items-center gap-2 text-[10px] text-gray-600">
          <span>{data.cache_age_seconds.toFixed(0)}s ago</span>
          <button
            onClick={forceRefresh}
            className="px-2 py-0.5 bg-gray-800 rounded hover:bg-gray-700 text-gray-300"
            title="Force regenerate"
          >
            ↻
          </button>
        </div>
      </div>

      {/* Headline */}
      <div className="text-base font-bold text-gray-100 leading-tight">
        {data.headline}
      </div>

      {/* Risk level bar */}
      <div className="flex items-center gap-2 mt-1">
        <span className="text-[10px] uppercase text-gray-500">Risk</span>
        <div className="flex-1 h-1.5 bg-gray-800 rounded-full overflow-hidden">
          <div
            className={`h-full rounded-full transition-all duration-500 ${rc.bar}`}
            style={{ width: `${riskPct}%` }}
          />
        </div>
        <span className={`text-xs font-bold ${rc.text}`}>{data.risk_level}/10</span>
      </div>
      <div className="text-[10px] text-gray-500 -mt-1">{data.risk_reason}</div>

      {/* Sections */}
      <div className="border-t border-gray-800 pt-2 flex flex-col gap-2">
        <div>
          <div className="text-[10px] uppercase tracking-wider text-gray-500 mb-0.5">Market</div>
          <div className="text-xs text-gray-300 leading-snug">{data.market_state}</div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wider text-gray-500 mb-0.5">Your Position</div>
          <div className="text-xs text-gray-300 leading-snug">{data.your_position}</div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wider text-blue-500 mb-0.5">Next Action</div>
          <div className="text-xs text-blue-200 leading-snug font-medium">{data.next_action}</div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wider text-gray-500 mb-0.5">Watch For</div>
          <div className="text-xs text-gray-400 leading-snug">{data.watch_for}</div>
        </div>
      </div>
    </div>
  );
};

// ---------------------------------------------------------------------------
// 2. SIGNAL CONFLUENCE
// ---------------------------------------------------------------------------

interface ConfluenceItem {
  signal: string;
  value: string;
  reason: string;
}

interface Confluence {
  bull: ConfluenceItem[];
  bear: ConfluenceItem[];
  neutral: ConfluenceItem[];
  bull_count: number;
  bear_count: number;
  neutral_count: number;
  confluence_score: number;
  dominant_side: "BULL" | "BEAR" | "MIXED";
}

const ConfluenceCard: React.FC = () => {
  const { data } = useApi<Confluence>("/api/signal-confluence", {
    pollInterval: 20_000,
  });

  if (!data) {
    return (
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-4 h-60 animate-pulse" />
    );
  }

  const dominantColor =
    data.dominant_side === "BULL"
      ? "text-green-400"
      : data.dominant_side === "BEAR"
      ? "text-red-400"
      : "text-gray-400";

  const Column: React.FC<{
    title: string;
    count: number;
    items: ConfluenceItem[];
    color: string;
  }> = ({ title, count, items, color }) => (
    <div className="flex flex-col gap-1 min-w-0">
      <div className={`text-[11px] uppercase font-bold tracking-widest ${color} flex items-center justify-between`}>
        <span>{title}</span>
        <span className="text-gray-200 bg-gray-800 rounded-full px-2 py-0.5 text-[10px]">
          {count}
        </span>
      </div>
      <div className="flex flex-col gap-1 overflow-y-auto max-h-48 pr-1">
        {items.length === 0 ? (
          <div className="text-[10px] text-gray-700 italic">none</div>
        ) : (
          items.map((it, i) => (
            <div
              key={i}
              className="bg-gray-800/50 rounded px-2 py-1 text-[10px] leading-tight"
            >
              <div className="flex justify-between items-baseline">
                <span className="text-gray-200 font-semibold truncate">{it.signal}</span>
                <span className="text-gray-500 font-mono ml-1">{it.value}</span>
              </div>
              <div className="text-gray-500 text-[9px] truncate">{it.reason}</div>
            </div>
          ))
        )}
      </div>
    </div>
  );

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-4 flex flex-col gap-2">
      <div className="flex items-baseline justify-between">
        <h3 className="text-xs uppercase tracking-widest text-gray-500 font-medium">
          Signal Confluence
        </h3>
        <div className="text-[10px]">
          <span className="text-gray-500">Dominant: </span>
          <span className={`font-bold ${dominantColor}`}>{data.dominant_side}</span>
          <span className="text-gray-500 ml-2">Score </span>
          <span className="text-gray-200 font-bold">{data.confluence_score}</span>
        </div>
      </div>

      {/* Balance bar showing bull vs bear weighting */}
      <div className="flex h-2 rounded-full overflow-hidden bg-gray-800">
        <div
          className="bg-green-500 transition-all duration-500"
          style={{
            width: `${
              (data.bull_count / Math.max(1, data.bull_count + data.bear_count)) * 100
            }%`,
          }}
        />
        <div
          className="bg-red-500 transition-all duration-500"
          style={{
            width: `${
              (data.bear_count / Math.max(1, data.bull_count + data.bear_count)) * 100
            }%`,
          }}
        />
      </div>

      <div className="grid grid-cols-3 gap-3 mt-1">
        <Column title="🐂 Bull" count={data.bull_count} items={data.bull} color="text-green-400" />
        <Column title="⚖ Neutral" count={data.neutral_count} items={data.neutral} color="text-gray-400" />
        <Column title="🐻 Bear" count={data.bear_count} items={data.bear} color="text-red-400" />
      </div>
    </div>
  );
};

// ---------------------------------------------------------------------------
// 3. ANOMALY ALERTS
// ---------------------------------------------------------------------------

interface AnomalyHit {
  category: string;
  severity: number;
  direction: "BULL" | "BEAR" | "NEUTRAL";
  title: string;
  description: string;
  metric_value: number | null;
  metric_threshold: number | null;
  icon?: string;
}

interface HistoryEntry extends AnomalyHit {
  id: number;
  time: number;
}

interface Anomalies {
  current: AnomalyHit[];
  history: HistoryEntry[];
}

function sevColor(sev: number): { bar: string; text: string; border: string } {
  if (sev >= 8) return { bar: "bg-red-500", text: "text-red-300", border: "border-red-900/60" };
  if (sev >= 6) return { bar: "bg-orange-500", text: "text-orange-300", border: "border-orange-900/50" };
  if (sev >= 4) return { bar: "bg-yellow-400", text: "text-yellow-300", border: "border-yellow-900/40" };
  return { bar: "bg-emerald-500", text: "text-emerald-300", border: "border-emerald-900/30" };
}

function fmtHM(timestampSec: number): string {
  return new Date(timestampSec * 1000).toLocaleTimeString("en-US", {
    hour: "2-digit", minute: "2-digit", hour12: false,
  });
}

const AnomalyCard: React.FC = () => {
  const [tab, setTab] = useState<"current" | "history">("current");
  const { data } = useApi<Anomalies>("/api/anomalies?hours=24", {
    pollInterval: 30_000,
  });

  if (!data) {
    return (
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-4 h-60 animate-pulse" />
    );
  }

  const current = data.current || [];
  const history = data.history || [];

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-4 flex flex-col gap-2">
      <div className="flex items-baseline justify-between">
        <h3 className="text-xs uppercase tracking-widest text-gray-500 font-medium">
          Anomaly Radar
        </h3>
        <div className="flex gap-1 text-[10px]">
          <button
            onClick={() => setTab("current")}
            className={`px-2 py-0.5 rounded ${
              tab === "current" ? "bg-gray-700 text-gray-100" : "text-gray-500 hover:text-gray-300"
            }`}
          >
            Active ({current.length})
          </button>
          <button
            onClick={() => setTab("history")}
            className={`px-2 py-0.5 rounded ${
              tab === "history" ? "bg-gray-700 text-gray-100" : "text-gray-500 hover:text-gray-300"
            }`}
          >
            24h log ({history.length})
          </button>
        </div>
      </div>

      <div className="flex flex-col gap-2 overflow-y-auto max-h-60 pr-1">
        {tab === "current" ? (
          current.length === 0 ? (
            <div className="text-[11px] text-gray-500 italic py-4 text-center">
              No extreme conditions detected. Market is in normal range.
            </div>
          ) : (
            current.map((a, i) => {
              const sc = sevColor(a.severity);
              const dirBadge =
                a.direction === "BULL"
                  ? "bg-green-900/60 text-green-300"
                  : a.direction === "BEAR"
                  ? "bg-red-900/60 text-red-300"
                  : "bg-gray-800 text-gray-400";
              return (
                <div
                  key={i}
                  className={`border rounded p-2 bg-gray-950/40 ${sc.border}`}
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="flex items-center gap-1 flex-1 min-w-0">
                      <span className="text-sm">{a.icon || "⚠️"}</span>
                      <span className="text-xs font-bold text-gray-100 truncate">
                        {a.title}
                      </span>
                    </div>
                    <span className={`text-[9px] px-1.5 py-0.5 rounded ${dirBadge}`}>
                      {a.direction}
                    </span>
                    <span className={`text-[10px] font-bold ${sc.text}`}>
                      {a.severity}/10
                    </span>
                  </div>
                  <div className="text-[10px] text-gray-400 mt-0.5 leading-snug">
                    {a.description}
                  </div>
                </div>
              );
            })
          )
        ) : history.length === 0 ? (
          <div className="text-[11px] text-gray-500 italic py-4 text-center">
            No anomalies in the last 24 hours.
          </div>
        ) : (
          history.map((h) => (
            <div key={h.id} className="text-[10px] flex items-start gap-2">
              <span className="text-gray-600 font-mono w-10 flex-shrink-0">
                {fmtHM(h.time)}
              </span>
              <span className="text-sm flex-shrink-0">{h.icon || "⚠️"}</span>
              <div className="flex-1 min-w-0">
                <span className="text-gray-300 font-semibold">{h.title}</span>
                <span className="text-gray-600 ml-2">sev {h.severity}</span>
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  );
};

// ---------------------------------------------------------------------------
// Main panel — 3-column grid
// ---------------------------------------------------------------------------

const SynthesisPanel: React.FC = () => (
  <div className="mb-6">
    <h2 className="text-xs uppercase tracking-widest text-gray-500 font-medium mb-3">
      Decision Support
    </h2>
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
      <NowBriefCard />
      <ConfluenceCard />
      <AnomalyCard />
    </div>
  </div>
);

export default SynthesisPanel;
