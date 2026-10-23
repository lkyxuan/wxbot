# wxbot 开发说明

- 只有 `src/wxbot/drivers/` 可以 import wxautox4；其他代码只依赖 `drivers/base.py` 的接口。
- wxautox4 是 UI 自动化，所有调用必须串行（驱动里已有锁），不要在新线程里直接操作微信。
- 新功能写成 `features/` 下的 Handler，并在 `core/bot.py: build_pipeline()` 注册；配套测试用 `FakeDriver`。
- 改编自 SiverWXbot_plus 的文件要在文件头注明来源（Apache-2.0 要求），见 NOTICE。
- 配置项加在 `config.py` 的 pydantic 模型里，同时更新 `config/config.example.yaml`。
- 提交前：`ruff check src tests && pytest -q`。
- 用户界面文案、注释、文档统一用中文。
