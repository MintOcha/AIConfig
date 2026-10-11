--!nocheck
-- describe / it / expect for test files, on top of check(). one check per `it`: a failed expect raises, `it` catches it and records the failure
-- with its message. a test file may mix these with plain check() calls.
--   expect(v):toBe(x)  :toEqual(x) (deep)  :toBeCloseTo(x, digits = 2)  :toBeTruthy()  :toBeFalsy()  :toBeNil()
--                      :toBeGreaterThan(x)  :toBeLessThan(x)  :toThrow()      and the same after .not
local Spec = {}

local function show(v)
	if type(v) == "table" then
		return "{table}"
	end
	return tostring(v)
end

local function deepEqual(a, b)
	if a == b then
		return true
	end
	if type(a) ~= "table" or type(b) ~= "table" then
		return false
	end
	for k, v in a do
		if not deepEqual(v, b[k]) then
			return false
		end
	end
	for k in b do
		if a[k] == nil then
			return false
		end
	end
	return true
end

function Spec.install(env)
	local prefix = {}

	function env.describe(name, fn)
		table.insert(prefix, name)
		fn()
		table.remove(prefix)
	end

	function env.it(name, fn)
		local ok, err = pcall(fn)
		env.check(table.concat(prefix, " > ") .. (#prefix > 0 and " > " or "") .. name, ok, err)
	end

	function env.expect(actual)
		local function matchers(negate)
			local m = {}
			local function assertion(pass, what, expected)
				if pass == negate then
					error(`expected {show(actual)}{negate and " not" or ""} {what}{expected ~= nil and " " .. show(expected) or ""}`, 3)
				end
			end
			function m.toBe(_, e)
				assertion(actual == e, "to be", e)
			end
			function m.toEqual(_, e)
				assertion(deepEqual(actual, e), "to equal", e)
			end
			function m.toBeCloseTo(_, e, digits)
				assertion(type(actual) == "number" and math.abs(actual - e) < 0.5 * 10 ^ -(digits or 2), "to be close to", e)
			end
			function m.toBeTruthy(_)
				assertion(actual ~= nil and actual ~= false, "to be truthy")
			end
			function m.toBeFalsy(_)
				assertion(actual == nil or actual == false, "to be falsy")
			end
			function m.toBeNil(_)
				assertion(actual == nil, "to be nil")
			end
			function m.toBeGreaterThan(_, e)
				assertion(actual > e, "to be greater than", e)
			end
			function m.toBeLessThan(_, e)
				assertion(actual < e, "to be less than", e)
			end
			function m.toThrow(_)
				assertion(not pcall(actual), "to throw")
			end
			return m
		end
		local m = matchers(false)
		m["not"] = matchers(true)
		return m
	end
end

return Spec
