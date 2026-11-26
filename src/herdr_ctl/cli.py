"""`hctrl` (= `herdr_ctl`) command line."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from . import roles as roles_mod
from .config import check_file, format_issues, orch_name, str_keys
from .herdr import Herdr, HerdrError, clean_env
from .orchestrator import LimitExceeded, ScopeError, current_actor, enforce, guard
from .project import Project, find_project, load_registry, project_by_name, register
from .state import LockBusy, State
from .up import Ctx

SUBCOMMANDS = {"init", "check", "up", "down", "status", "zoom", "run", "send", "wait", "stop",
               "resume", "ask", "role", "changes", "restart", "watch", "pending", "answer",
               "_worker", "_perm-hook", "ui", "shell-init", "update", "_update-check", "tab"}


class Fail(Exception):
    pass


def tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


# ------------------------------------------------------------- context
def resolve_project(path: str | None) -> Project:
    root = path or os.environ.get("HC_PROJECT")
    if root:
        p = find_project(Path(root))
        if p is None:
            raise Fail(f"{root}에서 프로젝트를 찾을 수 없습니다")
        return p
    p = find_project()
    if p is None:
        raise Fail("현재 위치에서 프로젝트를 찾을 수 없습니다. 프로젝트 폴더에서 실행하거나 hctrl --add / hctrl init을 먼저 하세요")
    return p


def make_herdr(session: str):
    return Herdr(session=session)


def load_ctx(args, verbose_warnings: bool = False) -> Ctx:
    project = resolve_project(getattr(args, "project", None))
    cfg, issues = check_file(project.config_path)
    errors = [i for i in issues if i.level == "error"]
    if errors:
        raise Fail("설정 오류로 실행하지 않습니다:\n" + format_issues(issues))
    if verbose_warnings and issues:
        print(format_issues(issues))
    return Ctx(project, cfg, make_herdr(cfg.session), State(project))


# ---------------------------------------------------------------- open
def cmd_open(name: str | None, project: Project | None = None) -> int:
    if name:
        project = project_by_name(name)
        if project is None:
            raise Fail(f"등록된 프로젝트 '{name}'이 없습니다 (hctrl --list)")
    elif project is None:
        project = find_project()
    if os.environ.get("HERDR_ENV") == "1":
        sock = os.environ.get("HERDR_SOCKET_PATH", "")
        if project and project.config_path.exists():
            cfg, _ = check_file(project.config_path)
            if cfg and f"/sessions/{cfg.session}/" in sock:
                print(f"이미 '{cfg.session}' 세션 안입니다.")
                return 0
        raise Fail("herdr 창 안에서는 프로젝트 세션을 열지 않습니다 (herdr 안에 herdr가 겹칩니다). "
                   "일반 터미널에서 hctrl을 실행하세요")
    if tty():
        show_update_notice()
    if project is None or not project.config_path.exists():
        if not tty():
            raise Fail("등록되지 않은 폴더입니다. hctrl --add [이름]으로 등록하세요" if project is None
                       else f"{project.config_path}가 없습니다 (hctrl init)")
        # Unregistered folder / no config yet: pick or set up a project in the browser UI;
        # the chosen project then opens right here, in this terminal.
        from .ui import run_launcher
        project = run_launcher(Path.cwd(), start=project)
        if project is None:
            return 0
    cfg, issues = check_file(project.config_path)
    if cfg is None:
        raise Fail("설정 오류:\n" + format_issues(issues))
    if issues:
        print(format_issues(issues))
    h = make_herdr(cfg.session)
    ctx = Ctx(project, cfg, h, State(project))
    if not h.session_running(cfg.session):
        project.ensure_dirs()
        print(f"세션 '{cfg.session}' 시작 (화면 없이 서버 먼저)...")
        h.start_server(cfg.session, str(project.var_dir / "server.log"))
        from .up import up
        for line in up(ctx).lines():
            print("  " + line)
    elif not ctx.state["members"]:
        from .up import up
        for line in up(ctx).lines():
            print("  " + line)
    else:
        from .up import ensure_control, ensure_workspace
        from .watch import detect
        try:  # the _hc tab may have been closed: bring it (and its watcher) back
            ensure_control(ctx, ensure_workspace(ctx), start_watch=True)
            ctx.state.save()
        except HerdrError as e:
            print(f"_hc 탭을 다시 만들지 못했습니다: {e} (hctrl up으로 다시 시도)")
        from .up import live_own_agents, reapply_launch_args
        try:  # agents herdr brought back by itself (server restart) lack hctrl's launch args
            for line in reapply_launch_args(ctx, live_own_agents(ctx)):
                print("  " + line)
            ctx.state.save()
        except HerdrError as e:
            print(f"agent 상태를 확인하지 못했습니다: {e}")
        pending = detect(ctx)
        if pending:
            print(f"설정 변경 {len(pending)}건이 반영 대기 중입니다 (세션 안에서 hctrl changes).")
    print(f"'{cfg.session}' 세션을 엽니다.")
    sys.stdout.flush()
    os.chdir(project.root)  # as if hctrl had been run from the project folder
    cd_file = os.environ.get("HC_CD_FILE")  # set by the `hctrl shell-init` wrapper
    if cd_file:
        try:
            Path(cd_file).write_text(str(project.root))
        except OSError:
            pass
    os.execvpe(h.binary, [h.binary, "--session", cfg.session], clean_env())
    return 0  # pragma: no cover


def cmd_list() -> int:
    reg = load_registry()
    if not reg:
        print("등록된 프로젝트가 없습니다 (hctrl --add)")
        return 0
    try:
        running = {r["name"]: r["status"] for r in Herdr().session_list()}
    except HerdrError:
        running = {}
    for name, path in sorted(reg.items()):
        p = Project(name, Path(path))
        session = name
        if p.config_path.exists():
            cfg, _ = check_file(p.config_path)
            session = cfg.session if cfg else name
        print(f"{name:<20} {running.get(session, '-'):<8} {path}")
    return 0


def cmd_add(name: str | None) -> int:
    cwd = Path.cwd()
    name = name or cwd.name
    register(name, cwd)
    print(f"✔ 등록: {name} → {cwd.resolve()}")
    return 0


# ------------------------------------------------------------ commands
HERDR_INSTALL_HELP = """herdr가 설치되어 있지 않습니다. hctrl은 herdr 위에서 동작하므로 먼저 herdr를 설치하세요.

설치 방법 (https://herdr.dev/tui/docs/install/):
  공식 스크립트:  curl -fsSL https://herdr.dev/install.sh | sh
  Homebrew:       brew install herdr
  mise:           mise use -g herdr

설치 후 새 터미널에서 'herdr --version'으로 확인한 뒤 hctrl init을 다시 실행하세요.
(설치 위치가 PATH에 없으면 HC_HERDR_BIN=/경로/herdr 로 지정할 수 있습니다)"""


def herdr_installed() -> bool:
    custom = os.environ.get("HC_HERDR_BIN")
    if custom:
        return os.path.isfile(custom) and os.access(custom, os.X_OK)
    return shutil.which("herdr") is not None


def _managed_pane() -> dict | None:
    """Registry entry of the hctrl-managed agent pane we run in (any project), else None."""
    from .orchestrator import pane_identity, registered_member
    ident = pane_identity()
    if not ident or not ident[0]:
        return None
    entry = registered_member(ident[0], ident[1])
    return entry if entry and entry.get("type") in ("orch", "agent") else None


def show_update_notice() -> None:
    """User terminals only: print the cached new-version notice and refresh the cache in the background."""
    from . import update
    if _managed_pane():
        return
    msg = update.notice()
    if msg:
        print("ℹ " + msg, flush=True)
    update.refresh_in_background()


def cmd_update(args) -> int:
    import urllib.error
    from . import update
    if _managed_pane():
        raise ScopeError("agent 창에서는 hctrl update를 사용할 수 없습니다 (사용자 전용)")
    try:
        return update.run_update(check_only=args.check)
    except update.UpdateError as e:
        raise Fail(str(e)) from e
    except urllib.error.HTTPError as e:
        raise Fail(f"릴리스 파일을 받지 못했습니다 ({e.code} {e.url}). 릴리스 직후라면 잠시 뒤 다시 실행하세요") from e
    except urllib.error.URLError as e:
        raise Fail(f"GitHub에 연결하지 못했습니다: {e.reason}") from e
    except OSError as e:  # e.g. no write permission on the install directory
        raise Fail(f"설치 파일을 바꾸지 못했습니다: {e}") from e


def cmd_update_check(args) -> int:
    from . import update
    update.check_now()
    return 0


def cmd_init(args) -> int:
    from .init_cmd import run_init
    if _managed_pane():
        raise ScopeError("agent 창에서는 hctrl init을 사용할 수 없습니다 (사용자 전용)")
    if not herdr_installed():
        print(HERDR_INSTALL_HELP, file=sys.stderr)
        return 1
    project = find_project() if not args.project else find_project(Path(args.project))
    if project is None:
        cwd = Path(args.project or Path.cwd()).resolve()
        project = Project(cwd.name, cwd)
    if args.template:
        if args.tab:
            raise Fail("--template은 새 설정에만 씁니다. 기존 설정에 템플릿 탭을 더하려면 설정 화면의 "
                       "[+ 템플릿에서 추가]를 쓰세요")
        from .init_cmd import run_init_template
        return 0 if run_init_template(project, args.template) else 1
    ok = run_init(project, add_tab=args.tab)
    return 0 if ok else 1


def cmd_ui(args) -> int:
    if _managed_pane():
        raise ScopeError("agent 창에서는 hctrl ui를 사용할 수 없습니다 (사용자 전용)")
    if args.name and args.name != ".":
        start = project_by_name(args.name)
        if start is None:
            raise Fail(f"등록된 프로젝트 '{args.name}'이 없습니다 (hctrl --list)")
    else:
        start = find_project(Path(args.project) if args.project else None)
    from .ui import run_launcher, run_settings
    show_update_notice()
    if os.environ.get("HERDR_ENV") == "1":
        return run_settings(start)  # inside herdr: settings only, sessions open from a normal terminal
    chosen = run_launcher(Path.cwd(), start=start)
    return cmd_open(None, chosen) if chosen else 0


SHELL_INIT = """# hctrl: after the herdr session detaches, stay in the project folder that was opened.
hctrl() {
  local __hc_cd __hc_rc
  __hc_cd="$(mktemp)" || { command hctrl "$@"; return; }
  HC_CD_FILE="$__hc_cd" command hctrl "$@"; __hc_rc=$?
  if [ -s "$__hc_cd" ]; then cd -- "$(cat -- "$__hc_cd")" || true; fi
  rm -f -- "$__hc_cd"
  return $__hc_rc
}
"""


def cmd_shell_init(args) -> int:
    print(SHELL_INIT, end="")
    return 0


def cmd_check(args) -> int:
    project = resolve_project(args.project)
    integrations = None
    try:
        integrations = Herdr().integration_status()
    except HerdrError:
        pass
    cfg, issues = check_file(project.config_path, integrations=integrations)
    print(format_issues(issues))
    return 1 if any(i.level == "error" for i in issues) else 0


def cmd_up(args) -> int:
    from .up import up
    ctx = load_ctx(args, verbose_warnings=True)
    guard(ctx, current_actor(ctx), "up")
    if not ctx.h.session_running(ctx.cfg.session):
        ctx.project.ensure_dirs()
        ctx.h.start_server(ctx.cfg.session, str(ctx.project.var_dir / "server.log"))
    for line in up(ctx, tab=args.tab, start_watch=not args.no_watch).lines():
        print(line)
    return 0


def _worktree_mode(args) -> str:
    return "force" if getattr(args, "force_worktree", False) else (
        "remove" if getattr(args, "remove_worktree", False) else "keep")


def _ask(prompt: str) -> str:
    return input(prompt).strip().lower()


def _cancelled() -> int:
    print("취소했습니다 (바뀐 것 없음)")
    return 1


def _ask_unmerged(e) -> str | None:
    """A worktree with work not in its base: keep it (default), remove anyway, or cancel."""
    print(str(e))
    if not tty():
        raise Fail(str(e) + "\n  worktree는 그대로 두려면 --remove-worktree 없이, 그래도 지우려면 --force-worktree")
    ans = _ask("  [k] worktree는 남기고 진행 (기본)  [f] 그래도 삭제 (브랜치는 유지)  [c] 취소 > ")
    if ans == "c":
        return None
    if ans == "f":
        if _ask("  정말 지울까요? 병합 안 된 커밋은 브랜치에 남지만 커밋 안 된 파일은 사라집니다 [y/N] > ") == "y":
            return "force"
        return None
    return "keep"


def _finish_tab(ctx, tab: str, mode: str, worktree: str) -> int:
    from .tabops import WorktreeNotMerged, finish
    try:
        lines = finish(ctx, tab, mode, worktree)
    except WorktreeNotMerged as e:
        worktree = _ask_unmerged(e)
        if worktree is None:
            return _cancelled()
        lines = finish(ctx, tab, mode, worktree)
    for line in lines:
        print("✔ " + line)
    return 0


def _confirm(question: str, yes: bool) -> bool:
    if yes:
        return True
    if not tty():
        raise Fail("확인이 필요합니다: --yes를 붙여 다시 실행하세요")
    return _ask(f"{question} [y/N] > ") == "y"


def _fmt_bytes(n: int) -> str:
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else (f"{n // 1024} KB" if n >= 1024 else f"{n} B")


def end_tab(ctx, args, name: str) -> int:
    """탭 종료: show the session files that go and what stays, settle the worktree, confirm, end."""
    from .tabops import CLEAN_LABELS, clean_plan, worktree_info, worktree_summary
    tab = ctx.cfg.tab(name)
    plan = clean_plan(ctx, tab)
    print(f"'{tab.name}' 탭 종료 — 지우는 것 (hctrl 세션 산출물)")
    if plan:
        for row in plan:
            print(f"  {CLEAN_LABELS[row['item']]:<14} {row['path']}" + (f"  {_fmt_bytes(row['bytes'])}" if row["bytes"] else ""))
    else:
        print("  (지울 산출물 없음)")
    print("  남는 것: 탭 설정, prompt(역할 상세), LLM 대화 원본")
    worktree = _worktree_mode(args)
    info = worktree_info(ctx, tab)
    if info is not None and worktree == "keep" and tty() and not args.yes:
        if info["safe"]:
            if _ask(f"  worktree({info['branch']}): {info['base']}에 병합됨 ✔ → 폴더와 브랜치도 정리할까요? [Y/n] > ") != "n":
                worktree = "remove"
        else:
            print("  " + worktree_summary(info) + " → worktree는 남깁니다")
    if not _confirm("종료할까요?", args.yes):
        return _cancelled()
    return _finish_tab(ctx, tab.name, "end", worktree)


def delete_tab(ctx, args, name: str) -> int:
    """탭 설정 삭제: the tab leaves config.yaml; files stay."""
    from .tabops import delete_plan
    tab = ctx.cfg.tab(name)
    print(f"'{tab.name}' 탭 설정 삭제")
    print("  지우는 것: config.yaml의 탭 '" + tab.name + "'" + "".join(", " + x for x in delete_plan(ctx, tab)))
    print("  남는 것  : prompt(역할 상세), 작업 파일, worktree")
    if ctx.state.member(next((m.name for m in tab.members), "")) is not None:
        print("  창이 열려 있어 먼저 닫습니다.")
    print("  💡 구성을 다시 쓰려면 먼저 템플릿으로 저장하세요 (hctrl ui → 템플릿)")
    if not _confirm("설정에서 지울까요?", args.yes):
        return _cancelled()
    return _finish_tab(ctx, tab.name, "delete", _worktree_mode(args))


def cmd_down(args) -> int:
    from .up import down
    ctx = load_ctx(args)
    guard(ctx, current_actor(ctx), "down")
    if args.tab is not None:
        if ctx.cfg.tab(args.tab) is None:
            raise Fail(f"탭 '{args.tab}'이 설정에 없습니다")
        return _finish_tab(ctx, args.tab, "close", _worktree_mode(args))
    closed = down(ctx)
    print("정리: " + (", ".join(closed) or "없음"))
    if args.remove_worktree or args.force_worktree:
        from .tabops import WorktreeNotMerged, remove_worktree
        for t in ctx.cfg.tabs:
            try:
                for line in remove_worktree(ctx, t, force=args.force_worktree):
                    print(line)
            except WorktreeNotMerged as e:
                print(f"{e} — 남겨 둠 (지우려면 hctrl down {t.name} --force-worktree)")
    if args.stop_session and args.tab is None:
        ctx.h.session_stop(ctx.cfg.session)
        print(f"세션 '{ctx.cfg.session}' 정지")
    return 0


def cmd_tab_action(args, ctx) -> int:
    from .tabops import TabOpError, reopen
    tab = ctx.cfg.tab(args.old)
    if tab is None:
        raise Fail(f"탭 '{args.old}'이 설정에 없습니다 (탭: {', '.join(t.name for t in ctx.cfg.tabs)})")
    try:
        if args.action == "reopen":
            for line in reopen(ctx, tab.name, resume=not args.fresh):
                print(line)
            return 0
        if args.action == "end":
            return end_tab(ctx, args, tab.name)
        return delete_tab(ctx, args, tab.name)
    except TabOpError as e:
        raise Fail(str(e)) from e


def cmd_tab(args) -> int:
    from .init_cmd import write_config
    from .tabrename import RenameError, rename_in_data, rename_tabs
    ctx = load_ctx(args)
    guard(ctx, current_actor(ctx), "tab")
    if args.action != "rename":
        return cmd_tab_action(args, ctx)
    if not args.new:
        raise Fail("새 탭 ID를 주세요: hctrl tab rename <이전> <새>")
    tab = ctx.cfg.tab(args.old)
    if tab is None:
        raise Fail(f"탭 '{args.old}'이 설정에 없습니다 (탭: {', '.join(t.name for t in ctx.cfg.tabs)})")
    old, new = tab.name, args.new
    if old == new:
        print("변경 없음")
        return 0
    if any(t.name.lower() == new.lower() and t is not tab for t in ctx.cfg.tabs):
        raise Fail(f"이미 있는 탭 ID입니다: {new}")
    data = str_keys(yaml.safe_load(ctx.project.config_path.read_text()))
    data = rename_in_data(data, old, new)
    from .config import check_data
    cfg2, issues = check_data(data, root=ctx.project.root)
    if cfg2 is None:
        raise Fail("바꾼 설정에 오류가 있습니다:\n" + format_issues(issues))
    try:
        live = ctx.h.session_running(ctx.cfg.session)
    except HerdrError:
        live = False
    try:
        report = rename_tabs(ctx.project, {old: new}, lambda: write_config(ctx.project, data), ctx.h, live)
    except RenameError as e:
        raise Fail(str(e)) from e
    for line in report:
        print(line)
    return 0


def cmd_status(args) -> int:
    from .up import status
    ctx = load_ctx(args)
    print("\n".join(status(ctx, getattr(args, "tab", None))))
    return 0


def cmd_zoom(args) -> int:
    from .up import zoom
    zoom(load_ctx(args), args.name)
    return 0


def _finish(ctx, rec, detach: bool) -> int:
    from .runner import execute, spawn_worker, summary
    if detach:
        spawn_worker(ctx.project, rec)
        print(rec["id"])
        return 0
    rec = execute(ctx, rec)
    print("\n".join(summary(rec)))
    return 0 if rec["status"] == "done" else 1


def cmd_run(args) -> int:
    from .runner import create_run
    ctx = load_ctx(args)
    flow = ctx.cfg.flows.get(args.flow)
    if flow is None:
        raise Fail(f"flow '{args.flow}'이 없습니다 (있는 flow: {', '.join(ctx.cfg.flows) or '-'})")
    actor = current_actor(ctx)
    enforce(ctx, actor, sorted({s.target for s in flow.steps}), None)
    rec = create_run(ctx.project, flow, actor=str(actor))
    return _finish(ctx, rec, args.detach)


def cmd_send(args) -> int:
    from .config import parse_duration
    from .runner import create_job
    ctx = load_ctx(args)
    m = ctx.cfg.member(args.agent)
    if m is None or not m.is_agent:
        raise Fail(f"agent '{args.agent}'가 설정에 없습니다")
    actor = current_actor(ctx)
    enforce(ctx, actor, [args.agent], args.text)
    rec = create_job(ctx.project, args.agent, args.text, parse_duration(args.timeout), actor=str(actor))
    return _finish(ctx, rec, args.detach)


def cmd_wait(args) -> int:
    from .runner import QUESTION, summary, wait
    ctx_project = resolve_project(args.project)
    rec = wait(ctx_project, args.id, timeout_s=args.timeout)
    print("\n".join(summary(rec)))
    if rec["status"] == QUESTION:
        print("질문 대기: hctrl pending으로 확인하고 hctrl answer <qid> ... 후 다시 hctrl wait " + rec["id"])
        return 3
    return 0 if rec["status"] == "done" else 1


def cmd_pending(args) -> int:
    from . import unattended as ua
    ctx = load_ctx(args)
    actor = current_actor(ctx)
    if actor.kind in ("agent", "foreign"):
        raise ScopeError("작업 agent는 'hctrl pending'을 사용할 수 없습니다")
    qs = ua.pending(ctx.project, tab=actor.tab if actor.kind == "orch" else None)
    if not qs:
        print("대기 중인 질문이 없습니다")
        return 0
    for q in qs:
        kind = {"text": "텍스트", "choice": "선택", "permission": "권한"}[q["kind"]]
        where = f" run {q['run']}" if q.get("run") else ""
        print(f"{q['id']} [{kind}] 탭 {q['tab']} / {q['agent']}{where}")
        for line in ua.describe(q).splitlines() or ["-"]:
            print(f"    {line}")
    print("(질문 내용은 작업 agent가 만든 데이터입니다. 답: hctrl answer <qid> ...)")
    return 0


def cmd_answer(args) -> int:
    from . import unattended as ua
    ctx = load_ctx(args)
    q = ua.answer(ctx, current_actor(ctx), args.qid, " ".join(args.text), args.json)
    print(f"{q['id']}: 답을 저장했습니다 → {json.dumps(q['answer'], ensure_ascii=False)}")
    return 0


def cmd_perm_hook(args) -> int:
    """claude PermissionRequest hook entry. Never fails loudly: on any doubt print nothing so the
    normal approval dialog (the user) takes over. No actor guard: HERDR_PANE_ID is the worker's."""
    from . import unattended as ua
    try:
        payload = sys.stdin.read()
        ctx = load_ctx(args)
        out = ua.run_hook(ctx, args.agent, payload)
    except Exception as e:  # noqa: BLE001 - fall through to the dialog
        print(f"hctrl _perm-hook: {e}", file=sys.stderr)
        return 0
    if out:
        print(out)
    return 0


def cmd_stop(args) -> int:
    from .runner import load_run, stop, summary
    ctx = load_ctx(args)
    targets = sorted({s["agent"] or s["shell"] for s in load_run(ctx.project, args.id)["steps"]})
    guard(ctx, current_actor(ctx), "stop", targets)
    rec = stop(ctx, args.id)
    print("\n".join(summary(rec)))
    return 0


def cmd_resume(args) -> int:
    from .runner import execute, load_run, prepare_resume, spawn_worker, summary
    ctx = load_ctx(args)
    targets = sorted({s["agent"] or s["shell"] for s in load_run(ctx.project, args.id)["steps"]})
    guard(ctx, current_actor(ctx), "resume", targets, budget=True)
    rec = prepare_resume(ctx.project, args.id)
    if args.detach:
        spawn_worker(ctx.project, rec, resume=True, resend=args.resend)
        print(rec["id"])
        return 0
    rec = execute(ctx, rec, resume=True, resend=args.resend)
    print("\n".join(summary(rec)))
    return 0 if rec["status"] == "done" else 1


def cmd_worker(args) -> int:
    from .runner import execute, load_run
    ctx = load_ctx(args)
    rec = load_run(ctx.project, args.id)
    rec = execute(ctx, rec, resume=args.resume, resend=args.resend)
    print(f"{rec['id']} {rec['status']}")
    return 0


def cmd_ask(args) -> int:
    from .orchestrator import ask
    ctx = load_ctx(args)
    guard(ctx, current_actor(ctx), "ask")
    ask(ctx, args.tab, args.text)
    print(f"orchestrator '{orch_name(ctx.cfg.tab(args.tab).name)}'에게 전달했습니다. 진행은 화면이나 hctrl status로 확인하세요.")
    return 0


def _draft(ctx, m) -> str:
    one, detail = roles_mod.effective_role(ctx.project, m)
    tab = ctx.cfg.tab(m.tab)
    goal = roles_mod.effective_role(ctx.project, tab.orchestrator)[0] if tab and tab.orchestrator else ""
    prompt = (f"코딩 agent '{m.name}'({m.kind})의 상세 역할 지시문을 한국어 마크다운으로 작성해 주세요. "
              f"한 줄 역할: '{one}'. 프로젝트 목표: '{goal or '-'}'. "
              "항목: 담당 범위, 하지 말아야 할 것, 완료 기준, 결과 보고 방식. 20줄 이내, 제목 없이 본문만.")
    cmd = shlex.split(os.environ.get("HC_DRAFT_CMD", "claude -p")) + [prompt]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ctx.project.root), timeout=300)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise Fail(f"초안 생성 실패: {(proc.stderr or proc.stdout).strip()[:300]}")
    return proc.stdout.strip()


def cmd_role(args) -> int:
    from .watch import notify_role
    ctx = load_ctx(args)
    project, cfg = ctx.project, ctx.cfg
    actor = current_actor(ctx)
    if actor.kind != "user":
        setting = args.name and args.text is not None and not any(
            (args.edit, args.draft, args.apply, args.history, args.revert, args.review, args.accept, args.reject))
        if args.apply and args.name:
            guard(ctx, actor, "role --apply", [args.name])
        elif not setting and (args.name is None or any((args.edit, args.draft, args.history, args.revert,
                                                              args.review, args.accept, args.reject))):
            raise Fail("역할 승인/편집/되돌리기는 사용자만 할 수 있습니다")
    if args.review or args.accept or args.reject:
        if args.accept or args.reject:
            p = roles_mod.resolve_proposal(project, cfg, args.accept or args.reject, accept=bool(args.accept))
            print(f"제안 {p['id']} ({p['name']}): {p['status']}")
            return 0
        items = roles_mod.proposals(project)
        if not items:
            print("대기 중인 역할 변경 제안이 없습니다")
            return 0
        for p in items:
            print(f"#{p['id']} {p['name']}: '{p['role']}'  (제안: {p['actor']}, 이유: {p['reason'] or '-'})")
            if tty():
                ans = input("  [a] 승인  [r] 거절  [s] 나중에 > ").strip().lower()
                if ans in ("a", "r"):
                    roles_mod.resolve_proposal(project, cfg, p["id"], accept=ans == "a")
        return 0
    if not args.name:
        for t in cfg.tabs:
            print(f"[{t.name}] 역할 변경 정책: {t.role_update}")
            for m in t.members:
                if not m.is_agent:
                    continue
                one, detail = roles_mod.effective_role(project, m)
                hist = roles_mod.history(project, m.name)
                last = f"최근 변경: {hist[-1]['actor']}" if hist else ""
                print(f"  {m.name:<14} {one or '-':<30} 상세:{'있음' if detail else '없음'}  {last}")
        pend = roles_mod.proposals(project)
        if pend:
            print(f"승인 대기 제안 {len(pend)}건 (hctrl role --review)")
        return 0
    m = cfg.member(args.name)
    if m is None or not m.is_agent:
        raise Fail(f"agent '{args.name}'가 설정에 없습니다")
    if args.history:
        for e in roles_mod.history(project, m.name):
            first = (e["after"].splitlines() or [""])[0]
            print(f"#{e['n']} {e['actor']:<10} {e['reason'] or '-':<20} {first}")
        return 0
    if args.revert:
        roles_mod.revert(project, m, args.revert)
        print(f"{m.name}: #{args.revert} 이전 상태로 되돌렸습니다 (실행 중 agent에는 hctrl role {m.name} --apply)")
        return 0
    if args.edit:
        f = roles_mod.ensure_template(project, m)
        before = f.read_text()
        editor = os.environ.get("EDITOR") or "vi"
        if tty():
            subprocess.call(shlex.split(editor) + [str(f)])
            after = f.read_text()
            if after != before:
                roles_mod._record(project, m.name, "user", "edit", before, after)
        print(f"역할 파일: {f}")
        return 0
    if args.draft:
        text = _draft(ctx, m)
        draft = project.roles_dir / f"{m.name}.draft.md"
        draft.write_text(text + "\n")
        print(text)
        if tty() and input("이 초안을 상세 역할로 저장할까요? [y/N] > ").strip().lower().startswith("y"):
            roles_mod.write_role(project, m, detail=text, actor="user", reason="draft")
            draft.unlink()
            print("저장했습니다")
        else:
            print(f"초안 파일: {draft}")
        return 0
    if args.apply:
        print(notify_role(ctx, m.name))
        return 0
    if args.text is None:
        one, detail = roles_mod.effective_role(project, m)
        print(f"{m.name}: {one}\n{detail}")
        return 0
    tab = cfg.tab(m.tab)
    if actor.kind == "agent":
        raise Fail("작업 agent는 역할을 직접 바꿀 수 없습니다. 결과 파일에 '역할 조정 제안'을 남기세요")
    if actor.kind == "orch":
        if m.tab != actor.tab:
            raise Fail("다른 탭 agent의 역할은 바꿀 수 없습니다")
        if m.type == "orch":
            raise Fail("orchestrator는 자신의 역할을 바꿀 수 없습니다")
        if tab.role_update == "off":
            raise Fail(f"탭 {tab.name}의 역할 변경 정책이 off입니다")
        if tab.role_update == "confirm":
            pid = roles_mod.propose(project, m.name, args.text, actor.name, args.reason or "")
            ctx.h.notify("hctrl: 역할 변경 제안", f"{m.name}: {args.text} (hctrl role --review)")
            print(f"제안 #{pid}로 저장했습니다. 사용자 승인 후 반영됩니다")
            return 0
        roles_mod.write_role(project, m, one=args.text, actor=actor.name, reason=args.reason or "")
        ctx.h.notify("hctrl: 역할 변경(자동 반영)", f"{m.name}: {args.text}")
        try:
            print(notify_role(ctx, m.name))
        except (RuntimeError, HerdrError) as e:
            print(f"역할 파일은 바꿨지만 agent에게 알리지 못했습니다: {e}")
        return 0
    roles_mod.write_role(project, m, one=args.text, actor="user", reason=args.reason or "")
    print(f"{m.name}: 역할을 바꿨습니다. 실행 중인 agent에 반영: hctrl role {m.name} --apply 또는 hctrl restart {m.name}")
    return 0


def cmd_changes(args) -> int:
    from .watch import apply_change, default_actions, detect, save_pending
    ctx = load_ctx(args)
    guard(ctx, current_actor(ctx), "changes")
    changes = detect(ctx, tab=args.tab)
    if not changes:
        if args.tab is None:
            save_pending(ctx.project, [])
        print("반영 대기 중인 변경이 없습니다" + (f" (탭 {args.tab})" if args.tab else ""))
        return 0
    labels = {"notify": "[a] 알리기", "restart": "[r] 재시작", "fresh": "[f] 인수인계 없이 재시작", "add": "[c] 추가",
              "remove": "[x] 닫기", "skip": "[s] 건너뛰기", "ack": "[k] 확인(옮기지 않음)", "move": "[m] 옮기기"}
    keys = {"a": "notify", "r": "restart", "f": "fresh", "c": "add", "x": "remove", "s": "skip", "k": "ack", "m": "move"}
    remaining = []
    for ch in changes:
        print(f"{ch.name}: {ch.detail}")
        actions = default_actions(ch)
        if args.list:
            remaining.append(ch)
            continue
        if args.yes:
            action = actions[0]
        elif tty():
            ans = input("  " + "  ".join(labels[a] for a in actions) + " > ").strip().lower()
            action = keys.get(ans, "skip")
            if action not in actions:
                action = "skip"
        else:
            remaining.append(ch)
            continue
        try:
            print("  → " + apply_change(ctx, ch, action, handoff=not args.no_handoff))
        except (RuntimeError, KeyError, HerdrError, LockBusy) as e:
            print(f"  ✘ {e}")
            remaining.append(ch)
        if action == "skip":
            remaining.append(ch)
    if args.tab is not None:  # other tabs' changes stay pending as they were
        mine = {c.name for c in changes}
        remaining += [c for c in detect(ctx) if c.name not in mine]
    save_pending(ctx.project, list({id(c): c for c in remaining}.values()))
    return 0


def cmd_restart(args) -> int:
    """Default: continue the same conversation (reloads settings, skills, MCP). --fresh: a new one with a
    handoff file; --no-handoff: a new one without."""
    from .watch import restart
    ctx = load_ctx(args)
    actor = current_actor(ctx)
    resume = not (args.fresh or args.no_handoff)
    if args.tab:
        from .tabops import restart_tab
        guard(ctx, actor, "restart")  # a whole tab (orchestrator included): the user only
        for line in restart_tab(ctx, args.tab, resume=resume, handoff=not args.no_handoff):
            print(line)
        return 0
    if not args.name:
        raise Fail("재시작할 agent 이름이나 --tab <탭>을 주세요")
    guard(ctx, actor, "restart", [args.name])
    if actor.name == args.name:
        raise Fail("orchestrator는 자기 자신을 재시작할 수 없습니다")
    print(restart(ctx, args.name, handoff=not args.no_handoff, resume=resume))
    return 0


# set across a watcher restart that the settings UI asked for ("1": the open page reloads itself) or that
# the user's [s] press caused ("announce": also open a fresh link in the browser)
REOPEN_UI_ENV = "HCTRL_REOPEN_UI"
RESUME_UI_ENV = "HCTRL_RESUME_UI"  # "<port>:<cookie secret>" of the settings UI across a watcher restart


def cmd_watch(args) -> int:
    from .watch import watch_loop
    project = resolve_project(args.project)
    cfg, _ = check_file(project.config_path)
    session = cfg.session if cfg else project.name
    h = make_herdr(session)
    if not tty():
        watch_loop(project, h, interval=args.interval)
        return 0
    from .watch import Menu, Reexec, TtyKeys
    from . import __version__, update
    from .selfcmd import self_cmd
    update.refresh_in_background()  # the _hc pane is the user's: it may check (watch_loop shows it)
    from .state import release_own
    release_own(project)  # locks left by this same pid before an in-place restart (os.execv)
    again: Reexec | None = None
    resume = None
    with TtyKeys() as keys:  # _hc tab: key menu (settings UI, changes, status, pending)
        menu = Menu(project, cooked=keys.cooked, fixed=True)  # target tab pinned at the top of the pane
        try:
            reopen = os.environ.pop(REOPEN_UI_ENV, None)
            port, _, secret = os.environ.pop(RESUME_UI_ENV, "").partition(":")
            resumed = port.isdigit() and bool(secret) and menu.resume_ui(int(port), secret)
            if reopen and not resumed:  # restarted from the settings UI: bring it back
                menu.settings()
            elif reopen == "announce" and menu.ui is not None:  # [s] pressed: the user expects a browser window
                from .ui import announce
                announce(menu.ui, project.name)
            watch_loop(project, h, interval=args.interval, keys=keys, menu=menu)
        except Reexec as e:
            again = e
            if menu.ui is not None:
                resume = f"{menu.ui.port}:{menu.ui.secret}"
        finally:
            menu.stop()
            menu.close()
    if again is None:
        return 0
    # the tty is restored and the UI server stopped: become the installed version in this same pane
    print(f"[hctrl watch] v{__version__} → v{again.new} 로 다시 시작", flush=True)
    if again.reopen_ui:
        os.environ[REOPEN_UI_ENV] = "announce" if again.announce else "1"
    if resume:  # the new process serves the same address, so an open settings page keeps working
        os.environ[RESUME_UI_ENV] = resume
    cmd = self_cmd() + ["--project", str(project.root), "watch", "--interval", str(args.interval)]
    os.execv(cmd[0], cmd)
    return 0  # not reached


# ----------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hctrl", description="herdr_ctl: 프로젝트 세션, 화면 배치, agent 파이프라인")
    p.add_argument("--project", help="프로젝트 경로 (기본: 현재 위치에서 찾기)")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("init", help="대화형으로 config.yaml 생성 (끝나면 자동 검사)")
    s.add_argument("--tab", help="기존 설정에 탭 추가")
    s.add_argument("--template", help="저장한 팀 템플릿으로 새 config.yaml 만들기 (설정 화면의 [☆ 템플릿으로 저장])")
    sub.add_parser("check", help="설정 검사")
    s = sub.add_parser("ui", help="브라우저 설정 화면 (프로젝트 선택/추가, config 편집)")
    s.add_argument("name", nargs="?", help="처음 보여 줄 프로젝트 ('.' = 현재 폴더)")
    s = sub.add_parser("shell-init", help="세션을 연 뒤 쉘도 프로젝트 폴더로 이동하게 하는 함수 출력")
    s.add_argument("shell", nargs="?", choices=["bash", "zsh"], default="bash")
    s = sub.add_parser("up", help="탭/창/agent 구성 (멱등)")
    s.add_argument("tab", nargs="?")
    s.add_argument("--no-watch", action="store_true", help="설정 감시를 시작하지 않음")
    s = sub.add_parser("down", help="창과 agent 닫기 (탭을 주면 그 탭만, 다시 열기: hctrl tab reopen)")
    s.add_argument("tab", nargs="?")
    s.add_argument("--remove-worktree", action="store_true", help="기준 브랜치에 병합됐을 때만 worktree 폴더 정리")
    s.add_argument("--force-worktree", action="store_true", help="병합 안 됐어도 worktree 폴더 삭제 (브랜치는 유지)")
    s.add_argument("--stop-session", action="store_true", help="(전체 정리 시) herdr 세션도 정지")
    s = sub.add_parser("tab", help="탭 관리: end <탭> (세션 종료·산출물 정리) | reopen <탭> | delete <탭> (설정 삭제) "
                                   "| rename <이전> <새>")
    s.add_argument("action", choices=["end", "reopen", "delete", "rename"])
    s.add_argument("old", metavar="tab")
    s.add_argument("new", nargs="?")
    s.add_argument("--fresh", action="store_true", help="reopen: 이전 대화를 잇지 않고 새로 시작")
    s.add_argument("--remove-worktree", action="store_true", help="end/delete: 병합됐을 때만 worktree 폴더 정리")
    s.add_argument("--force-worktree", action="store_true", help="병합 안 됐어도 worktree 폴더 삭제 (브랜치는 유지)")
    s.add_argument("--yes", action="store_true", help="확인 없이 진행")
    s = sub.add_parser("status", help="상태")
    s.add_argument("tab", nargs="?")
    s = sub.add_parser("zoom", help="창 하나 크게 보기 / 복귀")
    s.add_argument("name")
    s = sub.add_parser("run", help="flow 실행 (A 방식)")
    s.add_argument("flow")
    s.add_argument("--detach", action="store_true")
    s = sub.add_parser("send", help="agent 하나에 지시")
    s.add_argument("agent")
    s.add_argument("text")
    s.add_argument("--detach", action="store_true")
    s.add_argument("--timeout", default=None, help="예: 30m")
    s = sub.add_parser("wait", help="detach된 실행 대기")
    s.add_argument("id")
    s.add_argument("--timeout", type=float, default=None, help="초")
    s = sub.add_parser("stop", help="실행 중단")
    s.add_argument("id")
    s = sub.add_parser("resume", help="멈춘 실행 이어가기")
    s.add_argument("id")
    s.add_argument("--resend", action="store_true", help="멈춘 agent 단계의 지시를 다시 보냄")
    s.add_argument("--detach", action="store_true")
    s = sub.add_parser("ask", help="orchestrator에게 지시 (B 방식)")
    s.add_argument("tab")
    s.add_argument("text")
    s = sub.add_parser("role", help="역할 보기/변경")
    s.add_argument("name", nargs="?")
    s.add_argument("text", nargs="?")
    s.add_argument("--reason", default="")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--edit", action="store_true")
    g.add_argument("--draft", action="store_true")
    g.add_argument("--apply", action="store_true")
    g.add_argument("--history", action="store_true")
    g.add_argument("--revert", type=int)
    g.add_argument("--review", action="store_true")
    g.add_argument("--accept", type=int)
    g.add_argument("--reject", type=int)
    s = sub.add_parser("changes", help="설정 변경 확인 및 반영")
    s.add_argument("--list", action="store_true")
    s.add_argument("--tab", help="이 탭의 변경만")
    s.add_argument("--yes", action="store_true", help="각 변경에 기본 조치 적용")
    s.add_argument("--no-handoff", action="store_true")
    s = sub.add_parser("restart", help="agent 재시작 (기본: 같은 대화로 이어서, 설정·skill·MCP 다시 불러옴)")
    s.add_argument("name", nargs="?")
    s.add_argument("--tab", help="탭 전체 (작업 agent 먼저, orchestrator 마지막)")
    s.add_argument("--fresh", action="store_true", help="새 대화로 시작 (인수인계 파일 전달)")
    s.add_argument("--no-handoff", action="store_true", help="새 대화로 시작 (인수인계 없이)")
    s = sub.add_parser("watch", help="설정 변경 감시 (_hc 탭에서 자동 실행)")
    s.add_argument("--interval", type=float, default=2.0)
    sub.add_parser("pending", help="무인 모드: 답을 기다리는 질문 목록")
    s = sub.add_parser("answer", help="무인 모드: 질문에 답하기")
    s.add_argument("qid")
    s.add_argument("text", nargs="*", help="텍스트 답 / 선택지 번호·이름 / allow / deny [사유]")
    s.add_argument("--json", help="선택 질문이 여러 개일 때 {\"질문\": \"선택지\"}")
    s = sub.add_parser("update", help="새 버전으로 업데이트 (hctrl.pyz 설치)")
    s.add_argument("--check", action="store_true", help="확인만 하고 설치하지 않음")
    sub.add_parser("_update-check")
    s = sub.add_parser("_perm-hook")
    s.add_argument("agent")
    s = sub.add_parser("_worker")
    s.add_argument("id")
    s.add_argument("--resume", action="store_true")
    s.add_argument("--resend", action="store_true")
    return p


HANDLERS = {"init": cmd_init, "check": cmd_check, "up": cmd_up, "down": cmd_down, "status": cmd_status,
            "zoom": cmd_zoom, "run": cmd_run, "send": cmd_send, "wait": cmd_wait, "stop": cmd_stop,
            "resume": cmd_resume, "ask": cmd_ask, "role": cmd_role, "changes": cmd_changes,
            "restart": cmd_restart, "watch": cmd_watch, "pending": cmd_pending, "answer": cmd_answer,
            "_worker": cmd_worker, "update": cmd_update, "_update-check": cmd_update_check, "_perm-hook": cmd_perm_hook, "ui": cmd_ui, "shell-init": cmd_shell_init,
            "tab": cmd_tab}


# commands that may still be running when `hctrl update` swaps the .pyz (launcher/open handled in main)
LONG_RUNNING = {"watch", "_worker", "ui", "run", "resume", "wait", "ask", "send", "up", "down", "changes",
                "restart", "init", "role"}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in LONG_RUNNING or (argv[0] not in SUBCOMMANDS and not argv[0].startswith("-")):
        from .selfcmd import preload
        preload()  # zipapp: survive `hctrl update` replacing the archive while we run (costs ~0.2s)
    try:
        if not argv:
            return cmd_open(None)
        if argv[0] == "--list":
            return cmd_list()
        if argv[0] == "--add":
            return cmd_add(argv[1] if len(argv) > 1 else None)
        if argv[0] in ("-V", "--version"):
            from . import __version__, update
            print(f"hctrl (herdr_ctl) {__version__}")
            msg = update.notice()  # cache only: --version stays offline and fast
            if msg and tty():
                print(msg)
            return 0
        if argv[0] in ("-h", "--help"):
            build_parser().print_help()
            return 0
        if argv[0] not in SUBCOMMANDS and not argv[0].startswith("-"):
            return cmd_open(argv[0])
        args = build_parser().parse_args(argv)
        if not args.cmd:
            return cmd_open(None)
        return HANDLERS[args.cmd](args)
    except (Fail, KeyError, RuntimeError, LockBusy, LimitExceeded, ScopeError, ValueError, OSError,
            yaml.YAMLError) as e:
        msg = e.args[0] if isinstance(e, KeyError) and e.args else str(e)
        print(f"hctrl: {msg}", file=sys.stderr)
        return 1
    except HerdrError as e:
        print(f"hctrl: herdr 오류 — {e}", file=sys.stderr)
        return 2
    except EOFError:
        print("hctrl: 입력이 끝났습니다 (대화형 터미널에서 실행하세요)", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
