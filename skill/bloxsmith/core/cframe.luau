--!nocheck
-- roblox's CFrame, with lookAt corrected. Lune's CFrame.lookAt (and CFrame.new(position, lookAt)) face the opposite way
-- to Roblox's: looking toward -Z gives a LookVector of +Z. scripts see this corrected table instead.
local roblox = require("@lune/roblox")
local V3, Native = roblox.Vector3, roblox.CFrame

local function lookAt(at, target, up)
	local look = (target - at).Unit
	local right = look:Cross(up or V3.yAxis)
	if right.Magnitude < 1e-6 then
		right = look:Cross(V3.zAxis)
	end
	right = right.Unit
	return Native.fromMatrix(at, right, right:Cross(look).Unit, -look)
end

return setmetatable({
	identity = Native.new(),
	lookAt = lookAt,
	new = function(...)
		local count = select("#", ...)
		local a, b = ...
		if count == 2 and typeof(a) == "Vector3" and typeof(b) == "Vector3" then
			return lookAt(a, b)
		end
		return Native.new(...)
	end,
}, { __index = Native })
