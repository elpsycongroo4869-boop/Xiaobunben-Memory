"""Small fictional collection and extractive cue generation for the MCP demo."""
import json
from types import SimpleNamespace


QUOTE = "星群你看不到，我替你描一遍。"
DISTRACTOR = "配方卡写着柚子冻需要冷藏。"
QUERY = "柚子冻真好吃，可惜只能我一个人尝。"
RECORDS = {
    "night.md": "# 鹭湾观星台\n海风翻动手边的纸。我把铅笔拿起来，在纸页上写了一句。\n" + QUOTE,
    "recipe.md": "# 配方试做\n今天记的是两种温度的对照实验，准备把结果誊到食谱的下一页。\n" + DISTRACTOR,
}


def write_demo(root):
    source = root / "source"
    source.mkdir()
    for name, text in RECORDS.items():
        (source / name).write_text(text, encoding="utf-8")
    manifest = root / "allow.json"
    manifest.write_text(json.dumps({"files": list(RECORDS)}), encoding="utf-8")
    return source, manifest


class DemoClient:
    def __init__(self):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.complete))

    async def complete(self, *, messages, **options):
        prompt = messages[-1]["content"]
        if "\nEVIDENCE:\n" in prompt:
            text = prompt.split("\nEVIDENCE:\n", 1)[1]
            # The demo notes end with an evidence line. Extract it directly from the file.
            quote = text.splitlines()[-1].strip()
            cues = [{"cue_type": "detail", "quote": quote, "cue_text": quote}]
            result = {"cues": cues}
        else:
            result = {"keep": [0]}
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(result)))])
