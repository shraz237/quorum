import React from "react";

interface ScoreGaugeProps {
  label: string;
  value: number | null | undefined;
}

/** Returns Tailwind color classes based on score thresholds. */
function scoreColor(value: number | null | undefined): string {
  if (value == null) return "text-gray-400";
  if (value > 0.3) return "text-green-400";
  if (value < -0.3) return "text-red-400";
  return "text-yellow-400";
}

function scoreLabel(value: number | null | undefined): string {
  if (value == null) return "N/A";
  if (value > 0.3) return "Bullish";
  if (value < -0.3) return "Bearish";
  return "Neutral";
}

/**
 * Renders a single score as a circular-ish gauge with colour coding:
 *   > +0.3  → green (bullish)
 *   < -0.3  → red (bearish)
 *   otherwise → yellow (neutral)
 */
const ScoreGauge: React.FC<ScoreGaugeProps> = ({ label, value }) => {
  const color = scoreColor(value);
  const sentiment = scoreLabel(value);

  // Normalise -1…+1 to 0…100 for the progress ring
  const pct = value != null ? Math.round(((value + 1) / 2) * 100) : 50;
  const radius = 36;
  const circumference = 2 * Math.PI * radius;
  const dashoffset = circumference - (pct / 100) * circumference;

  return (
    <div className="flex flex-col items-center gap-1 p-4 bg-gray-800 rounded-xl w-36">
      {/* SVG ring */}
      <div className="relative w-20 h-20">
        <svg viewBox="0 0 80 80" className="w-full h-full -rotate-90">
          {/* Track */}
          <circle
            cx="40"
            cy="40"
            r={radius}
            fill="none"
            stroke="#374151"
            strokeWidth="8"
          />
          {/* Fill */}
          <circle
            cx="40"
            cy="40"
            r={radius}
            fill="none"
            className={
              value == null
                ? "stroke-gray-600"
                : value > 0.3
                ? "stroke-green-400"
                : value < -0.3
                ? "stroke-red-400"
                : "stroke-yellow-400"
            }
            strokeWidth="8"
            strokeLinecap="round"
            strokeDasharray={circumference}
            strokeDashoffset={dashoffset}
            style={{ transition: "stroke-dashoffset 0.6s ease" }}
          />
        </svg>
        {/* Center value */}
        <span
          className={`absolute inset-0 flex items-center justify-center text-sm font-bold ${color}`}
        >
          {value != null ? value.toFixed(2) : "—"}
        </span>
      </div>

      <span className="text-xs text-gray-400 text-center">{label}</span>
      <span className={`text-xs font-semibold ${color}`}>{sentiment}</span>
    </div>
  );
};

export default ScoreGauge;
