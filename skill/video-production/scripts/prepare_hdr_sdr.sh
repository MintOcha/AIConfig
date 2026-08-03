#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "Usage: $0 INPUT OUTPUT" >&2
  exit 2
fi

input_path=$1
output_path=$2

if [[ ! -f "$input_path" || "$input_path" == "$output_path" ]]; then
  echo "Input must exist and differ from output." >&2
  exit 2
fi

ffmpeg -hide_banner -y \
  -i "$input_path" \
  -map 0:v:0 -map '0:a:0?' \
  -vf 'zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv422p10le' \
  -c:v prores_ks -profile:v 2 -pix_fmt yuv422p10le -vendor apl0 \
  -color_primaries bt709 -color_trc bt709 -colorspace bt709 \
  -c:a pcm_s24le \
  "$output_path"
