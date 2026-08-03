#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "Usage: $0 INPUT OUTPUT" >&2
  exit 2
fi

input_path=$1
output_path=$2

if [[ ! -f "$input_path" ]]; then
  echo "Input file does not exist: $input_path" >&2
  exit 2
fi

if [[ "$input_path" == "$output_path" ]]; then
  echo "Input and output paths must differ." >&2
  exit 2
fi

output_parent=$(dirname "$output_path")
if [[ ! -d "$output_parent" ]]; then
  echo "Output directory does not exist: $output_parent" >&2
  exit 2
fi

if ! ffmpeg -hide_banner -encoders 2>/dev/null | grep -qE '[[:space:]]av1_qsv[[:space:]]'; then
  echo "This FFmpeg build does not provide the av1_qsv encoder." >&2
  exit 3
fi

ffmpeg -hide_banner -y \
  -i "$input_path" \
  -map 0:v:0 -map '0:a:0?' \
  -c:v av1_qsv \
  -global_quality 30 \
  -preset slow \
  -look_ahead 1 \
  -look_ahead_depth 80 \
  -c:a aac \
  -b:a 64k \
  -movflags +faststart \
  "$output_path"
