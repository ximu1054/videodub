# 参与开发

先说清楚这个项目的取舍，避免白干：

- **能用 > 优雅**。用户双击就能跑，比代码漂亮重要
- **Windows 优先**。CI 三个平台都跑，但 Windows 上出问题是第一优先级
- **不引入重型依赖**。能用一个纯函数解决的，不引一个库
- **每个 bug 都补一条回归测试**。尤其是时间轴相关的——它错起来是「听起来怪」，不是报错

## 开发环境

```bash
git clone https://github.com/ximu1054/videodub.git
cd videodub
uv venv --python 3.12
uv pip install -e ".[dev]"
uv run pytest
```

跑全量（含需要 GPU / 联网的）：

```bash
uv run pytest -m "gpu or network"
```

改了代码先过一遍格式：

```bash
uv run ruff check src tests
uv run ruff format src tests
```

## 代码结构

```
src/videodub/
├─ config.py        默认值的唯一来源
├─ errors.py        异常族，每个带退出码
├─ progress.py      进度事件协议（CLI 和 Web 共用）
├─ pipeline.py      四个阶段的编排，决定跳过还是重跑
├─ platforms/       平台差异隔离（代理探测、打开文件夹、杀进程）
├─ media/           ffmpeg 封装、音频原语、Range 响应
├─ stages/          download / asr / translate / render
│   ├─ segment.py   词 → 句
│   ├─ units.py     句 → 翻译单元（按时间预算切）
│   ├─ align.py     排布：决定每句从哪开口、压不压语速  ← 核心
│   └─ tts.py       合成 + 哈希缓存
├─ jobs/            任务存储与串行队列
└─ web/             FastAPI + 单页前端
```

读代码建议从 `pipeline.py` 开始，然后 `stages/align.py`——后者是这项目唯一
值得单独读的东西，文件头的注释解释了三处设计取舍的来由。

## 改时间轴算法要注意什么

`tests/test_align.py` 里有两个回归用例，用的是一份 30 分钟真实播客的时间轴
（`tests/fixtures/karpathy_vibe_coding.json`，182 句，来历见同目录的构建脚本）。
两个用例分别是：

1. **旧算法在这份样本上必须仍然漂 16 秒左右**——证明样本没被改坏
2. **译文守长度预算后，漂移必须小于 1 秒**——这是产品承诺

改完算法这两条都得绿。如果第 1 条挂了，说明你动了样本或者基线，先想清楚为什么。

## 提交

- 一个 PR 一件事
- commit message 写「为什么」，不写「改了什么」——diff 已经说明后一半
- 新功能请在 `CHANGELOG.md` 的 `Unreleased` 下加一行

## 不要提交的东西

`in/` `out*/` `tmp/` `bin/` 里的媒体文件、`translate_api.json`、任何 API key。
`.gitignore` 已经挡住了大部分，提交前扫一眼 `git status`。
