from collections.abc import Mapping
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from app.config import VfdCfg


def _flatten_fields(data: Mapping[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, Mapping):
            for sub_key, sub_val in value.items():
                if _is_field_value(sub_val):
                    fields[f"{key}_{sub_key}"] = sub_val
        elif _is_field_value(value):
            fields[key] = value
    return fields


def _is_field_value(value: Any) -> bool:
    return isinstance(value, (int, float, bool, str))


def write_vfd_telemetry(
    vfd: "VfdCfg",
    parsed: dict[str, Any],
    route: str,
    rtt_ms: int,
    *,
    op: str | None = None,
    code_id: int | None = None,
) -> None:
    from app.runtime import influx, site_cfg  # local import to avoid circular deps

    tags = {
        "site": getattr(site_cfg, "site", "unknown"),
        "edge_id": vfd.edge_id,
        "vfd_id": vfd.vfd_id,
        "drive_model": vfd.model,
    }
    # Ensure VFD telemetry carries a group tag so downstream Flux pivot queries can key on it.
    group_ids = [
        g.id
        for g in getattr(site_cfg.miners, "groups", []) or []
        if vfd.vfd_id in (g.vfd_ids or [])
    ]
    if not group_ids:
        tags["group"] = "ungrouped"
    elif len(group_ids) == 1:
        tags["group"] = group_ids[0]
    else:
        tags["group"] = "|".join(sorted(group_ids))
    if op:
        tags["op"] = op
    fields = {"route": route, "rtt_ms": rtt_ms}
    if code_id is not None:
        fields["code_id"] = int(code_id)
    if isinstance(parsed, Mapping):
        fields.update(_flatten_fields(parsed))
    influx.write_point("vfd_telemetry", tags, fields)
