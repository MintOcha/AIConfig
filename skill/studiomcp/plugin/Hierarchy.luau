local HttpService=game:GetService("HttpService")
local ReflectionService=game:GetService("ReflectionService")
local refs=setmetatable({}, {__mode="v"})
local identities=setmetatable({}, {__mode="k"})
local cursors={}
local function ref(object)
 if not object then return nil end
 local id=identities[object]
 if not id then id="instance:"..HttpService:GenerateGUID(false); identities[object]=id; refs[id]=object end
 return id
end
local function resolve(selector)
 if selector:sub(1,9)=="instance:" then
  local object=refs[selector]
  assert(object and (object==game or object:IsDescendantOf(game)),"Instance reference expired or instance was removed")
  return object
 end
 local object=game
 if selector=="game" then return object end
 for name in selector:gsub("^game[./]",""):gmatch("[^/%.]+") do
  local match
  for _,child in object:GetChildren() do
   if child.Name==name then assert(not match,"Ambiguous instance path; use a reference from ls"); match=child end
  end
  assert(match,"Missing instance: "..selector)
  object=match
 end
 return object
end
local function summary(object,depth)
 return {ref=ref(object),parent=ref(object.Parent),name=object.Name,className=object.ClassName,
  childCount=#object:GetChildren(),depth=depth}
end
local function value(data)
 local kind=typeof(data)
 if kind=="Instance" then return {type=kind,ref=ref(data)} end
 if kind=="nil" then return {type=kind} end
 if kind=="number" and (data~=data or math.abs(data)==math.huge) then return {type=kind,value=tostring(data)} end
 if kind=="string" or kind=="number" or kind=="boolean" then return data end
 if kind=="Vector3" then return {type=kind,value={data.X,data.Y,data.Z}} end
 if kind=="Vector2" then return {type=kind,value={data.X,data.Y}} end
 if kind=="Color3" then return {type=kind,value={data.R,data.G,data.B}} end
 if kind=="CFrame" then return {type=kind,value={data:GetComponents()}} end
 return {type=kind,value=tostring(data)}
end
local Hierarchy={}
Hierarchy.resolve=resolve
function Hierarchy.ls(command)
 local limit=command.limit or 200
 assert(limit%1==0 and limit>=1 and limit<=1000,"limit must be an integer from 1 to 1000")
 local now=os.clock()
 local count=0
 for id,state in cursors do
  if now-state.time>120 then cursors[id]=nil else count+=1 end
 end
 local state
 if command.cursor then
  state=cursors[command.cursor]; assert(state,"Listing cursor expired")
  assert(state.action==command.action,"Cursor belongs to another hierarchy operation")
  cursors[command.cursor]=nil
 else
  assert(count<32,"Too many active listings; finish existing pages or wait for expiry")
  local root=resolve(command.path or "game")
  state={root=root,action=command.action,recursive=command.recursive==true or command.action=="find",
   name=command.name and command.name:lower(),className=command.className,
   stack={{children=root:GetChildren(),index=1,depth=1}}}
  if state.className then assert(ReflectionService:GetClass(state.className),"Unknown class: "..state.className) end
 end
 local entries={}
 local scanned=0
 while #state.stack>0 and #entries<limit and scanned<5000 do
  local frame=state.stack[#state.stack]
  local object=frame.children[frame.index]
  if not object then table.remove(state.stack) else
   frame.index+=1
   scanned+=1
   if object:IsDescendantOf(state.root) then
    if (not state.name or object.Name:lower():find(state.name,1,true)) and
     (not state.className or object:IsA(state.className)) then
     table.insert(entries,summary(object,frame.depth))
    end
    if state.recursive then table.insert(state.stack,{children=object:GetChildren(),index=1,depth=frame.depth+1}) end
   end
  end
 end
 local cursor
 if #state.stack>0 then cursor=HttpService:GenerateGUID(false); state.time=now; cursors[cursor]=state end
 return {root=summary(state.root,0),entries=entries,cursor=cursor,recursive=state.recursive,
  complete=cursor==nil,scanned=scanned}
end
function Hierarchy.find(command)
 return Hierarchy.ls(command)
end
function Hierarchy.cat(command)
 local object=resolve(command.path)
 local result=summary(object,0)
 result.attributes={}
 for name,data in object:GetAttributes() do result.attributes[name]=value(data) end
 result.tags=object:GetTags()
 result.properties={}
 result.errors={}
 local properties=ReflectionService:GetPropertiesOfClass(object.ClassName,{ExcludeDisplay=true})
 assert(properties,"No reflection information for "..object.ClassName)
 for _,property in properties do
  local ok,data=pcall(function() return object[property.Name] end)
  if ok then result.properties[property.Name]=value(data) else result.errors[property.Name]=tostring(data) end
 end
 return result
end
return Hierarchy
