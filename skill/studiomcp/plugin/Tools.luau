local Tools={}
local Editor=game:GetService('ScriptEditorService')
local function resolve(path)
 if path:sub(1,12)=='LocalPlayer.' then
  local player=assert(game:GetService('Players').LocalPlayer,'No LocalPlayer in this data model')
  local object=player
  for name in path:sub(13):gmatch('[^/%.]+') do object=assert(object:FindFirstChild(name),'Missing instance: '..path) end
  return object
 end
 local details=require(script.Parent.Hierarchy)
 return details.resolve(path)
end
local function source(object)
 assert(object:IsA('LuaSourceContainer'),'Expected a script')
 return Editor:GetEditorSource(object)
end
local function encode(value,seen)
 local kind=typeof(value)
 if kind=='nil' then return {type='nil'} end
 if kind=='number' and (value~=value or math.abs(value)==math.huge) then return {type=kind,value=tostring(value)} end
 if kind=='string' or kind=='boolean' or kind=='number' then return value end
 if kind=='table' then
  seen=seen or {}; assert(not seen[value],'Cannot return cyclic tables'); seen[value]=true
  local result={}; for key,item in value do result[tostring(key)]=encode(item,seen) end
  seen[value]=nil; return result
 end
 if kind=='Instance' then return {type=kind,path='game.'..value:GetFullName(),className=value.ClassName} end
 return {type=kind,value=tostring(value)}
end
function Tools.execute(command)
 local running=game:GetService('RunService'):IsRunning()
 assert(command.datamodel=='auto' or command.datamodel==(running and 'Play' or 'Edit'),'Requested data model is not active; use playtest status/start/stop')
 local prints={}
 local function capture(kind,...)
  local texts={}; for index=1,select('#',...) do texts[index]=tostring(select(index,...)) end
  table.insert(prints,{type=kind,message=table.concat(texts,'\t')})
 end
 local module=command.execution
 if not module then module=Instance.new('ModuleScript'); module.Name='StudioMCPExecute'; module.Source='return function(print,warn)\n'..command.code..'\nend' end
 local result,done
 local worker=task.spawn(function()
  local ok,values=pcall(function() return table.pack(require(module)(function(...) capture('MessageOutput',...) end,function(...) capture('MessageWarning',...) end)) end)
  local serialized,decoded=pcall(function()
   local rows={}; if ok then for index=1,values.n do rows[index]=encode(values[index]) end end
   return {success=ok,results=rows,message=not ok and tostring(values) or nil,prints=prints,datamodel=running and 'Play' or 'Edit'}
  end)
  result=serialized and decoded or {success=false,message=tostring(decoded),prints=prints}; done=true
 end)
 local deadline=os.clock()+command.timeout
 while not done and os.clock()<deadline do task.wait() end
 if not done then task.cancel(worker); result={success=false,message='Execution timed out (mutations are not rolled back)',prints=prints} end
 module:Destroy(); return result
end
-- Headless server test: stages a Script, runs the place in Run mode through StudioTestService (no Studio
-- window needed), returns whatever the code returns. Edit mode only; the runner is removed afterwards.
function Tools.runServer(command)
 local RunService=game:GetService('RunService')
 assert(not RunService:IsRunning(),'runServer needs Studio in Edit mode')
 assert(type(command.code)=='string' and command.code~='','runServer needs code')
 local timeout=math.clamp(tonumber(command.timeout) or 30,1,600)
 local runner=Instance.new('Script')
 runner.Name='StudioMCP_RunServer'
 runner.Source=table.concat({
  "local STS=game:GetService('StudioTestService')",
  "local Http=game:GetService('HttpService')",
  "local finished=false",
  "local function finish(payload) if finished then return end finished=true STS:EndTest(Http:JSONEncode(payload)) end",
  "task.delay("..timeout..",function() finish({success=false,error='runServer timeout after "..timeout.."s'}) end)",
  "task.spawn(function()",
  " local function user()",
  command.code,
  "\n end",
  " local ok,res=xpcall(user,debug.traceback)",
  " if ok then",
  "  local good=pcall(Http.JSONEncode,Http,{res})",
  "  finish({success=true,result=good and res or tostring(res)})",
  " else finish({success=false,error=tostring(res)}) end",
  "end)",
 },'\n')
 runner.Parent=game:GetService('ServerScriptService')
 local ok,reply=pcall(function() return game:GetService('StudioTestService'):ExecuteRunModeAsync({source='romcp'}) end)
 pcall(function() runner:Destroy() end)
 if not ok then return {success=false,error=tostring(reply)} end
 local decoded,payload=pcall(game:GetService('HttpService').JSONDecode,game:GetService('HttpService'),reply)
 if decoded and type(payload)=='table' then return payload end
 return {success=false,error='runServer returned no result',raw=tostring(reply)}
end
function Tools.readScript(command)
 local object=resolve(command.path); local text=source(object); local lines=string.split(text,'\n')
 local first=command.start or 1; local last=math.min(command.finish or #lines,#lines)
 assert(first>=1 and first%1==0 and last>=first-1,'Invalid line range')
 local rows={}; for index=first,last do table.insert(rows,{line=index,text=lines[index]}) end
 return {path='game.'..object:GetFullName(),lines=rows,total=#lines}
end
function Tools.editScript(command)
 local exists,object=pcall(resolve,command.path)
 local created,parent,createName
 if not exists then
  assert(command.className=='Script' or command.className=='LocalScript' or command.className=='ModuleScript','Missing script; provide className to create it')
  local parentPath,name=command.path:match('^(.*)[/%.]([^/%.]+)$'); assert(parentPath and name,'Script needs a parent path')
  parent=resolve(parentPath); assert(not parent:FindFirstChild(name),'Cannot create ambiguous script')
  createName=name; created=true
 end
 local before=created and '' or source(object)
 if command.expected~=nil then assert(before==command.expected,'Script changed since read; refusing edit') end
 local lines=string.split(before,'\n'); local previous=#lines+1
 local edits=command.edits; assert(type(edits)=='table' and #edits>0,'Provide line edits')
 table.sort(edits,function(a,b) return a.start>b.start end)
 for _,edit in edits do
  assert(type(edit.text)=='string','Edit text must be a string')
  local first,last=edit.start,edit.finish
  assert(first%1==0 and last%1==0 and first>=1 and first<=#lines+1 and last>=first-1 and last<=#lines and last<previous,'Invalid or overlapping edit ranges')
  for index=last,first,-1 do table.remove(lines,index) end
  if edit.text~='' then local replacement=string.split(edit.text,'\n'); for index=#replacement,1,-1 do table.insert(lines,first,replacement[index]) end end
  previous=first
 end
 local after=table.concat(lines,'\n')
 if created then object=Instance.new(command.className); object.Name=createName; object.Source=after; object.Parent=parent
 else Editor:UpdateSourceAsync(object,function(current) assert(current==before,'Script changed during edit'); return after end) end
 return {path='game.'..object:GetFullName(),lines=#lines,changed=after~=before}
end
function Tools.grep(command)
 local rows,seen,truncated={},0,false
 local needle=command.ignore_case and command.query:lower() or command.query
 for _,object in game:GetDescendants() do if object:IsA('LuaSourceContainer') then
  for index,line in string.split(source(object),'\n') do
   local haystack=command.ignore_case and line:lower() or line
   if haystack:find(needle,1,command.plain) then
    seen+=1
    if seen>command.skip then
     if #rows>=command.limit then truncated=true; break end
     table.insert(rows,{path='game.'..object:GetFullName(),class=object.ClassName,line=index,text=line})
    end
   end
  end
  if truncated then break end
 end end
 return {matches=rows,truncated=truncated,next=command.skip+#rows}
end
function Tools.insert(command)
 return require(script.Parent.Library)({assetId=command.assetId,name=command.name})
end
function Tools.navigate(command)
 assert(game:GetService('RunService'):IsRunning(),'Navigation requires Play')
 local players=game:GetService('Players'); local player=players.LocalPlayer or players:GetPlayers()[1]
 local character=command.character and resolve(command.character) or assert(player,'No player').Character
 assert(character,'No character'); local humanoid=assert(character:FindFirstChildOfClass('Humanoid'),'No Humanoid')
 local root=assert(character:FindFirstChild('HumanoidRootPart'),'No HumanoidRootPart')
 local target
 if command.path then
  local object=resolve(command.path); assert(object:IsA('Model') or object:IsA('BasePart'),'Destination must be Model or BasePart'); target=object:GetPivot().Position
 else target=Vector3.new(unpack(command.position)) end
 local path=game:GetService('PathfindingService'):CreatePath(); path:ComputeAsync(root.Position,target)
 assert(path.Status==Enum.PathStatus.Success,'No traversable path')
 local deadline=os.clock()+command.timeout
 for _,waypoint in path:GetWaypoints() do
  if waypoint.Action==Enum.PathWaypointAction.Jump then humanoid.Jump=true end
  humanoid:MoveTo(waypoint.Position)
  repeat task.wait() until (root.Position-waypoint.Position).Magnitude<3 or os.clock()>deadline or humanoid.Health<=0
  if os.clock()>deadline or humanoid.Health<=0 then humanoid:Move(Vector3.zero); return {reached=false,position={root.Position.X,root.Position.Y,root.Position.Z},message='Navigation timed out or character died'} end
 end
 return {reached=(root.Position-target).Magnitude<3,position={root.Position.X,root.Position.Y,root.Position.Z}}
end
function Tools.keyboard(command)
 assert(game:GetService('RunService'):IsRunning(),'Keyboard input requires Play')
 local input=game:GetService('UserInputService'):CreateVirtualInput()
 for _,step in command.actions do
  if step.action=='wait' then task.wait(step.waitTimeMs/1000)
  elseif step.action=='textInput' then input:SendTextInput(step.textInputs)
  else
   local code=assert(Enum.KeyCode[step.keyCode],'Unknown key code')
   if step.action~='keyUp' then input:SendKey(true,code,false) end
   if step.action~='keyDown' then input:SendKey(false,code,false) end
  end
 end
 return {sent=#command.actions}
end
function Tools.mouse(command)
 assert(game:GetService('RunService'):IsRunning(),'Mouse input requires Play')
 local input=game:GetService('UserInputService'):CreateVirtualInput(); local x,y
 for _,step in command.actions do
  if step.instancePath then local object=resolve(step.instancePath); assert(object:IsA('GuiObject'),'Mouse target must be GuiObject'); local center=object.AbsolutePosition+object.AbsoluteSize/2; x,y=center.X,center.Y
  elseif step.x~=nil then x,y=step.x,step.y end
  if step.action=='wait' then task.wait(step.waitTimeMs/1000)
  else
   assert(x and y,'Mouse position is required')
   if step.action=='moveTo' then input:SendMousePosition(Vector2.new(x,y))
   elseif step.action=='scrollUp' or step.action=='scrollDown' then input:SendPointerAction(Vector2.new(x,y),{Wheel=step.action=='scrollUp' and 1 or -1})
   else local button=step.mouseButton=='left' and Enum.UserInputType.MouseButton1 or Enum.UserInputType.MouseButton2
    if step.action~='mouseButtonUp' then input:SendMouseButton(Vector2.new(x,y),button,true,0) end
    if step.action~='mouseButtonDown' then input:SendMouseButton(Vector2.new(x,y),button,false,0) end
   end
  end
 end
 return {sent=#command.actions}
end
return Tools
