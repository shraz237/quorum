/**
 * ConvictionMeter — composite decision-support widget.
 *
 * Renders a large 0..100 gauge with colour-coded strength band and a
 * BULL/BEAR/MIXED direction indicator. Lists the top 3-5 drivers behind
 * the reading so the user can see *why* the meter is where it is.
 */

import React from "react";
import useApi from "../hooks/useApi";

interface Driver {
  name: string;
  value: number | string;
  contribution: number;
}

interface Conviction {
  score: number;
  signed_score: number;
  direction: "BULL" | "BEAR" | "MIXED";
  label: string;
  color: string;
  drivers: Driver[];
  as_of: string;
}

function directionBadge(dir: string): { bg: string; text: string; arrow: string } {
  switch (dir) {
    case "BULL":
      return { bg: "bg-green-900/60 border-green-700", text: "text-green-300", arrow: "▲" };
    case "BEAR":
      return { bg: "bg-red-900/60 border-red-700", text: "text-red-300", arrow: "▼" };
    default:
      return { bg: "bg-gray-800 border-gray-700", text: "text-gray-400", arrow: "●" };
  }
}

function bandColor(score: number): { bar: string; text: string } {
  if (score >= 80) return { bar: "bg-red-500", text: "text-red-400" };
  if (score >= 60) return { bar: "bg-orange-500", text: "text-orange-400" };
  if (score >= 40) return { bar: "bg-yellow-400", text: "text-yellow-400" };
  if (score >= 20) return { bar: "bg-emerald-500", text: "text-emerald-400" };
  return { bar: "bg-gray-600", text: "text-gray-500" };
}

const ConvictionMeter: React.FC = () => {
  const { data, loading } = useApi<Conviction>("/api/conviction", {
    pollInterval: 15_000,
  });

  if (loading && !data) {
    return (
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-4 animate-pulse h-40" />
    );
  }
  if (!data) return null;

  const badge = directionBadge(data.direction);
  const band = bandColor(data.score);

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-4">
      <div className="flex items-start justify-between mb-3">
        <div>
          <h3 className="text-[11px] uppercase tracking-widest text-gray-500 font-medium">
            Conviction
          </h3>
          <div className="text-[10px] text-gray-600">composite decision support</div>
        </div>
        <span
          className={`px-2 py-0.5 rounded border text-[10px] font-bold ${badge.bg} ${badge.text}`}
        >
          {badge.arrow} {data.direction}
        </span>
      </div>

      {/* Big score + label */}
      <div className="flex items-baseline gap-2 mb-2">
        <span className={`text-4xl font-bold leading-none ${band.text}`}>
          {data.score.toFixed(0)}
        </span>
        <span className="text-[11px] text-gray-600">/ 100</span>
        <span className={`text-xs font-semibold ml-auto ${band.text}`}>
          {data.label}
        </span>
      </div>

      {/* Horizontal strength bar */}
      <div className="h-2 bg-gray-800 rounded-full overflow-hidden mb-3">
        <div
          className={`h-full rounded-full transition-all duration-500 ${band.bar}`}
          style={{ width: `${Math.min(100, data.score)}%` }}
        />
      </div>

      {/* Drivers list */}
      <div className="border-t border-gray-800 pt-2">
        <div className="text-[10px] text-gray-500 uppercase tracking-wider mb-1">
          Top drivers
        </div>
        <div className="flex flex-col gap-1">
          {data.drivers.length === 0 ? (
            <div className="text-[10px] text-gray-600 italic">no significant drivers</div>
          ) : (
            data.drivers.map((d, i) => {
              const sign = d.contribution >= 0 ? "+" : "";
              const color = d.contribution > 0 ? "text-green-400" : d.contribution < 0 ? "text-red-400" : "text-gray-500";
              return (
                <div key={i} className="flex items-center justify-between text-[10px]">
                  <span className="text-gray-400 truncate flex-1">{d.name}</span>
                  <span className="text-gray-600 mx-2 truncate max-w-[100px]">
                    {typeof d.value === "number" ? d.value.toFixed(2) : d.value}
                  </span>
                  <span className={`font-mono font-semibold w-12 text-right ${color}`}>
                    {sign}
                    {d.contribution.toFixed(1)}
                  </span>
                </div>
              );
            })
          )}
        </div>
      </div>
    </div>
  );
};

export default ConvictionMeter;
