# AIConfig

Opinionated prompts and reusable skills for coding agents. This repository is meant to be a small, inspectable source of truth: keep the files here under version control, then point each agent at the pieces it should load.

## Contents

- `prompts/coding-rules.md` contains opinionated coding preferences and guardrails.
- `skill/` contains opt-in agent skills. Each child directory is one complete skill.

## Use with Codex

Clone the repository somewhere permanent, then reference the prompt from your global Codex instructions. For example, add this line to `~/.codex/AGENTS.md`, replacing the path with your clone's absolute path:

```text
@/absolute/path/to/AIConfig/prompts/coding-rules.md
```

Do not replace an existing `AGENTS.md` if it already contains useful instructions; add the import alongside them. Repository- or directory-specific `AGENTS.md` files can override or supplement these global defaults.

Codex discovers personal skills in `~/.codex/skills`. Link the skills you want to enable:

```bash
mkdir -p ~/.codex/skills
ln -s /absolute/path/to/AIConfig/skill/design ~/.codex/skills/design
```

Restart Codex after changing global instructions or installed skills. The design skill will then be selected automatically for matching interface work, or it can be requested explicitly with `$design`.

Also configure some of my favourite addons:

codex plugin marketplace add DietrichGebert/ponytail
codex plugin add ponytail@ponytail

## Setup agents

Launch the setup menu:

```bash
./scripts/install.sh
```

Use `./scripts/install.sh --dry-run` to exercise the menus without
changing Codex configuration.

The main menu installs MCPs, the shared prompt, or skills as independent
choices. Installing a prompt or skill never reinstalls MCPs or touches their
credentials. MCPs are discovered from [`mcp.toml`](mcp.toml), so adding another entry only
requires editing that file. Skills are discovered from `skill/*/SKILL.md` and
grouped through [`skills.toml`](skills.toml), which keeps large skill catalogs
manageable. If an MCP URL contains a `{PLACEHOLDER}`, the menu offers to
paste its credential through hidden input or skip that MCP. Credentials are
substituted only for the `codex mcp add` call and are never stored here.

`prompts/coding-rules.md` is available as one selectable prompt. Add more Markdown files under
[`prompts/`](prompts/) to make them selectable, then describe them in
[`prompts.toml`](prompts.toml) to provide useful labels and descriptions. Use
`--codex-home PATH` for a separate Codex installation, and `--dry-run` to
preview every operation without changing the target.

The search entry installs one local FastMCP server, `web-search`, run by `uv`
from this repository. Its machine-local configuration is created at
`~/.codex/web-search-mcp.toml` (or under the `--codex-home` directory). Add
Brave and Tavily keys to the two `api_keys` lists there; each list supports
multiple keys and is round-robin. Brave searches are delegated to Brave's
official MCP server. No keys are placed in `mcp.toml` or in the Codex command.

The server exposes three intent-level tools: `web_search`, `fetch_content`, and
`tavily_research`. They do not expose provider selection, provider-specific
search options, or routing metadata. `web_search` translates its generic query
and result limit for each backend and round-robins through DuckDuckGo, Brave,
and Tavily in that order. If the selected provider fails, the same request
advances through the remaining providers and returns an error only if all three
fail. DuckDuckGo bot detection, HTTP 429s, empty responses, and other request
failures start a local cooldown so later requests skip the blocked route.
`fetch_content` maps URLs to Tavily extraction, while
`tavily_research` remains a separate tool for long-form research. Tavily calls
retry across the configured API keys before returning an error.


## Use with other coding agents

Point the agent's global instruction file at `prompts/coding-rules.md`, or copy its contents when the agent does not support imported instruction files. Skill support and installation locations vary by agent, so treat each directory under `skill/` as a self-contained package and follow that agent's skill-loading convention.

Review the prompt before enabling it: these rules are intentionally opinionated and ask the agent to make small, direct fixes when it encounters violations.

## Maintaining the repository

- Put every skill under `skill/<name>/` with its own `SKILL.md`.
- Keep machine-specific paths out of tracked files except in clearly marked examples.
- Edit prompts and skills here rather than editing installed symlinks.
- Keep additions focused; this repository is for broadly useful agent behavior, not project-specific implementation notes.
