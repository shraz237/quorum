/**
 * LearningPanel — feedback-loop widgets:
 *
 *   1. Trade Journal — closed campaigns + running performance stats
 *   2. Historical Pattern Match — similar past moments with forward returns
 *   3. Smart Alerts — condition-tree alerts with live evaluation status
 */

import React, { useState } from "react";
import useApi from "../hooks/useApi";

// ---------------------------------------------------------------------------
// Shared
// ---------------------------------------------------------------------------

function fmtUsd(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "—";
  const sign = v < 0 ? "-" : "";
  const abs = Math.abs(v);
  if (abs >= 1000) return `${sign}$${abs.toLocaleString("en-US", { maximumFractionDigits: 0 })}`;
  return `${sign}$${abs.toFixed(2)}`;
}

function signedUsd(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "—";
  return (v >= 0 ? "+" : "") + fmtUsd(v);
}

function pnlColor(v: number | null | undefined): string {
  if (v == null) return "text-gray-500";
  return v >= 0 ? "text-green-400" : "text-red-400";
}

function fmtDateTime(iso: string | null): string {
  if (!iso) return "—";
  return iso.replace("T", " ").substring(0, 16);
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
// 1. Trade Journal
// ---------------------------------------------------------------------------

interface SnapshotData {
  price?: number;
  scores?: Record<string, number | null>;
  ai_recommendation?: {
    action?: string;
    confidence?: number;
    analysis_text?: string;
    base_scenario?: string;
    alt_scenario?: string;
    unified_score?: number;
    opus_override_score?: number;
  };
  recent_news?: Array<{
    ts: string;
    summary: string;
    sentiment?: string;
    score?: number;
  }>;
  exit_context?: {
    total_friction_usd?: number;
    total_realized_pnl?: number;
    close_trigger?: string;
    close_notes?: string;
  };
  max_favorable_excursion_usd?: number;
  max_adverse_excursion_usd?: number;
  friction_config?: Record<string, unknown>;
  [key: string]: unknown;
}

interface JournalEntry {
  id: number;
  side: "LONG" | "SHORT";
  status: string;
  opened_at: string | null;
  closed_at: string | null;
  duration_minutes: number | null;
  realized_pnl: number | null;
  pnl_pct_of_entry_margin: number | null;
  notes: string | null;
  entry_snapshot: SnapshotData | null;
  exit_snapshot: SnapshotData | null;
}

interface JournalStats {
  total_trades: number;
  wins: number;
  losses: number;
  win_rate: number | null;
  total_pnl: number;
  avg_win: number | null;
  avg_loss: number | null;
  profit_factor: number | null | "Infinity";
  largest_win: number;
  largest_loss: number;
  avg_duration_minutes: number | null;
  sharpe_like: number | null;
}

interface JournalResponse {
  entries: JournalEntry[];
  stats: JournalStats;
}

const SnapshotDetail: React.FC<{ label: string; snap: SnapshotData | null }> = ({ label, snap }) => {
  if (!snap) return <div className="text-[9px] text-gray-600 italic">No {label.toLowerCase()} snapshot captured.</div>;

  const scores = snap.scores;
  const ai = snap.ai_recommendation;
  const news = snap.recent_news;
  const exitCtx = snap.exit_context;

  return (
    <div className="space-y-1.5 text-[10px]">
      {/* Price + Scores */}
      <div className="flex flex-wrap gap-x-3 gap-y-0.5 font-mono">
        {snap.price != null && (
          <span className="text-gray-400">Price: <span className="text-white">${snap.price.toFixed(3)}</span></span>
        )}
        {scores && Object.entries(scores).map(([k, v]) => (
          <span key={k} className="text-gray-500">
            {k}: <span className={v != null && v >= 60 ? "text-green-400" : v != null && v <= 40 ? "text-red-400" : "text-gray-300"}>
              {v != null ? v.toFixed(1) : "—"}
            </span>
          </span>
        ))}
      </div>

      {/* AI Recommendation */}
      {ai && (
        <div className="bg-gray-800/40 rounded p-1.5">
          <div className="text-[9px] text-gray-500 uppercase mb-0.5">AI Recommendation</div>
          <div className="flex flex-wrap gap-x-3 gap-y-0.5 font-mono text-[9px] mb-1">
            {ai.action && <span className={ai.action === "LONG" ? "text-green-400 font-bold" : ai.action === "SHORT" ? "text-red-400 font-bold" : "text-gray-300"}>{ai.action}</span>}
            {ai.confidence != null && <span className="text-gray-400">conf: {ai.confidence}%</span>}
            {ai.unified_score != null && <span className="text-gray-400">unified: {ai.unified_score.toFixed(1)}</span>}
            {ai.opus_override_score != null && <span className="text-gray-400">opus: {ai.opus_override_score.toFixed(1)}</span>}
          </div>
          {ai.analysis_text && (
            <div className="text-gray-400 text-[9px] line-clamp-4 whitespace-pre-wrap">{ai.analysis_text.slice(0, 500)}</div>
          )}
          {ai.base_scenario && (
            <div className="text-[9px] mt-0.5"><span className="text-gray-500">Base: </span><span className="text-gray-400">{ai.base_scenario}</span></div>
          )}
          {ai.alt_scenario && (
            <div className="text-[9px]"><span className="text-gray-500">Alt: </span><span className="text-gray-400">{ai.alt_scenario}</span></div>
          )}
        </div>
      )}

      {/* News headlines */}
      {news && news.length > 0 && (
        <div>
          <div className="text-[9px] text-gray-500 uppercase">Recent News</div>
          {news.map((n, i) => (
            <div key={i} className="text-[9px] text-gray-400 truncate">
              <span className={n.sentiment === "bearish" ? "text-red-400" : n.sentiment === "bullish" ? "text-green-400" : "text-gray-500"}>
                [{n.sentiment || "neutral"}]
              </span>{" "}
              {n.summary}
            </div>
          ))}
        </div>
      )}

      {/* Friction + MFE/MAE (exit only) */}
      {(exitCtx?.total_friction_usd != null || snap.max_favorable_excursion_usd != null) && (
        <div className="flex flex-wrap gap-x-4 gap-y-0.5 font-mono text-[9px]">
          {exitCtx?.total_friction_usd != null && (
            <span className="text-gray-500">Friction: <span className="text-amber-300">${exitCtx.total_friction_usd.toFixed(2)}</span></span>
          )}
          {snap.max_favorable_excursion_usd != null && (
            <span className="text-gray-500">MFE: <span className="text-green-400">+${snap.max_favorable_excursion_usd.toFixed(3)}</span></span>
          )}
          {snap.max_adverse_excursion_usd != null && (
            <span className="text-gray-500">MAE: <span className="text-red-400">-${snap.max_adverse_excursion_usd.toFixed(3)}</span></span>
          )}
        </div>
      )}
    </div>
  );
};

const TradeJournalCard: React.FC = () => {
  const { data } = useApi<JournalResponse>("/api/trade-journal?limit=50", { pollInterval: 60_000 });

  if (!data) {
    return <Card title="Trade Journal"><div className="text-gray-600 text-xs">loading…</div></Card>;
  }

  const s = data.stats;
  const entries = data.entries || [];

  return (
    <Card title="Trade Journal" subtitle={`${s.total_trades} closed`}>
      {s.total_trades === 0 ? (
        <div className="text-[11px] text-gray-500 italic py-4 text-center">
          No closed campaigns yet. Entry/exit snapshots will be captured automatically.
        </div>
      ) : (
        <>
          {/* Performance stats header */}
          <div className="grid grid-cols-3 gap-2 text-[10px]">
            <div className="bg-gray-800/50 rounded px-2 py-1">
              <div className="text-gray-500 uppercase">Win Rate</div>
              <div className={`text-sm font-bold ${s.win_rate != null && s.win_rate >= 50 ? "text-green-400" : "text-red-400"}`}>
                {s.win_rate != null ? `${s.win_rate.toFixed(1)}%` : "—"}
              </div>
              <div className="text-[9px] text-gray-600">{s.wins}W / {s.losses}L</div>
            </div>
            <div className="bg-gray-800/50 rounded px-2 py-1">
              <div className="text-gray-500 uppercase">Total PnL</div>
              <div className={`text-sm font-bold ${pnlColor(s.total_pnl)}`}>
                {signedUsd(s.total_pnl)}
              </div>
              <div className="text-[9px] text-gray-600">all-time</div>
            </div>
            <div className="bg-gray-800/50 rounded px-2 py-1">
              <div className="text-gray-500 uppercase">Profit Factor</div>
              <div className={`text-sm font-bold ${s.profit_factor != null && s.profit_factor !== "Infinity" && Number(s.profit_factor) >= 1.5 ? "text-green-400" : "text-gray-300"}`}>
                {s.profit_factor === "Infinity" ? "∞" : s.profit_factor ?? "—"}
              </div>
              <div className="text-[9px] text-gray-600">wins/losses</div>
            </div>
          </div>

          {/* Secondary row */}
          <div className="grid grid-cols-3 gap-2 text-[10px] mt-1">
            <div className="text-center">
              <div className="text-gray-500">Avg Win</div>
              <div className="text-green-400">{signedUsd(s.avg_win)}</div>
            </div>
            <div className="text-center">
              <div className="text-gray-500">Avg Loss</div>
              <div className="text-red-400">{signedUsd(s.avg_loss)}</div>
            </div>
            <div className="text-center">
              <div className="text-gray-500">Sharpe-ish</div>
              <div className="text-gray-300">{s.sharpe_like ?? "—"}</div>
            </div>
          </div>

          {/* Entry list */}
          <div className="border-t border-gray-800 pt-2 mt-1">
            <div className="max-h-72 overflow-y-auto flex flex-col gap-0.5">
              {entries.map((e) => (
                <details key={e.id} className="group rounded hover:bg-gray-800/20">
                  <summary className="text-[10px] flex items-center gap-2 px-1 py-0.5 cursor-pointer list-none [&::-webkit-details-marker]:hidden">
                    <span className="text-gray-600 group-open:rotate-90 transition-transform text-[8px]">&#9654;</span>
                    <span className={`font-bold w-12 ${e.side === "SHORT" ? "text-red-400" : "text-green-400"}`}>
                      {e.side}
                    </span>
                    <span className="text-gray-500 w-10">#{e.id}</span>
                    <span className="text-gray-400 w-24 truncate">{fmtDateTime(e.closed_at)}</span>
                    <span className={`w-20 text-right font-semibold ${pnlColor(e.realized_pnl)}`}>
                      {signedUsd(e.realized_pnl)}
                    </span>
                    <span className={`w-14 text-right text-[9px] ${pnlColor(e.pnl_pct_of_entry_margin)}`}>
                      {e.pnl_pct_of_entry_margin != null ? `${e.pnl_pct_of_entry_margin > 0 ? "+" : ""}${e.pnl_pct_of_entry_margin.toFixed(1)}%` : ""}
                    </span>
                  </summary>
                  <div className="px-2 pb-2 pt-1 space-y-2 border-l-2 border-gray-800 ml-2">
                    <details className="group/entry">
                      <summary className="text-[9px] text-gray-500 uppercase cursor-pointer hover:text-gray-400">
                        Entry Reasoning
                      </summary>
                      <div className="mt-1 pl-2">
                        <SnapshotDetail label="Entry" snap={e.entry_snapshot} />
                      </div>
                    </details>
                    <details className="group/exit">
                      <summary className="text-[9px] text-gray-500 uppercase cursor-pointer hover:text-gray-400">
                        Exit Reasoning
                      </summary>
                      <div className="mt-1 pl-2">
                        <SnapshotDetail label="Exit" snap={e.exit_snapshot} />
                      </div>
                    </details>
                  </div>
                </details>
              ))}
            </div>
          </div>
        </>
      )}
    </Card>
  );
};

// ---------------------------------------------------------------------------
// 2. Historical Pattern Match
// ---------------------------------------------------------------------------

interface Match {
  distance: number;
  timestamp: string;
  price: number;
  forward_return_1h_pct: number | null;
  forward_return_4h_pct: number | null;
  forward_return_24h_pct: number | null;
  features: Record<string, number | null>;
}

interface Distribution {
  mean: number | null;
  median: number | null;
  win_rate_pct: number | null;
  n: number;
}

interface PatternResponse {
  current_features: Record<string, number | null>;
  matches: Match[];
  distribution: {
    "1h": Distribution;
    "4h": Distribution;
    "24h": Distribution;
  };
  total_history: number;
  note?: string;
}

const PatternMatchCard: React.FC = () => {
  const { data } = useApi<PatternResponse>("/api/pattern-match?top_n=10", { pollInterval: 120_000 });

  if (!data) {
    return <Card title="Pattern Match"><div className="text-gray-600 text-xs">loading…</div></Card>;
  }

  if (data.note) {
    return (
      <Card title="Historical Pattern Match" subtitle="similarity search">
        <div className="text-[11px] text-gray-500 italic py-4 text-center">{data.note}</div>
      </Card>
    );
  }

  const dist = data.distribution;

  const DistRow: React.FC<{ label: string; d: Distribution }> = ({ label, d }) => (
    <div className="flex items-center gap-2 text-[10px]">
      <span className="text-gray-500 w-8">{label}</span>
      <span className={`font-mono w-14 text-right ${pnlColor(d.mean)}`}>
        {d.mean != null ? `${d.mean > 0 ? "+" : ""}${d.mean.toFixed(2)}%` : "—"}
      </span>
      <span className={`font-mono w-12 text-right ${d.win_rate_pct != null && d.win_rate_pct >= 50 ? "text-green-400" : "text-red-400"}`}>
        {d.win_rate_pct != null ? `${d.win_rate_pct.toFixed(0)}%` : "—"}
      </span>
      <span className="text-gray-600 text-[9px]">n={d.n}</span>
    </div>
  );

  return (
    <Card title="Historical Pattern Match" subtitle={`top-10 · ${data.total_history} prior`}>
      <div className="text-[10px] text-gray-500 uppercase">Forward returns (avg / win rate)</div>
      <DistRow label="1h" d={dist["1h"]} />
      <DistRow label="4h" d={dist["4h"]} />
      <DistRow label="24h" d={dist["24h"]} />

      <div className="border-t border-gray-800 pt-2 mt-1">
        <div className="text-[10px] text-gray-500 uppercase mb-1">Closest matches</div>
        <div className="max-h-40 overflow-y-auto flex flex-col gap-0.5">
          {data.matches.slice(0, 10).map((m, i) => (
            <div key={i} className="text-[10px] flex items-center gap-2 font-mono">
              <span className="text-gray-600 w-20 truncate">{fmtDateTime(m.timestamp)}</span>
              <span className="text-gray-500 w-14 text-right">d={m.distance.toFixed(2)}</span>
              <span className={`w-14 text-right ${pnlColor(m.forward_return_24h_pct)}`}>
                {m.forward_return_24h_pct != null ? `${m.forward_return_24h_pct > 0 ? "+" : ""}${m.forward_return_24h_pct.toFixed(2)}%` : "—"}
              </span>
            </div>
          ))}
        </div>
      </div>

      <div className="text-[9px] text-gray-600 pt-1 border-t border-gray-800 mt-1">
        Weighted Euclidean distance over funding / positioning / scores. Needs 24h+ of history to start producing results.
      </div>
    </Card>
  );
};

// ---------------------------------------------------------------------------
// 3. Smart Alerts
// ---------------------------------------------------------------------------

interface SmartAlert {
  id: number;
  created_at: string;
  status: string;
  expression: any;
  message: string | null;
  one_shot: boolean;
  triggered_at: string | null;
  matches_now: boolean;
  trace: string[];
}

const SmartAlertsCard: React.FC = () => {
  const { data, refetch } = useApi<SmartAlert[]>("/api/smart-alerts", { pollInterval: 30_000 });
  const [showNew, setShowNew] = useState(false);
  const [newMessage, setNewMessage] = useState("");
  const [newExpression, setNewExpression] = useState(JSON.stringify({
    op: "AND",
    clauses: [
      { metric: "funding_rate_pct", cmp: "<=", value: -0.03 },
      { metric: "orderbook_imbalance_pct", cmp: ">=", value: 30 },
    ],
  }, null, 2));

  const create = async () => {
    try {
      const expr = JSON.parse(newExpression);
      const res = await fetch("/api/smart-alerts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ expression: expr, message: newMessage || "Smart alert", one_shot: true }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setShowNew(false);
      setNewMessage("");
      refetch();
    } catch (e) {
      alert(`Failed: ${e}`);
    }
  };

  const del = async (id: number) => {
    if (!confirm(`Delete alert #${id}?`)) return;
    await fetch(`/api/smart-alerts/${id}`, { method: "DELETE" });
    refetch();
  };

  const alerts = data || [];

  return (
    <Card title="Smart Alerts" subtitle="confluence-based">
      <div className="flex items-center justify-between">
        <span className="text-[10px] text-gray-500">{alerts.length} configured</span>
        <button
          onClick={() => setShowNew((v) => !v)}
          className="text-[10px] px-2 py-0.5 rounded bg-blue-900/60 text-blue-200 hover:bg-blue-800"
        >
          {showNew ? "Cancel" : "+ New"}
        </button>
      </div>

      {showNew && (
        <div className="bg-gray-800/50 rounded p-2 space-y-1">
          <input
            value={newMessage}
            onChange={(e) => setNewMessage(e.target.value)}
            placeholder="Alert message (e.g. 'Pre-squeeze setup')"
            className="w-full text-[10px] bg-gray-900 border border-gray-700 rounded px-2 py-1 text-gray-100"
          />
          <textarea
            value={newExpression}
            onChange={(e) => setNewExpression(e.target.value)}
            rows={8}
            className="w-full text-[10px] font-mono bg-gray-900 border border-gray-700 rounded px-2 py-1 text-gray-300"
          />
          <button
            onClick={create}
            className="w-full text-[10px] py-1 rounded bg-green-900/60 text-green-200 hover:bg-green-800"
          >
            Save alert
          </button>
          <div className="text-[9px] text-gray-600 leading-tight">
            Metrics: price, unified, technical, conviction_score, funding_rate_pct,
            open_interest_change_24h_pct, retail_delta_pct, taker_buysell_ratio,
            orderbook_imbalance_pct, drawdown_pct, max_anomaly_severity.
            Operators: &lt;, &lt;=, &gt;, &gt;=, ==, !=. Nest with op: AND/OR.
          </div>
        </div>
      )}

      <div className="flex flex-col gap-1 max-h-48 overflow-y-auto">
        {alerts.length === 0 && !showNew ? (
          <div className="text-[10px] text-gray-500 italic text-center py-4">
            No smart alerts. Click + New to create one.
          </div>
        ) : (
          alerts.map((a) => (
            <div
              key={a.id}
              className={`border rounded p-2 text-[10px] ${
                a.status === "triggered"
                  ? "border-yellow-900/60 bg-yellow-950/30"
                  : a.matches_now
                  ? "border-orange-900/60 bg-orange-950/30"
                  : "border-gray-800 bg-gray-950/40"
              }`}
            >
              <div className="flex items-center justify-between">
                <span className="font-semibold text-gray-200 truncate">#{a.id} {a.message}</span>
                <button onClick={() => del(a.id)} className="text-red-500 text-[9px] ml-1">✕</button>
              </div>
              <div className="text-[9px] text-gray-500">
                Status: <span className={a.status === "active" ? "text-green-400" : "text-yellow-400"}>{a.status}</span>
                {a.matches_now && <span className="ml-2 text-orange-400 font-bold">⚡ MATCHES NOW</span>}
              </div>
              {a.trace.length > 0 && (
                <div className="mt-1 text-[9px] text-gray-600 font-mono">
                  {a.trace.slice(0, 4).map((t, i) => <div key={i}>{t}</div>)}
                </div>
              )}
            </div>
          ))
        )}
      </div>
    </Card>
  );
};

// ---------------------------------------------------------------------------
// Main panel
// ---------------------------------------------------------------------------

const LearningPanel: React.FC = () => (
  <div className="mb-6">
    <h2 className="text-xs uppercase tracking-widest text-gray-500 font-medium mb-3">
      Learning &amp; Feedback Loop
    </h2>
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
      <TradeJournalCard />
      <PatternMatchCard />
      <SmartAlertsCard />
    </div>
  </div>
);

export default LearningPanel;
