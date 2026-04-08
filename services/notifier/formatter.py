"""Alert formatter for Telegram notifications."""

from __future__ import annotations

ACTION_EMOJI = {
    "BUY": "\U0001f7e2",    # green circle
    "LONG": "\U0001f7e2",   # green circle
    "SELL": "\U0001f534",   # red circle
    "SHORT": "\U0001f534",  # red circle
    "HOLD": "\U0001f7e1",   # yellow circle
    "WAIT": "\U0001f7e1",   # yellow circle
}

WARNING_EMOJI = "\u26a0\ufe0f"


def format_signal_alert(rec: dict) -> str:
    """Format a recommendation dict into a Telegram alert message.

    Parameters
    ----------
    rec:
        Dictionary with RecommendationEvent fields.

    Returns
    -------
    str
        Formatted Telegram message (plain text / Markdown-friendly).
    """
    action = str(rec.get("action", "WAIT")).upper()
    emoji = ACTION_EMOJI.get(action, "\U0001f7e1")

    score = rec.get("unified_score")
    if score is None:
        score = rec.get("opus_override_score")
    score_str = f"{score:+.0f}/100" if score is not None else "N/A"

    confidence = rec.get("confidence")
    confidence_str = f"{confidence:.0%}" if confidence is not None else "N/A"

    entry = rec.get("entry_price")
    sl = rec.get("stop_loss")
    tp = rec.get("take_profit")
    entry_str = f"${entry:.2f}" if entry is not None else "N/A"
    sl_str = f"${sl:.2f}" if sl is not None else "N/A"
    tp_str = f"${tp:.2f}" if tp is not None else "N/A"

    haiku = rec.get("haiku_summary") or ""
    narrative = rec.get("grok_narrative") or ""
    opus = rec.get("opus_analysis") or ""

    risk_factors = rec.get("risk_factors") or []
    if isinstance(risk_factors, list) and risk_factors:
        risk_lines = "\n".join(f"  - {r}" for r in risk_factors)
    elif isinstance(risk_factors, str) and risk_factors:
        risk_lines = f"  - {risk_factors}"
    else:
        risk_lines = "  N/A"

    timestamp = rec.get("timestamp", "")

    lines = [
        f"{emoji} *WTI Crude Signal: {action}*",
        f"Score: {score_str} | Confidence: {confidence_str}",
        "",
    ]

    if haiku:
        lines += [
            "*Haiku Summary*",
            haiku,
            "",
        ]

    if narrative:
        lines += [
            "*Market Narrative*",
            narrative,
            "",
        ]

    if opus:
        lines += [
            "*Opus Analysis*",
            opus,
            "",
        ]

    lines += [
        "*Trade Levels*",
        f"Entry:     {entry_str}",
        f"Stop-Loss: {sl_str}",
        f"Take-Profit: {tp_str}",
        "",
        "*Risk Factors*",
        risk_lines,
    ]

    if timestamp:
        lines += ["", f"_Generated: {timestamp}_"]

    return "\n".join(lines)


def format_system_alert(message: str) -> str:
    """Format a system/operational alert."""
    return f"{WARNING_EMOJI} *System Alert*\n{message}"


_POSITION_EVENT_TITLES = {
    "opened":          ("\U0001f4e5", "Position OPENED"),    # inbox tray
    "tp_hit":          ("\U0001f3af", "TAKE-PROFIT HIT"),    # bullseye
    "sl_hit":          ("\U0001f6d1", "STOP-LOSS HIT"),      # stop sign
    "strategy_close":  ("\U0001f504", "Position CLOSED by strategy"),  # arrows in cycle
    "manual_close":    ("\u270b", "Position CLOSED manually"),         # hand
}


def format_marketfeed_digest(evt: dict) -> str | None:
    """Format a 5-minute @marketfeed knowledge digest."""
    if str(evt.get("type", "")) != "marketfeed_digest":
        return None

    sentiment_label = str(evt.get("sentiment_label", "neutral")).lower()
    score = evt.get("sentiment_score") or 0.0
    icon = (
        "\U0001f7e2" if sentiment_label == "bullish"
        else "\U0001f534" if sentiment_label == "bearish"
        else "\U0001f7e1"
    )

    count = evt.get("message_count", 0)
    window = evt.get("window", "5min")

    lines = [
        f"{icon} *@marketfeed digest* ({window}, {count} msgs)",
        f"Sentiment: {sentiment_label.upper()} ({score:+.2f})",
        "",
    ]

    summary = (evt.get("summary") or "").strip()
    if summary:
        lines += ["*Summary*", summary, ""]

    key_events = evt.get("key_events") or []
    if isinstance(key_events, list) and key_events:
        lines.append("*Key Events*")
        for ev in key_events[:6]:
            lines.append(f"  • {ev}")
        lines.append("")

    ts = evt.get("timestamp")
    if ts:
        lines.append(f"_at {ts}_")

    return "\n".join(lines)


def format_position_event(evt: dict) -> str | None:
    """Format a Position lifecycle event into a Telegram alert."""
    kind = str(evt.get("type", "")).lower()
    if kind not in _POSITION_EVENT_TITLES:
        return None

    icon, title = _POSITION_EVENT_TITLES[kind]
    side = str(evt.get("side", "")).upper()
    pos_id = evt.get("id")

    lines = [
        f"{icon} *{title}*",
        f"Position #{pos_id} — {side}",
        "",
    ]

    entry = evt.get("entry_price")
    close_p = evt.get("close_price")
    sl = evt.get("stop_loss")
    tp = evt.get("take_profit")
    pnl = evt.get("realised_pnl")

    if entry is not None:
        lines.append(f"Entry:       ${entry:.2f}")
    if sl is not None and close_p is None:
        lines.append(f"Stop-Loss:   ${sl:.2f}")
    if tp is not None and close_p is None:
        lines.append(f"Take-Profit: ${tp:.2f}")
    if close_p is not None:
        lines.append(f"Close:       ${close_p:.2f}")
    if pnl is not None:
        sign = "+" if pnl >= 0 else ""
        lines.append(f"P/L:         {sign}${pnl:.2f}")

    notes = evt.get("notes") or evt.get("reason")
    if notes:
        lines += ["", f"_{notes}_"]

    ts = evt.get("timestamp")
    if ts:
        lines += ["", f"_at {ts}_"]

    return "\n".join(lines)


_ALERT_KIND_ICONS = {
    "price":   "\U0001f514",  # bell
    "keyword": "\U0001f4f0",  # newspaper
    "score":   "\U0001f4ca",  # bar chart
}


def format_live_watch_update(evt: dict) -> str | None:
    """Format a live_watch_update event into a Telegram message."""
    if str(evt.get("type", "")) != "live_watch_update":
        return None

    tick = evt.get("tick_number", 0)
    focus = evt.get("focus", "EITHER")
    remaining = evt.get("remaining_seconds", 0)
    rem_min = remaining // 60
    rem_sec = remaining % 60
    final = evt.get("final", False)

    icon = "\U0001f3c1" if final else "\U0001f4e1"  # checkered flag / satellite

    lines = [
        f"{icon} *LIVE WATCH — {focus}* (tick {tick})",
    ]
    if not final:
        lines.append(f"_\u23f1 {rem_min}m {rem_sec}s remaining_")
    else:
        lines.append("_Session ended_")
    lines.append("\u2501" * 26)

    # Price
    price = evt.get("current_price")
    price_delta = evt.get("price_delta")
    price_delta_pct = evt.get("price_delta_pct")
    if price is not None:
        if price_delta is not None:
            arrow = "\u2197" if price_delta >= 0 else "\u2198"
            sign = "+" if price_delta >= 0 else ""
            lines.append(
                f"\U0001f4b5 Price: *${price:.2f}* {arrow} {sign}{price_delta:.2f} ({sign}{price_delta_pct:.2f}%)"
            )
        else:
            lines.append(f"\U0001f4b5 Price: *${price:.2f}*")

    # Scores
    scores = evt.get("scores") or {}
    if scores:
        t = scores.get("technical")
        f = scores.get("fundamental")
        s = scores.get("sentiment")
        u = scores.get("unified")
        lines.append(
            f"\U0001f4ca T:{t:+.0f} \u00b7 F:{f:+.0f} \u00b7 S:{s:+.0f} \u00b7 U:*{u:+.0f}*"
            if all(x is not None for x in (t, f, s, u))
            else "\U0001f4ca scores pending\u2026"
        )

    score_delta = evt.get("score_delta")
    if score_delta is not None and abs(score_delta) >= 0.5:
        sign = "+" if score_delta >= 0 else ""
        lines.append(f"  \u0394unified: {sign}{score_delta:.1f}")

    # Recent knowledge
    knowledge = evt.get("recent_knowledge") or []
    if knowledge:
        lines.append("")
        lines.append("\U0001f4f0 *Recent News*")
        for k in knowledge[:2]:
            label = k.get("sentiment_label", "?")
            score = k.get("sentiment_score") or 0
            emoji = "\U0001f7e2" if label == "bullish" else "\U0001f534" if label == "bearish" else "\U0001f7e1"
            summary = (k.get("summary") or "")[:150]
            lines.append(f"  {emoji} ({score:+.2f}) {summary}")

    # Verdict
    lines.append("")
    verdict = evt.get("verdict") or {}
    action = verdict.get("action", "?")
    conf = verdict.get("confidence", 0)
    summary = verdict.get("summary", "")
    lines.append(f"\U0001f3af *Verdict: {action}* (conf {conf:.0%})")
    if summary:
        lines.append(f"   _{summary}_")

    return "\n".join(lines)


def format_alert_triggered(evt: dict) -> str | None:
    """Format an alert.triggered event into a Telegram message."""
    if str(evt.get("type", "")) != "alert_triggered":
        return None

    kind = str(evt.get("kind", "")).lower()
    icon = _ALERT_KIND_ICONS.get(kind, "\U0001f6a8")
    alert_id = evt.get("alert_id")
    triggered_value = evt.get("triggered_value")
    match_info = evt.get("match_info")
    message = evt.get("message") or ""

    lines = [
        f"{icon} *Alert #{alert_id} triggered* ({kind})",
    ]

    if kind == "price" and triggered_value is not None:
        lines.append(f"WTI price: *${triggered_value:.2f}*")
    elif kind == "score" and triggered_value is not None:
        lines.append(f"Score value: *{triggered_value:+.1f}*")
    elif kind == "keyword" and match_info:
        lines.append(f"_{match_info[:300]}_")

    if message:
        lines += ["", message]

    ts = evt.get("timestamp")
    if ts:
        lines += ["", f"_at {ts}_"]

    return "\n".join(lines)
