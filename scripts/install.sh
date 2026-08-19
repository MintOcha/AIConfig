#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
target_agent="codex"
target_home="${CODEX_HOME:-${HOME}/.codex}"
mcp_file="${repo_root}/mcp.toml"
skills_file="${repo_root}/skills.toml"
prompts_file="${repo_root}/prompts.toml"
prompts_dir="${repo_root}/prompts"
dry_run=0
selected_prompt_path=""

shopt -s nullglob

if [[ -t 1 ]]; then
  bold=$'\033[1m'
  blue=$'\033[1;34m'
  cyan=$'\033[1;36m'
  green=$'\033[1;32m'
  red=$'\033[1;31m'
  yellow=$'\033[1;33m'
  dim=$'\033[2m'
  reset=$'\033[0m'
else
  bold=''
  blue=''
  cyan=''
  green=''
  red=''
  yellow=''
  dim=''
  reset=''
fi

draw_header() {
  printf '\n%s%s%s\n' "$blue" '────────────────────────────────────────────────────────' "$reset"
  printf '%s%s Agent setup%s\n' "$bold" "$cyan" "$reset"
  printf '%s  Source: %s%s\n' "$dim" "$repo_root" "$reset"
  printf '%s  Target: %s · %s%s\n' "$dim" "$target_agent" "$target_home" "$reset"
  ((dry_run)) && printf '%s  Mode:   preview only (no changes)%s\n' "$yellow" "$reset"
  printf '%s%s%s\n' "$blue" '────────────────────────────────────────────────────────' "$reset"
}

success() { printf '%s✓%s %s\n' "$green" "$reset" "$1"; }
warning() { printf '%s!%s %s\n' "$yellow" "$reset" "$1" >&2; }
failure() { printf '%sx%s %s\n' "$red" "$reset" "$1" >&2; }

usage() {
  cat <<'EOF'
Usage: ./scripts/install.sh

Launch the interactive setup menu. MCP choices are discovered from mcp.toml,
skill groups from skills.toml, and prompt metadata from prompts.toml. Secrets
are requested interactively and are never written to this repository.

Options:
  --dry-run             Exercise the menus without changing anything
  --codex-home PATH     Use a specific Codex home instead of ~/.codex
  --omp                  Install MCPs into ~/.omp/agent/mcp.json
  --droid, --factory     Install into Factory Droid (~/.factory)
  --factory-home PATH    Use a specific Factory Droid home instead of ~/.factory
  --freebuff             Install into Freebuff (~/.agents)
  --freebuff-home PATH   Use a specific Freebuff home instead of ~/.agents
EOF
}

require_command() {
  local command_name="$1"
  local install_hint="${2:-}"
  if ! command -v "$command_name" >/dev/null 2>&1; then
    printf 'error: %s is required but was not found in PATH\n' "$command_name" >&2
    [[ -n "$install_hint" ]] && printf '%s\n' "$install_hint" >&2
    exit 1
  fi
}

codex_command() {
  CODEX_HOME="$target_home" codex "$@"
}

require_config() {
  [[ -f "$mcp_file" ]] || {
    printf 'error: MCP catalog not found: %s\n' "$mcp_file" >&2
    exit 1
  }
  require_command python3
}

mcp_entries() {
  python3 - "$mcp_file" <<'PY'
import sys
import tomllib

with open(sys.argv[1], "rb") as stream:
    catalog = tomllib.load(stream)

for server_id, server in catalog.get("mcp", {}).items():
    label = server.get("label", server_id)
    category = server.get("category", "other")
    description = server.get("description", "")
    print(f"{server_id}\t{label}\t{category}\t{description}")
PY
}

mcp_value() {
  local server_id="$1"
  local field="$2"
  python3 - "$mcp_file" "$server_id" "$field" <<'PY'
import sys
import tomllib

with open(sys.argv[1], "rb") as stream:
    catalog = tomllib.load(stream)

server = catalog.get("mcp", {}).get(sys.argv[2])
if server is None:
    raise SystemExit(f"unknown MCP: {sys.argv[2]}")

value = server.get(sys.argv[3], "")
if isinstance(value, list):
    for item in value:
        print(item)
else:
    print(value)
PY
}

mcp_provider_entries() {
  local server_id="$1"
  python3 - "$mcp_file" "$server_id" <<'PY'
import sys
import tomllib

with open(sys.argv[1], "rb") as stream:
    catalog = tomllib.load(stream)

server = catalog.get("mcp", {}).get(sys.argv[2])
if server is None:
    raise SystemExit(f"unknown MCP: {sys.argv[2]}")

for provider in server.get("providers", []):
    print("\t".join((
        provider["label"],
        provider["kind"],
        provider["config_table"],
        provider["config_key"],
    )))
PY
}

render_mcp_template() {
  local value="$1"
  value="${value//\{REPO_ROOT\}/$repo_root}"
  value="${value//\{CODEX_HOME\}/$target_home}"
  printf '%s\n' "$value"
}

prepare_mcp_config() {
  local server_id="$1"
  local template_path config_path source_path
  template_path="$(mcp_value "$server_id" config_template)"
  config_path="$(mcp_value "$server_id" config_path)"

  [[ -z "$template_path" && -z "$config_path" ]] && return 0
  [[ -n "$template_path" && -n "$config_path" ]] || {
    printf 'error: %s must define both config_template and config_path\n' "$server_id" >&2
    return 1
  }

  source_path="${repo_root}/${template_path}"
  config_path="$(render_mcp_template "$config_path")"
  [[ -f "$source_path" ]] || {
    printf 'error: MCP configuration template not found: %s\n' "$source_path" >&2
    return 1
  }

  if [[ -e "$config_path" ]]; then
    printf '%s\n' "Using existing MCP configuration: $config_path"
  elif ((dry_run)); then
    printf '%s\n' "Dry run: would create editable MCP configuration: $config_path"
  else
    mkdir -p "$(dirname -- "$config_path")"
    install -m 600 "$source_path" "$config_path"
    success "Created editable MCP configuration: $config_path"
  fi
}

selected_mcp_ids=()

select_mcp_ids() {
  require_config
  local -a entries
  mapfile -t entries < <(mcp_entries)
  if ((${#entries[@]} == 0)); then
    printf '%s\n' 'No MCPs are defined in mcp.toml.'
    return 1
  fi

  printf '\n%s%s%s\n' "$blue" 'Available MCPs from mcp.toml' "$reset"
  local index=1
  local entry server_id label category description
  for entry in "${entries[@]}"; do
    IFS=$'\t' read -r server_id label category description <<< "$entry"
    printf '%2d) %s [%s] - %s\n' "$index" "$label" "$category" "$description"
    ((index += 1))
  done
  printf '%s\n' 'a) Install all'
  printf '%s' 'Select MCP numbers separated by commas, or a: '
  read -r selection

  selected_mcp_ids=()
  if [[ "$selection" == "a" || "$selection" == "A" ]]; then
    for entry in "${entries[@]}"; do
      IFS=$'\t' read -r server_id _ <<< "$entry"
      selected_mcp_ids+=("$server_id")
    done
    return 0
  fi

  local item item_number
  IFS=',' read -ra requested <<< "$selection"
  for item in "${requested[@]}"; do
    item="${item//[[:space:]]/}"
    [[ "$item" =~ ^[0-9]+$ ]] || {
      printf '%s%s%s\n' "$yellow" "Invalid MCP selection: $item" "$reset" >&2
      return 1
    }
    item_number=$((item - 1))
    ((item_number >= 0 && item_number < ${#entries[@]})) || {
      printf '%s%s%s\n' "$yellow" "MCP selection out of range: $item" "$reset" >&2
      return 1
    }
    IFS=$'\t' read -r server_id _ <<< "${entries[item_number]}"
    selected_mcp_ids+=("$server_id")
  done
}

prompt_secret() {
  local label="$1"
  secret_value=""

  printf 'Enter the %s (input hidden): ' "$label"
  IFS= read -r -s secret_value
  printf '\n'
  [[ -n "$secret_value" ]] || {
    printf '%s\n' 'A non-empty secret is required.' >&2
    return 1
  }
}

config_provider_state() {
  local config_path="$1"
  local config_table="$2"
  local config_key="$3"
  local kind="$4"
  python3 - "$config_path" "$config_table" "$config_key" "$kind" <<'PY'
import sys
import tomllib

with open(sys.argv[1], "rb") as stream:
    value = tomllib.load(stream)
for part in sys.argv[2].split("."):
    value = value.get(part, {}) if isinstance(value, dict) else {}
value = value.get(sys.argv[3]) if isinstance(value, dict) else None
if sys.argv[4] == "toggle":
    print("enabled" if value is True else "disabled")
else:
    print("configured" if isinstance(value, list) and bool(value) else "not configured")
PY
}

write_config_api_key() {
  local config_path="$1"
  local config_table="$2"
  local config_key="$3"

  python3 /dev/fd/3 "$config_path" "$config_table" "$config_key" 3<<'PY' <<<"$secret_value"
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import tomllib

path = Path(sys.argv[1])
table = sys.argv[2]
key = sys.argv[3]
secret = sys.stdin.read().rstrip("\n")
lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
header = f"[{table}]"
section_start = next(
    index for index, line in enumerate(lines) if line.strip() == header
)
section_end = next(
    (
        index
        for index in range(section_start + 1, len(lines))
        if lines[index].lstrip().startswith("[")
    ),
    len(lines),
)
key_pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
replacement = f"{key} = [{json.dumps(secret, ensure_ascii=False)}]\n"
for index in range(section_start + 1, section_end):
    if key_pattern.match(lines[index]):
        lines[index] = replacement
        break
else:
    lines.insert(section_start + 1, replacement)

updated = "".join(lines)
tomllib.loads(updated)
with tempfile.NamedTemporaryFile(
    "w", encoding="utf-8", dir=path.parent, delete=False
) as stream:
    stream.write(updated)
    temporary_path = stream.name
os.chmod(temporary_path, 0o600)
os.replace(temporary_path, path)
PY
}

toggle_config_boolean() {
  local config_path="$1"
  local config_table="$2"
  local config_key="$3"

  python3 - "$config_path" "$config_table" "$config_key" <<'PY'
import os
from pathlib import Path
import re
import sys
import tempfile
import tomllib

path = Path(sys.argv[1])
table = sys.argv[2]
key = sys.argv[3]
lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
parsed = tomllib.loads("".join(lines))
value = parsed
for part in table.split("."):
    value = value.get(part, {}) if isinstance(value, dict) else {}
current = value.get(key) is True if isinstance(value, dict) else False
header = f"[{table}]"
section_start = next(
    index for index, line in enumerate(lines) if line.strip() == header
)
section_end = next(
    (
        index
        for index in range(section_start + 1, len(lines))
        if lines[index].lstrip().startswith("[")
    ),
    len(lines),
)
key_pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
replacement = f"{key} = {'false' if current else 'true'}\n"
for index in range(section_start + 1, section_end):
    if key_pattern.match(lines[index]):
        lines[index] = replacement
        break
else:
    lines.insert(section_start + 1, replacement)

updated = "".join(lines)
tomllib.loads(updated)
with tempfile.NamedTemporaryFile(
    "w", encoding="utf-8", dir=path.parent, delete=False
) as stream:
    stream.write(updated)
    temporary_path = stream.name
os.chmod(temporary_path, 0o600)
os.replace(temporary_path, path)
PY
}

configure_mcp() {
  local server_id="$1"
  local config_path entry label kind config_table config_key state choice index
  local -a providers
  mapfile -t providers < <(mcp_provider_entries "$server_id")
  ((${#providers[@]} > 0)) || return 0

  config_path="$(render_mcp_template "$(mcp_value "$server_id" config_path)")"
  if ((dry_run)); then
    printf '%s\n' "Dry run: would open the provider configuration menu for $server_id"
    return 0
  fi

  while true; do
    printf '\n%s%s provider setup%s\n' "$bold" "$server_id" "$reset"
    index=1
    for entry in "${providers[@]}"; do
      IFS=$'\t' read -r label kind config_table config_key <<< "$entry"
      state="$(config_provider_state "$config_path" "$config_table" "$config_key" "$kind")"
      if [[ "$state" == "configured" || "$state" == "enabled" ]]; then
        printf ' %d) %-24s %s✓ %s%s\n' "$index" "$label" "$green" "$state" "$reset"
      else
        printf ' %d) %-24s %s%s%s\n' "$index" "$label" "$dim" "$state" "$reset"
      fi
      ((index += 1))
    done
    printf ' %d) Paste/edit full config in Vim\n' "$index"
    printf ' %d) Install config and continue\n' "$((index + 1))"
    printf 'Select an option [1-%d]: ' "$((index + 1))"
    read -r choice

    if [[ "$choice" =~ ^[0-9]+$ ]] && ((choice >= 1 && choice <= ${#providers[@]})); then
      IFS=$'\t' read -r label kind config_table config_key <<< "${providers[choice - 1]}"
      if [[ "$kind" == "toggle" ]]; then
        toggle_config_boolean "$config_path" "$config_table" "$config_key"
      else
        prompt_secret "$label" || continue
        write_config_api_key "$config_path" "$config_table" "$config_key"
        unset secret_value
      fi
      continue
    fi
    if [[ "$choice" == "$index" ]]; then
      require_command vim
      vim "$config_path"
      python3 -c 'import sys, tomllib; tomllib.load(open(sys.argv[1], "rb"))' "$config_path"
      continue
    fi
    if [[ "$choice" == "$((index + 1))" ]]; then
      return 0
    fi
    warning "Please choose a number from 1 to $((index + 1))."
  done
}

write_omp_mcp() {
  local server_id="$1"
  local transport="$2"
  shift 2
  local config_path="${target_home}/mcp.json"

  mkdir -p "$target_home"
  python3 - "$config_path" "$server_id" "$transport" "$@" <<'PY'
import json
import os
from pathlib import Path
import sys
import tempfile

path = Path(sys.argv[1])
server_id = sys.argv[2]
transport = sys.argv[3]
values = sys.argv[4:]
if path.exists():
    config = json.loads(path.read_text(encoding="utf-8"))
else:
    config = {"mcpServers": {}}
servers = config.setdefault("mcpServers", {})
if transport == "url":
    servers[server_id] = {"url": values[0]}
else:
    servers[server_id] = {"command": values[0], "args": values[1:]}

with tempfile.NamedTemporaryFile(
    "w", encoding="utf-8", dir=path.parent, delete=False
) as stream:
    json.dump(config, stream, indent=2)
    stream.write("\n")
    temporary_path = stream.name
os.chmod(temporary_path, 0o600)
os.replace(temporary_path, path)
PY
}

install_mcp() {
  local server_id="$1"
  local transport command_name url_template url placeholder secret_label secret_choice argument_index
  local -a command_args

  transport="$(mcp_value "$server_id" transport)"

  if [[ "$transport" == "url" ]]; then
    url_template="$(mcp_value "$server_id" url)"
    url="$url_template"
    if [[ "$url_template" =~ \{[^{}]+\} ]]; then
      placeholder="${BASH_REMATCH[0]}"
      secret_label="$(mcp_value "$server_id" secret_label)"
      [[ -n "$secret_label" ]] || secret_label="$server_id API key"
      printf '\n%s\n' "$server_id requires a value for $placeholder."
      printf '%s\n' '1) Paste the API key into this interactive setup'
      printf '%s\n' '2) Skip setup'
      printf '%s' 'Select an option [1-2]: '
      read -r secret_choice
      case "$secret_choice" in
        1) prompt_secret "$secret_label" ;;
        2) printf '%s\n' "Skipped MCP: $server_id"; return 0 ;;
        *) printf '%s\n' 'Please choose 1 or 2.' >&2; return 1 ;;
      esac
      url="${url//$placeholder/$secret_value}"
      [[ "$url" != *"$placeholder"* ]] || {
        printf 'error: %s placeholder was not replaced\n' "$server_id" >&2
        return 1
      }
    fi
    if ((dry_run)); then
      unset secret_value
      printf '%s\n' "Dry run: would install MCP: $server_id"
      return 0
    fi
    if [[ "$target_agent" == "omp" || "$target_agent" == "droid" || "$target_agent" == "freebuff" ]]; then
      write_omp_mcp "$server_id" url "$url"
    else
      codex_command mcp remove "$server_id" >/dev/null 2>&1 || true
      codex_command mcp add "$server_id" --url "$url"
    fi
  else
    prepare_mcp_config "$server_id" || return 1
    configure_mcp "$server_id" || return 1
    command_name="$(render_mcp_template "$(mcp_value "$server_id" command)")"
    mapfile -t command_args < <(mcp_value "$server_id" args)
    ((${#command_args[@]} > 0)) || {
      printf 'error: %s has no command arguments in mcp.toml\n' "$server_id" >&2
      return 1
    }
    for argument_index in "${!command_args[@]}"; do
      command_args[$argument_index]="$(render_mcp_template "${command_args[argument_index]}")"
    done
    if ((dry_run)); then
      printf '%s\n' "Dry run: would install MCP: $server_id"
      return 0
    fi
    if [[ "$target_agent" == "omp" || "$target_agent" == "droid" || "$target_agent" == "freebuff" ]]; then
      write_omp_mcp "$server_id" stdio "$command_name" "${command_args[@]}"
    else
      codex_command mcp remove "$server_id" >/dev/null 2>&1 || true
      codex_command mcp add "$server_id" -- "$command_name" "${command_args[@]}"
    fi
  fi

  unset secret_value
  printf '%s%s%s\n' "$green" "Installed MCP: $server_id" "$reset"
}

install_mcps() {
  [[ "$target_agent" == "omp" || "$target_agent" == "droid" || "$target_agent" == "freebuff" ]] || require_command codex
  select_mcp_ids || return 1
  local server_id
  for server_id in "${selected_mcp_ids[@]}"; do
    install_mcp "$server_id"
  done
}

prompt_entries() {
  for prompt_path in "$prompts_dir"/*.md; do
    [[ -f "$prompt_path" ]] || continue
    [[ "$(basename "$prompt_path")" == "README.md" ]] && continue
    prompt_name="$(basename "$prompt_path" .md)"
    prompt_label="$(prompt_value "$prompt_name" label 2>/dev/null || true)"
    [[ -n "$prompt_label" ]] || prompt_label="$prompt_name prompt"
    printf '%s\t%s\t%s\n' "$prompt_name" "$prompt_label" "$prompt_path"
  done
}

prompt_value() {
  local prompt_id="$1"
  local field="$2"
  python3 - "$prompts_file" "$prompt_id" "$field" <<'PY'
import sys
import tomllib

with open(sys.argv[1], "rb") as stream:
    catalog = tomllib.load(stream)
print(catalog.get("prompts", {}).get(sys.argv[2], {}).get(sys.argv[3], ""))
PY
}

select_prompt_source() {
  require_command python3
  local -a entries
  mapfile -t entries < <(prompt_entries)
  printf '\n%s%s%s\n' "$bold" 'Choose a prompt' "$reset"
  printf '%s\n' 'Prompts are discovered from prompts/ and described by prompts.toml.'

  local index=1 entry prompt_id label prompt_path description
  for entry in "${entries[@]}"; do
    IFS=$'\t' read -r prompt_id label prompt_path <<< "$entry"
    description="$(prompt_value "$prompt_id" description 2>/dev/null || true)"
    printf '%2d) %-24s %s\n' "$index" "$label" "$description"
    ((index += 1))
  done
  printf '%s\n' 'b) Back'
  printf '%s' 'Select a prompt: '
  read -r selection
  [[ "$selection" =~ ^[0-9]+$ ]] || {
    [[ "$selection" =~ ^[Bb]$ ]] && return 1
    warning 'Please choose a prompt number.'
    return 1
  }
  local prompt_number=$((selection - 1))
  ((prompt_number >= 0 && prompt_number < ${#entries[@]})) || {
    warning 'Prompt selection out of range.'
    return 1
  }
  IFS=$'\t' read -r _ _ selected_prompt_path <<< "${entries[prompt_number]}"
}

install_freebuff_prompt() {
  local target="${HOME}/.AGENTS.md"
  local prompt_name
  prompt_name="$(basename "$selected_prompt_path" .md)"
  local marker="AIConfig prompt: ${prompt_name}"

  if ((dry_run)); then
    printf '%s\n' "Preview: would inline $selected_prompt_path into $target"
    return 0
  fi

  if [[ -e "$target" ]] && grep -Fq "$marker" "$target"; then
    printf '%s\n' "Already installed in $target"
    return 0
  fi

  {
    [[ -e "$target" ]] && printf '\n'
    printf '# %s\n' "$marker"
    printf '%s\n' "<!-- installed from ${selected_prompt_path} by scripts/install.sh -->"
    printf '\n'
    cat "$selected_prompt_path"
    printf '\n'
  } >> "$target"
  success "Prompt inlined through $target"
}

install_prompt() {
  select_prompt_source || return 0

  if [[ "$target_agent" == "freebuff" ]]; then
    install_freebuff_prompt
    return 0
  fi

  local target="${target_home}/AGENTS.md"
  local import_line="@${selected_prompt_path}"

  if ((dry_run)); then
    printf '%s\n' "Preview: would link $import_line through $target"
    return 0
  fi

  mkdir -p "$target_home"
  if [[ ! -e "$target" && ! -L "$target" ]]; then
    printf '# Global Agent Instructions\n\n%s\n' "$import_line" > "$target"
  elif ! grep -Fqx "$import_line" "$target"; then
    printf '\n%s\n' "$import_line" >> "$target"
  fi
  success "Prompt linked through $target"
}

skill_entries() {
  python3 - "$repo_root/skill" <<'PY'
import sys
from pathlib import Path

for skill_dir in sorted(Path(sys.argv[1]).iterdir()):
    if skill_dir.is_dir() and (skill_dir / "SKILL.md").is_file():
        print(skill_dir.name)
PY
}

skill_groups() {
  python3 - "$skills_file" <<'PY'
import sys
import tomllib

with open(sys.argv[1], "rb") as stream:
    catalog = tomllib.load(stream)

for group_id, group in catalog.get("groups", {}).items():
    label = group.get("label", group_id)
    description = group.get("description", "")
    print(f"{group_id}\t{label}\t{description}")
PY
}

group_skills() {
  python3 - "$skills_file" "$repo_root/skill" "$1" <<'PY'
import sys
import tomllib
from pathlib import Path

with open(sys.argv[1], "rb") as stream:
    catalog = tomllib.load(stream)

available = sorted(
    path.name for path in Path(sys.argv[2]).iterdir()
    if path.is_dir() and (path / "SKILL.md").is_file()
)
members = catalog.get("groups", {}).get(sys.argv[3], {}).get("skills", [])
selected = available if "*" in members else [name for name in members if name in available]
for skill in selected:
    print(skill)

unknown = [name for name in members if name != "*" and name not in available]
if unknown:
    print(f"warning: unknown skills in group {sys.argv[3]}: {', '.join(unknown)}", file=sys.stderr)
PY
}

selected_skills=()

select_individual_skills() {
  local -a skills
  mapfile -t skills < <(skill_entries)
  ((${#skills[@]} > 0)) || {
    printf '%s\n' 'No skills with SKILL.md files were found.'
    return 1
  }

  printf '\n%s%s%s\n' "$bold" 'Choose individual skills' "$reset"
  local index=1 skill
  for skill in "${skills[@]}"; do
    printf '%2d) %s\n' "$index" "$skill"
    ((index += 1))
  done
  printf '%s\n' 'a) Install all discovered skills'
  printf '%s' 'Select skill numbers separated by commas, or a: '
  read -r selection

  selected_skills=()
  if [[ "$selection" == "a" || "$selection" == "A" ]]; then
    selected_skills=("${skills[@]}")
    return 0
  fi

  local item item_number
  IFS=',' read -ra requested <<< "$selection"
  for item in "${requested[@]}"; do
    item="${item//[[:space:]]/}"
    [[ "$item" =~ ^[0-9]+$ ]] || { warning "Invalid skill selection: $item"; return 1; }
    item_number=$((item - 1))
    ((item_number >= 0 && item_number < ${#skills[@]})) || { warning "Skill selection out of range: $item"; return 1; }
    selected_skills+=("${skills[item_number]}")
  done
}

select_skill_set() {
  require_command python3
  [[ -f "$skills_file" ]] || {
    printf 'error: skill groups not found: %s\n' "$skills_file" >&2
    return 1
  }

  local -a groups
  mapfile -t groups < <(skill_groups)
  if ((${#groups[@]} == 0)); then
    select_individual_skills
    return
  fi

  printf '\n%s%s%s\n' "$bold" 'Choose a skill set' "$reset"
  printf '%s\n' 'Groups are defined in skills.toml and expand against discovered skill directories.'
  local index=1 group_id label description entry
  for entry in "${groups[@]}"; do
    IFS=$'\t' read -r group_id label description <<< "$entry"
    printf '%2d) %s - %s\n' "$index" "$label" "$description"
    ((index += 1))
  done
  printf '%s\n' 'i) Choose individual skills'
  printf '%s\n' 'b) Back'
  printf '%s' 'Select a skill group: '
  read -r selection

  if [[ "$selection" == "i" || "$selection" == "I" ]]; then
    select_individual_skills
    return
  elif [[ "$selection" == "b" || "$selection" == "B" ]]; then
    selected_skills=()
    return 0
  fi

  [[ "$selection" =~ ^[0-9]+$ ]] || { warning 'Invalid skill group selection.'; return 1; }
  local group_number=$((selection - 1))
  ((group_number >= 0 && group_number < ${#groups[@]})) || { warning 'Skill group selection out of range.'; return 1; }
  IFS=$'\t' read -r group_id _ <<< "${groups[group_number]}"
  mapfile -t selected_skills < <(group_skills "$group_id")
  ((${#selected_skills[@]} > 0)) || {
    printf '%s\n' "The selected group has no available skills."
    return 1
  }
  printf '%s\n' "Selected group: $group_id (${#selected_skills[@]} skills)"
}

install_skills() {
  [[ "$target_agent" == "droid" || "$target_agent" == "freebuff" ]] || require_command codex
  local skills_dir="${target_home}/skills"
  select_skill_set || return 1

  ((dry_run)) || mkdir -p "$skills_dir"

  for skill in "${selected_skills[@]}"; do
    local target="${skills_dir}/${skill}"
    local source="${repo_root}/skill/${skill}"
    if [[ -L "$target" && "$(readlink -f "$target")" == "$source" ]]; then
      printf '%s\n' "Already linked: $skill"
    elif [[ -e "$target" || -L "$target" ]]; then
      warning "Skipped existing non-repository skill: $target"
    elif ((dry_run)); then
      printf '%s\n' "Preview: would link skill $skill"
    else
      ln -s "$source" "$target"
      success "Linked skill: $skill"
    fi
  done
}

menu() {
  while true; do
    draw_header
    printf '%s\n' 'What would you like to set up?'
    printf '%s\n' '  1) Install MCPs        Select from the catalog'
    if [[ "$target_agent" == "omp" ]]; then
      printf '%s\n' '  2) Exit'
      printf '%s' 'Select an option [1-2]: '
    else
      printf '%s\n' '  2) Install a prompt    Choose the global instruction set'
      printf '%s\n' '  3) Install skills      Choose a group or individual skills'
      printf '%s\n' '  4) Exit'
      printf '%s' 'Select an option [1-4]: '
    fi
    read -r choice || return 0

    case "$choice" in
      1) install_mcps || true ;;
      2) [[ "$target_agent" == "omp" ]] && return 0; install_prompt || true ;;
      3) [[ "$target_agent" != "omp" ]] && install_skills || true ;;
      4) [[ "$target_agent" != "omp" ]] && return 0 ;;
      *) warning 'Please choose one of the displayed options.' ;;
    esac
  done
}

while (($# > 0)); do
  case "$1" in
    --help|-h) usage; exit 0 ;;
    --dry-run) dry_run=1; shift ;;
    --codex-home)
      [[ $# -ge 2 ]] || { failure '--codex-home requires a path.'; exit 2; }
      target_home="$2"
      shift 2
      ;;
    --omp)
      target_agent="omp"
      target_home="${HOME}/.omp/agent"
      shift
      ;;
    --droid|--factory)
      target_agent="droid"
      target_home="${HOME}/.factory"
      shift
      ;;
    --factory-home|--droid-home)
      [[ $# -ge 2 ]] || { failure '--factory-home requires a path.'; exit 2; }
      target_agent="droid"
      target_home="$2"
      shift 2
      ;;
    --freebuff)
      target_agent="freebuff"
      target_home="${FREEBUFF_HOME:-${HOME}/.agents}"
      shift
      ;;
    --freebuff-home)
      [[ $# -ge 2 ]] || { failure '--freebuff-home requires a path.'; exit 2; }
      target_agent="freebuff"
      target_home="$2"
      shift 2
      ;;
    *) usage >&2; exit 2 ;;
  esac
done

menu
