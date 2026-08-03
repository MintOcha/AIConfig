#!/usr/bin/env python3
"""Create a timestamped frame contact sheet for visual video inspection."""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import cv2
import numpy as np

from video_tools import FrameReader, resize_contain


def timestamp(seconds: float) -> str:
    milliseconds = round(seconds * 1000)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}.{milliseconds:03d}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--start", type=float, default=0.0)
    parser.add_argument("--duration", type=float)
    parser.add_argument("--interval", type=float, default=4.0)
    parser.add_argument("--columns", type=int, default=5)
    parser.add_argument("--thumb-width", type=int, default=320)
    parser.add_argument("--thumb-height", type=int)
    args = parser.parse_args()

    if args.interval <= 0 or args.columns <= 0 or args.thumb_width <= 0:
        raise ValueError("Interval, columns, and thumbnail width must be positive")
    reader = FrameReader(args.input)
    try:
        end = reader.info.duration if args.duration is None else min(reader.info.duration, args.start + args.duration)
        if args.start < 0 or end <= args.start:
            raise ValueError("The requested sampling range is empty")
        times = np.arange(args.start, end, args.interval, dtype=float).tolist()
        thumb_height = args.thumb_height or round(args.thumb_width * reader.info.height / reader.info.width)
        rows = math.ceil(len(times) / args.columns)
        sheet = np.zeros((rows * thumb_height, args.columns * args.thumb_width, 3), dtype=np.uint8)
        for index, time_seconds in enumerate(times):
            frame = resize_contain(reader.get(time_seconds), (args.thumb_width, thumb_height))
            label = timestamp(time_seconds)
            scale = max(0.45, thumb_height / 360)
            thickness = max(1, round(thumb_height / 180))
            text_size, baseline = cv2.getTextSize(label, cv2.FONT_HERSHEY_DUPLEX, scale, thickness)
            cv2.rectangle(frame, (0, 0), (text_size[0] + 12, text_size[1] + baseline + 10), (18, 18, 18), -1)
            cv2.putText(frame, label, (6, text_size[1] + 4), cv2.FONT_HERSHEY_DUPLEX, scale, (245, 245, 245), thickness, cv2.LINE_AA)
            row, column = divmod(index, args.columns)
            y0 = row * thumb_height
            x0 = column * args.thumb_width
            sheet[y0 : y0 + thumb_height, x0 : x0 + args.thumb_width] = frame
        if not args.output.parent.is_dir():
            raise ValueError(f"Output directory does not exist: {args.output.parent}")
        if not cv2.imwrite(str(args.output), sheet):
            raise RuntimeError(f"Unable to write contact sheet: {args.output}")
    finally:
        reader.close()


if __name__ == "__main__":
    main()
