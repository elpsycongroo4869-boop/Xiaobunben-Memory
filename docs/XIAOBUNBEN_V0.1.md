# Xiaobunben-Memory v0.1 — ChatGPT 官方网页路线

状态：**实验性适配器已提交；未部署 MCP；未连接用户真实聊天或 Library；尚未完成 Callback_DLC 核心集成测试。**

## 方案边界

- 继续使用 ChatGPT 官方网页，不自建聊天前端。
- ChatGPT 自身历史记忆与 Library 保持原样；本项目不能修改 ChatGPT 内部记忆或给每一轮回复自动注入上下文。
- v0.1 为外部、只读、明确授权的 Markdown 文件适配器，适用于日记与项目文档的**本地导出副本**。
- 不自动读取 ChatGPT、Library 或浏览器 cookie；不把私有记录提交到公开 GitHub。
- 后续可考虑官方可用的 MCP/插件路径，具体可用性取决于账号权限和产品支持。

## 已提交内容

`adapters/markdown_memory.py` 提供 Callback_DLC 所需的 `moments`、`buckets`、`raw_events` 接口；所有读取均重新检查授权清单与当前原文。`tests/test_markdown_memory.py` 覆盖来源读回、未授权隔离、修改撤权、缺失文件和路径防护。

## 如何本地试用

在隔离目录准备明文文件 `source/notes/example.md`，内容仅使用**虚构示例**。创建 `allow.json`：

```json
{"files": ["notes/example.md"]}
```

从仓库根目录运行：

```bash
python3.12 -m unittest tests.test_markdown_memory
```

可用 Python 检查端口：

```python
from adapters.markdown_memory import MarkdownMemory
m = MarkdownMemory("source", "allow.json")
print(m.moments.list_all())
```

后续在真实 ChatGPT 工具调用前，必须补全：原有核心 `open_service` 集成、固定数据端到端检索、索引撤权同步测试、模型与嵌入服务配置、隐私与生命周期管理、MCP 服务认证及权限测试。

## 路线决策

1. 先用虚构数据验证文件权限与原文回读。
2. 再连接原项目联想核心并跑模拟端到端测试。
3. 最后才研究官方网页通过可用 MCP/插件接入；无此权限时保留手动查询/文件导入路线。

上游代码为 PolyForm Noncommercial 1.0.0；遵守原仓库的许可证与署名，不以此分支宣称商业授权。
