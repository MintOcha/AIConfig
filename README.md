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

Use `./scripts/install.sh --omp` to install selected MCPs into OMP's user MCP
registry at `~/.omp/agent/mcp.json`. OMP-specific runs show only MCP setup and
exit choices; Codex prompt and skill installation remain available in the
default mode.

Use `./scripts/install.sh --freebuff` to install into Freebuff. MCPs are merged
into `~/.agents/mcp.json`, skills are linked under `~/.agents/skills`, and the
selected prompt is inlined into `~/.AGENTS.md`. Freebuff reads user knowledge
files directly and does not resolve `@path` imports, so prompt content is copied
rather than referenced. Use `--freebuff-home PATH` for a different Freebuff home
directory.

Use `./scripts/install.sh --dsh` for the published DeepSeek Harness install. It
discovers the first CLIProxyAPI `api-keys` value with exactly four hyphens,
configures the local OpenAI-compatible endpoint, writes the built-in model
catalog and default `gpt-5.6-sol`, copies the coding prompt, links all AIConfig
skills, and installs the `mcp__web__search`/`fetch`/`research` bridge. The key is
stored only in DSH's owner-readable credentials/config files. DSH itself should
be installed with the official npm package (`npm install --global
@deepseek-ai/dsh`); this repository does not build a DSH source checkout.

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

The search entry installs one local FastMCP server, `web`, run by `uv`
from this repository. Its machine-local configuration is created under the
selected agent home. Setup opens a provider menu for Brave, DuckDuckGo, Codex
standalone search, and Tavily. Selecting a keyed provider requests its key
through hidden input; configured providers receive a green checkmark.
DuckDuckGo is disabled by default and its menu item toggles it directly. The
menu also offers to edit the complete configuration in Vim before installing.
No keys are placed in `mcp.toml` or in an agent command.
OMP exposes the concise `web/search`, `web/fetch`, and `web/research` tool set,
mounted as `mcp__web_search`, `mcp__web_fetch`, and `mcp__web_research`.

Codex standalone search uses an API key with
`POST {base_url}/alpha/search`; it defaults to
`https://litellm.v-rail.org/v1` with an empty `model` so the backend chooses
its own default model. It is a separate protocol
from Codex OAuth and the Responses API. Brave searches are delegated to
Brave's official MCP server. Each keyed provider accepts multiple keys in the
configuration file and rotates through them in order.

The server exposes three intent-level tools: `search`, `fetch`, and `research`.
They do not expose provider selection, provider-specific
search options, or routing metadata. `search` translates its generic query
and result limit for each backend and round-robins through Brave, DuckDuckGo,
Codex standalone, and Tavily. Unconfigured or disabled providers are skipped.
If the selected provider fails, the same request advances through the remaining
providers and returns an error only if all of them fail. DuckDuckGo bot
detection, HTTP 429s, empty responses, and other request failures start a local
cooldown so later requests skip the blocked route.
`fetch` maps URLs to Tavily extraction, while `research` remains a separate
tool for long-form research. Tavily calls
retry across the configured API keys before returning an error.


## Use with other coding agents

Point the agent's global instruction file at `prompts/coding-rules.md`, or copy its contents when the agent does not support imported instruction files. Skill support and installation locations vary by agent, so treat each directory under `skill/` as a self-contained package and follow that agent's skill-loading convention.

Review the prompt before enabling it: these rules are intentionally opinionated and ask the agent to make small, direct fixes when it encounters violations.

## Maintaining the repository

- Put every skill under `skill/<name>/` with its own `SKILL.md`.
- Keep machine-specific paths out of tracked files except in clearly marked examples.
- Edit prompts and skills here rather than editing installed symlinks.
- Keep additions focused; this repository is for broadly useful agent behavior, not project-specific implementation notes.
