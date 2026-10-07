# SunoAuxTool 环境整理与升级概览（2026-08-14）

## 已完成

1. **项目自身软件同步到 v0.5.2**
   - 问题：`pip` 元数据停留在 `0.1.0`，与代码/CHANGELOG 的 `0.5.2` 不一致。
   - 处理：`pip install -e . --no-deps` 重建 editable 元数据（不触碰已锁定的第三方依赖）。
   - 验证：`python -m sunoauxtool --version` → `sunoauxtool 0.5.2` ✅

2. **venv 清理：删除 25 个损坏安装残留**
   - 现象：`pip` 报 `Ignoring invalid distribution ~...`（中断安装留下的 `~` 前缀目录）。
   - 处理：移除 `venv/Lib/site-packages` 下全部 25 个 `~`-前缀目录；真实包（numpy/torch/numba/antlr4/easel）完好。

3. **项目构建/缓存产物清理（均已被 .gitignore 忽略，可再生产）**
   - 删除根目录 `__pycache__/`、`src/sunoauxtool/__pycache__/`、`dist/`（构建产物）、`desktop.ini`（Windows 杂项）。

4. **docs 整理**
   - 14 个历史规划/过程文档（PRD*/task-plan*/QA-acceptance*/research-P3/plan-*/旧 mermaid/architecture-P1P2）归档至 `docs/archive/`。
   - 保留在线文档：`usage.md`、`ai-integration.md`（`architecture.md`/`architecture-P3.md` 已于 2026-09-22 归档至 `docs/archive/`）。

5. **环境就绪验证**
   - `doctor`：Python / FluidSynth / 双 SoundFont / torch / CUDA / audiocraft / espeak 全部 ✅，结论「全部正常」。
   - `generate midi` 冒烟：成功生成 `output/default/20260814/pop_120_7_1.mid` ✅

## 关键决策

- **未盲目升级第三方依赖**。环境已处于项目锁定的精确版本（base/dev/ai 锁定的 typer、music21、numpy、torch 2.5.1+cu121、audiocraft 等完全一致）。盲升会破坏脆弱的 cu121 torch / audiocraft（git 依赖）/ xformers 环境，得不偿失。
- **`xformers` 与 torch 对齐**（2026-09-20 修正）：torch 仍钉 `2.5.1+cu121` **不动**，改把 xformers 修正为与它配对的 `0.0.28.post3`——原先误装的是面向 torch 2.6.0 的 `0.0.29.post3`，只会打印 `xFormers can't load C++/CUDA extensions` 并静默退化（内存高效注意力不可用）。这么选的理由同上：升 torch 等于动 audiocraft / DiffRhythm 整条链路，降 xformers 只动一个包且可回退。安装命令与索引说明见 `requirements/ai.txt`。
- 「我的软件更新到最新版本」= 项目自身包 `sunoauxtool` 的元数据同步（0.1.0 → 0.5.2），而非第三方依赖。

## 已知缺口（可选，非核心管线）

- **DiffRhythm 未安装**：需手动 `git clone` 官方仓库 + 下载约 7.5GB 权重，仅影响 `ai diffrhythm` 歌曲草稿命令，核心 generate→render→export 链路不受影响。

## 未动（属用户数据 / 运行时，需谨慎）

- `output/`（约 323MB 已生成音乐结果，如空间紧张可手动清理旧日期目录）
- `sunoauxtool.db`（灵感库 SQLite，运行态数据）
- `module/`（约 8GB：FluidSynth 二进制 + SoundFont + 权重，运行时必需）

## 运行方式

```bash
venv\Scripts\activate
sunoauxtool pipeline          # 零参数 demo：生成→渲染→导出
sunoauxtool doctor            # 环境健康检查
```
