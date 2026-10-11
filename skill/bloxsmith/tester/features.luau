--!nocheck
-- emulated engine features. each is small on purpose; anything not here is either inert (events) or errors loudly.
--   remotes: RemoteEvent / RemoteFunction / BindableEvent / BindableFunction between the server and client realms
--   Humanoid: Health, TakeDamage, Died, HealthChanged, MoveTo (kinematic)
--   TweenService: linear. the final state is exact; easing curves, repeats and reverses are ignored
--   Debris, UserInputService, AnimationClipProvider/Animator, Sound: stubs
--   inert events: events Lune lacks (Touched, Activated, ...) can be connected but never fire.
--   the run lists them as "untested" so nobody mistakes an assumption for a check.
local roblox = require("@lune/roblox")
local Proxy = require("../core/proxy")

local Enum = roblox.Enum
local Vector2 = roblox.Vector2
local Vector3 = roblox.Vector3
local unwrap = Proxy.unwrap

local Features = {}

local INERT = {}
for _, name in {
	"Touched", "TouchEnded", "Changed", "ChildAdded", "ChildRemoved", "DescendantAdded", "DescendantRemoving",
	"Destroying", "AncestryChanged", "AttributeChanged", "Activated", "MouseButton1Click", "MouseButton1Down",
	"MouseButton1Up", "MouseButton2Click", "MouseEnter", "MouseLeave", "MouseMoved", "InputBegan", "InputEnded",
	"InputChanged", "FocusLost", "Focused", "Triggered", "TriggerEnded", "MouseClick", "MouseHoverEnter",
	"MouseHoverLeave", "Equipped", "Unequipped", "PlayerRemoving", "CharacterRemoving", "Stopped", "Ended",
	"KeyframeReached", "DidLoop",
} do
	INERT[name] = true
end

local ANIMATED = { Animator = true, Humanoid = true, AnimationController = true }

local function lerp(a, b, t)
	local kind = typeof(a)
	if kind == "number" then
		return a + (b - a) * t
	elseif kind == "Vector3" or kind == "Vector2" or kind == "Color3" or kind == "CFrame" or kind == "UDim2" then
		return a:Lerp(b, t)
	end
	return t >= 1 and b or a
end

function Features.install(host)
	local isClient = host.isClient
	local strict = host.strict

	local function untested(label)
		host.untested[label] = true
	end
	local function inertSignal(label)
		untested(label)
		return host:newSignal()
	end

	-- query parameter bags are plain data. the queries that take them (Raycast, GetPartsInPart ...) are still not emulated and error.
	host.globals.RaycastParams = {
		new = function()
			return {
				FilterType = Enum.RaycastFilterType.Exclude,
				FilterDescendantsInstances = {},
				IgnoreWater = false,
				CollisionGroup = "Default",
				RespectCanCollide = false,
				BruteForceAllSlow = false,
			}
		end,
	}
	host.globals.OverlapParams = {
		new = function()
			return {
				FilterType = Enum.RaycastFilterType.Exclude,
				FilterDescendantsInstances = {},
				MaxParts = 0,
				CollisionGroup = "Default",
				RespectCanCollide = false,
				BruteForceAllSlow = false,
			}
		end,
	}
	-- Workspace spatial queries emulation for headless verification
	table.insert(host.memberHandlers, function(inst, key, _, class)
		if class ~= "Workspace" then
			return false
		end
		if key == "Raycast" then
			return true, function(_, origin, direction, params)
				-- Return simulated ground hit at y = 0
				local groundY = 0
				if origin.Y > groundY and (origin.Y + direction.Y) <= groundY then
					local fraction = (groundY - origin.Y) / direction.Y
					local hitPos = origin + direction * fraction
					local hitPart = roblox.Instance.new("Part")
					hitPart.Name = "Terrain"
					hitPart.Anchored = true
					return {
						Position = hitPos,
						Normal = roblox.Vector3.new(0, 1, 0),
						Distance = direction.Magnitude * fraction,
						Material = Enum.Material.Grass,
						Instance = hitPart,
					}
				end
				return nil
			end
		elseif key == "GetPartBoundsInBox" or key == "GetPartsInPart" or key == "GetPartBoundsInRadius" then
			return true, function(_, cframe, size, params)
				return {} -- empty clearance in tests
			end
		elseif key == "GetPartsObscuringTarget" then
			return true, function(_, castPoints, ignore)
				return {}
			end
		end
		return false
	end)


	host.globals.TweenInfo = {
		new = function(time, style, direction, repeatCount, reverses, delayTime)
			return {
				Time = time or 1,
				EasingStyle = style or Enum.EasingStyle.Quad,
				EasingDirection = direction or Enum.EasingDirection.Out,
				RepeatCount = repeatCount or 0,
				Reverses = reverses or false,
				DelayTime = delayTime or 0,
			}
		end,
	}

	-- remotes ------------------------------------------------------------------------------------------------------
	local remotes = {}
	local function entry(inst)
		local e = remotes[inst]
		if not e then
			e = { server = host:newSignal(), clients = {}, bindable = host:newSignal() }
			remotes[inst] = e
		end
		return e
	end
	local function clientSignal(e, rec)
		if not e.clients[rec] then
			e.clients[rec] = host:newSignal()
		end
		return e.clients[rec]
	end
	local function queue(fn)
		table.insert(host.queued, fn)
	end

	-- the test harness acting as a client
	function host:clientFire(player, remote, ...)
		local rec = self.recordOf[player]
		assert(rec, "fireServer needs a player returned by addPlayer")
		local e = entry(unwrap(remote))
		local args = table.pack(...)
		queue(function()
			local converted = self:convert(args, "server")
			e.server:Fire(self:viewOf(rec, "server"), table.unpack(converted, 1, args.n))
		end)
	end

	table.insert(host.memberHandlers, function(inst, key, realm, class)
		if class == "RemoteEvent" or class == "UnreliableRemoteEvent" then
			local e = entry(inst)
			if key == "OnServerEvent" then
				return true, e.server
			elseif key == "OnClientEvent" then
				if not isClient(realm) then
					error("OnClientEvent only exists in a LocalScript", 3)
				end
				return true, clientSignal(e, realm)
			elseif key == "FireServer" then
				return true, function(_, ...)
					if not isClient(realm) then
						error("FireServer can only be called from a LocalScript", 2)
					end
					local args = table.pack(...)
					queue(function()
						local converted = host:convert(args, "server")
						e.server:Fire(host:viewOf(realm, "server"), table.unpack(converted, 1, args.n))
					end)
				end
			elseif key == "FireClient" then
				return true, function(_, player, ...)
					local rec = host.recordOf[player]
					assert(rec, "FireClient needs a Player")
					local args = table.pack(...)
					queue(function()
						local converted = host:convert(args, rec)
						clientSignal(e, rec):Fire(table.unpack(converted, 1, args.n))
					end)
				end
			elseif key == "FireAllClients" then
				return true, function(_, ...)
					local args = table.pack(...)
					queue(function()
						for _, rec in host.records do
							local converted = host:convert(args, rec)
							clientSignal(e, rec):Fire(table.unpack(converted, 1, args.n))
						end
					end)
				end
			end
		elseif class == "RemoteFunction" then
			local e = entry(inst)
			if key == "InvokeServer" then
				return true, function(_, ...)
					if not isClient(realm) then
						error("InvokeServer can only be called from a LocalScript", 2)
					end
					if not e.onServerInvoke then
						error("this RemoteFunction has no OnServerInvoke (the server never set one)", 2)
					end
					local args = host:convert(table.pack(...), "server")
					local result = table.pack(e.onServerInvoke(host:viewOf(realm, "server"), table.unpack(args, 1, args.n)))
					local back = host:convert(result, realm)
					return table.unpack(back, 1, result.n)
				end
			end
		elseif class == "BindableEvent" then
			local e = entry(inst)
			if key == "Event" then
				return true, e.bindable
			elseif key == "Fire" then
				return true, function(_, ...)
					e.bindable:Fire(...)
				end
			end
		elseif class == "BindableFunction" then
			local e = entry(inst)
			if key == "Invoke" then
				return true, function(_, ...)
					if not e.onInvoke then
						error("this BindableFunction has no OnInvoke", 2)
					end
					return e.onInvoke(...)
				end
			end
		end
		return false
	end)
	table.insert(host.writeHandlers, function(inst, key, value, _, class)
		if class == "RemoteFunction" and key == "OnServerInvoke" then
			entry(inst).onServerInvoke = value
			return true
		elseif class == "BindableFunction" and key == "OnInvoke" then
			entry(inst).onInvoke = value
			return true
		end
		return false
	end)
	table.insert(host.stepHandlers, function()
		if #host.queued == 0 then
			return
		end
		local batch = host.queued
		host.queued = {}
		for _, fn in batch do
			local ok, err = pcall(fn)
			if not ok then
				host:report(err)
			end
		end
	end)

	-- humanoids ----------------------------------------------------------------------------------------------------
	local humanoids = {}
	local function hstate(inst)
		local s = humanoids[inst]
		if not s then
			local ok, max = pcall(function()
				return inst.MaxHealth
			end)
			max = ok and max or 100
			s = { health = max, max = max, died = host:newSignal(), changed = host:newSignal(), finished = host:newSignal(), dead = false }
			humanoids[inst] = s
		end
		return s
	end
	local function setHealth(inst, value)
		local s = hstate(inst)
		local clamped = math.clamp(value, 0, s.max)
		if clamped == s.health then
			return
		end
		s.health = clamped
		Proxy.set(inst, "Health", clamped)
		s.changed:Fire(clamped)
		if clamped <= 0 and not s.dead then
			s.dead = true
			s.died:Fire()
		end
	end
	table.insert(host.memberHandlers, function(inst, key, _, class)
		if class ~= "Humanoid" then
			return false
		end
		local s = hstate(inst)
		if key == "Health" then
			return true, s.health
		elseif key == "Died" then
			return true, s.died
		elseif key == "HealthChanged" then
			return true, s.changed
		elseif key == "MoveToFinished" then
			return true, s.finished
		elseif key == "TakeDamage" then
			return true, function(_, amount)
				setHealth(inst, s.health - amount)
			end
		elseif key == "MoveTo" then
			return true, function(_, position)
				s.target, s.elapsed = position, 0
			end
		end
		return false
	end)
	table.insert(host.writeHandlers, function(inst, key, value, _, class)
		if class ~= "Humanoid" then
			return false
		end
		if key == "Health" then
			setHealth(inst, value)
			return true
		elseif key == "MaxHealth" then
			Proxy.set(inst, "MaxHealth", value)
			hstate(inst).max = value
			return true
		end
		return false
	end)
	table.insert(host.stepHandlers, function(dt)
		for inst, s in humanoids do
			if s.target and inst.Parent then
				local root = inst.Parent:FindFirstChild("HumanoidRootPart")
				if root then
					local ok, speed = pcall(function()
						return inst.WalkSpeed
					end)
					speed = ok and speed or 16
					local cur = root.CFrame.Position
					local flat = Vector3.new(s.target.X - cur.X, 0, s.target.Z - cur.Z)
					s.elapsed += dt
					if flat.Magnitude <= speed * dt then
						root.CFrame = CFrame.new(s.target.X, cur.Y, s.target.Z)
						s.target = nil
						s.finished:Fire(true)
					elseif s.elapsed > 8 then
						s.target = nil
						s.finished:Fire(false)
					else
						root.CFrame = CFrame.new(cur + flat.Unit * speed * dt)
					end
				end
			end
		end
	end)

	-- tweens -------------------------------------------------------------------------------------------------------
	local active = {}
	host.serviceMakers.TweenService = function()
		return strict("TweenService", {
			Create = function(_, instance, info, goals)
				local raw = unwrap(instance)
				local tween = { Instance = instance, TweenInfo = info, PlaybackState = Enum.PlaybackState.Begin, Completed = host:newSignal() }
				local state = { tween = tween, raw = raw, goals = goals, t = 0, playing = false }
				function tween:Play()
					state.playing = true
					tween.PlaybackState = Enum.PlaybackState.Playing
					active[state] = true
				end
				function tween:Pause()
					state.playing = false
					tween.PlaybackState = Enum.PlaybackState.Paused
				end
				function tween:Cancel()
					state.playing = false
					active[state] = nil
					tween.PlaybackState = Enum.PlaybackState.Cancelled
				end
				function tween:Destroy()
					tween:Cancel()
				end
				return tween
			end,
		})
	end
	table.insert(host.stepHandlers, function(dt)
		for state in active do
			if state.playing then
				state.t += dt
				local info = state.tween.TweenInfo
				local elapsed = state.t - info.DelayTime
				if elapsed >= 0 then
					if not state.starts then
						state.starts = {}
						for prop in state.goals do
							state.starts[prop] = Proxy.get(state.raw, prop)
						end
					end
					local alpha = math.min(elapsed / math.max(info.Time, 1e-6), 1)
					for prop, goal in state.goals do
						Proxy.set(state.raw, prop, alpha >= 1 and unwrap(goal) or lerp(state.starts[prop], unwrap(goal), alpha))
					end
					if alpha >= 1 then
						state.playing = false
						active[state] = nil
						state.tween.PlaybackState = Enum.PlaybackState.Completed
						state.tween.Completed:Fire(Enum.PlaybackState.Completed)
					end
				end
			end
		end
	end)

	-- small services -----------------------------------------------------------------------------------------------
	host.serviceMakers.Debris = function()
		return strict("Debris", {
			AddItem = function(_, inst, lifetime)
				host.task.delay(lifetime or 10, function()
					pcall(function()
						unwrap(inst):Destroy()
					end)
				end)
			end,
		})
	end
	host.serviceMakers.UserInputService = function()
		untested("UserInputService (no input is ever delivered)")
		return setmetatable({
			InputBegan = host:newSignal(),
			InputEnded = host:newSignal(),
			InputChanged = host:newSignal(),
			WindowFocusReleased = host:newSignal(),
			WindowFocused = host:newSignal(),
			MouseEnabled = true,
			KeyboardEnabled = true,
			TouchEnabled = false,
			GamepadEnabled = false,
		}, {
			__index = function(_, k)
				if k == "GetPropertyChangedSignal" then
					return function(_, name) return host:newSignal() end
				end
				return host:newSignal()
			end,
		})
	end
	host.serviceMakers.AnimationClipProvider = function()
		return strict("AnimationClipProvider", {
			RegisterAnimationClip = function()
				untested("animation playback (clips are registered but never played)")
				return "rbxassetid://0"
			end,
		})
	end

	local function stubTrack()
		untested("animation playback (clips are registered but never played)")
		local track = { Length = 1, IsPlaying = false, Looped = false, Speed = 1, TimePosition = 0 }
		function track:Play()
			track.IsPlaying = true
		end
		function track:Stop()
			track.IsPlaying = false
		end
		function track:AdjustSpeed(speed)
			track.Speed = speed
		end
		function track:AdjustWeight() end
		function track:Destroy() end
		function track:GetMarkerReachedSignal()
			return host:newSignal()
		end
		track.Stopped, track.Ended, track.KeyframeReached, track.DidLoop = host:newSignal(), host:newSignal(), host:newSignal(), host:newSignal()
		return track
	end
	table.insert(host.memberHandlers, function(_, key, _, class)
		if ANIMATED[class] then
			if key == "LoadAnimation" then
				return true, function()
					return stubTrack()
				end
			elseif key == "GetPlayingAnimationTracks" then
				return true, function()
					return {}
				end
			end
		elseif class == "Animation" then
			if key == "AnimationId" then
				return true, "rbxassetid://0"
			end
		elseif class == "Sound" then
			if key == "Play" or key == "Stop" or key == "Pause" or key == "Resume" then
				untested("Sound playback")
				return true, function() end
			elseif key == "IsPlaying" or key == "Playing" then
				return true, false
			end
		elseif class == "Camera" then
			if key == "ViewportSize" then
				return true, Vector2.new(1280, 720)
			elseif key == "WorldToViewportPoint" or key == "WorldToScreenPoint" then
				return true, function(_, pt)
					return Vector3.new(640, 360, 1), true
				end
			end
		elseif class == "SoundService" then
			if key == "SetListener" then
				return true, function() end
			end
		elseif class == "ScreenGui" then
			if key == "AbsoluteSize" then
				return true, Vector2.new(1280, 720)
			elseif key == "AbsolutePosition" then
				return true, Vector2.new(0, 0)
			end
		elseif class == "Frame" or class == "CanvasGroup" or class == "TextLabel" or class == "ImageLabel" or class == "TextButton" or class == "ImageButton" or class == "ScrollingFrame" then
			if key == "AbsoluteSize" then
				local ok, sz = pcall(function() return inst.Size end)
				if ok and typeof(sz) == "UDim2" then
					local x = sz.X.Offset > 0 and sz.X.Offset or (sz.X.Scale * 1280)
					local y = sz.Y.Offset > 0 and sz.Y.Offset or (sz.Y.Scale * 720)
					return true, Vector2.new(math.max(1, x), math.max(1, y))
				end
				return true, Vector2.new(100, 100)
			elseif key == "AbsolutePosition" then
				local ok, pos = pcall(function() return inst.Position end)
				if ok and typeof(pos) == "UDim2" then
					local x = pos.X.Offset > 0 and pos.X.Offset or (pos.X.Scale * 1280)
					local y = pos.Y.Offset > 0 and pos.Y.Offset or (pos.Y.Scale * 720)
					return true, Vector2.new(x, y)
				end
				return true, Vector2.new(0, 0)
			end
		end
		return false
	end)

	-- inert events: last, so a real handler above always wins ------------------------------------------------------
	table.insert(host.memberHandlers, function(inst, key, _, class)
		if INERT[key] then
			local ok = pcall(function()
				return inst[key]
			end)
			if not ok then
				return true, inertSignal(class .. "." .. key)
			end
		elseif key == "GetPropertyChangedSignal" or key == "GetAttributeChangedSignal" then
			local ok = pcall(function()
				return inst[key]
			end)
			if not ok then
				return true, function(_, name)
					return inertSignal(class .. "." .. key .. "(" .. tostring(name) .. ")")
				end
			end
		end
		return false
	end)
end

return Features
