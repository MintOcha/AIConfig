--!nocheck
-- instance tree -> renderer scene nodes (robloxvibes preview schema v3). shared by export.luau and view.luau
local roblox = require("@lune/roblox")
local V3, CFrame = roblox.Vector3, roblox.CFrame

local Scene = {}

local db = roblox.getReflectionDatabase()
local propCache = {}

-- every readable, non-deprecated property of a class, including inherited ones
local function propertiesOf(className)
	if propCache[className] then
		return propCache[className]
	end
	local names, seen = {}, {}
	local name = className
	while name do
		local class = db:GetClass(name)
		if not class then
			break
		end
		for key, info in class.Properties do
			local tags = info.Tags or {}
			local bad = table.find(tags, "Deprecated") or table.find(tags, "NotScriptable") or table.find(tags, "WriteOnly")
			local readable = info.Scriptability == "ReadWrite" or info.Scriptability == "Read"
			if readable and not bad and not seen[key] and key ~= "Parent" then
				seen[key] = true
				table.insert(names, key)
			end
		end
		name = class.Superclass
	end
	table.sort(names)
	propCache[className] = names
	return names
end

local function finite(n)
	if n ~= n or n == math.huge or n == -math.huge then
		return nil
	end
	return n
end

function Scene.encode(value)
	local kind = typeof(value)
	if kind == "number" then
		return finite(value)
	elseif kind == "boolean" or kind == "string" then
		return value
	elseif kind == "Color3" then
		return { value.R, value.G, value.B }
	elseif kind == "Vector2" then
		return { value.X, value.Y }
	elseif kind == "Vector3" then
		return { value.X, value.Y, value.Z }
	elseif kind == "CFrame" then
		return { value:GetComponents() }
	elseif kind == "UDim" then
		return { value.Scale, value.Offset }
	elseif kind == "UDim2" then
		return { value.X.Scale, value.X.Offset, value.Y.Scale, value.Y.Offset }
	elseif kind == "Rect" then
		return { value.Min.X, value.Min.Y, value.Max.X, value.Max.Y }
	elseif kind == "NumberRange" then
		return { value.Min, value.Max }
	elseif kind == "NumberSequence" then
		local keys = {}
		for _, k in value.Keypoints do
			table.insert(keys, { k.Time, k.Value, k.Envelope })
		end
		return keys
	elseif kind == "ColorSequence" then
		local keys = {}
		for _, k in value.Keypoints do
			table.insert(keys, { k.Time, { k.Value.R, k.Value.G, k.Value.B } })
		end
		return keys
	elseif kind == "EnumItem" then
		return value.Name
	elseif kind == "Font" then
		return { family = value.Family, weight = value.Weight.Name, style = value.Style.Name }
	elseif kind == "BrickColor" then
		return { value.Color.R, value.Color.G, value.Color.B }
	end
	return nil
end

-- state: { nextId = 0, count = 0, onPart = function(instance)? }
function Scene.newState(onPart)
	return { nextId = 0, count = 0, onPart = onPart }
end

function Scene.node(instance, state)
	state.nextId += 1
	state.count += 1
	local id = state.nextId
	local props = {}
	for _, key in propertiesOf(instance.ClassName) do
		local ok, value = pcall(function()
			return instance[key]
		end)
		if ok and value ~= nil then
			props[key] = Scene.encode(value)
		end
	end
	if instance:IsA("BasePart") then
		local p = instance.CFrame.Position
		props.Position = { p.X, p.Y, p.Z }
		if state.onPart then
			state.onPart(instance)
		end
	end
	local children = {}
	for _, child in instance:GetChildren() do
		table.insert(children, Scene.node(child, state))
	end
	return { id = id, class = instance.ClassName, name = instance.Name, props = props, children = children }
end

-- Roblox's CFrame.lookAt (Lune's faces the other way)
function Scene.lookAt(at, target)
	local look = (target - at).Unit
	local right = look:Cross(V3.yAxis).Unit
	return CFrame.fromMatrix(at, right, right:Cross(look).Unit, -look)
end

-- axis-aligned box of the parts under an instance (a part counts as a cube of its largest dimension)
function Scene.bounds(root)
	local lo, hi
	local function add(part)
		local p = part.CFrame.Position
		local r = math.max(part.Size.X, part.Size.Y, part.Size.Z) / 2
		local a, b = p - V3.new(r, r, r), p + V3.new(r, r, r)
		lo = lo and V3.new(math.min(lo.X, a.X), math.min(lo.Y, a.Y), math.min(lo.Z, a.Z)) or a
		hi = hi and V3.new(math.max(hi.X, b.X), math.max(hi.Y, b.Y), math.max(hi.Z, b.Z)) or b
	end
	if root:IsA("BasePart") then
		add(root)
	end
	for _, d in root:GetDescendants() do
		if d:IsA("BasePart") then
			add(d)
		end
	end
	return lo, hi
end

return Scene
