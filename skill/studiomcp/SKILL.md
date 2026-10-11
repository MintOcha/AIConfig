---
name: studiomcp
description: Interact directly with live Roblox Studio sessions: execute Luau, control playtests, capture viewports, inspect instances, and manage Open Cloud uploads.
---

# StudioMCP

Direct live bridge to Roblox Studio and Open Cloud assets. File-directory authoring and build compilation belong in RbxMCP2.

## Commands

Run CLI via `studiomcp <command>` (when installed on PATH) or `python <skill_dir>/main.py <command>` (where `<skill_dir>` is this skill's directory).

### Session & Status
- `status` (alias `studios`): list connected Studios, contexts (Edit, Play server/client), lock status.
- `selectStudio <number>`: select active Studio receiver index.
- `events`: show recent Studio connection and playtest events.
- `savePlugin [dir]`: build and install receiver plugin (`StudioMCP.rbxmx`) into `%LOCALAPPDATA%/Roblox/Plugins/`.

### Live Execution & Scripts
- `execute <code> [--datamodel auto|Edit|Play] [--context client|server] [--timeout 30]`: execute Luau code safely and return results + prints.
- `runServer <code> [--timeout 30]`: execute Luau headlessly in Run mode via `StudioTestService` (Edit mode only).
- `readScript <path> [--start N] [--finish N]`: read script source by instance path.
- `editScript <path> <edits_json> [--expected <text>] [--className Script|LocalScript|ModuleScript]`: apply line-based edits; creates script with sensible default class if omitted (Script in ServerScriptService, LocalScript in Player/Gui, ModuleScript elsewhere).
- `grep <query> [--pattern] [--caseSensitive]`: search script source line-by-line across all scripts in Studio (case-insensitive plain text by default).

### Playtest & Observation
- `playtest [start|stop|status]`: start/stop native Play session (Windows F5) or query status (default: status).
- `output [--all]`: read console errors and warnings (use `--all` for all output lines).
- `view [path] [--output file.png] [--angle front|top|...] [--samples 9] [--contactSheet]`: capture visible viewport or isolated multi-angle model view (defaults to game viewport).
- `capture [--frames 6] [--interval 500] [--state "luau_expr"]`: capture timestamped frame sequence with state sampling.
- `camera [status|reset]`: query camera type or reset Edit camera to Custom.
- `profile <modules...> [--seconds 5] [--only funcs]`: function call profiling in Play mode.

### Studio Hierarchy & Input
- `ls [path] [--recursive] [--limit 200]`: browse instance tree (defaults to `game`).
- `cat <path>`: inspect instance properties, attributes, and tags.
- `find [path_or_name] [--name <str>] [--class <className>]`: search descendants by name or class. If given a single name (e.g. `find SpawnLocation`), searches whole game.
- `keyboard <actions_json>`: inject keyboard actions into Studio window.
- `mouse <actions_json>`: inject mouse actions into Studio window.
- `navigate <target>`: walk character to target instance path or `[x,y,z]` coordinates.

### Assets & Open Cloud
- `insertAsset <assetId> [--name <name>]`: insert asset into Studio.
- `exportAsset <path> [--output file.rbxm]`: export Studio model/part/animation as RBXM (defaults to `<name>.rbxm`).
- `librarySearch [query] [--category Model|Decal|Audio]`: search Creator Store / Toolbox assets.
- `libraryAdd <assetId> [--name name]`: quarantine-insert Creator Store asset into ServerStorage.StudioMCPAssetDiscovery with scripts disabled.
- `upload <file> [--creatorId id] [--assetType Model|Audio|...]`: upload via Open Cloud Assets API (creator ID, asset type, and name inferred if omitted).
- `uploadStatus <operationId>`: check status of pending upload.
- `createGamePass <name> [--price Robux]`: configure GamePass via Open Cloud (universe ID inferred; setting price sets forSale=True).
- `createDeveloperProduct <name> [--price Robux]`: configure DeveloperProduct via Open Cloud.
- `publishModel <assetId>`: publish model to Creator Store for free distribution.
