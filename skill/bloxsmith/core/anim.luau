--!nocheck
-- the .anim text format -> a real KeyframeSequence. one-way (no round trip), ported from the v1 format.
--
--   clip "Crawl" loop                          name must match the file name; loop is optional
--   (no units line: times are always seconds and rotations always degrees)
--   priority Action                            Core Idle Movement Action Action2 Action3 Action4 (default Core)
--   keyframe 1 0.5 "Windup"                    optional: id, time, name. needed only for two keyframes at the same time
--   marker 1 "Hit" "true"                      optional: keyframe id, name, value (no id = the first keyframe)
--   rig R6                                     optional (R6 | R15): lets a track name just the part: track "Right Arm"
--   track [["HumanoidRootPart",1],["LowerTorso",1]]     pose path from the root: [name, nth sibling with that name]
--   track HumanoidRootPart/Torso/Right Arm     the same as a slash path (nth sibling 1 everywhere)
--     0 move=0,-1,0 rotate=-45,0,0             time, then any of: move=x,y,z  rotate=x,y,z (degrees)  matrix=12 numbers
--     1.2 weight=0.5 style=Cubic direction=Out    weight= mask= style= direction= frame=
--   # comments and blank lines are ignored
-- ancestors of a keyed pose that are not keyed at that time are filled in for you: with their own value at that time
-- (linear between their keys) if they are animated, else a still weight=0 pose. so key only the poses you move.
local roblox = require("@lune/roblox")
local serde = require("@lune/serde")

local Anim = {}

local STYLES = { Linear = true, Constant = true, Elastic = true, Cubic = true, CubicV2 = true, Bounce = true }
local DIRECTIONS = { In = true, Out = true, InOut = true }
-- pose chains of the standard rigs, for `rig R6` / `rig R15` + short track names
local RIGS = {
	R6 = {
		Torso = { "HumanoidRootPart" },
		Head = { "HumanoidRootPart", "Torso" },
		["Left Arm"] = { "HumanoidRootPart", "Torso" },
		["Right Arm"] = { "HumanoidRootPart", "Torso" },
		["Left Leg"] = { "HumanoidRootPart", "Torso" },
		["Right Leg"] = { "HumanoidRootPart", "Torso" },
	},
	R15 = {
		LowerTorso = { "HumanoidRootPart" },
		UpperTorso = { "HumanoidRootPart", "LowerTorso" },
		Head = { "HumanoidRootPart", "LowerTorso", "UpperTorso" },
	},
}
for _, side in { "Left", "Right" } do
	local arm = { "HumanoidRootPart", "LowerTorso", "UpperTorso" }
	RIGS.R15[side .. "UpperArm"] = arm
	RIGS.R15[side .. "LowerArm"] = { arm[1], arm[2], arm[3], side .. "UpperArm" }
	RIGS.R15[side .. "Hand"] = { arm[1], arm[2], arm[3], side .. "UpperArm", side .. "LowerArm" }
	RIGS.R15[side .. "UpperLeg"] = { "HumanoidRootPart", "LowerTorso" }
	RIGS.R15[side .. "LowerLeg"] = { "HumanoidRootPart", "LowerTorso", side .. "UpperLeg" }
	RIGS.R15[side .. "Foot"] = { "HumanoidRootPart", "LowerTorso", side .. "UpperLeg", side .. "LowerLeg" }
end

local PRIORITIES = { Core = true, Idle = true, Movement = true, Action = true, Action2 = true, Action3 = true, Action4 = true }

local function tokenize(line)
	local tokens = {}
	local i = 1
	while i <= #line do
		local c = line:sub(i, i)
		if c:match("%s") then
			i += 1
		elseif c == '"' then
			local j = i + 1
			while j <= #line do
				local d = line:sub(j, j)
				if d == "\\" then
					j += 2
				elseif d == '"' then
					break
				else
					j += 1
				end
			end
			table.insert(tokens, serde.decode("json", line:sub(i, j)))
			i = j + 1
		else
			local j = i
			while j <= #line and not line:sub(j, j):match("%s") do
				j += 1
			end
			table.insert(tokens, line:sub(i, j - 1))
			i = j
		end
	end
	return tokens
end

local function enumToken(value, enumName, allowed)
	local prefix = "Enum." .. enumName .. "."
	local token = value:sub(1, #prefix) == prefix and value:sub(#prefix + 1) or value
	if not allowed[token] then
		local names = {}
		for name in allowed do
			table.insert(names, name)
		end
		table.sort(names)
		error(`expected {prefix}X where X is one of: {table.concat(names, ", ")} (got {value})`, 0)
	end
	return token
end

local function numbers(data, count, what)
	local out = {}
	for part in data:gmatch("[^,]+") do
		local n = tonumber(part)
		if not n or n ~= n or n == math.huge or n == -math.huge then
			error(`{what}: '{part}' is not a finite number`, 0)
		end
		table.insert(out, n)
	end
	if #out ~= count then
		error(`{what} needs {count} numbers, got {#out}`, 0)
	end
	return out
end

-- parse text -> clip table. path is only used in error messages.
function Anim.parse(source, path)
	local clip = { frames = {}, tracks = {}, markers = {} }
	local track = nil
	local keyLines = {}
	local lineNo = 0
	local function fail(message)
		error(`{path}:{lineNo}: {message}`, 0)
	end
	local seenTracks = {}
	local pendingShort = {} -- track index -> line, for single-name tracks a rig line may expand

	for raw in (source .. "\n"):gmatch("(.-)\r?\n") do
		lineNo += 1
		local line = raw:match("^%s*(.-)%s*$")
		if line == "" or line:sub(1, 1) == "#" then
			continue
		end
		if line:sub(1, 1) == "[" then
			fail("bracket lines ([EasingStyle = ...], [attribute ...]) only appear in pulled files. use style=/direction= on the key line")
		end
		if line:sub(1, 6) == "track " then
			local rest = line:sub(7):match("^%s*(.-)%s*$")
			local trackPath
			if rest:sub(1, 1) == "[" then
				local ok, decoded = pcall(serde.decode, "json", rest)
				if not ok then
					fail('track path must be JSON like [["HumanoidRootPart",1],["LowerTorso",1]]')
				end
				trackPath = decoded
			elseif rest:find("/", 1, true) then
				trackPath = {}
				for name in rest:gsub('"', ""):gmatch("[^/]+") do
					table.insert(trackPath, (name:match("^%s*(.-)%s*$")))
				end
			else
				local quote = rest:sub(1, 1)
				local name = rest
				if #rest >= 2 and (quote == '"' or quote == "'") and rest:sub(-1) == quote then
					name = rest:sub(2, -2)
				end
				trackPath = { name }
				pendingShort[#clip.tracks + 1] = lineNo
			end
			if type(trackPath) ~= "table" or #trackPath == 0 then
				fail("a track needs at least one pose name")
			end
			for i, segment in trackPath do
				if type(segment) == "string" then
					segment = { segment, 1 }
					trackPath[i] = segment
				end
				if type(segment) ~= "table" or #segment ~= 2 or type(segment[1]) ~= "string" or type(segment[2]) ~= "number" or segment[2] < 1 or segment[2] % 1 ~= 0 then
					fail('each track segment is a name or ["Name", nthSibling] with nthSibling = 1, 2, ...')
				end
			end
			local id = serde.encode("json", trackPath)
			if seenTracks[id] then
				fail("duplicate track " .. id)
			end
			seenTracks[id] = true
			track = { path = trackPath, keys = {} }
			table.insert(clip.tracks, track)
			continue
		end

		local tokens = tokenize(line)
		local head = tokens[1]
		if head == "clip" then
			if clip.name then
				fail("more than one clip line")
			end
			if #tokens < 2 or #tokens > 3 or (tokens[3] and tokens[3] ~= "loop") then
				fail('expected: clip "Name" [loop]')
			end
			clip.name, clip.loop = tokens[2], tokens[3] == "loop"
		elseif head == "units" then
			fail("remove this line: times are always seconds and rotations always degrees")
		elseif head == "priority" then
			if clip.priority or #tokens ~= 2 then
				fail("expected: priority <Core|Idle|Movement|Action|Action2|Action3|Action4> (once)")
			end
			local ok, value = pcall(enumToken, tokens[2], "AnimationPriority", PRIORITIES)
			if not ok then
				fail(value)
			end
			clip.priority = value
		elseif head == "native" then
			fail("native snapshot lines only appear in pulled files. delete the line")
		elseif head == "keyframe" then
			if #tokens ~= 4 or not tonumber(tokens[2]) or not tonumber(tokens[3]) then
				fail('expected: keyframe <id> <time> "Name"')
			end
			table.insert(clip.frames, { id = tonumber(tokens[2]), time = tonumber(tokens[3]), name = tokens[4] })
		elseif head == "rig" then
			if clip.rig or #tokens ~= 2 or not RIGS[tokens[2]] then
				fail("expected: rig R6 | rig R15 (once)")
			end
			clip.rig = tokens[2]
		elseif head == "marker" then
			if #tokens == 3 then
				table.insert(clip.markers, { frame = "first", name = tokens[2], value = tokens[3] })
			elseif #tokens == 4 and tonumber(tokens[2]) then
				table.insert(clip.markers, { frame = tonumber(tokens[2]), name = tokens[3], value = tokens[4] })
			else
				fail('expected: marker "Name" "Value" (first keyframe) or marker <keyframe id> "Name" "Value"')
			end
		else
			-- a key line: time, then fields
			local time = tonumber(head)
			if not time then
				fail(`unknown statement '{tostring(head)}'`)
			end
			if not track then
				fail("a key line must come after a track line")
			end
			if time ~= time or time < 0 or time == math.huge then
				fail("time must be a finite number >= 0")
			end
			local key = { time = time }
			keyLines[key] = lineNo
			for i = 2, #tokens do
				local name, data = tokens[i]:match("^(%w+)=(.*)$")
				if not name then
					fail(`expected name=value, got '{tokens[i]}'`)
				end
				name = (name == "EasingStyle" and "style") or (name == "EasingDirection" and "direction") or name
				if key[name] ~= nil then
					fail(`duplicate field {name}`)
				end
				local ok, err = pcall(function()
					if name == "move" or name == "rotate" then
						key[name] = numbers(data, 3, name)
					elseif name == "matrix" then
						key[name] = numbers(data, 12, name)
					elseif name == "weight" or name == "mask" then
						key[name] = numbers(data, 1, name)[1]
					elseif name == "frame" then
						key[name] = numbers(data, 1, name)[1]
					elseif name == "style" then
						key[name] = enumToken(data, "PoseEasingStyle", STYLES)
					elseif name == "direction" then
						key[name] = enumToken(data, "PoseEasingDirection", DIRECTIONS)
					else
						error(`unknown key field '{name}' (move, rotate, matrix, weight, mask, style, direction, frame)`, 0)
					end
				end)
				if not ok then
					fail(tostring(err))
				end
			end
			if key.matrix and (key.move or key.rotate) then
				fail("matrix cannot be combined with move/rotate")
			end
			table.insert(track.keys, key)
		end
	end

	local function require_(condition, message)
		if not condition then
			error(`{path}: {message}`, 0)
		end
	end
	require_(clip.name ~= nil, 'an animation needs a clip "Name" line')
	local expanded = {}
	for index, t in clip.tracks do
		if not pendingShort[index] then
			expanded[serde.encode("json", t.path)] = true
		end
	end
	for index, at in pendingShort do
		local t = clip.tracks[index]
		local name = t.path[1][1]
		local chain = clip.rig and RIGS[clip.rig][name]
		if chain then
			local full = {}
			for _, n in chain do
				table.insert(full, { n, 1 })
			end
			table.insert(full, { name, 1 })
			t.path = full
		elseif clip.rig == nil and name ~= "HumanoidRootPart" and (RIGS.R6[name] or RIGS.R15[name]) then
			error(`{path}:{at}: track "{name}" needs its chain: add a "rig R6" (or R15) line, or write the path (HumanoidRootPart/Torso/{name})`, 0)
		end
		local id = serde.encode("json", t.path)
		require_(not expanded[id], `duplicate track {id}`)
		expanded[id] = true
	end
	clip.priority = clip.priority or "Core"

	-- keyframes: declared ones, plus one per distinct key time that has none
	local frames, count = {}, 0
	for _, f in clip.frames do
		require_(f.id >= 1 and f.id % 1 == 0 and not frames[f.id], `keyframe id {f.id} is duplicate or not a positive integer`)
		require_(f.time >= 0, "keyframe times must be >= 0")
		frames[f.id] = f
		count += 1
	end
	local function framesAt(time)
		local found = {}
		for _, f in frames do
			if f.time == time then
				table.insert(found, f)
			end
		end
		return found
	end
	for _, t in clip.tracks do
		local seen = {}
		for _, key in t.keys do
			local where = `{path}:{keyLines[key]}`
			if not key.frame then
				local matches = framesAt(key.time)
				if #matches > 1 then
					error(`{where}: two keyframes share time {key.time}, say which one with frame=`, 0)
				end
				if #matches == 0 then
					local id = 0
					for existing in frames do
						id = math.max(id, existing)
					end
					id += 1
					frames[id] = { id = id, time = key.time, name = "Keyframe" }
					count += 1
					matches = { frames[id] }
				end
				key.frame = matches[1].id
			end
			local frame = frames[key.frame]
			if not frame or frame.time ~= key.time then
				error(`{where}: frame={key.frame} does not exist or is at a different time`, 0)
			end
			if seen[key.frame] then
				error(`{where}: this pose is already keyed in keyframe {key.frame}`, 0)
			end
			seen[key.frame] = true
		end
	end
	for _, m in clip.markers do
		if m.frame == "first" then
			local first
			for _, f in frames do
				if not first or f.time < first.time or (f.time == first.time and f.id < first.id) then
					first = f
				end
			end
			m.frame = first and first.id
		end
		require_(m.frame and frames[m.frame] ~= nil, `marker '{m.name}' refers to missing keyframe {tostring(m.frame)}`)
	end
	require_(count > 0, "an animation needs at least one keyframe (a key line in a track)")
	clip.frames = {}
	for _, f in frames do
		table.insert(clip.frames, f)
	end
	table.sort(clip.frames, function(a, b)
		return a.id < b.id
	end)
	return clip
end

-- clip table -> KeyframeSequence (raw Lune instance, not parented)
function Anim.build(clip, path)
	local Instance, CFrame, Enum = roblox.Instance, roblox.CFrame, roblox.Enum
	local seq = Instance.new("KeyframeSequence")
	seq.Name = clip.name
	seq.Loop = clip.loop
	seq.Priority = Enum.AnimationPriority[clip.priority]

	local frames, poses = {}, {}
	for _, f in clip.frames do
		local frame = Instance.new("Keyframe")
		frame.Name = f.name
		frame.Time = f.time
		frame.Parent = seq
		frames[f.id], poses[f.id] = frame, {}
	end

	-- shallow tracks first so ancestors exist before their children
	local ordered = table.clone(clip.tracks)
	table.sort(ordered, function(a, b)
		if #a.path ~= #b.path then
			return #a.path < #b.path
		end
		for i, segment in a.path do
			local other = b.path[i]
			if segment[1] ~= other[1] then
				return segment[1] < other[1]
			end
			if segment[2] ~= other[2] then
				return segment[2] < other[2]
			end
		end
		return false
	end)

	local function keyCFrame(key)
		if key.matrix then
			return CFrame.new(table.unpack(key.matrix))
		end
		local move, rotate = key.move or { 0, 0, 0 }, key.rotate or { 0, 0, 0 }
		return CFrame.new(move[1], move[2], move[3]) * CFrame.Angles(math.rad(rotate[1]), math.rad(rotate[2]), math.rad(rotate[3]))
	end
	-- a track's value at time t from its own keys (linear between neighbours, held before the first / after the last)
	local byPath = {}
	for _, t in clip.tracks do
		local sorted = table.clone(t.keys)
		table.sort(sorted, function(a, b)
			return a.time < b.time
		end)
		byPath[serde.encode("json", t.path)] = sorted
	end
	local function sampleTrack(keys, t)
		if t <= keys[1].time then
			return keyCFrame(keys[1]), keys[1]
		end
		for i = 2, #keys do
			local a, b = keys[i - 1], keys[i]
			if t <= b.time then
				local alpha = (t - a.time) / math.max(b.time - a.time, 1e-9)
				return keyCFrame(a):Lerp(keyCFrame(b), alpha), a
			end
		end
		return keyCFrame(keys[#keys]), keys[#keys]
	end

	for _, track in ordered do
		for _, key in track.keys do
			local parent = frames[key.frame]
			local walked = {}
			for depth, segment in track.path do
				table.insert(walked, segment)
				local id = serde.encode("json", walked)
				local pose = poses[key.frame][id]
				if depth < #track.path then
					if not pose then
						-- an ancestor not keyed at this time. if it is animated elsewhere it gets its own value here
						-- (interpolated from its keys), so filling the chain never changes the motion; otherwise a still,
						-- weightless pose
						pose = Instance.new("Pose")
						pose.Name = segment[1]
						local own = byPath[id]
						if own and #own > 0 then
							local cf, from = sampleTrack(own, key.time)
							pose.CFrame = cf
							pose.Weight = 1
							pose.EasingStyle = Enum.PoseEasingStyle[from.style or "Linear"]
							pose.EasingDirection = Enum.PoseEasingDirection[from.direction or "In"]
						else
							pose.Weight = 0
						end
						pose.Parent = parent
						poses[key.frame][id] = pose
					end
				else
					local occurrence = 1
					for _, sibling in parent:GetChildren() do
						if sibling:IsA("Pose") and sibling.Name == segment[1] then
							occurrence += 1
						end
					end
					if segment[2] ~= occurrence then
						error(`{path}: pose {segment[1]} is sibling number {segment[2]} but {occurrence - 1} earlier ones are keyed at time {key.time}`, 0)
					end
					pose = Instance.new("Pose")
					pose.Name = segment[1]
					pose.Parent = parent
					poses[key.frame][id] = pose
					if key.matrix then
						pose.CFrame = CFrame.new(table.unpack(key.matrix))
					else
						local move, rotate = key.move or { 0, 0, 0 }, key.rotate or { 0, 0, 0 }
						pose.CFrame = CFrame.new(move[1], move[2], move[3]) * CFrame.Angles(math.rad(rotate[1]), math.rad(rotate[2]), math.rad(rotate[3]))
					end
					pose.Weight = key.weight or 1
					pose.MaskWeight = key.mask or 0
					pose.EasingStyle = Enum.PoseEasingStyle[key.style or "Linear"]
					pose.EasingDirection = Enum.PoseEasingDirection[key.direction or "In"]
				end
				parent = pose
			end
		end
	end

	for _, m in clip.markers do
		local marker = Instance.new("KeyframeMarker")
		marker.Name = m.name
		marker.Value = m.value
		marker.Parent = frames[m.frame]
	end
	return seq
end

-- text -> KeyframeSequence named after the file. the clip line's name must match the file name (one name, no guessing).
function Anim.compile(source, path, fileName)
	local clip = Anim.parse(source, path)
	if clip.name ~= fileName then
		error(`{path}: the clip line says "{clip.name}" but the file is {fileName}.anim. they must match (the file name is the animation's name)`, 0)
	end
	return Anim.build(clip, path)
end

return Anim
