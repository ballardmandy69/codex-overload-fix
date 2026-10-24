#!/bin/bash
set -euo pipefail

# macOS per-user launchd uninstaller for the Codex overload relay.
# @author lyt
# @date 2026-10-25

STATE_DIR="$HOME/Library/Application Support/codex-overload-fix"
PURGE=0
LABEL="com.codex-overload-fix.relay"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$LAUNCH_AGENTS_DIR/$LABEL.plist"
MANIFEST_PATH="$STATE_DIR/install.json"

usage() {
    cat <<'EOF'
用法：./scripts/uninstall-mac.sh [选项]

选项：
  --state-dir PATH        与安装时相同的状态目录
  --purge                 同时删除状态目录和 relay.log
  -h, --help              显示帮助
EOF
}

die() {
    echo "错误：$*" >&2
    exit 1
}

while (($# > 0)); do
    case "$1" in
        --state-dir)
            (($# >= 2)) || die "--state-dir 缺少参数"
            STATE_DIR="$2"
            MANIFEST_PATH="$STATE_DIR/install.json"
            shift 2
            ;;
        --purge)
            PURGE=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "未知参数：$1"
            ;;
    esac
done

if [[ -f "$MANIFEST_PATH" ]]; then
    PYTHON_PATH="$(command -v python3 || true)"
    [[ -n "$PYTHON_PATH" ]] || die "删除安装清单需要 python3"
    INSTALL_INFO="$("$PYTHON_PATH" - "$MANIFEST_PATH" "$LABEL" "$PLIST_PATH" <<'PY'
import json
import sys
from pathlib import Path

manifest_path, expected_label, expected_plist = sys.argv[1:]
data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
if data.get("label") != expected_label or data.get("plist_path") != expected_plist:
    raise SystemExit("安装清单与当前用户的 launchd 配置不匹配，已停止删除")
print(data["label"])
print(data["plist_path"])
PY
    )"
    LABEL="$(printf '%s\n' "$INSTALL_INFO" | sed -n '1p')"
    PLIST_PATH="$(printf '%s\n' "$INSTALL_INFO" | sed -n '2p')"
    [[ -n "$LABEL" && -n "$PLIST_PATH" ]] || die "安装清单格式不正确"
fi

launchctl bootout "gui/$(id -u)" "$PLIST_PATH" >/dev/null 2>&1 || true
launchctl unload "$PLIST_PATH" >/dev/null 2>&1 || true
rm -f "$PLIST_PATH"

if ((PURGE == 1)); then
    DEFAULT_STATE_DIR="$HOME/Library/Application Support/codex-overload-fix"
    [[ "$STATE_DIR" == "$DEFAULT_STATE_DIR" || "$STATE_DIR" == "$DEFAULT_STATE_DIR/"* ]] || die "--purge 只允许删除默认状态目录及其子目录"
    rm -rf "$STATE_DIR"
fi

echo "macOS 中继已停止并卸载。"
if ((PURGE == 0)); then
    echo "如需删除日志和安装清单，请再次执行：./scripts/uninstall-mac.sh --purge"
fi


