--!nocheck
-- a roblox style signal: Connect / Once / Wait / Fire. every handler runs in its own thread, so one error
-- does not stop the others (like roblox). spawn(f, ...) runs a function as a managed thread.
local Signal = {}

function Signal.new(spawn, resume)
	local list = {}
	local waiters = {}
	local sig = {}
	function sig:Connect(f)
		local entry = { f = f }
		table.insert(list, entry)
		local conn = { Connected = true }
		function conn:Disconnect()
			conn.Connected = false
			local i = table.find(list, entry)
			if i then
				table.remove(list, i)
			end
		end
		return conn
	end
	function sig:Once(f)
		local conn
		conn = sig:Connect(function(...)
			conn:Disconnect()
			f(...)
		end)
		return conn
	end
	function sig:Wait()
		table.insert(waiters, coroutine.running())
		return coroutine.yield()
	end
	function sig:Fire(...)
		for _, e in table.clone(list) do
			spawn(e.f, ...)
		end
		if #waiters > 0 then
			local woken = waiters
			waiters = {}
			for _, co in woken do
				resume(co, ...)
			end
		end
	end
	function sig:Count()
		return #list
	end
	return setmetatable(sig, {
		__index = function(_, k)
			error(`Signal.{tostring(k)} is not emulated`, 2)
		end,
	})
end

return Signal
