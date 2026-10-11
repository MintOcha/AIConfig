--!nocheck
-- Roblox's Random (Lune has none). deterministic: Random.new() without a seed uses seed 0, so builds are repeatable.
local Random = {}
Random.__index = Random

local function step(self)
	local x = self.state
	x = bit32.bxor(x, bit32.lshift(x, 13))
	x = bit32.bxor(x, bit32.rshift(x, 17))
	x = bit32.bxor(x, bit32.lshift(x, 5))
	self.state = x
	return x / 4294967296
end

function Random.new(seed)
	local state = bit32.bxor(math.floor(seed or 0), 0x9E3779B9)
	if state == 0 then
		state = 1
	end
	local self = setmetatable({ state = state }, Random)
	for _ = 1, 4 do
		step(self)
	end
	return self
end

function Random:NextNumber(min, max)
	local r = step(self)
	if min == nil then
		return r
	end
	return min + (max - min) * r
end

function Random:NextInteger(min, max)
	return min + math.floor(step(self) * (max - min + 1))
end

function Random:Clone()
	return setmetatable({ state = self.state }, Random)
end

return Random
