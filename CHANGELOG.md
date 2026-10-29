# 更新日志

格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循语义化。

## [Unreleased]

### 待办

- 多音色：识别说话人并按人分配音色
- 字幕导出（srt / ass）
- 译成英文及其他语言

## [0.2.0] — 2026-10-29

第一个对外版本。相比早期的一堆脚本，这一版重写了对齐算法、补齐了网页界面。

### 新增

- 本地网页界面：拖拽、队列、四阶段实时进度、在线播放、音色试听
- 命令行：`run` / `serve` / `doctor` / `voices` / `models` / `clean` / `config`
- 串行任务队列，可取消、可从任意阶段重试
- 断点续跑：产物存在就跳过，翻译逐批落盘
- TTS 哈希缓存：改一句译文只重合成一句
- 伴奏混音，用侧链压缩让人声优先
- 响度标准化到 −16 LUFS，直接可用不必再调音量
- 环境体检报告（`videodub doctor`），可直接贴进 issue

### 修复

- **长视频时间轴漂移**：旧实现最大漂移 16 秒。根因有三处，都已修：
  - 翻译单元按「320 个英文字符」切，与时间预算脱钩 → 改为按时间预算切
  - 槽位按原片时刻算，晚开口的句子必然再次越界 → 改为按实际开口时刻算
  - 译文超长只能事后硬压 → 改为翻译后校验长度、超了就让模型重译短一点
- 音频装进 MP4 用 Opus 导致声画错位 → 改 AAC 48kHz 立体声
- 数据轨未剥离导致部分播放器异常 → `-dn`
- 中文系统下 UTF-8 模式导致批处理乱码 → 脚本改 UTF-8 + `chcp 65001`

### 变更

- ffmpeg 改由 `imageio-ffmpeg` 提供，仓库不再带 300MB 二进制，也不再出现
  ffmpeg/ffprobe 版本错配
- 配置收敛到单一 `config.toml`（原来三个入口各有一份互不相同的默认值）
- HuggingFace 镜像改为自动探测：能直连就不走镜像

[Unreleased]: https://github.com/ximu1054/videodub/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/ximu1054/videodub/releases/tag/v0.2.0
