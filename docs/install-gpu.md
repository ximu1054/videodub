# GPU 与几个难装的依赖

默认安装（`pip install -e .`）就能跑，用 CPU 识别、用在线接口翻译。
这份文档讲三个「想更快 / 想全离线」时才需要碰的东西。

## 一、让识别跑在 GPU 上

faster-whisper 通过 CTranslate2 调 CUDA，需要 **cuDNN 9** 和 **cuBLAS**。
装了 CUDA Toolkit 不代表这两个库就位——它们从 CUDA 11 起就被拆出去了。

最省事的办法是让 pip 把它们装进虚拟环境：

```bash
uv pip install nvidia-cublas-cu12 nvidia-cudnn-cu12 nvidia-cuda-runtime-cu12
```

Windows 上这些 DLL 在 `site-packages/nvidia/*/bin`，不在 PATH 里，
所以 VideoDub 启动时会自己把它们加进 DLL 搜索路径（`platforms/windows.py`
的 `enable_cuda_dlls`）。这一步不做，你会看到那个经典报错：

```
Could not locate cudnn_ops64_9.dll
```

验证：

```bash
videodub doctor
```

看到 `CUDA 可用  RTX 4070 Ti  12.0GB` 就成了。

如果显卡有 6GB 以上但识别仍然 OOM，把模型换小一档：

```bash
videodub run video.mp4 --asr-model medium
```

`large-v3` 约需 3GB 显存，`medium` 约 1.5GB，质量差距在播客类内容上很小。

## 二、本地翻译（完全离线）

```bash
uv pip install -e ".[local-mt]"
```

torch 要装 CUDA 版，否则会装成 CPU 版然后慢十倍。用 uv 时
`pyproject.toml` 里已经配好了 PyTorch 官方索引，一条命令就对：

```bash
uv pip install -e ".[local-mt]"
```

用 pip 的话要自己指定索引：

```bash
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu126
pip install -e ".[local-mt]"
```

验证装的是不是 CUDA 版：

```bash
python -c "import torch; print(torch.version.cuda, torch.cuda.is_available())"
```

打印 `None False` 说明装成了 CPU 版，重装。

配置里指定：

```toml
[mt]
backend = "local"
local_model = "Qwen/Qwen2.5-3B-Instruct"
```

3B 模型 fp16 约占 6GB 显存。和识别模型同时驻留需要 9GB 左右；
VideoDub 用子进程隔离每个阶段，所以它们不会同时占用——识别进程退出时
显存就还给系统了。

## 三、自动分离伴奏（实验性，不建议）

设计上，伴奏应该由用户自己提供（「高级」里的伴奏文件）。自动分离是为了
偷懒用的，代价很大：

- demucs 4.x 把 `torchaudio<2.1` 钉死了，和你大概率已经装好的新版 torch 冲突
- 分离一首 30 分钟的音轨，在 4070 Ti 上要 4 分钟
- 分离质量对播客（本来就没伴奏）毫无意义

真要试：

```bash
uv pip install demucs --no-deps
uv pip install dora-search einops julius lameenc openunmix pyyaml
```

（`--no-deps` 是必须的，否则 pip 会试图降级 torchaudio 而把整个环境搞坏。）

然后界面里会出现「自动分离伴奏」选项。如果它坏了，别修——直接用自己的伴奏文件。

## 四、国内网络

模型从 HuggingFace 下载。VideoDub 默认会先探测 `huggingface.co` 通不通，
不通才走 `hf-mirror.com`，所以不用手动配。

要强制指定：

```toml
[network]
hf_mirror = "hf-mirror.com"     # 或 "none" 强制直连，或完整 URL
```

下载视频（yt-dlp）走的是系统代理。代理只监听本机时留 `auto` 即可。
要指定：

```toml
[network]
proxy = "http://127.0.0.1:7890"
```

配音合成（edge-tts）必须能连微软的服务器，这一环没有国内镜像。
连不上时它会明确报「TTS 连接失败」，不会静默出个没有声音的视频。
