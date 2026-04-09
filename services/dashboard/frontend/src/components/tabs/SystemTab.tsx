/**
 * SystemTab — operational / rarely-opened.
 *
 *   1. LlmUsagePanel — token + cost breakdown for every LLM call
 *   2. LogsPanel — Docker container logs stream
 *   (Future: smart alerts config, health checks, heartbeat run detail)
 */

import React from "react";
import LlmUsagePanel from "../LlmUsagePanel";
import LogsPanel from "../LogsPanel";

const SystemTab: React.FC = () => {
  return (
    <>
      <LlmUsagePanel />
      <section>
        <LogsPanel />
      </section>
    </>
  );
};

export default SystemTab;
