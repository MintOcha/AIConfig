#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 || ! -f "$1" ]]; then
  echo "Usage: $0 VIDEO" >&2
  exit 2
fi

ffprobe -v error \
  -show_entries format=duration,size \
  -show_entries stream=index,codec_type,codec_name,profile,width,height,pix_fmt,r_frame_rate,nb_frames,sample_rate,channels \
  -of default=noprint_wrappers=1 \
  "$1"

ffmpeg -hide_banner -v error -i "$1" -map 0:v:0 -map '0:a:0?' -f null -
