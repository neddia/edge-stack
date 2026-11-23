from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Optional

from app.config import SiteConfig, VfdCfg


@dataclass(frozen=True)
class EdgePair:
    id: str
    primary: str
    secondary: str


class RoleMap:
    """
    Tiny helper that lets services ask "am I the primary edge for this VFD?"
    without re-parsing site-config each time.
    """

    def __init__(self, cfg: Optional[SiteConfig] = None):
        self._pairs: Dict[str, EdgePair] = {}
        if cfg:
            self.update(cfg)

    def update(self, cfg: SiteConfig) -> None:
        pairs: Dict[str, EdgePair] = {}
        for entry in getattr(cfg, "edge_pairs", []) or []:
            if not getattr(entry, "id", None):
                continue
            primary = getattr(entry, "primary", "") or ""
            secondary = getattr(entry, "secondary", "") or ""
            pairs[entry.id] = EdgePair(id=entry.id, primary=primary, secondary=secondary)
        self._pairs = pairs

    # ---- queries ---------------------------------------------------------

    def pair_for_vfd(self, vfd: VfdCfg) -> Optional[EdgePair]:
        pair_id = getattr(vfd, "control_group", None)
        if not pair_id:
            return None
        return self._pairs.get(pair_id)

    def is_member(self, edge_id: str, vfd: VfdCfg) -> bool:
        pair = self.pair_for_vfd(vfd)
        if not pair:
            return vfd.edge_id == edge_id
        return edge_id in (pair.primary, pair.secondary)

    def is_primary_for(self, edge_id: str, vfd: VfdCfg) -> bool:
        pair = self.pair_for_vfd(vfd)
        if not pair:
            return vfd.edge_id == edge_id
        return edge_id == pair.primary

    def primary_edge_for_vfds(self, vfds: Iterable[VfdCfg]) -> Optional[str]:
        """
        If every VFD shares the same control_group and the pair is known,
        return that pair's current primary edge id.
        """
        pair_id: Optional[str] = None
        for vfd in vfds:
            pair = self.pair_for_vfd(vfd)
            if not pair:
                return None
            if pair_id is None:
                pair_id = pair.id
            elif pair.id != pair_id:
                return None
        if not pair_id:
            return None
        pair = self._pairs.get(pair_id)
        return pair.primary if pair else None

    def members_for_pair(self, pair_id: str) -> Optional[tuple[str, str]]:
        pair = self._pairs.get(pair_id)
        if not pair:
            return None
        return pair.primary, pair.secondary
