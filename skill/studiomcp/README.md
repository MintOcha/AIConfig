# StudioMCP

A lean MCP server and CLI for directly interacting with running Roblox Studio instances and managing Open Cloud assets.

File-based development, headless Lune testing, and scene builds are handled by `RbxMCP2`. `StudioMCP` provides only the live runtime bridge into Studio.

## Architecture

- **Python MCP Server & CLI**: listens on `127.0.0.1:23456` (HTTP command dispatch) and `127.0.0.1:23457` (FastMCP).
- **Studio Receiver Plugin**: runs inside Roblox Studio, connects over localhost HTTP, executes Luau commands against Edit/Play DataModels, and returns observations.
- **Windows Automation**: handles native foregrounding, F5 playtest triggers, visible viewport capture, and input injection.

## Usage

Run directly via `studiomcp <command>` (when installed via AIConfig) or `python main.py <command>`:

```bash
studiomcp savePlugin
studiomcp status
```

`savePlugin` builds and installs `StudioMCP.rbxmx` into `%LOCALAPPDATA%\Roblox\Plugins\` in pure Python without requiring external binaries.

## Key Capabilities

1. **Execution**: `execute` runs arbitrary Luau in Studio Edit or Play models with print/warn capture; `runServer` runs headlessly in Run mode.
2. **Playtest**: `playtest start|stop|status` toggles Play mode safely and verifies client frame readiness.
3. **Observation**: `output` streams condensed console logs; `view` captures viewport or multi-angle model inspect renders.
4. **Script Editing**: `readScript`, `editScript`, and `grep` operate on live scripts in Studio.
5. **Open Cloud**: `upload`, `createGamePass`, `createDeveloperProduct`, and `publishModel` interact directly with Roblox Open Cloud APIs.
