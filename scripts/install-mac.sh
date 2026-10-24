#!/bin/bash
set -euo pipefail

# macOS per-user launchd installer for the Codex overload relay.
# @author lyt
# @date 2026-10-25

PORT=8787
CLASH_PORT=""
CLASH_HOST="127.0.0.1"
PROXY=""
PYTHON_PATH=""
STATE_DIR="$HOME/Library/Application Support/codex-overload-fix"
NO_AUTOSTART=0
NO_START=0
LABEL="com.codex-overload-fix.relay"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$LAUNCH_AGENTS_DIR/$LABEL.plist"
LOG_PATH="$STATE_DIR/relay.log"
MANIFEST_PATH="$STATE_DIR/install.json"

usage() {
    cat <<'EOF'
用法：./scripts/install-mac.sh [选项]

选项：
  --port PORT             本地监听端口，默认 8787
  --clash-port PORT       Clash HTTP/混合端口，例如 7890
  --clash-host HOST       Clash 地址，默认 127.0.0.1
  --proxy URL             直接指定 HTTP 代理 URL
  --python-path PATH      指定 Python 3.9+ 可执行文件
  --state-dir PATH        状态和日志目录
  --no-autostart          只生成 plist，不加载当前 launchd 会话
  --no-start              写入 plist，但不加载当前会话；下次登录时启动
  -h, --help              显示帮助
EOF
}

die() {
    echo "错误：$*" >&2
    exit 1
}

is_port() {
    [[ "$1" =~ ^[0-9]+$ ]] && ((10#$1 >= 1 && 10#$1 <= 65535))
}

while (($# > 0)); do
    case "$1" in
        --port)
            (($# >= 2)) || die "--port 缺少参数"
            PORT="$2"
            shift 2
            ;;
        --clash-port)
            (($# >= 2)) || die "--clash-port 缺少参数"
            CLASH_PORT="$2"
            shift 2
            ;;
        --clash-host)
            (($# >= 2)) || die "--clash-host 缺少参数"
            CLASH_HOST="$2"
            shift 2
            ;;
        --proxy)
            (($# >= 2)) || die "--proxy 缺少参数"
            PROXY="$2"
            shift 2
            ;;
        --python-path)
            (($# >= 2)) || die "--python-path 缺少参数"
            PYTHON_PATH="$2"
            shift 2
            ;;
        --state-dir)
            (($# >= 2)) || die "--state-dir 缺少参数"
            STATE_DIR="$2"
            LOG_PATH="$STATE_DIR/relay.log"
            MANIFEST_PATH="$STATE_DIR/install.json"
            shift 2
            ;;
        --no-autostart)
            NO_AUTOSTART=1
            shift
            ;;
        --no-start)
            NO_START=1
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

is_port "$PORT" || die "本地端口必须是 1-65535 的整数：$PORT"
if [[ -n "$CLASH_PORT" ]]; then
    is_port "$CLASH_PORT" || die "Clash 端口必须是 1-65535 的整数：$CLASH_PORT"
    [[ "$CLASH_PORT" != "$PORT" ]] || die "Clash 端口不能与本地中继端口相同"
fi

[[ "$CLASH_HOST" == "127.0.0.1" || "$CLASH_HOST" == "localhost" ]] || die "Clash 主机只能是 127.0.0.1 或 localhost"

if [[ -z "$PYTHON_PATH" ]]; then
    PYTHON_PATH="$(command -v python3 || true)"
fi
[[ -n "$PYTHON_PATH" && -x "$PYTHON_PATH" ]] || die "找不到可执行的 python3，请安装 Python 3.9+ 或用 --python-path 指定"
PYTHON_PATH="$(cd "$(dirname "$PYTHON_PATH")" && pwd)/$(basename "$PYTHON_PATH")"

PYTHON_VERSION="$("$PYTHON_PATH" -c 'import sys; print("%d.%d" % sys.version_info[:2])')" || die "无法运行 Python：$PYTHON_PATH"
PYTHON_MAJOR="${PYTHON_VERSION%%.*}"
PYTHON_MINOR="${PYTHON_VERSION##*.}"
((PYTHON_MAJOR > 3 || (PYTHON_MAJOR == 3 && PYTHON_MINOR >= 9))) || die "Python 版本必须为 3.9+，当前为 $PYTHON_VERSION"

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RELAY_PATH="$REPO_DIR/relay/relay.py"
[[ -f "$RELAY_PATH" ]] || die "找不到 relay/relay.py"

if [[ -n "$CLASH_PORT" && -z "$PROXY" ]]; then
    PROXY="http://$CLASH_HOST:$CLASH_PORT"
fi

if [[ -n "$PROXY" ]]; then
    "$PYTHON_PATH" - "$PROXY" "$PORT" <<'PY'
import sys
from urllib.parse import urlsplit

proxy, relay_port = sys.argv[1], int(sys.argv[2])
parsed = urlsplit(proxy)
if parsed.scheme not in {"http"}:
    raise SystemExit("代理必须是 http:// URL；7891 等 SOCKS 端口不支持")
if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
    raise SystemExit("本地安装只允许使用回环代理地址")
if parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
    raise SystemExit("代理 URL 只能包含协议、主机和端口，不支持凭据、路径或查询参数")
if parsed.port is not None and not 1 <= parsed.port <= 65535:
    raise SystemExit("代理端口必须是 1-65535")
if parsed.hostname in {"127.0.0.1", "localhost", "::1"} and parsed.port == relay_port:
    raise SystemExit("代理不能指向本地中继自身")
PY
fi

mkdir -p "$STATE_DIR" "$LAUNCH_AGENTS_DIR"

if [[ -n "$CLASH_PORT" ]]; then
    if ! "$PYTHON_PATH" - "$CLASH_HOST" "$CLASH_PORT" <<'PY'
import socket
import sys

host, port = sys.argv[1], int(sys.argv[2])
try:
    with socket.create_connection((host, port), timeout=1):
        pass
except OSError as exc:
    print(f"Clash 端口不可连接：{host}:{port} ({exc})", file=sys.stderr)
    raise SystemExit(1)
PY
    then
        die "请确认 Clash 已启动，且 HTTP/混合端口配置正确"
    fi
fi

if launchctl bootout "gui/$(id -u)" "$PLIST_PATH" >/dev/null 2>&1; then
    :
else
    launchctl unload "$PLIST_PATH" >/dev/null 2>&1 || true
fi

if curl -fsS --max-time 1 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    die "端口 $PORT 已被其他进程占用，请换端口或先停止旧中继"
fi

"$PYTHON_PATH" - "$PLIST_PATH" "$LABEL" "$PYTHON_PATH" "$RELAY_PATH" "$PORT" "$LOG_PATH" "$PROXY" "$STATE_DIR" <<'PY'
import plistlib
import sys

plist_path, label, python_path, relay_path, port, log_path, proxy, state_dir = sys.argv[1:]
args = [python_path, relay_path, "--port", port, "--log", log_path]
if proxy:
    args.extend(["--proxy", proxy])
payload = {
    "Label": label,
    "ProgramArguments": args,
    "WorkingDirectory": state_dir,
    "RunAtLoad": True,
    "KeepAlive": True,
    "ThrottleInterval": 3,
    "ProcessType": "Background",
    "StandardOutPath": log_path,
    "StandardErrorPath": log_path,
}
with open(plist_path, "wb") as handle:
    plistlib.dump(payload, handle, sort_keys=False)
PY

"$PYTHON_PATH" - "$MANIFEST_PATH" "$LABEL" "$PLIST_PATH" "$PYTHON_PATH" "$RELAY_PATH" "$PORT" "$PROXY" <<'PY'
import json
import sys
from pathlib import Path

manifest_path, label, plist_path, python_path, relay_path, port, proxy = sys.argv[1:]
payload = {
    "label": label,
    "plist_path": plist_path,
    "python_path": python_path,
    "relay_path": relay_path,
    "port": int(port),
    "proxy": proxy,
}
Path(manifest_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

if ((NO_AUTOSTART == 0 && NO_START == 0)); then
    launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
    launchctl kickstart -k "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
    HEALTHY=0
    for _ in {1..20}; do
        if curl -fsS --max-time 1 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
            HEALTHY=1
            break
        fi
        sleep 0.25
    done
    ((HEALTHY == 1)) || die "中继未能启动，请查看日志：$LOG_PATH"
elif ((NO_AUTOSTART == 0 && NO_START == 1)); then
    echo "已写入 LaunchAgents；当前会话未加载，下一次登录时自动启动。"
fi

cat <<EOF
macOS 安装完成。
本地地址：http://127.0.0.1:$PORT
配置文件：$PLIST_PATH
日志文件：$LOG_PATH

将以下内容写入 ~/.codex/config.toml 后重启 Codex：

model_provider = "openai-retry"

[model_providers.openai-retry]
name = "OpenAI (retry)"
base_url = "http://127.0.0.1:$PORT"
wire_api = "responses"
requires_openai_auth = true
stream_max_retries = 0
request_max_retries = 0
EOF


