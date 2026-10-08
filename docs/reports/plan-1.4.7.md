# 1.4.7 修复/补强计划（审计收尾批次）✅ 已完成（2026-10-08）

> 执行结果：三项主线全部落地并发布（F1 commit `87f4131`、F3 `8027f57`、
> L3 `5d85ba1`）；F3 过程中额外发现并修复 `video config init` 的
> NoneType TOML 序列化崩溃（write_template）。video/cli.py 覆盖率 29% → 66%。

> 依据：`docs/reports/audit-2026-10-07.md` 建议批次表第 2 行——「1.4.7 或同批：
> F1 doctor、F3 video CLI 转发测试、L3 死代码清理」。定位为 1.4.6 bugfix 之后的
> **微量补强版**（无行为破坏），仍是 0.0.1 小版本步进。
> 状态：计划（未开始）。前置：1.4.6 已发布。

## 范围（3 项主线 + 1 项登记）

### 1. F1 doctor 增强【小改动，高日常价值】

现状：`sunoauxtool doctor` 已检查 Python/fluidsynth/SF2/torch/CUDA/audiocraft/
diffrhythm/espeak/项目完整性/配置，但缺三块日常必用的依赖面：

| 探测项 | 方法 | 提示语要点 |
|---|---|---|
| ffmpeg / ffprobe | 复用 `download` 包的 `find_ffmpeg()`（SUNO_FFMPEG / SUNO_FFMPEG_DIRS / PATH 三层） | 命中则报版本（`ffmpeg -version` 首行）；未命中给 4 条修法提示（同 20 号错误码文案） |
| AUDIOSR_DIR 源码目录 | 环境变量存在性 + `audiosr` 可导入性（延迟导入，缺失不算失败只标 warn） | 指向 `requirements/vasr.txt` 的源码目录模式说明 |
| basic_pitch | 延迟导入 + 四后端探测（onnxruntime 等）；注意 `basic_pitch/__init__.py` 无 else 分支，后端全缺会模块级 `NameError` → 按 AiDependencyError(6) 口径捕获 | 提示 `pip install basic-pitch --no-deps` + `resampy mir-eval onnxruntime` 三步（见 requirements/ai.txt 2026-09-22 实测结论） |

验收：doctor 输出三段新探测；未装任何可选依赖时 doctor 仍整体成功（退出码 0），
只是逐项标 `[warn]`；单测用 monkeypatch 模拟命中/未命中两路（≥6 例）。

### 2. F3 `video/cli.py` 转发面测试【覆盖率 29% → ~60%】

1.4.5 新转发了 `presets` / `config` / `version` 三个子命令但无测试。补 CliRunner
用例（放 `tests/test_video_cli.py` 或并入现有 video 测试文件）：

- `sunoaux post video presets`：退出码 0，输出含 douyin/youtube/instagram/official；
- `sunoaux post video config`：打印默认配置（含平台预设字段）；
- `sunoaux post video version`：打印版本且与 `sunoauxtool.video.__version__` 一致；
- `render` 缺参数走参数校验分支（退出码非 0 即可，不真渲染）；
- 未知子命令 → typer 自身报错路径。

验收：`video/cli.py` 覆盖率 ≥60%，全套件覆盖率不降（门槛 87% 保持）。

### 3. L3 死代码清理【一行级】

`pipeline.py:183-184`：`tmp_dir` 创建后从未使用（白建白删一个 mkdtemp）。删除
这两行 + 确认无副作用引用；`tests/test_pipeline.py` 现有 14 例守护。

### 4. 登记项（不在 1.4.7 实施，写入模块文档/审计 L 表）

- **beat 相位精度上限**：onset 包络用非中心 STFT，flux 峰按「首个包含起音的窗」
  起始帧计时，相位固有抖动 ±2 帧（hop=512 时约 ±46ms）；1.4.6 已做半窗中心
  校正（误差收敛到 ±18ms），进一步精度需改 `frame_view` 为 centered-STFT
  （牵动 spectral 全模块，单独立项评估）。
- B6 torch.load `weights_only`：留作 torch ≥2.6 升级前置检查（F6 清单）。

## 执行顺序与验证

1. L3（1 行）→ F3（纯测试）→ F1（代码+测试），每步单独提交；
2. 全量套件 + `--cov-fail-under=87` + ruff + CHANGELOG guard（版本写 1.4.7）；
3. CHANGELOG 补 [1.4.7] 条目；commit + push（github.com:443 不通时走 API 兜底
   `push_single_commit_by_api.py`）；tag v1.4.7，GitHub Release（不发 wheel，惯例）。

预估：半天内完成；无破坏性变更，CHANGELOG 不含「移除」节。
