--!nocheck
-- children named after a member of their parent's class. in Roblox `parent.Remove` / `button.Selected` / `frame.Size`
-- returns the member, not the child, so script code that dots into that child silently gets the wrong thing.
-- NameCheck.run(roots) -> list of warning strings (at most 25 + a count) (FindFirstChild still works, so this is a warning, not an error).
local roblox = require("@lune/roblox")

local NameCheck = {}

-- methods and events are not in the reflection database: the likely ones, with the class that has them
local METHODS = {}
local function add(class, names)
	for _, name in names do
		METHODS[name] = METHODS[name] or {}
		table.insert(METHODS[name], class)
	end
end
add("Instance", {
	"Remove", "Destroy", "Clone", "ClearAllChildren", "FindFirstChild", "FindFirstChildOfClass", "FindFirstChildWhichIsA",
	"FindFirstAncestor", "GetChildren", "GetDescendants", "GetAttribute", "SetAttribute", "GetAttributes", "IsA",
	"IsDescendantOf", "IsAncestorOf", "WaitForChild", "GetFullName", "AddTag", "RemoveTag", "HasTag", "GetTags",
	"GetPropertyChangedSignal", "GetAttributeChangedSignal", "Changed", "ChildAdded", "ChildRemoved", "Destroying",
	"AncestryChanged", "DescendantAdded", "DescendantRemoving", "AttributeChanged",
})
add("GuiObject", { "TweenPosition", "TweenSize", "TweenSizeAndPosition", "InputBegan", "InputEnded", "MouseEnter", "MouseLeave" })
add("GuiButton", { "Activated", "MouseButton1Click", "MouseButton1Down", "MouseButton1Up", "MouseButton2Click" })
add("PVInstance", { "GetPivot", "PivotTo" })
add("Model", { "MoveTo", "GetBoundingBox", "GetExtentsSize", "ScaleTo", "GetScale" })
add("BasePart", { "Touched", "TouchEnded", "GetMass", "ApplyImpulse", "GetTouchingParts" })
add("Sound", { "Play", "Stop", "Pause", "Resume", "Played", "Ended" })
add("TweenBase", { "Play", "Cancel", "Pause", "Completed" })
add("Humanoid", { "MoveTo", "Died", "LoadAnimation", "TakeDamage", "AddAccessory", "EquipTool" })

local ok, db = pcall(roblox.getReflectionDatabase)

-- does `className` (or a superclass) have a property called `name`?
local propertyCache = {}
local function hasProperty(className, name)
	if not ok or not db then
		return false
	end
	local key = className .. "." .. name
	if propertyCache[key] ~= nil then
		return propertyCache[key]
	end
	local found = false
	local cls = db:GetClass(className)
	while cls do
		if cls.Properties[name] then
			found = true
			break
		end
		cls = cls.Superclass and db:GetClass(cls.Superclass)
	end
	propertyCache[key] = found
	return found
end

local function isA(className, base)
	if className == base then
		return true
	end
	if not ok or not db then
		return base == "Instance"
	end
	local cls = db:GetClass(className)
	while cls do
		if cls.Name == base then
			return true
		end
		cls = cls.Superclass and db:GetClass(cls.Superclass)
	end
	return false
end

local function hasMethod(className, name)
	for _, base in METHODS[name] or {} do
		if isA(className, base) then
			return true
		end
	end
	return false
end

local function path(inst)
	local parts = {}
	while inst and inst.ClassName ~= "DataModel" do
		table.insert(parts, 1, inst.Name)
		inst = inst.Parent
	end
	return table.concat(parts, ".")
end

function NameCheck.run(roots)
	local warnings = {}
	local function visit(inst)
		for _, child in inst:GetChildren() do
			local name = child.Name
			local kind = hasMethod(inst.ClassName, name) and "method" or (hasProperty(inst.ClassName, name) and "property" or nil)
			if kind then
				-- a property that holds this very child (Workspace.Terrain, Workspace.CurrentCamera) is not a clash
				local same = false
				if kind == "property" then
					local okGet, value = pcall(function()
						return inst[name]
					end)
					same = okGet and value == child
				end
				if not same then
					table.insert(warnings, `{path(child)}: "{name}" is a {kind} of {inst.ClassName}, so {inst.Name}.{name} is not this {child.ClassName} (rename it, or use FindFirstChild)`)
				end
			end
			visit(child)
		end
	end
	for _, root in roots do
		visit(root)
	end
	local LIMIT = 25
	if #warnings > LIMIT then
		local more = #warnings - LIMIT
		warnings = table.move(warnings, 1, LIMIT, 1, {})
		table.insert(warnings, `... and {more} more children named after a member of their parent`)
	end
	return warnings
end

return NameCheck
