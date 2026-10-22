# dsh-server API 文档

本文档对应服务版本 `0.1.0`，官方 SDK 版本 `deepseek-harness-sdk 0.2.1rc2`。服务运行时还提供自动生成的 OpenAPI 文档：

- Swagger UI：`GET /docs`
- ReDoc：`GET /redoc`
- OpenAPI JSON：`GET /openapi.json`，可以导入 Postman、Apifox 等工具

## 目录

- [概览](#概览)
- [鉴权](#鉴权)
- [通用约定](#通用约定)
- [健康检查](#健康检查)
  - [GET /healthz](#get-healthz)
- [对话](#对话)
  - [POST /v1/chat](#post-v1chat)
  - [POST /v1/chat/stream](#post-v1chatstream)
  - [WS /v1/ws](#ws-v1ws)
  - [流式事件](#流式事件)
- [会话管理](#会话管理)
  - [GET /v1/sessions](#get-v1sessions)
  - [GET /v1/sessions/{session_id}](#get-v1sessionssession_id)
  - [GET /v1/sessions/{session_id}/messages](#get-v1sessionssession_idmessages)
  - [DELETE /v1/sessions/{session_id}](#delete-v1sessionssession_id)
- [错误](#错误)
- [运行行为](#运行行为)

---

## 概览

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/healthz` | 否 | 健康检查 |
| POST | `/v1/chat` | 是 | 提交任务，等任务结束后返回最终回复 |
| POST | `/v1/chat/stream` | 是 | 提交任务，以 SSE 实时推送过程事件和最终结果 |
| WS | `/v1/ws` | 是 | WebSocket：发送任务，实时接收事件；同一连接可以多轮对话 |
| GET | `/v1/sessions` | 是 | 会话列表 |
| GET | `/v1/sessions/{session_id}` | 是 | 会话详情 |
| GET | `/v1/sessions/{session_id}/messages` | 是 | 会话的对话历史 |
| DELETE | `/v1/sessions/{session_id}` | 是 | 删除会话 |

默认地址：`http://127.0.0.1:8080`。请求体和响应体均为 UTF-8 编码的 JSON（SSE 接口除外）。

## 鉴权

鉴权由配置项 `SERVICE_API_KEY` 控制：

- **未配置**（默认）：所有接口无需鉴权。
- **已配置**：值为逗号分隔的一个或多个 token。除 `/healthz` 外的接口都必须携带其中一个 token：
  - HTTP：请求头 `Authorization: Bearer <token>`
  - WebSocket：请求头 `Authorization: Bearer <token>`，或者 URL 参数 `?token=<token>`（浏览器的 WebSocket API 无法设置请求头时使用）

鉴权失败时，HTTP 接口返回 `401`：

```json
{"detail": "invalid or missing bearer token"}
```

WebSocket 先接受连接，再以关闭码 `4401`（reason `unauthorized`）关闭。

**每个 token 有独立的会话空间。** 不同 token 使用相同的 `session_id`，访问的是两个互不相干的会话；会话管理接口也只能看到和操作当前 token 自己的会话。未开启鉴权时创建的会话，在开启鉴权后无法通过接口访问，反之亦然。

## 通用约定

### session_id

- 格式：`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`，即以字母或数字开头，只包含字母、数字、`.`、`_`、`-`，最长 128 个字符。
- 对话接口不传 `session_id` 时，服务自动生成一个，形如 `session-<32 位 hex>`，并在响应中返回。
- 再次使用同一个 `session_id` 会续接之前的对话。会话持久化在磁盘上，服务重启后仍可续接。

### 时间

会话管理接口中的时间均为 ISO 8601 格式的 UTC 时间，精确到毫秒，例如 `2026-10-22T10:51:17.500Z`。

### 错误响应

业务错误的响应体统一为：

```json
{"detail": "人类可读的错误信息", "code": "机器可读的错误码"}
```

请求参数校验失败时返回 `422`，响应体为 FastAPI 的标准格式：

```json
{"detail": [{"type": "string_too_short", "loc": ["body", "prompt"], "msg": "String should have at least 1 character", "input": "", "ctx": {"min_length": 1}}]}
```

完整的错误码见 [错误](#错误)。

---

## 健康检查

### GET /healthz

无需鉴权。

**响应 200**

```json
{
  "status": "ok",
  "version": "0.1.0",
  "sdk_version": "0.2.1rc2",
  "profile": "sdk",
  "model": "deepseek-v4-flash",
  "harness": {"started": false, "busy": false, "pending": 0}
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `status` | string | 固定为 `ok` |
| `version` | string | 服务版本 |
| `sdk_version` | string | 官方 SDK 版本 |
| `profile` | string | 使用的 DSH profile（`DSH_PROFILE`） |
| `model` | string | 使用的模型（`DSH_MODEL`） |
| `harness.started` | boolean | dsh 运行时进程是否已启动（第一次请求时才启动） |
| `harness.busy` | boolean | 当前是否有任务正在执行 |
| `harness.pending` | integer | 正在执行和排队中的任务总数 |

---

## 对话

三个对话接口使用相同的请求体：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `prompt` | string | 是 | 发给 Agent 的任务或消息，不能为空或只包含空白字符 |
| `session_id` | string | 否 | 续接已有会话时传入；不传则新建会话。格式见 [session_id](#session_id) |

所有任务**按提交顺序逐个执行**，三个接口共用同一个队列。

### POST /v1/chat

提交任务，等任务结束后一次性返回结果。

**请求**

```bash
curl -s http://127.0.0.1:8080/v1/chat \
  -H 'Content-Type: application/json' \
  -d '{"prompt": "用一句话介绍你自己", "session_id": "demo-1"}'
```

**响应 200**

```json
{
  "session_id": "demo-1",
  "final_response": "我是 DeepSeek Harness 驱动的编码智能体，可以帮你读代码、改文件、跑命令、搜资料，解决实际的开发任务。",
  "finish_reason": "completed",
  "error": null
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `session_id` | string | 本次使用的会话 ID |
| `final_response` | string | Agent 最后一条文字回复；没有文字回复时为空字符串 |
| `finish_reason` | string \| null | 本轮的结束原因，常见值：`completed`（正常结束）、`max-tokens`（达到输出 token 上限）、`error`（模型侧出错） |
| `error` | string \| null | `finish_reason` 为 `error` 时的错误信息，例如 API Key 无效、余额不足 |

> **模型侧错误不改变 HTTP 状态码。** 例如 API Key 无效时仍返回 200，此时 `finish_reason` 为 `error`，`error` 字段给出原因。服务侧错误（排队已满、运行时崩溃、超时等）才返回非 2xx 状态码，见 [错误](#错误)。

客户端在任务开始前断开连接，任务会从队列中移除；任务已经开始的，会在后台继续执行完，结果照常写入会话。

### POST /v1/chat/stream

提交任务，以 [Server-Sent Events](https://html.spec.whatwg.org/multipage/server-sent-events.html) 实时推送过程事件和最终结果。

**请求**

```bash
curl -N http://127.0.0.1:8080/v1/chat/stream \
  -H 'Content-Type: application/json' \
  -d '{"prompt": "在当前目录创建 hello.py 并运行"}'
```

**响应 200**

响应头：

| 响应头 | 值 |
|---|---|
| `Content-Type` | `text/event-stream; charset=utf-8` |
| `Cache-Control` | `no-cache` |
| `X-Accel-Buffering` | `no`（避免 Nginx 缓冲） |
| `X-Session-Id` | 本次使用的会话 ID |

响应体由一系列 SSE 消息组成，每条消息的 `event` 是事件类型，`data` 是完整的事件 JSON（格式见 [流式事件](#流式事件)）：

```
event: start
data: {"type": "start", "session_id": "session-3f2a...", "data": {}}

event: notification
data: {"type": "notification", "session_id": "session-3f2a...", "data": {"method": "session.status", "params": {"sessionId": "session-3f2a...", "status": "running"}}}

event: notification
data: {"type": "notification", "session_id": "session-3f2a...", "data": {"method": "session.event", "params": {"sessionId": "session-3f2a...", "event": {"type": "tool/call", "seq": 14, "time": 1792666703170, "data": {"turn": 1, "step": 1, "callId": "call_00_...", "name": "bash", "arguments": "{\"command\": \"python3 hello.py\"}"}}}}}

: ping

event: result
data: {"type": "result", "session_id": "session-3f2a...", "data": {"session_id": "session-3f2a...", "final_response": "...", "finish_reason": "completed", "error": null}}
```

- 事件顺序固定：一个 `start`，若干个 `notification`，最后是一个 `result` 或 `error`，之后服务端关闭响应流。
- 空闲时每隔 `STREAM_HEARTBEAT_SECONDS`（默认 15 秒）发送一条 SSE 注释行 `: ping`，标准 SSE 客户端会自动忽略。
- 任务提交阶段的错误（鉴权失败、请求体不合法、排队已满、会话正在删除等）直接返回对应的 HTTP 状态码，不会建立事件流。
- 事件流建立后发生的错误（运行时崩溃、超时等）以 `error` 事件推送，HTTP 状态码仍是 200。
- 客户端中途断开的处理方式与 `/v1/chat` 相同。

### WS /v1/ws

WebSocket 接口。连接建立后，客户端每发送一条消息就提交一个任务，服务端推送这个任务的事件流。同一连接可以连续进行多轮对话。

**连接**

```
ws://127.0.0.1:8080/v1/ws
ws://127.0.0.1:8080/v1/ws?token=<token>     # 开启鉴权时
```

**客户端消息**

一条文本消息（或 UTF-8 编码的二进制消息），内容是对话请求体的 JSON：

```json
{"prompt": "记住这个数字：42", "session_id": "ws-demo"}
```

**服务端消息**

每条消息是一个事件 JSON，格式见 [流式事件](#流式事件)。一个任务的事件依次为：一个 `start`，若干个 `notification` 和 `ping`，最后是一个 `result` 或 `error`。收到 `result` 或 `error` 后，客户端即可发送下一条消息。

**交互规则**

- 同一连接上每次只执行一个任务。任务执行期间收到的消息会暂存，等当前任务结束后再依次处理。
- 消息不是合法 JSON，或者字段校验失败时，服务端返回一条 `error` 事件（`code` 为 `invalid_request`，`session_id` 为 `null`），**连接保持打开**，可以继续发送。
- 任务提交失败（排队已满、会话正在删除等）时，服务端返回一条 `error` 事件，连接保持打开。
- 客户端断开连接时，正在排队的任务会被取消；已经开始的任务在后台继续执行完。

**关闭码**

| 关闭码 | 含义 |
|---|---|
| `1000` / `1001` | 正常关闭 |
| `4401` | 鉴权失败 |

**示例（Python）**

```python
import asyncio, json, websockets

async def main():
    async with websockets.connect("ws://127.0.0.1:8080/v1/ws") as ws:
        for prompt in ["记住这个数字：42", "我让你记住的数字是多少？"]:
            await ws.send(json.dumps({"prompt": prompt, "session_id": "ws-demo"}))
            while True:
                event = json.loads(await ws.recv())
                if event["type"] == "result":
                    print(event["data"]["final_response"])
                    break
                if event["type"] == "error":
                    print("error:", event["data"])
                    break

asyncio.run(main())
```

**示例（浏览器）**

```javascript
const ws = new WebSocket("ws://127.0.0.1:8080/v1/ws?token=<token>");
ws.onopen = () => ws.send(JSON.stringify({ prompt: "你好", session_id: "web-demo" }));
ws.onmessage = (msg) => {
  const event = JSON.parse(msg.data);
  if (event.type === "result") console.log(event.data.final_response);
};
ws.onclose = (e) => { if (e.code === 4401) console.error("unauthorized"); };
```

### 流式事件

SSE 和 WebSocket 使用相同的事件格式：

```json
{"type": "<事件类型>", "session_id": "<会话 ID>", "data": {}}
```

| `type` | `data` | 说明 |
|---|---|---|
| `start` | `{}` | 任务已提交（可能还在排队） |
| `notification` | `{"method": string, "params": object}` | SDK 推送的原始通知，内容见下文 |
| `ping` | `{}` | 空闲心跳。SSE 中以注释行 `: ping` 发送，不是一条 `event` |
| `result` | 与 [`POST /v1/chat`](#post-v1chat) 的响应相同 | 任务结束。模型侧出错时 `finish_reason` 为 `error` |
| `error` | `{"code": string, "message": string}` | 服务侧失败，`code` 见 [错误](#错误) |

事件中的 `session_id` 始终是客户端看到的会话 ID。`error` 事件在请求体校验失败时 `session_id` 为 `null`。

**notification 的 method**

| `method` | `params` | 说明 |
|---|---|---|
| `session.event` | `{"sessionId", "event": {"type", "seq", "time", "data"}}` | 会话日志中新增的一个事件 |
| `session.status` | `{"sessionId", "status"}` | 会话状态变化，`status` 为 `running` 或 `idle` |
| `subagent.started` | `{"parentSessionId", "childSessionId", ...}` | Agent 派生了子 agent |
| `subagent.finished` | `{"parentSessionId", "childSessionId", "status", ...}` | 子 agent 结束 |

`params` 中的会话 ID 是运行时内部的 ID：开启鉴权时带有 token 前缀（形如 `t<12 位 hex>--<session_id>`），子 agent 的事件则是子会话的 ID。

**常见的 session.event 事件类型**（`params.event.type`）

| 事件类型 | 说明 |
|---|---|
| `turn/start`、`turn/end` | 一轮对话开始和结束。`turn/end` 的 `data.reason.kind` 即 `finish_reason` |
| `step/start`、`step/end` | 一轮中的一次模型调用开始和结束 |
| `user/message` | 用户消息。`data.source.kind` 为 `user` 时是用户的 prompt，为 `runtime-context` 时是运行时注入的上下文 |
| `assistant/attempt` | 模型调用的流式输出过程 |
| `assistant/message` | 模型的一条完整回复，`data.message.content` 中可能包含 `text`、`reasoning`、`tool-call` 块，`data.usage` 是 token 用量 |
| `tool/call`、`tool/result` | 工具调用及其结果 |
| `job/registered`、`job/settled` | 后台作业（如长时间运行的 bash 命令）开始和结束 |
| `approval/asked`、`approval/decided` | 需要审批的操作。服务没有审批通道，此类操作会被自动拒绝 |
| `session/title` | 会话标题生成 |
| `request/context` | 本次模型调用使用的 provider、模型和上下文窗口 |

`system/message`（完整系统提示词）和 `request/header`（完整工具 schema）体积大，默认不推送，可通过配置项 `STREAM_EXCLUDED_EVENT_TYPES` 调整。

---

## 会话管理

会话管理接口直接读取 `DSH_HOME/sessions/` 下的会话日志，服务重启前创建的会话也能查到。结果只包含当前 token 的会话；子 agent 会话不会单独出现，但会随父会话一起删除。

### GET /v1/sessions

列出会话，按最后更新时间倒序排列。

**查询参数**

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `limit` | integer | 50 | 每页数量，取值 1–500 |
| `offset` | integer | 0 | 跳过的条数 |

**响应 200**

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

| 字段 | 类型 | 说明 |
|---|---|---|
| `total` | integer | 符合条件的会话总数（不受分页影响） |
| `sessions[].session_id` | string | 会话 ID |
| `sessions[].title` | string \| null | 运行时根据第一条 prompt 生成的标题 |
| `sessions[].created_at` | string \| null | 创建时间 |
| `sessions[].updated_at` | string \| null | 最后一个事件的时间 |
| `sessions[].turns` | integer | 用户发送的 prompt 条数 |
| `sessions[].status` | string | `running`（正在执行）、`queued`（有任务在排队）、`idle`（空闲） |

### GET /v1/sessions/{session_id}

会话详情。在列表字段的基础上，增加以下字段：

**响应 200**

```json
{
  "session_id": "e2e-sse",
  "title": "在当前目录创建 hello.py，内容",
  "created_at": "2026-10-22T10:51:17.500Z",
  "updated_at": "2026-10-22T10:51:25.171Z",
  "turns": 1,
  "status": "idle",
  "model": "deepseek-v4-flash",
  "cwd": "/data/workspace/dsh-server/data/workspace",
  "event_count": 40,
  "last_finish_reason": "completed",
  "last_error": null,
  "usage": {
    "input_tokens": 1166,
    "output_tokens": 995,
    "cache_read_tokens": 29056,
    "cache_write_tokens": 0,
    "total_tokens": 31217
  },
  "subagent_sessions": 0
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `model` | string \| null | 最近一次模型调用使用的模型 |
| `cwd` | string \| null | 会话创建时的工作目录 |
| `event_count` | integer | 会话日志中的事件数 |
| `last_finish_reason` | string \| null | 最后一轮的结束原因 |
| `last_error` | string \| null | 最后一轮以 `error` 结束时的错误信息 |
| `usage` | object | 该会话所有模型调用的 token 用量之和，不含子 agent 会话 |
| `subagent_sessions` | integer | 由该会话派生的子 agent 会话数量（含多级） |

**响应 404**：会话不存在，或者不属于当前 token。

### GET /v1/sessions/{session_id}/messages

会话的对话历史，按时间顺序排列。

**查询参数**

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `include_tools` | boolean | `false` | 附带助手的工具调用（`tool_calls`）和工具结果（`role` 为 `tool` 的消息） |
| `include_reasoning` | boolean | `false` | 附带模型的推理内容（`reasoning`） |

**请求**

```bash
curl -s 'http://127.0.0.1:8080/v1/sessions/e2e-sse/messages?include_tools=true'
```

**响应 200**

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

| 字段 | 类型 | 说明 |
|---|---|---|
| `seq` | integer | 事件在会话日志中的序号 |
| `time` | string | 事件时间 |
| `role` | string | `user`、`assistant` 或 `tool` |
| `text` | string | 文字内容 |
| `reasoning` | string | 仅 `include_reasoning=true` 且有推理内容时出现 |
| `tool_calls` | array | 仅 `include_tools=true` 且助手调用了工具时出现。`arguments` 是模型生成的原始 JSON 字符串 |
| `tool_call_id` | string | 仅 `role` 为 `tool` 时出现，对应 `tool_calls[].id` |
| `is_error` | boolean | 仅 `role` 为 `tool` 时出现，表示工具执行是否出错 |

- 值为空的可选字段不会出现在响应中。
- 运行时自动注入的上下文消息（工作目录、沙箱策略等）不会出现在结果中。
- 默认模式下，只调用了工具、没有文字的助手消息会被省略。

**响应 404**：会话不存在，或者不属于当前 token。

### DELETE /v1/sessions/{session_id}

删除会话，连同它派生的所有子 agent 会话和对应的投影缓存。

**请求**

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X DELETE http://127.0.0.1:8080/v1/sessions/e2e-sse
```

**响应**

| 状态码 | 说明 |
|---|---|
| `204` | 删除成功，无响应体 |
| `404` | 会话不存在，或者不属于当前 token |
| `409` | 会话还有任务在排队或执行中，或者正在被另一个请求删除。等会话空闲后重试 |

- 如果会话已被运行时加载（本次服务启动后用过），删除时会重启运行时来释放它；如果此时有其他会话的任务正在执行，删除会等该任务结束后再进行。其他会话不受影响，下次使用时自动从磁盘恢复。
- 删除过程中，对该会话发起的新任务返回 `409`。
- 删除后可以再次使用同一个 `session_id`，会开始一个全新的会话。
- Agent 在工作目录中创建的文件不会被删除（工作目录由所有会话共用）。

---

## 错误

### HTTP 状态码与错误码

| HTTP | `code` | 出现在 | 说明 |
|---|---|---|---|
| 401 | - | 除 `/healthz` 外的所有 HTTP 接口 | 缺少或错误的 Bearer Token |
| 404 | `session_not_found` | 会话管理接口 | 会话不存在，或者不属于当前 token |
| 409 | `session_busy` | 对话接口、`DELETE /v1/sessions/{id}` | 删除时会话仍有任务；或者向正在删除的会话提交任务 |
| 422 | - | 所有带参数的接口 | 请求体或参数校验失败 |
| 429 | `queue_full` | 对话接口 | 执行中和排队中的任务总数已达 `MAX_PENDING_RUNS`，稍后重试 |
| 502 | `harness_error` | 对话接口 | 运行时异常（进程退出、协议错误、启动失败等），下次请求会自动重启运行时 |
| 503 | `service_closed` | 对话接口、`DELETE /v1/sessions/{id}` | 服务正在关闭 |
| 503 | `cancelled` | 对话接口 | 任务在开始前被取消 |
| 504 | `timeout` | 对话接口 | 任务超过 `RUN_TIMEOUT_SECONDS`，运行时已被重启 |

### 流式接口中的错误

- **SSE**：任务提交前的错误直接返回上表中的 HTTP 状态码；事件流建立后的错误以 `error` 事件推送，`data.code` 取值同上表。
- **WebSocket**：所有错误都以 `error` 事件推送，连接保持打开（鉴权失败除外，见关闭码 `4401`）。额外的错误码：

| `code` | 说明 |
|---|---|
| `invalid_request` | 消息不是合法 JSON，或者字段校验失败。`message` 中包含具体字段和原因 |
| `internal_error` | 未预期的服务端错误 |

---

## 运行行为

- **串行执行**：服务只有一个 Harness 运行时，所有任务按提交顺序逐个执行。执行中和排队中的任务总数上限为 `MAX_PENDING_RUNS`（默认 8）。
- **运行时启动**：dsh 运行时进程在第一次提交任务时启动（约 1–2 秒），之后一直复用。运行时崩溃或任务超时后，下一个任务会自动重启运行时。
- **会话续接**：服务或运行时重启后，再次使用某个 `session_id` 时，服务会自动从磁盘恢复该会话。如果会话原来的工作目录已不存在（例如从本地运行切换到 Docker 部署），会切换到当前的 `DSH_WORKSPACE`。
- **取消**：SDK 没有提供取消接口。客户端断开时，排队中的任务会被移除，已开始的任务会在后台执行完。
- **超时**：设置 `RUN_TIMEOUT_SECONDS` 后，单个任务超时会返回 `504` 并重启运行时；默认不限制。
