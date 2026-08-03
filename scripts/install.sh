#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
codex_home="${CODEX_HOME:-${HOME}/.codex}"
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
  printf '%s  Target: Codex · %s%s\n' "$dim" "$codex_home" "$reset"
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
  CODEX_HOME="$codex_home" codex "$@"
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

install_mcp() {
  local server_id="$1"
  local transport command_name url_template url placeholder secret_label secret_choice
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
    codex_command mcp remove "$server_id" >/dev/null 2>&1 || true
    codex_command mcp add "$server_id" --url "$url"
  else
    command_name="$(mcp_value "$server_id" command)"
    mapfile -t command_args < <(mcp_value "$server_id" args)
    ((${#command_args[@]} > 0)) || {
      printf 'error: %s has no command arguments in mcp.toml\n' "$server_id" >&2
      return 1
    }
    if ((dry_run)); then
      printf '%s\n' "Dry run: would install MCP: $server_id"
      return 0
    fi
    codex_command mcp remove "$server_id" >/dev/null 2>&1 || true
    codex_command mcp add "$server_id" -- "$command_name" "${command_args[@]}"
  fi

  unset secret_value
  printf '%s%s%s\n' "$green" "Installed MCP: $server_id" "$reset"
}

install_mcps() {
  require_command codex
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

install_prompt() {
  select_prompt_source || return 0
  local target="${codex_home}/AGENTS.md"
  local import_line="@${selected_prompt_path}"

  if ((dry_run)); then
    printf '%s\n' "Preview: would link $import_line through $target"
    return 0
  fi

  mkdir -p "$codex_home"
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
  require_command codex
  local skills_dir="${codex_home}/skills"
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
    printf '%s\n' '  2) Install a prompt    Choose the global instruction set'
    printf '%s\n' '  3) Install skills      Choose a group or individual skills'
    printf '%s\n' '  4) Exit'
    printf '%s' 'Select an option [1-4]: '
    read -r choice || return 0

    case "$choice" in
      1) install_mcps || true ;;
      2) install_prompt || true ;;
      3) install_skills || true ;;
      4) return 0 ;;
      *) warning 'Please choose 1, 2, 3, or 4.' ;;
    esac
  done
}

while (($# > 0)); do
  case "$1" in
    --help|-h) usage; exit 0 ;;
    --dry-run) dry_run=1; shift ;;
    --codex-home)
      [[ $# -ge 2 ]] || { failure '--codex-home requires a path.'; exit 2; }
      codex_home="$2"
      shift 2
      ;;
    *) usage >&2; exit 2 ;;
  esac
done

menu
