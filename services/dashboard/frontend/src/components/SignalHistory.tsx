import React from "react";

export interface Signal {
  id: number;
  timestamp: string;
  action: string;
  confidence: number | null;
  unified_score: number | null;
  entry_price: number | null;
  stop_loss: number | null;
  take_profit: number | null;
  haiku_summary: string | null;
}

interface SignalHistoryProps {
  signals: Signal[];
}

function ActionBadge({ action }: { action: string }) {
  const upper = action.toUpperCase();
  const styles: Record<string, string> = {
    BUY: "bg-green-900 text-green-300 border border-green-700",
    SELL: "bg-red-900 text-red-300 border border-red-700",
    HOLD: "bg-yellow-900 text-yellow-300 border border-yellow-700",
  };
  const cls = styles[upper] ?? "bg-gray-700 text-gray-300";
  return (
    <span className={`px-2 py-0.5 rounded text-xs font-bold uppercase ${cls}`}>
      {upper}
    </span>
  );
}

function fmt(val: number | null, decimals = 2): string {
  return val != null ? val.toFixed(decimals) : "—";
}

function fmtDate(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/**
 * Table listing recent AI trading recommendations.
 */
const SignalHistory: React.FC<SignalHistoryProps> = ({ signals }) => {
  if (signals.length === 0) {
    return (
      <div className="bg-gray-900 rounded-xl p-4 text-gray-500 text-sm">
        No signals yet.
      </div>
    );
  }

  return (
    <div className="bg-gray-900 rounded-xl p-4 overflow-x-auto">
      <h2 className="text-sm font-semibold text-gray-300 mb-3">
        Recent AI Signals
      </h2>
      <table className="w-full text-xs text-left border-collapse">
        <thead>
          <tr className="text-gray-500 border-b border-gray-800">
            <th className="pb-2 pr-3 font-medium">Time</th>
            <th className="pb-2 pr-3 font-medium">Action</th>
            <th className="pb-2 pr-3 font-medium">Confidence</th>
            <th className="pb-2 pr-3 font-medium">Score</th>
            <th className="pb-2 pr-3 font-medium">Entry</th>
            <th className="pb-2 pr-3 font-medium">Stop</th>
            <th className="pb-2 pr-3 font-medium">Target</th>
            <th className="pb-2 font-medium">Summary</th>
          </tr>
        </thead>
        <tbody>
          {signals.map((s) => (
            <tr
              key={s.id}
              className="border-b border-gray-800 hover:bg-gray-800 transition-colors"
            >
              <td className="py-2 pr-3 text-gray-400 whitespace-nowrap">
                {fmtDate(s.timestamp)}
              </td>
              <td className="py-2 pr-3">
                <ActionBadge action={s.action} />
              </td>
              <td className="py-2 pr-3 text-gray-300">
                {s.confidence != null
                  ? `${(s.confidence * 100).toFixed(0)}%`
                  : "—"}
              </td>
              <td className="py-2 pr-3 text-gray-300">
                {fmt(s.unified_score)}
              </td>
              <td className="py-2 pr-3 text-gray-300">
                {s.entry_price != null ? `$${fmt(s.entry_price)}` : "—"}
              </td>
              <td className="py-2 pr-3 text-red-400">
                {s.stop_loss != null ? `$${fmt(s.stop_loss)}` : "—"}
              </td>
              <td className="py-2 pr-3 text-green-400">
                {s.take_profit != null ? `$${fmt(s.take_profit)}` : "—"}
              </td>
              <td className="py-2 text-gray-400 max-w-xs truncate">
                {s.haiku_summary ?? "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
};

export default SignalHistory;
