#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 || $# -gt 3 ]]; then
  echo "Usage: $0 INPUT OUTPUT [auto|libx264|libsvtav1|av1_qsv]" >&2
  exit 2
fi

input_path=$1
output_path=$2
encoder=${3:-auto}

if [[ ! -f "$input_path" || "$input_path" == "$output_path" ]]; then
  echo "Input must exist and differ from output." >&2
  exit 2
fi

supports_saved_qsv_profile() {
  ffmpeg -hide_banner -loglevel error \
    -f lavfi -i color=c=black:s=64x64:r=1 \
    -frames:v 1 -an \
    -c:v av1_qsv \
    -global_quality 30 \
    -preset slow \
    -look_ahead 1 \
    -look_ahead_depth 80 \
    -f null - >/dev/null 2>&1
}

if [[ "$encoder" == "auto" ]]; then
  if supports_saved_qsv_profile; then
    encoder=av1_qsv
    echo "Using the saved Intel AV1 QSV quality profile." >&2
  else
    encoder=libx264
    echo "Intel AV1 QSV is unavailable; using slow quality-based H.264." >&2
  fi
fi

case "$encoder" in
  libx264)
    video_options=(-c:v libx264 -crf 18 -preset slow -pix_fmt yuv420p)
    ;;
  libsvtav1)
    video_options=(-c:v libsvtav1 -crf 30 -preset 6 -pix_fmt yuv420p10le)
    ;;
  av1_qsv)
    if ! supports_saved_qsv_profile; then
      echo "The saved av1_qsv profile is unsupported by this FFmpeg/GPU combination." >&2
      exit 3
    fi
    video_options=(-c:v av1_qsv -global_quality 30 -preset slow -look_ahead 1 -look_ahead_depth 80)
    ;;
  *)
    echo "Unsupported encoder: $encoder" >&2
    exit 2
    ;;
esac

ffmpeg -hide_banner -y \
  -i "$input_path" \
  -map 0:v:0 -map '0:a:0?' \
  "${video_options[@]}" \
  -c:a aac -b:a 64k \
  -movflags +faststart \
  "$output_path"
