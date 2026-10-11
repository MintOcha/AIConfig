--!nocheck
-- lune run renderer/view.luau <project-dir> <out-dir> [target] [--cframe=x,y,z,r00..r22] [--angles=top,+x] [--fov=55]
-- builds the project and writes renderer scenes + a manifest describing what to render.
-- target is either
--   a dotted path in the built game (Workspace.Coins, StarterGui.HUD), shown inside the whole world, or
--   a file or folder under src/ (src/StarterGui/HUD, src/Workspace/Coins), built on its own and shown alone
-- no target = the whole world.
local fs = require("@lune/fs")
local process = require("@lune/process")
local roblox = require("@lune/roblox")
local serde = require("@lune/serde")
local Build = require("../core/build")
local Scene = require("./scene")
local V3, CFrame = roblox.Vector3, roblox.CFrame

local args, flags = {}, {}
for _, a in process.args do
	local k, v = a:match("^%-%-([%w-]+)=?(.*)$")
	if k then
		flags[k] = v ~= "" and v or true
	else
		table.insert(args, a)
	end
end
local root, outDir, targetPath = args[1], args[2], args[3]
assert(root and outDir, "usage: view.luau <project-dir> <out-dir> [target] [flags]")
if not fs.isDir(outDir) then
	fs.writeDir(outDir)
end

-- models in ServerStorage / ReplicatedStorage are not in the world, so a preview would be empty. show a clone in Workspace.
local function stage(dm, list)
	local ws = dm:GetService("Workspace")
	local out = {}
	for _, inst in list do
		local physical = inst:IsA("BasePart")
		if not physical then
			for _, d in inst:GetDescendants() do
				if d:IsA("BasePart") then
					physical = true
					break
				end
			end
		end
		if physical and not inst:IsDescendantOf(ws) then
			local clone = inst:Clone()
			clone.Parent = ws
			table.insert(out, clone)
		else
			table.insert(out, inst)
		end
	end
	return out
end

-- show targets on their own: clear the rest of the world, add daylight (unless it IS the lighting) and a floor under them
local function isolate(dm, targets, keepLighting, floorSize)
	local ws = dm:GetService("Workspace")
	if not keepLighting then
		local light = dm:GetService("Lighting")
		light.ClockTime, light.Brightness, light.GlobalShadows = 14, 3, true
	end
	for _, target in targets do -- a target nested in Map/Areas/... must not die with its container: hoist it first (parts hold world coordinates)
		if target.Parent ~= ws and target:IsDescendantOf(ws) then
			target.Parent = ws
		end
	end
	for _, child in ws:GetChildren() do
		if child.ClassName ~= "Terrain" and child.ClassName ~= "Camera" and not table.find(targets, child) then
			child:Destroy()
		end
	end
	local lowest, sx, sz, n = nil, 0, 0, 0
	for _, inst in targets do
		local nodes = inst:GetDescendants()
		table.insert(nodes, inst)
		for _, d in nodes do
			if d:IsA("BasePart") and d:IsDescendantOf(ws) then
				local p = d.CFrame.Position
				lowest = lowest and math.min(lowest, p.Y - d.Size.Y / 2) or (p.Y - d.Size.Y / 2)
				sx, sz, n = sx + p.X, sz + p.Z, n + 1
			end
		end
	end
	if n > 0 then
		local floor = roblox.Instance.new("Part")
		floor.Name, floor.Anchored = "Floor", true
		floor.Size = V3.new(floorSize or 120, 1, 120)
		floor.CFrame = CFrame.new(sx / n, lowest - 0.5, sz / n)
		floor.Color = roblox.Color3.fromRGB(120, 140, 120)
		floor.Parent = ws
	end
end

-- animation preview ---------------------------------------------------------------------------------------------------
-- pose a copy of the rig at every keyframe (or --samples N points along the clip), place the copies side by side, and give
-- each copy its own camera. a pose moves a joint: child = parent * C0 * pose * C1:Inverse() (poses are matched to Part1's name).
local ANGLE_DIRECTIONS = {
	["top"] = V3.new(0.001, 1, 0.12), ["+x"] = V3.new(1, 0.3, 0), ["-x"] = V3.new(-1, 0.3, 0), ["+z"] = V3.new(0, 0.3, 1), ["-z"] = V3.new(0, 0.3, -1),
	["+x+z"] = V3.new(0.7, 0.5, 0.7), ["+x-z"] = V3.new(0.7, 0.5, -0.7), ["-x+z"] = V3.new(-0.7, 0.5, 0.7), ["-x-z"] = V3.new(-0.7, 0.5, -0.7),
}

local function poseMap(keyframe)
	local map = {}
	local function walk(inst)
		for _, child in inst:GetChildren() do
			if child.ClassName == "Pose" then
				map[child.Name] = { cf = child.CFrame, weight = child.Weight }
				walk(child)
			end
		end
	end
	walk(keyframe)
	return map
end

local function findRig(dm, sequence, wanted)
	local names = {}
	for _, kf in sequence:GetChildren() do
		if kf.ClassName == "Keyframe" then
			for name in poseMap(kf) do
				names[name] = true
			end
		end
	end
	local candidates = {}
	for _, service in { "ServerStorage", "ReplicatedStorage", "Workspace" } do
		for _, d in dm:GetService(service):GetDescendants() do
			if d.ClassName == "Model" and d.PrimaryPart and (not wanted or d.Name == wanted) then
				local have = { [d.PrimaryPart.Name] = true }
				for _, part in d:GetDescendants() do
					if part:IsA("BasePart") then
						have[part.Name] = true
					end
				end
				local missing = {}
				for name in names do
					if not have[name] then
						table.insert(missing, name)
					end
				end
				table.insert(candidates, { model = d, missing = missing })
			end
		end
	end
	local best
	for _, c in candidates do
		if #c.missing == 0 then
			if not best or (sequence.Name:sub(1, #c.model.Name) == c.model.Name and #c.model.Name > #best.model.Name) then
				best = c
			end
		end
	end
	if not best then
		local near = {}
		for _, c in candidates do
			table.insert(near, `{c.model.Name} (missing {table.concat(c.missing, ", ")})`)
		end
		print(`no model has every pose this clip keys ({sequence.Name}). candidates: {#near > 0 and table.concat(near, "; ") or "none"}. name one with --rig=<model name>`)
		process.exit(1)
	end
	return best.model, names
end

local function animationScene(dm, sequence)
	local rig, poseNames = findRig(dm, sequence, type(flags.rig) == "string" and flags.rig or nil)
	local jointByPart = {}
	for _, d in rig:GetDescendants() do
		if d.ClassName == "Motor6D" and d.Part0 and d.Part1 then
			jointByPart[d.Part1.Name] = true
		end
	end
	local unmatched = {}
	for name in poseNames do
		if not jointByPart[name] and name ~= rig.PrimaryPart.Name then
			table.insert(unmatched, name)
		end
	end
	table.sort(unmatched)

	-- the frames: every keyframe, or N evenly spaced samples
	local keyframes = {}
	for _, kf in sequence:GetChildren() do
		if kf.ClassName == "Keyframe" then
			table.insert(keyframes, { time = kf.Time, poses = poseMap(kf) })
		end
	end
	table.sort(keyframes, function(a, b)
		return a.time < b.time
	end)
	local length = keyframes[#keyframes].time
	local function sampleAt(t)
		local before, after = keyframes[1], keyframes[#keyframes]
		for _, kf in keyframes do
			if kf.time <= t then
				before = kf
			end
		end
		for i = #keyframes, 1, -1 do
			if keyframes[i].time >= t then
				after = keyframes[i]
			end
		end
		local alpha = after.time > before.time and (t - before.time) / (after.time - before.time) or 0
		local out = {}
		for name, a in before.poses do
			local b = after.poses[name] or a
			local cf = a.cf:Lerp(b.cf, alpha)
			out[name] = CFrame.new():Lerp(cf, a.weight + (b.weight - a.weight) * alpha)
		end
		return out
	end
	local frames = {}
	local wanted = tonumber(flags.samples)
	if wanted and wanted >= 2 then
		for i = 0, wanted - 1 do
			local t = length * i / (wanted - 1)
			table.insert(frames, { time = t, poses = sampleAt(t) })
		end
	else
		local step = math.max(1, math.ceil(#keyframes / 12)) -- at most 12 cells
		for i = 1, #keyframes, step do
			table.insert(frames, { time = keyframes[i].time, poses = sampleAt(keyframes[i].time) })
		end
	end

	-- rest bounds decide the spacing and the camera distance
	local restLow, restHigh = Scene.bounds(rig)
	local span = math.max((restHigh - restLow).X, (restHigh - restLow).Y, (restHigh - restLow).Z, 3)
	local spacing = span * 4 + 8 -- far enough apart that a neighbour never shows in a frame's edge
	local ws = dm:GetService("Workspace")
	local copies, views = {}, {}
	local angle = type(flags.angles) == "string" and flags.angles:match("[^,]+") or "+x+z"
	local direction = (ANGLE_DIRECTIONS[angle] or ANGLE_DIRECTIONS["+x+z"]).Unit
	local fov = tonumber(flags.fov) or 40
	local distance = (span / 2) / math.tan(math.rad(fov / 2)) * 1.3 + span * 0.5
	for i, frame in frames do
		local copy = rig:Clone()
		local offset = CFrame.new((i - 1) * spacing, 0, 0)
		for _, part in copy:GetDescendants() do
			if part:IsA("BasePart") then
				part.CFrame = offset * part.CFrame
			end
		end
		-- forward kinematics from the root outward
		local pending = {}
		for _, d in copy:GetDescendants() do
			if d.ClassName == "Motor6D" and d.Part0 and d.Part1 then
				table.insert(pending, d)
			end
		end
		local placed = { [copy.PrimaryPart] = true }
		local progress = true
		while #pending > 0 and progress do
			progress = false
			for k = #pending, 1, -1 do
				local j = pending[k]
				if placed[j.Part0] then
					j.Part1.CFrame = j.Part0.CFrame * j.C0 * (frame.poses[j.Part1.Name] or CFrame.new()) * j.C1:Inverse()
					placed[j.Part1] = true
					table.remove(pending, k)
					progress = true
				end
			end
		end
		copy.Parent = ws
		table.insert(copies, copy)
		local center = (restLow + restHigh) / 2 + V3.new((i - 1) * spacing, 0, 0)
		table.insert(views, {
			name = string.format("t=%.2fs", frame.time),
			cframe = { Scene.lookAt(center + direction * distance, center):GetComponents() },
			fov = fov,
		})
	end
	isolate(dm, copies, false, #copies * spacing + 80)
	local notes = {
		`animation {sequence.Name} on rig {rig.Name}: {#frames} frames of {string.format("%.2f", length)}s, angle {angle}`,
		#unmatched == 0 and "every pose in the clip matches a joint of the rig" or `WARNING: poses with no matching joint (they do nothing in the game): {table.concat(unmatched, ", ")}`,
	}
	return copies, views, notes
end

-- a dotted world path (Workspace.Map.Areas.Foo) that matches a source file or folder (src/Workspace/Map/Areas/Foo...) is built
-- on its own, like a src/ path: about a second instead of the whole project. no match (the name comes from inside a built file,
-- or from a script) falls through to the full build below.
local function sourceForWorldPath(p)
	if p:find("/", 1, true) or p:find("\\", 1, true) or p:match("%.luau$") or p:match("%.rbxm$") or p:match("%.anim$") then
		return nil
	end
	local parts = string.split(p, ".")
	if parts[1] == "game" then
		table.remove(parts, 1)
	end
	if #parts < 2 then
		return nil
	end
	local base = "src/" .. table.concat(parts, "/")
	if fs.isDir(root .. "/" .. base) then
		return base
	end
	for _, suffix in { ".build.luau", ".module.luau", ".client.luau", ".server.luau", ".luau" } do
		if fs.isFile(root .. "/" .. base .. suffix) then
			return base .. suffix
		end
	end
	return nil
end
if targetPath ~= nil and targetPath ~= "" then
	local unitPath = sourceForWorldPath(targetPath)
	if unitPath then
		targetPath = unitPath
	end
end

local isRbxm = targetPath ~= nil and targetPath:match("%.rbxm$") ~= nil
local isPath = not isRbxm and targetPath ~= nil and (targetPath:find("/", 1, true) or targetPath:find("\\", 1, true) or targetPath:match("%.luau$")) ~= nil
local animName = targetPath ~= nil and targetPath:gsub("\\", "/"):match("([^/]+)%.anim$") or nil
if animName then
	isPath = false -- an animation needs the whole project: the rig lives elsewhere
end
local dm, targets, label = nil, {}, "world"
local sequence, customViews, notes = nil, nil, {}

if isRbxm then
	local filePath = targetPath
	if not fs.isFile(filePath) then
		filePath = root .. "/" .. targetPath
	end
	if not fs.isFile(filePath) then
		print("no such rbxm file: " .. targetPath)
		process.exit(1)
	end
	local bytes = fs.readFile(filePath)
	local ok, models = pcall(roblox.deserializeModel, bytes)
	if not ok or not models or #models == 0 then
		print("failed to deserialize rbxm file: " .. tostring(models))
		process.exit(1)
	end
	local unitDm = roblox.Instance.new("DataModel")
	local ws = unitDm:GetService("Workspace")
	for _, m in models do
		m.Parent = ws
	end
	dm, label = unitDm, targetPath
	targets = stage(dm, models)
	isolate(dm, targets, false)
elseif isPath then
	-- a source file or folder, built on its own
	local ok, results, _, unitDm = pcall(Build.unit, root, targetPath, true)
	if not ok then
		print("build failed: " .. tostring(results))
		process.exit(1)
	end
	dm, label = unitDm, targetPath
	targets = stage(dm, results)
	isolate(dm, targets, targetPath:gsub("\\", "/"):match("^src/Lighting") ~= nil)
else
	local ok, built = pcall(Build.project, root, true)
	if not ok then
		print("build failed: " .. tostring(built))
		process.exit(1)
	end
	dm = built
	if animName then
		for _, d in dm:GetService("ReplicatedStorage"):GetDescendants() do
			if d.ClassName == "KeyframeSequence" and d.Name == animName then
				sequence = d
				break
			end
		end
		if not sequence then
			print("no animation named " .. animName .. " was built (is it under src/ReplicatedStorage?)")
			process.exit(1)
		end
		label = targetPath
	elseif targetPath and targetPath ~= "" then
		local parts = string.split(targetPath, ".")
		if parts[1] == "game" then
			table.remove(parts, 1)
		end
		local inst = dm:FindFirstChildOfClass(parts[1])
		if not inst then
			print(`no such service: {parts[1]}`)
			process.exit(1)
		end
		for i = 2, #parts do
			local nxt = inst:FindFirstChild(parts[i])
			if not nxt then
				local names = {}
				for _, c in inst:GetChildren() do
					table.insert(names, c.Name)
				end
				print(`no such path: {table.concat(parts, ".", 1, i)} (children of {table.concat(parts, ".", 1, i - 1)}: {table.concat(names, ", ")})`)
				process.exit(1)
			end
			inst = nxt
		end
		if inst.ClassName == "KeyframeSequence" then
			sequence, label = inst, targetPath
		else
			targets, label = stage(dm, { inst }), targetPath
		end
		if targets[1] and targets[1] ~= inst then -- it was not in the world (ServerStorage etc.): show just that
			isolate(dm, targets, false)
		end
	end
end

if sequence then
	targets, customViews, notes = animationScene(dm, sequence)
end

-- what kind of thing is it? a class check on the targets and their subtrees, no guessing
local function insideGui(inst)
	local n = inst
	while n and n ~= dm do
		if n:IsA("LayerCollector") or n.ClassName == "StarterGui" or n.ClassName == "PlayerGui" then
			return true
		end
		n = n.Parent
	end
	return false
end
local hasParts, hasGui, partCount = false, false, 0
if #targets == 0 then
	hasParts = true
	for _, d in dm:GetService("Workspace"):GetDescendants() do
		if d:IsA("BasePart") then
			partCount += 1
		end
	end
	hasGui = #dm:GetService("StarterGui"):GetChildren() > 0
else
	for _, target in targets do
		hasGui = hasGui or insideGui(target)
		local nodes = target:GetDescendants()
		table.insert(nodes, target)
		for _, d in nodes do
			if d:IsA("BasePart") then
				hasParts = true
				partCount += 1
			elseif d.ClassName == "ScreenGui" then
				hasGui = true
			end
		end
	end
end

local function lightingNode(state)
	return Scene.node(dm:GetService("Lighting"), state)
end

local function baseScene(state, screen)
	local w, h = screen[1], screen[2]
	return {
		version = 3,
		bubbles = {},
		time = 0,
		screen = { w, h },
		safeArea = { w, h - 58 },
		topbarInset = { 0, 0, 0, 0 },
		preferredInput = "KeyboardAndMouse",
		terrain = {},
		lighting = lightingNode(state),
		notes = { project = root, target = label },
	}
end

local scenes = {}

-- world scene: the whole workspace for context, camera fitted to the targets
if hasParts then
	local state = Scene.newState()
	local ws = dm:GetService("Workspace")
	local children = {}
	for _, c in ws:GetChildren() do
		if c.ClassName ~= "Terrain" and c.ClassName ~= "Camera" then
			table.insert(children, Scene.node(c, state))
		end
	end
	state.nextId += 1
	local scene = baseScene(state, { 1280, 720 })
	scene.workspace = { id = state.nextId, class = "Workspace", name = "Workspace", props = {}, children = children }

	-- bounds of the targets (or the whole workspace), ignoring the floor we added
	local lo, hi
	local function addBounds(root2)
		local a, b = Scene.bounds(root2)
		if a then
			lo = lo and V3.new(math.min(lo.X, a.X), math.min(lo.Y, a.Y), math.min(lo.Z, a.Z)) or a
			hi = hi and V3.new(math.max(hi.X, b.X), math.max(hi.Y, b.Y), math.max(hi.Z, b.Z)) or b
		end
	end
	if #targets == 0 then
		addBounds(ws)
	else
		for _, target in targets do
			addBounds(target)
		end
	end
	local views = {}
	if customViews then
		views = customViews
	elseif type(flags.cframe) == "string" then
		local n = {}
		for x in flags.cframe:gmatch("[^,]+") do
			table.insert(n, tonumber(x))
		end
		assert(#n == 12, "--cframe needs 12 numbers: x,y,z,r00,r01,r02,r10,r11,r12,r20,r21,r22")
		table.insert(views, { name = "custom", cframe = n, fov = tonumber(flags.fov) or 55 })
	elseif lo then
		local c = (lo + hi) / 2
		local size = math.max(hi.X - lo.X, hi.Y - lo.Y, hi.Z - lo.Z, 4)
		local fov = tonumber(flags.fov) or 55
		-- distance so the whole box fits the frame
		local d = (size / 2) / math.tan(math.rad(fov / 2)) * 1.25 + size * 0.35
		local function view(name, dx, dy, dz)
			local dir = V3.new(dx, dy, dz).Unit
			table.insert(views, { name = name, cframe = { Scene.lookAt(c + dir * d, c):GetComponents() }, fov = fov })
		end
		view("top", 0.001, 1, 0.12)
		view("+x", 1, 0.3, 0)
		view("-x", -1, 0.3, 0)
		view("+z", 0, 0.3, 1)
		view("-z", 0, 0.3, -1)
		view("+x+z", 0.7, 0.5, 0.7)
		view("+x-z", 0.7, 0.5, -0.7)
		view("-x+z", -0.7, 0.5, 0.7)
		view("-x-z", -0.7, 0.5, -0.7)
	end
	if type(flags.angles) == "string" and not customViews then
		local want = {}
		for a in flags.angles:gmatch("[^,]+") do
			want[a] = true
		end
		local kept = {}
		for _, v in views do
			if want[v.name] then
				table.insert(kept, v)
			end
		end
		views = kept
	end
	if #views == 0 then
		print("no parts to look at in " .. label)
		process.exit(1)
	end
	scene.camera = { cframe = views[1].cframe, fov = views[1].fov }
	local file = outDir .. "/world.json"
	fs.writeFile(file, serde.encode("json", scene))
	table.insert(scenes, { kind = "world", file = file, views = views, screen = scene.screen })
end

-- gui scenes: the target ScreenGui(s) forced on, at desktop and phone size
if hasGui then
	local sources = {}
	if #targets == 0 or (#targets == 1 and targets[1].ClassName == "StarterGui") then
		sources = dm:GetService("StarterGui"):GetChildren()
	else
		for _, target in targets do
			local g = target
			while g and not g:IsA("LayerCollector") and g.Parent and g.Parent ~= dm do
				g = g.Parent
			end
			local pick = (g and g:IsA("LayerCollector")) and g or target
			if not table.find(sources, pick) then
				table.insert(sources, pick)
			end
		end
	end
	for _, screen in { { "desktop", 1280, 720 }, { "phone", 844, 390 } } do
		local state = Scene.newState()
		local children = {}
		for _, s in sources do
			local node = Scene.node(s, state)
			if s:IsA("LayerCollector") then
				node.props.Enabled = true
			end
			table.insert(children, node)
		end
		local scene = baseScene(state, { screen[2], screen[3] })
		state.nextId += 1
		scene.workspace = { id = state.nextId, class = "Workspace", name = "Workspace", props = {}, children = {} }
		scene.playerGui = { id = state.nextId + 1, class = "PlayerGui", name = "PlayerGui", props = {}, children = children }
		scene.camera = { cframe = { 0, 10, 20, 1, 0, 0, 0, 1, 0, 0, 0, 1 }, fov = 55 }
		local file = `{outDir}/gui_{screen[1]}.json`
		fs.writeFile(file, serde.encode("json", scene))
		table.insert(scenes, { kind = "gui", name = screen[1], file = file, views = { { name = screen[1], cframe = scene.camera.cframe, fov = scene.camera.fov } }, screen = scene.screen })
	end
end

if #scenes == 0 then
	print("nothing to view in " .. label)
	process.exit(1)
end
fs.writeFile(outDir .. "/manifest.json", serde.encode("json", { target = label, scenes = scenes, parts = partCount, notes = notes }))
