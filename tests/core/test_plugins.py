"""统一插件 / 扩展发现（R12）测试。

用 ``monkeypatch`` 替换 ``sunoauxtool.plugins.entry_points`` 注入伪 entry point，
覆盖：builtins 保底、插件追加、坏插件隔离、类型校验、``instantiate=False`` 保留类、
五个扩展点的「发现不崩」冒烟，以及 ``create_visualizer`` 的 ``**extra`` 透传回归。
"""

from __future__ import annotations

import warnings

import numpy as np
import pytest

from sunoauxtool import plugins


class _FakeEP:
    """伪 entry point：``load()`` 返回 ``loader()``（loader 可抛异常）。"""

    def __init__(self, name: str, loader):
        self.name = name
        self._loader = loader

    def load(self):
        return self._loader()


def _patch(monkeypatch, eps):
    monkeypatch.setattr(plugins, "entry_points", lambda group=None: list(eps))


class _Base:
    """类型校验用的基类。"""


class _Ok(_Base):
    pass


class _Other:
    pass


# -- discover() 语义 -------------------------------------------------------


def test_group_of():
    assert plugins.group_of("download_sources") == "sunoauxtool.download_sources"
    assert plugins.GROUP_PREFIX == "sunoauxtool."


def test_discover_builtins_only(monkeypatch):
    _patch(monkeypatch, [])
    assert plugins.discover("whatever", {"a": 1}) == {"a": 1}


def test_discover_appends_plugin(monkeypatch):
    _patch(monkeypatch, [_FakeEP("plug", lambda: _Ok)])
    out = plugins.discover("x", {"a": 1}, base=_Base)
    assert out["a"] == 1
    assert isinstance(out["plug"], _Ok)  # 载入的是类 -> 被实例化


def test_discover_instantiate_false_keeps_class(monkeypatch):
    _patch(monkeypatch, [_FakeEP("plug", lambda: _Ok)])
    out = plugins.discover("x", {}, base=_Base, instantiate=False)
    assert out["plug"] is _Ok  # 保留类本身


def test_discover_broken_plugin_isolated(monkeypatch):
    def boom():
        raise RuntimeError("坏插件")

    _patch(monkeypatch, [_FakeEP("bad", boom)])
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        out = plugins.discover("x", {"a": 1}, base=_Base)
    assert out == {"a": 1}  # 内置能力不受影响
    assert any("加载失败" in str(w.message) for w in caught)


def test_discover_wrong_type_skipped(monkeypatch):
    _patch(monkeypatch, [_FakeEP("wrong", lambda: _Other)])
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        out = plugins.discover("x", {"a": 1}, base=_Base)
    assert out == {"a": 1}
    assert any("已跳过" in str(w.message) for w in caught)


# -- 各扩展点注册表 --------------------------------------------------------


def test_download_sources_registry():
    from sunoauxtool.download.sources.base import discover_sources, list_sources

    reg = discover_sources()
    assert set(reg) == {"catcatch", "suno-api", "haimeng", "tianyin"}
    assert len(list_sources()) == 4  # 兼容旧签名仍在


def test_video_visuals_registry_keeps_classes():
    from sunoauxtool.video.visuals import discover_visuals

    reg = discover_visuals()
    assert {"score", "reactive", "tracks"} <= set(reg)
    assert all(isinstance(v, type) for v in reg.values())


def test_transcribe_backends_registry():
    from sunoauxtool.analysis import discover_transcribe_backends

    reg = discover_transcribe_backends()
    assert "basic-pitch" in reg
    assert reg["bp"] is reg["basic-pitch"]  # 别名同指


def test_ai_and_render_registries():
    from sunoauxtool.ai import discover_ai_backends
    from sunoauxtool.render import discover_render_engines

    assert {"musicgen", "diffrhythm"} <= set(discover_ai_backends())
    assert "fluidsynth" in discover_render_engines()


def test_smoke_all_points_survive_broken_plugin(monkeypatch):
    """CI 冒烟：五个扩展点在「存在坏插件」时仍能发现全部内置项，不崩。"""

    def boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(
        plugins, "entry_points", lambda group=None: [_FakeEP("bad", boom)]
    )
    from sunoauxtool.ai import discover_ai_backends
    from sunoauxtool.analysis import discover_transcribe_backends
    from sunoauxtool.download.sources.base import discover_sources
    from sunoauxtool.render import discover_render_engines
    from sunoauxtool.video.visuals import discover_visuals

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert "catcatch" in discover_sources()
        assert "score" in discover_visuals()
        assert "basic-pitch" in discover_transcribe_backends()
        assert "musicgen" in discover_ai_backends()
        assert "fluidsynth" in discover_render_engines()


# -- 回归：create_visualizer 必须透传 **extra ------------------------------
# （score 样式依赖 score/beat_times/bpm/notation 送达构造器）


def test_create_visualizer_passes_extra(monkeypatch):
    from sunoauxtool.video.config import Config
    from sunoauxtool.video.visuals import Visualizer, create_visualizer

    captured: dict = {}

    class _Probe(Visualizer):
        def __init__(self, config, **kw):
            super().__init__(config)
            captured.update(kw)

        def render_frame(self, ctx, frame_idx):
            return np.zeros((4, 4, 3), dtype=np.uint8)

        def get_style_name(self):
            return "probe"

    _patch(monkeypatch, [_FakeEP("probe", lambda: _Probe)])
    viz = create_visualizer("probe", Config(), score="S", bpm=120)

    assert isinstance(viz, _Probe)
    assert captured == {"score": "S", "bpm": 120}


def test_create_visualizer_unknown_style_raises():
    from sunoauxtool.video.config import Config
    from sunoauxtool.video.visuals import create_visualizer

    with pytest.raises(ValueError, match="未知 PIL 创意层风格"):
        create_visualizer("definitely-not-a-style", Config())
