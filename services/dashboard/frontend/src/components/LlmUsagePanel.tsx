/**
 * LlmUsagePanel — live token + cost breakdown for every LLM call the bot makes.
 *
 * Reads /api/llm-usage which aggregates the llm_usage table (populated by
 * shared/llm_usage.py from every call site). Shows today's spend, a 24h
 * sparkline, per-call-site breakdown, cache savings, and heartbeat skip ratio.
 */

import React from "react";
import useApi from "../hooks/useApi";

interface Rollup {
  total_calls: number;
  success_calls: number;
  failed_calls: number;
  total_cost_usd: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_cache_read_tokens: number;
  total_cache_creation_tokens: number;
  cache_savings_usd: number;
  by_call_site: Array<{
    call_site: string;
    calls: number;
    cost: number;
    input_tokens: number;
    output_tokens: number;
    cache_read_tokens: number;
    failed: number;
  }>;
  by_model: Array<{ model: string; calls: number; cost: number }>;
  by_service: Array<{ service: string; calls: number; cost: number }>;
}

interface HourlyPoint {
  hour: string;
  cost: number;
  calls: number;
}

interface HeartbeatStat {
  skipped_unchanged: number;
  opus_called: number;
  total: number;
  skip_ratio: number;
}

interface LlmUsageData {
  today: Rollup;
  yesterday: Rollup;
  last_7d: Rollup;
  last_30d: Rollup;
  hourly_24h: HourlyPoint[];
  heartbeat_24h: HeartbeatStat;
  generated_at: string;
}

const fmt = (n: number, d = 2) =>
  n.toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d });

const fmtTokens = (n: number): string => {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return n.toString();
};

const Sparkline: React.FC<{ points: HourlyPoint[] }> = ({ points }) => {
  if (!points || points.length === 0) {
    return <div className="text-[9px] text-gray-600">no data</div>;
  }
  const max = Math.max(...points.map((p) => p.cost), 0.001);
  const width = 280;
  const height = 48;
  const step = width / Math.max(points.length - 1, 1);

  const pathData = points
    .map((p, i) => {
      const x = i * step;
      const y = height - (p.cost / max) * height;
      return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");

  return (
    <svg width={width} height={height} className="block">
      <defs>
        <linearGradient id="sparkGrad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#60a5fa" stopOpacity="0.4" />
          <stop offset="100%" stopColor="#60a5fa" stopOpacity="0" />
        </linearGradient>
      </defs>
      <path
        d={`${pathData} L${(points.length - 1) * step},${height} L0,${height} Z`}
        fill="url(#sparkGrad)"
      />
      <path d={pathData} fill="none" stroke="#60a5fa" strokeWidth="1.5" />
    </svg>
  );
};

const LlmUsagePanel: React.FC = () => {
  const { data } = useApi<LlmUsageData>("/api/llm-usage", { pollInterval: 30_000 });

  if (!data) {
    return (
      <div className="mb-6 bg-gray-900 border border-gray-800 rounded-xl p-4 animate-pulse h-64" />
    );
  }

  const today = data.today;
  const yesterday = data.yesterday;
  const hb = data.heartbeat_24h;

  const dayDelta = today.total_cost_usd - yesterday.total_cost_usd;
  const dayDeltaColor =
    dayDelta > 0.5 ? "text-red-400" : dayDelta < -0.5 ? "text-emerald-400" : "text-gray-400";
  const dayDeltaSign = dayDelta >= 0 ? "+" : "";

  return (
    <div className="mb-6">
      <h2 className="text-xs uppercase tracking-widest text-gray-500 font-medium mb-3 flex items-center gap-2">
        <span>LLM Usage</span>
        <span className="text-[9px] text-gray-600">token + cost tracker</span>
      </h2>
      <div className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
        {/* Top stats strip */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-px bg-gray-800">
          <div className="bg-gray-900 px-4 py-3">
            <div className="text-[9px] text-gray-500 uppercase tracking-wider">Today</div>
            <div className="text-2xl font-black text-white tabular-nums">
              ${fmt(today.total_cost_usd)}
            </div>
            <div className={`text-[10px] tabular-nums ${dayDeltaColor}`}>
              {dayDeltaSign}${fmt(dayDelta)} vs yesterday
            </div>
          </div>
          <div className="bg-gray-900 px-4 py-3">
            <div className="text-[9px] text-gray-500 uppercase tracking-wider">Calls today</div>
            <div className="text-2xl font-black text-white tabular-nums">
              {today.total_calls}
            </div>
            <div className="text-[10px] text-gray-400 tabular-nums">
              {today.failed_calls > 0 ? `${today.failed_calls} failed` : "all ok"}
            </div>
          </div>
          <div className="bg-gray-900 px-4 py-3">
            <div className="text-[9px] text-gray-500 uppercase tracking-wider">7d / 30d</div>
            <div className="text-sm font-bold text-white tabular-nums">
              ${fmt(data.last_7d.total_cost_usd)}
            </div>
            <div className="text-[10px] text-gray-400 tabular-nums">
              / ${fmt(data.last_30d.total_cost_usd)}
            </div>
          </div>
          <div className="bg-gray-900 px-4 py-3">
            <div className="text-[9px] text-gray-500 uppercase tracking-wider">Cache saved</div>
            <div className="text-sm font-bold text-emerald-300 tabular-nums">
              ${fmt(today.cache_savings_usd)}
            </div>
            <div className="text-[10px] text-gray-500 tabular-nums">today</div>
          </div>
        </div>

        {/* 24h sparkline */}
        <div className="px-4 py-3 border-t border-gray-800">
          <div className="flex items-center justify-between mb-1">
            <div className="text-[10px] uppercase tracking-wider text-gray-500">Last 24h</div>
            <div className="text-[10px] text-gray-600">
              {data.hourly_24h.length} hourly buckets
            </div>
          </div>
          <Sparkline points={data.hourly_24h} />
        </div>

        {/* Heartbeat skip ratio */}
        {hb.total > 0 && (
          <div className="px-4 py-2 border-t border-gray-800 flex items-center justify-between text-[11px]">
            <span className="text-gray-500 uppercase tracking-wider">🫀 Heartbeat 24h</span>
            <span className="text-gray-300 font-mono">
              {hb.skipped_unchanged}/{hb.total} skipped ·{" "}
              <span
                className={
                  hb.skip_ratio >= 0.5
                    ? "text-emerald-300 font-bold"
                    : hb.skip_ratio >= 0.25
                    ? "text-amber-300"
                    : "text-gray-400"
                }
              >
                {Math.round(hb.skip_ratio * 100)}% cache rate
              </span>
            </span>
          </div>
        )}

        {/* By call site */}
        <div className="px-4 py-3 border-t border-gray-800">
          <div className="text-[10px] uppercase tracking-wider text-gray-500 mb-2">
            Today by call site
          </div>
          {today.by_call_site.length === 0 ? (
            <div className="text-[11px] text-gray-600">no activity yet today</div>
          ) : (
            <div className="space-y-1">
              {today.by_call_site.map((site) => {
                const pct = today.total_cost_usd > 0 ? (site.cost / today.total_cost_usd) * 100 : 0;
                return (
                  <div key={site.call_site} className="text-[11px]">
                    <div className="flex items-center justify-between gap-2 mb-0.5 font-mono">
                      <span className="text-gray-300 truncate">{site.call_site}</span>
                      <span className="text-gray-500 text-[9px]">
                        {site.calls} calls · in {fmtTokens(site.input_tokens)} · out {fmtTokens(site.output_tokens)}
                        {site.cache_read_tokens > 0 ? ` · cache ${fmtTokens(site.cache_read_tokens)}` : ""}
                        {site.failed > 0 ? ` · ${site.failed} failed` : ""}
                      </span>
                      <span className="text-white font-bold tabular-nums w-16 text-right">
                        ${fmt(site.cost, 3)}
                      </span>
                    </div>
                    <div className="h-1 bg-gray-800 rounded overflow-hidden">
                      <div
                        className="h-full bg-blue-500"
                        style={{ width: `${Math.min(pct, 100)}%` }}
                      />
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {/* By model */}
        <div className="px-4 py-3 border-t border-gray-800">
          <div className="text-[10px] uppercase tracking-wider text-gray-500 mb-2">
            Today by model
          </div>
          {today.by_model.length === 0 ? (
            <div className="text-[11px] text-gray-600">—</div>
          ) : (
            <div className="grid grid-cols-2 md:grid-cols-3 gap-2 text-[11px] font-mono">
              {today.by_model.map((m) => (
                <div key={m.model} className="bg-gray-800/60 rounded px-2 py-1">
                  <div className="text-gray-300 truncate text-[10px]">{m.model}</div>
                  <div className="flex justify-between items-baseline mt-0.5">
                    <span className="text-gray-500">{m.calls} calls</span>
                    <span className="text-white font-bold">${fmt(m.cost, 3)}</span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="px-4 py-1 bg-gray-950/40 text-[9px] text-gray-600 font-mono flex items-center justify-between">
          <span>
            Tokens today: in {fmtTokens(today.total_input_tokens)} · out{" "}
            {fmtTokens(today.total_output_tokens)} · cache read{" "}
            {fmtTokens(today.total_cache_read_tokens)}
          </span>
          <span>upd {new Date(data.generated_at).toLocaleTimeString()}</span>
        </div>
      </div>
    </div>
  );
};

export default LlmUsagePanel;
