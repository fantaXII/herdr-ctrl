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
    label: str  # state key: X, X-2, X-build, X-build-2 ...
    cfg: TabCfg
    members: list[Member]
    title: str  # herdr tab label: the tab title (or id) with the same suffixes

    def split_plan(self):
        return SPLIT_PLANS[len(self.members)]


def _chunks(tab: TabCfg, members: list[Member], label: str, title: str) -> list[TabGroup]:
    groups = []
    for i in range(0, len(members), MAX_PANES_PER_TAB):
        suffix = "" if i == 0 else f"-{i // MAX_PANES_PER_TAB + 1}"
        groups.append(TabGroup(label + suffix, tab, members[i:i + MAX_PANES_PER_TAB], title + suffix))
    return groups


def group_tab(tab: TabCfg) -> list[TabGroup]:
    """Orchestrator (slot 0) and agents in X, X-2 …; shell panes in their own tab X-build, X-build-2 …"""
    workers = ([tab.orchestrator] if tab.orchestrator else []) + tab.agents
    return (_chunks(tab, workers, tab.name, tab.display)
            + _chunks(tab, tab.shells, f"{tab.name}-build", f"{tab.display}-build"))


def source_slot(n_members: int, slot: int) -> tuple[int, str, float | None] | None:
    for new, src, direction, ratio in SPLIT_PLANS[n_members]:
        if new == slot:
            return src, direction, ratio
    return None
