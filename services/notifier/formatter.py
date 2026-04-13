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
        f"{emoji} *{_test_prefix(rec)}WTI Crude Signal: {action}*",
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


def _is_test_event(evt: dict) -> bool:
    """True if the event is a test/smoke-test payload.

    Convention: every smoke-test publish sets `is_test=True` (or legacy
    `test=True`). Formatters that render user-facing Telegram messages
    prepend a loud 🧪 TEST marker in that case so real alerts can never
    be confused with test traffic.
    """
    if not isinstance(evt, dict):
        return False
    return bool(evt.get("is_test") or evt.get("test"))


def _test_prefix(evt: dict) -> str:
    """Return the title prefix string to use for a possibly-test event.

    Empty string for real events, '🧪 TEST — ' for test payloads.
    Designed to be prepended to the title of any format helper:

        title = f"{_test_prefix(evt)}Heartbeat Status"
    """
    return "🧪 TEST — " if _is_test_event(evt) else ""


def _format_sizing_block(evt: dict) -> list[str]:
    """Render the margin / leverage / notional exposure block.

    Used by every campaign + heartbeat notification so the user always
    sees exactly how much skin is in the trade. Format:

        Margin:   $3,000  x10 leverage
        Exposure: $30,000 (2.54 lots ~ 254 bbl)

    Silently omits any field that's missing — returns an empty list
    when there's nothing to show, so callers can `+=` without worry.
    """
    lines: list[str] = []

    margin = evt.get("total_margin")
    leverage = evt.get("leverage")
    nominal = evt.get("total_nominal")
    lots = evt.get("total_lots")

    # Margin + leverage line
    if margin is not None:
        try:
            margin_f = float(margin)
            if leverage is not None:
                try:
                    lev_int = int(leverage)
                    lines.append(f"Margin:      ${margin_f:,.0f}  x{lev_int} leverage")
                except (TypeError, ValueError):
                    lines.append(f"Margin:      ${margin_f:,.0f}")
            else:
                lines.append(f"Margin:      ${margin_f:,.0f}")
        except (TypeError, ValueError):
            pass

    # Notional exposure line with lots and barrels
    if nominal is not None:
        try:
            nominal_f = float(nominal)
            detail_parts: list[str] = []
            if lots is not None:
                try:
                    lots_f = float(lots)
                    barrels = int(round(lots_f * 100))
                    detail_parts.append(f"{lots_f:.2f} lots ~ {barrels} bbl")
                except (TypeError, ValueError):
                    pass
            detail = f" ({' · '.join(detail_parts)})" if detail_parts else ""
            lines.append(f"Exposure:    ${nominal_f:,.0f}{detail}")
        except (TypeError, ValueError):
            pass

    return lines


_POSITION_EVENT_TITLES = {
    # Legacy single-position events
    "opened":                ("\U0001f4e5", "Position OPENED"),
    "tp_hit":                ("\U0001f3af", "TAKE-PROFIT HIT"),
    "sl_hit":                ("\U0001f6d1", "STOP-LOSS HIT"),
    "strategy_close":        ("\U0001f504", "Position CLOSED by strategy"),
    "manual_close":          ("\u270b",     "Position CLOSED manually"),
    # Campaign lifecycle events (ai-brain auto-trader + dashboard API)
    "campaign_opened":       ("\U0001f680", "Campaign OPENED"),          # rocket
    "dca_layer_added":       ("\U0001f501", "DCA Layer ADDED"),          # repeat
    "campaign_manual_close": ("\u270b",     "Campaign CLOSED manually"),
    "campaign_tp":           ("\U0001f3af", "Campaign TAKE-PROFIT HIT"),
    "campaign_hard_stop":    ("\U0001f6d1", "Campaign HARD STOP HIT"),
    # Heartbeat Opus position manager actions
    "heartbeat_action":      ("\U0001fac0", "Heartbeat Action"),             # anatomical heart
    # Heartbeat periodic status ping — "still watching" updates every ~20 min
    "heartbeat_status":      ("\U0001fac0", "Heartbeat Status"),             # anatomical heart
    # Scalp brain — ultimate scalper verdict transitions
    "scalp_brain_alert":     ("\u26a1",     "Scalp Brain"),                  # lightning
    # Thesis lifecycle — ALL thesis events silenced on Telegram.
    # thesis_created, thesis_triggered, thesis_resolved — all absent.
    # Scalper persona auto-trade events
    "scalper_opened":        ("\U0001f3af", "Scalper OPENED"),               # target
    "scalper_closed":        ("\U0001f3af", "Scalper CLOSED"),               # target
}


def _format_thesis_plan_block(evt: dict) -> list[str]:
    """Shared entry/SL/TP/size renderer used by thesis_created and
    thesis_triggered formatters so the planned trade is always shown
    the same way."""
    lines: list[str] = []
    action = str(evt.get("planned_action") or "").upper()
    entry = evt.get("planned_entry")
    sl = evt.get("planned_stop_loss")
    tp = evt.get("planned_take_profit")
    size = evt.get("planned_size_margin")

    if action and action != "NONE":
        lines.append(f"Action: *{action}*")

    def _fmt_price(label: str, val) -> str | None:
        if val is None:
            return None
        try:
            return f"{label}: `${float(val):.3f}`"
        except (TypeError, ValueError):
            return None

    for label, val in (("Entry", entry), ("SL", sl), ("TP", tp)):
        formatted = _fmt_price(label, val)
        if formatted:
            lines.append(formatted)

    if size is not None:
        try:
            size_f = float(size)
            leverage = 10  # matches account_manager.DEFAULT_LEVERAGE
            exposure = size_f * leverage
            lines.append(f"Size: `${size_f:,.0f}` margin  x{leverage}  (~`${exposure:,.0f}` exposure)")
        except (TypeError, ValueError):
            pass

    return lines


def _format_trigger_description(trigger_type: str, params: dict) -> str:
    """Human-readable trigger condition for the Telegram body."""
    if not isinstance(params, dict):
        params = {}
    if trigger_type == "price_cross_above":
        p = params.get("price")
        return f"price crosses ABOVE `${float(p):.3f}`" if p is not None else "price crosses above"
    if trigger_type == "price_cross_below":
        p = params.get("price")
        return f"price crosses BELOW `${float(p):.3f}`" if p is not None else "price crosses below"
    if trigger_type == "score_above":
        s = params.get("score")
        k = params.get("score_key", "unified")
        return f"{k} score ≥ `{float(s):.1f}`" if s is not None else "score above"
    if trigger_type == "score_below":
        s = params.get("score")
        k = params.get("score_key", "unified")
        return f"{k} score ≤ `{float(s):.1f}`" if s is not None else "score below"
    if trigger_type == "time_elapsed":
        m = params.get("minutes", 0)
        return f"{int(m)} min elapsed since creation"
    if trigger_type == "news_keyword":
        kws = params.get("keywords") or []
        if isinstance(kws, str):
            kws = [kws]
        return f"news contains any of: {', '.join(kws)}" if kws else "news keyword"
    if trigger_type == "scalp_brain_state":
        s = params.get("state", "?")
        return f"scalp brain verdict becomes {s}"
    if trigger_type == "manual":
        return "manual only"
    return trigger_type


def _format_thesis_created(evt: dict) -> str:
    camp_scalp_tag = "scalp" if evt.get("domain") == "scalp" else "campaign"
    title = f"{_test_prefix(evt)}Thesis created [{camp_scalp_tag}]"
    lines = [f"\U0001f4cc *{title}*"]

    th_id = evt.get("thesis_id")
    user_label = str(evt.get("created_by", "")).replace("_", " ")
    if th_id is not None:
        meta = f"#{th_id}"
        if user_label:
            meta += f" · by {user_label}"
        lines.append(meta)

    thesis_title = (evt.get("title") or "").strip()
    if thesis_title:
        lines.append("")
        lines.append(f"*{thesis_title}*")

    text = (evt.get("thesis_text") or "").strip()
    if text:
        lines.append(text)

    lines.append("")
    lines.append(f"Trigger: {_format_trigger_description(evt.get('trigger_type', ''), evt.get('trigger_params') or {})}")

    plan_lines = _format_thesis_plan_block(evt)
    if plan_lines:
        lines.append("")
        lines += plan_lines

    ts = evt.get("timestamp")
    if ts:
        lines += ["", f"_at {ts}_"]
    return "\n".join(lines)


def _format_thesis_triggered(evt: dict) -> str:
    camp_scalp_tag = "scalp" if evt.get("domain") == "scalp" else "campaign"
    title = f"{_test_prefix(evt)}Thesis TRIGGERED [{camp_scalp_tag}]"
    lines = [f"\U0001f514 *{title}*"]

    th_id = evt.get("thesis_id")
    if th_id is not None:
        lines.append(f"#{th_id}")

    thesis_title = (evt.get("title") or "").strip()
    if thesis_title:
        lines.append("")
        lines.append(f"*{thesis_title}*")

    text = (evt.get("thesis_text") or "").strip()
    if text:
        lines.append(text)

    snap = evt.get("trigger_snapshot") or {}
    lines.append("")
    lines.append(f"Trigger: {_format_trigger_description(evt.get('trigger_type', ''), {})}")
    # Show what we saw at trigger time
    if "current_price" in snap and "target_price" in snap:
        try:
            lines.append(
                f"Saw price `${float(snap['current_price']):.3f}` vs target `${float(snap['target_price']):.3f}`"
            )
        except (TypeError, ValueError):
            pass
    if "current_score" in snap:
        try:
            lines.append(f"Saw score `{float(snap['current_score']):.1f}`")
        except (TypeError, ValueError):
            pass
    if "match" in snap and isinstance(snap["match"], dict):
        kw = snap["match"].get("matched_keyword")
        summ = snap["match"].get("summary", "")
        if kw:
            lines.append(f"News keyword: `{kw}`")
        if summ:
            lines.append(f"> {summ[:400]}")

    plan_lines = _format_thesis_plan_block(evt)
    if plan_lines:
        lines.append("")
        lines += plan_lines

    lines += ["", "_Decide now — this is a notification, nothing was auto-executed._"]
    ts = evt.get("timestamp")
    if ts:
        lines += ["", f"_at {ts}_"]
    return "\n".join(lines)


def _format_thesis_resolved(evt: dict) -> str:
    camp_scalp_tag = "scalp" if evt.get("domain") == "scalp" else "campaign"
    outcome = str(evt.get("outcome", "?")).lower()
    icon_by_outcome = {
        "correct": "\u2705",
        "wrong": "\u274c",
        "partial": "\u3030",
        "unresolved": "\u2754",
    }
    outcome_icon = icon_by_outcome.get(outcome, "\U0001f4ca")
    title = f"{_test_prefix(evt)}Thesis RESOLVED [{camp_scalp_tag}] {outcome.upper()}"
    lines = [f"{outcome_icon} *{title}*"]

    th_id = evt.get("thesis_id")
    if th_id is not None:
        lines.append(f"#{th_id}")
    thesis_title = (evt.get("title") or "").strip()
    if thesis_title:
        lines.append("")
        lines.append(f"*{thesis_title}*")

    notes = (evt.get("notes") or "").strip()
    if notes:
        lines.append("")
        lines.append(notes)

    pnl = evt.get("hypothetical_pnl_usd")
    if pnl is not None:
        try:
            pnl_f = float(pnl)
            sign = "+" if pnl_f >= 0 else ""
            lines.append(f"Hypothetical P/L: {sign}${pnl_f:.0f}")
        except (TypeError, ValueError):
            pass

    mfe = evt.get("max_favorable_excursion")
    mae = evt.get("max_adverse_excursion")
    if mfe is not None or mae is not None:
        parts = []
        try:
            if mfe is not None:
                parts.append(f"MFE ${float(mfe):.2f}")
            if mae is not None:
                parts.append(f"MAE ${float(mae):.2f}")
        except (TypeError, ValueError):
            pass
        if parts:
            lines.append(" · ".join(parts))

    ts = evt.get("timestamp")
    if ts:
        lines += ["", f"_at {ts}_"]
    return "\n".join(lines)


def _format_heartbeat_status(evt: dict) -> str:
    """Compact per-campaign status ping — fired ~every 20 min while a
    campaign is open so the user sees the bot is alive and what it's
    thinking even when Opus is holding quietly.

    Payload shape (from services/ai-brain/heartbeat.py:_build_status_ping_payload):
      campaign_id, side, current_price, avg_entry,
      unrealized_pnl_usd, unrealized_pnl_pct,
      take_profit, stop_loss,
      distance_to_tp_pct, distance_to_sl_pct,
      layers, age_hours, latest_reason
    """
    icon = "\U0001fac0"  # anatomical heart
    camp_id = evt.get("campaign_id")
    side = str(evt.get("side", "")).upper()
    early_wake = evt.get("early_wake_reason")
    title = "Heartbeat status" if not early_wake else "Heartbeat ALERT"

    lines = [f"{icon} *{_test_prefix(evt)}{title}*"]
    if early_wake:
        lines.append(f"⚡ _{early_wake}_")
    if camp_id is not None:
        lines.append(f"Campaign #{camp_id} — {side}")
    lines.append("")

    # Price + entry + P/L
    price = evt.get("current_price")
    avg_entry = evt.get("avg_entry")
    pnl_usd = evt.get("unrealized_pnl_usd")
    pnl_pct = evt.get("unrealized_pnl_pct")

    if price is not None:
        try:
            lines.append(f"Price: `${float(price):.3f}`")
        except (TypeError, ValueError):
            pass
    if avg_entry is not None:
        try:
            lines.append(f"Entry: `${float(avg_entry):.3f}`")
        except (TypeError, ValueError):
            pass
    if pnl_usd is not None and pnl_pct is not None:
        try:
            pnl_f = float(pnl_usd)
            pct_f = float(pnl_pct)
            sign = "+" if pnl_f >= 0 else ""
            lines.append(f"P/L: {sign}${pnl_f:.0f}  ({sign}{pct_f:.2f}%)")
        except (TypeError, ValueError):
            pass

    # Distance to TP / SL
    tp = evt.get("take_profit")
    sl = evt.get("stop_loss")
    d_tp = evt.get("distance_to_tp_pct")
    d_sl = evt.get("distance_to_sl_pct")

    if tp is not None:
        try:
            dist_str = f" ({float(d_tp):+.2f}%)" if d_tp is not None else ""
            lines.append(f"TP: `${float(tp):.3f}`{dist_str}")
        except (TypeError, ValueError):
            pass
    if sl is not None:
        try:
            dist_str = f" ({float(d_sl):+.2f}%)" if d_sl is not None else ""
            lines.append(f"SL: `${float(sl):.3f}`{dist_str}")
        except (TypeError, ValueError):
            pass

    # Sizing block: Margin x Leverage = Exposure
    sizing_lines = _format_sizing_block(evt)
    if sizing_lines:
        lines += sizing_lines

    # Layers + age
    layers = evt.get("layers")
    max_layers = evt.get("max_layers")
    age_hours = evt.get("age_hours")
    meta_parts = []
    if layers is not None:
        if max_layers is not None:
            meta_parts.append(f"{layers}/{max_layers} layers")
        else:
            meta_parts.append(f"{layers} layers")
    if age_hours is not None:
        try:
            meta_parts.append(f"{float(age_hours):.1f}h open")
        except (TypeError, ValueError):
            pass
    if meta_parts:
        lines.append(" · ".join(meta_parts))

    reason = (evt.get("latest_reason") or "").strip()
    if reason:
        # Full reason — no truncation. The notifier's _send_chunked()
        # handles Telegram's 3800-char cap by splitting across messages.
        lines += ["", f"_{reason}_"]

    ts = evt.get("timestamp")
    if ts:
        lines += ["", f"_at {ts}_"]

    return "\n".join(lines)


def _format_scalp_brain_alert(evt: dict) -> str:
    """Format a scalp_brain_alert event into a Telegram message.

    Fires on verdict transitions into LONG NOW / SHORT NOW with a 5-min
    per-side cooldown handled on the publisher side.
    """
    verdict = str(evt.get("verdict", "")).upper()
    icon = "\U0001f7e2" if verdict == "LONG" else "\U0001f534" if verdict == "SHORT" else "\u26a1"
    current = evt.get("current_price")
    conviction = evt.get("conviction_pct")

    title = f"Scalp {verdict} NOW"
    lines = [f"{icon} *{_test_prefix(evt)}{title}*"]
    if current is not None:
        try:
            lines.append(f"Price: `${float(current):.3f}`  •  Conviction {int(conviction or 0)}%")
        except (TypeError, ValueError):
            pass
    lines.append("")

    entry = evt.get("entry")
    sl = evt.get("stop_loss")
    tp1 = evt.get("take_profit_1")
    tp2 = evt.get("take_profit_2")
    rr = evt.get("rr_tp1")
    if entry is not None and sl is not None and tp1 is not None:
        try:
            lines.append(f"Entry `${float(entry):.3f}`")
            lines.append(f"SL    `${float(sl):.3f}`")
            lines.append(f"TP1  `${float(tp1):.3f}`  •  R:R `{float(rr or 0):.2f}`")
            if tp2 is not None:
                lines.append(f"TP2  `${float(tp2):.3f}`")
        except (TypeError, ValueError):
            pass

    why = evt.get("why")
    if why:
        lines += ["", f"_{why}_"]

    ts = evt.get("timestamp")
    if ts:
        lines += ["", f"_at {ts}_"]

    return "\n".join(lines)


def _format_heartbeat_action(evt: dict) -> str:
    """Format a heartbeat_action event into a Telegram message.

    Shape:
      {type: heartbeat_action, campaign_id, action, reason, side, ...}
      action in {close, update_levels}

    `hold` never reaches the notifier — it's filtered on publish.
    """
    icon = "\U0001fac0"  # anatomical heart
    camp_id = evt.get("campaign_id")
    side = str(evt.get("side", "")).upper()
    action = str(evt.get("action", "")).lower()
    reason = evt.get("reason") or ""

    if action == "close":
        title = "Heartbeat CLOSED campaign"
    elif action == "update_levels":
        title = "Heartbeat UPDATED levels"
    elif action == "add_dca":
        title = "Heartbeat DCA Layer Added"
    else:
        title = f"Heartbeat {action}"

    lines = [f"{icon} *{_test_prefix(evt)}{title}*"]
    if camp_id is not None:
        lines.append(f"Campaign #{camp_id} — {side}")
    lines.append("")

    if action == "close":
        pnl = evt.get("realized_pnl")
        pnl_pct = evt.get("pnl_pct_at_close")
        if pnl is not None:
            try:
                pnl_f = float(pnl)
                sign = "+" if pnl_f >= 0 else ""
                lines.append(f"Realised P/L: {sign}${pnl_f:.2f}")
            except (TypeError, ValueError):
                pass
        if pnl_pct is not None:
            try:
                lines.append(f"At close:     {float(pnl_pct):+.2f}%")
            except (TypeError, ValueError):
                pass
    elif action == "update_levels":
        old_tp = evt.get("old_take_profit")
        new_tp = evt.get("new_take_profit")
        old_sl = evt.get("old_stop_loss")
        new_sl = evt.get("new_stop_loss")
        if new_tp is not None or old_tp is not None:
            lines.append(f"TP: {old_tp} → {new_tp}")
        if new_sl is not None or old_sl is not None:
            lines.append(f"SL: {old_sl} → {new_sl}")
        pnl = evt.get("unrealized_pnl_usd")
        pnl_pct = evt.get("unrealized_pnl_pct")
        if pnl is not None and pnl_pct is not None:
            try:
                pnl_f = float(pnl)
                pct_f = float(pnl_pct)
                sign = "+" if pnl_f >= 0 else ""
                lines.append(f"Unrealised:  {sign}${pnl_f:.0f}  ({sign}{pct_f:.2f}%)")
            except (TypeError, ValueError):
                pass

    # DCA-specific: show entry price, new avg, layers, position total
    if action == "add_dca":
        price = evt.get("price")
        avg_entry = evt.get("avg_entry")
        layers = evt.get("layers")
        max_layers = evt.get("max_layers")
        pnl_usd = evt.get("unrealized_pnl_usd")
        pnl_pct = evt.get("unrealized_pnl_pct")
        if price is not None:
            try:
                lines.append(f"Layer entry: `${float(price):.3f}`")
            except (TypeError, ValueError):
                pass
        if avg_entry is not None:
            try:
                lines.append(f"Avg entry:   `${float(avg_entry):.3f}`")
            except (TypeError, ValueError):
                pass
        if layers is not None:
            lines.append(f"Layers:      {layers}/{max_layers or '?'}")
        if pnl_usd is not None and pnl_pct is not None:
            try:
                pnl_f = float(pnl_usd)
                pct_f = float(pnl_pct)
                sign = "+" if pnl_f >= 0 else ""
                lines.append(f"Position P/L: {sign}${pnl_f:.0f}  ({sign}{pct_f:.2f}%)")
            except (TypeError, ValueError):
                pass

    # Sizing block: Margin x Leverage = Exposure
    sizing_lines = _format_sizing_block(evt)
    if sizing_lines:
        lines += sizing_lines

    if reason:
        lines += ["", f"_{reason}_"]

    ts = evt.get("timestamp")
    if ts:
        lines += ["", f"_at {ts}_"]

    return "\n".join(lines)


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
        f"{icon} *{_test_prefix(evt)}@marketfeed digest* ({window}, {count} msgs)",
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
    """Format a Position or Campaign lifecycle event into a Telegram alert.

    Handles two event families that share one stream:

    A. Single-position events from pre-campaign era:
       {type: opened/tp_hit/sl_hit/strategy_close/manual_close,
        id, side, entry_price, close_price, stop_loss, take_profit,
        realised_pnl, timestamp, notes}

    B. Campaign-level events from ai-brain auto-trader + dashboard API:
       - campaign_opened:       {id|campaign_id, side, entry_price, layer, reason}
       - dca_layer_added:       {campaign_id, position_id, side, entry_price|price, layer, reason}
       - campaign_manual_close: {campaign_id, side, realized_pnl, ...}
       - campaign_tp/hard_stop: {campaign_id, side, ...}
    """
    kind = str(evt.get("type", "")).lower()
    if kind not in _POSITION_EVENT_TITLES:
        return None

    # Heartbeat events have their own custom formatter (different field shape)
    if kind == "heartbeat_action":
        return _format_heartbeat_action(evt)
    if kind == "heartbeat_status":
        return _format_heartbeat_status(evt)
    if kind == "scalp_brain_alert":
        return _format_scalp_brain_alert(evt)
    # ALL thesis events (created, triggered, resolved) are intentionally
    # dropped at the _POSITION_EVENT_TITLES guard above — they're for
    # learning data only (dashboard Theses tab). No Telegram noise.

    # Scalper persona events — add persona tag to the standard rendering
    if kind in ("scalper_opened", "scalper_closed"):
        persona = evt.get("persona") or "scalper"
        # Fall through to the standard rendering below, but note the
        # persona tag is already in the title from _POSITION_EVENT_TITLES

    icon, title = _POSITION_EVENT_TITLES[kind]
    title = f"{_test_prefix(evt)}{title}"
    side = str(evt.get("side", "")).upper()

    # Identifier: prefer campaign_id for campaign events, else fall back to id
    camp_id = evt.get("campaign_id")
    pos_id = evt.get("position_id") or evt.get("id")

    header_lines = [f"{icon} *{title}*"]
    if kind.startswith("campaign_") or kind == "dca_layer_added":
        if camp_id is not None:
            header_lines.append(f"Campaign #{camp_id} — {side}")
        elif pos_id is not None:
            header_lines.append(f"Campaign #{pos_id} — {side}")
    elif pos_id is not None:
        header_lines.append(f"Position #{pos_id} — {side}")

    lines = header_lines + [""]

    # Entry price appears under multiple keys depending on event shape
    entry = (
        evt.get("entry_price")
        if evt.get("entry_price") is not None
        else evt.get("price")
        if evt.get("price") is not None
        else evt.get("avg_entry_price")
    )
    close_p = evt.get("close_price") or evt.get("current_price")
    sl = evt.get("stop_loss")
    tp = evt.get("take_profit")
    pnl = evt.get("realised_pnl") or evt.get("realized_pnl")

    if entry is not None:
        try:
            lines.append(f"Entry:       ${float(entry):.2f}")
        except (TypeError, ValueError):
            pass
    if sl is not None and close_p is None:
        try:
            lines.append(f"Stop-Loss:   ${float(sl):.2f}")
        except (TypeError, ValueError):
            pass
    if tp is not None and close_p is None:
        try:
            lines.append(f"Take-Profit: ${float(tp):.2f}")
        except (TypeError, ValueError):
            pass
    if close_p is not None and kind.endswith("_close") or kind in ("tp_hit", "sl_hit"):
        try:
            lines.append(f"Close:       ${float(close_p):.2f}")
        except (TypeError, ValueError):
            pass
    if pnl is not None:
        try:
            pnl_f = float(pnl)
            sign = "+" if pnl_f >= 0 else ""
            lines.append(f"P/L:         {sign}${pnl_f:.2f}")
        except (TypeError, ValueError):
            pass

    # Campaign-level extras
    layers_used = evt.get("layers_used")
    max_layers = evt.get("max_layers")
    if layers_used is not None and max_layers is not None:
        lines.append(f"Layers:      {layers_used}/{max_layers}")

    layer_idx = evt.get("layer")
    if layer_idx is not None:
        lines.append(f"Layer:       #{layer_idx}")

    # Sizing block: Margin / Leverage / Exposure + lots. Shown on every
    # campaign event so the user instantly sees skin-in-the-trade.
    lines += _format_sizing_block(evt)

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
