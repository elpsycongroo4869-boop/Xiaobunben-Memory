# Xiaobunben-Memory v0.1 — ChatGPT 官方网页路线

**本地已接通 Callback_DLC 联想检索核心。** ChatGPT 官方网页接入已核对官方文档，MCP 服务封装、部署及网页调用仍待验收。所有测试使用虚构记录和离线模型替身。

## 本地接入与验收

在仓库外准备 `source/`、`allow.json` 和独立的可写 `state/`。将选定的虚构 Markdown 文件放入 `source/`，清单写成 `{"files": ["notes/example.md"]}`。路径相对于 `source/`，使用 `/` 分隔。适配器按当前清单逐文件授权，保留 UTF-8 原文及换行，拒绝路径别名、链接和超过 1 MiB 的文件。批量读取遇到缺失文件时会报错，避免把读取故障误当作撤权。清单更新使用临时文件加原子替换。

[`open_markdown_service`](../adapters/markdown_memory.py) 把 `moments`、`buckets` 和 `raw_events` 交给原项目 `open_service`。宿主提供 `RecallModel` 和 `EmbeddingEngine` 所需的客户端与模型，调用 `await index.rebuild(limit=12)` 分批建库，再用 `await index.warm()` 刷新缓存。`await index.search([query])` 返回经当前原文核对的候选。需要验证旧候选时，使用 `recall.validate` 携带证据版本，或用 `index.resolve` 核对候选 ID、版本和引文。一个授权集合和受众使用一个独立状态目录。每个文件对应一个片段，核心从正文前 6,000 字符生成线索，少于 35 字符的正文不会生成线索。

安装依赖后，从仓库根目录运行以下命令。测试自动在临时目录创建并清理虚构文件和 SQLite 状态，无需模型服务或 API Key。

```sh
python -m pip install -r requirements-test.txt
python -m pip check
python -B -m unittest discover -s tests -p 'test_markdown*.py' -v
python -B -m pytest -q -p no:cacheprovider tests/test_markdown_memory.py tests/test_markdown_recall.py tests/test_assoc_index.py tests/test_assoc_directions.py tests/test_assoc_recall.py tests/test_assoc_persisted_scope.py
```

在 Windows、Python 3.12.14 上，依赖检查通过，Markdown 单元与集成测试共 **14 项通过**，连同相关核心回归共 **27 项通过**。仓库根目录全量收集时，Ombre-Brain 适配器依赖 `os.O_DIRECTORY`，原生 Windows 不支持。单独运行整个 `tests/` 得到 95 项通过、33 项失败，失败集中在同一 Ombre-Brain 导入和旧测试或示例的 SQLite 文件句柄清理。原仓库面向 macOS/Linux 的全量测试待在对应系统复跑。

| 检查 | 结果 |
| --- | --- |
| SQLite 建库及重开 | 扫描并建索引 2 条，失败 0 条。两条线索均持久化向量，未授权文件未送入模型替身。重开同一份状态后可搜索。 |
| 跨话题召回 | 用「柚子冻真好吃，可惜只能我一个人尝」查询。关键词检索只命中配方记录，加入固定语义向量后首条命中观星记录「星群你看不到，我替你描一遍。」 |
| 原文验证 | 伪造引文被核心拒绝。修改原文附近文字后，即使引文本身未变，旧证据版本也失效。重建后返回新的上下文和证据版本。 |
| 撤权 | 原文件保留，移出清单后，旧候选、已签发的复核凭据和热缓存均无法返回该记录。重建删除该记录的线索、计划和索引状态行。重开后结果一致，仍获授权的配方记录可查。无效清单阻断召回和重建。 |

撤权控制后续读取和交付。SQLite、WAL、备份及已导出的结果各有保留周期，删行属于逻辑清理。状态目录和授权清单由可信宿主管理。模型会收到用于生成线索的原文，嵌入服务会收到线索或查询，本轮均在进程内处理。将来部署时沿用明文边界，仅向网页实验发送虚构记录或单独授权的脱敏摘录。私人聊天、日记原文和 API Key 留在信任边界内。

## ChatGPT 官方网页接入

以下是 2026-11-20 核对的产品条件。目标账号和会话入口的实际可用性仍需用网页工具调用确认。

| 状态 | 能力与条件 |
| --- | --- |
| 官方支持 | ChatGPT 网页可在聊天中调用无界面的 MCP 工具。Pro 支持具有读取或获取权限的自定义服务器。完整读写 MCP 面向 Business 和 Enterprise/Edu。Business 由管理员或所有者创建和发布，Enterprise/Edu 可通过 RBAC 委派创建，发布限管理员或所有者。Plus 和 Free 的同类自定义接入资格未在该指引中确认。新 Plugins 界面可直接添加，旧 Apps 界面仍有 Developer Mode 路线。[OpenAI 帮助中心](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt) |
| 需要封装与部署 | 将 `index.search` 包装为只读 MCP 工具。网页通过可达的 HTTPS Streamable HTTP 端点（通常为 `/mcp`），或通过 OpenAI Secure MCP Tunnel 转发到私网的 stdio 或 HTTP 服务。本机 `localhost` 或 stdio 不能直接填进网页 URL 入口。Tunnel 需要关联目标 ChatGPT 工作区、相应的 Tunnels Read + Use 权限、本地运行凭据和出站 HTTPS。服务认证和授权仍需单独验证。[连接和测试插件](https://developers.openai.com/plugins/deploy/connect-chatgpt) · [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels) · [身份验证](https://developers.openai.com/plugins/build/auth) |
| 当前网页能力边界 | 官方文档描述的是为单条消息选用工具。需要新数据时再次 `@` 插件。公开自定义 MCP 接口没有提供逐轮自动注入、监听任意聊天及助手提交回执、批量读取用户历史或 Library、替换内置 Memory 的宿主接口。Callback_DLC 原有轮间预备、分支回执和提及冷却所需的宿主生命周期，本轮网页方案无法接入。断开连接只停止未来访问，已进入聊天和 Memory 的内容按各自的设置处理。[自定义 MCP 聊天用法](https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt) · [断开连接及数据保留](https://help.openai.com/en/articles/20001494-connecting-and-managing-app-accounts-in-chatgpt) |

## 下一步

1. 在目标 `chatgpt.com` 账号和工作区确认 **Plugins → + → Add custom MCP server** 可见，并检查所需权限。
2. 用虚构库封装单个只读查询工具。输入为当前查询，结果只返回核验后的有限引文、不透明来源 ID 和证据版本。用 MCP Inspector 检查发现、调用、空结果、认证和撤权。用独立的本地模型或可信服务验证真实模型及向量质量。测试中的固定向量不衡量实际召回率。
3. 使用经认证的 HTTPS 端点或官方 Tunnel 连接。凭据由宿主本地秘密存储提供。网页创建私有插件后先核对已发现的工具，再在实际使用的会话里 `@` 插件查询柚子冻，检查观星引文及版本。撤掉该文件授权后，用新消息再次调用，确认新工具结果无该引文。连接健康或工具出现在列表里只证明基础链路可用。

上游代码为 PolyForm Noncommercial 1.0.0；遵守原仓库的许可证与署名，不以此分支宣称商业授权。
