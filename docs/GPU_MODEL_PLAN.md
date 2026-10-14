# GPU 模型与依赖规划（2026-10-14 定稿，后续代码照此执行）

> 背景：hf-mirror 反代对 3GB 大 blob 单连接限速僵死 + 新实例盘空 + brotli/hf_hub 版本错位，
> 导致 large-v3 在旧链路上 3 小时下不完。新链路 ModelScope 3 分 19 秒下完 2.9G。

## 1. 模型下载：ModelScope 主，hf-mirror 备，SFTP 兜底

1. 所有 ≥500MB 的模型预下载走 `modelscope.snapshot_download`（国内 CDN，不断点续传）。
2. `bili/autodl.py::_prefetch_whisper_model` 顺序：ModelScope → hf-mirror（3 次重试）→ warn 放行，
   工作机首次转写复用同一缓存（不断第二份）。
3. `bili/transcribe.py::_get_whisper_model` 加载顺序：本地 ModelScope 快照
   （`/root/autodl-tmp/huggingface/modelscope/models/<org>--<name>/snapshots/master`，
   认 `model.bin`）→ 本地 HF hub 快照 → 联网 `download_model`。
4. 新开 GPU 第一次即打 `large-v3.tar.gz` 快照存 Mac，以后 SFTP 直传解压，不走镜像站。

## 2. 依赖安装：能跳就跳，pin 死版本

- `_bootstrap_collect` 先 `find_spec` 探活（faster_whisper/av/rapidocr_onnxruntime/onnxruntime/brotli），
  全齐就跳过 pip；缺啥装啥，`--prefer-binary`。
- `requirements-ai.txt` 全部 pin：`faster-whisper==1.2.1`、`av>=12,<19`、
  `rapidocr-onnxruntime==1.4.4`、`onnxruntime==1.20.1`、`brotli>=1.1.0`、
  `modelscope>=1.18.0`。brotli<1.1 会触发 `process() takes no keyword arguments`，禁止回退。
- 新镜像先 `conda install -y pip`（部分 AutoDL 镜像自带 pip 24.0 已坏，装啥都 `Version(None)`）。

## 3. 代码同步：小文件必传，大文件按 hash

- 200KB 以下代码/配置每次重连必传（修过“同字节跳过”导致远端永不更新的 bug）。
- 模型快照/语料等大文件单列，按 sha 跳过，绝不混在 13 文件清单里。
- 每次同步后清远端 `__pycache__` 再重启 worker，否则 `.pyc` 还是旧逻辑。

## 4. 排障手册

| 现象 | 根因 | 动作 |
|---|---|---|
| `process() takes no keyword arguments`（下载链） | brotli<1.1 + httpx2 | 升 brotli |
| `No matching distribution for rapidocr-onnxruntime==1.3.x` | 该版本被 yank / 限 Python | 查 `pip index versions` 再 pin |
| 预下载超时但 worker 起来了 | 大文件慢，warn 放行是设计 | 等 `MODEL_READY`，勿重连 |
| 多个 download_model 进程 | UI 超时孤儿 + 后台 + 转写触发 | 只留 1 个，杀孤儿，缓存不丢 |
| pip 全挂 `Version(None)` | 镜像 pip 坏 | `conda install -y pip` |
