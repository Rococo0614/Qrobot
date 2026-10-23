# QRobot

QRobot 是一个面向个人实验的 **QQ + LLM 聊天助手**。它可以通过两种方式接收和发送 QQ 消息：

1. **OneBot 11 forward WebSocket**：适合群聊和长期运行；
2. **Linux/X11 桌面自动化**：监听官方 QQ 桌面通知，并用 `xdotool` + `xclip` 回复。

模型后端支持：

- **OpenCode**：复用本机 OpenCode 已登录的 OpenAI、Copilot 等账号，不导出 OAuth 凭据；
- **OpenAI Responses API**：使用单独计费的 `OPENAI_API_KEY`；
- **GitHub Copilot SDK**：使用 Copilot 登录态或 Token；
- **DeepSeek API**：使用官方 OpenAI-compatible 接口。

```text
QQ / NapCat / 桌面通知
          │
          ▼
  触发器、白名单、限流
          │
          ├── 本地结构化长期记忆（SQLite）
          ├── 可选网页搜索（DDGS）
          └── 可选图片下载与大小校验
          │
          ▼
 OpenCode / OpenAI / Copilot / DeepSeek
          │
          ▼
    dry-run 或发送回 QQ
```

## 当前实现

- OneBot 11 forward WebSocket 自动重连、API echo 对应和发送结果检查；
- 群号与私聊账号分别使用白名单；
- 支持 `/ai `、@机器人、二者任一或被动监听四种触发模式；
- 按会话保留短期上下文，按“群 + 群友”隔离长期记忆；
- 只保存提取后的结构化记忆，不保存完整群聊原文；
- 密码、Token、手机号、身份证号、银行卡号、邮箱和住址等内容拒绝进入记忆；
- OpenAI/Copilot 后端支持图片；不支持图片的后端会明确降级，不猜测图片内容；
- 可选 DDGS 网页搜索，适用于新闻、天气、价格、版本等实时问题；
- 回复长度、并发、队列、每人频率和图片大小均有限制；
- 默认 `dry_run=true`，不会向 QQ 发送消息；
- 提供环境检查、后端测试、消息模拟、记忆管理和单元测试。

## 重要安全边界

1. **不要直接把正在编程的 OpenCode Agent 会话暴露给群聊。** QRobot 使用 OpenCode 的无工具单次生成接口，自行传入有限上下文，群消息不能调用终端、读写文件或修改代码。
2. OpenCode V2 的生成 HTTP API 当前标记为实验性。调用被封装在 `src/qrobot/opencode_backend.py`，以后升级只需调整该适配器。
3. ChatGPT Plus/Pro 等订阅通常不等于 OpenAI API 额度。使用 `llm_backend=openai` 时需要单独的 API Key；使用 `llm_backend=opencode` 时可复用 OpenCode 支持的 OAuth 登录。
4. NapCat 等 OneBot 网关属于非官方 QQ 集成。请自行评估 QQ 服务条款、账号风控和隐私风险，建议使用测试小号与测试群。
5. 桌面模式只控制已安装的官方 QQ，不保存 QQ 密码，也不会绕过扫码、验证码或设备验证。
6. 不要把 `.env`、`qrobot.json` 中的私人群号、`data/`、QQ 登录数据或任何 Token 提交到 Git。
7. 应告知群友这是 AI 助手，不要用它群发、骚扰、冒充真人或处理高风险决策。
8. 使用订阅或 API 进行自动化调用前，请确认符合 OpenAI、GitHub Copilot 或所选模型服务的条款和速率限制。

## 环境要求

基础要求：

- Python 3.10+；Copilot SDK 需要 Python 3.11+；
- `httpx` 与 `python-dotenv`（基础依赖会自动安装）；
- OneBot 模式需要 `websockets`；
- 搜索需要 `ddgs`；
- 桌面模式需要 Linux + X11、官方 QQ、`xdotool`、`xclip`、`dbus-monitor`。

Ubuntu 桌面依赖：

```bash
sudo apt install xdotool xclip dbus
```

## 安装

```bash
cd /home/jiangrui/Qrobot
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev,onebot,search]'
cp config.onebot.example.json qrobot.json
cp .env.example .env
```

如需 Copilot：

```bash
pip install -e '.[copilot]'
python -m copilot download-runtime
```

检查全部基础条件：

```bash
./run.sh doctor
```

在 OneBot 模式下，`doctor` 还会建立一次 WebSocket 并调用只读的
`get_version_info`；它不会读取好友、群列表或聊天记录。

## 模型后端

### 1. OpenCode 登录态（本机推荐）

这种模式通过本机 OpenCode V2 服务调用模型。OAuth 凭据仍由 OpenCode 管理，QRobot 不读取或复制 Token。

```json
{
  "llm_backend": "opencode",
  "opencode_model": "openai/gpt-5.6-sol",
  "opencode_variant": "medium",
  "opencode_cli": "opencode",
  "opencode_transport": "http",
  "opencode_working_directory": "."
}
```

检查 OpenCode：

```bash
opencode service status
./run.sh doctor
./run.sh test-opencode
```

模型名必须使用 `provider/model` 格式。`doctor` 会确认模型存在、档位有效，并检查对应 Provider 是否已经登录。

OpenCode 后端特点：

- 不需要把 OAuth Token 写入 `.env`；
- 默认只读取权限应为 `0600` 的 OpenCode 本地服务登记文件，并通过回环地址认证；
- 聊天正文放在 HTTP 请求体中，不出现在进程参数或 QRobot 默认日志中；
- 使用无工具、无状态生成，不授予群友任何电脑权限；
- QRobot 自行管理有限的短期上下文和结构化长期记忆；
- 当前不通过该接口传图片，收到图片时会如实提示无法读取；
- 适合 QRobot 与 OpenCode 在同一宿主机运行的情况。

`opencode_transport` 推荐保持 `http`。兼容模式 `auto` 在无法发现服务登记文件时会
回退到 CLI；`cli` 强制使用 CLI，但请求正文会短暂出现在当前用户可见的进程参数中。
若使用非标准登记路径，可配置 `opencode_service_file`。QRobot 会拒绝权限过宽、非当前
用户所有或指向非回环地址的服务登记文件。

> Docker 容器中的 `127.0.0.1` 不是宿主机，且不应随意把完整 OpenCode 服务暴露给容器或公网。因此复用本机 OpenCode 登录态时，推荐让 QRobot 直接运行在宿主机，仅让 NapCat 使用 Docker。

### 2. OpenAI 官方 API

这会使用 API 平台的独立账单，不自动使用 ChatGPT 订阅额度。

`.env`：

```env
QROBOT_LLM_BACKEND=openai
OPENAI_API_KEY=你的_OpenAI_API_Key
```

`qrobot.json`：

```json
{
  "llm_backend": "openai",
  "openai_model": "gpt-5.6-sol",
  "openai_reasoning_effort": "medium",
  "openai_max_output_tokens": 1200
}
```

测试：

```bash
./run.sh test-openai
```

### 3. GitHub Copilot SDK

```json
{
  "llm_backend": "copilot",
  "copilot_model": "gpt-5",
  "copilot_reasoning_effort": "medium"
}
```

安装运行时后，通过官方 Copilot 工具完成登录，或在无交互环境中设置：

```env
COPILOT_GITHUB_TOKEN=你的短期Token
```

```bash
./run.sh test-copilot
```

Copilot runtime 在 QRobot 进程中复用；每轮创建无工具会话，不向群聊开放内置文件或终端工具。图片以本地临时附件传入。

### 4. DeepSeek API

`.env`：

```env
QROBOT_LLM_BACKEND=deepseek
DEEPSEEK_API_KEY=你的_DeepSeek_API_Key
```

`qrobot.json`：

```json
{
  "llm_backend": "deepseek",
  "deepseek_model": "deepseek-chat",
  "deepseek_base_url": "https://api.deepseek.com"
}
```

```bash
./run.sh test-deepseek
```

## OneBot 群聊模式

### 配置 QRobot

```bash
cp config.onebot.example.json qrobot.json
```

至少填写允许使用机器人的群号：

```json
{
  "qq_backend": "onebot",
  "onebot_ws_url": "ws://127.0.0.1:3001",
  "onebot_group_ids": ["123456789"],
  "onebot_require_group_allowlist": true,
  "trigger_mode": "mention_or_prefix",
  "dry_run": true
}
```

如果 NapCat 配置了 Access Token，在 `.env` 中设置：

```env
ONEBOT_ACCESS_TOKEN=换成你的令牌
```

默认触发方式：

```text
@机器人 你好
/ai 你好
```

`trigger_mode` 可选：

| 值 | 行为 |
|---|---|
| `prefix` | 只响应 `/ai ` 前缀 |
| `mention` | 只响应 @机器人 |
| `mention_or_prefix` | 两者任一，群聊推荐 |
| `passive` | 响应白名单群中的所有消息，风险较高 |

先以 dry-run 监听：

```bash
./run.sh run
```

日志出现“已连接 OneBot”“收到触发消息”和“dry-run 回复”后，再实际发送：

```bash
./run.sh run --send
```

### 私聊

私聊默认关闭。开启时仍建议强制白名单：

```json
{
  "respond_to_private": true,
  "onebot_require_private_allowlist": true,
  "onebot_private_user_ids": ["允许的QQ号"]
}
```

### NapCat Docker 示例

仓库中的 `docker-compose.onebot.example.yml` 同时提供 NapCat 与 QRobot 容器示例。

```bash
cp config.docker.example.json qrobot.json
cp .env.example .env
```

Docker 示例默认使用 DeepSeek，因此还需在 `.env` 中填写
`DEEPSEEK_API_KEY`。也可以改为使用独立 API Key 的 OpenAI，或在容器中单独配置
Copilot 登录。模板已经使用容器内地址 `ws://napcat:3001`。

然后启动：

```bash
mkdir -p data/qrobot data/napcat/config data/napcat/qq
NAPCAT_UID=$(id -u) NAPCAT_GID=$(id -g) \
QROBOT_UID=$(id -u) QROBOT_GID=$(id -g) \
  docker compose -f docker-compose.onebot.example.yml up -d --build
docker compose -f docker-compose.onebot.example.yml logs -f
```

1. 打开 `http://127.0.0.1:6099/webui`；
2. WebUI Token 可从 NapCat 日志中取得；
3. 完成 QQ 扫码/设备验证；
4. 在 NapCat 中启用 OneBot 11 正向 WebSocket 服务器并监听容器内 `0.0.0.0:3001`；
5. 保持群白名单与 `dry_run=true`，验证后再发送。

NapCat WebUI 的 Websocket 服务器默认只监听 `127.0.0.1`。Docker 模式必须改为
`0.0.0.0`；当前 WebUI 在非回环地址上还会要求配置 Token。建议设置如下：

```text
类型：Websocket 服务器
启用：是
Host：0.0.0.0
Port：3001
消息格式：Array
上报自身消息：否
Token：随机长字符串（不要留空）
```

同一个 Token 只放进 QRobot 的 `.env`，不要写进 JSON 或提交到 Git：

```env
ONEBOT_ACCESS_TOKEN=与NapCat中完全相同的Token
```

NapCat 的配置和 QQ 登录数据分别保存在：

```text
data/napcat/config
data/napcat/qq
```

这些都是私人数据，不要上传。

> Compose 内的 QRobot 默认适合 `deepseek`、`openai` 或 `copilot` 后端。`opencode` 后端推荐直接在宿主机运行。

## 官方 QQ 桌面模式

桌面模式目前针对 Linux + X11。QQ 必须由用户在官方客户端中登录。

```bash
cp config.example.json qrobot.json
./run.sh login
./run.sh doctor
./run.sh run
```

确认通知解析与生成结果正确后：

```bash
./run.sh run --send
```

发送过程：

1. `dbus-monitor` 读取 `org.freedesktop.Notifications.Notify`；
2. 只接收配置中 `notification_app_names` 对应的通知；
3. `xdotool` 激活 QQ、点击搜索框并选择会话；
4. `xclip` 临时写入文本，发送后尽量恢复原剪贴板。

默认坐标基于约 960×640 的 QQ 主窗口，并使用窗口相对坐标：

```json
{
  "reply": {
    "search_offset": [150, 66],
    "composer_offset": [690, 575],
    "search_delay_seconds": 0.8,
    "conversation_delay_seconds": 0.8
  }
}
```

QQ 版本、主题、缩放和布局变化都可能导致坐标失效。Wayland 通常会阻止 `xdotool`，建议登录 X11 会话。

## 长期记忆

默认数据库为 `data/memory.sqlite3`。数据按会话和群友隔离，不保存完整消息历史。

群内命令：

```text
@机器人 记住：我喜欢猫
@机器人 忘记：我喜欢猫
@机器人 记忆
@机器人 清空我的记忆
```

还会保守捕获少量明确自述，例如：

```text
我叫小明
我喜欢打羽毛球
我不喜欢香菜
我最近在做 FPGA 项目
```

管理命令：

```bash
./run.sh memory list --scope group:123456789 --user 10001
./run.sh memory forget --scope group:123456789 --user 10001 --query 喜欢猫
./run.sh memory clear --scope group:123456789 --user 10001 --yes
```

## 图片

- 每条消息默认最多下载 4 张；
- 单张默认最大 12 MiB；
- 支持 JPEG、PNG、GIF、WebP、BMP；
- 默认拒绝内网、本机、带 URL 凭据的地址，降低 SSRF 风险；
- 临时文件权限为 `0600`，生成结束后删除；
- OneBot 只给出本地文件路径而没有 HTTP(S) URL 时会退化为 `[图片]`；
- OpenAI API 与 Copilot 后端可读取图片；OpenCode 与 DeepSeek 后端会明确说明无法读取。

如果网关只在可信内网提供图片，可显式设置：

```json
{
  "image_allow_private_hosts": true
}
```

这会降低网络隔离能力，不建议在不受信任的群中启用。

## 联网搜索

```json
{
  "web_search_enabled": true,
  "web_search_max_results": 5,
  "web_search_timeout_seconds": 15
}
```

遇到“今天、最新、新闻、天气、价格、版本”等问题时自动搜索。公共搜索源可能超时、限流或包含错误内容；网页内容会被标记为不可信资料，不能改变系统规则。

开启后，触发搜索的提问会发送给公开搜索服务。对隐私敏感的群建议关闭该功能。

## 限流与隐私配置

常用配置：

| 配置 | 默认值 | 说明 |
|---|---:|---|
| `dry_run` | `true` | 只打印，不发送 |
| `max_input_chars` | `6000` | 单条触发文本上限 |
| `max_reply_chars` | `4000` | 模型回复总上限 |
| `onebot_message_chunk_chars` | `1500` | QQ 分段发送长度 |
| `history_max_messages` | `16` | 每个会话内存中的短期消息数 |
| `memory_max_records_per_user` | `200` | 每名群友在每个会话中的记忆上限 |
| `max_parallel_requests` | `3` | 全局模型并发上限 |
| `event_queue_size` | `200` | OneBot 待处理消息上限 |
| `max_message_age_seconds` | `300` | 断线重连或积压后不回复过期消息 |
| `rate_limit_messages` | `8` | 每个群友在窗口内最多触发数 |
| `rate_limit_window_seconds` | `60` | 限流窗口秒数 |
| `log_message_content` | `false` | 是否把消息正文写入日志 |

日志默认写入 `data/qrobot.log`，会轮转保留三份。默认日志只记录会话 ID、用户 ID、字符数和状态，不记录聊天正文。

## 常用命令

```bash
# 检查配置、依赖、白名单、OpenCode 登录和模型
./run.sh doctor

# 输出生效配置，不含凭据
./run.sh show-config

# 不接触 QQ，走完整触发、记忆和模型链路
./run.sh simulate --conversation self-test --text '/ai 请只回复“连接成功”'

# 测试当前后端
./run.sh test-backend

# 分别测试后端
./run.sh test-opencode
./run.sh test-openai
./run.sh test-copilot
./run.sh test-deepseek

# 监听但不发送
./run.sh run --dry-run

# 实际发送
./run.sh run --send
```

## 宿主机长期运行

确认 dry-run 和实际发送都正常后，可使用仓库中的 systemd **用户服务**模板：

```bash
mkdir -p ~/.config/systemd/user
cp deploy/qrobot.service.example ~/.config/systemd/user/qrobot.service
systemctl --user daemon-reload
systemctl --user enable --now qrobot
journalctl --user -u qrobot -f
```

模板出于安全考虑仍使用 `run --dry-run`。确认无误后，将服务文件最后的
`--dry-run` 改为 `--send`，再执行：

```bash
systemctl --user daemon-reload
systemctl --user restart qrobot
```

桌面模式还需要在图形会话中执行模板注释里的 `import-environment`；OneBot 模式不需要
`DISPLAY`。不要用 root 运行 QRobot，也不要把 OpenCode 服务监听到公网。

## 测试与开发

```bash
source .venv/bin/activate
pip install -e '.[dev,onebot]'
pytest
ruff check src tests
```

测试不会登录 QQ，也不会调用真实模型 API。

## 故障排查

### OpenCode 模型不可用

```bash
opencode service status
opencode api get /api/model
./run.sh doctor
```

确认 `opencode_model` 是 `provider/model`，并在 OpenCode 中完成对应 Provider 登录。QRobot 不会读取或打印 OAuth Token。

### OneBot 已连接但不回复

依次检查：

1. 群号是否在 `onebot_group_ids`；
2. 是否使用了正确触发方式；
3. NapCat 的 WebSocket 是“正向服务器”而不是反向客户端；
4. Access Token 是否一致；
5. 是否仍在 `dry_run`；
6. 日志是否出现限流或队列已满。

如果 `doctor` 显示 `InvalidMessage`、连接被重置或 WebSocket 握手失败，通常表示端口虽已
映射，但 NapCat 的 OneBot 11 **正向 WebSocket 服务器**尚未启用，或只监听了容器内
`127.0.0.1`。请在 WebUI 中让它监听 `0.0.0.0:3001`，保存后重启 NapCat 再检查。

### 桌面模式收不到通知

- QQ 会话当前处于打开状态时，桌面可能不发通知；
- 检查系统是否允许 QQ 通知；
- 手动运行 `dbus-monitor "interface='org.freedesktop.Notifications',member='Notify'"`；
- 确认是 X11 而不是 Wayland；
- 检查 `notification_app_names` 是否与实际通知应用名一致。

### QQ 搜索或发送坐标不对

调整 `reply.search_offset` 与 `reply.composer_offset`。坐标相对于 QQ 窗口，而不是整个屏幕。

## 项目结构

```text
src/qrobot/
├── backend.py            后端接口、提示构造和后端工厂
├── opencode_backend.py   OpenCode V2 无工具生成适配器
├── openai_backend.py     OpenAI Responses API
├── copilot_backend.py    GitHub Copilot SDK
├── deepseek.py           DeepSeek Chat Completions
├── config.py             JSON/.env 配置、校验与脱敏
├── onebot.py             OneBot 11 WebSocket 收发
├── notifications.py      D-Bus 通知解析与监听
├── qq.py                 Linux/X11 官方 QQ 自动化
├── runner.py             白名单、触发、限流、上下文和编排
├── memory.py             SQLite 结构化长期记忆
├── images.py             安全图片下载与格式检查
├── search.py             DDGS 搜索
├── doctor.py             环境诊断
└── cli.py                命令行入口
```

## 迁移与备份

建议只迁移：

- 项目代码；
- 脱敏后的 `qrobot.json`；
- 如确有需要，单独加密备份 `data/memory.sqlite3`。

不要复制或提交：

- `.env`；
- OpenCode/Copilot OAuth 数据；
- `data/napcat/qq` 登录数据；
- 未经群友同意的私人记忆。

每台机器都应重新完成 QQ 和模型服务登录，并先运行 `doctor` 与 dry-run。
