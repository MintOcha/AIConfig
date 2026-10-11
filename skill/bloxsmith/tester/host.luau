--!nocheck
-- test host: runs real game scripts on a virtual clock against a built DataModel.
-- server scripts and each player's client scripts run in separate realms: they see different proxies, so
-- remote:FireServer only works from a client and Players.LocalPlayer only exists on a client.
-- anything not emulated errors loudly (members) or is inert and reported as untested (events); see features.luau.
local serde = require("@lune/serde")
local roblox = require("@lune/roblox")
local luau = require("@lune/luau")
local Proxy = require("../core/proxy")
local Signal = require("./signal")
local CFrameFixed = require("../core/cframe")
local RandomFixed = require("../core/random")
local Features = require("./features")

local Instance, Vector3, CFrame, Color3 = roblox.Instance, roblox.Vector3, roblox.CFrame, roblox.Color3
local wrap, unwrap = Proxy.wrap, Proxy.unwrap

local SERVER = "server"
local function isClient(realm)
	return realm ~= nil and realm ~= SERVER
end

local function strict(name, t)
	return setmetatable(t, {
		__index = function(_, k)
			error(`{name}.{tostring(k)} is not emulated`, 2)
		end,
	})
end

local Host = {}
Host.__index = Host
Host.isClient = isClient
Host.strict = strict

function Host.new(dm)
	Proxy.verify = false -- runtime writes are hot; the build already checked the world
	local self = setmetatable({}, Host)
	self.dm = dm
	self.now = 0
	self.sleepers = {}
	self.errors = {}
	self.untested = {}
	self.records = {}
	self.views = {}
	self.recordOf = setmetatable({}, { __mode = "k" })
	self.modules = {}
	self.movers = {}
	self.queued = {}
	self.memberHandlers, self.writeHandlers, self.stepHandlers = {}, {}, {}
	self.serviceMakers, self.serviceCache, self.games = {}, {}, {}
	self.realmSignals = {}
	self.globals = {}
	self.nextUserId = 1
	for _, child in dm:GetChildren() do
		local ok, service = pcall(function() return dm:GetService(child.Name) end)
		if ok and service and service ~= child then
			for _, item in child:GetChildren() do
				item.Parent = service
			end
			child:Destroy()
		end
	end
	self.workspaceRaw = dm:GetService("Workspace")
	self.heartbeat = self:newSignal()
	self.postSimulation = self:newSignal()
	self.preSimulation = self:newSignal()
	self.stepped = self:newSignal()
	self.renderStepped = self:newSignal()

	local task = {}
	function task.wait(d)
		table.insert(self.sleepers, { t = self.now + (d or 0), co = coroutine.running() })
		coroutine.yield()
		return d or 0
	end
	function task.spawn(f, ...)
		return self:spawn(f, ...)
	end
	function task.delay(d, f, ...)
		local args = table.pack(...)
		return self:spawn(function()
			task.wait(d)
			f(table.unpack(args, 1, args.n))
		end)
	end
	function task.defer(f, ...)
		return task.delay(0, f, ...)
	end
	self.task = strict("task", task)

	self:installServices()

	Proxy.hooks.member = function(inst, key, realm)
		local class = inst.ClassName
		for _, handler in self.memberHandlers do
			local handled, value = handler(inst, key, realm, class)
			if handled then
				return true, value
			end
		end
		return false
	end
	Proxy.hooks.write = function(inst, key, value, realm)
		local class = inst.ClassName
		for _, handler in self.writeHandlers do
			if handler(inst, key, value, realm, class) then
				return true
			end
		end
		return false
	end
	Features.install(self)
	return self
end

function Host:newSignal()
	return Signal.new(function(f, ...)
		return self:spawn(f, ...)
	end, function(co, ...)
		self:resumeThread(co, ...)
	end)
end

function Host:report(err)
	table.insert(self.errors, tostring(err))
end

function Host:resumeThread(co, ...)
	local ok, err = coroutine.resume(co, ...)
	if not ok then
		self:report(err)
	end
end

function Host:spawn(f, ...)
	local co = coroutine.create(f)
	self:resumeThread(co, ...)
	return co
end

-- a signal per (name, realm); fireRealms fires them all with realm-specific arguments
function Host:realmSignal(name, realm)
	local by = self.realmSignals[name]
	if not by then
		by = {}
		self.realmSignals[name] = by
	end
	local key = realm == nil and "none" or realm
	if not by[key] then
		by[key] = { realm = realm, signal = self:newSignal() }
	end
	return by[key].signal
end

function Host:fireRealms(name, makeArgs)
	for _, entry in self.realmSignals[name] or {} do
		entry.signal:Fire(makeArgs(entry.realm))
	end
end

-- values crossing between realms: instance proxies and player views are re-expressed in the destination realm
function Host:convert(v, realm)
	if type(v) ~= "table" then
		return v
	end
	local rec = self.recordOf[v]
	if rec then
		return self:viewOf(rec, realm)
	end
	local raw = unwrap(v)
	if raw ~= v then
		return wrap(raw, realm)
	end
	if getmetatable(v) == nil then
		local out = {}
		for k, x in v do
			out[k] = self:convert(x, realm)
		end
		return out
	end
	return v
end

-- services -------------------------------------------------------------------------------------------------------

function Host:service(realm, name)
	local key = realm == nil and "none" or realm
	local cache = self.serviceCache[key]
	if not cache then
		cache = {}
		self.serviceCache[key] = cache
	end
	if cache[name] then
		return cache[name]
	end
	local made
	if self.serviceMakers[name] then
		made = self.serviceMakers[name](realm)
	else
		local found = self.dm:FindFirstChild(name)
		local ok, service = pcall(function()
			return self.dm:GetService(name)
		end)
		if ok and service and found and found ~= service then
			for _, child in found:GetChildren() do
				child.Parent = service
			end
			found:Destroy()
		end
		if not ok or not service then
			error(`game:GetService("{name}") is not available`, 3)
		end
		made = wrap(service, realm)
	end
	return made
end

Host.gameProperties = { PlaceId = 0, GameId = 0, PlaceVersion = 0, CreatorId = 0, JobId = "", PrivateServerId = "", PrivateServerOwnerId = 0 }

function Host:gameFor(realm)
	local key = realm == nil and "none" or realm
	if not self.games[key] then
		self.games[key] = setmetatable({
			GetService = function(_, name)
				return self:service(realm, name)
			end,
			BindToClose = function(_, fn)
				-- registered on server shutdown; no-op in headless test host
			end,
		}, {
			__index = function(_, name)
				-- DataModel properties (not services): a fresh local test server has no place and an empty job
				local plain = self.gameProperties[name]
				if plain ~= nil then
					return plain
				end
				return self:service(realm, name)
			end,
		})
	end
	return self.games[key]
end

function Host:installServices()
	self.serviceMakers.RunService = function(realm)
		return strict("RunService", {
			Heartbeat = self.heartbeat,
			PostSimulation = self.postSimulation,
			PreSimulation = self.preSimulation,
			Stepped = self.stepped,
			RenderStepped = self.renderStepped,
			IsClient = function()
				return isClient(realm)
			end,
			IsServer = function()
				return not isClient(realm)
			end,
			IsStudio = function()
				return true -- a local headless run is an unpublished place (PlaceId 0), which is what Studio reports too
			end,
			IsRunning = function()
				return true
			end,
			BindToRenderStep = function(_, name, priority, fn)
				return self.renderStepped:Connect(fn)
			end,
			UnbindFromRenderStep = function(_, name) end,
		})
	end

	-- HttpService: ids are a counter so runs are repeatable; JSON via serde. requests are not emulated and error.
	local guids = 0
	self.serviceMakers.HttpService = function()
		return strict("HttpService", {
			GenerateGUID = function(_, wrapInBraces)
				guids += 1
				local g = string.format("%08X-0000-4000-8000-%012X", guids, guids)
				if wrapInBraces == false then
					return g
				end
				return "{" .. g .. "}"
			end,
			JSONEncode = function(_, value)
				return serde.encode("json", value)
			end,
			JSONDecode = function(_, text)
				return serde.decode("json", text)
			end,
		})
	end

	-- TeleportService: only the failure event exists (never fires). anything else is a cloud call and errors as not emulated.
	self.serviceMakers.TeleportService = function()
		self.untested["TeleportService.TeleportInitFailed"] = true
		return strict("TeleportService", {
			TeleportInitFailed = self:newSignal(),
		})
	end
	-- PhysicsService: collision groups emulation for headless tests
	local collisionGroups = {}
	self.serviceMakers.PhysicsService = function()
		return strict("PhysicsService", {
			IsCollisionGroupRegistered = function(_, name)
				return collisionGroups[name] ~= nil
			end,
			RegisterCollisionGroup = function(_, name)
				if collisionGroups[name] == nil then
					collisionGroups[name] = { name = name }
				end
			end,
			GetRegisteredCollisionGroups = function(_)
				local list = {}
				for _, g in collisionGroups do
					table.insert(list, g)
				end
				return list
			end,
			CollisionGroupSetCollidable = function(_, g1, g2, collidable)
				-- recorded, physics simulation is not active in headless tests
			end,
			CollisionGroupsAreCollidable = function(_, g1, g2)
				return true
			end,
		})
	end


	-- MessagingService: cross-server messages go nowhere in a single local run. subscribing works, publishing is accepted and dropped.
	self.serviceMakers.MessagingService = function()
		self.untested["MessagingService delivery"] = true
		return strict("MessagingService", {
			SubscribeAsync = function()
				return { Disconnect = function() end }
			end,
			PublishAsync = function() end,
		})
	end

	self.serviceMakers.CollectionService = function(realm)
		return strict("CollectionService", {
			-- tag add/remove events never fire (inert), but a script can connect to them
			GetInstanceAddedSignal = function()
				self.untested["CollectionService.GetInstanceAddedSignal"] = true
				return self:newSignal()
			end,
			GetInstanceRemovedSignal = function()
				self.untested["CollectionService.GetInstanceRemovedSignal"] = true
				return self:newSignal()
			end,
			AddTag = function(_, inst, tag)
				unwrap(inst):AddTag(tag)
			end,
			RemoveTag = function(_, inst, tag)
				unwrap(inst):RemoveTag(tag)
			end,
			HasTag = function(_, inst, tag)
				return unwrap(inst):HasTag(tag)
			end,
			GetTagged = function(_, tag)
				local out = {}
				for _, d in self.dm:GetDescendants() do
					if d:HasTag(tag) then
						table.insert(out, wrap(d, realm))
					end
				end
				return out
			end,
		})
	end

	self.serviceMakers.Players = function(realm)
		return setmetatable({
			PlayerAdded = self:realmSignal("PlayerAdded", realm),
			PlayerRemoving = self:realmSignal("PlayerRemoving", realm), -- nothing removes a player in a test yet, so it never fires
			GetPlayers = function()
				local out = {}
				for _, rec in self.records do
					table.insert(out, self:viewOf(rec, realm))
				end
				return out
			end,
			GetPlayerFromCharacter = function(_, model)
				local raw = unwrap(model)
				for _, rec in self.records do
					if rec.char == raw then
						return self:viewOf(rec, realm)
					end
				end
				return nil
			end,
		}, {
			__index = function(_, k)
				if k == "LocalPlayer" then
					return isClient(realm) and self:viewOf(realm, realm) or nil
				elseif k == "GetUserThumbnailAsync" then
					return function(_, userId, thumbType, thumbSize)
						return "rbxassetid://0", true
					end
				end
				error(`Players.{tostring(k)} is not emulated`, 2)
			end,
		})
	end
end

-- players ----------------------------------------------------------------------------------------------------------

-- a player as seen from one realm. same player, different table per realm (scripts never compare across realms)
function Host:viewOf(rec, realm)
	local key = realm == nil and "none" or realm
	local views = self.views[key]
	if not views then
		views = {}
		self.views[key] = views
	end
	if views[rec] then
		return views[rec]
	end
	local view = setmetatable({}, {
		__index = function(_, k)
			return self:playerMember(rec, realm, k)
		end,
		__newindex = function(_, k)
			if k == "CameraMaxZoomDistance" or k == "CameraMinZoomDistance" then
				return -- the camera is not simulated
			end
			error(`Player.{tostring(k)} cannot be set in tests`, 2)
		end,
		__tostring = function()
			return rec.name
		end,
	})
	views[rec] = view
	self.recordOf[view] = rec
	return view
end

function Host:recSignal(rec, name, realm)
	local by = rec.sigs[name]
	if not by then
		by = {}
		rec.sigs[name] = by
	end
	local key = realm == nil and "none" or realm
	if not by[key] then
		by[key] = { realm = realm, signal = self:newSignal() }
	end
	return by[key].signal
end

function Host:playerMember(rec, realm, k)
	if k == "Name" or k == "DisplayName" then
		return rec.name
	elseif k == "UserId" then
		return rec.userId
	elseif k == "ClassName" then
		return "Player"
	elseif k == "Character" then
		return rec.char and wrap(rec.char, realm) or nil
	elseif k == "PlayerGui" then
		return wrap(rec.gui, realm)
	elseif k == "Parent" then
		return rec.parent and wrap(rec.parent, realm) or self:service(realm, "Players")
	elseif k == "FindFirstChild" then
		return function(_, name)
			if name == "PlayerGui" then
				return wrap(rec.gui, realm)
			elseif name == "Character" then
				return rec.char and wrap(rec.char, realm) or nil
			end
			return nil
		end
	elseif k == "WaitForChild" then
		return function(_, name)
			if name == "PlayerGui" then
				return wrap(rec.gui, realm)
			elseif name == "Character" then
				return rec.char and wrap(rec.char, realm) or nil
			end
			return nil
		end
	elseif k == "IsA" then
		return function(_, class)
			return class == "Player" or class == "Instance"
		end
	elseif k == "SetAttribute" then
		return function(_, name, value)
			if rec.attrs[name] ~= value then
				rec.attrs[name] = value
				for _, entry in rec.attrSignals[name] or {} do
					entry.signal:Fire()
				end
			end
		end
	elseif k == "GetAttribute" then
		return function(_, name)
			return rec.attrs[name]
		end
	elseif k == "GetAttributeChangedSignal" then
		return function(_, name)
			local by = rec.attrSignals[name]
			if not by then
				by = {}
				rec.attrSignals[name] = by
			end
			local key = realm == nil and "none" or realm
			if not by[key] then
				by[key] = { realm = realm, signal = self:newSignal() }
			end
			return by[key].signal
		end
	elseif k == "CharacterAdded" then
		return self:recSignal(rec, "CharacterAdded", realm)
	elseif k == "GetMouse" then
		-- a mouse that never moves or clicks: Hit and Target are nil, so scripts must cope with "not aiming at anything"
		return function()
			self.untested["Mouse (no cursor, no clicks)"] = true
			return { Hit = nil, Target = nil, TargetFilter = nil, Button1Down = self:newSignal(), Button1Up = self:newSignal(), Move = self:newSignal() }
		end
	elseif k == "CameraMaxZoomDistance" or k == "CameraMinZoomDistance" then
		return 128
	end
	error(`Player.{tostring(k)} is not emulated`, 2)
end

function Host:makeCharacter(rec, pos)
	local model = Instance.new("Model")
	model.Name = rec.name
	local root = Instance.new("Part")
	root.Name = "HumanoidRootPart"
	root.Size = Vector3.new(2, 2, 1)
	root.Anchored = true
	root.Transparency = 1
	root.CFrame = CFrame.new(pos)
	root.Parent = model
	-- a blocky body so a render shows where the player is. it follows the root (syncBody), nothing else about it is simulated
	local humanoid = Instance.new("Humanoid")
	humanoid.Parent = model
	local animator = Instance.new("Animator")
	animator.Parent = humanoid
	model.PrimaryPart = root
	rec.bodyOffsets = {}
	local function limb(name, size, offset, color)
		local part = Instance.new("Part")
		part.Name = name
		part.Size = size
		part.Anchored = true
		part.CanCollide = false
		part.Color = color
		part.CFrame = CFrame.new(pos) * offset
		part.Parent = model
		rec.bodyOffsets[part] = offset
		return part
	end
	local skin, shirt, trousers = Color3.fromRGB(245, 205, 48), Color3.fromRGB(13, 105, 172), Color3.fromRGB(40, 127, 71)
	limb("Head", Vector3.new(2, 1, 1), CFrame.new(0, 1.5, 0), skin)
	limb("Torso", Vector3.new(2, 2, 1), CFrame.new(0, 0, 0), shirt)
	limb("Left Arm", Vector3.new(1, 2, 1), CFrame.new(-1.5, 0, 0), skin)
	limb("Right Arm", Vector3.new(1, 2, 1), CFrame.new(1.5, 0, 0), skin)
	limb("Left Leg", Vector3.new(1, 2, 1), CFrame.new(-0.5, -2, 0), trousers)
	limb("Right Leg", Vector3.new(1, 2, 1), CFrame.new(0.5, -2, 0), trousers)
	local humanoid = Instance.new("Humanoid")
	humanoid.Parent = model
	model.PrimaryPart = root
	model.Parent = self.workspaceRaw
	rec.char, rec.root = model, root
end

-- the body follows the root part, however the root was moved (a script, walkTo, teleport)
function Host:syncBody(rec)
	local at = rec.root.CFrame
	for part, offset in rec.bodyOffsets do
		part.CFrame = at * offset
	end
end

function Host:syncBodies()
	for _, rec in self.records do
		if rec.root and rec.bodyOffsets then
			self:syncBody(rec)
		end
	end
end

-- teleport(player, Vector3 | CFrame, lookAt): put the player there. a CFrame sets the facing too; a position with a lookAt point faces it;
-- a position alone keeps the way the player was facing.
function Host:teleport(view, target, lookAt)
	local rec = self.recordOf[view]
	assert(rec, "teleport needs a player returned by addPlayer")
	local cf
	if typeof(target) == "CFrame" then
		cf = target
	elseif lookAt then
		cf = CFrameFixed.lookAt(target, Vector3.new(lookAt.X, target.Y, lookAt.Z))
	else
		cf = CFrame.new(target) * rec.root.CFrame.Rotation
	end
	rec.root.CFrame = cf
	self.movers[rec] = nil
	self:syncBody(rec)
end

function Host:addPlayer(name, pos)
	local rec = { name = name, userId = self.nextUserId, attrs = {}, attrSignals = {}, sigs = {} }
	self.nextUserId += 1
	local gui = Instance.new("Folder")
	gui.Name = "PlayerGui"
	rec.gui = gui
	table.insert(self.records, rec)
	self:makeCharacter(rec, pos or Vector3.new(0, 3, 0))

	local clientScripts = {}
	for _, child in self.dm:GetService("StarterGui"):GetChildren() do
		local copy = child:Clone()
		copy.Parent = gui
		if copy.ClassName == "LocalScript" then
			table.insert(clientScripts, copy)
		end
		for _, d in copy:GetDescendants() do
			if d.ClassName == "LocalScript" then
				table.insert(clientScripts, d)
			end
		end
	end
	for _, d in self.dm:GetService("StarterPlayer"):GetDescendants() do
		if d.ClassName == "LocalScript" then
			table.insert(clientScripts, d)
		end
	end

	self:fireRealms("PlayerAdded", function(realm)
		return self:viewOf(rec, realm)
	end)
	for _, s in clientScripts do
		self:run(s, rec)
	end
	for _, entry in rec.sigs.CharacterAdded or {} do
		entry.signal:Fire(wrap(rec.char, entry.realm))
	end
	return self:viewOf(rec, SERVER)
end

function Host:movePlayerTo(view, pos, speed)
	local rec = self.recordOf[view]
	assert(rec, "walkTo needs a player returned by addPlayer")
	self.movers[rec] = { goal = pos, speed = speed or 16 }
end

-- running scripts --------------------------------------------------------------------------------------------------

function Host:envFor(scriptInst, realm)
	local env = {}
	for name, value in roblox do
		env[name] = value
	end
	for name, value in self.globals do
		env[name] = value
	end
	env.Instance = {
		new = function(className)
			return wrap(Instance.new(className), realm)
		end,
	}
	env.CFrame = CFrameFixed
	env.Random = RandomFixed
	env.game = self:gameFor(realm)
	env.workspace = self:service(realm, "Workspace")
	env.task = self.task
	env.script = wrap(scriptInst, realm)
	env.tick = function() return os.clock() end
	env.time = function() return self.now end
	env.elapsedTime = function() return self.now end
	env.UserSettings = function() return { IsUserFeatureEnabled = function() return false end } end
	env.warn = function(...)
		print("[warn]", ...)
	end
	env.require = function(m)
		return self:require(m, realm)
	end
	return env
end

function Host:require(m, realm)
	m = unwrap(m)
	if typeof(m) ~= "Instance" or m.ClassName ~= "ModuleScript" then
		error("require expects a ModuleScript", 3)
	end
	local key = realm == nil and "none" or realm
	local cache = self.modules[key]
	if not cache then
		cache = {}
		self.modules[key] = cache
	end
	if cache[m] == nil then
		local chunk = luau.load(m.Source, { debugName = m.Name, environment = self:envFor(m, realm) })
		cache[m] = { value = chunk() }
	end
	return cache[m].value
end

function Host:run(inst, realm)
	local ok, chunk = pcall(luau.load, inst.Source, { debugName = inst.Name, environment = self:envFor(inst, realm) })
	if not ok then
		self:report(chunk)
		return
	end
	self:spawn(chunk)
end

function Host:boot()
	for _, root in { self.dm:GetService("ServerScriptService"), self.workspaceRaw } do
		for _, d in root:GetDescendants() do
			if d.ClassName == "Script" then
				self:run(d, SERVER)
			end
		end
	end
end

-- time ---------------------------------------------------------------------------------------------------------------

function Host:step(dt)
	self.now += dt
	for rec, m in self.movers do
		local cur = rec.root.CFrame.Position
		local delta = m.goal - cur
		local len = m.speed * dt
		local flat = Vector3.new(delta.X, 0, delta.Z)
		local arrived = delta.Magnitude <= len
		local nextPos = arrived and m.goal or cur + delta.Unit * len
		if flat.Magnitude > 0.05 then -- face the way the player walks
			rec.root.CFrame = CFrameFixed.lookAt(nextPos, nextPos + flat)
		else
			rec.root.CFrame = CFrame.new(nextPos) * rec.root.CFrame.Rotation
		end
		if arrived then
			self.movers[rec] = nil
		end
	end
	self:syncBodies()
	for _, handler in self.stepHandlers do
		handler(dt)
	end
	self.stepped:Fire(self.now, dt)
	self.preSimulation:Fire(dt)
	self.heartbeat:Fire(dt)
	self.postSimulation:Fire(dt)
	self.renderStepped:Fire(dt)
	local due = {}
	for i = #self.sleepers, 1, -1 do
		if self.sleepers[i].t <= self.now then
			table.insert(due, 1, table.remove(self.sleepers, i))
		end
	end
	for _, s in due do
		self:resumeThread(s.co, dt)
	end
end

function Host:advance(seconds, dt)
	dt = dt or 1 / 60
	for _ = 1, math.floor(seconds / dt + 0.5) do
		self:step(dt)
	end
end

return Host
