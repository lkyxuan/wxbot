# 自建 AI 网关

让每个微信群背后接一个「你自己的产品」，而不只是一段 prompt。

```
微信群 ──> SiverWXbot（只管收发） ──> 本网关（OpenAI 兼容接口）
                                         │  model = 智能体名，例如 crypto / food
                                         ├─ 智能体 = 提示词 + 一组工具
                                         ├─ 调大模型（DeepSeek 等，支持工具调用即可）
                                         └─ 工具 = 各个产品的 MCP 服务（各自的数据库、程序）
```

- **机器人**：收到触发回复的消息（@机器人、命中规则等）后，把消息和最近几轮记录发给接口，再把返回的文字发回群里。机器人代码不需要改。
- **网关**（`gateway.py`）：对外是 OpenAI 兼容接口。面板里「模型」填哪个智能体名，就由哪个智能体回答。一个网关可以服务多个群，各群能用的工具互不相同。
- **产品 / 工具**（`mcp_servers/`）：每个产品是一个独立的 MCP 服务。写一次，网关能用，Claude、Dify、Cursor 等也能直接用。

## 目录

| 文件 | 作用 |
|---|---|
| `gateway.py` | 网关本体 |
| `config.example.json` | 配置模板，复制成 `config.json` 使用（`config.json` 不会提交到仓库） |
| `agents/*.md` | 各智能体的提示词 |
| `mcp_servers/crypto_server.py` | 示例产品：加密货币行情（CoinGecko 公共接口） |
| `mcp_servers/food_server.py` | 美食地图：识别分享卡片、高德查店、本地 SQLite 收录、附近查询、地图链接 |
| `refresh_base.py` / `refresh_base.bat` | 美食底库：按圆心和半径从高德批量拉周边餐饮店，每周更新 |
| `public/food-map/` | 美食网页地图页面（含 Leaflet 1.9.4，BSD-2 许可） |
| `start_gateway.bat` | Windows 一键启动 |

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

## 美食地图

1. 在[高德开放平台](https://lbs.amap.com)注册并做个人认证，创建「Web 服务」类型的 Key，设到环境变量 `AMAP_KEY`。个人认证开发者每月免费 5000 次搜索、15 万次地理编码。
2. 面板新增接口（模型填 `food`），把美食群映射过去。
3. 群设置里开了「仅@时回复」的话，同时打开「分享卡片也回复」，这样群友转发大众点评、高德卡片不用 @ 也会被收录。
4. 数据存在 `gateway/data/food.db`（SQLite），不会提交到仓库，记得定期备份。
5. 发图片（静态地图、店铺照片）：图片下载到 `gateway/data/images/`。在机器人的 `config/config.json` 里加上
   `"ai_reply_image_dirs": ["D:\\SiverWXbot_plus\\gateway\\data\\images"]`（换成实际路径）。机器人只会发送这个目录里的图片，没配置就不发图。

6. 网页地图：浏览器打开 `http://127.0.0.1:8100/food-map/` 可以看到所有收录的店（底图用高德，可搜索）。这个路径不需要 Key，其余接口仍需要。要让群友手机能打开，需要把这个路径对外公开（例如 Tailscale Funnel 只公开 `/food-map`），再把公开地址填到 `mcp_servers.food.env.FOOD_MAP_URL`。

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
- 这次没搜到的底库店记一次缺席，连续两次标为「疑似关店」，不删除，网页地图上不再显示。没拉完的一次（配额用完、报错）不做这个判断。
- 高德一次查询能翻到的条数有上限，脚本把范围切成 1 公里的格子，某格结果满 200 条就四分再搜。7 公里半径估计一次几百次搜索，每周一次一个月一两千次，`--max-calls`（默认 1500）防止意外用光每月 5000 次的配额。
- 每周更新：把 `AMAP_KEY` 设成系统环境变量，然后用计划任务每周一凌晨 3 点运行 `refresh_base.bat`（日志写到 `data/refresh_base.log`）：
  `schtasks /create /tn "云谷美食底库" /tr "D:\SiverWXbot_plus\gateway\refresh_base.bat" /sc weekly /d MON /st 03:00`（换成实际路径）

识别方式：机器人读到的分享卡片只有卡片上的文字（标题、描述、来源），拿不到网址。AI 从标题里提取店名和城市，再用高德搜索得到地址、坐标、评分。内容里带高德店铺 ID（B0 开头）或高德链接时，会直接按 ID 精确查询。大众点评没有个人可用的开放接口，也不抓它的网页。

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
