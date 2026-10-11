--!nocheck
-- lune run core/build_main.luau <project-dir> [path]
--   no path: the whole game -> dist/game.rbxl
--   path (a file or folder under world/ or src/): just that piece -> dist/<path>.rbxm
local fs = require("@lune/fs")
local process = require("@lune/process")
local roblox = require("@lune/roblox")
local Build = require("./build")
local Proxy = require("./proxy")
local NameCheck = require("./namecheck")


local root, rel = process.args[1], process.args[2]
assert(root, "usage: build_main.luau <project-dir> [path]")

local t0 = os.clock()
if not fs.isDir(root .. "/dist") then
	fs.writeDir(root .. "/dist")
end

if rel and rel ~= "" then
	local ok, instances, stats = pcall(Build.unit, root, rel)
	if not ok then
		print("build failed: " .. tostring(instances))
		process.exit(1)
	end
	local name = rel:gsub("\\", "/"):gsub("/+$", ""):gsub("%.luau$", ""):gsub("/", "_")
	Proxy.finalizeText(instances)
	local bytes = roblox.serializeModel(instances)
	fs.writeFile(`{root}/dist/{name}.rbxm`, bytes)
	for _, w in NameCheck.run(instances) do
		print("warning: " .. w)
	end
	print(`built {rel} -> dist/{name}.rbxm ({stats.instances} instances, {stats.scripts} scripts, {math.floor(#bytes / 1024)} KB, {math.floor((os.clock() - t0) * 1000)} ms)`)
else
	local ok, dm, stats = pcall(Build.project, root)
	if not ok then
		print("build failed: " .. tostring(dm))
		process.exit(1)
	end
	Proxy.finalizeText({ dm })
	local bytes = roblox.serializePlace(dm)
	fs.writeFile(root .. "/dist/game.rbxl", bytes)
	print(`built {stats.instances} instances ({stats.scripts} scripts) -> dist/game.rbxl, {math.floor(#bytes / 1024)} KB, {math.floor((os.clock() - t0) * 1000)} ms`)
	for _, rel in stats.unused do
		print(`warning: assets/{rel}.module.luau is never required`)
	end
	for _, w in stats.warnings do
		print("warning: " .. w)
	end
	for _, w in NameCheck.run({ dm }) do
		print("warning: " .. w)
	end
end
