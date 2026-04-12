import React from "react";
import useApi from "../hooks/useApi";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface AccountState {
  starting_balance: number;
  cash: number;
  equity: number;
  margin_used: number;
  free_margin: number;
  margin_level_pct: number | null;
  realized_pnl_total: number;
  unrealised_pnl: number;
  account_drawdown_pct: number;
  account_hard_stop_pct: number;
  open_campaigns: number;
  leverage: number;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Format dollar amounts: 0 decimals for >=1000, 2 decimals otherwise */
function fmtUsd(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "—";
  if (Math.abs(v) >= 1000) {
    return "$" + v.toLocaleString("en-US", { maximumFractionDigits: 0 });
  }
  return "$" + v.toFixed(2);
}

function signedUsd(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "—";
  const prefix = v >= 0 ? "+" : "";
  return prefix + fmtUsd(v);
}

function marginLevelColor(pct: number | null): string {
  if (pct == null) return "text-gray-500";
  if (pct > 500) return "text-green-400";
  if (pct >= 200) return "text-yellow-400";
  return "text-red-400";
}

function pnlColor(v: number): string {
  return v >= 0 ? "text-green-400" : "text-red-400";
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

interface CardProps {
  label: string;
  children: React.ReactNode;
  accent?: boolean;
}

const Card: React.FC<CardProps> = ({ label, children, accent }) => (
  <div
    className={`rounded-lg p-3 flex flex-col gap-1 ${
      accent
        ? "bg-gray-800 border border-gray-700"
        : "bg-gray-900 border border-gray-800"
    }`}
  >
    <span className="text-[10px] uppercase tracking-widest text-gray-500 font-medium">
      {label}
    </span>
    {children}
  </div>
);

// ---------------------------------------------------------------------------
// AccountPanel
// ---------------------------------------------------------------------------

const AccountPanel: React.FC = () => {
  const { data, loading, error } = useApi<AccountState>("/api/account", {
    pollInterval: 5_000,
  });

  if (loading && !data) {
    return (
      <div className="mb-6 grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3 animate-pulse">
        {Array.from({ length: 6 }).map((_, i) => (
          <div key={i} className="bg-gray-900 border border-gray-800 rounded-lg h-16" />
        ))}
      </div>
    );
  }

  if (error && !data) {
    return (
      <div className="mb-6 bg-gray-900 border border-gray-800 rounded-lg p-3 text-red-400 text-xs">
        Account data unavailable: {error}
      </div>
    );
  }

  if (!data) return null;

  const equity = data.equity ?? 0;
  const startBal = data.starting_balance ?? 50000;
  const equityDelta = equity - startBal;
  const equityDeltaPct = startBal !== 0 ? (equityDelta / startBal) * 100 : 0;

  // Distance to the -50% account hard stop, as a bar.
  const hardStopPct = Math.abs(data.account_hard_stop_pct || 50);
  const ddPct = data.account_drawdown_pct ?? 0;
  const hardStopFill =
    ddPct >= 0 ? 0 : Math.min(100, (Math.abs(ddPct) / hardStopPct) * 100);
  const hardStopColor =
    hardStopFill > 75 ? "bg-red-500" : hardStopFill > 40 ? "bg-yellow-400" : "bg-green-500";

  return (
    <div className="mb-6 grid grid-cols-2 md:grid-cols-3 lg:grid-cols-7 gap-3">
      {/* Equity — large, highlighted */}
      <Card label="Equity" accent>
        <span className={`text-lg font-bold leading-tight ${pnlColor(equityDelta)}`}>
          {fmtUsd(data.equity)}
        </span>
        <span className={`text-xs font-medium ${pnlColor(equityDelta)}`}>
          {signedUsd(equityDelta)}{" "}
          <span className="text-gray-500">
            ({equityDelta >= 0 ? "+" : ""}
            {equityDeltaPct.toFixed(1)}%)
          </span>
        </span>
      </Card>

      {/* Cash — wallet balance (starting + realized PnL), NOT reduced by margin */}
      <Card label="Cash">
        <span className="text-base font-semibold text-gray-100">
          {fmtUsd(data.cash)}
        </span>
        <span className="text-[10px] text-gray-500">wallet balance</span>
      </Card>

      {/* Margin Used */}
      <Card label="Margin Used">
        <span className="text-base font-semibold text-gray-100">
          {fmtUsd(data.margin_used)}
        </span>
        {data.margin_level_pct != null && (
          <span className={`text-xs font-medium ${marginLevelColor(data.margin_level_pct)}`}>
            {data.margin_level_pct.toFixed(1)}% level
          </span>
        )}
      </Card>

      {/* Free Margin */}
      <Card label="Free Margin">
        <span className="text-base font-semibold text-gray-100">
          {fmtUsd(data.free_margin)}
        </span>
        <span className="text-[10px] text-gray-500">x{data.leverage} leverage</span>
      </Card>

      {/* Realized PnL */}
      <Card label="Realized PnL">
        <span
          className={`text-base font-semibold ${pnlColor(data.realized_pnl_total)}`}
        >
          {signedUsd(data.realized_pnl_total)}
        </span>
        <span className="text-[10px] text-gray-500">all-time</span>
      </Card>

      {/* Open Campaigns */}
      <Card label="Open Campaigns">
        <span className="text-2xl font-bold text-gray-100">
          {data.open_campaigns}
        </span>
        <span className="text-[10px] text-gray-500">active</span>
      </Card>

      {/* Equity delta — drawdown when negative, P&L from start when positive */}
      <Card label={ddPct >= 0 ? "Equity Δ" : "Drawdown"}>
        <span className={`text-base font-semibold ${pnlColor(ddPct)}`}>
          {ddPct >= 0 ? "+" : ""}
          {ddPct.toFixed(2)}%
        </span>
        <div className="mt-1 h-1.5 bg-gray-800 rounded-full overflow-hidden">
          <div
            className={`h-full rounded-full transition-all duration-300 ${hardStopColor}`}
            style={{ width: `${hardStopFill}%` }}
          />
        </div>
        <span className="text-[10px] text-gray-500">
          {ddPct >= 0
            ? `${hardStopPct.toFixed(0)}% hard stop far away`
            : `${Math.abs(ddPct).toFixed(2)}% / ${hardStopPct.toFixed(0)}% hard stop`}
        </span>
      </Card>
    </div>
  );
};

export default AccountPanel;
