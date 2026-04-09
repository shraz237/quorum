/**
 * HeartbeatPill — tiny header control for the Opus live position manager.
 *
 * Shows current state (live / paused / running) plus a next-run countdown,
 * and lets the user flip the kill-switch with one click. The actual
 * heartbeat loop lives in the ai-brain service; this component only talks
 * to the dashboard /api/heartbeat endpoints which read/write the shared
 * Redis flag.
 */

import React, { useEffect, useState } from "react";

interface HeartbeatStatus {
  enabled: boolean;
  last_run_at: string | null;
  next_run_at: string | null;
  running_now: boolean;
  recent_decisions: Array<{
    id: number;
    ran_at: string;
    campaign_id: number;
    decision: string;
    reason: string;
    executed: boolean;
  }>;
}

function formatCountdown(targetIso: string | null): string {
  if (!targetIso) return "—";
  const target = new Date(targetIso).getTime();
  const now = Date.now();
  const secs = Math.max(0, Math.floor((target - now) / 1000));
  if (secs <= 0) return "now";
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  return `${m}:${s.toString().padStart(2, "0")}`;
}

const HeartbeatPill: React.FC = () => {
  const [status, setStatus] = useState<HeartbeatStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [beat, setBeat] = useState(false);

  const fetchStatus = async () => {
    try {
      const res = await fetch("/api/heartbeat/status");
      if (!res.ok) return;
      const json = await res.json();
      if (json.data) setStatus(json.data);
    } catch {
      // ignore
    }
  };

  useEffect(() => {
    fetchStatus();
    const poll = window.setInterval(fetchStatus, 5000);
    // Heartbeat animation: pulse once per second when enabled
    const beatTimer = window.setInterval(() => setBeat((b) => !b), 1000);
    return () => {
      window.clearInterval(poll);
      window.clearInterval(beatTimer);
    };
  }, []);

  // Re-render every second for the countdown
  const [, setTick] = useState(0);
  useEffect(() => {
    const t = window.setInterval(() => setTick((x) => x + 1), 1000);
    return () => window.clearInterval(t);
  }, []);

  const toggle = async () => {
    if (!status || busy) return;
    setBusy(true);
    try {
      const endpoint = status.enabled
        ? "/api/heartbeat/pause"
        : "/api/heartbeat/resume";
      const res = await fetch(endpoint, { method: "POST" });
      if (res.ok) {
        const json = await res.json();
        if (json.data) setStatus(json.data);
      }
    } catch {
      // ignore
    } finally {
      setBusy(false);
    }
  };

  if (!status) {
    return (
      <button
        className="flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-medium bg-gray-800 text-gray-500"
        disabled
      >
        <span>🫀</span>
        <span>loading…</span>
      </button>
    );
  }

  const enabled = status.enabled;
  const running = status.running_now;
  const countdown = formatCountdown(status.next_run_at);

  let bgClass = "bg-gray-800 text-gray-400";
  let label = "paused";
  let heartClass = "";
  if (enabled) {
    if (running) {
      bgClass = "bg-yellow-900 text-yellow-300";
      label = "running…";
      heartClass = "animate-pulse";
    } else {
      bgClass = "bg-rose-900 text-rose-200";
      label = `next ${countdown}`;
      heartClass = beat ? "scale-110" : "scale-100";
    }
  }

  const title = enabled
    ? `Heartbeat live. Last run: ${status.last_run_at ?? "—"}. Click to pause.`
    : "Heartbeat paused. Click to resume.";

  return (
    <button
      onClick={toggle}
      disabled={busy}
      title={title}
      className={`flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-medium transition-all ${bgClass} hover:opacity-80 disabled:opacity-50`}
    >
      <span className={`transition-transform duration-300 ${heartClass}`}>
        {enabled ? "🫀" : "⏸"}
      </span>
      <span>{label}</span>
    </button>
  );
};

export default HeartbeatPill;
