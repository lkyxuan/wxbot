# wxbot

在 Windows 电脑上运行的微信自动化机器人：AI 自动回复、关键词回复、对话记忆、管理员命令，后续逐步加入定时消息、新好友、朋友圈等功能。

底层通过 [wxautox4](https://pypi.org/project/wxautox4/)（付费闭源库）以 UI 自动化方式操作微信 PC 版。部分设计和代码改编自 [SiverWXbot_plus](https://github.com/SiverKing/SiverWXbot_plus)（Apache-2.0），详见 [NOTICE](NOTICE)。

## 运行环境

- Windows 10/11 64 位，保持登录、不锁屏、关闭自动睡眠
- Python 3.10 ~ 3.13
- 微信 PC 版 4.1.9 ~ 4.1.15.56（关闭微信自动更新）
- wxautox4 激活码（向作者购买）

## 快速开始

```bat
git clone https://github.com/lkyxuan/wxbot.git
cd wxbot
py -3.12 -m venv .venv
.venv\Scripts\pip install -e ".[wx,dev]"

:: 激活 wxautox4（只需一次）
.venv\Scripts\python -m wxbot activate 你的激活码

:: 准备配置
copy config\config.example.yaml config\config.yaml
copy .env.example .env
:: 编辑 config\config.yaml 和 .env，填好监听名单和 AI Key

:: 先登录微信 PC 版，然后启动
.venv\Scripts\python -m wxbot run
```

也可以双击 `scripts\run_windows.bat`（首次会自动建虚拟环境并安装依赖）。

在 `admin_chat`（默认“文件传输助手”）里发送 `/帮助` 查看管理员命令。

## 目录结构

```
src/wxbot/
├─ main.py              命令行入口（run / activate）
├─ config.py            配置模型与加载（YAML + .env）
├─ drivers/             微信驱动层：唯一接触 wxautox4 的地方
│  ├─ base.py           WeChatDriver 接口与统一消息模型
│  ├─ wxautox4_driver.py
│  └─ fake_driver.py    测试用假驱动，无需微信
├─ core/
│  ├─ bot.py            生命周期、消息入口、回复（延时、分段、@）
│  └─ pipeline.py       处理链：管理员命令 → 关键词 → AI
├─ features/            每个功能一个 Handler
├─ ai/                  AI 接口（OpenAI 兼容，支持备用接口）
├─ memory/              按会话保存聊天记录
└─ notify/              Webhook 告警
```

## 开发

```bash
pip install -e ".[dev]"
pytest -q          # 测试使用 FakeDriver，Linux/Mac 上也能跑
ruff check src tests
```

新增功能：在 `src/wxbot/features/` 写一个带 `name` 和 `handle(ctx) -> bool` 的类，然后在 `core/bot.py` 的 `build_pipeline()` 里注册。功能路线图见 [docs/roadmap.md](docs/roadmap.md)。

## 风险提示

非官方自动化违反微信用户协议，存在限制登录或封号风险。建议先用小号测试，保留随机回复延时，避免群发和高频操作。

## 许可证

Apache-2.0。wxautox4 不随本仓库分发，需自行安装并购买授权。
