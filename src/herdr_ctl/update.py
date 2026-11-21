"""New-version notice and `hctrl update` (single-file hctrl.pyz installs).

- The check asks GitHub for the latest release at most once a day (cache in state_home()); it runs
  in a detached background process so no command waits on the network, and failures stay silent.
- Only user-facing places read the cache (launcher terminal, settings UI, the _hc tab); agent panes
  never check. HCTRL_NO_UPDATE_CHECK=1 turns the check off.
- `hctrl update` replaces the running .pyz only after the release's .sha256 matches and the new
  file reports the expected version.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Callable

from . import __version__
from .project import state_home

REPO = "fantaXII/herdr-ctrl"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
DOWNLOAD = f"https://github.com/{REPO}/releases/download/{{tag}}/{{name}}"
CHECK_EVERY_S = 24 * 3600
RETRY_AFTER_FAIL_S = 3600
TIMEOUT_S = 5.0
ASSET = "hctrl.pyz"

Fetch = Callable[[str, float], bytes]


def _fetch(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": f"hctrl/{__version__}",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (fixed https URLs)
        return r.read()


def parse_version(v: str | None) -> tuple[int, ...] | None:
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", (v or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def is_newer(latest: str | None, current: str = __version__) -> bool:
    a, b = parse_version(latest), parse_version(current)
    return bool(a and b and a > b)


def disabled() -> bool:
    return os.environ.get("HCTRL_NO_UPDATE_CHECK", "") not in ("", "0")


# ------------------------------------------------------------------ cache
def cache_path() -> Path:
    return state_home() / "update-check.json"


def read_cache() -> dict:
    try:
        d = json.loads(cache_path().read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def write_cache(d: dict) -> None:
    p = cache_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(d))
        os.replace(tmp, p)
    except OSError:
        pass


def latest_release(fetch: Fetch = _fetch, timeout: float = TIMEOUT_S) -> str:
    tag = json.loads(fetch(API_LATEST, timeout)).get("tag_name")
    if not parse_version(tag):
        raise ValueError(f"알 수 없는 릴리스 태그: {tag!r}")
    return str(tag)


def check_now(fetch: Fetch = _fetch) -> str | None:
    """Ask GitHub and record the answer (or the failure, so we do not retry every command)."""
    try:
        tag = latest_release(fetch)
    except Exception:  # offline, rate limit, proxy...: silent, retry later
        write_cache({**read_cache(), "checked_at": time.time(), "failed": True})
        return None
    write_cache({"checked_at": time.time(), "latest": tag, "failed": False})
    return tag


def due(now: float | None = None) -> bool:
    c = read_cache()
    age = (now or time.time()) - float(c.get("checked_at") or 0)
    return age >= (RETRY_AFTER_FAIL_S if c.get("failed") else CHECK_EVERY_S)


def refresh_in_background(spawn: Callable[..., object] = subprocess.Popen) -> None:
    """Start `hctrl _update-check` detached when the cache is stale (never blocks the caller)."""
    if disabled() or not due():
        return
    from .selfcmd import self_cmd
    write_cache({**read_cache(), "checked_at": time.time()})  # claim this round: no parallel checks
    try:
        spawn(self_cmd() + ["_update-check"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
              stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass


def available() -> str | None:
    """Newer release tag known from the cache, else None (no network)."""
    if disabled():
        return None
    tag = read_cache().get("latest")
    return tag if is_newer(tag) else None


def notice() -> str | None:
    tag = available()
    if not tag:
        return None
    return f"새 버전 {tag}이 나왔습니다 (현재 v{__version__}). 업데이트: hctrl update"


# ------------------------------------------------------------------ update
class UpdateError(RuntimeError):
    pass


def install_target() -> Path | None:
    """The .pyz file to replace, or None for a package install (pipx / source checkout)."""
    from .selfcmd import PKG_PARENT, is_zipapp
    return PKG_PARENT if is_zipapp() else None


def run_update(check_only: bool = False, fetch: Fetch = _fetch, target: Path | None = None,
               python: str = sys.executable, out: Callable[[str], None] = print) -> int:
    tag = latest_release(fetch)
    write_cache({"checked_at": time.time(), "latest": tag, "failed": False})
    if not is_newer(tag):
        out(f"최신 버전입니다 (v{__version__}).")
        return 0
    out(f"새 버전 {tag} (현재 v{__version__})")
    if check_only:
        return 0
    target = target or install_target()
    if target is None:
        raise UpdateError("hctrl.pyz 설치가 아닙니다 (pipx 또는 소스 실행). "
                          "pipx upgrade herdr-ctl 또는 git pull로 업데이트하세요")
    out("다운로드: " + DOWNLOAD.format(tag=tag, name=ASSET))
    data = fetch(DOWNLOAD.format(tag=tag, name=ASSET), 60.0)
    try:
        sums = fetch(DOWNLOAD.format(tag=tag, name=ASSET + ".sha256"), TIMEOUT_S).decode()
    except Exception as e:
        raise UpdateError(f"{tag} 릴리스에 체크섬 파일이 없어 업데이트하지 않았습니다 ({e}). "
                          "install.sh로 설치하세요") from e
    want = sums.split()[0].lower() if sums.split() else ""
    got = hashlib.sha256(data).hexdigest()
    if want != got:
        raise UpdateError(f"체크섬이 맞지 않습니다 (기대 {want[:16]}…, 받은 파일 {got[:16]}…). 업데이트하지 않았습니다")
    tmp = target.with_name(f".{target.name}.update")
    try:
        tmp.write_bytes(data)
        tmp.chmod(0o755)
        r = subprocess.run([python, str(tmp), "--version"], capture_output=True, text=True, timeout=30)
        if r.stdout.strip() != f"hctrl (herdr_ctl) {tag.lstrip('v')}":
            raise UpdateError(f"받은 파일의 버전이 다릅니다: {(r.stdout or r.stderr).strip()!r}")
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    out(f"✔ 업데이트 완료: v{__version__} → {tag}")
    out("  설정(config.yaml, 프로젝트 목록, 상태)은 그대로입니다. "
        "실행 중인 herdr 세션의 _hc 탭은 세션을 다시 열어야 새 버전으로 바뀝니다.")
    return 0
