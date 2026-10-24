"""``soundbank`` 子命令（2.0 PR-G）：列出/查看/校验音源库。

    sunoaux soundbank list [--json]
    sunoaux soundbank info <bank_id> [--json]
    sunoaux soundbank verify [<bank_id>] [--all] [--json]

只做**登记与校验**：不下载、不内置商业音源（计划书 N4）。
校验是结构性的（存在性 + 体积 + RIFF/sfbk 魔数）——深校验（真加载）由渲染时暴露。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from sunoauxtool.commands.helpers import _guard
from sunoauxtool.soundbank.registry import SoundBank, SoundBankRegistry, build_registry

app = typer.Typer(
    help="音源库：列出 / 查看 / 校验 SF2（只登记不下载）",
    no_args_is_help=True,
    add_completion=False,
)


def _registry(extra_path: Optional[Path] = None) -> SoundBankRegistry:
    """构造注册表（可用 ``--bank`` 追加一个显式音源）。"""
    extras = []
    if extra_path is not None:
        extras.append(SoundBank(
            id=extra_path.stem.lower(), name=extra_path.stem, path=extra_path,
            format="sf2",
        ))
    return build_registry(extra=extras)


def _row(bank: SoundBank) -> str:
    result = bank.verify()
    mark = "OK  " if result.ok else "FAIL"
    return (
        f"{mark} {bank.id:<24} {bank.format:<4} {bank.size_mb:>6.1f}MB  "
        f"{bank.name[:28]:<28} {bank.license}"
    )


@app.command("list")
@_guard
def list_banks(
    as_json: bool = typer.Option(False, "--json", help="以 JSON 输出"),
    soundbank: Optional[Path] = typer.Option(
        None, "--bank", help="额外登记一个音源文件（不扫描也能看到）"
    ),
) -> None:
    """列出已发现的音源（含结构校验结果）。"""
    registry = _registry(soundbank)
    banks = registry.list()
    if as_json:
        typer.echo(json.dumps(
            [dict(bank.to_dict(), verify=bank.verify().ok) for bank in banks],
            ensure_ascii=False,
        ))
        return
    if not banks:
        typer.echo("未发现任何音源（把 .sf2 放进 module/ 或配置 [paths] soundfont）")
        return
    typer.echo(f"共 {len(banks)} 套音源（module/ 扫描）")
    for bank in banks:
        typer.echo(f"  {_row(bank)}")


@app.command("info")
@_guard
def info(
    bank_id: str = typer.Argument(..., help="音源 id（soundbank list 里第一列）"),
    as_json: bool = typer.Option(False, "--json", help="以 JSON 输出"),
) -> None:
    """查看某套音源的详情。"""
    bank = _registry().get(bank_id)
    result = bank.verify()
    if as_json:
        typer.echo(json.dumps(
            {**bank.to_dict(), "verify": {"ok": result.ok, "code": result.code,
                                          "message": result.message}},
            ensure_ascii=False,
        ))
        return
    typer.echo(f"{bank.name}（{bank.id}）")
    typer.echo(f"  路径   {bank.path}")
    typer.echo(f"  格式   {bank.format}｜{bank.size_mb} MB")
    typer.echo(f"  许可   {bank.license}")
    typer.echo(f"  校验   {result.describe()}")
    if bank.description:
        typer.echo(f"  来源   {bank.description}")


@app.command("verify")
@_guard
def verify(
    bank_id: Optional[str] = typer.Argument(None, help="音源 id；省略或 --all 时校验全部"),
    all_banks: bool = typer.Option(False, "--all", help="校验全部已发现音源"),
    as_json: bool = typer.Option(False, "--json", help="以 JSON 输出"),
) -> None:
    """结构校验（存在性/体积/魔数）；失败返回 41。"""
    registry = _registry()
    if bank_id is None or all_banks:
        results = registry.verify_all()
    else:
        bank = registry.get(bank_id)
        results = {bank.id: bank.verify()}

    failed = [name for name, result in results.items() if not result.ok]
    if as_json:
        typer.echo(json.dumps(
            {name: {"ok": result.ok, "code": result.code, "message": result.message}
             for name, result in results.items()},
            ensure_ascii=False,
        ))
    else:
        for name, result in results.items():
            typer.echo(f"  {name:<24} {result.describe()}")
        typer.echo(f"合计 {len(results)} 套，失败 {len(failed)} 套")

    if failed:
        raise typer.Exit(41)
