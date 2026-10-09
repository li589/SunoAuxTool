"""滚动谱面可视化测试（#14）。

覆盖：
- 注册表与工厂（create_visualizer("score") / 未知风格报错文案不受影响）
- ScoreVisualizer staff / jianpu 双模式逐帧渲染（形状、dtype、高亮、播放头）
- 节拍网格（beat_times）绘制
- 无谱面数据提示帧
- ``_dia_of_pitch`` 音级映射（黑键落下方自然音线位）
- ``video()`` 装配路径：style=score 缺 MIDI 谱面 → RenderError
"""

from __future__ import annotations

import numpy as np
import pytest
import soundfile as sf

from sunoauxtool.video.config import Config
from sunoauxtool.video.exceptions import RenderError
from sunoauxtool.video.visuals import ScoreVisualizer, VisualContext, create_visualizer
from sunoauxtool.video.visuals.score import _dia_of_pitch

BASE_COLOR = (74, 158, 255)  # Config 默认 #4a9eff


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

def _make_score():
    """两小节 4/4 最小谱（melody + bass），bpm=120（1 拍 = 0.5s）。"""
    from sunoauxtool.models.notes import Note, NoteSequence
    from sunoauxtool.score import Score

    seq = NoteSequence(bpm=120, key="C major", time_signature="4/4", bars=2)
    seq.add_track(
        "melody", 0, 0,
        [
            Note(pitch=72, start=0.0, duration=1.0, velocity=80),
            Note(pitch=74, start=1.0, duration=1.0, velocity=80),
            Note(pitch=76, start=2.0, duration=2.0, velocity=80),
            Note(pitch=67, start=4.0, duration=4.0, velocity=80),
        ],
    )
    seq.add_track(
        "bass", 32, 2,
        [
            Note(pitch=36, start=0.0, duration=4.0, velocity=70),
            Note(pitch=41, start=4.0, duration=4.0, velocity=70),
        ],
    )
    return Score.from_sequence(seq)


@pytest.fixture(scope="module")
def score():
    return _make_score()


def _ctx(w=480, h=320, fps=30):
    return VisualContext(width=w, height=h, fps=fps, analysis=None, background=None)


def _has_color(arr: np.ndarray, color, tol=0) -> bool:
    if tol == 0:
        return bool(np.any(np.all(arr == color, axis=-1)))
    diff = np.abs(arr.astype(int) - np.array(color)).max(axis=-1)
    return bool(np.any(diff <= tol))


# ---------------------------------------------------------------------------
# 注册表与工厂
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_score_registered(self):
        viz = create_visualizer("score", Config())
        assert isinstance(viz, ScoreVisualizer)
        assert viz.get_style_name() == "score"

    def test_style_case_insensitive(self):
        assert isinstance(create_visualizer("Score", Config()), ScoreVisualizer)

    def test_unknown_style_still_raises(self):
        with pytest.raises(ValueError, match="score"):
            create_visualizer("nonexistent", Config())


# ---------------------------------------------------------------------------
# 音级映射
# ---------------------------------------------------------------------------

class TestPitchMapping:
    def test_white_keys(self):
        assert _dia_of_pitch(60) == 28   # C4
        assert _dia_of_pitch(69) == 33   # A4
        assert _dia_of_pitch(36) == 14   # C2

    def test_black_key_maps_to_lower_natural(self):
        assert _dia_of_pitch(61) == 28   # C#4 → C4 线位
        assert _dia_of_pitch(66) == 31   # F#4 → F4 线位


# ---------------------------------------------------------------------------
# 逐帧渲染
# ---------------------------------------------------------------------------

class TestRender:
    def test_frame_shape_staff(self, score):
        viz = ScoreVisualizer(Config(), score=score)
        frame = viz.render_frame(_ctx(), 0)
        assert frame.shape == (320, 480, 3)
        assert frame.dtype == np.uint8

    def test_frame_shape_jianpu(self, score):
        viz = ScoreVisualizer(Config(), score=score, notation="jianpu")
        frame = viz.render_frame(_ctx(), 0)
        assert frame.shape == (320, 480, 3)
        assert frame.dtype == np.uint8

    def test_invalid_notation_falls_back_to_staff(self, score):
        viz = ScoreVisualizer(Config(), score=score, notation="bogus")
        assert viz.notation == "staff"

    def test_active_note_highlighted(self, score):
        """t=0（frame 0）有音正响 → 出现主色精确像素。"""
        viz = ScoreVisualizer(Config(), score=score)
        frame = viz.render_frame(_ctx(), 0)
        assert _has_color(frame, BASE_COLOR)

    def test_no_highlight_in_gap(self, score):
        """t=4.25s（末音 4.0+4.0 拍 = 2.0-4.0s 已结束）→ 谱面区无 active 主色。

        头部 BPM 标注常驻主色，故只探测头部以下的谱面区。
        """
        viz = ScoreVisualizer(Config(), score=score)
        ctx = _ctx()
        frame = viz.render_frame(ctx, int(4.25 * 30))
        assert not _has_color(frame[int(ctx.height * 0.12):], BASE_COLOR)

    def test_playhead_centered_mid_song(self, score):
        """t=2.0s 播放头应在画面中线（全曲 4s，窗口 4s，t_start=0）。"""
        viz = ScoreVisualizer(Config(), score=score)
        ctx = _ctx()
        frame = viz.render_frame(ctx, 60)  # t = 2.0s
        col = np.expand_dims(frame[:, ctx.width // 2], 0)  # (1, H, 3)
        assert _has_color(col, (255, 255, 255))

    def test_bpm_override(self, score):
        viz = ScoreVisualizer(Config(), score=score, bpm=60)
        assert viz.sec_per_beat == pytest.approx(1.0)

    def test_window_indices_sorted_access(self, score):
        viz = ScoreVisualizer(Config(), score=score)
        for f in range(0, 150, 7):  # 扫几帧确保 bisect 窗口不出界
            assert viz.render_frame(_ctx(), f).shape[0] == 320


# ---------------------------------------------------------------------------
# 节拍网格
# ---------------------------------------------------------------------------

class TestBeatGrid:
    def test_ruler_drawn(self, score):
        beat_times = np.arange(0.0, 4.0, 0.5)  # 120 BPM → 每拍 0.5s
        viz_with = ScoreVisualizer(Config(), score=score, beat_times=beat_times)
        viz_without = ScoreVisualizer(Config(), score=score)
        ctx = _ctx()
        f_with = viz_with.render_frame(ctx, 30)
        f_without = viz_without.render_frame(ctx, 30)
        # 底部节拍尺区（强拍刻度为主色）只在有网格时出现
        bottom_with = f_with[int(ctx.height * 0.9):]
        bottom_without = f_without[int(ctx.height * 0.9):]
        assert _has_color(bottom_with, BASE_COLOR)
        assert not _has_color(bottom_without, BASE_COLOR)

    def test_bpm_label_annotated(self, score):
        beat_times = np.arange(0.0, 4.0, 0.5)
        viz = ScoreVisualizer(Config(), score=score, beat_times=beat_times)
        ctx = _ctx()
        frame = viz.render_frame(ctx, 0)
        # 头部右侧出现主色（BPM 标注文字）
        assert _has_color(frame[: int(ctx.height * 0.12)], BASE_COLOR, tol=40)


# ---------------------------------------------------------------------------
# 无谱面数据
# ---------------------------------------------------------------------------

class TestFallback:
    def test_none_score_hint_frame(self):
        viz = ScoreVisualizer(Config(), score=None)
        frame = viz.render_frame(_ctx(), 0)
        assert frame.shape == (320, 480, 3)
        # 有提示文字 → 存在偏离背景色的像素（背景色本身仍在）
        bg = np.array((26, 26, 46))
        assert bool(np.any(np.abs(frame.astype(int) - bg).max(axis=-1) > 0))

    def test_empty_score_renders_hint(self):
        from sunoauxtool.models.notes import NoteSequence
        from sunoauxtool.score import Score

        seq = NoteSequence(bpm=120, key="C major", time_signature="4/4", bars=1)
        score = Score.from_sequence(seq)
        viz = ScoreVisualizer(Config(), score=score)
        frame = viz.render_frame(_ctx(), 0)
        assert frame.shape == (320, 480, 3)


# ---------------------------------------------------------------------------
# video() 装配路径
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def tone_wav(tmp_path_factory):
    p = tmp_path_factory.mktemp("tone") / "tone.wav"
    sr = 22050
    t = np.linspace(0, 2.0, int(sr * 2.0), endpoint=False)
    audio = (0.4 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    sf.write(str(p), audio, sr)
    return p


class TestVideoWiring:
    def test_score_style_without_midi_raises(self, tone_wav, tmp_path):
        from sunoauxtool.video.videomaker import video

        with pytest.raises(RenderError, match="score-midi"):
            video(
                str(tone_wav),
                str(tmp_path / "out.mp4"),
                visual_style="score",
            )

    def test_score_style_with_missing_midi_file_raises(self, tone_wav, tmp_path):
        from sunoauxtool.video.videomaker import video

        with pytest.raises(Exception):  # InputFileError（码 3）
            video(
                str(tone_wav),
                str(tmp_path / "out.mp4"),
                visual_style="score",
                score_midi=str(tmp_path / "missing.mid"),
            )
