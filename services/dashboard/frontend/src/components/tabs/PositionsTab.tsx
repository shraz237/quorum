/**
 * PositionsTab — dual-persona position management.
 *
 * Two visually distinct sections, each with its own account panel
 * and campaign list:
 *
 *   Main Trader   — conservative DCA campaigns (blue accent)
 *   Scalper       — fast auto-traded scalps (amber accent)
 *
 * Plus the shared RiskToolsPanel at the bottom.
 */

import React from "react";
import useApi from "../../hooks/useApi";
import CampaignsPanel from "../CampaignsPanel";
import RiskToolsPanel from "../RiskToolsPanel";

interface AccountState {
  persona: string;
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

const fmt = (n: number, d = 0) =>
  n.toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d });

const PersonaAccountStrip: React.FC<{
  title: string;
  icon: string;
  accent: string;
  accentBg: string;
  data: AccountState | null;
}> = ({ title, icon, accent, accentBg, data }) => {
  if (!data) {
    return (
      <div className={`${accentBg} border rounded-xl p-4 animate-pulse h-24`} />
    );
  }

  const dd = data.account_drawdown_pct;
  const ddColor =
    dd <= -30 ? "text-red-400" : dd <= -15 ? "text-amber-400" : dd >= 5 ? "text-emerald-300" : "text-gray-300";
  const pnlColor = data.unrealised_pnl >= 0 ? "text-emerald-300" : "text-red-300";
  const realColor = data.realized_pnl_total >= 0 ? "text-emerald-300" : "text-red-300";

  return (
    <div className={`${accentBg} border border-gray-800 rounded-xl p-4`}>
      <div className="flex items-baseline gap-2 mb-3">
        <span className="text-lg">{icon}</span>
        <span className={`text-sm font-bold ${accent}`}>{title}</span>
        <span className="text-[9px] text-gray-600 ml-auto">
          ${fmt(data.starting_balance)} starting · x{data.leverage} leverage
        </span>
      </div>
      <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-7 gap-3 text-[11px] font-mono">
        <div>
          <div className="text-gray-500 uppercase text-[9px]">Equity</div>
          <div className="text-white font-bold text-lg tabular-nums">${fmt(data.equity)}</div>
        </div>
        <div>
          <div className="text-gray-500 uppercase text-[9px]">Cash</div>
          <div className="text-gray-200 tabular-nums">${fmt(data.cash)}</div>
        </div>
        <div>
          <div className="text-gray-500 uppercase text-[9px]">Open P/L</div>
          <div className={`font-bold tabular-nums ${pnlColor}`}>
            {data.unrealised_pnl >= 0 ? "+" : ""}${fmt(data.unrealised_pnl)}
          </div>
        </div>
        <div>
          <div className="text-gray-500 uppercase text-[9px]">Realized</div>
          <div className={`tabular-nums ${realColor}`}>
            {data.realized_pnl_total >= 0 ? "+" : ""}${fmt(data.realized_pnl_total)}
          </div>
        </div>
        <div>
          <div className="text-gray-500 uppercase text-[9px]">Margin</div>
          <div className="text-gray-300 tabular-nums">${fmt(data.margin_used)}</div>
        </div>
        <div>
          <div className="text-gray-500 uppercase text-[9px]">Free</div>
          <div className="text-gray-300 tabular-nums">${fmt(data.free_margin)}</div>
        </div>
        <div>
          <div className="text-gray-500 uppercase text-[9px]">Drawdown</div>
          <div className={`font-bold tabular-nums ${ddColor}`}>
            {dd >= 0 ? "+" : ""}{dd.toFixed(1)}%
          </div>
          <div className="text-[9px] text-gray-600">{data.open_campaigns} campaign{data.open_campaigns !== 1 ? "s" : ""}</div>
        </div>
      </div>
    </div>
  );
};

const PositionsTab: React.FC = () => {
  const { data: mainAccount } = useApi<AccountState>(
    "/api/account?persona=main",
    { pollInterval: 5_000 }
  );
  const { data: scalperAccount } = useApi<AccountState>(
    "/api/account?persona=scalper",
    { pollInterval: 5_000 }
  );

  return (
    <>
      {/* Main Trader section */}
      <div className="mb-6">
        <PersonaAccountStrip
          title="Main Trader"
          icon="🏛️"
          accent="text-blue-300"
          accentBg="bg-blue-950/20"
          data={mainAccount}
        />
      </div>
      <CampaignsPanel persona="main" />

      {/* Divider */}
      <div className="my-6 flex items-center gap-3">
        <div className="flex-1 border-t border-gray-800" />
        <span className="text-[10px] text-gray-600 uppercase tracking-wider">vs</span>
        <div className="flex-1 border-t border-gray-800" />
      </div>

      {/* Scalper section */}
      <div className="mb-6">
        <PersonaAccountStrip
          title="Scalper"
          icon="⚡"
          accent="text-amber-300"
          accentBg="bg-amber-950/20"
          data={scalperAccount}
        />
      </div>
      <CampaignsPanel persona="scalper" />

      {/* Shared risk tools */}
      <RiskToolsPanel />
    </>
  );
};

export default PositionsTab;
