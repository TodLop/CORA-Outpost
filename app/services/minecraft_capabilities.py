"""Runtime availability checks for optional integrations in the public admin extract."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

CapabilityState = Literal[
    "disabled",
    "available",
    "missing_config",
    "missing_plugin",
    "unhealthy",
    "unknown",
]

BLUEMAP_PROXY = "bluemap_proxy"


@dataclass(frozen=True)
class CapabilityStatus:
    id: str
    module_id: str
    enabled: bool
    available: bool
    state: CapabilityState
    reason: str
    remediation: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CapabilityUnavailableError(RuntimeError):
    def __init__(self, status: CapabilityStatus):
        super().__init__(status.reason)
        self.status = status


def _disabled(capability_id: str, module_id: str) -> CapabilityStatus:
    return CapabilityStatus(
        id=capability_id,
        module_id=module_id,
        enabled=False,
        available=False,
        state="disabled",
        reason=f"Optional integration '{module_id}' is not included in the public admin extract.",
        remediation="Keep this integration in a private fork or add a reviewed module with explicit tests.",
    )


def get_bluemap_capability(request: Any | None = None) -> CapabilityStatus:
    return _disabled(BLUEMAP_PROXY, "bluemap_proxy")


def get_capability(capability_id: str, request: Any | None = None) -> CapabilityStatus:
    if capability_id == BLUEMAP_PROXY:
        return get_bluemap_capability(request)
    return CapabilityStatus(
        id=capability_id,
        module_id=capability_id,
        enabled=False,
        available=False,
        state="unknown",
        reason=f"Unknown capability '{capability_id}'.",
        remediation="Use a capability exported by the public admin extract.",
    )


def get_capabilities(request: Any | None = None) -> dict[str, dict[str, Any]]:
    return {BLUEMAP_PROXY: get_bluemap_capability(request).to_dict()}


def require_capability(capability_id: str, request: Any | None = None) -> CapabilityStatus:
    status = get_capability(capability_id, request)
    if not status.available:
        raise CapabilityUnavailableError(status)
    return status
