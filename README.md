# QRobot

一个**独立、可迁移的电脑端 QQ + LLM 聊天实验项目**。

它不调用 QQ 私有机器人协议，也不修改 QQ 数据库，而是通过已安装的官方 QQ 客户端工作：

```text
官方 QQ 客户端（手动登录）
        │
        ├─ 桌面通知：发现新消息
        └─ 窗口自动化：打开会话、粘贴并发送回复
                         │
                         ▼
                   LLM backend
                 /             \
        DeepSeek API       GitHub Copilot SDK
```

当前已实现并针对本机验证的适配器是：

- Ubuntu/Linux + X11
- 官方 Linux QQ 3.2.34（Electron）
- `dbus-monitor` 读取 QQ 桌面通知
- `xdotool` + `xclip` 在 QQ 窗口中搜索会话和发送文本
- OneBot 11 forward WebSocket 收发客户端（已用标准事件/API 和本地假 WebSocket 测试）

项目的核心配置、DeepSeek 客户端、通知解析和会话记忆不依赖 Linux。Windows 适配器接口已经独立出来；Windows 的 QQ UI/通知实现需要在目标 Windows 机器上按实际 QQ 版本校准，不能承诺和 Linux 坐标完全相同。

## 重要边界

1. **每台电脑都要在官方 QQ 客户端手动登录**。项目不保存 QQ 密码、不复制 QQ 登录态、不绕过扫码/验证码/设备验证。
2. 默认只处理以 `/ai ` 开头的消息，避免误回复全部聊天；建议先只配置自己的测试号/测试会话。
3. 默认 `dry_run=true`，只调用所选模型后端并在终端打印回复，不发送。确认通知和搜索坐标正确后，使用 `--send` 或把配置改为 `dry_run=false`。
4. 这是桌面 UI 自动化，QQ 升级、主题、窗口缩放或布局变化都可能使发送坐标失效。项目不尝试规避 QQ 风控或检测。
5. 使用时应让聊天对象知道这是自动回复账号，并遵守 QQ、GitHub Copilot 及模型服务条款；不要用于群发、骚扰或绕过平台限制。

## Linux 快速开始

系统要求：Python 3.10+（Copilot 后端为 3.11+）、X11 会话、官方 QQ、`xdotool`、`xclip`、`dbus-monitor`。

```bash
cd /home/vivo/Qrobot
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp config.example.json qrobot.json
cp .env.example .env
```

项目支持两种模型后端：

### 方案 1：GitHub Copilot 订阅（当前推荐）

Copilot 模式不需要 DeepSeek API Key。需要 Python 3.11+，并安装官方 SDK。若系统没有 Python 3.11，可以使用 Conda：

```bash
conda create -n qrobot-copilot python=3.11 pip -y
conda activate qrobot-copilot
pip install -e '.[onebot,copilot,search]'
python -m copilot download-runtime
```

然后在 `qrobot.json` 中设置：

```json
{
  "llm_backend": "copilot",
  "copilot_model": "gpt-5",
  "web_search_enabled": true,
  "web_search_max_results": 5
}
```

联网搜索通过 `ddgs` 聚合公开搜索源，不需要额外 API Key，但公共搜索源可能超时或限流。
遇到新闻、天气、价格、版本等可能变化的问题时，机器人会优先调用 `web_search`，再根据结果回答。

首次使用前，需要在官方 Copilot CLI 中完成 GitHub 登录。当前机器的 CLI 安装在用户 npm 目录中，如该目录不在 PATH 中，先执行：

```bash
export PATH="$HOME/.npm-global/bin:$PATH"
copilot login
```

按终端提示完成浏览器或设备码授权。SDK 会使用本机安全存储中的 Copilot 登录态；不要把 GitHub OAuth/PAT 填到 `DEEPSEEK_API_KEY` 中。

测试：

```bash
./run.sh test-copilot
```

### 方案 2：DeepSeek API

```bash
pip install -e .
```

在 `.env` 中填写官方 DeepSeek Key，并设置：

```env
DEEPSEEK_API_KEY=你的DeepSeek_API_Key
QROBOT_LLM_BACKEND=deepseek
```

不要把真实 key 写入 Git。

如果系统没有工具：

```bash
sudo apt install xdotool xclip dbus
```

### 检查环境

```bash
python -m qrobot doctor
```

检查 QQ 是否已登录/启动；如果未启动：

```bash
python -m qrobot login
```

登录动作仍然在官方 QQ 窗口中手动完成。

### 测试 DeepSeek

```bash
python -m qrobot test-deepseek
```

Copilot 模式：

```bash
python -m qrobot test-copilot
```

### 不碰 QQ 的解析测试

```bash
python -m qrobot simulate --conversation self-test --text '/ai 请只回复“连接成功”'
```

这会调用当前选定的模型后端，但不会发送到 QQ。

### 监听 QQ

先用默认安全模式观察通知：

```bash
python -m qrobot run
```

在另一个 QQ 账号向机器人账号发送：

```text
/ai 你好
```

确认终端能看到会话名、消息和生成结果后，再启用实际发送：

```bash
python -m qrobot run --send
```

## OneBot + Docker 模式（推荐的后台模式）

桌面模式会操作 QQ 窗口；OneBot 模式则通过 WebSocket 接收群消息，并通过
`send_group_msg` 直接发送回复，不需要打开聊天窗口、搜索群名或使用剪贴板。

当前项目实现的是 **OneBot 11 forward WebSocket 客户端**。QQ 侧可以使用任何
兼容 OneBot 11 的网关；仓库提供了 NapCat-Docker 的示例 Compose，但它属于非官方
QQ 集成，使用前请单独评估账号和服务条款风险。

先复制 OneBot 配置：

```bash
cp config.onebot.example.json qrobot.json
```

修改其中的群号：

```json
{
  "qq_backend": "onebot",
  "onebot_ws_url": "ws://127.0.0.1:3001",
  "onebot_group_ids": ["你的群号"],
  "onebot_require_group_allowlist": true,
  "llm_backend": "copilot",
  "dry_run": true
}
```

本机直接连接网关时：

```bash
pip install -e '.[onebot]'
./run.sh doctor
./run.sh run
```

确认收到群消息和生成回复后再改成：

```bash
./run.sh run --send
```

### 使用 Docker Compose

```bash
cp config.onebot.example.json qrobot.json
cp .env.example .env
```

在 `.env` 中至少设置一个 WebUI Token：

```env
NAPCAT_WEBUI_TOKEN=换成随机长字符串
```

启动：

```bash
docker compose -f docker-compose.onebot.example.yml up -d --build
docker compose -f docker-compose.onebot.example.yml logs -f qrobot
```

第一次打开 `http://127.0.0.1:6099/webui`，在网关中完成 QQ 账号登录和 OneBot
配置。`data/napcat-qq` 会保存网关的 QQ 数据，容器重启不会自动丢失登录态，但
QQ 仍可能要求重新验证。

OneBot 示例配置默认支持两种触发方式：

```text
@机器人 你好
/ai 你好
```

`trigger_mode` 可选：

- `prefix`：只响应 `/ai `；
- `mention`：只响应 @机器人；
- `mention_or_prefix`：两者都响应，群聊推荐；
- `passive`：所有群消息都进入模型，不建议直接开启。

OneBot 会把发言者昵称加入模型上下文。想调整“活人感”时，优先修改
`system_prompt`，控制口语化程度、回复长度和人设，不建议用大量随机口头禅来伪造真人。

当前还会根据本轮消息做语气镜像：友好消息保持熟人式友好，玩梗消息顺着接，明显挑衅
则可以更直接地回击事情和说法。Copilot runtime 会在 QRobot 进程中复用，第一条消息
通常较慢，后续消息不再重复启动 Copilot CLI。

OneBot 图片消息会提取图片 URL，下载到临时文件后作为 Copilot 图片附件发送给模型。
当前限制为每条消息最多 4 张图、单图最多 12 MB；如果网关只提供本地文件路径而没有 URL，
机器人会退化为文字提示 `[图片]`。

### 长期记忆

QRobot 现在会在 `data/memory.sqlite3` 保存结构化长期记忆，而不是保存完整群聊原文。
记忆按群和群友隔离，只在相关话题中注入模型上下文。

可以让机器人显式记住：

```text
@小Q 记住：我喜欢猫
@小Q 记住：我最近在做 FPGA 项目
```

也可以删除或查看：

```text
@小Q 忘记：我喜欢猫
@小Q 记忆
```

默认还会捕获少量明确表达，例如“我叫……”“我喜欢……”“我不喜欢……”。密码、Token、
手机号、住址等敏感模式会被拒绝写入。长期记忆是本地文件，迁移项目时请把它视为私人数据，
不要上传或提交到 Git。

Copilot 在容器中需要自己的认证来源。可以在 Compose 运行时传入
`COPILOT_GITHUB_TOKEN`，不要把它写进 Dockerfile 或镜像；也可以把模型后端切换成
DeepSeek 并只通过 `DEEPSEEK_API_KEY` 注入。

OneBot 模式下建议保持：

```json
{
  "onebot_require_group_allowlist": true,
  "onebot_group_ids": ["测试群号"],
  "dry_run": true
}
```

确认日志中出现 `已连接 OneBot`、`收到触发消息 [group:群号]` 和
`dry-run 回复` 后，再打开实际发送。

也可以在 `qrobot.json` 中设置：

```json
{
  "dry_run": false,
  "require_allowlist": true,
  "allowed_conversations": ["你的测试 QQ 昵称或群名"]
}
```

## QQ 窗口坐标

当前 Linux QQ 的默认布局是 960×640 主窗口：搜索框大约在窗口相对坐标 `(150, 66)`，消息编辑框大约在 `(690, 575)`。项目使用**窗口相对坐标**，不是屏幕绝对坐标，因此换分辨率仍有机会工作。

如果 QQ 主题/缩放导致发送失败，修改 `qrobot.json` 的 `reply.search_offset` 和 `reply.composer_offset`。后续可以再加交互式校准命令。

## 跨电脑迁移

可迁移的是项目和配置模板，不是 QQ 的登录凭据：

```text
目标电脑安装 Python + 官方 QQ + 对应桌面自动化依赖
复制 Qrobot 项目
复制 qrobot.json（不要把 key 写进去）
按所选后端设置 `DEEPSEEK_API_KEY`，或完成本机 Copilot 登录
启动官方 QQ，手动扫码/验证相同账号
运行 python -m qrobot doctor
运行 python -m qrobot run --send
```

Linux 机器之间的 `qrobot.json` 可以直接复用；Windows 需要使用 Windows 版 QQ 的适配器和自己的 UI 坐标/控件校准。多台电脑同时登录同一个 QQ 账号是否保持在线由 QQ 的设备策略决定，项目无法保证平台一定不下线其他设备。

## 目录

- `src/qrobot/config.py`：配置与环境变量
- `src/qrobot/deepseek.py`：DeepSeek OpenAI-compatible API 客户端
- `src/qrobot/copilot_backend.py`：GitHub Copilot SDK 后端
- `src/qrobot/onebot.py`：OneBot 11 WebSocket 收发和群白名单
- `src/qrobot/notifications.py`：D-Bus QQ 通知解析
- `src/qrobot/qq.py`：官方 QQ 启动、窗口发现和 Linux UI 自动化
- `src/qrobot/runner.py`：触发过滤、上下文记忆和回复编排
- `src/qrobot/cli.py`：命令行入口
