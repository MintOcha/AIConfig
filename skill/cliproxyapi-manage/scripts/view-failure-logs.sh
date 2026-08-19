#!/usr/bin/env bash

set -euo pipefail

minutes="${1:-60}"
limit="${2:-50}"

if ! [[ "$minutes" =~ ^[1-9][0-9]*$ && "$limit" =~ ^[1-9][0-9]*$ ]]; then
  echo "Usage: $0 [minutes] [limit]" >&2
  exit 2
fi

proxy_container="${PROXY_CONTAINER:-cli-proxy-api}"
postgres_container="${POSTGRES_CONTAINER:-cliproxyapi-postgres}"
display_timezone="${DISPLAY_TIMEZONE:-Asia/Shanghai}"

for command_name in docker date awk sed; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "Required command not found: $command_name" >&2
    exit 1
  fi
done

query=$(
  cat <<SQL
SELECT to_char("timestamp", 'YYYY-MM-DD HH24:MI:SS.MS'),
       model,
       "latencyMs",
       "inputTokens",
       "outputTokens",
       "reasoningTokens",
       "totalTokens"
FROM usage_records
WHERE failed = true
  AND "timestamp" >= (CURRENT_TIMESTAMP AT TIME ZONE 'UTC') - interval '${minutes} minutes'
ORDER BY "timestamp" DESC
LIMIT ${limit};
SQL
)

records="$(
  docker exec "$postgres_container" sh -c \
    'psql -X -q -A -t -F "|" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"' \
    sh "$query"
)"

if [[ -z "$records" ]]; then
  echo "No failed usage records found in the last ${minutes} minutes."
  exit 0
fi

printf '%-23s %-18s %-6s %-22s %-8s %-10s %s\n' \
  "REQUESTED" "REQUEST ID" "HTTP" "ENDPOINT" "LATENCY" "TOKENS" "MODEL"
printf '%s\n' "--------------------------------------------------------------------------------------------------------------"

while IFS='|' read -r timestamp model latency_ms input_tokens output_tokens reasoning_tokens total_tokens; do
  start_epoch="$(TZ=UTC date -d "${timestamp} UTC" +%s)"
  finish_epoch=$((start_epoch + ((latency_ms + 999) / 1000)))
  start_local="$(TZ="$display_timezone" date -d "@${start_epoch}" '+%Y-%m-%d %H:%M:%S')"
  since="$(TZ="$display_timezone" date -d "@$((start_epoch - 1))" --iso-8601=seconds)"
  until="$(TZ="$display_timezone" date -d "@$((finish_epoch + 2))" --iso-8601=seconds)"

  window_logs="$(docker logs --since "$since" --until "$until" "$proxy_container" 2>&1 || true)"
  selector_line="$(
    awk -v timestamp="$start_local" -v model="$model" \
      'index($0, "[" timestamp "]") && index($0, "model=" model) { print; exit }' \
      <<<"$window_logs"
  )"
  request_id="$(sed -nE 's/.*\[[0-9-]+ [0-9:]+\] \[([^]]+)\].*/\1/p' <<<"$selector_line")"

  status="?"
  endpoint="unknown"
  if [[ -n "$request_id" ]]; then
    completion_line="$(
      awk -v request_id="[$request_id]" \
        'index($0, request_id) && index($0, "gin_logger.go") && index($0, "POST") { line=$0 } END { print line }' \
        <<<"$window_logs"
    )"
    status="$(sed -nE 's/.*gin_logger\.go:[0-9]+\] ([0-9]{3}) \|.*/\1/p' <<<"$completion_line")"
    endpoint="$(sed -nE 's/.*POST[[:space:]]+"([^"]+)".*/\1/p' <<<"$completion_line")"
    status="${status:-?}"
    endpoint="${endpoint:-unknown}"
  else
    request_id="not-found"
  fi

  printf '%-23s %-18s %-6s %-22s %7sms %10s %s\n' \
    "$start_local" "$request_id" "$status" "$endpoint" "$latency_ms" "$total_tokens" "$model"
  printf '  tokens: input=%s output=%s reasoning=%s\n' \
    "$input_tokens" "$output_tokens" "$reasoning_tokens"

  if [[ "$status" == "200" && "$total_tokens" == "0" ]]; then
    echo "  diagnosis=transport returned HTTP 200, but upstream usage was marked failed with no token-bearing output"
  elif [[ "$status" == "?" ]]; then
    echo "  diagnosis=no matching proxy completion log found (container logs may have rotated)"
  fi
done <<<"$records"
