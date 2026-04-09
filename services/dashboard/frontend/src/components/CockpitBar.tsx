/**
 * CockpitBar — persistent top strip, always visible regardless of active tab.
 *
 * Contains the "3-second glance" information: heartbeat status, live price,
 * conviction, account, WS status — plus the tab switcher. Everything here
 * keeps polling even when other tabs are unmounted, so the user never
 * loses sight of critical state.
 */

import React from "react";
import LivePriceTicker from "./LivePriceTicker";
import HeartbeatPill from "./HeartbeatPill";
import ConvictionOneLiner from "./ConvictionOneLiner";
import AccountOneLiner from "./AccountOneLiner";

export type TabKey = "trade_now" | "positions" | "market" | "theses" | "investigate" | "system";

export interface TabDef {
  key: TabKey;
  icon: string;
  label: string;
  shortcut: string;
}

export const TABS: TabDef[] = [
  { key: "trade_now",  icon: "🎯", label: "Trade Now",   shortcut: "1" },
  { key: "positions",  icon: "📊", label: "Positions",   shortcut: "2" },
  { key: "market",     icon: "🌍", label: "Market",      shortcut: "3" },
  { key: "theses",     icon: "📌", label: "Theses",      shortcut: "4" },
  { key: "investigate", icon: "🔍", label: "Investigate", shortcut: "5" },
  { key: "system",     icon: "⚙️", label: "System",      shortcut: "6" },
];

interface Props {
  activeTab: TabKey;
  onTabChange: (tab: TabKey) => void;
  wsConnected: boolean;
  lastUpdate: string | null;
}

const CockpitBar: React.FC<Props> = ({ activeTab, onTabChange, wsConnected, lastUpdate }) => {
  return (
    <div className="sticky top-0 z-40 bg-gray-950/95 backdrop-blur border-b border-gray-800 -mx-4 md:-mx-6 px-4 md:px-6">
      {/* Row 1: title + cockpit pills + WS status */}
      <div className="flex items-center justify-between gap-3 py-2">
        <div className="flex items-center gap-3 min-w-0">
          <div>
            <h1 className="text-sm font-bold text-white tracking-tight leading-tight">
              WTI Crude
            </h1>
            <p className="text-[9px] text-gray-500 leading-tight">
              {lastUpdate
                ? `upd ${new Date(lastUpdate).toLocaleTimeString()}`
                : "Connecting…"}
            </p>
          </div>
          <LivePriceTicker />
        </div>

        <div className="flex items-center gap-1.5 flex-wrap justify-end">
          <ConvictionOneLiner onClick={() => onTabChange("investigate")} />
          <AccountOneLiner onClick={() => onTabChange("positions")} />
          <HeartbeatPill />
          <span
            className={`flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-medium ${
              wsConnected
                ? "bg-green-900 text-green-300"
                : "bg-red-900 text-red-300"
            }`}
            title={wsConnected ? "WebSocket connected" : "WebSocket disconnected"}
          >
            <span
              className={`w-2 h-2 rounded-full ${
                wsConnected ? "bg-green-400 animate-pulse" : "bg-red-400"
              }`}
            />
            <span className="hidden sm:inline">{wsConnected ? "Live" : "Off"}</span>
          </span>
        </div>
      </div>

      {/* Row 2: tab switcher */}
      <div className="flex items-center gap-1 overflow-x-auto -mb-px">
        {TABS.map((tab) => {
          const isActive = activeTab === tab.key;
          return (
            <button
              key={tab.key}
              onClick={() => onTabChange(tab.key)}
              className={`flex items-center gap-2 px-4 py-2 text-xs font-medium transition-all border-b-2 whitespace-nowrap ${
                isActive
                  ? "text-white border-blue-500"
                  : "text-gray-500 border-transparent hover:text-gray-300 hover:border-gray-700"
              }`}
              title={`${tab.label} (press ${tab.shortcut})`}
            >
              <span className="text-sm">{tab.icon}</span>
              <span className="hidden md:inline">{tab.label}</span>
              <span className="hidden lg:inline text-[9px] text-gray-600 font-mono">
                {tab.shortcut}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
};

export default CockpitBar;
