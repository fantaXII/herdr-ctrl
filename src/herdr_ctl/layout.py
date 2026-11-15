"""Pane distribution and split plans (max 4 panes per tab)."""
from __future__ import annotations

from dataclasses import dataclass

from .config import MAX_PANES_PER_TAB, Member, TabCfg

# For n panes: list of (new_slot, source_slot, direction, ratio). Slot 0 is the tab root.
#  3 = main-right:  0 | 1/2      4 = grid:  0 | 1
#                                              2 | 3
SPLIT_PLANS: dict[int, list[tuple[int, int, str, float | None]]] = {
    1: [],
    2: [(1, 0, "right", None)],
    3: [(1, 0, "right", 0.55), (2, 1, "down", None)],
    4: [(1, 0, "right", None), (2, 0, "down", None), (3, 1, "down", None)],
}


@dataclass
class TabGroup:
    label: str  # herdr tab label: X, X-2, ...
    cfg: TabCfg
    members: list[Member]

    def split_plan(self):
        return SPLIT_PLANS[len(self.members)]


def group_tab(tab: TabCfg) -> list[TabGroup]:
    """Chunk members into herdr tabs of at most 4; the orchestrator (first) stays in slot 0 of X."""
    members = tab.members
    groups = []
    for i in range(0, len(members), MAX_PANES_PER_TAB):
        n = i // MAX_PANES_PER_TAB
        label = tab.name if n == 0 else f"{tab.name}-{n + 1}"
        groups.append(TabGroup(label, tab, members[i:i + MAX_PANES_PER_TAB]))
    return groups


def source_slot(n_members: int, slot: int) -> tuple[int, str, float | None] | None:
    for new, src, direction, ratio in SPLIT_PLANS[n_members]:
        if new == slot:
            return src, direction, ratio
    return None
