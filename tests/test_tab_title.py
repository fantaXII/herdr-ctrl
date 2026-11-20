"""Tab display title: herdr label follows `title`, the tab id (used by commands and state) does not change."""
import textwrap

import pytest
import yaml

from herdr_ctl.config import check_data
from herdr_ctl.up import sync_titles, up
from herdr_ctl.watch import detect
from conftest import SAMPLE, always_found, make_ctx, make_project


def labels(h):
    return {t["label"] for t in h.tab_list()}


def with_title(text, tab="X", title="로그인 작업"):
    return text.replace(f"  {tab}:\n", f"  {tab}:\n    title: \"{title}\"\n", 1)


@pytest.mark.parametrize("title,ok", [("로그인 작업", True), ("a" * 30, True), ("a" * 31, False),
                                      ("", False), ("  ", False), ("두\n줄", False), (3, False)])
def test_title_validation(title, ok):
    data = yaml.safe_load(textwrap.dedent(SAMPLE))
    data["tabs"]["X"]["title"] = title
    cfg, issues = check_data(data, which=always_found)
    assert (cfg is not None) == ok, issues
    if ok:
        assert cfg.tab("X").display == title.strip()


def test_up_uses_title_as_label(tmp_path):
    ctx = make_ctx(make_project(tmp_path, textwrap.dedent(with_title(SAMPLE))))
    up(ctx, start_watch=False)
    assert labels(ctx.h) == {"_hc", "로그인 작업", "Y"}
    assert "X" in ctx.state["tabs"]  # state stays keyed by the tab id


def test_title_change_renames_in_place_without_changes(ctx):
    up(ctx, start_watch=False)
    panes_before = dict(ctx.h.panes)
    agents_before = set(ctx.h.agents)
    ctx.project.config_path.write_text(textwrap.dedent(with_title(SAMPLE)))
    c2 = make_ctx(ctx.project, ctx.h)
    assert detect(c2) == []  # title-only edit: nothing to restart or relayout
    assert sync_titles(c2) == ["X → 로그인 작업"]
    assert labels(ctx.h) == {"_hc", "로그인 작업", "Y"}
    assert set(ctx.h.panes) == set(panes_before) and set(ctx.h.agents) == agents_before
    assert sync_titles(c2) == []  # idempotent


def test_up_syncs_title_of_existing_tab(ctx):
    up(ctx, start_watch=False)
    ctx.project.config_path.write_text(textwrap.dedent(with_title(SAMPLE, title="새 이름")))
    c2 = make_ctx(ctx.project, ctx.h)
    n_panes = len(ctx.h.panes)
    up(c2, start_watch=False)
    assert labels(ctx.h) == {"_hc", "새 이름", "Y"} and len(ctx.h.panes) == n_panes


@pytest.mark.parametrize("title", ["_hc", "Y", "y", "dup"])
def test_title_must_not_collide(title):
    data = yaml.safe_load(textwrap.dedent(SAMPLE))
    data["tabs"]["X"]["title"] = title
    if title == "dup":
        data["tabs"]["Y"]["title"] = "DUP"
    cfg, issues = check_data(data, which=always_found)
    assert cfg is None and any(i.path == "tabs.X.title" for i in issues)


def test_label_fallback_never_takes_a_claimed_tab(ctx):
    up(ctx, start_watch=False)
    h = ctx.h
    # tab Y renamed by hand to "Z"; a new config tab Z must get its own herdr tab, not Y's
    y_id = ctx.state["tabs"]["Y"]["tab_id"]
    h.tab_rename(y_id, "Z")
    data = yaml.safe_load(textwrap.dedent(SAMPLE))
    data["tabs"]["Z"] = {"orchestrator": "none", "agents": {"z1": {"kind": "claude"}}}
    ctx.project.config_path.write_text(yaml.safe_dump(data, allow_unicode=True))
    c2 = make_ctx(ctx.project, h)
    up(c2, start_watch=False)
    assert c2.state["tabs"]["Z"]["tab_id"] != y_id and c2.state["tabs"]["Y"]["tab_id"] == y_id
