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
    assert labels(ctx.h) == {"_hc", "로그인 작업", "로그인 작업-build", "Y"}
    assert "X" in ctx.state["tabs"]  # state stays keyed by the tab id


def test_title_change_renames_in_place_without_changes(ctx):
    up(ctx, start_watch=False)
    panes_before = dict(ctx.h.panes)
    agents_before = set(ctx.h.agents)
    ctx.project.config_path.write_text(textwrap.dedent(with_title(SAMPLE)))
    c2 = make_ctx(ctx.project, ctx.h)
    assert detect(c2) == []  # title-only edit: nothing to restart or relayout
    assert sync_titles(c2) == ["X → 로그인 작업", "X-build → 로그인 작업-build"]
    assert labels(ctx.h) == {"_hc", "로그인 작업", "로그인 작업-build", "Y"}
    assert set(ctx.h.panes) == set(panes_before) and set(ctx.h.agents) == agents_before
    assert sync_titles(c2) == []  # idempotent


def test_up_syncs_title_of_existing_tab(ctx):
    up(ctx, start_watch=False)
    ctx.project.config_path.write_text(textwrap.dedent(with_title(SAMPLE, title="새 이름")))
    c2 = make_ctx(ctx.project, ctx.h)
    n_panes = len(ctx.h.panes)
    up(c2, start_watch=False)
    assert labels(ctx.h) == {"_hc", "새 이름", "새 이름-build", "Y"} and len(ctx.h.panes) == n_panes


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


# ---- a tab renamed in herdr itself keeps that name
def title_of(ctx, tab):
    return (yaml.safe_load(ctx.project.config_path.read_text())["tabs"][tab] or {}).get("title")


def test_herdr_rename_becomes_config_title_and_survives_reopen(ctx):
    from herdr_ctl.up import down, pull_titles
    up(ctx, start_watch=False)
    h = ctx.h
    h.tab_rename(ctx.state["tabs"]["X"]["tab_id"], "로그인")
    assert pull_titles(ctx, rename_rest=True) == ["탭 'X' 표시 이름: X → 로그인 (herdr에서 바꾼 이름 저장)"]
    assert title_of(ctx, "X") == "로그인" and ctx.cfg.tab("X").display == "로그인"
    assert labels(h) == {"_hc", "로그인", "로그인-build", "Y"}  # its build tab follows
    assert detect(ctx) == []  # nothing to restart
    assert pull_titles(ctx) == []  # once
    c2 = make_ctx(ctx.project, h)
    down(c2, "X")
    up(make_ctx(ctx.project, h), start_watch=False)
    assert labels(h) == {"_hc", "로그인", "로그인-build", "Y"}


def test_up_keeps_a_herdr_rename(ctx):
    up(ctx, start_watch=False)
    ctx.h.tab_rename(ctx.state["tabs"]["Y"]["tab_id"], "검증")
    up(make_ctx(ctx.project, ctx.h), start_watch=False)
    assert "검증" in labels(ctx.h) and title_of(ctx, "Y") == "검증"


def test_down_saves_the_herdr_name_before_closing(ctx):
    from herdr_ctl.up import down
    up(ctx, start_watch=False)
    ctx.h.tab_rename(ctx.state["tabs"]["Y"]["tab_id"], "검증")
    down(make_ctx(ctx.project, ctx.h), "Y")
    assert title_of(ctx, "Y") == "검증"


def test_renaming_back_to_the_tab_id_drops_the_title(tmp_path):
    from herdr_ctl.up import pull_titles
    ctx = make_ctx(make_project(tmp_path, textwrap.dedent(with_title(SAMPLE))))
    up(ctx, start_watch=False)
    ctx.h.tab_rename(ctx.state["tabs"]["X"]["tab_id"], "X")
    pull_titles(ctx)
    assert title_of(ctx, "X") is None


def test_config_title_edit_wins_over_the_live_label(ctx):
    """UI save / hand edit: the config changed since hctrl applied it, so the tab follows the config."""
    from herdr_ctl.up import pull_titles
    up(ctx, start_watch=False)
    ctx.project.config_path.write_text(textwrap.dedent(with_title(SAMPLE, title="새 이름")))
    c2 = make_ctx(ctx.project, ctx.h)
    assert pull_titles(c2) == [] and title_of(c2, "X") == "새 이름"
    sync_titles(c2)
    assert "새 이름" in labels(ctx.h)
    ctx.h.tab_rename(c2.state["tabs"]["X"]["tab_id"], "또 바꿈")  # and a herdr rename after that is kept
    pull_titles(c2)
    assert title_of(c2, "X") == "또 바꿈"


def test_a_herdr_name_the_config_cannot_take_is_reported_once(ctx):
    from herdr_ctl.up import pull_titles
    up(ctx, start_watch=False)
    ctx.h.tab_rename(ctx.state["tabs"]["X"]["tab_id"], "Y")  # taken by tab Y
    out = pull_titles(ctx)
    assert len(out) == 1 and "반영하지 못했습니다" in out[0] and title_of(ctx, "X") is None
    assert pull_titles(ctx) == []


def test_watch_tick_pulls_titles(ctx):
    from herdr_ctl.watch import pull_tab_titles
    up(ctx, start_watch=False)
    ctx.h.tab_rename(ctx.state["tabs"]["Y"]["tab_id"], "검증")
    assert pull_tab_titles(ctx.project, ctx.h, which=always_found)
    assert title_of(ctx, "Y") == "검증"
