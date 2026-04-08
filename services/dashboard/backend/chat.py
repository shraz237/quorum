"""Chat service — Anthropic-powered streaming assistant for the trading dashboard."""

from __future__ import annotations

import json
import logging
from typing import Generator

from anthropic import Anthropic

from shared.config import settings
from shared.redis_streams import get_redis
from chat_tools import TOOLS, execute_tool
import plugin_campaign_mgmt as _plugin_campaign_mgmt
import plugin_analytics as _plugin_analytics
import plugin_alerts as _plugin_alerts
import plugin_web as _plugin_web
import plugin_deep_dive as _plugin_deep_dive
import plugin_live_watch as _plugin_live_watch

# Merge all plugin tools into the tools list sent to Opus
_PLUGINS = [
    _plugin_campaign_mgmt,
    _plugin_analytics,
    _plugin_alerts,
    _plugin_web,
    _plugin_deep_dive,
    _plugin_live_watch,
]
for _p in _PLUGINS:
    TOOLS = TOOLS + _p.PLUGIN_TOOLS

logger = logging.getLogger(__name__)

MODEL = "claude-opus-4-6"

SYSTEM_PROMPT = """You are the trading assistant for a Brent crude oil CFD trader on XTB.
You have access to a live trading bot's database AND can EXECUTE trades on the bot's
internal book (5-component scoring, AI-generated recommendations, @marketfeed digests,
open positions, account state, DCA campaigns).

Today's date is 2026-04-08. The user trades aggressively with a $100k account at x10
leverage, scaling in via DCA layers ($3k → $6k → $10k → $20k → $30k → $30k margin).

## YOU CAN EXECUTE — write tools available
- `close_campaign(campaign_id, reason)` — close ALL DCA layers in a campaign at market
- `add_dca_layer(campaign_id, reason)` — scale into an existing campaign (next layer)
- `open_new_campaign(side, reason)` — open a new LONG/SHORT campaign (only one at a time)

When the user says "close my short", "exit", "zamknij" → CALL close_campaign immediately.
When the user says "add", "scale in", "more" → CALL add_dca_layer.
When the user says "open long", "go short", "wejdź" → CALL open_new_campaign.

Do NOT tell the user "you have to do it manually in xStation" — you have the tools.
The bot's book is independent of XTB; the user manages XTB themselves but uses you
to track strategy, scoring, and the internal book.

## CRITICAL RULES
1. **Conversation history is STALE for live state.** Never trust what you said in
   previous messages about current prices, open campaigns, active watch sessions,
   alerts, or scores. They change by the minute. For ANY question about current
   state you MUST call a tool — even if you "remember" the answer from earlier.
2. ALWAYS call get_current_market_state FIRST when the user asks "should I", "what now",
   or any trading decision. Never guess prices from training data.
3. Before answering "do I have an active watch / position / alert" → call the
   corresponding getter tool (get_active_watch, get_campaigns, list_active_alerts).
4. CITE specific evidence — campaign IDs, recommendation IDs, knowledge digest events,
   score values — and always from CURRENT tool output, never from memory.
5. Be concise (3-5 sentences max; tables for data).
6. Before opening or DCAing, check get_account_state to confirm free_margin available.
7. After executing a write tool, briefly report what you did (campaign id, side, reason).
8. Never invent data. If a tool returns an error, surface it to the user."""


# ---------------------------------------------------------------------------
# Session history helpers (Redis-backed, 24 h TTL)
# ---------------------------------------------------------------------------

def _history_key(session_id: str) -> str:
    return f"chat:{session_id}"


def _load_history(session_id: str) -> list[dict]:
    r = get_redis()
    raw = r.get(_history_key(session_id))
    if raw is None:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


def _save_history(session_id: str, history: list[dict]) -> None:
    r = get_redis()
    r.setex(_history_key(session_id), 86400, json.dumps(history))


# ---------------------------------------------------------------------------
# Streaming chat generator
# ---------------------------------------------------------------------------

def stream_chat(message: str, session_id: str = "default") -> Generator[str, None, None]:
    """Generator yielding SSE-formatted event strings."""
    history = _load_history(session_id)
    history.append({"role": "user", "content": message})

    client = Anthropic(api_key=settings.anthropic_api_key)

    # Agentic tool-use loop — continue until the model stops requesting tools
    max_iterations = 20
    for iteration in range(max_iterations):
        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=1500,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=history,
            )
        except Exception as exc:
            logger.exception("Anthropic API call failed (iteration %d)", iteration)
            yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"
            return

        # Decompose the response into text blocks and tool_use blocks
        assistant_blocks: list[dict] = []
        text_content = ""
        tool_calls: list[dict] = []

        for block in response.content:
            if block.type == "text":
                text_content += block.text
                assistant_blocks.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                tool_calls.append(
                    {"id": block.id, "name": block.name, "input": block.input}
                )
                assistant_blocks.append(
                    {
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    }
                )

        # Append the full assistant turn (with tool_use blocks) to history
        history.append({"role": "assistant", "content": assistant_blocks})

        # Stream the text portion to the client
        if text_content:
            yield f"event: token\ndata: {json.dumps({'text': text_content})}\n\n"

        # Stream tool-call metadata so the UI can show "thinking" indicators
        for tc in tool_calls:
            yield (
                f"event: tool_call\ndata: "
                f"{json.dumps({'name': tc['name'], 'input': tc['input']})}\n\n"
            )

        # If the model is done (no tool calls or stop_reason != tool_use), wrap up
        if not tool_calls or response.stop_reason != "tool_use":
            _save_history(session_id, history)
            yield f"event: done\ndata: {json.dumps({})}\n\n"
            return

        # Execute each requested tool and collect results
        tool_results: list[dict] = []
        for tc in tool_calls:
            try:
                # Try each plugin in order; fall back to core tools
                result = None
                for _p in _PLUGINS:
                    result = _p.execute(tc["name"], tc["input"])
                    if result is not None:
                        break
                if result is None:
                    result = execute_tool(tc["name"], tc["input"])
                yield (
                    f"event: tool_result\ndata: "
                    f"{json.dumps({'name': tc['name'], 'output': result}, default=str)}\n\n"
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tc["id"],
                        # Truncate at 8 000 chars to stay within context budget
                        "content": json.dumps(result, default=str)[:8000],
                    }
                )
            except Exception as exc:
                logger.exception("Tool '%s' raised an exception", tc["name"])
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tc["id"],
                        "content": f"error: {exc}",
                        "is_error": True,
                    }
                )

        # Feed tool results back as a user turn so the model can continue
        history.append({"role": "user", "content": tool_results})

    # Safety net: should never reach here in practice
    logger.error("stream_chat exceeded max_iterations=%d for session %s", max_iterations, session_id)
    yield f"event: error\ndata: {json.dumps({'error': 'max iterations exceeded'})}\n\n"
