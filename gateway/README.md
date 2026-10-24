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

## 新增一个产品 / 群

1. 在 `mcp_servers/` 下新建一个 MCP 服务，照 `crypto_server.py` 用 `@server.tool()` 写函数。函数参数和 docstring 就是给 AI 看的工具说明。
2. 在 `config.json` 的 `mcp_servers` 里登记它，在 `agents` 里加一个智能体，写上用哪些 MCP。
3. 在 `agents/` 下写它的提示词。
4. 重启网关，面板里再加一个接口（模型填新智能体名），把对应的群映射过去。

一个智能体可以同时挂多个 MCP（比如加密助手同时挂行情和你自己的数据库），不同智能体也可以共用同一个 MCP。

## 目前的限制

- 回复只发文字。让 AI 发图片（例如静态地图）需要给机器人加一个小改动，后续再做。
- 机器人目前只把消息文字发过来，「哪个群、谁说的」还传不过来，也要给机器人做个小改动。
- CoinGecko 免费接口有频率限制，查太勤会提示稍后再试；可以申请 Demo Key 设到环境变量 `COINGECKO_API_KEY`。
