"""Scoped runtime profiling without editing the game.

`profile` wraps the exported functions of chosen ModuleScripts for a few seconds, counts
calls and times each one (inclusive = with callees, exclusive = own time), then puts every
original function back. It runs in the live Play client or server through `execute`.

Limits worth knowing: only functions stored on the module table (or its class table) are
visible; a module's private `local function`s are folded into whoever calls them.
"""
import json

LUAU = r'''
local RunService = game:GetService("RunService")
local targets, seconds = __TARGETS__, __SECONDS__
local only = __ONLY__
local clock = os.clock
local stats, originals, stack = {}, {}, {}

local function resolve(path)
	-- The running client scripts are copies under PlayerScripts; the StarterPlayer originals would
	-- load a second, unused module instance, so point at the copies the game really runs.
	if RunService:IsClient() then
		path = string.gsub(path, "^game%.StarterPlayer%.StarterPlayerScripts", "game.Players.LocalPlayer.PlayerScripts")
		path = string.gsub(path, "^game%.StarterGui", "game.Players.LocalPlayer.PlayerGui")
	end
	local node = game
	for part in string.gmatch(path, "[^.]+") do
		if part == "game" and node == game then
			-- the root
		elseif part == "LocalPlayer" and node == game:GetService("Players") then
			node = node.LocalPlayer
		else
			node = node:FindFirstChild(part)
		end
		if not node then return nil end
	end
	return node
end

local function wrap(label, owner, key, fn)
	local entry = {calls = 0, inclusive = 0, exclusive = 0, max = 0}
	stats[label] = entry
	originals[#originals + 1] = {owner, key, fn}
	owner[key] = function(...)
		local frame = {child = 0}
		stack[#stack + 1] = frame
		local start = clock()
		local results = table.pack(pcall(fn, ...))
		local elapsed = clock() - start
		stack[#stack] = nil
		entry.calls += 1
		entry.inclusive += elapsed
		entry.exclusive += elapsed - frame.child
		if elapsed > entry.max then entry.max = elapsed end
		if stack[#stack] then stack[#stack].child += elapsed end
		if not results[1] then error(results[2], 0) end
		return table.unpack(results, 2, results.n)
	end
end

local function instrument(module, name)
	local ok, exported = pcall(require, module)
	if not ok or type(exported) ~= "table" then return end
	for key, value in pairs(exported) do
		if type(value) == "function" and (#only == 0 or table.find(only, key)) then
			wrap(name .. "." .. tostring(key), exported, key, value)
		end
	end
end

local missing = {}
for _, path in ipairs(targets) do
	local node = resolve(path)
	if not node then
		missing[#missing + 1] = path
	elseif node:IsA("ModuleScript") then
		instrument(node, node.Name)
	else
		for _, child in ipairs(node:GetChildren()) do
			if child:IsA("ModuleScript") then instrument(child, child.Name) end
		end
	end
end

local frames, frameTime, worst = 0, 0, 0
local beat = RunService.Heartbeat:Connect(function(dt)
	frames += 1 frameTime += dt
	if dt > worst then worst = dt end
end)
task.wait(seconds)
beat:Disconnect()
for index = #originals, 1, -1 do
	local saved = originals[index]
	saved[1][saved[2]] = saved[3]
end

local rows = {}
for label, entry in pairs(stats) do
	if entry.calls > 0 then
		rows[#rows + 1] = {label = label, calls = entry.calls,
			inclusiveMs = entry.inclusive * 1000, exclusiveMs = entry.exclusive * 1000,
			perCallMs = entry.inclusive * 1000 / entry.calls, maxMs = entry.max * 1000}
	end
end
table.sort(rows, function(a, b) return a.exclusiveMs > b.exclusiveMs end)
return {seconds = seconds, frames = frames, fps = frames / math.max(frameTime, 1e-6), worstFrameMs = worst * 1000,
	missing = missing, rows = rows}
'''


def script(targets, seconds=5, only=None):
    """Luau source for one profiling run. Targets are dotted paths (game.X.Y) of a module or a folder of modules."""
    def table(values):
        return '{' + ','.join(json.dumps(v) for v in values) + '}'
    return LUAU.replace('__TARGETS__', table(targets)).replace('__SECONDS__', str(float(seconds))).replace('__ONLY__', table(only or []))


def listify(value):
    """Luau arrays come back through `execute` as {"1": a, "2": b}; turn those back into lists."""
    if isinstance(value, dict) and value and all(key.isdigit() for key in value):
        return [listify(value[key]) for key in sorted(value, key=int)]
    if isinstance(value, dict):
        return {key: listify(item) for key, item in value.items()}
    return value


SERVER_WRAPPER = r'''
-- Server scripts run in the game's own Lua VM, so the profiler must run as a real Script there:
-- a plugin-context require() would load private copies of the modules and see no calls.
local HttpService = game:GetService("HttpService")
local runner = Instance.new("Script")
runner.Name = "RoMCPProfile"
runner.Source = [==[__BODY__]==]
runner.Parent = game:GetService("ServerScriptService")
local deadline = os.clock() + __SECONDS__ + 15
repeat task.wait(0.1) until runner:GetAttribute("Result") or os.clock() > deadline
local result = runner:GetAttribute("Result")
runner:Destroy()
if not result then return {error = "the profiler script did not report back"} end
return HttpService:JSONDecode(result)
'''


def server_script(targets, seconds=5, only=None):
    """The same profiling run, delivered as a temporary server Script that reports through an attribute."""
    body = script(targets, seconds, only)
    index = body.rindex('return {seconds = seconds')
    body = body[:index] + 'local report = {seconds = seconds' + body[index + len('return {seconds = seconds'):]
    body += '\nscript:SetAttribute("Result", game:GetService("HttpService"):JSONEncode(report))\n'
    return SERVER_WRAPPER.replace('__SECONDS__', str(float(seconds))).replace('__BODY__', body)


def render(result, top=15):
    """A short human table; the raw result keeps every row."""
    lines = [f"{result['frames']} frames in {result['seconds']:g}s = {result['fps']:.1f} fps, worst frame {result['worstFrameMs']:.1f} ms"]
    for path in result.get('missing', []):
        lines.append(f'  not found: {path}')
    lines.append(f"{'function':42} {'calls':>7} {'self ms':>9} {'total ms':>9} {'per call':>9} {'max':>7}")
    for row in result['rows'][:top]:
        lines.append(f"{row['label'][:42]:42} {row['calls']:>7} {row['exclusiveMs']:>9.1f} {row['inclusiveMs']:>9.1f} {row['perCallMs']:>9.3f} {row['maxMs']:>7.2f}")
    if not result['rows']:
        lines.append('  no calls recorded (nothing ran, or the functions are private locals)')
    return '\n'.join(lines)
