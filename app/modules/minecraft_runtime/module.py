"""Minecraft runtime lifecycle module definition."""

from __future__ import annotations

import logging
from types import ModuleType

from app.modules.contracts import ModuleContract

logger = logging.getLogger(__name__)

SchedulerSpec = tuple[ModuleType, str]


def _scheduler_modules() -> tuple[SchedulerSpec, ...]:
    from app.services import backup_scheduler
    from app.services import minecraft_update_automation
    from app.services import reboot_scheduler
    from app.services import server_metrics

    return (
        (reboot_scheduler, "Reboot"),
        (backup_scheduler, "Backup"),
        (minecraft_update_automation, "Minecraft update automation"),
        (server_metrics, "Server metrics"),
    )


async def start_minecraft_runtime() -> None:
    from app.services import minecraft_server

    try:
        if await minecraft_server.ensure_log_tailer_running():
            logger.info("Minecraft server detected, log tailer started")
        else:
            logger.info("Minecraft server not running")
    except Exception:
        logger.error("Failed to start Minecraft log tailer", exc_info=True)

    for scheduler, name in _scheduler_modules():
        try:
            await scheduler.start_scheduler()
            logger.info("%s scheduler started", name)
        except Exception:
            logger.error("Failed to start %s scheduler", name, exc_info=True)


async def stop_minecraft_runtime() -> None:
    for scheduler, name in reversed(_scheduler_modules()):
        try:
            await scheduler.stop_scheduler()
        except Exception:
            logger.error("Failed to stop %s scheduler", name, exc_info=True)


module = ModuleContract(
    slug="minecraft_runtime",
    display_name="Minecraft Runtime",
    enabled_by_default=False,
    route_prefixes=(),
    routers=(),
    startup=(start_minecraft_runtime,),
    shutdown=(stop_minecraft_runtime,),
)
