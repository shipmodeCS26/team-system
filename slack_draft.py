"""Slack draft for one client's EOD report. Draft only: this module never sends anything.

The client → channel mapping lives in private settings (CLIENT_CHANNELS_JSON), following
docs/clients/README.md. It is never hard-coded here. There is deliberately no send function;
a person reviews the draft in that client's channel and sends it.
"""
from __future__ import annotations

import json
import os
import re

CHANNEL_ID = re.compile(r"[CG][A-Z0-9]{8,}")  # G… = private channel created before 2021


def channel_for(client_id: str, registry: dict | None = None) -> str | None:
    if registry is None:
        try:
            registry = json.loads(os.getenv("CLIENT_CHANNELS_JSON", "{}") or "{}")
        except json.JSONDecodeError:
            return None  # a broken private setting means "no channel mapped", never a failed report
    channel = registry.get(client_id) if isinstance(registry, dict) else None
    return channel if isinstance(channel, str) and CHANNEL_ID.fullmatch(channel) else None


def draft(client_id: str, report: dict, registry: dict | None = None) -> dict:
    """What a person places as a draft in the client's own channel. `send` is always False."""
    channel = channel_for(client_id, registry)
    return {"client_id": client_id, "channel": channel, "text": report["text"],
            "attach": f"{client_id}-dashboard-{re.sub(r'[^A-Za-z0-9]+', '-', report.get('as_of', '')).strip('-')}.png",
            "ready_to_send": bool(report["ready_to_send"] and channel), "send": False,
            "note": None if channel else "No Slack channel is mapped for this client in private settings."}
