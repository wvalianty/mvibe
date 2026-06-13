# mvibe

把本地 **Claude Code** TUI 镜像到远程聊天（微信）。本地终端与直接运行 `claude`
逐字节一致；两个开关（输入路由 `flag` + 输出/入站总闸 `gate`）决定远程接管程度。
**全程只有一个 `claude` 进程**——切换只是 I/O 路由，不重启、不交接会话。

## 工作原理

```
            ┌──────────── mvibe run（持 PTY master）────────────┐
 终端   ────┤ stdin → PTY     （local 路由；一敲键即夺回）       │
        ←───┤ PTY  → stdout   （始终；远程驱动时也能围观）       │──► claude TUI
            │ inject FIFO → PTY（remote 路由时）                │     （单进程）
            └───────────────────────────────────────────────────┘        │ 写入
                                                          .jsonl ◄─────────┘
 微信 ──入站──► inject FIFO + flag=remote                        │
     ◄── 推送(iLink) ── tail transcript .jsonl（assistant text）◄─┘
                       [mvibe bridge]（出站受 remote gate 控制）
```

- **本地保持真 TUI**（富渲染、slash 命令、plan mode）：PTY 透明透传，即
  `script(1)` / `asciinema` 那套。
- **远程输出 ≠ 裸 ANSI**：从会话 transcript（`~/.claude/projects/<cwd>/<id>.jsonl`）
  读，而非镜像 PTY 字节——TUI 的 ANSI 流在聊天框里是乱码。
- **远程输入**：当作键盘注入 PTY，驱动真 TUI。

### 两个开关，别搞混

- **`flag`（`~/.mvibe/active`，local|remote）= 输入路由**：谁的键盘进 claude。
  入站消息置 `remote`；**本地敲任意键立即翻回 `local` 并夺回控制**（human 优先，
  永不锁死）。这是输入端互斥，杜绝两人同敲。
- **`gate`（remote on/off）= 输出镜像 + 入站总闸**：决定是否把 claude 回复推手机、
  是否接受手机消息。`/mvibe-on` 开、`/mvibe-off` 关。
- 二者解耦：所以你本地夺回打字（flag=local）时，回复仍会推手机（gate 仍 on）；
  要彻底断开手机，`/mvibe-off`。

## 安装

```bash
uv tool install --editable /path/to/mvibe    # 把 mvibe 装上全局 PATH
cp /path/to/mvibe/commands/mvibe-*.md ~/.claude/commands/   # 装会话内 slash 开关
mvibe login                                   # 一次性：扫码绑定微信 bot
```

依赖仅 `aiohttp` + `segno`（PyPI），**零 avibe 依赖**。

## 用法（单终端，推荐）

进到想用 claude 的目录，直接 `mvibe`——**裸 `mvibe` 等价于在当前目录 `mvibe up`**：
同一终端前台跑 claude TUI，后台跑 bridge（镜像输出 + 微信入站轮询）。桥日志写
`~/.mvibe/bridge.log`，不污染界面。

```bash
cd /path/to/project
mvibe                 # = mvibe up，cwd 即当前目录
```

等价写法：`mvibe up`（同上）、`mvibe up --cwd /other/dir`（指定目录）。

手机端：先给 bot 发一条消息建立会话，之后你发的内容驱动这个 Claude TUI，
Claude 的回复以聊天消息返回。退出 claude（`/exit`）即整体结束。

### 会话内开关（Claude slash 命令）

把 `commands/mvibe-*.md` 拷到 `~/.claude/commands/`，即可在任意会话里：

| 命令 | 作用 |
|------|------|
| `/mvibe-on`     | 开启远程接管（微信驱动会话） |
| `/mvibe-off`    | 关闭远程接管（忽略微信，保持本地） |
| `/mvibe-status` | 查看 remote 开关 / 路由 flag / transcript |

`mvibe up` 默认 remote=on（`--no-remote` 可改）。

**夺回控制**：远程接管时本地键盘被让出，但**只要你在本地敲任意键，立即夺回
本地控制**（human 在场优先）——所以你永远不会被锁死，可随时打字。想彻底不让
手机再抢，敲 `/mvibe-off`（关闭 gate）。下次手机消息会再次接管，除非 gate 关着。

### 两终端模式（可选）

```bash
mvibe run --cwd .       # 终端1：claude TUI（必须先起）
mvibe bridge --cwd .    # 终端2：镜像 + 入站
```

### 直接命令 / HTTP

```bash
mvibe remote on|off|status      # 远程接管开关
mvibe flag remote|local         # 仅切路由
mvibe send "继续"                # 注入一行
curl -X POST --data '继续' localhost:8765/inbound   # HTTP 驱动（无需微信）
```

## 微信：独立 bot

mvibe 端到端自持微信 bot——**零 avibe 依赖**、无需 service、无需公网 webhook：

- **绑定**（`mvibe login`，`wechat_login.py`）：跑官方 iLink 扫码流程，把
  `bot_token` / `base_url` / `user_id` 写入 `~/.mvibe/config.json`。
- **入站**（`wechat_in.py`）：iLink `getUpdates` 长轮询，无需 webhook/公网。
  每条消息产出 `(text, user_id)`，置 flag 为 `remote` 并把文本注入 PTY。同步游标
  与每用户 `context_token` 持久化在 `~/.mvibe/state/`。
- **出站**（`wechat_out.py`）：tailer 读到的 assistant 文本经 `send_message`
  推手机，用记住的 `context_token` 寻址。**只在 remote gate 开启时推送**（跟 gate
  走，不跟 flag），所以本地按键夺回输入不会吞掉回复。失败写 `~/.mvibe/bridge.log`
  （如 `errcode -14` 会话过期，需手机再发条消息刷新）。

iLink 协议代码 vendored 在 `mvibe/ilink/`（`wechat_api.py` / `wechat_auth.py`，
源自 avibe，仅依赖 `aiohttp` + stdlib）。mvibe 不在运行时引用任何 avibe 安装。

> 一个 bot_token 上不要同时跑两个 `getUpdates` 轮询（会争同步游标）。mvibe 有自己
> 的登录/bot，与任何 avibe 实例互不干扰。

## 安全

远程聊天面拥有**完整 agent 权限**（Claude 可跑 bash/工具）。务必收紧入口：

- **入站鉴权（默认安全）**：`mvibe login` 会写入 `wechat.user_id`，此后只有该
  用户能驱动会话；其余被丢弃。多人放行用 `wechat.allowed_users: [...]`。若两者
  皆空，则放行所有人并打印告警——别在这种状态下长期运行。
- **HTTP `/inbound` 鉴权**：默认仅绑 `127.0.0.1`。需要时设 `MVIBE_HTTP_TOKEN`
  环境变量，请求须带 `X-MVIBE-Token` 头匹配。body 上限 64KB。
- **凭证文件权限**：`~/.mvibe` 为 `0700`，`config.json` / `context_tokens` /
  `sync_buf` 为 `0600`（仅本人可读）。
- **注入消毒**：远程文本注入 PTY 前剥除 C0 控制符（含 ESC），防终端转义/控制键
  注入；制表与换行保留。
- **TLS**：若存在 `~/tmp/cacert.pem`，会经 `SSL_CERT_FILE` 启用该 CA bundle
  （Cloudflare WARP 拦截环境所需）——这会扩大信任根，按需保留。

## 注意事项

- iLink 仅允许在手机最近一次给 bot 发消息后的窗口内推送（`context_token`）；
  长时间空闲后推送可能报 errcode -14。
- 键盘注入以 CR 提交；多行文本可能提前断行提交。
- 两端必须用**相同 cwd**（transcript 目录按 cwd 编码）。

## 项目结构

```
mvibe/
  cli.py          命令行（argparse）：默认/up/run/bridge/login/remote/send/flag/status
  wrapper.py      PTY mux 核心：本地透传 + flag 路由 + 本地按键夺回 + 注入消毒
  bridge.py       后台：transcript→微信镜像 + 微信入站 poll + HTTP 接收
  tailer.py       follow transcript .jsonl，抽 assistant text
  wechat_in.py    iLink getUpdates 长轮询入站
  wechat_out.py   iLink send_message 出站
  wechat_login.py QR 扫码绑定
  config.py       ~/.mvibe 凭证/tokens/sync_buf（0600）
  paths.py        路径布局 + flag/gate + cwd→transcript 编码
  _tls.py         可选 CA bundle
  ilink/          vendored iLink 协议（wechat_api.py / wechat_auth.py，仅 aiohttp）
commands/         Claude slash 命令模板（/mvibe-on /off /status）
scripts/smoke.sh  本地自测（无微信、隔离 HOME）
```

## 自测

```bash
bash scripts/smoke.sh    # 9 项：注入/flag 互斥/消毒/HTTP/token/权限/tailer
```

不碰真 `~/.mvibe`（用隔离 `MVIBE_HOME`），无网络无微信，安全可随时跑。
