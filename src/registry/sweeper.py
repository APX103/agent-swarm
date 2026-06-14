"""Resilient periodic health-sweep for the agent registry.

``AgentRegistry.health_sweep`` prunes orphaned skill-index entries (agents whose
TTL expired but whose skill-set memberships linger). It is safe to call any time,
but nothing drove it on a schedule. This loop does, surviving per-iteration errors
and stopping cleanly on shutdown (stop_event or cancellation).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)


async def health_sweep_loop(
    registry,
    interval: float,
    stop_event: Optional[asyncio.Event] = None,
) -> None:
    """Call ``registry.health_sweep()`` every *interval* seconds until stopped.

    A failing sweep is logged and the loop continues. The loop exits when
    *stop_event* is set, when the task is cancelled, or never (if neither).
    """
    logger.info("Registry health-sweep loop started (interval=%.0fs)", interval)
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            try:
                swept = await registry.health_sweep()
                if swept:
                    logger.info("health_sweep removed %d orphaned skill-index entries", swept)
            except Exception:
                logger.warning("health_sweep iteration failed", exc_info=True)

            if stop_event is not None:
                # sleep, but wake early if the stop event fires
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=interval)
                    break
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("Registry health-sweep loop cancelled")
        raise
    logger.info("Registry health-sweep loop stopped")
