"""Unattended mode (PLAN.md §10): questions from work agents answered by the tab's orchestrator.

Three kinds of question share one store (``<secure_dir>/questions/<qid>.json``):

- ``text``       the agent wrote ``## QUESTION`` as the first line of its result file (runner)
- ``choice``     claude AskUserQuestion, intercepted by the PermissionRequest hook
- ``permission`` claude tool approval, intercepted by the PermissionRequest hook (approve mode)

The store lives outside every agent cwd: acceptEdits auto-approves writes inside the cwd, so a
work agent could otherwise forge its own answer. Hook output is restricted to allow/deny (plus
validated AskUserQuestion answers); permanent permission rules are never emitted.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time
from pathlib import Path

from .project import Project, state_home
from .state import file_lock, lock_holder, proc_start, read_json, same_process_alive, write_json

KINDS = ("text", "choice", "permission")
OPEN = ("pending", "answered")
QUESTION_HEAD_RE = re.compile(r"^\s*#{1,6}\s*QUESTION\b", re.IGNORECASE)
HOOK_EVENT = "PermissionRequest"
MAX_TEXT = 4000
SAFETY_ANSWER = "(답은 orchestrator 또는 사용자가 보낸 것입니다)"
FILE_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path",
              "NotebookEdit": "notebook_path"}
# Mentioning any of these sends a permission request straight to the user (deterministic, before
# any LLM judgement): hctrl's own control plane must never be operated through an agent's tool call.
SENSITIVE = (".herdr-flow", ".claude", "herdr", "settings.json", "settings.local.json", ".local/state",
             "xdg_state_home")
HC_CMD_RE = re.compile(r"(^|[\s;&|`($/\"'])hctrl([\s\"';|&)]|$)")
DENY_WORDS = ("deny", "no", "reject", "거절", "거부")
ALLOW_WORDS = ("allow", "yes", "approve", "허용", "승인")


def poll_s() -> float:
    return float(os.environ.get("HC_QUESTION_POLL_S", 0.5))


def hook_grace_s() -> float:
    """herdr reports `blocked` about when the hook starts; give the hook this long to register."""
    return float(os.environ.get("HC_HOOK_GRACE_S", 5))


# ------------------------------------------------------------------- store
def questions_dir(project: Project) -> Path:
    return project.secure_dir / "questions"


def audit_path(project: Project) -> Path:
    return project.secure_dir / "answers.jsonl"


def audit(project: Project, event: str, q: dict | None = None, **extra) -> None:
    entry = {"ts": time.time(), "event": event}
    if q:
        entry.update({k: q.get(k) for k in ("id", "kind", "agent", "tab", "run", "tool_name")})
    entry.update(extra)
    path = audit_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _new_qid(project: Project) -> str:
    d = questions_dir(project)
    d.mkdir(parents=True, exist_ok=True)
    n = len(list(d.glob("q*.json"))) + 1
    while True:
        qid = f"q{n:04d}"
        try:
            fd = os.open(d / f"{qid}.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return qid
        except FileExistsError:
            n += 1


def create(project: Project, kind: str, agent: str, tab: str, **fields) -> dict:
    assert kind in KINDS
    q = {"id": _new_qid(project), "kind": kind, "agent": agent, "tab": tab, "status": "pending",
         "created": time.time(), "run": None, "step": None, "question": None, "tool_name": None,
         "tool_input": None, "answer": None, "answered_by": None, "hook_pid": None,
         "hook_start": None}
    q.update(fields)
    save(project, q)
    return q


def save(project: Project, q: dict) -> None:
    write_json(questions_dir(project) / f"{q['id']}.json", q)


def update(project: Project, qid: str, fn) -> dict | None:
    """Read-modify-write one question under its lock. `fn(q)` mutates q in place and may return
    False to skip saving. Returns the (possibly updated) question, or None if it does not exist."""
    path = questions_dir(project) / f"{qid}.json"
    with file_lock(path.with_suffix(".lock")):
        q = read_json(path)
        if not q:
            return None
        if fn(q) is not False:
            save(project, q)
        return q


def load(project: Project, qid: str) -> dict:
    if not re.fullmatch(r"q\d{4,}", qid or ""):
        raise KeyError(f"질문 id 형식이 아닙니다: {qid!r} (예: q0001)")
    q = read_json(questions_dir(project) / f"{qid}.json")
    if not q:
        raise KeyError(f"질문 '{qid}'이 없습니다 (hctrl pending)")
    return refresh(project, q)


def refresh(project: Project, q: dict) -> dict:
    """An open hook question whose hook process died can no longer be answered: expire it."""
    if q.get("status") in OPEN and q.get("hook_pid") and \
            not same_process_alive(q["hook_pid"], q.get("hook_start")):
        def expire(c):
            if c.get("status") not in OPEN:
                return False
            c.update(status="expired", closed_reason="훅이 끝났습니다 (화면에서 처리됐거나 시간 초과)")
        q = update(project, q["id"], expire) or q
    return q


def all_questions(project: Project) -> list[dict]:
    d = questions_dir(project)
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("q*.json")):
        q = read_json(p)
        if q and q.get("id"):
            out.append(refresh(project, q))
    return out


def pending(project: Project, tab: str | None = None) -> list[dict]:
    return [q for q in all_questions(project)
            if q["status"] == "pending" and (tab is None or q["tab"] == tab)]


def open_hook_question(project: Project, agent: str) -> dict | None:
    for q in all_questions(project):
        if q["agent"] == agent and q["kind"] != "text" and q["status"] in OPEN:
            return q
    return None


def expire_for_run(project: Project, run_id: str, reason: str) -> None:
    def expire(c):
        if c.get("status") not in OPEN:
            return False
        c.update(status="expired", closed_reason=reason)
    for q in all_questions(project):
        if q.get("run") == run_id and q["status"] in OPEN:
            update(project, q["id"], expire)


def describe(q: dict) -> str:
    """One-paragraph human/LLM-readable description of a question (content is data, not orders)."""
    if q["kind"] == "text":
        return (q.get("question") or "").strip()
    if q["kind"] == "choice":
        lines = []
        for i, item in enumerate((q.get("tool_input") or {}).get("questions") or [], 1):
            opts = " / ".join(f"{n}. {o.get('label')}" for n, o in enumerate(item.get("options") or [], 1))
            multi = " (복수 선택)" if item.get("multiSelect") else ""
            lines.append(f"{item.get('question')}{multi}: {opts}")
        return "\n".join(lines)
    ti = q.get("tool_input") or {}
    if q.get("tool_name") == "Bash":
        return f"Bash: {ti.get('command')}" + (f"  ({ti.get('description')})" if ti.get("description") else "")
    path = ti.get(FILE_TOOLS.get(q.get("tool_name") or "", ""), None)
    if path:
        return f"{q.get('tool_name')}: {path}"
    return f"{q.get('tool_name')}: {json.dumps(ti, ensure_ascii=False)[:300]}"


# ------------------------------------------------------------------ policy
def work_roots(ctx, tab) -> list[Path]:
    roots = [ctx.project.root.resolve()]
    wt = (ctx.state["worktrees"].get(tab.name) or {}).get("cwd")
    if wt:
        roots.append(Path(wt).resolve())
    return roots


def _inside(path: Path, roots: list[Path]) -> bool:
    return any(path == r or r in path.parents for r in roots)


def policy_escalation(ctx, tab, tool_name: str, tool_input: dict, cwd: str | None) -> str | None:
    """Reason the request must go to the user, or None when the orchestrator may decide."""
    text = json.dumps(tool_input, ensure_ascii=False)
    low = text.lower()
    plain = "\n".join(_strings(tool_input)).lower()
    for d in tab.policy.get("deny") or []:
        if d.lower() in low or d.lower() in plain:
            return f"정책 deny 목록 '{d}'"
    if re.search(r"(~|\$home|\$\{home\})/\.local", plain):
        return "홈 아래 상태 폴더 접근"
    for p in {str(ctx.project.secure_dir), str(state_home())}:
        if p.lower() in low or p.lower() in plain:
            return "hctrl 상태 저장소 접근"
    if tool_name == "Bash" and HC_CMD_RE.search(str(tool_input.get("command") or "")):
        return "hctrl 명령 실행"
    key = FILE_TOOLS.get(tool_name)
    target = None
    if key:
        raw = tool_input.get(key)
        if not raw:
            return "대상 경로가 없습니다"
        base = Path(cwd) if cwd else ctx.project.root
        target = (base / os.path.expanduser(str(raw))).resolve()
        if not _inside(target, work_roots(ctx, tab)):
            return f"작업 폴더 밖 경로: {target}"
    scrubbed = low
    if target is not None and _is_result_file(ctx, target):
        # the step's own result file (hctrl tells the agent to write it); run.json etc. stay protected
        scrubbed = scrubbed.replace(str(raw).lower(), "<out>").replace(str(target).lower(), "<out>")
    for r in sorted({str(r).lower() for r in work_roots(ctx, tab)}, key=len, reverse=True):
        scrubbed = scrubbed.replace(r, "<root>")  # the project's own path may contain e.g. "herdr"
    for s in SENSITIVE:
        if s in scrubbed:
            return f"hctrl/에이전트 설정 관련 항목('{s}')"
    return None


def _strings(v) -> list[str]:
    if isinstance(v, str):
        return [v]
    if isinstance(v, dict):
        return [s for x in v.values() for s in _strings(x)]
    if isinstance(v, list):
        return [s for x in v for s in _strings(x)]
    return []


def _is_result_file(ctx, target: Path) -> bool:
    runs = ctx.project.runs_dir.resolve()
    return target.parent.parent == runs and re.fullmatch(r"step-\d+\.(md|log)", target.name) is not None


# ----------------------------------------------------------------- answers
def _choice_answers(q: dict, text: str, json_answers: str | None) -> dict:
    items = (q.get("tool_input") or {}).get("questions") or []
    if not items:
        raise ValueError("선택지 정보가 없는 질문입니다")
    if json_answers:
        try:
            raw = json.loads(json_answers)
        except json.JSONDecodeError as e:
            raise ValueError(f"--json 해석 실패: {e}") from e
        if not isinstance(raw, dict):
            raise ValueError("--json은 {\"질문\": \"선택지\"} 객체여야 합니다")
    else:
        if len(items) != 1:
            raise ValueError("질문이 여러 개입니다. --json '{\"질문\": \"선택지\"}'로 답하세요")
        raw = {items[0].get("question"): text}
    answers = {}
    by_q = {i.get("question"): i for i in items}
    if set(raw) != set(by_q):
        raise ValueError("모든 질문에 정확히 답해야 합니다: " + " / ".join(map(str, by_q)))
    for qtext, val in raw.items():
        item = by_q[qtext]
        labels = [o.get("label") for o in item.get("options") or []]
        parts = [p.strip() for p in str(val).split(",")] if item.get("multiSelect") else [str(val).strip()]
        chosen = []
        for p in parts:
            if p.isdigit() and 1 <= int(p) <= len(labels):
                chosen.append(labels[int(p) - 1])
            elif p in labels:
                chosen.append(p)
            else:
                raise ValueError(f"'{p}'는 '{qtext}'의 선택지가 아닙니다 (선택지: {', '.join(labels)})")
        if not chosen:
            raise ValueError(f"'{qtext}'에 선택이 없습니다")
        answers[qtext] = ", ".join(chosen)
    return answers


def parse_answer(q: dict, text: str, json_answers: str | None = None) -> dict:
    text = (text or "").strip()
    first, _, rest = text.partition(" ")
    if q["kind"] == "text":
        if not text:
            raise ValueError("답이 비어 있습니다")
        return {"text": text[:MAX_TEXT]}
    if first.lower() in DENY_WORDS:
        return {"decision": "deny", "reason": rest.strip() or "orchestrator가 거절했습니다. 다른 방법을 쓰거나 결과 파일에 보고하세요"}
    if q["kind"] == "permission":
        if first.lower() in ALLOW_WORDS and not rest.strip():
            return {"decision": "allow"}
        raise ValueError("권한 요청에는 'allow' 또는 'deny [사유]'로 답하세요")
    return {"decision": "allow", "answers": _choice_answers(q, text, json_answers)}


def answer(ctx, actor, qid: str, text: str, json_answers: str | None = None) -> dict:
    from .orchestrator import ScopeError, consume_answer
    q = load(ctx.project, qid)
    if q["status"] != "pending":
        raise RuntimeError(f"질문 {qid}은 이미 {q['status']} 상태입니다"
                           + (f" ({q['closed_reason']})" if q.get("closed_reason") else ""))
    tab = ctx.cfg.tab(q["tab"])
    if actor.kind in ("agent", "foreign"):
        raise ScopeError("작업 agent는 질문에 답할 수 없습니다")
    if actor.kind == "orch":
        if tab is None or actor.tab != tab.name:
            raise ScopeError("다른 탭의 질문에는 답할 수 없습니다")
        if tab.unattended == "off":
            raise ScopeError(f"탭 {tab.name}의 무인 모드가 off입니다. 사용자가 답해야 합니다")
        if q["kind"] == "permission" and tab.unattended != "approve":
            raise ScopeError("권한 승인은 unattended: approve일 때만 orchestrator가 답할 수 있습니다")
    try:
        parsed = parse_answer(q, text, json_answers)
    except ValueError as e:
        raise RuntimeError(str(e)) from e
    if actor.kind == "orch":
        consume_answer(ctx, actor, tab)
    state = {}

    def apply(c):  # compare-and-swap: only a still-pending question takes the answer
        state["was"] = c.get("status")
        if c.get("status") != "pending":
            return False
        c.update(status="answered", answer=parsed, answered_by=str(actor), answered_at=time.time())
    q = update(ctx.project, qid, apply)
    if q is None or state.get("was") != "pending":
        raise RuntimeError(f"질문 {qid}은 이미 {state.get('was')} 상태입니다")
    audit(ctx.project, "answered", q, by=str(actor), answer=parsed)
    return q


# -------------------------------------------------------------------- hook
def hook_settings(ctx, tab, m) -> Path | None:
    """Write the PermissionRequest hook settings for claude work agent `m` (None when not needed)."""
    if m.type != "agent" or m.kind != "claude" or tab.unattended == "off":
        return None
    src = str(Path(__file__).resolve().parent.parent)
    cmd = (f"PYTHONPATH={shlex.quote(src)} {shlex.quote(sys.executable)} -m herdr_ctl.cli "
           f"--project {shlex.quote(str(ctx.project.root))} _perm-hook {shlex.quote(m.name)}")
    matcher = "AskUserQuestion" if tab.unattended == "questions" else "*"
    timeout = int(tab.policy["answer_timeout"]) + 60
    data = {"hooks": {HOOK_EVENT: [{"matcher": matcher,
                                    "hooks": [{"type": "command", "command": cmd, "timeout": timeout}]}]}}
    path = ctx.project.secure_dir / "hooks" / f"{m.name}.json"
    write_json(path, data)
    return path


def _output(q: dict) -> str:
    ans = q["answer"] or {}
    if ans.get("decision") == "allow":
        dec = {"behavior": "allow"}
        if q["kind"] == "choice":
            ti = q["tool_input"] or {}
            dec["updatedInput"] = {"questions": ti.get("questions"), "answers": ans["answers"]}
    else:
        dec = {"behavior": "deny", "message": f"[hctrl {q['answered_by']}] {ans.get('reason') or '거절됨'}"}
    return json.dumps({"hookSpecificOutput": {"hookEventName": HOOK_EVENT, "decision": dec}},
                      ensure_ascii=False)


def _valid_choice_input(ti: dict) -> bool:
    items = ti.get("questions") if isinstance(ti, dict) else None
    if not isinstance(items, list) or not items:
        return False
    for i in items:
        if not isinstance(i, dict) or not isinstance(i.get("question"), str):
            return False
        opts = i.get("options")
        if not isinstance(opts, list) or not opts or not all(isinstance(o, dict) and o.get("label") for o in opts):
            return False
    return True


def run_hook(ctx, agent: str, payload: str, sleep=time.sleep) -> str:
    """PermissionRequest hook body. Returns the JSON to print, or "" to fall through to the
    normal dialog (the user). Never raises: any doubt falls through to the user."""
    from .herdr import HerdrError, agent_status
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(data, dict) or data.get("hook_event_name") != HOOK_EVENT:
        return ""
    m = ctx.cfg.member(agent)
    tab = ctx.cfg.tab(m.tab) if m else None
    if m is None or tab is None or m.type != "agent" or m.kind != "claude" or tab.unattended == "off":
        return ""
    pane = os.environ.get("HERDR_PANE_ID")
    rec = ctx.state.member(agent) or {}
    if pane and rec.get("pane_id") and pane != rec["pane_id"]:
        return ""  # someone ran `hctrl _perm-hook <other agent>` by hand: not that agent's dialog
    tool = data.get("tool_name")
    tinput = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    if tool == "AskUserQuestion":
        if not _valid_choice_input(tinput):
            return ""
        kind = "choice"
    else:
        if tab.unattended != "approve" or not isinstance(tool, str):
            return ""
        kind = "permission"
        why = policy_escalation(ctx, tab, tool, tinput, data.get("cwd"))
        if why:
            audit(ctx.project, "escalated", {"agent": agent, "tab": tab.name, "tool_name": tool}, reason=why)
            ctx.h.notify(f"hctrl: {agent} 승인 요청 → 사용자", f"{why}. 화면에서 직접 처리하세요")
            return ""
    holder = lock_holder(ctx.project, agent) or {}
    # one write only: once the record exists it can be answered at any moment
    text = describe({"kind": kind, "tool_name": tool, "tool_input": tinput})
    q = create(ctx.project, kind, agent, tab.name, tool_name=tool, tool_input=tinput, question=text,
               run=holder.get("owner"), hook_pid=os.getpid(), hook_start=proc_start(os.getpid()))
    audit(ctx.project, "asked", q)
    ctx.h.notify(f"hctrl: 질문 {q['id']} ({agent})",
                 f"{q['question'][:200]}\n답: hctrl answer {q['id']} … (orchestrator 또는 사용자)")
    deadline = q["created"] + tab.policy["answer_timeout"]
    grace_end = q["created"] + hook_grace_s()
    seen_blocked = False
    out: dict = {}

    def deliver(c):
        if c.get("status") != "answered":
            return False
        out["json"] = _output(c)
        c["status"] = "resolved"

    def close(reason):
        def fn(c):
            if c.get("status") != "pending":
                return False
            c.update(status="expired", closed_reason=reason)
        return fn

    while True:
        cur = update(ctx.project, q["id"], deliver) or {}
        if "json" in out:
            audit(ctx.project, "delivered", cur)
            return out["json"]
        if cur.get("status") != "pending":
            return ""
        if time.time() >= deadline:
            break
        if time.time() >= grace_end:
            try:
                st = agent_status(ctx.h, agent)
            except HerdrError:
                st = "blocked"
            seen_blocked = seen_blocked or st == "blocked"
            if seen_blocked and st is not None and st != "blocked":  # a human answered on screen
                cur = update(ctx.project, q["id"], close("화면에서 직접 처리됨")) or {}
                if cur.get("status") == "expired":
                    audit(ctx.project, "handled_on_screen", cur)
                    return ""
                continue  # it was answered meanwhile: deliver it
        sleep(poll_s())
    cur = update(ctx.project, q["id"], close("답변 시간 초과 → 사용자")) or {}
    if cur.get("status") == "answered":  # answered right at the deadline: still deliver
        cur = update(ctx.project, q["id"], deliver) or {}
        if "json" in out:
            audit(ctx.project, "delivered", cur)
            return out["json"]
    if cur.get("status") == "expired":
        audit(ctx.project, "timeout", cur)
        ctx.h.notify(f"hctrl: 질문 {q['id']} 시간 초과", f"{agent}: 화면에서 직접 처리하세요")
    return ""
