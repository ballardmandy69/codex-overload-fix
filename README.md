# Codex 满载修复

本项目提供一个本地、纯 Python 标准库的重试中继，解决 Codex 偶发出现：

```
Selected model is at capacity. Please try a different model.
```

中继只监听本机 `127.0.0.1:8787`，把请求转发到官方 Codex 端点；它不会保存凭证、修改请求体或把流量发送到第三方服务器。Windows 使用任务计划程序，macOS 使用当前用户的 `launchd`，两种系统都能在中继退出后自动拉起。

> 不想使用 git：下载 [最新压缩包](https://github.com/NewBeeM4n/codex-overload-fix/releases/latest)。Windows 解压后运行 `scripts\install.ps1`，macOS 解压后运行 `chmod +x scripts/install-mac.sh scripts/uninstall-mac.sh && ./scripts/install-mac.sh`。

## 先看结论

这个报错通常不是额度耗尽，也不是普通网络错误。真实失败可能是 HTTP 200 响应中的 SSE 事件：

```json
{"type":"error","sequence_number":3,
 "error":{"type":"service_unavailable_error",
          "code":"server_is_overloaded",
          "message":"Our servers are currently overloaded. Please try again later."}}
```

状态码为 200 时，按 HTTP 状态码工作的代理和重试层看不到失败；Codex 读完整条流后才知道失败，于是默认重试次数用完后把错误交给用户。这个项目在 SSE 流尚未提交给用户前识别 overload，并重新发送同一个请求。

本项目解决的是“被服务端拒绝后没有得到足够重试”的问题，不能解决额度真正用尽、登录过期、网络完全不通或模型能力变化。

## 为什么大请求更容易遇到它

本地抓到的 71 个 `/responses` 请求显示，重试次数与请求体积存在明显断崖：

| 请求体 | 数量 | 平均尝试次数 | 最多尝试 |
|---|---:|---:|---:|
| < 100 KB | 20 | 1.95 | 5 |
| 100–200 KB | 24 | 1.96 | 9 |
| 200–500 KB | 23 | 1.74 | 4 |
| **> 500 KB** | **4** | **6.75** | **9** |

大于 500 KB 的样本都需要多次尝试；同一秒并发发出多个约 770 KB 请求时更明显。这是单机样本，具体阈值会随服务端准入策略变化，不能当成官方固定限制。

桌面版新会话约 89 KB，主要由工具定义、基础提示、技能清单和记忆块组成；长会话日志中的 `pre_compression_bytes` 曾达到约 724 KB。实际使用时，**开新会话、压缩附件图片、禁用不用的技能和插件**，通常比换模型更有效。

删除不用的插件和技能时，优先使用 Codex 的卸载命令：

```bash
codex plugin remove <名称>@<市场名>
```

如果市场返回 `Installed-by-default plugins cannot be uninstalled`，就在 `config.toml` 中禁用它：

```toml
[plugins."<名称>@<市场名>"]
enabled = false
```

直接删除远程插件缓存没有长期效果，Codex 启动时可能重新下载；应使用卸载或配置禁用。

## 中继实际做什么

请求路径仍然是官方端点：

```
Codex
  ↓ HTTP（本机）
127.0.0.1:8787 本地中继
  ↓ HTTP CONNECT（可选，Clash HTTP/混合端口）
chatgpt.com/backend-api/codex
```

中继对 `POST .../responses` 执行以下流程：

1. 读取并限制请求体，默认上限 64 MiB；请求体、鉴权头和压缩内容原样保留。
2. 向官方端点发起一次请求。
3. 如果返回 429/500/502/503/504、上游连接短暂中断，或 HTTP 200 的 SSE 中先出现 `response.failed` / `error` 且错误命中 overload 判断，则关闭本次上游连接并重试。
4. 空的输出项、内容块和心跳继续缓冲；看到正文、推理文本、工具参数、完整工具调用、未知事件或非 overload 错误时提交，后续不再重放，避免重复生成。
5. 退避从约 0.2 秒开始，乘 1.6 并加入随机抖动，单次最多等待 0.5 秒；默认最多 10 次、提交前总预算 20 秒。
6. 如果 Codex 已取消请求或关闭本地连接，中继立即停止等待和重试。

上游明确要求的 `Retry-After` 若超过单次退避上限，中继直接返回该失败，避免长时间空等或提前重试。

缺少 `Content-Type` 的未压缩响应会先检查最多 8 KiB 的前缀；识别为 SSE 后补齐响应类型，
按事件处理并在提交边界及时转发，不再等整条响应结束。单次最多等待 20 秒出现实际输出，
普通请求的所有提交前尝试共享 20 秒总预算。提交后连续 30 秒没有输出进展会断开且不重放，
心跳、空占位事件不重置这个期限；正常输出持续到达时会刷新期限，不限制整条响应的总时长。
可用 `--commit-deadline`、`--total-budget`、`--stream-idle-timeout` 调整这些期限。
较短期限也可能结束长时间没有输出的正常推理。

上下文压缩使用独立的 120 秒提交前预算与 120 秒输出停滞期限，可通过 `--compaction-timeout` 调整。
识别依据为 Codex 的 `x-codex-turn-metadata` 中明确的 `request_kind=compaction`，
请求头缺失时兼容未压缩请求体中的 `client_metadata["x-codex-turn-metadata"]`；
此备用检查仅处理不超过 8 MiB 的请求体。独立的 `/responses/compact` 接口也使用压缩期限。
没有明确用途标记的请求沿用普通期限，不根据请求体积或提示词推测。
请求与凭证原样转发，日志仅记录用途、识别来源和期限，不记录完整元数据或请求内容。
用途字段参考 [Codex 0.167.0 源码](https://github.com/openai/codex/blob/rust-v0.167.0/codex-rs/core/src/responses_metadata.rs)。

HTTP 编码压缩的响应原样流式转发，空闲期限按收到的字节刷新；日志用 `precommit_deadline`、
`stream_idle_timeout`、`client_cancelled`、`socket_timeout` 和 `upstream_disconnect`
区分实际输出前超时、输出后停滞、客户端取消、套接字超时与上游断连，并记录请求编号。

每条已提交的流结束时会记录 `stream ended`，包含关闭原因、结束事件、转发字节数、
总耗时和转发耗时。只有观察到 `response.completed` 才标记完成；`terminal=none`
表示未观察到已知结束事件，单凭 EOF 或客户端关闭不能判断回答已完整生成。
诊断只保存白名单事件类型，不保存事件原文、回答内容或凭证。
未完成事件超过 256 KiB 时停止诊断解析，标记 `inspection=stopped` 并改按字节活动刷新空闲期限，
原始内容继续透传；JSON 和压缩响应标记 `inspection=opaque`。

这部分借鉴了上游的[流结束诊断](https://github.com/NewBeeM4n/codex-overload-fix/commit/b5ab2f25627d023b00003ebe466fbbdca085b49d)，
并保留本地的 20 秒提交前预算、30 秒输出停滞期限和提交前有限连接重试。

## 配置 Codex

先备份 `config.toml`。根据系统编辑以下文件：

- Windows：`%USERPROFILE%\.codex\config.toml`
- macOS：`~/.codex/config.toml`

加入：

```toml
model_provider = "openai-retry"

[model_providers.openai-retry]
name = "OpenAI (retry)"
base_url = "http://127.0.0.1:8787"
wire_api = "responses"
requires_openai_auth = true
stream_max_retries = 0
request_max_retries = 0
```

`openai` 是 Codex 内置保留 provider，不能覆盖，所以使用新的 `openai-retry`。 `requires_openai_auth = true` 会复用 Codex 已有的 ChatGPT OAuth，不需要重新登录，也不会改变套餐或账号。

修改后必须完全退出并重新启动 Codex。回滚时删除 `model_provider` 和 `[model_providers.openai-retry]` 配置块即可。

## Windows 安装

在 PowerShell 中执行：

```powershell
git clone https://github.com/NewBeeM4n/codex-overload-fix.git
cd codex-overload-fix
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -ClashPort 7890
```

安装脚本会找到 Python 3.9+，注册当前用户的任务计划程序任务，并在用户登录时启动独立看门狗。
任务通过 `wscript.exe` 调用状态目录中的 `start-relay.vbs`，从创建进程时隐藏 PowerShell 窗口，
等待看门狗退出并传递退出码，使任务计划程序可以在异常退出后重新拉起。
Python 自动检测使用 ASCII JSON 传递解释器路径，兼容中文用户名和带空格的路径。
这些安装兼容性修复参考了上游实现，并沿用本项目的任务计划程序和状态目录。

不使用 Clash 时省略 `-ClashPort`。卸载：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\uninstall.ps1
```

## macOS 安装

先确认已安装 Python 3.9+，然后执行：

```bash
git clone https://github.com/NewBeeM4n/codex-overload-fix.git
cd codex-overload-fix
chmod +x scripts/install-mac.sh scripts/uninstall-mac.sh
./scripts/install-mac.sh --clash-port 7890
```

脚本会：

- 检查 Python 版本、本地端口和 Clash 端口是否可连接；
- 生成当前用户的 `~/Library/LaunchAgents/com.codex-overload-fix.relay.plist`；
- 使用 `RunAtLoad + KeepAlive` 在登录后启动，并在中继退出后自动重启；
- 将日志写入 `~/Library/Application Support/codex-overload-fix/relay.log`；
- 输出应写入 `~/.codex/config.toml` 的配置片段。

macOS 使用的是用户级 `LaunchAgents`，不需要管理员权限；它在用户登录会话中运行，注销用户后自然停止。只生成 plist、不注册自动启动：

```bash
./scripts/install-mac.sh --clash-port 7890 --no-autostart
```

写入 plist，但不加载当前会话；下次登录时自动启动：

```bash
./scripts/install-mac.sh --clash-port 7890 --no-start
```

卸载服务但保留日志和安装清单：

```bash
./scripts/uninstall-mac.sh
```

连同状态目录和日志一起删除：

```bash
./scripts/uninstall-mac.sh --purge
```

如果使用自定义状态目录，卸载时带上相同的 `--state-dir`。

## Clash 端口

Clash 常见端口含义如下：

| 端口 | 类型 | 能否直接传给本项目 |
|---:|---|---|
| 7890 | HTTP 或 Mixed | 可以 |
| 7891 | SOCKS5 | 不可以 |
| 自定义端口 | 取决于 Clash 配置 | 必须是 HTTP 或 Mixed |

HTTP/混合端口示例：

```bash
python3 relay/relay.py --port 8787 --clash-port 7890 --log relay.log
```

Windows PowerShell：

```powershell
python relay\relay.py --port 8787 --clash-port 7890 --log relay.log
```

也可以显式指定：

```bash
python3 relay/relay.py --port 8787 --proxy http://127.0.0.1:7890 --log relay.log
```

本项目只接受回环地址上的 HTTP 代理，不支持 SOCKS、HTTPS 代理、代理认证或带路径的代理 URL。Clash 未运行、端口填成 SOCKS 端口、或端口被其他程序占用时，安装和启动都会失败并给出提示。

## 手动启动与健康检查

macOS / Linux：

```bash
python3 relay/relay.py --port 8787 --clash-port 7890 --log relay.log
curl http://127.0.0.1:8787/health
```

Windows：

```powershell
python relay\relay.py --port 8787 --clash-port 7890 --log relay.log
Invoke-WebRequest http://127.0.0.1:8787/health
```

健康检查返回 HTTP 200 和 `service=codex-overload-fix`，只能证明本地中继进程已监听；真正验证转发，需要在 Codex 发起一次请求并查看日志中的 `attempt ... ok, streaming`。

诊断命令：

```bash
python3 tools/capture_server.py outdir 18090
python3 tools/analyze_log.py relay.log
```

运行 `capture_server.py` 后，把临时 provider 指向 `http://127.0.0.1:18090`，执行一轮真实请求，再检查 `outdir` 中保存的请求体；它用于测量自己的请求大小，不参与正常重试链路。

Windows 将 `python3` 替换为 `python`，路径分隔符可以使用 `tools\capture_server.py`。

## 相关工具

| 文件 | 用途 |
|---|---|
| `relay/relay.py` | 本地 HTTP/SSE 重试中继 |
| `scripts/install.ps1` | Windows 安装和任务计划程序注册 |
| `scripts/uninstall.ps1` | Windows 卸载 |
| `scripts/install-mac.sh` | macOS launchd 安装 |
| `scripts/uninstall-mac.sh` | macOS launchd 卸载 |
| `tools/capture_server.py` | 捕获真实请求体，测量大小 |
| `tools/analyze_log.py` | 统计请求、重试和体积分布 |
| `tests/test_relay.py` | 中继单元和集成测试 |

## 安全、边界与回滚

- 中继只绑定 `127.0.0.1`，不接受局域网连接。
- 请求和鉴权头只在本机中继与官方端点之间转发，不保存 token，不共享凭证，不提供第三方反代。
- 未提交任何输出前才允许重试；流一旦提交，连接中断不会重放。提交前的上游连接中断会立即进入重试，并给客户端短暂宽限，连续失败最多 3 次；中间任何一次 overload 重试会清零连接失败计数，仍失败才返回 502。
- 普通 403、登录失效、额度不足、上游内容错误会原样透传。带 Cloudflare 特征的 HTML 403 会在未提交响应前最多重试 3 次，仍失败则返回原始 403。
- 本地日志可能包含请求大小、状态和错误摘要；不要把日志公开，并不要在自定义日志代码中输出 `Authorization`。
- 删除 Codex 配置中的自定义 provider 即可回到官方直连。

## 已验证和未验证

一次真实任务中统计到 71 个请求、82 次内部重试、0 次放弃、0 次错误弹到用户面前；其中一条约 799 KB 请求第 9 次才成功。这些数字来自单机、单账号和特定时间窗，说明重试机制有效，不代表服务端存在固定公开阈值。

以下操作在同一环境中没有稳定改善容量错误：更换出口节点、在坏窗口切换模型、修改系统时区、切换 `service_tier`、反复重启 App。它们可能改变时机，但没有改变准入问题本身。

## 上游机制的合理改进

服务端在容量不足时快速拒绝是必要的，避免队列和客户端重试造成雪崩；更合理的信号应是：

- HTTP 503，并带 `Retry-After`；
- 或让客户端默认识别 SSE 中的 `server_is_overloaded` 并退避重试；
- 对大请求和并发请求提供更明确的准入反馈；
- 不要用“换个模型”描述看起来与请求体积有关的拒绝。

## 许可证

MIT，见 [LICENSE](LICENSE)。
