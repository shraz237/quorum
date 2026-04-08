import React, { useState } from "react";
import useApi from "../hooks/useApi";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface CampaignPosition {
  id: number;
  layer_index: number;
  entry_price: number;
  lots: number;
  margin_used: number;
  opened_at: string;
}

interface DcaPreviewRow {
  offset_pct: number;
  trigger_price: number;
  added_lots: number;
  added_margin: number;
  new_total_lots: number;
  new_avg_entry: number;
  new_total_margin: number;
  new_breakeven: number;
}

interface Campaign {
  id: number;
  side: "LONG" | "SHORT";
  status: string;
  opened_at: string;
  closed_at: string | null;
  avg_entry_price: number;
  total_lots: number;
  total_margin: number;
  total_nominal: number;
  layers_used: number;
  max_layers: number;
  next_layer_margin: number | null;
  current_price: number | null;
  unrealised_pnl: number;
  unrealised_pnl_pct: number;
  max_loss_pct: number;
  positions: CampaignPosition[];
  dca_preview?: DcaPreviewRow[];
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function fmtUsd(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "—";
  if (Math.abs(v) >= 1000) {
    return "$" + v.toLocaleString("en-US", { maximumFractionDigits: 0 });
  }
  return "$" + v.toFixed(2);
}

function signedUsd(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "—";
  return (v >= 0 ? "+" : "") + fmtUsd(v);
}

function fmtPrice(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "—";
  return "$" + v.toFixed(2);
}


function fmtTime(iso: string): string {
  return iso.replace("T", " ").substring(0, 16);
}

function pnlColor(v: number): string {
  return v >= 0 ? "text-green-400" : "text-red-400";
}

/** Per-layer unrealised PnL: (current - entry) * lots * 100 * direction */
function layerPnl(
  layer: CampaignPosition,
  currentPrice: number | null,
  side: "LONG" | "SHORT"
): number | null {
  if (currentPrice == null) return null;
  const direction = side === "SHORT" ? -1 : 1;
  return (currentPrice - layer.entry_price) * layer.lots * 100 * direction;
}

// ---------------------------------------------------------------------------
// MaxLoss bar
// ---------------------------------------------------------------------------

interface MaxLossBarProps {
  pnlPct: number;
  maxLossPct: number;
}

const MaxLossBar: React.FC<MaxLossBarProps> = ({ pnlPct, maxLossPct }) => {
  // Clamp fill: 0% to 100%
  const fillPct =
    maxLossPct > 0
      ? Math.min(100, Math.max(0, (-pnlPct / maxLossPct) * 100))
      : 0;

  let barColor = "bg-green-500";
  if (fillPct > 75) barColor = "bg-red-500";
  else if (fillPct > 40) barColor = "bg-yellow-400";

  return (
    <div className="mt-2">
      <div className="flex items-center justify-between mb-1">
        <span className="text-[10px] text-gray-500" title="Campaign PnL as % of its margin. Independent of account-level drawdown.">
          Layer margin stop ({maxLossPct}%)
        </span>
        <span className={`text-[10px] font-medium ${pnlColor(pnlPct)}`}>
          {pnlPct >= 0 ? "+" : ""}
          {pnlPct.toFixed(2)}% / -{maxLossPct}%
        </span>
      </div>
      <div className="h-2 bg-gray-800 rounded-full overflow-hidden">
        <div
          className={`h-full rounded-full transition-all duration-300 ${barColor}`}
          style={{ width: `${fillPct}%` }}
        />
      </div>
    </div>
  );
};

// ---------------------------------------------------------------------------
// CampaignCard
// ---------------------------------------------------------------------------

interface CampaignCardProps {
  campaign: Campaign;
  onRefetch: () => void;
}

const CampaignCard: React.FC<CampaignCardProps> = ({ campaign, onRefetch }) => {
  const [expanded, setExpanded] = useState(false);
  const [actionLoading, setActionLoading] = useState<"dca" | "close" | null>(null);

  const c = campaign;
  const isMaxLayers = c.layers_used >= c.max_layers;

  const handleDca = async () => {
    const ok = confirm(
      `Add next DCA layer to campaign #${c.id} ${c.side}? (Layer ${c.layers_used + 1} of ${c.max_layers})`
    );
    if (!ok) return;
    setActionLoading("dca");
    try {
      const res = await fetch(`/api/campaigns/${c.id}/dca`, { method: "POST" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      onRefetch();
    } catch (e) {
      alert(`DCA failed: ${e}`);
    } finally {
      setActionLoading(null);
    }
  };

  const handleClose = async () => {
    const ok = confirm(
      `Close campaign #${c.id} ${c.side} (${c.layers_used} layers, ${signedUsd(c.unrealised_pnl)})?`
    );
    if (!ok) return;
    setActionLoading("close");
    try {
      const res = await fetch(`/api/campaigns/${c.id}/close`, { method: "POST" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      onRefetch();
    } catch (e) {
      alert(`Close failed: ${e}`);
    } finally {
      setActionLoading(null);
    }
  };

  const layerBarPct = Math.round((c.layers_used / c.max_layers) * 100);

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
      {/* Header row */}
      <div
        className="flex flex-wrap items-center gap-3 p-3 cursor-pointer hover:bg-gray-800/40 select-none transition"
        onClick={() => setExpanded((v) => !v)}
      >
        {/* Side badge */}
        <span
          className={`px-2 py-0.5 rounded border text-[10px] font-bold ${
            c.side === "LONG"
              ? "bg-green-900/40 text-green-300 border-green-700"
              : "bg-red-900/40 text-red-300 border-red-700"
          }`}
        >
          {c.side}
        </span>

        {/* Campaign ID */}
        <span className="text-sm font-semibold text-gray-300">
          Campaign #{c.id}
        </span>

        {/* Opened time */}
        <span className="text-[10px] text-gray-500">{fmtTime(c.opened_at)}</span>

        {/* Prices */}
        <div className="flex items-center gap-1.5 text-xs text-gray-400">
          <span>avg {fmtPrice(c.avg_entry_price)}</span>
          {c.current_price != null && (
            <>
              <span className="text-gray-700">→</span>
              <span className="text-gray-200">{fmtPrice(c.current_price)}</span>
            </>
          )}
        </div>

        {/* PnL */}
        <span
          className={`text-sm font-bold ml-auto md:ml-0 ${pnlColor(c.unrealised_pnl)}`}
        >
          {signedUsd(c.unrealised_pnl)}{" "}
          <span className="text-xs font-medium opacity-80">
            ({c.unrealised_pnl_pct >= 0 ? "+" : ""}
            {c.unrealised_pnl_pct.toFixed(2)}%)
          </span>
        </span>

        {/* Layers progress */}
        <div className="flex flex-col gap-0.5 min-w-[80px]">
          <span className="text-[10px] text-gray-500">
            Layers {c.layers_used}/{c.max_layers}
          </span>
          <div className="h-1.5 bg-gray-800 rounded-full overflow-hidden">
            <div
              className={`h-full rounded-full ${
                isMaxLayers ? "bg-red-500" : "bg-blue-500"
              }`}
              style={{ width: `${layerBarPct}%` }}
            />
          </div>
        </div>

        {/* Action buttons — stop click propagation so they don't toggle expand */}
        <div
          className="flex items-center gap-2 ml-auto"
          onClick={(e) => e.stopPropagation()}
        >
          <button
            onClick={handleDca}
            disabled={isMaxLayers || actionLoading !== null}
            className={`px-3 py-1 text-xs rounded font-medium transition ${
              isMaxLayers
                ? "bg-gray-800 text-gray-600 cursor-not-allowed"
                : "bg-blue-700 text-white hover:bg-blue-600 disabled:opacity-50"
            }`}
          >
            {actionLoading === "dca" ? "…" : "+ DCA"}
          </button>
          <button
            onClick={handleClose}
            disabled={actionLoading !== null}
            className="px-3 py-1 text-xs rounded font-medium bg-red-900/60 text-red-300 hover:bg-red-800 transition disabled:opacity-50"
          >
            {actionLoading === "close" ? "…" : "Close"}
          </button>
        </div>

        {/* Expand chevron */}
        <span className="text-gray-600 text-xs" onClick={(e) => e.stopPropagation()}>
          <span
            className={`inline-block transition-transform duration-200 cursor-pointer ${
              expanded ? "rotate-180" : ""
            }`}
            onClick={() => setExpanded((v) => !v)}
          >
            ▼
          </span>
        </span>
      </div>

      {/* Expanded detail */}
      {expanded && (
        <div className="border-t border-gray-800 p-4 space-y-4">
          {/* Summary stats */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <div className="bg-gray-800/60 rounded-lg p-2">
              <span className="text-[10px] uppercase tracking-widest text-gray-500">
                Total Lots
              </span>
              <div className="text-sm font-semibold text-gray-200 mt-0.5">
                {c.total_lots.toFixed(2)}
              </div>
            </div>
            <div className="bg-gray-800/60 rounded-lg p-2">
              <span className="text-[10px] uppercase tracking-widest text-gray-500">
                Total Margin
              </span>
              <div className="text-sm font-semibold text-gray-200 mt-0.5">
                {fmtUsd(c.total_margin)}
              </div>
            </div>
            <div className="bg-gray-800/60 rounded-lg p-2">
              <span className="text-[10px] uppercase tracking-widest text-gray-500">
                Nominal Exposure
              </span>
              <div className="text-sm font-semibold text-gray-200 mt-0.5">
                {fmtUsd(c.total_nominal)}
              </div>
            </div>
            <div className="bg-gray-800/60 rounded-lg p-2">
              <span className="text-[10px] uppercase tracking-widest text-gray-500">
                Next Layer Margin
              </span>
              <div className="text-sm font-semibold text-gray-200 mt-0.5">
                {c.next_layer_margin != null ? fmtUsd(c.next_layer_margin) : "—"}
              </div>
            </div>
          </div>

          {/* Max loss bar */}
          <MaxLossBar
            pnlPct={c.unrealised_pnl_pct}
            maxLossPct={c.max_loss_pct}
          />

          {/* Next DCA Preview — simulated outcomes at several price levels */}
          {c.dca_preview && c.dca_preview.length > 0 && (
            <div>
              <div className="flex items-baseline justify-between mb-1">
                <span className="text-[10px] uppercase tracking-widest text-gray-500">
                  Next DCA Layer Preview (+${c.next_layer_margin?.toFixed(0)} margin)
                </span>
                <span className="text-[10px] text-gray-600">
                  simulated outcomes
                </span>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-[11px]">
                  <thead>
                    <tr className="text-gray-500 border-b border-gray-800">
                      <th className="text-left py-1 pr-3">Trigger</th>
                      <th className="text-right py-1 pr-3">+ Lots</th>
                      <th className="text-right py-1 pr-3">New Total Lots</th>
                      <th className="text-right py-1 pr-3">New Avg</th>
                      <th className="text-right py-1 pr-3">New Margin</th>
                      <th className="text-right py-1">Breakeven</th>
                    </tr>
                  </thead>
                  <tbody>
                    {c.dca_preview.map((row, i) => {
                      const isCurrent = row.offset_pct === 0;
                      return (
                        <tr
                          key={i}
                          className={`border-b border-gray-900 last:border-0 ${
                            isCurrent ? "bg-gray-800/40" : ""
                          }`}
                        >
                          <td className="py-1 pr-3">
                            <span className={`font-semibold ${
                              row.offset_pct === 0
                                ? "text-blue-300"
                                : "text-gray-300"
                            }`}>
                              {fmtPrice(row.trigger_price)}
                            </span>{" "}
                            <span className="text-[10px] text-gray-600">
                              {row.offset_pct === 0
                                ? "now"
                                : (row.offset_pct > 0 ? "+" : "") + row.offset_pct + "%"}
                            </span>
                          </td>
                          <td className="py-1 pr-3 text-right text-gray-400">
                            {row.added_lots.toFixed(3)}
                          </td>
                          <td className="py-1 pr-3 text-right text-gray-200 font-medium">
                            {row.new_total_lots.toFixed(3)}
                          </td>
                          <td className="py-1 pr-3 text-right text-gray-100 font-semibold">
                            {fmtPrice(row.new_avg_entry)}
                          </td>
                          <td className="py-1 pr-3 text-right text-gray-300">
                            {fmtUsd(row.new_total_margin)}
                          </td>
                          <td className="py-1 text-right text-yellow-400">
                            {fmtPrice(row.new_breakeven)}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              <div className="text-[10px] text-gray-600 mt-1">
                {c.side === "LONG"
                  ? "Dips (negative offsets) improve your avg entry and lower breakeven"
                  : "Rallies (positive offsets) improve your avg entry and raise breakeven"}
              </div>
            </div>
          )}

          {/* DCA layer table */}
          {c.positions.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-gray-500 border-b border-gray-800">
                    <th className="text-left py-1.5 pr-3">Layer</th>
                    <th className="text-right py-1.5 pr-3">Entry</th>
                    <th className="text-right py-1.5 pr-3">Lots</th>
                    <th className="text-right py-1.5 pr-3">Margin</th>
                    <th className="text-right py-1.5 pr-3">PnL</th>
                    <th className="text-left py-1.5">Time</th>
                  </tr>
                </thead>
                <tbody>
                  {c.positions
                    .slice()
                    .sort((a, b) => a.layer_index - b.layer_index)
                    .map((pos) => {
                      const pnl = layerPnl(pos, c.current_price, c.side);
                      return (
                        <tr
                          key={pos.id}
                          className="border-b border-gray-800/60 hover:bg-gray-800/30"
                        >
                          <td className="py-1.5 pr-3 text-gray-400 font-medium">
                            #{pos.layer_index + 1}
                          </td>
                          <td className="py-1.5 pr-3 text-right text-gray-300">
                            {fmtPrice(pos.entry_price)}
                          </td>
                          <td className="py-1.5 pr-3 text-right text-gray-300">
                            {pos.lots.toFixed(2)}
                          </td>
                          <td className="py-1.5 pr-3 text-right text-gray-300">
                            {fmtUsd(pos.margin_used)}
                          </td>
                          <td
                            className={`py-1.5 pr-3 text-right font-semibold ${
                              pnl == null ? "text-gray-500" : pnlColor(pnl)
                            }`}
                          >
                            {pnl == null
                              ? "—"
                              : `${signedUsd(pnl)}`}
                          </td>
                          <td className="py-1.5 text-gray-500">
                            {fmtTime(pos.opened_at)}
                          </td>
                        </tr>
                      );
                    })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
};

// ---------------------------------------------------------------------------
// CampaignsPanel
// ---------------------------------------------------------------------------

const CampaignsPanel: React.FC = () => {
  const { data, loading, error, refetch } = useApi<Campaign[]>(
    "/api/campaigns?status=open",
    { pollInterval: 5_000 }
  );

  return (
    <section className="mb-6">
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-xs uppercase tracking-widest text-gray-500 font-semibold">
          Open Campaigns
        </h2>
        {data && data.length > 0 && (
          <span className="text-[10px] text-gray-600">
            {data.length} campaign{data.length !== 1 ? "s" : ""} active
          </span>
        )}
      </div>

      {loading && !data && (
        <div className="space-y-3">
          {[0, 1].map((i) => (
            <div
              key={i}
              className="bg-gray-900 border border-gray-800 rounded-xl h-14 animate-pulse"
            />
          ))}
        </div>
      )}

      {error && !data && (
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-4 text-red-400 text-xs">
          Campaigns unavailable: {error}
        </div>
      )}

      {data && data.length === 0 && (
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-6 text-center text-gray-600 text-sm">
          No open campaigns
        </div>
      )}

      {data && data.length > 0 && (
        <div className="space-y-3">
          {data.map((campaign) => (
            <CampaignCard
              key={campaign.id}
              campaign={campaign}
              onRefetch={refetch}
            />
          ))}
        </div>
      )}
    </section>
  );
};

export default CampaignsPanel;
