/**
 * ThesesTab — forward-looking conditional plans.
 *
 * Two independent sections so the scalp brain's own theses stay
 * visually (and statistically) separate from campaign theses:
 *
 *   📌 Campaign Theses — user + heartbeat + ai-brain
 *   ⚡ Scalp Theses    — scalp brain's own learning corpus
 *
 * Each section shows per-status lists (pending, triggered, resolved)
 * + a stats strip (hit rate, hypothetical P/L, counts).
 *
 * Read-only for v1 — creation happens via chat panel or natural
 * language ("remember if price hits 95 I want to go long") so the
 * chat tools are the primary creation UX. A dashboard form is a
 * nice follow-up but not required for v1.
 */

import React from "react";
import useApi from "../../hooks/useApi";

type Status = "pending" | "triggered" | "expired" | "cancelled" | "resolved";
type Domain = "campaign" | "scalp";
type Outcome = "correct" | "wrong" | "partial" | "unresolved";

interface Thesis {
  id: number;
  created_at: string;
  created_by: string;
  domain: Domain;
  title: string;
  thesis_text: string;
  reasoning: string | null;
  trigger_type: string;
  trigger_params: Record<string, unknown>;
  expires_at: string | null;
  planned_action: string;
  planned_entry: number | null;
  planned_stop_loss: number | null;
  planned_take_profit: number | null;
  planned_size_margin: number | null;
  status: Status;
  triggered_at: string | null;
  triggered_price: number | null;
  resolved_at: string | null;
  outcome: Outcome | null;
  outcome_notes: string | null;
  outcome_price: number | null;
  outcome_hypothetical_pnl_usd: number | null;
  outcome_max_favorable_excursion: number | null;
  outcome_max_adverse_excursion: number | null;
}

interface DomainStats {
  domain: string;
  days: number;
  total_created: number;
  by_status: Record<Status, number>;
  resolved: {
    correct: number;
    wrong: number;
    partial: number;
    unresolved: number;
    hit_rate: number | null;
    hypothetical_pnl_usd: number;
  };
}

interface ThesesPayload {
  domain_filter: string | null;
  pending: Thesis[];
  triggered: Thesis[];
  resolved: Thesis[];
  other: Thesis[];
  stats: Record<string, DomainStats>;
  generated_at: string;
}

const fmt = (n: number | null | undefined, d = 2): string =>
  n == null ? "—" : n.toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d });

function triggerDescription(t: Thesis): string {
  const p = (t.trigger_params || {}) as Record<string, unknown>;
  switch (t.trigger_type) {
    case "price_cross_above":
      return `price ≥ $${fmt(Number(p.price), 3)}`;
    case "price_cross_below":
      return `price ≤ $${fmt(Number(p.price), 3)}`;
    case "score_above":
      return `${String(p.score_key || "unified")} score ≥ ${fmt(Number(p.score), 1)}`;
    case "score_below":
      return `${String(p.score_key || "unified")} score ≤ ${fmt(Number(p.score), 1)}`;
    case "time_elapsed":
      return `${p.minutes} min elapsed`;
    case "news_keyword":
      return `news: ${Array.isArray(p.keywords) ? p.keywords.join(", ") : String(p.keywords)}`;
    case "scalp_brain_state":
      return `scalp brain → ${String(p.state)}`;
    default:
      return t.trigger_type;
  }
}

function outcomeBadge(outcome: Outcome | null): { text: string; color: string } {
  if (outcome === "correct") return { text: "✅ correct", color: "text-emerald-300 bg-emerald-900/40" };
  if (outcome === "wrong") return { text: "❌ wrong", color: "text-red-300 bg-red-900/40" };
  if (outcome === "partial") return { text: "〰 partial", color: "text-amber-300 bg-amber-900/40" };
  if (outcome === "unresolved") return { text: "❓ unresolved", color: "text-gray-400 bg-gray-800" };
  return { text: "—", color: "text-gray-500 bg-gray-800" };
}

const ThesisRow: React.FC<{ thesis: Thesis }> = ({ thesis }) => {
  const age = thesis.created_at
    ? Math.round((Date.now() - new Date(thesis.created_at).getTime()) / 60000)
    : null;

  return (
    <div className="border border-gray-800 rounded-lg p-3 text-[11px] bg-gray-950/50 hover:border-gray-700">
      <div className="flex items-start justify-between gap-2 mb-1">
        <div className="flex-1 min-w-0">
          <div className="font-bold text-gray-100 truncate">{thesis.title}</div>
          <div className="text-[9px] text-gray-600 font-mono mt-0.5">
            #{thesis.id} · {thesis.created_by.replace(/_/g, " ")}
            {age !== null && ` · ${age < 60 ? `${age}m ago` : `${Math.round(age / 60)}h ago`}`}
          </div>
        </div>
        {thesis.status === "resolved" && thesis.outcome && (
          <span className={`px-2 py-0.5 rounded-full text-[9px] font-bold ${outcomeBadge(thesis.outcome).color}`}>
            {outcomeBadge(thesis.outcome).text}
          </span>
        )}
      </div>

      <div className="text-gray-400 text-[10px] line-clamp-3">{thesis.thesis_text}</div>

      <div className="mt-2 flex items-center flex-wrap gap-x-3 gap-y-0.5 font-mono text-[10px]">
        <span className="text-gray-500">Trigger:</span>
        <span className="text-gray-200">{triggerDescription(thesis)}</span>
        {thesis.planned_action && thesis.planned_action !== "NONE" && thesis.planned_action !== "WATCH" && (
          <>
            <span className="text-gray-700">·</span>
            <span className={thesis.planned_action === "LONG" ? "text-emerald-300" : "text-red-300"}>
              {thesis.planned_action}
            </span>
          </>
        )}
        {thesis.planned_entry !== null && (
          <>
            <span className="text-gray-500">entry</span>
            <span className="text-gray-200">${fmt(thesis.planned_entry, 3)}</span>
          </>
        )}
        {thesis.planned_stop_loss !== null && (
          <>
            <span className="text-gray-500">SL</span>
            <span className="text-red-300">${fmt(thesis.planned_stop_loss, 3)}</span>
          </>
        )}
        {thesis.planned_take_profit !== null && (
          <>
            <span className="text-gray-500">TP</span>
            <span className="text-emerald-300">${fmt(thesis.planned_take_profit, 3)}</span>
          </>
        )}
      </div>

      {thesis.status === "resolved" && thesis.outcome_hypothetical_pnl_usd != null && (
        <div className="mt-1 text-[10px] font-mono">
          <span className="text-gray-500">Hypothetical P/L: </span>
          <span className={thesis.outcome_hypothetical_pnl_usd >= 0 ? "text-emerald-300 font-bold" : "text-red-300 font-bold"}>
            {thesis.outcome_hypothetical_pnl_usd >= 0 ? "+" : ""}${fmt(thesis.outcome_hypothetical_pnl_usd, 0)}
          </span>
          {thesis.outcome_notes && <span className="text-gray-600"> — {thesis.outcome_notes}</span>}
        </div>
      )}

      {thesis.status === "triggered" && thesis.triggered_at && (
        <div className="mt-1 text-[10px] text-amber-300 font-mono">
          🔔 Triggered at ${fmt(thesis.triggered_price, 3)} — {new Date(thesis.triggered_at).toLocaleTimeString()}
        </div>
      )}
    </div>
  );
};

const StatsStrip: React.FC<{ stats: DomainStats }> = ({ stats }) => {
  const hitRate = stats.resolved.hit_rate;
  const pnl = stats.resolved.hypothetical_pnl_usd;
  const hitRatePct = hitRate !== null ? Math.round(hitRate * 100) : null;

  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-px bg-gray-800 rounded-lg overflow-hidden mb-3">
      <div className="bg-gray-900 px-3 py-2">
        <div className="text-[9px] text-gray-500 uppercase">30d created</div>
        <div className="text-lg font-bold text-white tabular-nums">{stats.total_created}</div>
      </div>
      <div className="bg-gray-900 px-3 py-2">
        <div className="text-[9px] text-gray-500 uppercase">Hit rate</div>
        <div className={`text-lg font-bold tabular-nums ${
          hitRatePct === null ? "text-gray-500"
          : hitRatePct >= 60 ? "text-emerald-300"
          : hitRatePct >= 40 ? "text-amber-300"
          : "text-red-300"
        }`}>
          {hitRatePct === null ? "—" : `${hitRatePct}%`}
        </div>
        <div className="text-[9px] text-gray-600">
          ✅ {stats.resolved.correct}  ❌ {stats.resolved.wrong}  〰 {stats.resolved.partial}
        </div>
      </div>
      <div className="bg-gray-900 px-3 py-2">
        <div className="text-[9px] text-gray-500 uppercase">Hypothetical P/L</div>
        <div className={`text-lg font-bold tabular-nums ${
          pnl >= 0 ? "text-emerald-300" : "text-red-300"
        }`}>
          {pnl >= 0 ? "+" : ""}${fmt(pnl, 0)}
        </div>
      </div>
      <div className="bg-gray-900 px-3 py-2">
        <div className="text-[9px] text-gray-500 uppercase">By status</div>
        <div className="text-[10px] text-gray-300 mt-1 font-mono">
          {Object.entries(stats.by_status).map(([s, n]) => (
            <div key={s}>{s}: {n}</div>
          ))}
        </div>
      </div>
    </div>
  );
};

const DomainSection: React.FC<{
  heading: string;
  icon: string;
  color: string;
  description: string;
  stats: DomainStats | undefined;
  pending: Thesis[];
  triggered: Thesis[];
  resolved: Thesis[];
}> = ({ heading, icon, color, description, stats, pending, triggered, resolved }) => {
  return (
    <div className="mb-6">
      <div className="flex items-baseline gap-3 mb-2">
        <h3 className={`text-sm font-bold ${color}`}>
          {icon} {heading}
        </h3>
        <span className="text-[10px] text-gray-600">{description}</span>
      </div>

      {stats && <StatsStrip stats={stats} />}

      {triggered.length > 0 && (
        <div className="mb-3">
          <div className="text-[9px] uppercase tracking-wider text-amber-400 mb-1.5">
            🔔 Triggered — decide now ({triggered.length})
          </div>
          <div className="space-y-2">
            {triggered.map((t) => <ThesisRow key={t.id} thesis={t} />)}
          </div>
        </div>
      )}

      <div className="mb-3">
        <div className="text-[9px] uppercase tracking-wider text-blue-400 mb-1.5">
          ⏳ Pending ({pending.length})
        </div>
        {pending.length === 0 ? (
          <div className="text-[11px] text-gray-600 italic">No pending theses.</div>
        ) : (
          <div className="space-y-2">
            {pending.map((t) => <ThesisRow key={t.id} thesis={t} />)}
          </div>
        )}
      </div>

      {resolved.length > 0 && (
        <details className="mb-2">
          <summary className="text-[9px] uppercase tracking-wider text-gray-500 cursor-pointer hover:text-gray-400">
            📊 Resolved history ({resolved.length}) — click to expand
          </summary>
          <div className="space-y-2 mt-2">
            {resolved.map((t) => <ThesisRow key={t.id} thesis={t} />)}
          </div>
        </details>
      )}
    </div>
  );
};

const ThesesTab: React.FC = () => {
  const { data } = useApi<ThesesPayload>("/api/theses", { pollInterval: 20_000 });

  if (!data) {
    return (
      <div className="mb-6 bg-gray-900 border border-gray-800 rounded-xl p-4 animate-pulse h-64" />
    );
  }

  const allPending = data.pending;
  const allTriggered = data.triggered;
  const allResolved = data.resolved;

  const campaignPending = allPending.filter((t) => t.domain === "campaign");
  const campaignTriggered = allTriggered.filter((t) => t.domain === "campaign");
  const campaignResolved = allResolved.filter((t) => t.domain === "campaign");

  const scalpPending = allPending.filter((t) => t.domain === "scalp");
  const scalpTriggered = allTriggered.filter((t) => t.domain === "scalp");
  const scalpResolved = allResolved.filter((t) => t.domain === "scalp");

  return (
    <>
      <h2 className="text-xs uppercase tracking-widest text-gray-500 font-medium mb-3 flex items-center gap-2">
        <span>Theses</span>
        <span className="text-[9px] text-gray-600">forward-looking conditional plans</span>
      </h2>

      <DomainSection
        heading="Campaign Theses"
        icon="📌"
        color="text-blue-300"
        description="User + heartbeat + ai-brain. Plans tied to the main campaign system."
        stats={data.stats.campaign}
        pending={campaignPending}
        triggered={campaignTriggered}
        resolved={campaignResolved}
      />

      <DomainSection
        heading="Scalp Theses"
        icon="⚡"
        color="text-amber-300"
        description="Scalp brain's own learning corpus. Auto-proposed on LEAN states. Never touches real campaigns."
        stats={data.stats.scalp}
        pending={scalpPending}
        triggered={scalpTriggered}
        resolved={scalpResolved}
      />

      <div className="text-[9px] text-gray-600 text-right mt-4">
        Last update: {new Date(data.generated_at).toLocaleTimeString()}
      </div>
    </>
  );
};

export default ThesesTab;
