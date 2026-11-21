#!/bin/sh
# hctrl installer: downloads the single-file hctrl.pyz from GitHub Releases.
#
#   curl -fsSL https://raw.githubusercontent.com/fantaXII/herdr-ctrl/main/install.sh | sh
#
# Options (environment variables):
#   HCTRL_VERSION=v2.1.0   install that release tag instead of the latest one
#   HCTRL_BIN_DIR=DIR      install directory (default: ~/.local/bin)
#   HCTRL_URL=URL          download from this exact URL (testing)
#
# Update: run the same command again.  Uninstall: rm ~/.local/bin/hctrl ~/.local/bin/herdr_ctl
set -eu

REPO="fantaXII/herdr-ctrl"
BIN_DIR="${HCTRL_BIN_DIR:-$HOME/.local/bin}"

say() { printf '%s\n' "$*"; }
die() { printf 'hctrl 설치 실패: %s\n' "$*" >&2; exit 1; }

# 1. python3 >= 3.10
command -v python3 >/dev/null 2>&1 || die "python3가 없습니다. Python 3.10 이상을 먼저 설치하세요 (예: sudo apt install python3)."
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
    || die "Python 3.10 이상이 필요합니다 (현재: $(python3 -V 2>&1))."

# 2. download
if [ -n "${HCTRL_URL:-}" ]; then
    URL="$HCTRL_URL"
elif [ -n "${HCTRL_VERSION:-}" ]; then
    URL="https://github.com/$REPO/releases/download/$HCTRL_VERSION/hctrl.pyz"
else
    URL="https://github.com/$REPO/releases/latest/download/hctrl.pyz"
fi

mkdir -p "$BIN_DIR"
# this installer always writes a regular file; a symlink means another install (pipx) owns the name
if [ -L "$BIN_DIR/hctrl" ]; then
    die "$BIN_DIR/hctrl 이(가) 다른 설치(예: pipx)의 링크입니다 -> $(readlink "$BIN_DIR/hctrl")
  먼저 지우고 다시 실행하세요:  pipx uninstall herdr-ctl"
fi
TMP="$(mktemp "$BIN_DIR/.hctrl.XXXXXX")"
trap 'rm -f "$TMP"' EXIT INT TERM

say "다운로드: $URL"
if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$URL" -o "$TMP" || die "다운로드하지 못했습니다."
elif command -v wget >/dev/null 2>&1; then
    wget -qO "$TMP" "$URL" || die "다운로드하지 못했습니다."
else
    die "curl 또는 wget이 필요합니다."
fi

# 3. check it really runs before replacing an existing install
VERSION="$(python3 "$TMP" --version 2>&1)" || die "받은 파일이 실행되지 않습니다: $VERSION"

chmod 755 "$TMP"
mv -f "$TMP" "$BIN_DIR/hctrl"
trap - EXIT INT TERM
ln -sf hctrl "$BIN_DIR/herdr_ctl"
say "설치 완료: $BIN_DIR/hctrl ($VERSION)"

# 4. hints
case ":$PATH:" in
    *":$BIN_DIR:"*)
        FOUND="$(command -v hctrl 2>/dev/null || true)"
        if [ -n "$FOUND" ] && [ "$FOUND" != "$BIN_DIR/hctrl" ]; then
            say ""
            say "주의: PATH에서 다른 hctrl이 먼저 실행됩니다: $FOUND"
        fi
        ;;
    *)
        say ""
        say "$BIN_DIR 이(가) PATH에 없습니다. 셸 설정 파일(~/.bashrc 또는 ~/.zshrc)에 추가하세요:"
        say "  export PATH=\"$BIN_DIR:\$PATH\""
        ;;
esac

if ! command -v herdr >/dev/null 2>&1; then
    say ""
    say "herdr가 아직 없습니다. hctrl은 herdr 위에서 동작하므로 먼저 설치하세요:"
    say "  curl -fsSL https://herdr.dev/install.sh | sh"
fi

say ""
say "시작하기: 프로젝트 폴더에서  hctrl init  →  hctrl"
