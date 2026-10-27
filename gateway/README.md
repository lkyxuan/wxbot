# 自建 AI 网关

让每个微信群背后接一个「你自己的产品」，而不只是一段 prompt。

```
微信群 ──> SiverWXbot（只管收发） ──> 本网关（OpenAI 兼容接口）
                                         │  model = 智能体名，例如 crypto / yungu-food
                                         ├─ 智能体 = 提示词 + 一组工具
                                         ├─ 调大模型（DeepSeek 等，支持工具调用即可）
                                         └─ 工具 = 各个产品的 MCP 服务（各自的数据库、程序）
```

- **机器人**：收到触发回复的消息（@机器人、命中规则等）后，把消息和最近几轮记录发给接口，再把返回的文字发回群里。也可以按群设成每句话都发，由网关判断接不接（见「让 AI 自己判断接不接话」）。
- **网关**（`gateway.py`）：对外是 OpenAI 兼容接口。面板里「模型」填哪个智能体名，就由哪个智能体回答。一个网关可以服务多个群，各群能用的工具互不相同。
- **产品 / 工具**（`mcp_servers/`）：每个产品是一个独立的 MCP 服务。写一次，网关能用，Claude、Dify、Cursor 等也能直接用。

## 目录

| 文件 | 作用 |
|---|---|
| `gateway.py` | 网关本体 |
| `config.example.json` | 配置模板，复制成 `config.json` 使用（`config.json` 不会提交到仓库） |
| `agents/*.md` | 各智能体的提示词 |
| `mcp_servers/crypto_server.py` | 示例产品：加密货币行情（CoinGecko 公共接口） |
| `mcp_servers/video_server.py` | 视频号视频：从 wx_channel 取视频、解密、转文字，供私聊助手总结 |
| `mcp_servers/github_server.py` | GitHub 项目：按名字/关键词找仓库，记进 Notion「视频里的 GitHub 项目」 |
| `mcp_servers/food_server.py` | 云谷美食：店库（高德）+ 群友口碑，识别分享卡片、推荐、记口碑、组局、高德链接 |
| `refresh_base.py` / `refresh_base.bat` | 美食底库：按圆心和半径从高德批量拉周边餐饮店，每周更新 |
| `start_gateway.bat` | Windows 一键启动 |
| `tests/` | 单元测试（`python -m pytest tests`，需另装 pytest） |

## 在 Windows 上部署

1. 安装依赖（在 `gateway` 目录下）：
   ```
   pip install -r requirements.txt
   ```
2. 复制 `config.example.json` 为 `config.json`，修改：
   - `api_keys`：改成一串随机字符，面板里接口的 API Key 填同一串。
   - `llm`：大模型接口。默认是 DeepSeek（`deepseek-chat` 支持工具调用）。Key 建议放环境变量 `DEEPSEEK_API_KEY`，不要写进文件。换别家的话改 `base_url` / `model` 即可，只要兼容 OpenAI 格式并支持 function calling。
3. 启动：双击 `start_gateway.bat`，看到 `MCP 服务 crypto 已连接` 和 `网关已启动` 即可。网关默认只监听 `127.0.0.1:8100`，外网和局域网都访问不到。

## 在 SiverWXbot 面板里接群

1. **模型配置 → 新增接口**：
   - SDK：`OpenAI API 格式兼容接口`
   - 模型：`crypto`（智能体名）
   - Base URL：`http://127.0.0.1:8100/v1`
   - API Key：上面 `api_keys` 里的那串
2. **群聊设置**：在测试群那一行，「AI 接口」下拉选刚加的接口。
3. 面板里给这个群选的 Prompt 仍然生效，会接在智能体提示词后面，适合调人设和语气。不想要的话，在智能体配置里设 `"use_client_prompt": false`。

## 云谷美食（智能体 `yungu-food`）

店库分两层：店铺来自高德（底库脚本批量拉云谷周边 + 群友分享），口碑是群友说过的话（推荐、踩雷、去过、想去），推荐时口碑优先。店铺都用高德自己的店铺页链接分享（`https://www.amap.com/detail/<POI ID>`，很短；没有 POI ID 的店退回坐标标点链接），不自建网页。

1. 在[高德开放平台](https://lbs.amap.com)注册并做个人认证，创建「Web 服务」类型的 Key，设到环境变量 `AMAP_KEY`。个人认证开发者每月免费 5000 次搜索、15 万次地理编码。
2. 面板新增接口（模型填 `yungu-food`），把美食群映射过去。想让它旁听记口碑、自己判断接不接话，给这个群勾上「AI 判断接话」（见下文）。
3. 群设置里开了「仅@时回复」的话，同时打开「分享卡片也回复」，这样群友转发大众点评、高德卡片不用 @ 也会被收录。
4. 数据存在 `gateway/data/food.db`（SQLite），不会提交到仓库，记得定期备份。
5. 云谷食堂的档口高德上没有，用 `save_stall` 工具手工录入（`origin = 食堂档口`，坐标取食堂位置，第一次高德查一次存进 `meta`）。群里有人报档口时智能体会自己录，也可以在 Windows 上直接调用这个函数批量录。
6. 发图片（静态地图、店铺照片）：图片下载到 `gateway/data/images/`。在机器人的 `config/config.json` 里加上
   `"ai_reply_image_dirs": ["D:\\SiverWXbot_plus\\gateway\\data\\images"]`（换成实际路径）。机器人只会发送这个目录里的图片，没配置就不发图。
7. 群友评分（1～5 星，群里叫「阿粉认证」：1 还行早知道在家吃泡面、2 好吃但远下次不来、3 推荐怎么之前没发现、4 权威下次喊几个朋友来、5 香到睡不着）。每人只算最新一次，报了星用原星，没报星时推荐算 3、一般和踩雷算 1；群友改口后自动更新。推荐时群友 2 星及以上的排最前，低于 2 星的排到没人评过的店后面。机器人提到星级时会用这套说法。
7. 群友打卡：「@机器人 打卡 店名 4星 毛肚好吃」，记一条带星级的口碑。平时聊天里的评价在旁听模式下自动记，不出声。

工具：`find_place`（先查店库，没有再搜高德）、`save_place`（入库）、`record_mention`（记口碑，同一条消息不重复记）、`recommend`（按口味、距离、人均挑店，群友评分优先，其次由近到远，默认 3 公里内）、`place_detail`、`who_wants`（谁想去、谁去过）、`nearby_places`（默认云谷中心）、`search_places`、`place_map_link`、`place_images`、`delete_place`。


### 美食底库（云谷周边）

`refresh_base.py` 以云谷中心为圆心，用高德多边形搜索把半径内的餐饮店（高德分类 050000，含中餐、快餐、咖啡、茶饮、甜品）批量拉进 `food.db`，群友不用一家家分享也能查到。不经过大模型。

```
cd gateway
set AMAP_KEY=你的 Key
python refresh_base.py --dry-run     # 先试搜，看看有多少家、用了多少次高德
python refresh_base.py               # 正式入库，默认半径 7000 米（云谷周边店少，范围放大）
python refresh_base.py --center 120.02,30.28 --radius 5000   # 换圆心 / 半径
```

- 圆心默认用高德在杭州搜「云谷中心」的第一个结果，第一次运行后存进数据库，之后一直用同一个点；要换就传 `--center 经度,纬度` 或 `--center-name 地名`。
- 已有的店只更新高德字段（评分、人均、营业时间、电话、照片），群友的备注和推荐人不动；群友分享的店在半径内也会被刷新。
- 这次没搜到的底库店记一次缺席，连续两次标为「疑似关店」，不删除，查询和推荐时不再出现。没拉完的一次（配额用完、报错）不做这个判断。
- 高德一次查询能翻到的条数有上限，脚本把范围切成 1 公里的格子，某格结果满 200 条就四分再搜。7 公里半径估计一次几百次搜索，每周一次一个月一两千次，`--max-calls`（默认 1500）防止意外用光每月 5000 次的配额。
- 每周更新：把 `AMAP_KEY` 设成系统环境变量，然后用计划任务每周一凌晨 3 点运行 `refresh_base.bat`（日志写到 `data/refresh_base.log`）：
  `schtasks /create /tn "云谷美食底库" /tr "D:\SiverWXbot_plus\gateway\refresh_base.bat" /sc weekly /d MON /st 03:00`（换成实际路径）

识别方式：机器人读到的分享卡片只有卡片上的文字（标题、描述、来源），拿不到网址。AI 从标题里提取店名和城市，再用高德搜索得到地址、坐标、评分。内容里带高德店铺 ID（B0 开头）或高德链接时，会直接按 ID 精确查询。大众点评没有个人可用的开放接口，也不抓它的网页。

## 让 AI 自己判断接不接话

默认机器人只在被 @（或命中规则）时才把消息发给网关。想让某个群每句话都交给 AI、由它决定接不接：

1. 机器人面板「群聊设置」里，给这个群勾上「AI 判断接话」。勾了的群不受「仅@时回复」限制，每条消息都发给网关，并带上 `X-WX-Mentioned`（有没有 @ 机器人）、群名、发言人请求头。
2. 在这个群的智能体配置里加 `auto_reply`（见 `config.example.json` 里的 `yungu-food`）：

| 字段 | 作用 |
|---|---|
| `reply_when` | 什么情况该主动接话，用大白话写，每个智能体自己定 |
| `judge_llm` | 判断用的模型，例如 `{"model": "deepseek-chat"}`；不写就用智能体 / 全局的 `llm` |
| `context_messages` | 判断时带最近几条聊天，默认 8 |
| `labels` | 判断时顺便打的标签 `{名称: 说明}`，例如「是不是在评价某家店」 |
| `silent_on_labels` | 命中这些标签、但不接话时，让智能体静默跑一遍（可以调工具记录，回复不发）。写 `["*"]` 表示所有不接话的消息都旁听 |
| `cooldown_seconds` | 没被 @ 时，同一个群多久最多主动说一次，默认 600 秒，设 0 表示不冷却、完全由判断决定（美食群示例为 0）；被 @ 不受限制 |
| `chat_when` | 闲聊里什么情况可以插一句（不写就不插闲聊）。判断步骤在不需要接话时再判断一个 `chat` |
| `chat_cooldown_seconds` | 闲聊插话的冷却，同一个群多久最多插一句，默认 60 秒；和 `cooldown_seconds` 分开计算，问问题不受它影响 |

处理顺序：被 @ 一定由智能体回答；没被 @ 时先用便宜模型带最近几条聊天判断，要接且不在冷却期才交给智能体（带工具、长提示词）；不需要接话但判断为可以闲聊插一句、且不在闲聊冷却期时，也交给智能体，并提示它简短插一句。判断出错一律按不接话处理。没带 `X-WX-Mentioned` 的请求（没勾「AI 判断接话」的群、其它客户端）照常回答。

不回复：智能体或网关回 `<NO_REPLY>` 时，机器人什么都不发。主动接话时智能体也会被告知「没什么要说就回 `<NO_REPLY>`」。

旁听时智能体的系统提示里会多一段，格式固定，智能体的提示词可以按它写记录逻辑：

```
【旁听】这条消息不需要回复，你的回复不会发到群里。需要记录的话用工具记录，然后只回复 <NO_REPLY>。
命中的标签：review
群名：美食群
发言人：小红
收到时间：2026-10-25 12:01:02
消息标识（可用于去重）：sha1(群名\n发言人\n消息内容) 的前 16 位
```

网关日志里每条都记判断结果（接话 / 不接话 / 冷却中 / 静默旁听和标签），部署后可以据此看判断准不准、估算费用。冷却只记在内存里，重启网关清零。

## 私聊助手：总结视频号视频（智能体 `assistant`）

私聊里转发视频号视频给机器人，它会读视频里说的话并总结。视频号卡片本身只有作者名，所以链路是：

1. 机器人收到卡片后点开它（面板「私聊设置」勾选「私聊视频号卡片点开读取」），视频在微信侧边栏里播放；
2. 本机的 [wx_channel](https://github.com/nobiyou/wx_channel)（MIT，以管理员身份运行）拦截到播放请求，记下视频 ID、标题、地址和解密 key；
3. 机器人从 wx_channel 找到这条视频，立即关掉侧边栏（否则会自动连播），把「标题、作者、视频ID」交给网关；
4. 智能体调用 `get_channels_video_transcript`：下载视频（720p 即可）→ 解密开头 128KB → 用 imageio-ffmpeg 抽音频 → 第三方语音转文字 → 总结。

部署：

1. 下载 wx_channel 的 Release，解压到 `D:\wx_channel`，`config.yaml` 里设 `cloud_enabled: false`、`auto_open_channels: false`，`web_console_token` 改成随机串。以管理员身份运行一次（会安装根证书 SunnyRoot，只代理 `WeChatAppEx.exe`）。卸载证书：`wx_channel.exe --uninstall`。
   ⚠ 它的控制台窗口要保持最小化：管理员窗口在前台时，Windows 会丢掉机器人的模拟点击和键盘输入（UIPI），机器人会发不出消息。
2. 环境变量（机器人和网关都要能读到）：
   - `WX_CHANNEL_TOKEN`：上面的 `web_console_token`
   - `ASR_API_KEY`：语音转文字的 Key。默认用[硅基流动](https://cloud.siliconflow.cn)的 `FunAudioLLM/SenseVoiceSmall`；换别家就设 `ASR_BASE_URL` / `ASR_MODEL`，只要兼容 OpenAI 的 `/audio/transcriptions`。
3. `pip install -r requirements.txt`（新增 `imageio-ffmpeg`，自带 ffmpeg，不用另装）。
4. 面板新增接口，模型填 `assistant`，作为私聊用的接口。`assistant` 设了 `use_client_prompt: false`，不会接面板里的私聊提示词。
5. 记录视频里的 GitHub 项目（`github` 服务）：在 [Notion 集成页](https://www.notion.so/my-integrations) 建一个内部集成，Key 设到 `NOTION_TOKEN`；打开数据库「视频里的 GitHub 项目」，右上角「⋯ → 连接」把这个集成加进去；数据库 ID（链接里 32 位十六进制）设到 `NOTION_PROJECTS_DB`。`GITHUB_TOKEN` 可选，只为提高搜索额度。
6. 文字稿缓存在 `data/videos/<视频ID>.json`，追问同一条视频不会重复转写。`video` 服务配了 `tool_timeout: 150`，长视频转写慢时可调大（机器人那边请求超时是 180 秒）。

## 新增一个产品 / 群

1. 在 `mcp_servers/` 下新建一个 MCP 服务，照 `crypto_server.py` 用 `@server.tool()` 写函数。函数参数和 docstring 就是给 AI 看的工具说明。
2. 在 `config.json` 的 `mcp_servers` 里登记它，在 `agents` 里加一个智能体，写上用哪些 MCP。
3. 在 `agents/` 下写它的提示词。
4. 重启网关，面板里再加一个接口（模型填新智能体名），把对应的群映射过去。

一个智能体可以同时挂多个 MCP（比如加密助手同时挂行情和你自己的数据库），不同智能体也可以共用同一个 MCP。

## 目前的限制

- 只有群聊回复支持发图片，私聊回复仍只发文字。
- 机器人发过来的消息格式是「昵称: 内容」，能知道是谁说的，但不知道是哪个群（一群一个智能体时不影响）。
- CoinGecko 免费接口有频率限制，查太勤会提示稍后再试；可以申请 Demo Key 设到环境变量 `COINGECKO_API_KEY`。
