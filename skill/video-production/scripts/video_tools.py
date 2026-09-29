#!/usr/bin/env python3
"""Shared aspect-safe primitives for bundled video-production scripts."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np


@dataclass(frozen=True)
class VideoInfo:
    width: int
    height: int
    fps: float
    frame_count: int

    @property
    def duration(self) -> float:
        return self.frame_count / self.fps


def open_video(path: Path) -> tuple[cv2.VideoCapture, VideoInfo]:
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise RuntimeError(f"Unable to open video: {path}")
    width = round(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = round(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = capture.get(cv2.CAP_PROP_FPS)
    frame_count = round(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    if width <= 0 or height <= 0 or fps <= 0 or frame_count <= 0:
        capture.release()
        raise RuntimeError(f"Invalid video metadata: {path}")
    return capture, VideoInfo(width, height, fps, frame_count)


class FrameReader:
    def __init__(self, path: Path) -> None:
        self.capture, self.info = open_video(path)
        self.index = -1
        self.frame: np.ndarray | None = None

    def get(self, time_seconds: float) -> np.ndarray:
        target = max(0, min(self.info.frame_count - 1, round(time_seconds * self.info.fps)))
        if target < self.index or target > self.index + 8:
            self.capture.set(cv2.CAP_PROP_POS_FRAMES, target)
            self.index = target - 1
        while self.index < target:
            ok, frame = self.capture.read()
            if not ok:
                raise RuntimeError(f"Unable to read frame {target}")
            self.index += 1
            self.frame = frame
        if self.frame is None:
            raise RuntimeError(f"Unable to read frame {target}")
        return self.frame

    def close(self) -> None:
        self.capture.release()


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def smoothstep(value: float) -> float:
    value = clamp(value)
    return value * value * (3.0 - 2.0 * value)


def envelope(time_seconds: float, start: float, end: float, fade: float) -> float:
    return min(smoothstep((time_seconds - start) / fade), smoothstep((end - time_seconds) / fade))


def blend(base: np.ndarray, overlay: np.ndarray, alpha: np.ndarray | float) -> np.ndarray:
    alpha_array = np.asarray(alpha, dtype=np.float32)
    if alpha_array.ndim == 2:
        alpha_array = alpha_array[:, :, None]
    return np.clip(
        base.astype(np.float32) * (1.0 - alpha_array) + overlay.astype(np.float32) * alpha_array,
        0,
        255,
    ).astype(np.uint8)


def resize_cover(frame: np.ndarray, size: tuple[int, int], center: tuple[float, float] = (0.5, 0.5)) -> np.ndarray:
    target_width, target_height = size
    source_height, source_width = frame.shape[:2]
    if source_width / source_height > target_width / target_height:
        crop_height = source_height
        crop_width = round(crop_height * target_width / target_height)
    else:
        crop_width = source_width
        crop_height = round(crop_width * target_height / target_width)
    x0 = round(center[0] * source_width - crop_width / 2)
    y0 = round(center[1] * source_height - crop_height / 2)
    x0 = max(0, min(source_width - crop_width, x0))
    y0 = max(0, min(source_height - crop_height, y0))
    crop = frame[y0 : y0 + crop_height, x0 : x0 + crop_width]
    return cv2.resize(crop, size, interpolation=cv2.INTER_LANCZOS4)


def resize_contain(frame: np.ndarray, size: tuple[int, int], color: tuple[int, int, int] = (0, 0, 0)) -> np.ndarray:
    target_width, target_height = size
    source_height, source_width = frame.shape[:2]
    scale = min(target_width / source_width, target_height / source_height)
    fitted_width = max(1, round(source_width * scale))
    fitted_height = max(1, round(source_height * scale))
    fitted = cv2.resize(frame, (fitted_width, fitted_height), interpolation=cv2.INTER_LANCZOS4)
    canvas = np.full((target_height, target_width, 3), color, dtype=np.uint8)
    x0 = (target_width - fitted_width) // 2
    y0 = (target_height - fitted_height) // 2
    canvas[y0 : y0 + fitted_height, x0 : x0 + fitted_width] = fitted
    return canvas


def start_encoder(
    source: Path,
    output: Path,
    info: VideoInfo,
    crf: int,
    preset: str,
    audio_bitrate: str,
) -> subprocess.Popen[bytes]:
    if source.resolve() == output.resolve():
        raise ValueError("Input and output paths must differ")
    if not output.parent.is_dir():
        raise ValueError(f"Output directory does not exist: {output.parent}")
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{info.width}x{info.height}",
        "-r", f"{info.fps:.8f}", "-i", "pipe:0", "-i", str(source),
        "-map", "0:v:0", "-map", "1:a:0?", "-t", f"{info.duration:.8f}",
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", audio_bitrate, "-movflags", "+faststart", str(output),
    ]
    return subprocess.Popen(command, stdin=subprocess.PIPE)


def finish_encoder(encoder: subprocess.Popen[bytes]) -> None:
    if encoder.stdin is not None:
        encoder.stdin.close()
    status = encoder.wait()
    if status != 0:
        raise RuntimeError(f"FFmpeg exited with status {status}")
