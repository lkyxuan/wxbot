# 功能路线图

第一版（本仓库骨架）已有：wxautox4 驱动、白名单监听、管理员命令、关键词回复、AI 回复（OpenAI 兼容 + 备用接口）、对话记忆、随机延时、长消息分段、群 @ 回复、Webhook 告警。

下面是计划从 SiverWXbot_plus 移植或重写的功能，按建议优先级排列：

| 优先级 | 功能 | SiverWXbot_plus 参考位置 |
|---|---|---|
| P1 | 图片识别（下载图片 + 多模态） | wxbot_core.py `message_handle_callback` / OpenAIAPI 图片部分 |
| P1 | 按会话绑定不同 AI 接口 | `group_api_map` / `chat_api_map` |
| P1 | 群新人欢迎语 | `send_group_welcome_msg` |
| P1 | 定时消息（每天/每周/每月/单次） | `scheduled_msg_list` 相关逻辑 |
| P2 | 多条消息合并回复 | `_should_buffer_message` / `_check_merge_buffers` |
| P2 | AI 拆分多条回复 | `chat_split_reply_switch` |
| P2 | 新好友自动通过、打招呼、备注 | `GetNewFriends` 相关逻辑 |
| P2 | 自定义转发规则 | `_handle_custom_forward` |
| P2 | Dify / Coze 接口 | `DifyAPI` / `CozeAPI` |
| P3 | 全局监听（黑名单模式） | `AllListen_switch` / `GetNextNewMessage` |
| P3 | 朋友圈点赞、定时发圈 | `Moments` / `PublishMoment` |
| P3 | 网页管理面板（仅绑定 127.0.0.1） | web_server.py |
| P3 | 邮件告警 | email_send.py |

不打算移植：SiverPanel 远程中转（siver_panel.py）、检查更新与开发者广播。
