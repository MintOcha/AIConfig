#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 || $# -gt 3 ]]; then
  echo "Usage: $0 INPUT OUTPUT [libx264|libsvtav1|av1_qsv]" >&2
  exit 2
fi

input_path=$1
output_path=$2
encoder=${3:-libx264}

if [[ ! -f "$input_path" || "$input_path" == "$output_path" ]]; then
  echo "Input must exist and differ from output." >&2
  exit 2
fi

case "$encoder" in
  libx264)
    video_options=(-c:v libx264 -crf 18 -preset slow -pix_fmt yuv420p)
    ;;
  libsvtav1)
    video_options=(-c:v libsvtav1 -crf 30 -preset 6 -pix_fmt yuv420p10le)
    ;;
  av1_qsv)
    if ! ffmpeg -hide_banner -encoders 2>/dev/null | grep -qE '[[:space:]]av1_qsv[[:space:]]'; then
      echo "This FFmpeg build does not provide the av1_qsv encoder." >&2
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
