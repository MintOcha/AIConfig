--!nocheck
-- lune run tester/run.luau <project-dir> [name-filter]
-- builds the project, then runs every tests/*.test.luau against the headless host.
-- test files are plain scripts. real roblox api works (workspace, game, require, Instance.new...).
-- the harness adds: addPlayer, advance, waitUntil, walkTo, teleport, fireServer, snapshot, check
local fs = require("@lune/fs")
local luau = require("@lune/luau")
local process = require("@lune/process")
local roblox = require("@lune/roblox")
local Build = require("../core/build")
local Host = require("./host")
local Proxy = require("../core/proxy")
local Frames = require("../renderer/frames")
local serde = require("@lune/serde")
local snapshots, snapshotCount, file_label = {}, 0, ""

local root = process.args[1]
local filter = process.args[2]
assert(root, "usage: run.luau <project-dir> [name-filter]")

local t0 = os.clock()
local built, dm = pcall(Build.project, root, true)
if not built then
	print("build failed: " .. tostring(dm))
	process.exit(1)
end
local serializedBase = roblox.serializeModel(dm:GetChildren())

local files = {}
for _, name in fs.readDir(root .. "/tests") do
	if name:match("%.test%.luau$") and (not filter or name:find(filter, 1, true)) then
		table.insert(files, name)
	end
end
table.sort(files)
if #files == 0 then
	print("no tests found in tests/")
	process.exit(1)
end

local passed, failed = 0, 0
for _, file in files do
	local tree = roblox.Instance.new("DataModel")
	for _, inst in roblox.deserializeModel(serializedBase) do
		inst.Parent = tree
	end
	local host = Host.new(tree)
	host:boot()
	local results = {}
	file_label = (file:gsub("%.test%.luau$", ""))
	local env = {}
	for name, value in roblox do
		env[name] = value
	end
	for name, value in host.globals do
		env[name] = value
	end
	env.CFrame = require("../core/cframe") -- lookAt faces the way roblox's does
	-- instances a test makes are wrapped like the ones game scripts make, so they mix with services and with each other
	env.Instance = {
		new = function(className)
			return Proxy.wrap(roblox.Instance.new(className), "server")
		end,
	}
	env.game = host:gameFor("server")
	env.workspace = host:service("server", "Workspace")
	env.require = function(m)
		return host:require(m, "server")
	end
	env.addPlayer = function(name, pos)
		return host:addPlayer(name, pos)
	end
	-- teleport(player, position or CFrame, lookAt): put the player somewhere now. a position can take a point to face.
	env.teleport = function(player, target, lookAt)
		host:teleport(player, target, lookAt)
	end
	env.walkTo = function(player, pos, speed)
		host:movePlayerTo(player, pos, speed)
	end
	-- advance(seconds, step): step is the time per tick (default 1/60). bigger steps make long simulations fast.
	env.advance = function(seconds, step)
		host:advance(seconds, step)
	end
	-- advance until fn() is true (or timeout seconds pass); returns whether it became true
	env.waitUntil = function(fn, timeout, step)
		step = step or 1 / 60
		local waited = 0
		while waited < (timeout or 5) do
			if fn() then
				return true
			end
			host:step(step)
			waited += step
		end
		return fn() and true or false
	end
	-- the test acts as a client: fireServer(player, remote, ...)
	env.fireServer = function(player, remote, ...)
		host:clientFire(player, remote, ...)
	end
	env.tagged = function(tag)
		return host:service("server", "CollectionService"):GetTagged(tag)
	end
	-- snapshot() / snapshot(thing) / snapshot(thing, cframe) / snapshot(thing, cframe, label): a picture of the world as it is right now (after the
	-- game scripts ran). the scene is exported here, in this same process; when the run ends `main.py test` draws every snapshot into one filmstrip.
	--   thing  an instance to look at (framed on its own); leave it out for the player's view with their screens over the world
	--   cframe where the camera is (it looks along the CFrame)
	env.snapshot = function(thing, cframe, label)
		if typeof(thing) == "string" then
			thing, cframe, label = nil, nil, thing
		elseif typeof(thing) == "CFrame" then
			thing, cframe, label = nil, thing, cframe
		end
		local request = { gui = host.records[1] and host.records[1].gui, cframe = cframe and { cframe:GetComponents() } or nil }
		if thing ~= nil then
			local raw = Proxy.unwrap(thing)
			local names = {}
			local walk = raw
			while walk and walk ~= tree do
				table.insert(names, 1, walk.Name)
				walk = walk.Parent
			end
			if names[1] == "Workspace" then
				request.target = raw
			else
				-- not in the world: use where it is (its own CFrame) and draw the world around that spot
				local ok, cf = pcall(function()
					return thing:GetPivot()
				end)
				if not ok then
					ok, cf = pcall(function()
						return thing.CFrame
					end)
				end
				local where = table.concat(names, ".")
				if not (ok and cf) then
					print(`snapshot: {where} is not under Workspace ({names[1]}) and has no position. nothing drawn.`)
					return
				end
				request.at = { cf.Position.X, cf.Position.Y, cf.Position.Z }
				print(`snapshot: {where} is not under Workspace ({names[1]}): drawing the world around its position ({string.format("%.0f, %.0f, %.0f", request.at[1], request.at[2], request.at[3])})`)
			end
		end
		host:syncBodies() -- bodies follow wherever the scripts moved the roots
		if not fs.isDir(root .. "/dist/snapshots") then
			fs.writeDir(root .. "/dist/snapshots")
		end
		snapshotCount += 1
		local frame = Frames.make(tree, request)
		local file = `dist/snapshots/{snapshotCount}.json`
		fs.writeFile(`{root}/{file}`, serde.encode("json", frame.scene))
		table.insert(snapshots, {
			file = file,
			views = frame.views,
			topbar = frame.topbar,
			label = label or (thing ~= nil and thing.Name) or "world",
			t = host.now,
			test = file_label,
		})
	end
	env.check = function(name, ok, detail)
		table.insert(results, { name = name, ok = ok and true or false, detail = detail })
	end
	require("./spec").install(env)
	env.print = print
	setmetatable(env, { __index = _G })

	local source = fs.readFile(root .. "/tests/" .. file)
	local loaded, chunk = pcall(luau.load, source, { debugName = file, environment = env })
	local ran, err
	if loaded then
		ran, err = pcall(chunk)
	else
		ran, err = false, chunk
	end
	print(file)
	if not ran then
		print("  ERROR " .. tostring(err):match("[^\n]*"))
		failed += 1
	end
	for _, r in results do
		if r.ok then
			passed += 1
			print("  PASS " .. r.name)
		else
			failed += 1
			print("  FAIL " .. r.name .. (r.detail and (" - " .. r.detail) or ""))
		end
	end
	for _, e in host.errors do
		failed += 1
		print("  SCRIPT ERROR " .. e:match("[^\n]*"))
	end
	local untested = {}
	for label in host.untested do
		table.insert(untested, label)
	end
	if #untested > 0 then
		table.sort(untested)
		print("  untested (not emulated, assumed to work): " .. table.concat(untested, ", "))
	end
end

if #snapshots > 0 then
	fs.writeFile(root .. "/dist/snapshots/requests.json", serde.encode("json", snapshots))
end

print(`{passed} passed, {failed} failed ({math.floor((os.clock() - t0) * 1000)} ms)`)
if failed > 0 then
	process.exit(1)
end
