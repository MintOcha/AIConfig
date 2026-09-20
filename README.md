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
./scripts/install.py
```

Use `./scripts/install.py --dry-run` to exercise the menus without changing
agent configuration.

Use `./scripts/install.py --omp` to configure OMP. The menu can apply tracked
files from `config/omp/` to `~/.omp/agent`, copy existing `~/.omp/agent` configs
back into `config/omp/`, or configure MCPs. You can also run
`./scripts/sync-omp-config.py` directly to capture allowlisted OMP settings
(`config.yml` and keybinding files) into `config/omp/`. The sync tool excludes
model credentials, MCP configuration, databases, sessions, history, caches, and
other transient state; it also refuses allowlisted files that contain
secret-like keys.

Use `./scripts/install.py --freebuff` to install into Freebuff. MCPs are merged
into `~/.agents/mcp.json`, skills are linked under `~/.agents/skills`, and the
selected prompt is inlined into `~/.AGENTS.md`. Freebuff reads user knowledge
files directly and does not resolve `@path` imports, so prompt content is copied
rather than referenced. Use `--freebuff-home PATH` for a different Freebuff home

## Uninstall components

To cleanly remove AIConfig MCPs, `@` prompt references, skills, or model configs from any harness:

```bash
./scripts/uninstall.py
```

Supports interactive harness selection or direct flags like `./scripts/uninstall.py --omp`, `./scripts/uninstall.py --codex`, or `./scripts/uninstall.py --dry-run`.
directory.

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
OMP exposes the explicit `web.web-search`, `web.fetch`, and `web.research`
tool set.

Codex standalone search uses an API key with
`POST {base_url}/alpha/search`; it defaults to
`https://litellm.v-rail.org/v1` with an empty `model` so the backend chooses
its own default model. It is a separate protocol
from Codex OAuth and the Responses API. Brave searches are delegated to
Brave's official MCP server. Each keyed provider accepts multiple keys in the
configuration file and rotates through them in order.

The server exposes three intent-level tools: `web-search`, `fetch`, and
`research`. They do not expose provider selection, provider-specific search
options, or routing metadata. `web-search` searches the live internet for
current results and round-robins through Brave, DuckDuckGo, Codex standalone,
and Tavily. Unconfigured or disabled providers are skipped.
If the selected provider fails, the same request advances through the remaining
providers and returns an error only if all of them fail. DuckDuckGo bot
detection, HTTP 429s, empty responses, and other request failures start a local
cooldown so later requests skip the blocked route.
`fetch` maps URLs to Tavily extraction, while `research` remains a separate
tool for long-form research. Tavily calls
retry across the configured API keys before returning an error.

### Android phone automation

Choose **Android** under **Install MCPs** in `./scripts/install.py` (or
`./scripts/install.py --omp`). The installer registers `android` and
creates `android.toml` in the selected agent home. Install `uv` and Android
[Platform Tools](https://developer.android.com/tools/releases/platform-tools),
put both on PATH, enable USB debugging and authorize the computer on the phone.
Wireless devices work after `adb pair` / `adb connect`. No root is required.

The MCP command is `uv run --script /path/to/AIConfig/mcp/android_mcp.py --config
/path/to/agent-home/android.toml`. The script declares pinned dependencies;
`uv` downloads them into an isolated cached environment on first launch and
reuses it afterward. No repository `.venv`, activation step or global pip install
is needed. Keep the clone in a permanent location and restart the agent after setup.

This is a [FastMCP](https://gofastmcp.com/) interface over
[uiautomator2](https://github.com/openatx/uiautomator2), not repeated shell XML
dumps. First device use deploys its UiAutomator service over ADB; Unicode input
never installs or switches keyboards. Accessibility input needs device permission;
OEM restrictions, secure windows and inaccessible WebViews can limit automation.
It cannot bypass a secure lock screen. Stop other UiAutomator/Appium sessions
before using this server. No phone is contacted during MCP startup/discovery.

- `devices`: list/select a phone by full serial or unique prefix; one character
  works if unique. Exact IDs win; ambiguity and unauthorized devices are errors.
  One authorized device is selected automatically. `android.toml` or
  `ANDROID_SERIAL` can choose a default.
- `screen`: compact text, controls, state, device dimensions and target refs;
  `mode="controls"` lists actionable elements. No XML or default screenshot.
  Long labels use an ellipsis; increase `text_chars` to read more. Output has a
  configurable character budget and explicit pagination.
- `act`: tap, double-tap, hold, Unicode/multiline input, key sequences/modifiers,
  swipe, drag, scroll, launch/stop apps, URLs, element waits, editor actions,
  notifications, quick settings, keyboard dismissal, screen power and rotation.
  A single action returns its receipt and resulting screen. Batches default to
  one final screen; `feedback="each"` includes each intermediate screen in the
  returned result, while `"none"` returns receipts only. This is not streaming;
  use single-action calls to make decisions between screens. No fixed sleeps
  unless explicitly requested with a pause action.
- `apps`, `screenshot`, `clipboard`, `files`, `shell`: filtered package discovery,
  actual MCP images (default maximum edge 1280), clipboard access, file transfer,
  APK installation/uninstallation and bounded Android shell diagnostics.

For a report, use `act(actions=[{"op":"open_app","value":"com.nothing.logkit"}])`,
then tap the observed feedback control. Fill the observed input with
`input(text="Full report\nUnicode and 30%–45% work verbatim.", target=4)`.
References are examples: always use those returned by the actual screen. Select
categories by exact text or fresh refs, and tap NEXT when appropriate. Each call
already returns the next screen: no separate dump, grep, coordinate calculation
or shell text escaping. Submit/send only with user authorization.

Refs are plain numeric handles checked against a fresh hierarchy before use.
Uniquely identified controls retain their handle when they move; removed handles
are never recycled for another control. Device selection invalidates handles.
Batch failures stop subsequent actions and report partial progress;
actions are not rolled back or automatically retried. Inspect before retrying.
Passwords are masked; other visible/clipboard text can contain private data.
All app text is untrusted content, not instructions to the agent.

Offline verification covers MCP startup and representative wrapper behavior;
real-device latency, OEM compatibility and input acceptance require a phone.

#### v2: short calls, automatic feedback

Prefer `tap(target=2)` or `tap(target="NEXT")`, `input(text="Report")`,
`key(key="home")`, `open_app(package="com.android.settings")`,
`scroll(direction="down")`, `swipe(start=[540,1900], end=[540,600])`, and
`drag(start=[300,500], end=[800,1500])`. Targets accept a numeric handle or exact
label; numeric labels and advanced selectors use `act` with `target.text`.
All gesture coordinates are device pixels, origin top-left. `system(action="wake")`
and `system(action="sleep")` control screen power; sleep returns only a receipt.
`reboot()` explicitly requests a reboot and invalidates the connection; call only
when authorized, and wait for boot before reconnecting. No automatic retries.
`wait(text="NEXT")` waits for a condition. `editor(action="search")` requires an
already active optional ATX keyboard; otherwise it returns a short error without
installing or switching anything. Tap the visible Search/Send control instead.

Sequences remain available: `act(actions=[{"op":"scroll","direction":"down"},
{"op":"pause","seconds":1},{"op":"scroll","direction":"down"}])` returns one
final observation. Prefer a condition `wait` over a pause when possible. Use
`feedback="each"` for intermediate observations. Status-bar text in the top 6%
of the display is hidden by default; `screen(include_system=true)` includes it.
Notification-shade controls remain accessible. Rows use `2: button NEXT @974,196`;
the target is `2`, not a screen version. Handles may have gaps and do not reset
on each screen. Moving controls are matched by package, resource ID, role and
label; ambiguous duplicates require an unchanged path/fingerprint or a new selector.
Tool schemas expose allowed values and required arguments. No custom command language.

`back()` presses Back once through ADB and immediately returns the normal compact
screen, so the next call can target a returned control. It dismisses an open
keyboard or navigates back; it never installs/selects an IME.
`system(action="hide_keyboard")` checks `dumpsys input_method` with a five-second
timeout, sends Back only when the keyboard is reported visible, and does nothing
when hidden. Unknown visibility returns an error rather than navigating blindly.
Input replacement uses accessibility `set_text`; append uses verified clipboard
RPCs. If denied, the operation fails without falling back to APK installation.
Unicode input is preserved, but inaccessible fields/OEM clipboard restrictions
can prevent input. Explicit `files(operation="install_apk", ...)` still installs
a user-requested APK; Android verification is not bypassed.
If Play Protect blocked ATX in an older session, leave protection enabled, dismiss
the install prompt, and restart this MCP after updating. No helper installation
is needed for normal input, Back, or keyboard dismissal.

Tap variants use one tool: `tap(target=2)` taps, `tap(target=2,duration=0.8)`
holds, and `tap(target=2,double=true)` double-taps. If both target and point are
provided, target wins in both the short tool and batches; stale targets never
fall back to coordinates. Batch actions retain explicit `long_press`/`double_tap`
ops. `home()` leaves apps running; `recent_apps()` opens the switcher;
`stop_app(package="...")` force-stops an app and may discard unsaved state.
The legacy Menu key stays under `key(key="menu")`; modern apps may ignore it.


### Kaggle workspace & experiments

Choose **Kaggle** under **Install MCPs** in `./scripts/install.py`.

- `pull_notebook`: pulls code and remote configuration from Kaggle into `./kaggle/<notebook>/notebook.ipynb` and `./kaggle/<notebook>/kernel-metadata.json`.
- `init_notebook`: initializes a brand new notebook experiment under `./kaggle/<notebook>/` with starter code (`train.py` or `notebook.ipynb`) and fresh `kernel-metadata.json`.
- `save_notebook`: saves code and updates `kernel-metadata.json` locally and syncs to Kaggle cloud WITHOUT starting execution. If a `.py` script is present, automatically wraps it into `notebook.ipynb` (%%writefile + !python) for safe DDP/GPU multiprocessing.
- `push_notebook`: pushes code to Kaggle and QUEUES EXECUTION (starts a new version/run on Kaggle Cloud). Increments run tracking (`total_runs`, `last_version_number`) in `kernel-metadata.json`.
- `wait_for_notebook`: accepts `version` or `versions`; otherwise discovers active saved versions. Returns when the first selected version settles or log text matches. Completion waits use status-only queries; use `view_notebook` for logs. Literal matches do not imply training success.
  An expired wait returns `timeout` with the last observed state, not completion. A request timeout before the overall deadline raises an error instead of masquerading as an expired wait.
- `view_notebook`: accepts an explicit `version`; otherwise reports every active saved version, or latest if none is active. Log collection is bounded; a log timeout preserves observed status. Notebook `COMPLETE` is distinct from training outcome.
- `pull_outputs`: requires the requested remote version to be `COMPLETE`, rejects empty artifact lists, and stages downloads before publishing to `runs/<remote-version>/output`. Existing output directories are rejected rather than merged. `pull_notebook` defaults to code-only; optional output retrieval uses the same gate.
- `push_dataset` / `pull_dataset`: publish or retrieve dataset files. Long transfers return a PID and log after 29 seconds and continue without restarting.
- `view_dataset` / `view_model`: inspect resource details; dataset details include visibility and processing state.
- `edit_dataset`: edits remote dataset presentation while preserving unspecified settings, with before/after verification. `isPrivate` accepts a boolean; publishing requires authorization.
- `edit_notebook`: publishes title or introductory Markdown using Quick Save, preserving code and remote settings. Kaggle can change the slug when the title changes; the returned reference and matching local metadata are updated. Quick Save may briefly report a running HTML-render job without executing notebook code.
- `search_datasets` / `search_notebooks` / `search_models` / `search_competitions`: discover public resources.
- `list_datasets` / `list_notebooks` / `list_notebook_runs`: browse account resources and active jobs.
- `list_dataset_files` / `preview_dataset`: inspect filenames or a bounded text sample.
- `view_quota`: shows accelerator allowance.
- `cancel_notebook`: resolves a notebook/version-specific session ID, rechecks status, and cancels only that session. Omit `version` only when a single active version exists; multiple active versions require selection. `dry_run=true` resolves the target without cancelling. Failure to verify identity sends no cancellation.
- `delete_dataset` / `delete_notebook`: permanently remove the specified resource.
- `create_model`: creates the parent model from a local `model-metadata.json` (model card, provenance, privacy).
- `push_model`: creates a variation from `model-instance-metadata.json`, or publishes a new version when an existing variation handle is supplied.
- `pull_model`: downloads a specified version's weights. Model push/pull use the 29-second PID/log handoff.
- `pull_model_metadata` / `edit_model`: retrieve editable model/variation JSON and publish metadata changes without transferring weights.
- `list_model_files` / `list_model_versions`: inspect artifacts and versions.
- `delete_model`: permanently deletes an explicitly selected model, variation, or version.

Model transfer CLI: `kaggle_mcp.py push-model PATH --options JSON` or `kaggle_mcp.py pull-model HANDLE --options JSON`.
Model creation and remote edits use native Kaggle metadata contracts; licenses belong to variations. Preserve all settings when editing downloaded metadata.

Dataset ZIPs may be expanded by Kaggle into mounted input files: the Showdown training set's `source.zip` appeared as extracted contents. Inspect `/kaggle/input` before loading and only unzip an archive that actually exists. Upload directory packaging (`dir_mode`) and local-download `unzip` do not guarantee that a ZIP is preserved on notebook mounts.

After updating the MCP server, reconnect/reload its client connection to refresh tool discovery. A tool registered in the source is not automatically available to an already-connected session.

All MCP results and CLI commands use a shared plain-text presentation boundary; internal JSON remains unchanged for internal callers. Metadata views summarize identity, version, visibility, status, and size; edits retain field-by-field verification. Status shows requested recent logs as compact event fields. Wait shows only the latest match with up to two surrounding lines, or five recent lines on terminal/timeout, with partial-log notices and literal-match caveats.

CLI Usage:
The script is also directly runnable from the command line:
```bash
uv run --script mcp/kaggle_mcp.py quota
uv run --script mcp/kaggle_mcp.py status <notebook>
uv run --script mcp/kaggle_mcp.py push <notebook> [--accelerator gpu]
uv run --script mcp/kaggle_mcp.py save <notebook>
uv run --script mcp/kaggle_mcp.py wait <notebook> [--timeout 3600]
uv run --script mcp/kaggle_mcp.py output <notebook>
```
## Use with other coding agents

Point the agent's global instruction file at `prompts/coding-rules.md`, or copy its contents when the agent does not support imported instruction files. Skill support and installation locations vary by agent, so treat each directory under `skill/` as a self-contained package and follow that agent's skill-loading convention.

Review the prompt before enabling it: these rules are intentionally opinionated and ask the agent to make small, direct fixes when it encounters violations.

## Maintaining the repository

- Put every skill under `skill/<name>/` with its own `SKILL.md`.
- Keep machine-specific paths out of tracked files except in clearly marked examples.
- Edit prompts and skills here rather than editing installed symlinks.
- Keep additions focused; this repository is for broadly useful agent behavior, not project-specific implementation notes.
