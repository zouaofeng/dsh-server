# dsh-server

基于 FastAPI 和官方 DeepSeek Harness Python SDK（[`deepseek-harness-sdk`](https://github.com/deepseek-ai/deepseek-harness/tree/main/python/sdk)）的轻量服务，提供三种调用方式：

| 方式 | 接口 | 说明 |
|---|---|---|
| HTTP 同步 | `POST /v1/chat` | 等任务结束后一次性返回最终回复 |
| HTTP SSE | `POST /v1/chat/stream` | 以 `text/event-stream` 实时推送过程事件和最终结果 |
| WebSocket | `WS /v1/ws` | 发送 JSON prompt，实时接收事件；同一连接可以连续进行多轮对话 |

另外，[`/v1/sessions`](#会话管理) 提供会话管理接口（列表、详情、对话历史、删除），`GET /healthz` 用于健康检查，`/docs` 是 OpenAPI 文档页面。

**完整的接口说明见 [API 文档](docs/API.md)。** 支持本地运行和 [Docker Compose 部署](#docker-compose-部署)。

## 快速开始

先安装沙箱后端 bubblewrap（Linux）：

```bash
dnf install -y bubblewrap        # Debian/Ubuntu：apt install -y bubblewrap
```

> 默认的 `workspace-write` 模式依赖 bubblewrap 或 Landlock（需要内核开启 `CONFIG_SECURITY_LANDLOCK`）。如果两者都不可用，DSH 会**拒绝执行任何 shell 命令**：Agent 仍然可以读写文件，但不能运行代码。

```bash
cd dsh-server
python3.10 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt     # 自带当前平台的 dsh 运行时，不需要 Node.js

cp .env.example .env                # 至少填写 DEEPSEEK_API_KEY
uvicorn app.main:app --host 0.0.0.0 --port 8080 --workers 1 --timeout-graceful-shutdown 10
# 或者：python -m app.main
```

> 只能使用 **单个 worker**。Harness 实例保存在进程内存中，多个 worker 会各自启动运行时，导致会话互不可见。
>
> `--timeout-graceful-shutdown` 用来限制停止服务时的等待时间：不加的话，停止服务时如果还有任务在执行，uvicorn 会一直等它结束。

## Docker Compose 部署

```bash
cd dsh-server
cp .env.example .env                # 至少填写 DEEPSEEK_API_KEY
docker compose up -d --build        # 旧版本 compose 使用 docker-compose
docker compose ps                   # STATUS 显示 healthy 即可使用
docker compose logs -f
```

镜像已内置 bubblewrap，不需要在宿主机安装。服务监听宿主机的 `PORT` 端口（默认 8080），数据保存在宿主机的 `DATA_DIR` 目录（默认 `./data`，挂载到容器内的 `/app/data`）。

`.env` 中与部署相关的变量（只被 compose 使用）：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PORT` | `8080` | 宿主机上发布的端口 |
| `DATA_DIR` | `./data` | 宿主机数据目录 |
| `APP_UID` / `APP_GID` | `1000` | 数据文件的属主。建议设为宿主机当前用户（`id -u` / `id -g`），方便在宿主机上直接查看和修改工作区文件 |
| `PIP_MIRROR` | 空（pypi.org） | 构建镜像时使用的 PyPI 镜像源，例如 `https://mirrors.tencent.com/pypi/simple/` |

容器内 `DSH_HOME` 和 `DSH_WORKSPACE` 固定为 `/app/data/home`、`/app/data/workspace`，`.env` 中的这两项不生效。

**容器的安全配置**

- 服务以非 root 用户运行。启动时入口脚本以 root 身份把 `DATA_DIR` 的属主改为 `APP_UID:APP_GID`，然后降权启动服务。
- bubblewrap 需要在容器内创建命名空间并挂载 `/proc`，所以 compose 文件关闭了容器的 seccomp、AppArmor 限制，并解除了 `/proc` 的路径屏蔽（`systempaths=unconfined`）。容器没有获得任何额外的 capability。
- 如果不允许放开这些限制，可以删掉 compose 文件中的 `security_opt`，同时设置 `DSH_PERMISSION_MODE=danger-full-access`：Agent 在容器内不再受文件写入限制，由容器本身作为隔离边界。

**停止服务**

`docker compose stop` / `down` 时，正在执行的任务最多再执行 10 秒，然后被中断；随后关闭运行时，整个过程不超过 30 秒。被中断的任务已写入的会话内容会保留。

**从本地运行切换到 Docker**

直接把原来的 `data/` 目录作为 `DATA_DIR` 即可，会话列表、对话历史都能正常查看和续接。需要注意：

- 会话会记住创建时的工作目录。本地创建的会话在容器内续接时，原来的目录不存在，服务会自动把它切换到容器的工作目录。
- 但会话的沙箱写入范围在创建时就已确定，仍是原来的路径，所以这些旧会话在容器内**无法执行 shell 命令**，只能继续对话。需要执行命令时请新建会话。反过来从 Docker 切回本地也一样。

## 调用示例

请求体（三种方式通用）：`{"prompt": "...", "session_id": "可选"}`

- 不传 `session_id` 时会自动生成一个（形如 `session-<hex>`）。
- 再次使用同一个 `session_id` 会**续接之前的对话**。会话持久化在 `$DSH_HOME/sessions/`，服务或运行时重启后也能接着聊：服务会自动从磁盘恢复会话。
- `session_id` 只能包含字母、数字、`.`、`_`、`-`，最长 128 个字符。

如果配置了 `SERVICE_API_KEY`，需要带上 `-H "Authorization: Bearer <token>"`。

### 会话隔离

- **模型的对话记忆按会话隔离**：新会话看不到其他会话的对话内容。
- **按 token 隔离会话**：配置了 `SERVICE_API_KEY` 后，每个 token 有独立的会话空间。不同 token 使用同一个 `session_id`，得到的是两个互不相干的会话。实现方式是在运行时内部给会话 ID 加上 token 哈希前缀（形如 `t<12位hex>--<session_id>`）。接口返回的 `session_id` 仍然是客户端传入的值；只有 `notification` 事件里 SDK 原样透传的 `params.sessionId` 会带前缀。
  - 更换 token 后，旧 token 下的会话就无法再通过接口访问了，但数据仍保留在磁盘上。
  - 同理，开启或关闭鉴权会切换会话空间：未开启鉴权时创建的会话，开启鉴权后无法通过接口访问。
- **没有隔离的部分**：所有会话共用同一个工作目录 `DSH_WORKSPACE`；`workspace-write` 也不限制读取，所以 Agent 能通过 bash 读到 `$DSH_HOME/sessions/` 下其他会话的记录。如果需要隔离到这一层，要给每个用户单独部署服务实例（单独的 `DSH_HOME` 和 `DSH_WORKSPACE`），或者使用容器隔离。

### HTTP 同步

```bash
curl -s http://127.0.0.1:8080/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"prompt": "列出当前目录的文件", "session_id": "demo-1"}'
```

```json
{"session_id": "demo-1", "final_response": "...", "finish_reason": "completed", "error": null}
```

- `finish_reason` 取自 SDK，常见值有 `completed`、`max-tokens`、`error`。
- 模型侧出错时（例如 API Key 无效），HTTP 状态码仍是 200，此时 `finish_reason` 为 `error`，`error` 字段给出原因。

### HTTP SSE

```bash
curl -N http://127.0.0.1:8080/v1/chat/stream \
  -H 'Content-Type: application/json' \
  -d '{"prompt": "写一个 hello.py 并运行"}'
```

```
event: start
data: {"type": "start", "session_id": "session-...", "data": {}}

event: notification
data: {"type": "notification", "session_id": "session-...", "data": {"method": "session.event", "params": {...}}}

: ping

event: result
data: {"type": "result", "session_id": "session-...", "data": {"session_id": "...", "final_response": "...", "finish_reason": "completed", "error": null}}
```

响应头 `X-Session-Id` 会返回本次使用的会话 ID。

### WebSocket

```python
import asyncio, json, websockets   # 随 uvicorn[standard] 一起安装

async def main():
    # 鉴权方式：在 URL 加 ?token=<token>，或在请求头中带 Authorization: Bearer <token>
    async with websockets.connect("ws://127.0.0.1:8080/v1/ws") as ws:
        for prompt in ["你好", "刚才我说了什么？"]:
            await ws.send(json.dumps({"prompt": prompt, "session_id": "ws-demo"}))
            while True:
                event = json.loads(await ws.recv())
                if event["type"] in ("result", "error"):
                    print(event["data"])
                    break

asyncio.run(main())
```

- 同一连接上每次只执行一个任务。任务执行期间收到的消息会排队，等当前任务结束后再处理。
- 消息格式错误时，服务端返回 `invalid_request` 错误事件，连接不会断开。
- 鉴权失败时，连接以关闭码 `4401` 关闭。

## 事件格式

SSE 和 WebSocket 使用相同的事件格式：`{"type", "session_id", "data"}`。

| type | 含义 |
|---|---|
| `start` | 任务已经提交 |
| `notification` | SDK 原始通知，`data = {"method", "params"}`。`method` 包括 `session.event`（具体事件见 `params.event.type`，例如 `turn/start`、`assistant/attempt`、`assistant/message`、工具调用、`turn/end`）、`session.status`（`running`/`idle`）、`subagent.started`/`subagent.finished` 等 |
| `ping` | 空闲心跳，间隔为 `STREAM_HEARTBEAT_SECONDS`。SSE 中以注释行 `: ping` 发送 |
| `result` | 任务结束，内容同 `/v1/chat` 的响应 |
| `error` | 服务侧失败，`data = {"code", "message"}`，`code` 取值见下表 |

`system/message` 和 `request/header` 两类事件体积大（包含完整系统提示词和工具 schema），默认不推送。可以通过 `STREAM_EXCLUDED_EVENT_TYPES` 调整。

## 会话管理

| 方法 | 接口 | 说明 |
|---|---|---|
| GET | `/v1/sessions?limit=50&offset=0` | 列出会话，按最后更新时间倒序。`limit` 取值 1–500 |
| GET | `/v1/sessions/{session_id}` | 会话详情：模型、轮数、最后一轮的结束原因和错误、累计 token 用量等 |
| GET | `/v1/sessions/{session_id}/messages` | 对话历史。`include_tools=true` 附带工具调用和工具结果，`include_reasoning=true` 附带模型的推理内容 |
| DELETE | `/v1/sessions/{session_id}` | 删除会话，连同它派生的子 agent 会话和缓存，成功返回 204 |

和对话接口一样，这些接口在配置了 `SERVICE_API_KEY` 时需要带 Bearer Token，并且**只能看到和操作当前 token 自己的会话**（见[会话隔离](#会话隔离)）。数据直接读取 `$DSH_HOME/sessions/` 下的会话日志，服务重启前创建的会话也能查到。

### 列表和详情

```bash
curl -s 'http://127.0.0.1:8080/v1/sessions?limit=20'
```

```json
{
  "total": 1,
  "sessions": [
    {
      "session_id": "e2e-sse",
      "title": "在当前目录创建 hello.py，内容",
      "created_at": "2026-10-22T10:51:17.500Z",
      "updated_at": "2026-10-22T10:51:25.171Z",
      "turns": 1,
      "status": "idle"
    }
  ]
}
```

- `title` 由运行时根据第一条 prompt 生成；`turns` 是用户发送的 prompt 条数。
- `status`：`running`（正在执行）、`queued`（排队中）、`idle`（空闲）。
- 子 agent 会话不会单独列出。

`GET /v1/sessions/e2e-sse` 在上面的字段之外还会返回：

```json
{
  "model": "deepseek-v4-flash",
  "cwd": "/data/workspace/dsh-server/data/workspace",
  "event_count": 40,
  "last_finish_reason": "completed",
  "last_error": null,
  "usage": {"input_tokens": 1166, "output_tokens": 995, "cache_read_tokens": 29056, "cache_write_tokens": 0, "total_tokens": 31217},
  "subagent_sessions": 0
}
```

`usage` 是该会话所有模型调用的 token 用量之和，不含子 agent 会话。

### 对话历史

```bash
curl -s 'http://127.0.0.1:8080/v1/sessions/e2e-sse/messages?include_tools=true'
```

```json
{
  "session_id": "e2e-sse",
  "messages": [
    {"seq": 8, "time": "2026-10-22T10:51:17.605Z", "role": "user", "text": "在当前目录创建 hello.py，内容是打印 hello dsh，然后用 python3 运行它，最后告诉我输出。"},
    {"seq": 13, "time": "2026-10-22T10:51:18.465Z", "role": "assistant", "text": "我会在当前目录创建该文件，然后运行它。",
     "tool_calls": [{"id": "call_00_ET_20XM...", "name": "write", "arguments": "{\"file_path\": \"/data/workspace/dsh-server/data/workspace/hello.py\", \"content\": \"print(\\\"hello dsh\\\")\\n\"}"}]},
    {"seq": 15, "time": "2026-10-22T10:51:18.538Z", "role": "tool", "text": "<path>/data/workspace/dsh-server/data/workspace/hello.py</path>\n<type>file</type>...", "tool_call_id": "call_00_ET_20XM...", "is_error": false}
  ]
}
```

- 默认只返回用户的 prompt 和助手的文字回复。运行时自动注入的上下文消息（工作目录、沙箱策略等）不会出现在结果里。只调用工具、没有文字的助手消息，在默认模式下也会被省略。
- `seq` 是事件在会话日志中的序号，`arguments` 是模型生成的原始 JSON 字符串。

### 删除

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X DELETE http://127.0.0.1:8080/v1/sessions/e2e-sse   # 204
```

- 删除的内容：会话目录（日志和锁文件）、它派生的所有子 agent 会话，以及对应的投影缓存。
- 会话还有排队中或执行中的任务时返回 409 `session_busy`。删除过程中对这个会话发起的新任务也会返回 409。
- 如果会话已经被运行时加载（本次启动后用过），删除时会**重启运行时**来释放它：如果其他会话正在执行任务，要等那个任务结束才会执行删除。其他会话不受影响，下次使用时会自动从磁盘恢复。
- 删除后，同一个 `session_id` 可以再次使用，会开始一个全新的会话。

## 错误码

| HTTP | code | 场景 |
|---|---|---|
| 401 | - | 鉴权失败 |
| 404 | `session_not_found` | 会话不存在，或者不属于当前 token |
| 409 | `session_busy` | 删除时会话还有任务在排队或执行；或者对正在删除的会话发起新任务 |
| 422 | - | 请求体或参数不合法 |
| 429 | `queue_full` | 运行中和排队中的任务总数超过 `MAX_PENDING_RUNS` |
| 502 | `harness_error` | 运行时异常（进程退出、协议错误、启动失败等）。下次请求会自动重启运行时 |
| 504 | `timeout` | 任务超过 `RUN_TIMEOUT_SECONDS`，运行时已被重启 |
| 503 | `service_closed` / `cancelled` | 服务正在关闭，或任务已被取消 |

SSE 和 WebSocket 中，同样的错误会以 `error` 事件推送。SSE 的排队已满会直接返回 HTTP 429。

## 运行模型

- 全局只有一个 `DeepSeekHarness` 实例。`dsh` 子进程在第一次请求时启动，之后一直复用。
- 所有任务在一个专用线程中**按提交顺序逐个执行**，HTTP、SSE、WS 共用同一个队列。
- 客户端断开时：
  - 如果任务还在排队，会被直接取消。
  - 如果任务已经开始，会在后台继续执行完（SDK 没有提供取消接口），会话照常保存。

## 数据目录

默认配置下，所有运行数据都在 `./data`（已加入 `.gitignore`）。本地运行时，位置由 `DSH_HOME` 和 `DSH_WORKSPACE` 决定，相对路径以启动服务时的当前目录为准；Docker 部署时在 `DATA_DIR` 目录下，结构相同。

```
data/
├── home/                                    DSH_HOME：Harness 的 home 目录（SDK 不会使用 ~/.dsh）
│   ├── .anonymous-user-id                   运行时生成的匿名用户标识
│   ├── profiles/
│   │   └── sdk/                             DSH_PROFILE 对应的 profile，首次启动时生成
│   │       ├── package.json                 profile 由哪些插件 bundle 组成
│   │       ├── cordis.yml                   profile 根配置（空列表，不要修改）
│   │       ├── cordis.patch.yml             持久化的用户 patch 层，对该 profile 的每次启动都生效
│   │       └── pnpm-workspace.yaml          用 dsh plugin 安装插件时使用
│   ├── sessions/                            会话持久化
│   │   └── --data-workspace-dsh-server-data-workspace--/   项目目录，由会话的工作目录路径转换而来
│   │       └── <会话 ID>/                    每个会话一个目录
│   │           ├── session.v4.jsonl.zstd    会话日志：第 1 行是 header，之后每行一个事件，按 zstd 帧追加写入
│   │           └── session.lock             写锁：会话被运行时加载期间，由运行时通过 flock 持有
│   └── storages/
│       └── session_projcache/sessions/
│           └── <会话 ID>.json                会话投影缓存（标题等派生数据），删掉后会自动重建
└── workspace/                               DSH_WORKSPACE：Agent 的工作目录，所有会话共用
```

- **会话目录名**：未开启鉴权时就是 `session_id`；开启鉴权后是 `t<12 位 hex>--<session_id>`，前缀是 token 的哈希值（token 本身不会写到磁盘上）。子 agent 会话的目录名是随机 UUID，header 中的 `parentSession` 字段指向父会话。
- **项目目录**：由会话创建时的工作目录生成（`/` 换成 `-`）。修改 `DSH_WORKSPACE` 后，新会话会写到新的项目目录下。运行时和会话管理接口都会在所有项目目录中按 ID 查找会话，所以旧会话仍然可以续接和管理。
- **查看原始日志**：`zstdcat data/home/sessions/*/<会话 ID>/session.v4.jsonl.zstd | head`。事件类型包括 `user/message`、`assistant/message`、`tool/call`、`tool/result`、`turn/end` 等。
- **清理数据**：删除单个会话请用 `DELETE /v1/sessions/{session_id}`，不要在服务运行时手动删除会话目录。要清空全部数据，先停止服务，再 `rm -rf data/`，下次启动会自动重建。

## 配置

完整配置项见 [`.env.example`](.env.example)，常用的有：

- `DSH_PROFILE`：
  - `sdk`（默认）：完整工具集，包括文件、bash、web、子 agent 等。
  - `sdk-minimal`：只有持久 shell，权限固定为 `danger-full-access`。
- `DSH_PERMISSION_MODE`：只对 `sdk` profile 生效，默认 `workspace-write`，即只允许写工作区。
  - 服务没有人工审批通道，需要审批的操作会被**自动拒绝**。
  - 设为 `danger-full-access` 表示不再审批，也不做限制。
- `DISABLE_SESSION_LOG_UPLOAD=true`（默认）：通过 [`patches/disable-session-log.patch.yml`](patches/disable-session-log.patch.yml) 关闭官方 `session-log-deepseek` 组件。这个组件默认会把会话日志随模型请求一起上传给 DeepSeek，每次最多 8 MiB。
- `DSH_EXTRA_PATCHES`：追加自定义 cordis patch 文件，例如启用其他插件。

## 安全提示

- Agent 可以在 `DSH_WORKSPACE` 中执行 shell 命令和修改文件。请使用一次性的目录或容器，并且不要把服务直接暴露到公网；如需对外提供，至少配置 `SERVICE_API_KEY`。
- `workspace-write` 由 bubblewrap 把工作区以外的路径挂载为只读，写到工作区之外会报 `Read-only file system`。但它**不限制读取，也不限制网络访问**，Agent 仍然能读到宿主机上的文件、访问外网。这个服务的定位是内部使用；如果要开放给外部用户或运行不可信代码，需要换成容器（最好配合 gVisor）或微型虚拟机，并加上出网白名单。
- 服务日志只记录 prompt 长度和 `session_id`，不记录 prompt 内容和 API Key。

## 测试

```bash
pip install -r requirements.txt
python -m pytest
```

测试使用真实 SDK，但把 `dsh_bin` 指向 [`tests/fake_dsh.py`](tests/fake_dsh.py)（一个模拟的 JSON-RPC 运行时），因此不需要 API Key，也不会产生网络请求。

## 目录结构

```
app/
  main.py             FastAPI 入口、lifespan、异常映射
  routes.py           /healthz、/v1/chat、/v1/chat/stream、/v1/ws
  session_routes.py   /v1/sessions 会话管理接口
  harness_service.py  单实例 Harness、单线程执行、通知桥接、超时与重启、会话删除
  sessions.py         会话管理：按 token 过滤、组装列表 / 详情 / 历史
  session_store.py    读取和删除 $DSH_HOME 下持久化的会话
  scopes.py           session_id 规则与 token 作用域
  config.py           配置（环境变量 / .env）
  schemas.py          请求和响应模型
  auth.py             可选 Bearer Token 鉴权
patches/              传给 SDK 的 cordis patch
tests/                pytest 用例与模拟运行时
docs/API.md           API 文档
Dockerfile            服务镜像（内置 bubblewrap，非 root 运行）
docker-entrypoint.sh  容器入口：准备数据目录权限后降权启动
docker-compose.yml    Docker Compose 部署配置
data/                 运行数据（见“数据目录”）
```
