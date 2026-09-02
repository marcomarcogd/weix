# Weix — Windows 微信 AI 自动回复

Weix 是一个运行在用户自己电脑上的微信 AI 自动回复项目。它从本机微信数据库读取新消息，通过大模型、规则或工作流生成回复，再由已登录的微信客户端完成发送。

当前版本以 **Windows 微信 4.x** 为主要维护与实测环境，并提供 Vue 3 管理后台。Windows 默认使用 UI Automation（UIA）发送，优先尝试后台操作，不移动物理鼠标；macOS 保留 AppleScript 发送支持。

> 微信客户端自动化、进程内存读取和自动回复均存在非零账号、隐私与合规风险。请先使用非主账号、小范围白名单和低频消息测试。微信升级后，UI 结构、数据库格式或密钥位置都可能变化。

## 项目介绍

如果你经常需要回答相似问题、维护多个微信群、在忙碌时保持及时回复，或者希望让 AI 按自己的表达习惯协助处理微信消息，Weix 可以把这些重复工作集中到一套可管理的自动回复流程中。

它不要求把微信账号交给第三方托管，也不需要改用另一个聊天客户端。微信仍在用户自己的电脑上运行，用户可以决定监听哪些联系人和群聊、什么情况自动回复、使用 AI 还是固定规则，以及何时暂停整个系统。

### 适合谁

| 用户 | 典型用途 |
| --- | --- |
| 个人用户 | 在忙碌时处理重复咨询，用自定义 Prompt、上下文记忆和本人 Skill 保持熟悉的回复风格 |
| 社群管理者 | 为不同群聊配置独立规则、白名单和固定回复，减少日常答疑与重复通知 |
| 小团队客服 | 组合常见问题、AI 对话、工作流、消息转发和统计，统一管理微信咨询过程 |

### 为什么选择 Weix

- **尽量不打扰当前工作**：Windows 后台 UIA 优先，不占用真实鼠标；微信最小化且控件可验证时也可以完成会话切换和发送。
- **本机数据由自己保管**：微信数据库、解密密钥、配置和日志保存在用户自己的电脑上，不需要上传到 Weix 的集中式服务。
- **AI 与确定性规则可以组合**：开放问题交给模型，固定问题使用关键词、正则或专属群规则，复杂过程交给工作流。
- **控制范围足够细**：私聊、群聊、白名单、按群规则和总开关可以分别配置，先从一个测试对象开始，再逐步扩大范围。
- **每次发送都有迹可查**：管理后台提供消息日志、尝试 ID、UI 验证和数据库验证；结果不明确时停止补发，降低重复消息风险。
- **能力可以继续扩展**：模型、Prompt、本人 Skill、模板、工具、转发规则和工作流都可以按实际场景调整。

> **数据边界说明**：微信数据库和解密密钥保留在本机；如果配置的是云端大模型，生成回复所需的消息内容会按模型服务的 API 规则发送给该服务。请根据数据敏感程度选择模型并阅读其隐私政策。

下面的核心能力共同支撑这些用户场景。

## 核心能力

- 从微信本地数据库监听私聊和群聊消息
- 接入 DeepSeek、OpenAI 兼容接口和其他 LangChain 模型
- 多轮上下文、长期记忆、个性化 Prompt 和本人 Skill
- 关键词、正则、意图规则及按群独立配置的回复规则
- 工作流、消息模板、自动转发和定时任务
- 发言排行、时段热力图、关键词和 AI 摘要
- Windows 微信账号与进程 PID 绑定，避免操作错误实例
- Windows 后台 UIA 会话切换、文本写入和发送后双重验证
- Vue 3 管理后台、只读 UIA 诊断和发送日志

## 工作原理

~~~text
微信客户端
   │
   ├── 本地数据库 ──只读──▶ 解密与消息监听
   │                              │
   │                              ▼
   │                       规则 / 工作流 / AI
   │                              │
   │                              ▼
   └◀── Windows UIA / macOS AppleScript ── 回复结果
~~~

### 收消息

日常监听只读取微信本地数据库。首次获取数据库密钥时：

- Windows 使用 <code>ReadProcessMemory</code> 读取微信进程内存
- macOS 使用 <code>mach_vm_read_overwrite</code>

提取出的密钥保存在本机 <code>data/all_keys.json</code>，不应提交到 Git 或发送给他人。

### Windows 发消息

Windows 默认使用 UIA，并将 UIA 窗口绑定到管理后台所选账号对应的微信 PID。发送前会确认目标会话、会话标题和输入框；后台路径可使用 <code>PostMessage</code> 驱动已识别的窗口控件，不产生物理鼠标移动。

当发送动作已经发生、草稿已经清空或结果处于等待验证状态时，系统不会盲目重试。最终状态综合 UI 状态和数据库回读判断，避免因为单一 API 返回值不准确而重复发送。

## 平台支持

| 维度 | Windows | macOS |
| --- | --- | --- |
| 主要实测版本 | 微信 4.1.13.12 | 微信 4.x |
| 消息读取 | 本地 SQLite / WCDB 数据库 | 本地 SQLite / WCDB 数据库 |
| 密钥提取 | Win32 进程内存只读 | Mach VM 只读 |
| 默认发送 | UIA，后台优先 | AppleScript |
| 真实鼠标 | UIA 默认不移动；鼠标兜底默认关闭 | 不适用 |
| 最小化发送 | 绑定和 UIA 控件完整时支持；否则停止 | 不保证 |
| 管理员权限 | 建议使用，密钥提取通常需要 | 首次密钥提取通常需要 sudo |

“主要实测版本”不代表其他版本一定不可用。微信更新后应先重新执行 UIA 检测，再开启自动回复。

## Windows 快速开始

### 1. 准备环境

- Windows 10 或 Windows 11
- PowerShell 7 或更高版本
- Python 3.10 或更高版本
- Node.js 20 或更高版本
- 已登录的 Windows 微信客户端
- 一个可用的大模型 API Key

建议使用管理员权限启动 Weix，以便读取微信进程内存并提取数据库密钥。

### 2. 克隆项目

~~~powershell
git clone https://github.com/marcomarcogd/weix.git
Set-Location .\weix
~~~

### 3. 创建本地配置

~~~powershell
Copy-Item .env.example .env
Copy-Item config\config.example.yaml config\config.yaml
~~~

编辑 <code>.env</code>，至少设置：

~~~dotenv
DEEPSEEK_API_KEY=请替换为实际APIKey
JWT_SECRET=请替换为随机长字符串
ADMIN_PASSWORD=请替换为管理后台密码
~~~

如使用其他模型服务，请同时修改 <code>config/config.yaml</code> 中的 <code>ai.provider</code>、<code>ai.base_url</code> 和 <code>ai.model</code>。

<code>.env</code>、<code>config/config.yaml</code>、<code>data/</code> 和 <code>logs/</code> 都包含本机状态或敏感信息，不应提交。

### 4. 安装依赖

在 PowerShell 7 中运行：

~~~powershell
.\scripts\setup.bat
~~~

脚本会创建 <code>venv</code>、安装后端与前端依赖，并尝试预下载 AI 模型。

### 5. 启动服务

~~~powershell
.\scripts\start.bat
~~~

启动成功后：

- 管理后台：http://127.0.0.1:5173
- 后端 API：http://127.0.0.1:8000
- API 文档：http://127.0.0.1:8000/docs

关闭 <code>Weix-Backend</code> 和 <code>Weix-Frontend</code> 窗口可停止脚本启动的服务。

### 6. 可选：构建 WeixManager

WeixManager 可以统一启动、停止、重启前后端，查看日志并打开 UIA 检测。仓库源码不直接跟踪生成的 EXE，需要时可自行构建：

~~~powershell
pwsh -File .\scripts\build_manager.ps1
.\dist\WeixManager.exe
~~~

生成的管理器只负责管理当前项目服务，不打包 <code>.env</code>、<code>config/config.yaml</code>、数据库密钥或日志。

### 7. 选择微信账号并检测 UIA

1. 保持目标微信账号已登录，并打开一次微信主窗口。
2. 进入管理后台的“聊天配置”页面。
3. 选择需要自动回复的微信账号。
4. 使用 WeixManager 或启动脚本重启服务，使账号与 PID 重新绑定。
5. 点击“检测微信 UIA”，确认主窗口、搜索框、会话列表、输入框和发送按钮均已找到。
6. 配置少量私聊或群聊白名单，再开启自动回复。

切换账号后、服务重启前，自动发送会保持静默，避免使用旧 PID 发送。

## Windows 发送模式

<code>config/config.yaml</code> 中的关键默认配置如下：

~~~yaml
windows_sender:
  method: uia
  send_mode: auto
  background_post_message: true
  allow_foreground_activation: true
  allow_mouse_fallback: false
  require_ui_verify: true
  hot_activate_accessibility: false

monitor:
  poll_interval: 0.25
  lookback_seconds: 0
~~~

| <code>send_mode</code> | 行为 |
| --- | --- |
| <code>auto</code> | 先检测后台能力并优先后台发送；只有 <code>allow_foreground_activation: true</code> 时才允许转到前台 UIA |
| <code>background</code> | 只允许后台 UIA；后台条件不满足就停止，不切前台 |
| <code>foreground</code> | 使用前台 UIA，可能激活或恢复微信窗口 |

其他安全相关配置：

- <code>background_post_message: true</code>：允许对已识别的窗口控件投递窗口消息，不等同于物理鼠标点击
- <code>allow_mouse_fallback: false</code>：UIA 失败后不使用 pyautogui 坐标兜底
- <code>require_ui_verify: true</code>：发送后必须进行 UI 状态验证
- <code>lookback_seconds: 0</code>：启动后不回放旧消息，降低重启后重复回复风险
- <code>poll_interval: 0.25</code>：消息监听轮询间隔为 0.25 秒

在微信 4.1.13.12 的一次实机测试中，非当前会话的后台切换耗时约为 0.25–0.27 秒；这是特定机器和会话状态下的观测值，不是性能保证。

## UIA 诊断与热激活

### 只读诊断

“检测微信 UIA”只读取所选微信实例的 UIA 控件树，不输入文字、不切换聊天、不发送消息。检测失败时先确认：

1. 微信已登录且主窗口至少手动打开过一次。
2. 管理后台选择的是正确账号。
3. 选择账号后已经重启 Weix。
4. Windows“讲述人”已开启再关闭一次，然后重新检测。

### 单字节热激活

部分微信 4.x 版本可能没有主动暴露完整 UIA 控件树。项目提供可选的单字节热激活：

~~~yaml
windows_sender:
  hot_activate_accessibility: false
~~~

此功能默认关闭。开启或手动执行时会要求确认，并只在模块、地址、绑定 PID、原始值和写入回读都符合预期时，修改所选微信进程中 <code>Weixin.dll</code> 的一个运行时 gate 字节。任何校验失败都会停止，不会继续发送。

这不是“零进程写入”方案，也不能承诺零封号风险。只有在只读诊断和讲述人方式均不能恢复 UIA、并且你理解风险时才考虑启用。

## 自动回复配置

新配置默认关闭自动回复：

~~~yaml
auto_reply:
  enabled: false
  private_chat_mode: whitelist
  private_whitelist: []
  group_chat_mode: whitelist
  group_whitelist: []
  group_reply_rules: []
~~~

建议配置顺序：

1. 先选择并绑定微信账号。
2. 添加一个测试联系人或测试群到白名单。
3. 配置 AI 或一条确定性的关键词回复规则。
4. 完成 UIA 只读检测。
5. 开启自动回复并发送一条全新的测试消息。
6. 在“消息日志”中核对目标、尝试 ID、UI 验证和数据库验证状态。

当 <code>lookback_seconds</code> 为 0 时，服务不会回复启动前已经存在的旧消息。测试应在服务完全启动后重新发送。

## 验收工具

验收脚本会把 JSON 结果写入 <code>logs/</code>。该目录不应提交。

### 服务与 UIA 检查

<code>windows_uia_acceptance.py</code> 默认只登录本地服务并检查平台状态和 UIA，不向微信发送消息：

~~~powershell
$env:WEIX_ADMIN_PASSWORD = "请替换为管理后台密码"
.\venv\Scripts\python.exe .\scripts\windows_uia_acceptance.py
~~~

只有显式添加 <code>--live</code> 才会发送测试消息。私聊和群聊都必须同时提供名称与稳定 ID：

~~~powershell
.\venv\Scripts\python.exe .\scripts\windows_uia_acceptance.py --live --private-name "测试联系人名称" --private-id "wxid_placeholder" --group-name "测试群名称" --group-id "chatroom_placeholder" --count 1
~~~

### 隔离后台发送探测

<code>windows_background_uia_probe.py</code> 不修改生产配置，但会真实发送 1–4 条测试消息。它要求显式提供已确认属于所选账号的微信主进程 PID；首次失败后立即停止，不切前台、不使用鼠标补发：

~~~powershell
$wechatPid = Read-Host "请输入已确认属于所选账号的微信主进程 PID"
.\venv\Scripts\python.exe .\scripts\windows_background_uia_probe.py --receiver "测试联系人名称" --target-id "wxid_placeholder" --pid ([int]$wechatPid) --count 1
~~~

不要把验收脚本用于未经对方同意的账号或群聊。

## 常见问题

### 服务启动后没有自动回复

- 确认 <code>auto_reply.enabled</code> 已开启
- 确认目标联系人或群聊在白名单内
- 确认消息是在服务启动完成后新发送的
- 确认后台已选择正确微信账号并完成重启
- 查看“消息日志”和后端日志中的失败阶段，而不是直接重复发送

### UIA 检测不到输入框或发送按钮

先打开微信主窗口并保持正常布局，重新检测；仍不可用时，开启再关闭一次 Windows 讲述人。单字节热激活应作为需要明确授权的最后选项。

### 微信最小化后无法切换聊天

后台发送需要绑定窗口、会话列表、输入框和发送控件均可验证。条件不足时会失败关闭，这是安全行为。不要通过固定搜索结果坐标或自动重复点击绕过。

### API 返回失败，但微信里似乎已经出现消息

不要立即补发。检查消息日志中的 <code>attempt_id</code>、<code>action_performed</code>、<code>draft_cleared</code>、UI 验证和数据库验证。只要发送动作可能已经发生，系统就会禁止自动重试。

### 微信升级后突然失效

先停止自动回复，重新选择账号、重启服务并执行 UIA 检测。若 UIA 结构、数据库格式或密钥定位发生变化，需要等待项目适配，不应使用不受验证的坐标或循环重试。

## macOS

macOS 仍使用 AppleScript 发送，快速开始如下：

~~~bash
cp config/config.example.yaml config/config.yaml
cp .env.example .env
bash scripts/setup.sh
sudo bash scripts/start.sh
~~~

首次运行需要授予终端辅助功能权限。macOS 路径没有本 README 所述的 Windows UIA 后台保证。

## 管理后台

主要页面包括：

- 仪表盘与平台状态
- 聊天配置、账号选择、白名单和 UIA 检测
- 自动回复规则和按群回复规则
- AI 配置、本人 Skill 和知识库
- 消息日志、统计报告和定时任务
- 模板、工作流与转发规则

## 技术栈

- 后端：FastAPI、SQLAlchemy async、aiosqlite
- AI：LangChain、LangGraph
- 前端：Vue 3、Element Plus、Pinia、ECharts
- Windows 自动化：UI Automation、Win32 API、wechatauto-replica
- 数据库解密：pycryptodome、SQLCipher 4 兼容逻辑
- 调度：APScheduler

## 目录结构

~~~text
weix/
├── backend/
│   ├── app/
│   │   ├── core/       # 数据库读取、监听、Windows UIA 与平台适配
│   │   ├── ai/         # Agent、模型、工具、记忆与知识库
│   │   ├── workflow/   # 规则、模板、工作流与转发
│   │   ├── api/        # REST API
│   │   ├── services/   # 业务服务
│   │   └── models/     # ORM 与数据模型
│   └── tests/
├── frontend/           # Vue 3 管理后台
├── config/             # 配置模板
├── scripts/            # 安装、启动、构建、诊断和验收脚本
├── data/               # 本机数据库与密钥，不提交
└── logs/               # 本机运行及验收日志，不提交
~~~

## 风险与边界

- 本项目不是微信官方工具，也不保证兼容未来微信版本。
- 自动化存在被限制功能、风控或封号的可能。
- 数据库密钥、聊天记录、API Key 和管理员密码都属于敏感信息。
- 默认白名单、频率控制、发送验证和失败关闭只能降低风险，不能消除风险。
- 请遵守适用法律、平台条款和聊天参与者的隐私要求。

## 第三方声明

仓库当前未包含主项目的 <code>LICENSE</code> 文件，因此本 README 不对项目整体许可证作额外声明。第三方代码与许可信息见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
