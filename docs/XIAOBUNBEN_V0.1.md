# Xiaobunben-Memory v0.1 — ChatGPT 官方网页路线

**Callback_DLC 已封装成只读 MCP 工具。** 工具使用原项目的 SQLite 线索索引和当前原文复核。默认演示只加载两条程序内置的虚构记录，测试无需模型 API 或 API Key。

## 本地接入与验收

在仓库外准备 `source/`、`allow.json` 和独立的可写 `state/`。将选定的虚构 Markdown 文件放入 `source/`，清单写成 `{"files": ["notes/example.md"]}`。路径相对于 `source/`，使用 `/` 分隔。适配器按当前清单逐文件授权，保留 UTF-8 原文及换行，拒绝路径别名、链接和超过 1 MiB 的文件。批量读取遇到缺失文件时会报错，避免把读取故障误当作撤权。清单更新使用临时文件加原子替换。

[`open_markdown_service`](../adapters/markdown_memory.py) 把 `moments`、`buckets` 和 `raw_events` 交给原项目 `open_service`。宿主提供 `RecallModel` 和 `EmbeddingEngine` 所需的客户端与模型，调用 `await index.rebuild(limit=12)` 分批建库，再用 `await index.warm()` 刷新缓存。`await index.search([query])` 返回经当前原文核对的候选。需要验证旧候选时，使用 `recall.validate` 携带证据版本，或用 `index.resolve` 核对候选 ID、版本和引文。一个授权集合和受众使用一个独立状态目录。每个文件对应一个片段，核心从正文前 6,000 字符生成线索，少于 35 字符的正文不会生成线索。

安装依赖后，从仓库根目录运行以下命令。测试自动在临时目录创建并清理虚构文件和 SQLite 状态，无需模型服务或 API Key。

```sh
python -m pip install -r requirements-test.txt
python -m pip check
python -B -m unittest discover -s tests -p 'test_markdown*.py' -v
python -B -m pytest -q -p no:cacheprovider tests/test_memory_mcp.py tests/test_markdown_memory.py tests/test_markdown_recall.py tests/test_assoc_index.py tests/test_assoc_directions.py tests/test_assoc_recall.py tests/test_assoc_persisted_scope.py
```

在 Windows、Python 3.12.14 上，依赖检查和本轮 **31 项相关测试**通过。MCP 测试用独立子进程验证两个协议版本的 stdio 工具发现、查询、空结果和热撤权，并用本机 Streamable HTTP 再验证调用。上一轮仓库全量测试在原生 Windows 上受 Ombre-Brain 的 POSIX 文件操作和旧测试的 SQLite 清理问题限制，macOS/Linux 全量回归待复跑。

| 检查 | 结果 |
| --- | --- |
| SQLite 建库及重开 | 扫描并建索引 2 条，失败 0 条。两条线索均持久化向量，未授权文件未送入模型替身。重开同一份状态后可搜索。 |
| 跨话题召回 | 用「柚子冻真好吃，可惜只能我一个人尝」查询。关键词检索只命中配方记录，加入固定语义向量后首条命中观星记录「星群你看不到，我替你描一遍。」 |
| 原文验证 | 伪造引文被核心拒绝。修改原文附近文字后，即使引文本身未变，旧证据版本也失效。重建后返回新的上下文和证据版本。 |
| 撤权 | 原文件保留，移出清单后，旧候选、已签发的复核凭据和热缓存均无法返回该记录。重建删除该记录的线索、计划和索引状态行。重开后结果一致，仍获授权的配方记录可查。无效清单阻断召回和重建。 |

撤权控制后续读取和交付。SQLite、WAL、备份及已导出的结果各有保留周期，删行属于逻辑清理。状态目录和授权清单由可信宿主管理。模型会收到用于生成线索的原文，嵌入服务会收到线索或查询，本轮均在进程内处理。将来部署时沿用明文边界，仅向网页实验发送虚构记录或单独授权的脱敏摘录。私人聊天、日记原文和 API Key 留在信任边界内。

## MCP 服务

从仓库根目录运行以下命令。`memory_mcp` 通过标准输入输出接收 MCP 请求，等待连接时终端可以保持空白。

```sh
python -m pip install -r requirements-mcp.txt
python -B -m memory_mcp
```

客户端可发现一个只读工具 `search_memory`，参数为一条最多 500 字符的 `query`。工具核对当前授权和原文后，最多返回一条引文、不透明来源 ID 和证据版本。空结果为 `{"matches": []}`。指定 `--demo-manifest` 可以用[示例授权清单](../host_example/memory-demo-allow.example.json)做撤权测试。移除 `night.md` 后，原来命中的观星引文即消失。HTTP 模式用 `--demo-http 18743`，它固定监听 `127.0.0.1:18743/mcp`，只提供内置虚构数据。

需要测试自己的授权资料时，可复制[本机配置示例](../host_example/memory-local.example.json)到仓库外，再用 `--local-config` 指定配置路径。相对路径以该配置文件所在目录为基准。检索所用模型需要提供兼容的本机 `/v1/chat/completions` 和 `/v1/embeddings`。该模式使用 stdio，模型地址只接受数字形式的回环 IP，禁用环境代理和重定向。实际模型的召回质量仍需用安全样本评估。对网页开放私人资料之前，需要额外的 MCP 用户认证和每次请求的授权，隧道连接凭据只负责隧道本身。[身份验证](https://developers.openai.com/plugins/build/auth)

## ChatGPT 官方网页接入

以下是 2026-11-21 的接入记录。目标账号已确认有「添加自定义 MCP 服务器」表单，支持服务器 URL 和隧道。临时 HTTPS 端点已经用独立 MCP 客户端通过发现和调用，普通 Chat 的调用结果须另行确认。

| 状态 | 能力与条件 |
| --- | --- |
| 官方支持 | ChatGPT 网页可在聊天中调用无界面的 MCP 工具。Pro 支持具有读取或获取权限的自定义服务器。完整读写 MCP 面向 Business 和 Enterprise/Edu。Business 由管理员或所有者创建和发布，Enterprise/Edu 可通过 RBAC 委派创建，发布限管理员或所有者。Plus 和 Free 的同类自定义接入资格未在该指引中确认。新 Plugins 界面可直接添加，旧 Apps 界面仍有 Developer Mode 路线。[OpenAI 帮助中心](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt) |
| 可选连接方式 | 网页可以访问 HTTPS Streamable HTTP `/mcp`，也可以通过 OpenAI Secure MCP Tunnel 连接本机 stdio。官方隧道需要关联目标 ChatGPT 工作区、相应的 Tunnels Read + Use 权限、本地运行凭据和出站 HTTPS。[连接和测试插件](https://developers.openai.com/plugins/deploy/connect-chatgpt) · [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels) |
| 当前网页能力边界 | 官方文档描述的是为单条消息选用工具。需要新数据时再次 `@` 插件。公开自定义 MCP 接口没有提供逐轮自动注入、监听任意聊天及助手提交回执、批量读取用户历史或 Library、替换内置 Memory 的宿主接口。Callback_DLC 原有轮间预备、分支回执和提及冷却所需的宿主生命周期，本轮网页方案无法接入。断开连接只停止未来访问，已进入聊天和 Memory 的内容按各自的设置处理。[自定义 MCP 聊天用法](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt) · [断开连接及数据保留](https://help.openai.com/en/articles/20001494-connecting-and-managing-app-accounts-in-chatgpt) |

### 在普通 Chat 做一次虚构验收

1. 在 ChatGPT 网页选择「插件 → 添加 → 添加自定义 MCP 服务器」，名称填写「小笨笨记忆演示」。使用短时公开 HTTPS 试验时，选「服务器 URL」，填包含 `/mcp` 的试验地址。使用官方 Secure MCP Tunnel 时，选「隧道」并选择已运行的目标隧道。身份验证选「无身份验证」仅适用于这套虚构演示。[连接步骤](https://developers.openai.com/plugins/deploy/connect-chatgpt)
2. 勾选「我已了解，并希望继续」，点「创建为插件」。详情应发现 `search_memory`。打开一个新的普通 Chat 会话，输入 `@` 并选中插件，然后发送「请调用关联记忆工具。柚子冻真好吃，可惜只能我一个人尝。」检查工具结果里的观星引文、来源 ID 和证据版本。
3. 用演示授权清单移除 `night.md`。在一条新消息重新 `@` 插件并查询，检查**新工具结果**中已无观星引文。工具出现或隧道就绪，只分别证明工具发现或网络可达。

### 无密钥临时网页验收

只用内置虚构记录时，可用 [Cloudflare Quick Tunnel](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)把本机 `/mcp` 暂时转发到 HTTPS。HTTPS 通道已用 MCP 客户端验证 `2026-07-28` 和 `2025-11-25` 两种协议，返回同一条核验引文。临时 URL 由命令输出，停止 `cloudflared` 后失效。可访问该 URL 的人都能查询这套虚构数据；新开普通 Chat 只发送测试句子。

```powershell
python -B -m memory_mcp --demo-http 18743 --demo-manifest 'C:/MCP-test/demo-allow.json'
```

```powershell
cloudflared tunnel --no-autoupdate --protocol http2 --http-host-header '127.0.0.1:18743' --url 'http://127.0.0.1:18743'
```

### Windows 官方隧道配置示例

在 [OpenAI Platform 的 Tunnels 页面](https://platform.openai.com/settings/organization/tunnels)创建测试隧道并关联目标 ChatGPT 工作区，从 Runtime API keys 创建受限运行凭据。运行[演示启动脚本](../host_example/Start-MemoryDemoTunnel.ps1)时输入 Tunnel ID，密钥在本机窗口中隐藏输入。脚本执行官方 `doctor` 检查再启动，窗口关闭后停止联调。使用独立测试隧道可避免同一 stdio 隧道上运行两个客户端。二进制由 [OpenAI 官方发行页](https://github.com/openai/tunnel-client/releases/latest)提供。

```powershell
pwsh -NoProfile -File ./host_example/Start-MemoryDemoTunnel.ps1 -TunnelClient 'C:/Tools/tunnel-client.exe' -Python '.venv/Scripts/python.exe' -DemoManifest 'C:/MCP-test/demo-allow.json'
```

上游代码为 PolyForm Noncommercial 1.0.0；遵守原仓库的许可证与署名，不以此分支宣称商业授权。
