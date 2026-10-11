--!nocheck
-- snapshot frames: turns the live world of a running test into a renderer scene, inside the test's own Lune process (no saved worlds, no extra processes).
--   Frames.make(dm, { gui = Folder of the player's screens, target = Instance in Workspace, cframe = {12 numbers}, at = {x, y, z} })
--   returns { scene = <table to write as json>, views = { { name, cframe, fov } }, topbar = bool }
-- what gets drawn is chosen here, so a test only says what it wants to see:
--   nothing    the player's view (camera behind their character) with their enabled screens over the world: one frame
--   cframe     the same, from that camera
--   target/at  that model (or that spot) in the world with the ground around it, from three angles, no screens
local roblox = require("@lune/roblox")
local Scene = require("./scene")
local V3 = roblox.Vector3

local FOV = 70 -- the play camera's field of view
local AROUND = 30 -- studs of the surrounding world kept in frame

local Frames = {}

local function header(dm, state, width, height, label)
	return {
		version = 3,
		bubbles = {},
		time = 0,
		screen = { width, height },
		safeArea = { width, height - 58 },
		topbarInset = { 0, 0, 0, 0 },
		preferredInput = "KeyboardAndMouse",
		terrain = {},
		lighting = Scene.node(dm:GetService("Lighting"), state),
		notes = { target = label },
	}
end

local function worldNodes(dm, state)
	local children = {}
	for _, c in dm:GetService("Workspace"):GetChildren() do
		if c.ClassName ~= "Terrain" and c.ClassName ~= "Camera" then
			table.insert(children, Scene.node(c, state))
		end
	end
	return children
end

-- where a real player's camera would be: behind their character like the default follow camera, looking at the head
local function playerView(dm)
	for _, c in dm:GetService("Workspace"):GetChildren() do
		local root = c.ClassName == "Model" and c:FindFirstChild("HumanoidRootPart")
		if root then
			local look = root.CFrame.LookVector * V3.new(1, 0, 1)
			look = look.Magnitude > 0.01 and look.Unit or V3.new(0, 0, -1)
			local head = root.CFrame.Position + V3.new(0, 2, 0)
			return { Scene.lookAt(head - look * 14 + V3.new(0, 5, 0), head):GetComponents() }
		end
	end
end

function Frames.make(dm, request)
	local state = Scene.newState()
	local ws = dm:GetService("Workspace")

	if request.target == nil and request.at == nil then
		-- the player's screen: their enabled screens drawn over the world from their camera (or the one given)
		local screens = {}
		for _, screen in request.gui and request.gui:GetChildren() or {} do
			if screen.ClassName == "ScreenGui" and screen.Enabled then
				table.insert(screens, Scene.node(screen, state))
			end
		end
		local scene = header(dm, state, 1280, 720, "player")
		local children = worldNodes(dm, state)
		state.nextId += 1
		scene.workspace = { id = state.nextId, class = "Workspace", name = "Workspace", props = {}, children = children }
		scene.playerGui = { id = state.nextId + 1, class = "PlayerGui", name = "PlayerGui", props = {}, children = screens }
		local camera = request.cframe or playerView(dm) or { 0, 60, 80, 1, 0, 0, 0, 0.9, -0.4, 0, 0.4, 0.9 }
		scene.camera = { cframe = camera, fov = FOV }
		return { scene = scene, views = { { name = "", cframe = camera, fov = FOV } }, topbar = true }
	end

	-- a model or a spot in the world, from a few angles
	local lo, hi
	if request.at then
		local a = request.at
		lo, hi = V3.new(a[1], a[2] - 2, a[3]), V3.new(a[1], a[2] + 2, a[3])
	else
		lo, hi = Scene.bounds(request.target)
	end
	local scene = header(dm, state, 1280, 720, "model")
	local children = worldNodes(dm, state)
	state.nextId += 1
	scene.workspace = { id = state.nextId, class = "Workspace", name = "Workspace", props = {}, children = children }
	local center = (lo + hi) / 2
	local size = math.max(hi.X - lo.X + AROUND * 2, hi.Y - lo.Y, hi.Z - lo.Z + AROUND * 2, 4)
	local distance = (size / 2) / math.tan(math.rad(FOV / 2)) * 1.25 + size * 0.35
	local views = {}
	for _, angle in { { "+x+z", 0.7, 0.5, 0.7 }, { "-x-z", -0.7, 0.5, -0.7 }, { "top", 0.001, 1, 0.12 } } do
		local direction = V3.new(angle[2], angle[3], angle[4]).Unit
		table.insert(views, { name = angle[1], cframe = { Scene.lookAt(center + direction * distance, center):GetComponents() }, fov = FOV })
	end
	scene.camera = { cframe = views[1].cframe, fov = FOV }
	return { scene = scene, views = views, topbar = false }
end

return Frames
