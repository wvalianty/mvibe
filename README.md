# mvibe

把本地 **Claude Code** TUI 镜像到远程聊天（微信）。本地终端与直接运行 `claude`
逐字节一致；一个标志文件决定是否由远程接管输入/输出。**全程只有一个 `claude`
进程**——切换只是 I/O 路由开关，不重启、不交接会话。

## 工作原理

```
            ┌──────────── mvibe run（持 PTY master）────────────┐
 终端   ────┤ stdin → PTY     （仅 flag=local 时）             │
        ←───┤ PTY  → stdout   （始终；remote 时也能围观）       │──► claude TUI
            │ inject FIFO → PTY（仅 flag=remote 时）            │     （单进程）
            └───────────────────────────────────────────────────┘        │ 写入
                                                          .jsonl ◄─────────┘
 微信 ──POST /inbound──► inject FIFO + flag=remote               │
     ◄── 推送(iLink) ── tail transcript .jsonl（assistant text）◄─┘
                              [mvibe bridge]
```

- **本地保持真 TUI**（富渲染、slash 命令、plan mode）：PTY 透明透传，即
  `script(1)` / `asciinema` 那套。
- **远程输出 ≠ 裸 ANSI**：从会话 transcript（`~/.claude/projects/<cwd>/<id>.jsonl`）
  读，而非镜像 PTY 字节——TUI 的 ANSI 流在聊天框里是乱码。
- **远程输入**：当作键盘注入 PTY，驱动真 TUI。
- **切换** = `~/.mvibe/active` 的内容（`local` | `remote`）。它同时是互斥锁：
  `remote` 丢本地键盘，`local` 丢注入，杜绝两人同敲。

## 用法

```bash
uv run mvibe login               # 一次性：扫码绑定一个微信 bot
uv run mvibe run                 # 终端1：包装版 claude，照常用
uv run mvibe bridge --cwd .      # 终端2：镜像输出 + 微信入站轮询

uv run mvibe flag remote         # 交给远程
uv run mvibe send "继续"          # 注入一行到当前会话
uv run mvibe flag local          # 收回本地
uv run mvibe status
```

手机端：先给 bot 发一条消息建立会话，之后你发的内容会驱动当前 Claude TUI；
Claude 的回复以聊天消息返回。

也可纯 HTTP 驱动（无需微信）：

```bash
curl -X POST --data '继续' localhost:8765/inbound
curl -X POST localhost:8765/flag/local
```

## 微信：独立 bot

mvibe 端到端自持微信 bot——无需 avibe service、无需公网 webhook：

- **绑定**（`mvibe login`，`wechat_login.py`）：经 avibe `WeChatAuthManager`
  跑官方 iLink 扫码流程，把 `bot_token` / `base_url` / `user_id` 写入 **mvibe
  自己的** `~/.mvibe/config.json`。
- **入站**（`wechat_in.py`）：iLink `getUpdates` 长轮询循环，无需 webhook/公网。
  每条消息产出 `(text, user_id)`，置 flag 为 `remote` 并把文本注入 PTY。同步游标
  与每用户 `context_token` 持久化在 `~/.mvibe/state/`。
- **出站**（`wechat_out.py`）：`send_message`，用记住的 `context_token` 寻址。

协议代码运行时借用 avibe 的 `modules.im.*`（把其 venv 的 site-packages 拼到
`sys.path`）；mvibe 自身不内置。

> 请跑**自己**的 `mvibe login`，不要复用 avibe 的 bot_token：同一 bot 上两个
> `getUpdates` 轮询会争抢同步游标。若复用 avibe 凭证（自动 fallback），先停掉
> avibe 的微信适配器。

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
