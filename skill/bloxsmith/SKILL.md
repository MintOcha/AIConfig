---
name: bloxsmith
description: Bloxsmith (formerly RbxMCP2) - forge a Roblox game without Studio. Build, test and view it headlessly (build, test, view, toolbox, catalog, avatar, pull, upload, publish), a short .anim format (rig R6, key only what moves), eye>target cameras, member-name clash warnings and the Studio-vs-preview gotchas. Use for any Roblox project with the src/, assets/, tests/ layout.
---

bloxsmith: build, test and look at a roblox game without studio. one project at a time.
(the command line is still called rbxmcp: `python <skill_dir>/main.py`, cache in %LOCALAPPDATA%/rbxmcp, env vars RBXMCP_*.)

Run via `rbxmcp <cmd>` (when installed on PATH) or `python <skill_dir>/main.py <cmd>` (where `<skill_dir>` is this skill's directory). If the current working directory has a `src/` folder, it is used automatically; otherwise pass `--project <dir>` or run `set-project <dir>` once.

commands (`rbxmcp [--project <dir>] <cmd>` or `python <skill_dir>/main.py [--project <dir>] <cmd>`)
set-project <dir>   pick the default project, once. status shows it.
--project <dir>     use another project for this one command only. every command prints which project it used. if a command says a file or folder
                    does not exist, you are probably aimed at the wrong project: add --project <dir>.
build [path]        no path: the whole game -> dist/game.rbxl (open that in studio to play).
                    with a path (a file or folder under src/): just that piece -> dist/<path>.rbxm. use this while others are editing.
test [filter]       run tests/*.test.luau headless. prints PASS/FAIL lines and what was not emulated, takes seconds
view [target]       pictures, printed as absolute paths under <project>/dist/view. also prints how many parts were drawn (0 = you looked at nothing).
                    no target = whole world. target is either
                      a file or folder under src/ (src/StarterGui/HUD, src/ServerStorage/Towers/Gunner): built alone, shown alone. use this right after you write it.
                        models anywhere (ServerStorage too) are copied into the world and shown alone on a floor.
                      ANY other file in the project works too: an assets/ shared module (assets/ReplicatedStorage/Assets/Effects/Parachute.module.luau), a loose .build.luau, .rbxm or .anim.
                        an assets module that returns a function is called with no arguments at build time and the Instance it returns is shown alone on a floor.
                      a raw .rbxm file (assets/Boss.rbxm, dist/model.rbxm): deserialized and shown alone on a floor.
                      a dotted path in the built game (Workspace.Coins, StarterGui.HUD): shown inside the whole world.
                      a .anim file or a KeyframeSequence: the clip drawn frame by frame on the rig that has every joint it keys (see animation).
                    world things -> one sheet, 9 angles (top, +x -x +z -z, 4 corners). gui things -> desktop above phone.
                    --angles=top,+x picks some. --look=x,y,z>tx,ty,tz is your own camera (eye>target, repeat it). --cframe=12 numbers also works. --out=name names the picture. --fov=55. --rig=<model> and --samples=N for animations.
                    only the finished image is saved, never the single frames.
toolbox <query>     search Creator Store / Toolbox assets (models, decals, audio). flags: --category=Model, --limit=10.
catalog <query>     search Roblox Avatar Catalog items. flags: --category=All, --limit=10.
avatar [userId]     inspect user's avatar rig (R15/R6), body scales, and equipped accessories/animations.
pull <assetId>      download asset from Roblox and save as .rbxm in assets/ (or --output=path).
upload <path>       upload a file to Roblox Open Cloud Assets API. if path is a .build.luau, it is compiled to .rbxm first.
                    flags: --name=Name, --creatorId=ID, --creatorType=user|group, --assetType=Model.
publish <assetId>   distribute an existing model for free in Creator Store.
mcp                 run the FastMCP server over stdio for LLM / IDE tools (Codex, Claude, Cursor).


project
src/       mirrors the game. the path is the place: src/Workspace/Map/Tree.build.luau puts things in Workspace.Map.
           the top folder is a service (Workspace, Lighting, ServerScriptService, ReplicatedStorage, StarterPlayer, StarterGui, ...).
           a folder is a Folder. if it has an init file it is that thing instead, and the other files in it go inside it.
           the suffix says what a file is. plain .luau is an error.
             Foo.build.luau    runs once at build time, never ships. plain roblox luau: Instance.new, workspace, game:GetService, Enum...
                               script.Parent is the folder's instance. put what you make there: part.Parent = script.Parent
             Foo.server.luau   Script         Foo.client.luau  LocalScript      Foo.module.luau  ModuleScript
                               these ship and run inside roblox.
             Foo.anim          an animation clip (format below). becomes a KeyframeSequence named Foo.
             init.build.luau   must create exactly one instance under script.Parent. the folder's name is given to it.
                               init.server / init.client / init.module make the folder itself that script.
           any number of build files may sit in a folder: each runs, and whatever it creates under script.Parent is in the game (one Model, five parts,
           nested things). prefer a plain Maps/Meadow.build.luau that makes the Model over a Meadow/ folder with an init. use init only when the folder
           itself must be the thing and other files go inside it.
           the map and every model are made by .build.luau at build time. never generate them at runtime.
assets/    build-only, never ships.
           1. Shared build code: Foo.module.luau, required as require("@assets/Foo") from a .build.luau.
           2. Raw models: Foo.rbxm files placed here are directly accessible in any .build.luau via `assets.Foo:Clone()`
              or `require("@assets/Foo")`.
           runtime scripts cannot require @assets. inside an @assets module script is nil: take the parent as an argument.
tests/     *.test.luau
dist/      output, ignore

example: src/Workspace/Coins/init.build.luau
local Coin = require("@assets/Coin")
local folder = Instance.new("Folder")
folder.Parent = script.Parent
for i = 1, 5 do
	Coin(i).Parent = folder
end

build scripts are normal roblox luau with a few gaps. Terrain:FillBlock, Touched and other events do not exist at build time. models face -Z.
CFrame.lookAt and Random work as in roblox. FindFirstChild(name, true) works. a property written with the wrong type or a value roblox would not keep
fails on that line with the property named: fix that line, nothing else is wrong. legacy Font = Enum.Font.X is converted for you.
a model's PrimaryPart can be rotated on purpose (a cylinder Part lies along X, so stand it upright with a 90 degree turn). model:PivotTo(CFrame.new(pos))
throws that rotation away and lays the whole model on its side: keep it, PivotTo(CFrame.new(pos) * CFrame.Angles(0, yaw, 0) * model:GetPivot().Rotation).
to put a model on the ground, measure its lowest visible point (rotation aware) and lift by that, do not trust the pivot's height.
a UIListLayout sorts children by Name unless SortOrder = Enum.SortOrder.LayoutOrder: set it, then set LayoutOrder on each child.

guidelines: see `GUIDELINES.md` in this skill's folder for how to design games cleanly (responsive relative scale UI, modular components, transform rules, and architecture).

animations: src/ReplicatedStorage/Animations/Shoot.anim
clip "Shoot"                  name must match the file name. add loop for a looping clip. times are seconds, rotations are degrees.
priority Action               Core Idle Movement Action Action2 Action3 Action4
rig R15                       optional (R6 | R15): then a track may name just the part
marker "Hit" "true"           optional: a KeyframeMarker on the first keyframe (or: marker <keyframe id> "Name" "Value")
track RightUpperArm           with a rig line; or a path: track HumanoidRootPart/LowerTorso/UpperTorso/RightUpperArm
  0 rotate=90,0,0             time, then move=x,y,z rotate=x,y,z (degrees) weight= style= direction=
  0.04 rotate=98,0,0
the track path is the pose chain from the root part, not the explorer tree (JSON [["Name",n],...] paths still work). key only the
poses you move: unkeyed ancestors are filled in (their own interpolated value, or a still weight=0 pose). the number after each name picks the nth sibling with that name (1 = the first). a pose is matched to the joint
whose Part1 has that name. move= is studs in that joint's frame. a rotate about +X swings a hanging limb forward but bends a knee the wrong way.
look at it: view src/ReplicatedStorage/Animations/Shoot.anim. it says whether every pose matches a joint (a pose with no joint does nothing in the game).
to play it at runtime: AnimationClipProvider:RegisterAnimationClip(sequence) gives an id for an Animation. tests register clips but never play them.

tests are plain luau. real roblox api works (workspace, game:GetService, attributes, tags, Instance.new, remotes, Humanoid health, TweenService, Debris).
the harness adds only these:
addPlayer(name, position)    fake player with Character.HumanoidRootPart and PlayerGui. client scripts start.
advance(seconds)             run the game clock forward instantly
waitUntil(fn, timeout)       advance until fn() is true. returns true/false.
walkTo(player, pos, speed)   move the character there while time advances.
teleport(player, pos, lookAt)  put the character there now. pass a CFrame to set the facing too, or a point to look at. the character has a visible body in snapshots.
fireServer(player, remote, ...)   the player's client fires a RemoteEvent. the server handlers run on the next step.
check(name, ok, detail)      record a result. detail shows when it fails.
snapshot(thing, cframe, label)   take a picture of the game as the scripts left it. nothing is written per snapshot: the frames are kept in memory and
                             ONE filmstrip per test file is saved when the run ends (dist/view/<test>_filmstrip.png), each frame labelled with the game clock.
                             snapshot() = the player's view (camera behind their character) with their live screens over the world.
                             snapshot(model) = that model standing in the world with the ground around it, from three angles (3 frames).
                             snapshot(model, CFrame.lookAt(from, to)) or snapshot(nil, cframe) = your own camera, with the player's screens over it.
                             a model outside Workspace (ServerStorage...) uses its own position: the world around that spot is drawn, and a note says so.
                             take one every few seconds of game time to see how a wave plays out.

local player = addPlayer("Tester", Vector3.new(0, 3, 0))
fireServer(player, game.ReplicatedStorage.Remotes.Place, 10, 20, "Gunner")
advance(0.1)
check("a tower was built", #workspace.Towers:GetChildren() == 1)

what tests can and cannot see
logic yes: scripts, signals, attributes, tags, timers, remotes, player state, gui text.
inert, never fire: Touched, Activated, MouseClick, Changed, input and the mouse (Hit and Target are nil). the run lists them as untested. assume they work, say so.
no physics, pathfinding, raycasts, gui layout, animation playback. an unsupported method errors. do not work around it, say so.

what view shows and does not show
shows: parts, materials, colours, lighting, gui layout at desktop and phone size, an animation as a row of poses.
does not show: meshes (not loaded yet), real Studio shading (Metal can look dark, Neon blooms white), physics, particles, sounds, motion. a gui looks like it will in roblox but fonts differ a little.
after changing anything visual, view it. when you change a material to suit the preview, remember studio may differ.

studio vs preview: things the headless tools get wrong (each one cost a real bug)
- a gui Folder: Roblox DRAWS GuiObjects inside a Folder under a ScreenGui (a Folder is see-through to gui layout); the preview skips them.
  templates kept in a ScreenGui must be Visible = false, and each clone made visible.
- name clashes: a child named after a member of its parent (row.Remove, button.Selected, frame.Name, frame.Position, button.Play) is
  unreachable by dot access in Roblox: you get the method / property, and the script breaks silently. the tests do not catch it.
  `build` warns about these ("... is a property of TextButton ..."): rename the child (Highlight, Message, Title, RemoveButton).
- UIGradient tints text too: a gradient on a button colours its label. ScrollingFrames clip a Border UIStroke (a "selected" outline):
  give every scroller a 4 px UIPadding. TextScaled and AutomaticSize do not mix: size chips from their text in code.
  UIGridLayout inside an AutomaticCanvasSize scroller previews badly: a UIListLayout of fixed-height rows is safe.
- build scripts can only require @assets. to run a shipped ModuleScript at build time (a preview that uses the real runtime code):
  local fn = loadstring(module.Source, module.Name); setfenv(fn, getfenv(1)); local M = fn()
- a dotted path or a file under src/ builds ONLY that piece (no ReplicatedStorage). to see something that needs the rest of the game,
  view the whole world with --look cameras.
- read-only at build time (set them in Studio by hand, say so): StarterPlayer.GameSettingsAvatar (Game Settings > Avatar), Lighting.Technology.
- tests cannot set Player.CameraMode: only touch it when the player toggles something, not on spawn.

characters, accessories, animations (Roblox native first)
- never hand-build a character rig. bodies: Players:CreateHumanoidModelFromDescription(desc, Enum.HumanoidRigType.R6) and
  Humanoid:ApplyDescription (morphs: Players:GetHumanoidDescriptionFromUserId). for previews and a fallback, pull a toolbox dummy
  (R6 dummy: toolbox 9315536903) and read it first: toolbox models can carry scripts. never ship toolbox scripts, reuse only asset ids.
- accessories: a native Accessory = a Handle part + an Attachment named like the body attachment it goes on (HatAttachment,
  WaistCenterAttachment, RightGripAttachment ...). Humanoid:AddAccessory welds it. extra parts: Weld each to the Handle with
  C0 = handle.CFrame:ToObjectSpace(part.CFrame) BEFORE anything moves. a WeldConstraint freezes whatever offset the parts have when it is
  made, and a Weld with default C0/C1 snaps parts onto the Handle: both "destroy offsets".
- holding two characters at an offset (pair emotes, carrying): Weld partner.HumanoidRootPart to leader.HumanoidRootPart with C0 = the offset;
  the partner follows exactly with no per-frame code. make the partner Massless, CanCollide false, Humanoid.PlatformStand while welded.
- KeyframeSequences play only in Studio (AnimationClipProvider:RegisterAnimationClip). a live game needs uploaded animation ids.
- R6 joints (stock C0/C1): Torso and Head rotate +X = lean / nod forward, Z = turn. Right Arm +Z raises forward, +Y swings it inward;
  Left Arm mirrors (-Z, -Y). an R6 hand end reaches only ~1.6 studs from the shoulder joint.
- R6 limbs MAY be dislocated: most R6 animators move= a limb off the body for poses the stiff limbs cannot reach (drink, hug, pat).
- do not guess contacts. view the clip, and view both partners together in the world with --look cameras.
- Roblox's first person turns the character to face the camera (and anything welded to it). for free look, use a Scriptable camera in
  first person (mouse delta -> yaw / pitch) and write BasePart.LocalTransparencyModifier = 0 after RenderPriority.Camera to keep arms visible.

rules
test the logic with tests, check the look with view, then stop. do not polish past what the user asked.
if a tool breaks, say what broke and what you ran. do not go fix the tool.
