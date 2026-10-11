--!nocheck
-- wraps Lune instances so scripts see roblox behaviour: derived properties (Position) work, writes are checked
-- against the roblox reflection database, pivots and bounding boxes exist, and anything Lune cannot do errors loudly
-- instead of being silently ignored. shared by the compiler (.build.luau) and the test host (runtime scripts).
--
-- realms: the same instance wrapped for the server and for each client is a different proxy, so the host can tell who is
-- asking (a client script's remote:FireServer is not a server script's). realm is nil at build time.
-- hooks (set by the test host): Proxy.hooks.member(inst, key, realm) -> handled, value
--                               Proxy.hooks.write(inst, key, value, realm) -> handled
local roblox = require("@lune/roblox")
local CFrame = roblox.CFrame
local V3 = roblox.Vector3
local db = roblox.getReflectionDatabase()

local Proxy = {}
Proxy.hooks = {}

local byRealm = {}
local rawOf = {}
local realmOf = {}

local function unwrap(v)
	if type(v) == "table" and rawOf[v] then
		return rawOf[v]
	end
	return v
end

local propCache = {}
local function propertyInfo(className, key)
	local cacheKey = className .. "." .. key
	local cached = propCache[cacheKey]
	if cached ~= nil then
		return cached or nil
	end
	local found = false
	local name = className
	while name do
		local class = db:GetClass(name)
		if not class then
			break
		end
		if class.Properties[key] then
			found = class.Properties[key]
			break
		end
		name = class.Superclass
	end
	propCache[cacheKey] = found
	return found or nil
end

-- pivots and bounding boxes (Lune has neither). deterministic, so fine for build scripts and tests.
local function partsOf(inst)
	local parts = {}
	if inst:IsA("BasePart") then
		table.insert(parts, inst)
	end
	for _, d in inst:GetDescendants() do
		if d:IsA("BasePart") then
			table.insert(parts, d)
		end
	end
	return parts
end

local function boundingBox(inst)
	local lo, hi
	for _, part in partsOf(inst) do
		local half = part.Size / 2
		for _, sx in { -1, 1 } do
			for _, sy in { -1, 1 } do
				for _, sz in { -1, 1 } do
					local p = part.CFrame * V3.new(half.X * sx, half.Y * sy, half.Z * sz)
					lo = lo and V3.new(math.min(lo.X, p.X), math.min(lo.Y, p.Y), math.min(lo.Z, p.Z)) or p
					hi = hi and V3.new(math.max(hi.X, p.X), math.max(hi.Y, p.Y), math.max(hi.Z, p.Z)) or p
				end
			end
		end
	end
	if not lo then
		return CFrame.new(0, 0, 0), V3.zero
	end
	return CFrame.new((lo + hi) / 2), hi - lo
end

-- roblox: a part's pivot is CFrame * PivotOffset; a model's is its PrimaryPart's pivot (or WorldPivot without one).
-- pivotTo moves every part by the same delta, so it is the inverse of getPivot for both.
local function getPivot(inst)
	if inst:IsA("BasePart") then
		return inst.CFrame * inst.PivotOffset
	end
	local primary = inst.PrimaryPart
	if primary then
		return primary.CFrame * primary.PivotOffset
	end
	return (boundingBox(inst))
end

local function pivotTo(inst, target)
	local delta = target * getPivot(inst):Inverse()
	for _, part in partsOf(inst) do
		part.CFrame = delta * part.CFrame
	end
end

-- Model:ScaleTo / GetScale (Lune has neither). like roblox: part sizes and offsets from the pivot scale, so do attachment and joint offsets.
-- the scale itself is remembered per model (weak, copied by Clone) since there is no property to hold it.
local modelScale = setmetatable({}, { __mode = "k" })

-- BasePart.LocalTransparencyModifier is a client-side render modifier Lune does not hold: remembered per part, default 0.
local localTransparency = setmetatable({}, { __mode = "k" })

-- Lune cannot assign nil to an Instance-valued property (Highlight.Adornee = nil, Weld.Part1 = nil ...). roblox can. a cleared ref is remembered
-- here and reads back as nil until something is assigned again. (the saved file keeps the old value: only reads are emulated.)
local clearedRefs = setmetatable({}, { __mode = "k" })

local function getScale(inst)
	return modelScale[inst] or 1
end

local function scaleTo(inst, target)
	assert(type(target) == "number" and target > 0, "ScaleTo expects a positive number")
	local k = target / getScale(inst)
	local pivot = getPivot(inst)
	local origin = pivot.Position
	for _, part in partsOf(inst) do
		local cf = part.CFrame
		part.Size = part.Size * k
		part.CFrame = CFrame.new(origin + (cf.Position - origin) * k) * cf.Rotation
	end
	for _, d in inst:GetDescendants() do
		if d:IsA("Attachment") then
			d.CFrame = CFrame.new(d.CFrame.Position * k) * d.CFrame.Rotation
			d.Position = d.Position * k -- Lune keeps Position beside CFrame, roblox derives one from the other
		elseif d:IsA("JointInstance") then
			d.C0 = CFrame.new(d.C0.Position * k) * d.C0.Rotation
			d.C1 = CFrame.new(d.C1.Position * k) * d.C1.Rotation
		end
	end
	modelScale[inst] = target
end

-- Lune sometimes ACCEPTS a write and ignores or rewrites it (legacy/alias properties like Font, derived ones). when verify is on
-- (build time) every write is read back and must match, so the line that wrote it fails, not the file write much later.
Proxy.verify = false
local VERIFIED = { number = true, boolean = true, string = true, EnumItem = true, Vector3 = true, Vector2 = true, Color3 = true, UDim2 = true, UDim = true, CFrame = true }

-- roblox stores some values at lower precision (Part.Size 0.3 reads back as 0.30000305), so compare with a tolerance
local function close(a, b)
	return math.abs(a - b) <= 1e-3 * (1 + math.abs(a))
end

local function sameValue(a, b)
	local kind = typeof(a)
	if kind ~= typeof(b) then
		return false
	end
	if kind == "number" then
		return close(a, b)
	elseif kind == "Vector3" then
		return close(a.X, b.X) and close(a.Y, b.Y) and close(a.Z, b.Z)
	elseif kind == "Vector2" then
		return close(a.X, b.X) and close(a.Y, b.Y)
	elseif kind == "Color3" then
		return close(a.R, b.R) and close(a.G, b.G) and close(a.B, b.B)
	elseif kind == "UDim" then
		return close(a.Scale, b.Scale) and close(a.Offset, b.Offset)
	elseif kind == "UDim2" then
		return close(a.X.Scale, b.X.Scale) and close(a.X.Offset, b.X.Offset) and close(a.Y.Scale, b.Y.Scale) and close(a.Y.Offset, b.Y.Offset)
	elseif kind == "CFrame" then
		local ca, cb = { a:GetComponents() }, { b:GetComponents() }
		for i = 1, 12 do
			if not close(ca[i], cb[i]) then
				return false
			end
		end
		return true
	end
	return a == b
end

local function verifyWrite(inst, key, value)
	if not VERIFIED[typeof(value)] then
		return
	end
	local ok, back = pcall(function()
		return inst[key]
	end)
	if ok and back ~= nil and not sameValue(back, value) then
		error(`{inst.ClassName} "{inst.Name}": {tostring(key)} = {tostring(value)} was not stored, it reads back as {tostring(back)} (Lune ignores or rewrites this property)`, 4)
	end
end

-- raw read/write that know about derived properties. used by the proxy and by the host (tweens, humanoids)
function Proxy.get(inst, key)
	if key == "Position" and inst:IsA("BasePart") then
		return inst.CFrame.Position
	end
	if key == "LocalTransparencyModifier" and inst:IsA("BasePart") then
		return localTransparency[inst] or 0
	end
	if clearedRefs[inst] and clearedRefs[inst][key] then
		return nil
	end
	return inst[key]
end

-- Studio couples TextScaled and TextWrapped: scaled text is always wrapped, and a file that says TextScaled = true with
-- TextWrapped = false loads with TextScaled off (size 8, Lune's default). So a scaled label is always written wrapped.
-- Lune's binary writer also fills every label that never set a TextSize with 8, so no label may carry a TextSize at all: a
-- fixed size is recorded as an attribute while building and turned into TextScaled + a UITextSizeConstraint with min = max
-- (same size, and it ignores the text's length) by finalizeText right before the file is written.
local TEXT_CLASSES = { TextLabel = true, TextButton = true, TextBox = true }
local FIXED_TEXT_SIZE = "__FixedTextSize"

function Proxy.finalizeText(roots)
	local function visit(inst)
		if TEXT_CLASSES[inst.ClassName] then
			local size = inst:GetAttribute(FIXED_TEXT_SIZE)
			if size then
				inst:SetAttribute(FIXED_TEXT_SIZE, nil)
				for _, child in inst:GetChildren() do
					if child.ClassName == "UITextSizeConstraint" then
						child:Destroy()
					end
				end
				local limit = roblox.Instance.new("UITextSizeConstraint")
				limit.Name = "FixedTextSize"
				limit.MinTextSize = size
				limit.MaxTextSize = size
				limit.Parent = inst
				inst.TextScaled = true
			end
			if inst.TextScaled then
				inst.TextWrapped = true
			end
		end
		for _, child in inst:GetChildren() do
			visit(child)
		end
	end
	for _, root in roots do
		visit(root)
	end
end

function Proxy.set(inst, key, value)
	if inst:IsA("BasePart") then
		if key == "LocalTransparencyModifier" then
			localTransparency[inst] = value
			return
		end
		if key == "Position" then
			inst.CFrame = CFrame.new(value) * (inst.CFrame - inst.CFrame.Position)
			return
		elseif key == "Orientation" or key == "Rotation" then
			inst.CFrame = CFrame.new(inst.CFrame.Position) * CFrame.fromEulerAnglesYXZ(math.rad(value.X), math.rad(value.Y), math.rad(value.Z))
			return
		end
	end
	-- the legacy Font enum: roblox keeps it in sync with FontFace, Lune ignores it (and the file then fails to write)
	if key == "Font" and typeof(value) == "EnumItem" and propertyInfo(inst.ClassName, "FontFace") then
		inst.FontFace = roblox.Font.fromEnum(value)
		return
	end
	-- at build time a fixed text size is not written as TextSize (see Proxy.finalizeText), so the file never carries one
	if Proxy.verify and key == "TextSize" and typeof(value) == "number" and TEXT_CLASSES[inst.ClassName] then
		inst:SetAttribute(FIXED_TEXT_SIZE, value)
		return
	end
	local info = propertyInfo(inst.ClassName, tostring(key))
	if not info then
		error(`{inst.ClassName} has no property '{tostring(key)}'`, 3)
	end
	if info.Scriptability ~= "ReadWrite" and not (key == "MeshId" or key == "TextureID") then
		error(`{inst.ClassName}.{tostring(key)} is read-only`, 3)
	end
	local raw = unwrap(value)
	if raw == nil then
		if not pcall(function() inst[key] = nil end) then
			local cleared = clearedRefs[inst]
			if not cleared then cleared = {} clearedRefs[inst] = cleared end
			cleared[key] = true
		end
		return
	end
	if clearedRefs[inst] then clearedRefs[inst][key] = nil end
	inst[key] = raw
	if Proxy.verify then
		verifyWrite(inst, key, raw)
	end
end

local wrap

local function wrapResult(v, realm)
	if typeof(v) == "Instance" then
		return wrap(v, realm)
	end
	if type(v) == "table" then
		local out = {}
		for k, x in v do
			out[k] = wrapResult(x, realm)
		end
		return out
	end
	return v
end

-- move values (instance proxies, plain tables of them) into another realm
function Proxy.rewrap(v, realm)
	if type(v) == "table" then
		if rawOf[v] then
			return wrap(rawOf[v], realm)
		end
		if getmetatable(v) == nil then
			local out = {}
			for k, x in v do
				out[k] = Proxy.rewrap(x, realm)
			end
			return out
		end
	end
	return v
end

local mt = {}

function mt.__index(proxy, key)
	local inst, realm = rawOf[proxy], realmOf[proxy]
	local hook = Proxy.hooks.member
	if hook then
		local handled, value = hook(inst, key, realm)
		if handled then
			return value
		end
	end
	if key == "Position" and inst:IsA("BasePart") then
		return inst.CFrame.Position
	end
	if key == "LocalTransparencyModifier" and inst:IsA("BasePart") then
		return localTransparency[inst] or 0
	end
	-- Attachment.WorldCFrame / WorldPosition: the attachment's CFrame in its BasePart parent's space, moved to the world
	if (key == "WorldCFrame" or key == "WorldPosition") and inst:IsA("Attachment") and inst.Parent and inst.Parent:IsA("BasePart") then
		local world = inst.Parent.CFrame * inst.CFrame
		if key == "WorldPosition" then
			return world.Position
		end
		return world
	end
	if key == "GetPivot" and (inst:IsA("Model") or inst:IsA("BasePart")) then
		return function()
			return getPivot(inst)
		end
	elseif key == "PivotTo" and (inst:IsA("Model") or inst:IsA("BasePart")) then
		return function(_, cframe)
			pivotTo(inst, cframe)
		end
	elseif key == "GetScale" and inst:IsA("Model") then
		return function()
			return getScale(inst)
		end
	elseif key == "ScaleTo" and inst:IsA("Model") then
		return function(_, scale)
			scaleTo(inst, scale)
		end
	elseif key == "Clone" and inst:IsA("Model") then
		return function()
			local copy = inst:Clone()
			-- descendants that are models keep their own scale too
			local src, dst = { inst, table.unpack(inst:GetDescendants()) }, { copy, table.unpack(copy:GetDescendants()) }
			for i, m in src do
				if modelScale[m] then
					modelScale[dst[i]] = modelScale[m]
				end
			end
			return wrapResult(copy, realm)
		end
	elseif key == "GetBoundingBox" and inst:IsA("Model") then
		return function()
			return boundingBox(inst)
		end
	end
	if key == "WaitForChild" then
		return function(_, name)
			return wrapResult(inst:FindFirstChild(name), realm)
		end
	end
	if clearedRefs[inst] and clearedRefs[inst][key] then
		return nil
	end
	local ok, v = pcall(function()
		return inst[key]
	end)
	if not ok then
		local child = inst:FindFirstChild(key)
		if child then
			return wrapResult(child, realm)
		end
		error(`{tostring(key)} is not a member of {inst.ClassName} "{inst.Name}": either it does not exist (typo, or a child nothing has created yet) or it is not emulated here`, 2)
	end
	if type(v) == "function" then
		return function(_, ...)
			local args = table.pack(...)
			for i = 1, args.n do
				args[i] = unwrap(args[i])
			end
			return wrapResult(v(inst, table.unpack(args, 1, args.n)), realm)
		end
	end
	return wrapResult(v, realm)
end

function mt.__newindex(proxy, key, value)
	local inst, realm = rawOf[proxy], realmOf[proxy]
	local hook = Proxy.hooks.write
	if hook and hook(inst, key, value, realm) then
		return
	end
	Proxy.set(inst, key, value)
end

function mt.__tostring(proxy)
	return tostring(rawOf[proxy])
end

function wrap(inst, realm)
	local key = realm == nil and "build" or realm
	local map = byRealm[key]
	if not map then
		map = {}
		byRealm[key] = map
	end
	local p = map[inst]
	if p then
		return p
	end
	p = setmetatable({}, mt)
	rawOf[p] = inst
	realmOf[p] = realm
	map[inst] = p
	return p
end

Proxy.wrap = wrap
Proxy.unwrap = unwrap
return Proxy
