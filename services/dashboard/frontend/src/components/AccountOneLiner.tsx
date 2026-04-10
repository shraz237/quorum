/**
 * AccountOneLiner — compact dual-persona view for the cockpit bar.
 *
 * Shows both Main and Scalper equity + P/L in one row so the user
 * sees both traders' performance at a glance.
 */

import React from "react";
import useApi from "../hooks/useApi";

interface Account {
  persona: string;
  equity: number;
  unrealised_pnl: number;
  account_drawdown_pct: number;
  open_campaigns: number;
  free_margin: number;
}

interface Props {
  onClick?: () => void;
}

const MiniAccount: React.FC<{ label: string; data: Account; color: string }> = ({
  label,
  data,
  color,
}) => {
  const pnl = data.unrealised_pnl;
  const pnlColor = pnl >= 0 ? "text-emerald-300" : "text-red-300";
  const pnlSign = pnl >= 0 ? "+" : "";

  return (
    <span className="flex items-center gap-1">
      <span className={`text-[8px] uppercase tracking-wider ${color}`}>{label}</span>
      <span className="font-bold tabular-nums text-gray-100 text-[11px]">
        ${(data.equity / 1000).toFixed(1)}k
      </span>
      <span className={`tabular-nums text-[10px] ${pnlColor}`}>
        {pnlSign}${Math.round(pnl)}
      </span>
    </span>
  );
};

const AccountOneLiner: React.FC<Props> = ({ onClick }) => {
  const { data: main } = useApi<Account>("/api/account?persona=main", {
    pollInterval: 5_000,
  });
  const { data: scalper } = useApi<Account>("/api/account?persona=scalper", {
    pollInterval: 5_000,
  });

  if (!main && !scalper) {
    return (
      <div className="flex items-center gap-1.5 px-3 py-1 rounded-full text-xs bg-gray-800 text-gray-500 animate-pulse">
        <span>Account …</span>
      </div>
    );
  }

  const mainTitle = main
    ? `Main: eq $${main.equity.toFixed(0)} · dd ${main.account_drawdown_pct.toFixed(1)}% · ${main.open_campaigns} open`
    : "";
  const scalperTitle = scalper
    ? `Scalper: eq $${scalper.equity.toFixed(0)} · dd ${scalper.account_drawdown_pct.toFixed(1)}% · ${scalper.open_campaigns} open`
    : "";

  return (
    <button
      onClick={onClick}
      className="flex items-center gap-2 px-3 py-1 rounded-full text-xs font-medium bg-gray-800/80 hover:bg-gray-700 transition"
      title={`${mainTitle}\n${scalperTitle}`}
    >
      {main && <MiniAccount label="M" data={main} color="text-blue-400" />}
      {main && scalper && <span className="text-gray-700 text-[9px]">|</span>}
      {scalper && <MiniAccount label="S" data={scalper} color="text-amber-400" />}
    </button>
  );
};

export default AccountOneLiner;
