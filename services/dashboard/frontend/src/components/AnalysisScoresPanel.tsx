/**
 * AnalysisScoresPanel — full-width row of 5 score cards.
 *
 * Scores are on the -100..+100 scale (published by the analyzer service).
 * Each card shows:
 *   - large numeric value
 *   - sentiment label (Strong Bear / Bear / Neutral / Bull / Strong Bull)
 *   - a horizontal bar positioned on the -100..+100 axis
 *   - accent color matching the sentiment band
 *
 * The Unified card is slightly emphasised to stand out as the composite.
 */

import React from "react";

export interface ScoreValues {
  technical_score: number | null;
  fundamental_score: number | null;
  sentiment_score: number | null;
  shipping_score: number | null;
  unified_score: number | null;
}

interface Props {
  scores: ScoreValues | null;
  loading?: boolean;
}

// ---------------------------------------------------------------------------
// Sentiment thresholds on -100..+100 scale
// ---------------------------------------------------------------------------

type Band = {
  label: string;
  textColor: string;
  barColor: string;
  accent: string;
};

function band(value: number | null | undefined): Band {
  if (value == null) {
    return {
      label: "N/A",
      textColor: "text-gray-500",
      barColor: "bg-gray-700",
      accent: "border-gray-800",
    };
  }
  if (value >= 50)
    return { label: "Strong Bull", textColor: "text-green-300", barColor: "bg-green-400", accent: "border-green-900/60" };
  if (value >= 20)
    return { label: "Bullish", textColor: "text-green-400", barColor: "bg-green-500", accent: "border-green-900/40" };
  if (value >= 5)
    return { label: "Mild Bull", textColor: "text-emerald-400", barColor: "bg-emerald-500", accent: "border-emerald-900/30" };
  if (value > -5)
    return { label: "Neutral", textColor: "text-gray-400", barColor: "bg-gray-500", accent: "border-gray-800" };
  if (value > -20)
    return { label: "Mild Bear", textColor: "text-orange-400", barColor: "bg-orange-500", accent: "border-orange-900/30" };
  if (value > -50)
    return { label: "Bearish", textColor: "text-red-400", barColor: "bg-red-500", accent: "border-red-900/40" };
  return { label: "Strong Bear", textColor: "text-red-300", barColor: "bg-red-400", accent: "border-red-900/60" };
}

// ---------------------------------------------------------------------------
// Bipolar bar — positioned on -100..+100 axis with zero centre line
// ---------------------------------------------------------------------------

const BipolarBar: React.FC<{ value: number | null; barColor: string }> = ({
  value,
  barColor,
}) => {
  if (value == null) {
    return (
      <div className="relative w-full h-2 bg-gray-800 rounded-full overflow-hidden">
        <div className="absolute inset-y-0 left-1/2 w-px bg-gray-700" />
      </div>
    );
  }

  const clamped = Math.max(-100, Math.min(100, value));
  // Left half = bear (-100 to 0), right half = bull (0 to +100)
  // Bar grows from centre outward.
  const isPositive = clamped >= 0;
  const widthPct = (Math.abs(clamped) / 100) * 50; // 0..50% of total width
  const leftPct = isPositive ? 50 : 50 - widthPct;

  return (
    <div className="relative w-full h-2 bg-gray-800 rounded-full overflow-hidden">
      {/* Zero reference line */}
      <div className="absolute inset-y-0 left-1/2 w-px bg-gray-600 z-10" />
      {/* Fill bar */}
      <div
        className={`absolute inset-y-0 ${barColor} transition-all duration-500 rounded-full`}
        style={{ left: `${leftPct}%`, width: `${widthPct}%` }}
      />
    </div>
  );
};

// ---------------------------------------------------------------------------
// Single score card
// ---------------------------------------------------------------------------

interface CardProps {
  label: string;
  value: number | null | undefined;
  hint?: string;
  emphasis?: boolean;
}

const ScoreCard: React.FC<CardProps> = ({ label, value, hint, emphasis }) => {
  const b = band(value);
  const display = value == null ? "—" : value.toFixed(2);

  return (
    <div
      className={`flex-1 min-w-0 bg-gray-900 border ${b.accent} rounded-xl p-4 flex flex-col gap-2 transition-colors ${
        emphasis ? "ring-1 ring-gray-700/60" : ""
      }`}
    >
      <div className="flex items-center justify-between">
        <span className="text-[11px] uppercase tracking-widest text-gray-500 font-medium">
          {label}
        </span>
        <span className={`text-[10px] font-semibold ${b.textColor}`}>{b.label}</span>
      </div>

      <div className="flex items-baseline gap-2">
        <span className={`font-bold leading-none ${emphasis ? "text-4xl" : "text-3xl"} ${b.textColor}`}>
          {display}
        </span>
        <span className="text-[10px] text-gray-600">/ 100</span>
      </div>

      <BipolarBar value={value ?? null} barColor={b.barColor} />

      <div className="flex justify-between text-[9px] text-gray-600 font-mono -mt-1">
        <span>-100</span>
        <span>0</span>
        <span>+100</span>
      </div>

      {hint && (
        <span className="text-[10px] text-gray-500 leading-tight mt-1">{hint}</span>
      )}
    </div>
  );
};

// ---------------------------------------------------------------------------
// Main panel
// ---------------------------------------------------------------------------

const AnalysisScoresPanel: React.FC<Props> = ({ scores, loading }) => {
  if (loading && !scores) {
    return (
      <section className="mb-6">
        <h2 className="text-xs uppercase tracking-widest text-gray-500 mb-3">
          Analysis Scores
        </h2>
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-3">
          {Array.from({ length: 5 }).map((_, i) => (
            <div key={i} className="bg-gray-900 border border-gray-800 rounded-xl h-36 animate-pulse" />
          ))}
        </div>
      </section>
    );
  }

  const s = scores ?? {
    technical_score: null,
    fundamental_score: null,
    sentiment_score: null,
    shipping_score: null,
    unified_score: null,
  };

  return (
    <section className="mb-6">
      <div className="flex items-baseline justify-between mb-3">
        <h2 className="text-xs uppercase tracking-widest text-gray-500 font-medium">
          Analysis Scores
        </h2>
        <span className="text-[10px] text-gray-600">scale −100 … +100</span>
      </div>
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-3">
        <ScoreCard
          label="Technical"
          value={s.technical_score}
          hint="Multi-TF RSI / MACD / MA / ADX"
        />
        <ScoreCard
          label="Fundamental"
          value={s.fundamental_score}
          hint="EIA / FRED / COT / JODI / OPEC"
        />
        <ScoreCard
          label="Sentiment"
          value={s.sentiment_score}
          hint="News + Twitter + @marketfeed"
        />
        <ScoreCard
          label="Shipping"
          value={s.shipping_score}
          hint="AIS tanker flow + chokepoints"
        />
        <ScoreCard
          label="Unified"
          value={s.unified_score}
          hint="Weighted composite — decision input"
          emphasis
        />
      </div>
    </section>
  );
};

export default AnalysisScoresPanel;
