/**
 * RiskToolsPanel — risk calculators row.
 *
 *   1. Scenario Calculator — what happens to PnL/equity/margin at various prices
 *   2. Monte Carlo — GBM simulation of margin call probability
 *   3. VWAP & Pivot — where is price relative to value
 *   4. Events Calendar — upcoming high-impact events
 */

import React from "react";
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
// 1. Scenario Calculator
// ---------------------------------------------------------------------------

interface ScenarioRow {
  offset_pct: number;
  price: number;
  pnl: number;
  equity: number;
  free_margin: number;
  margin_level_pct: number | null;
  drawdown_pct: number;
  status: string;
}

interface ScenarioResponse {
  current_price: number;
  current_equity: number;
  current_margin_used: number;
  current_cash: number;
  starting_balance: number;
  side_bias: "LONG" | "SHORT" | null;
  total_lots: number;
  key_levels: {
    breakeven?: number;
    stop_out_price?: number;
    half_drawdown_price?: number;
    distance_to_stop_out_pct?: number;
    distance_to_half_dd_pct?: number;
  };
  scenarios: ScenarioRow[];
}

function statusColor(status: string): string {
  switch (status) {
    case "MARGIN_CALL": return "text-red-400 font-bold";
    case "DANGER": return "text-red-400";
    case "ELEVATED": return "text-orange-400";
    case "SAFE": return "text-green-400";
    default: return "text-gray-500";
  }
}

const ScenarioCard: React.FC = () => {
  const { data } = useApi<ScenarioResponse>("/api/scenario-calculator", {
    pollInterval: 10_000,
  });

  if (!data) {
    return <Card title="Scenario Calculator"><div className="text-gray-600 text-xs">loading…</div></Card>;
  }
  if (!data.side_bias) {
    return (
      <Card title="Scenario Calculator">
        <div className="text-gray-500 text-xs italic py-4">
          No open positions — open a campaign to see price scenarios.
        </div>
      </Card>
    );
  }

  const k = data.key_levels;

  return (
    <Card title="Scenario Calculator" subtitle={`${data.side_bias} @ ${data.current_price.toFixed(2)}`}>
      {/* Key levels */}
      <div className="grid grid-cols-3 gap-2 text-[10px]">
        <div className="bg-gray-800/50 rounded px-2 py-1">
          <div className="text-gray-500 uppercase">Breakeven</div>
          <div className="text-blue-300 font-bold text-sm">${k.breakeven?.toFixed(2)}</div>
        </div>
        <div className="bg-red-950/40 rounded px-2 py-1 border border-red-900/40">
          <div className="text-red-400 uppercase">Stop-out</div>
          <div className="text-red-300 font-bold text-sm">${k.stop_out_price?.toFixed(2)}</div>
          <div className="text-red-500/80 text-[9px]">
            {k.distance_to_stop_out_pct != null ? `${k.distance_to_stop_out_pct > 0 ? "+" : ""}${k.distance_to_stop_out_pct.toFixed(2)}%` : ""}
          </div>
        </div>
        <div className="bg-red-950/60 rounded px-2 py-1 border border-red-900">
          <div className="text-red-400 uppercase">-50% HS</div>
          <div className="text-red-300 font-bold text-sm">${k.half_drawdown_price?.toFixed(2)}</div>
          <div className="text-red-500/80 text-[9px]">
            {k.distance_to_half_dd_pct != null ? `${k.distance_to_half_dd_pct > 0 ? "+" : ""}${k.distance_to_half_dd_pct.toFixed(2)}%` : ""}
          </div>
        </div>
      </div>

      {/* Scenario table */}
      <div className="overflow-x-auto mt-1">
        <table className="w-full text-[10px] font-mono">
          <thead>
            <tr className="text-gray-500 border-b border-gray-800">
              <th className="text-right py-1 pr-2">Move</th>
              <th className="text-right py-1 pr-2">Price</th>
              <th className="text-right py-1 pr-2">PnL</th>
              <th className="text-right py-1 pr-2">Equity</th>
              <th className="text-right py-1 pr-2">ML%</th>
              <th className="text-left py-1">Status</th>
            </tr>
          </thead>
          <tbody>
            {data.scenarios.map((s, i) => {
              const isCurrent = s.offset_pct === 0;
              return (
                <tr
                  key={i}
                  className={`border-b border-gray-900 last:border-0 ${isCurrent ? "bg-blue-950/30" : ""}`}
                >
                  <td className={`py-1 pr-2 text-right ${s.offset_pct > 0 ? "text-green-400" : s.offset_pct < 0 ? "text-red-400" : "text-blue-300"}`}>
                    {s.offset_pct > 0 ? "+" : ""}{s.offset_pct}%
                  </td>
                  <td className="py-1 pr-2 text-right text-gray-200">${s.price.toFixed(2)}</td>
                  <td className={`py-1 pr-2 text-right ${s.pnl >= 0 ? "text-green-400" : "text-red-400"}`}>
                    {signedUsd(s.pnl)}
                  </td>
                  <td className="py-1 pr-2 text-right text-gray-200">{fmtUsd(s.equity)}</td>
                  <td className="py-1 pr-2 text-right text-gray-400">
                    {s.margin_level_pct != null ? `${s.margin_level_pct.toFixed(0)}` : "—"}
                  </td>
                  <td className={`py-1 text-[9px] ${statusColor(s.status)}`}>
                    {s.status}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Card>
  );
};

// ---------------------------------------------------------------------------
// 2. Monte Carlo
// ---------------------------------------------------------------------------

interface MonteCarloResponse {
  horizon_hours: number;
  n_paths: number;
  sigma_hourly_pct: number;
  current_equity: number;
  prob_margin_call: number;
  prob_half_dd: number;
  expected_equity: number;
  p5_equity: number;
  p50_equity: number;
  p95_equity: number;
  worst_equity: number;
  best_equity: number;
  note?: string;
  error?: string;
}

const MonteCarloCard: React.FC = () => {
  const { data } = useApi<MonteCarloResponse>("/api/monte-carlo?horizon_hours=24&n_paths=2000", {
    pollInterval: 60_000,
  });

  if (!data) {
    return <Card title="Monte Carlo"><div className="text-gray-600 text-xs">loading…</div></Card>;
  }
  if (data.error || data.note === "no open position") {
    return (
      <Card title="Monte Carlo" subtitle="24h horizon">
        <div className="text-gray-500 text-xs italic py-4">{data.error || data.note}</div>
      </Card>
    );
  }

  const mcRiskColor =
    data.prob_margin_call >= 20 ? "text-red-400" : data.prob_margin_call >= 10 ? "text-orange-400" : data.prob_margin_call >= 5 ? "text-yellow-400" : "text-green-400";

  return (
    <Card title="Monte Carlo" subtitle={`${data.horizon_hours}h · ${data.n_paths} paths`}>
      <div className="grid grid-cols-2 gap-2 text-xs">
        <div className="bg-gray-800/50 rounded p-2">
          <div className="text-[10px] text-gray-500 uppercase">P(Margin Call)</div>
          <div className={`text-2xl font-bold ${mcRiskColor}`}>{data.prob_margin_call.toFixed(1)}%</div>
          <div className="text-[9px] text-gray-600">in {data.horizon_hours}h</div>
        </div>
        <div className="bg-gray-800/50 rounded p-2">
          <div className="text-[10px] text-gray-500 uppercase">P(-50% HS)</div>
          <div className={`text-2xl font-bold ${data.prob_half_dd >= 5 ? "text-red-400" : "text-emerald-400"}`}>
            {data.prob_half_dd.toFixed(1)}%
          </div>
          <div className="text-[9px] text-gray-600">hard stop hit</div>
        </div>
      </div>

      <div className="border-t border-gray-800 pt-1 mt-1">
        <div className="text-[10px] uppercase text-gray-500 mb-1">Equity in {data.horizon_hours}h (percentiles)</div>
        <div className="space-y-0.5 text-[10px] font-mono">
          <div className="flex justify-between"><span className="text-red-400">P5 (worst 5%)</span><span className="text-red-300">{fmtUsd(data.p5_equity)}</span></div>
          <div className="flex justify-between"><span className="text-gray-400">Median</span><span className="text-gray-200">{fmtUsd(data.p50_equity)}</span></div>
          <div className="flex justify-between"><span className="text-green-400">P95 (best 5%)</span><span className="text-green-300">{fmtUsd(data.p95_equity)}</span></div>
        </div>
        <div className="text-[9px] text-gray-600 pt-1">
          Hourly σ = {data.sigma_hourly_pct.toFixed(3)}% from 7d history
        </div>
      </div>
    </Card>
  );
};

// ---------------------------------------------------------------------------
// 3. VWAP
// ---------------------------------------------------------------------------

interface VwapResponse {
  timeframe: string;
  hours: number;
  bar_count: number;
  vwap: number;
  current_price: number;
  distance_points: number;
  distance_pct: number | null;
  price_vs_vwap: "above" | "below";
}

const VwapCard: React.FC = () => {
  const { data: vwap24 } = useApi<VwapResponse>("/api/vwap?timeframe=1H&hours=24", { pollInterval: 30_000 });
  const { data: vwap168 } = useApi<VwapResponse>("/api/vwap?timeframe=1H&hours=168", { pollInterval: 60_000 });

  if (!vwap24) {
    return <Card title="VWAP"><div className="text-gray-600 text-xs">loading…</div></Card>;
  }

  const Row: React.FC<{ label: string; data: VwapResponse | null }> = ({ label, data }) => {
    if (!data) return null;
    const above = data.price_vs_vwap === "above";
    const color = above ? "text-green-400" : "text-red-400";
    const dp = data.distance_pct ?? 0;
    return (
      <div className="flex items-center justify-between text-xs">
        <span className="text-gray-500 text-[10px] uppercase w-12">{label}</span>
        <span className="text-gray-200 font-mono">${data.vwap.toFixed(3)}</span>
        <span className={`font-mono ${color}`}>
          {above ? "+" : ""}{dp.toFixed(2)}%
        </span>
      </div>
    );
  };

  const current = vwap24.current_price;

  return (
    <Card title="VWAP" subtitle="session · week">
      <div className="text-center py-2">
        <div className="text-[10px] text-gray-500 uppercase">Current</div>
        <div className="text-2xl font-bold text-gray-100">${current.toFixed(3)}</div>
      </div>
      <div className="flex flex-col gap-1 border-t border-gray-800 pt-2">
        <Row label="24h" data={vwap24} />
        <Row label="7d" data={vwap168 ?? null} />
      </div>
      <div className="text-[9px] text-gray-600 mt-1 leading-tight">
        Price above VWAP = buyers in control. Below = sellers. Mean-reversion candidates when &gt; 1% away.
      </div>
    </Card>
  );
};

// ---------------------------------------------------------------------------
// 4. Events Calendar
// ---------------------------------------------------------------------------

interface CalendarEvent {
  date: string;
  time_utc: string;
  event: string;
  importance: string;
  note: string;
}

interface CalendarResponse {
  events: CalendarEvent[];
  event_count: number;
  window_days: number;
}

function eventCountdown(dateStr: string, timeStr: string): { text: string; urgent: boolean } {
  const dt = new Date(`${dateStr}T${timeStr}:00Z`);
  const ms = dt.getTime() - Date.now();
  if (ms < 0) return { text: "past", urgent: false };
  const hours = Math.floor(ms / 3_600_000);
  const mins = Math.floor((ms % 3_600_000) / 60_000);
  const days = Math.floor(hours / 24);
  const text = days >= 1 ? `${days}d ${hours % 24}h` : hours >= 1 ? `${hours}h ${mins}m` : `${mins}m`;
  return { text, urgent: ms < 2 * 3_600_000 };  // <2h → urgent
}

function importanceColor(imp: string): string {
  if (imp === "HIGH") return "text-red-400 bg-red-950/40 border-red-900/50";
  if (imp === "MEDIUM") return "text-yellow-400 bg-yellow-950/30 border-yellow-900/40";
  return "text-gray-400 bg-gray-800/40 border-gray-800";
}

const EventsCalendarCard: React.FC = () => {
  const { data } = useApi<CalendarResponse>("/api/upcoming-events?days=14", { pollInterval: 300_000 });

  if (!data) {
    return <Card title="Events Calendar"><div className="text-gray-600 text-xs">loading…</div></Card>;
  }
  const events = data.events || [];
  const now = events.length > 0 ? events.slice(0, 6) : [];

  return (
    <Card title="Events Calendar" subtitle="next 14d · high-impact">
      {now.length === 0 ? (
        <div className="text-[11px] text-gray-500 italic py-4 text-center">
          No high-impact events scheduled.
        </div>
      ) : (
        <div className="flex flex-col gap-1 overflow-y-auto max-h-60 pr-1">
          {now.map((e, i) => {
            const cd = eventCountdown(e.date, e.time_utc);
            return (
              <div
                key={i}
                className={`border rounded p-2 ${importanceColor(e.importance)}`}
              >
                <div className="flex items-baseline justify-between gap-2">
                  <span className="text-xs font-bold truncate">{e.event}</span>
                  <span className={`text-[10px] font-mono ${cd.urgent ? "text-red-300 font-bold" : ""}`}>
                    {cd.urgent && "⏱ "}{cd.text}
                  </span>
                </div>
                <div className="text-[9px] text-gray-500 mt-0.5">
                  {e.date} {e.time_utc} UTC
                </div>
                <div className="text-[10px] text-gray-400 mt-0.5 leading-tight">{e.note}</div>
              </div>
            );
          })}
        </div>
      )}
    </Card>
  );
};

// ---------------------------------------------------------------------------
// Main panel
// ---------------------------------------------------------------------------

const RiskToolsPanel: React.FC = () => (
  <div className="mb-6">
    <h2 className="text-xs uppercase tracking-widest text-gray-500 font-medium mb-3">
      Risk & Scenario Tools
    </h2>
    <div className="grid grid-cols-1 lg:grid-cols-4 gap-3">
      <ScenarioCard />
      <MonteCarloCard />
      <VwapCard />
      <EventsCalendarCard />
    </div>
  </div>
);

export default RiskToolsPanel;
