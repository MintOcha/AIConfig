# AIConfig

Opinionated prompts and reusable skills for coding agents. This repository is meant to be a small, inspectable source of truth: keep the files here under version control, then point each agent at the pieces it should load.

## Contents

- `PROMPT.md` contains the shared coding preferences and guardrails.
- `skill/` contains opt-in agent skills. Each child directory is one complete skill.

## Use with Codex

Clone the repository somewhere permanent, then reference the prompt from your global Codex instructions. For example, add this line to `~/.codex/AGENTS.md`, replacing the path with your clone's absolute path:

```text
@/absolute/path/to/AIConfig/PROMPT.md
```

Do not replace an existing `AGENTS.md` if it already contains useful instructions; add the import alongside them. Repository- or directory-specific `AGENTS.md` files can override or supplement these global defaults.

Codex discovers personal skills in `~/.codex/skills`. Link the skills you want to enable:

```bash
mkdir -p ~/.codex/skills
ln -s /absolute/path/to/AIConfig/skill/design ~/.codex/skills/design
```

Restart Codex after changing global instructions or installed skills. The design skill will then be selected automatically for matching interface work, or it can be requested explicitly with `$design`.

## Use with other coding agents

Point the agent's global instruction file at `PROMPT.md`, or copy its contents when the agent does not support imported instruction files. Skill support and installation locations vary by agent, so treat each directory under `skill/` as a self-contained package and follow that agent's skill-loading convention.

Review the prompt before enabling it: these rules are intentionally opinionated and ask the agent to make small, direct fixes when it encounters violations.

## Maintaining the repository

- Put every skill under `skill/<name>/` with its own `SKILL.md`.
- Keep machine-specific paths out of tracked files except in clearly marked examples.
- Edit prompts and skills here rather than editing installed symlinks.
- Keep additions focused; this repository is for broadly useful agent behavior, not project-specific implementation notes.

