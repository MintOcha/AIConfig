--!nocheck
-- compiles a project into one in-memory DataModel.
--   src/     mirrors the game. the path is the place: src/Workspace/Map/... -> Workspace.Map...
--            suffixes: .build.luau runs at build time (never ships), .server/.client/.module.luau ship as scripts
--   assets/  build-only input: Foo.module.luau files here are shared build code, required as require("@assets/Foo")
local roblox = require("@lune/roblox")
local fs = require("@lune/fs")
local luau = require("@lune/luau")
local Proxy = require("./proxy")
local Anim = require("./anim")
local CFrameFixed = require("./cframe")
local RandomFixed = require("./random")

local wrap, unwrap = Proxy.wrap, Proxy.unwrap
local Build = {}

local SUFFIX_CLASS = { server = "Script", client = "LocalScript", module = "ModuleScript" }

-- scripts that would sit where they never run (a warning, not an error)
local DEAD = {
	LocalScript = { ServerScriptService = true, ServerStorage = true, ReplicatedStorage = true, Lighting = true },
	Script = { ServerStorage = true, ReplicatedStorage = true, StarterPlayer = true, StarterGui = true, ReplicatedFirst = true, Lighting = true },
}
local SERVICE_HINT = "ServerScriptService, ReplicatedStorage, StarterPlayer, StarterGui, ReplicatedFirst, StarterPack, ServerStorage, Workspace, Lighting"

local function normalize(path)
	path = path:gsub("\\", "/")
	return (path:gsub("/+$", ""))
end

local function relName(state, path)
	return path:sub(#state.root + 2)
end

local function firstLine(err)
	return (tostring(err):match("[^\n]*"))
end

-- what a .build.luau sees: roblox luau with real Instance.new, game, workspace and a require for build modules
local function newBase(state)
	local dm = state.dm
	local base = {}
	base.print = function(...)
		if not state.quiet then
			print(...)
		end
	end
	for name, value in roblox do
		base[name] = value
	end
	base.Instance = {
		new = function(className)
			return wrap(roblox.Instance.new(className))
		end,
	}
	base.print = function(...)
		if not state.quiet then
			print(...)
		end
	end
	base.CFrame = CFrameFixed
	base.Random = RandomFixed
	local collection = {
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
			for _, d in dm:GetDescendants() do
				if d:HasTag(tag) then
					table.insert(out, wrap(d))
				end
			end
			return out
		end,
	}
	base.game = {
		GetService = function(_, name)
			if name == "CollectionService" then
				return collection
			end
			local ok, service = pcall(function()
				return dm:GetService(name)
			end)
			if not ok or not service then
				error(`game:GetService("{name}") is not available at build time`, 2)
			end
			return wrap(service)
		end,
	}
	base.workspace = base.game:GetService("Workspace")
	
	local function loadAssetsTree(dir)
		local folder = roblox.Instance.new("Folder")
		folder.Name = "assets"
		if not fs.isDir(dir) then
			return wrap(folder)
		end
		local function scan(curDir, curFolder)
			for _, entry in fs.readDir(curDir) do
				local full = curDir .. "/" .. entry
				if fs.isDir(full) then
					local sub = roblox.Instance.new("Folder")
					sub.Name = entry
					sub.Parent = curFolder
					scan(full, sub)
				elseif entry:match("%.rbxm$") then
					local ok, models = pcall(roblox.deserializeModel, fs.readFile(full))
					if ok and models then
						for _, m in models do
							m.Parent = curFolder
						end
					end
				end
			end
		end
		scan(dir, folder)
		return wrap(folder)
	end
	base.assets = loadAssetsTree(state.root .. "/assets")

	base.require = function(spec)
		if type(spec) ~= "string" or spec:sub(1, 8) ~= "@assets/" then
			error('a .build.luau can only require build modules: require("@assets/Name") (the file is assets/Name.module.luau or assets/Name.rbxm)', 2)
		end
		local name = spec:sub(9)
		local cached = state.modules[name]
		if cached then
			return cached.value
		end
		if state.loading[name] then
			error(`require cycle through @assets/{name}`, 2)
		end

		-- Check if it's an .rbxm file
		local rbxmFile = state.root .. "/assets/" .. name
		if not rbxmFile:match("%.rbxm$") then
			rbxmFile = rbxmFile .. ".rbxm"
		end
		if fs.isFile(rbxmFile) then
			local ok, models = pcall(roblox.deserializeModel, fs.readFile(rbxmFile))
			if not ok or not models or #models == 0 then
				error(`assets/{name}: failed to deserialize rbxm`, 0)
			end
			local val = wrap(models[1])
			state.modules[name] = { value = val }
			return val
		end

		local file = state.root .. "/assets/" .. name .. ".module.luau"
		if not fs.isFile(file) then
			error(`require "{spec}": no file assets/{name}.module.luau or assets/{name}.rbxm`, 2)
		end
		state.loading[name] = true
		local env = table.clone(base) -- luau.load only sees own fields, so each script gets a flat copy
		local ok, value = pcall(function()
			return luau.load(fs.readFile(file), { debugName = "assets/" .. name, environment = env })()
		end)
		state.loading[name] = nil
		if not ok then
			error("assets/" .. name .. ".module.luau: " .. firstLine(value), 0)
		end
		state.modules[name] = { value = value }
		return value
	end
	return base
end

local function newState(root, quiet)
	local state = { quiet = quiet, root = normalize(root), dm = roblox.Instance.new("DataModel"), modules = {}, loading = {}, service = nil }
	state.stats = { scripts = 0, instances = 0, unused = {}, warnings = {} }
	state.base = newBase(state)
	return state
end

local function newContainer(name)
	local class = "Folder"
	if name == "StarterPlayerScripts" or name == "StarterCharacterScripts" then
		class = name
	end
	local ok, made = pcall(roblox.Instance.new, class)
	local inst = ok and made or roblox.Instance.new("Folder")
	inst.Name = name
	return inst
end

-- runs a .build.luau once. script.Parent is the instance at the file's place in the tree.
local function runBuildScript(state, path, parent)
	local name = path:match("([^/]+)%.build%.luau$")
	local scriptTable = setmetatable({ Parent = wrap(parent), Name = name }, {
		__index = function(_, key)
			error(`script.{tostring(key)} is not available in a .build.luau (only script.Parent)`, 2)
		end,
	})
	local env = table.clone(state.base)
	env.script = scriptTable
	local ok, err = pcall(function()
		local chunk = luau.load(fs.readFile(path), { debugName = relName(state, path), environment = env })
		chunk()
	end)
	if not ok then
		error(relName(state, path) .. ": " .. firstLine(err), 0)
	end
end

local function mountScript(state, parent, path, name, class)
	local source = fs.readFile(path)
	if source:find("@assets/", 1, true) then
		error(`{relName(state, path)}: runtime scripts cannot use @assets (assets/ is build-only and does not ship). do it in a .build.luau instead`, 0)
	end
	local script = roblox.Instance.new(class)
	script.Name = name
	script.Source = source
	script.Parent = parent
	state.stats.scripts += 1
	local dead = DEAD[class]
	if dead and state.service and dead[state.service] then
		table.insert(state.stats.warnings, `{relName(state, path)} is a {class} in {state.service}, where it never runs`)
	end
	return script
end

local function processFile(state, parent, path, fileName)
	if fileName:match("%.anim$") then
		local base = fileName:gsub("%.anim$", "")
		Anim.compile(fs.readFile(path), relName(state, path), base).Parent = parent
		return
	end
	if fileName:match("%.rbxm$") then
		local ok, models = pcall(roblox.deserializeModel, fs.readFile(path))
		if not ok or not models then
			error(`{relName(state, path)}: failed to deserialize rbxm`, 0)
		end
		for _, m in models do
			m.Parent = parent
		end
		return
	end
	local base, kind = fileName:match("^(.+)%.(%a+)%.luau$")
	if kind == "build" then
		runBuildScript(state, path, parent)
	elseif kind and SUFFIX_CLASS[kind] then
		mountScript(state, parent, path, base, SUFFIX_CLASS[kind])
	else
		error(`{relName(state, path)}: files need a suffix: Foo.build.luau (runs at build time), Foo.server.luau (Script), Foo.client.luau (LocalScript), Foo.module.luau (ModuleScript), Foo.anim (animation) or Foo.rbxm (model)`, 0)
	end
end

local function sortedEntries(dirPath)
	local entries = fs.readDir(dirPath)
	table.sort(entries)
	return entries
end

-- files first (init handled by the caller), then folders
local processDir
local function processChildren(state, self, dirPath)
	local entries = sortedEntries(dirPath)
	for _, name in entries do
		local path = dirPath .. "/" .. name
		if fs.isFile(path) and (name:match("%.luau$") or name:match("%.anim$") or name:match("%.rbxm$")) and not name:match("^init%.") then
			processFile(state, self, path, name)
		end
	end
	for _, name in entries do
		local path = dirPath .. "/" .. name
		if fs.isDir(path) then
			processDir(state, self, path, name)
		end
	end
end

-- a folder under a parent becomes (or merges into) the instance of that name, or is whatever its init file makes
function processDir(state, parent, dirPath, dirName)
	local inits = {}
	for _, name in sortedEntries(dirPath) do
		if name:match("^init%.%a+%.luau$") or name == "init.rbxm" then
			table.insert(inits, name)
		end
	end
	if #inits > 1 then
		error(`{relName(state, dirPath)}: more than one init file ({table.concat(inits, ", ")})`, 0)
	end
	local self
	if #inits == 1 then
		if inits[1] == "init.rbxm" then
			local path = dirPath .. "/init.rbxm"
			local ok, models = pcall(roblox.deserializeModel, fs.readFile(path))
			if not ok or not models or #models ~= 1 then
				error(`{relName(state, path)} must contain exactly one root instance`, 0)
			end
			self = models[1]
			self.Name = dirName
			self.Parent = parent
		else
			local kind = inits[1]:match("^init%.(%a+)%.luau$")
			local path = dirPath .. "/" .. inits[1]
			if kind == "build" then
				local before = #parent:GetChildren()
				runBuildScript(state, path, parent)
				local children = parent:GetChildren()
				local made = #children - before
				if made ~= 1 then
					error(`{relName(state, path)}: an init.build.luau must create exactly one instance under script.Parent (it made {made})`, 0)
				end
				self = children[#children]
				self.Name = dirName
			elseif SUFFIX_CLASS[kind] then
				self = mountScript(state, parent, path, dirName, SUFFIX_CLASS[kind])
			else
				error(`{relName(state, path)}: init files are init.build.luau, init.server.luau, init.client.luau or init.module.luau`, 0)
			end
		end
	else
		self = parent:FindFirstChild(dirName)
		if not self then
			self = newContainer(dirName)
			self.Parent = parent
		end
	end
	processChildren(state, self, dirPath)
	return self
end

local function serviceFor(state, name)
	local ok, service = pcall(function()
		return state.dm:GetService(name)
	end)
	if not ok or not service then
		error(`src/{name} is not a Roblox service. top level folders under src/ are service names: {SERVICE_HINT}`, 0)
	end
	return service
end

local function processService(state, name, path)
	for _, entry in sortedEntries(path) do
		if entry:match("^init%.") then
			error(`{relName(state, path .. "/" .. entry)}: a service folder cannot have an init file (use a regular .build.luau)`, 0)
		end
	end
	state.service = name
	processChildren(state, serviceFor(state, name), path)
end

local function listModules(dir, prefix, out)
	for _, name in fs.readDir(dir) do
		local path = dir .. "/" .. name
		if fs.isDir(path) then
			listModules(path, prefix .. name .. "/", out)
		elseif name:match("%.module%.luau$") then
			table.insert(out, prefix .. (name:gsub("%.module%.luau$", "")))
		end
	end
	return out
end

-- the whole game: every folder under src/ in path order. stats.unused = assets modules nothing requires.
function Build.project(root, quiet)
	Proxy.verify = true
	local state = newState(root, quiet)
	local srcDir = state.root .. "/src"
	if not fs.isDir(srcDir) then
		error("this project has no src/ folder", 0)
	end
	for _, name in sortedEntries(srcDir) do
		local path = srcDir .. "/" .. name
		if fs.isDir(path) then
			processService(state, name, path)
		elseif name:match("%.luau$") then
			error(`src/{name}: files go inside a service folder, e.g. src/ServerScriptService/{name}`, 0)
		end
	end
	state.stats.instances = #state.dm:GetDescendants()
	local assetsDir = state.root .. "/assets"
	if fs.isDir(assetsDir) then
		for _, rel in listModules(assetsDir, "", {}) do
			if not state.modules[rel] then
				table.insert(state.stats.unused, rel)
			end
		end
	end
	return state.dm, state.stats, { name = state.root:match("([^/]+)$") }
end

-- any file outside src/ (assets/ modules, loose .build.luau, .rbxm, .anim): built into Workspace so it can be looked at alone.
-- an assets module that returns a function is called with no arguments; an Instance (or list of them) it returns is the result.
function Build.outsideSrc(state, rel, segs)
	local abs = state.root .. "/" .. rel
	if not fs.isFile(abs) then
		error(`no such file: {rel}`, 0)
	end
	local workspace = state.dm:GetService("Workspace")
	local file = segs[#segs]
	local result = {}
	local modName = segs[1] == "assets" and #segs >= 2 and rel:match("^assets/(.+)%.module%.luau$")
	if modName then
		local value = state.base.require("@assets/" .. modName)
		if type(value) == "function" then
			local ok, made = pcall(value)
			if not ok then
				error(`{rel}: calling the returned function failed: {firstLine(made)}`, 0)
			end
			value = made
		end
		value = Proxy.unwrap(value)
		if typeof(value) == "Instance" then
			value.Parent = workspace
			table.insert(result, value)
		elseif type(value) == "table" then
			for _, v in value do
				v = Proxy.unwrap(v)
				if typeof(v) == "Instance" then
					v.Parent = workspace
					table.insert(result, v)
				end
			end
		end
		if #result == 0 then
			error(`{rel} did not return an Instance, a list of Instances, or a function that makes one`, 0)
		end
	else
		local before = #workspace:GetChildren()
		processFile(state, workspace, abs, file)
		local children = workspace:GetChildren()
		for i = before + 1, #children do
			table.insert(result, children[i])
		end
		if #result == 0 then
			error(`{rel} did not create anything to build`, 0)
		end
	end
	for _, inst in result do
		state.stats.instances += 1 + #inst:GetDescendants()
	end
	return result, state.stats, state.dm
end

-- one piece on its own: a file or folder under src/. ancestors are plain folders (their init files do not run).
-- returns { instances }, stats
function Build.unit(root, rel, quiet)
	Proxy.verify = true
	local state = newState(root, quiet)
	rel = normalize(rel)
	local segs = string.split(rel, "/")
	if segs[1] ~= "src" or #segs < 2 then
		return Build.outsideSrc(state, rel, segs)
	end
	local abs = state.root .. "/" .. rel
	if not (fs.isFile(abs) or fs.isDir(abs)) then
		error(`no such file or folder: {rel}`, 0)
	end
	local service = serviceFor(state, segs[2])
	state.service = segs[2]
	local parent = service
	for i = 3, #segs - 1 do
		local folder = newContainer(segs[i])
		folder.Parent = parent
		parent = folder
	end
	local result
	if #segs == 2 then
		if not fs.isDir(abs) then
			error(`{rel}: files go inside a service folder`, 0)
		end
		processService(state, segs[2], abs)
		result = service:GetChildren()
	elseif fs.isDir(abs) then
		result = { processDir(state, parent, abs, segs[#segs]) }
	else
		local before = #parent:GetChildren()
		processFile(state, parent, abs, segs[#segs])
		local children = parent:GetChildren()
		result = {}
		for i = before + 1, #children do
			table.insert(result, children[i])
		end
		if #result == 0 then
			error(`{rel} did not create anything to build`, 0)
		end
	end
	for _, inst in result do
		state.stats.instances += 1 + #inst:GetDescendants()
	end
	return result, state.stats, state.dm
end

return Build
