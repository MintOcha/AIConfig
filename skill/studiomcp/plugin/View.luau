local views={
 {"Front",Vector3.new(0,0,-1)}, {"Back",Vector3.new(0,0,1)}, {"Left",Vector3.new(-1,0,0)},
 {"Right",Vector3.new(1,0,0)}, {"Top",Vector3.new(0,1,0)}, {"Bottom",Vector3.new(0,-1,0)},
 {"Front right",Vector3.new(1,1,-1)}, {"Front left",Vector3.new(-1,1,-1)}, {"Back right",Vector3.new(1,1,1)},
}
local session
local function restore()
 local previous=session
 session=nil
 if not previous then return end
 local failures={}
 local function attempt(label,operation)
  local ok,err=pcall(operation)
  if not ok then table.insert(failures,label..': '..tostring(err)) end
 end
 if previous.camera then
  attempt('camera frame',function() previous.camera.CFrame=previous.frame end)
  attempt('camera focus',function() previous.camera.Focus=previous.focus end)
  attempt('camera type',function() previous.camera.CameraType=previous.cameraType end)
 end
 if previous.lighting then
  for property,value in previous.lighting do
   attempt('lighting '..property,function() game:GetService('Lighting')[property]=value end)
  end
 end
 if previous.atmospheres then
  for atmosphere,properties in previous.atmospheres do
   attempt('atmosphere',function() if atmosphere.Parent then atmosphere.Density=properties.Density; atmosphere.Haze=properties.Haze end end)
  end
 end
 if previous.added then attempt('observer disconnect',function() previous.added:Disconnect() end) end
 if previous.scratch then attempt('scratch removal',function() previous.scratch:Destroy() end) end
 if previous.clones then
  for _,clone in previous.clones do attempt('clone removal',function() clone:Destroy() end) end
 end
 for part,modifier in previous.geometry do
  attempt('part visibility',function() if part.Parent then part.LocalTransparencyModifier=modifier end end)
 end
 for humanoid,distance in previous.labels do
  attempt('humanoid labels',function() if humanoid.Parent then humanoid.DisplayDistanceType=distance end end)
 end
 for gui,enabled in previous.ui do
  attempt('UI visibility',function() if gui.Parent then gui.Enabled=enabled end end)
 end
 attempt('selection',function() game:GetService('Selection'):Set(previous.selection) end)
 assert(#failures==0,'Inspection restoration failed: '..table.concat(failures,'; '))
end
local function armTimeout()
 local current=session
 current.deadline=os.clock()+120
 local deadline=current.deadline
 task.delay(120,function()
  if session==current and current.deadline==deadline then restore() end
 end)
end
local function bounds(object)
 if object:IsA('Model') then
  assert(object:FindFirstChildWhichIsA('BasePart',true),'Inspection model has no BaseParts')
  return object:GetBoundingBox()
 end
 return object.CFrame,object.Size
end
local function fit(camera,entry,direction)
 -- Flat terrain needs a little elevation rather than an edge-on horizon.
 if math.abs(direction.Y)<.01 and entry.size.Y<math.max(entry.size.X,entry.size.Z)*.2 then
  direction=(direction+Vector3.yAxis*.3).Unit
 end
 local up=math.abs(direction.Y)>.99 and Vector3.new(0,0,-1) or Vector3.yAxis
 local basis=CFrame.lookAt(entry.center,entry.center-direction,up)
 local vertical=math.tan(math.rad(camera.FieldOfView/2))
 local horizontal=vertical*camera.ViewportSize.X/camera.ViewportSize.Y
 local distance=0
 -- Fit the oriented box in perspective, including each corner's depth.
 -- 80% of the available width/height leaves a stable inspection border.
 local near=math.abs(camera.NearPlaneZ)+.05
 for x=-1,1,2 do for y=-1,1,2 do for z=-1,1,2 do
  local point=entry.box:PointToWorldSpace(entry.size*Vector3.new(x,y,z)/2)
  local relative=basis:PointToObjectSpace(point)
  distance=math.max(distance,relative.Z+math.abs(relative.X)/(.8*horizontal),relative.Z+math.abs(relative.Y)/(.8*vertical),relative.Z+near)
 end end end
 return CFrame.lookAt(entry.center+direction*distance,entry.center,up)
end
local function run(object,stage,index,rig,angle,samples,cameraFrame,contactSheet)
 if stage=='end' then restore(); return {restored=true} end
 if stage=='begin' then
  assert(object,'Missing instance')
  if session then restore() end
  if object==game then object=workspace end
  if cameraFrame then
   assert(object==workspace and not contactSheet,'camera_cframe is supported only for game viewing')
   assert(type(cameraFrame)=='table' and #cameraFrame==12,'camera_cframe requires 12 CFrame components')
  end
  local target=object:IsA('LayerCollector') and object or object:IsA('GuiObject') and object:FindFirstAncestorWhichIsA('LayerCollector')
  if target and not contactSheet then
   session={ui={},geometry={},labels={},selection=game:GetService('Selection'):Get(),count=1,target=target}
   armTimeout()
   local function isolate(gui)
    if gui:IsA('LayerCollector') and not gui:IsA('PluginGui') and not gui:IsDescendantOf(game:GetService('CoreGui')) then
     if session.ui[gui]==nil then session.ui[gui]=gui.Enabled end
     gui.Enabled=gui==target
    end
   end
   for _,gui in game:GetDescendants() do isolate(gui) end
   session.added=game.DescendantAdded:Connect(isolate)
   return {views=1}
  end
  local entries={}
  if contactSheet then
   for _,child in object:GetChildren() do
    if child:IsA('Model') or child:IsA('BasePart') then table.insert(entries,{object=child,label=child.Name}) end
   end
   assert(#entries>0,'Contact sheet requires child Models or BaseParts')
  else
   assert(object==workspace or object:IsA('Model') or object:IsA('BasePart'),'Inspect requires game, a model, a part or a UI root')
   entries={{object=object,label=object.Name}}
  end
  local direction
  if angle and object~=workspace then
   for _,view in views do if view[1]:lower()==angle:lower() then direction=view[2].Unit end end
   assert(direction,'Unknown inspection angle')
  end
  local camera=assert(workspace.CurrentCamera,'Studio has no viewport camera')
  local viewport=camera.ViewportSize
  assert(viewport.X>0 and viewport.Y>0,'Studio viewport unavailable')
  session={camera=camera,cameraType=camera.CameraType,frame=camera.CFrame,focus=camera.Focus,direction=direction,
   count=contactSheet and #entries or object==workspace and 1 or direction and 1 or #views,scene=object==workspace and not contactSheet,
   entries=entries,contactSheet=contactSheet,ui={},geometry={},labels={},selection=game:GetService('Selection'):Get()}
  armTimeout()
  if not session.scene then
   local lighting=game:GetService('Lighting')
   session.lighting={FogStart=lighting.FogStart,FogEnd=lighting.FogEnd}
   lighting.FogStart=1000000; lighting.FogEnd=1000000
   session.atmospheres={}
   for _,effect in lighting:GetChildren() do if effect:IsA('Atmosphere') then
    session.atmospheres[effect]={Density=effect.Density,Haze=effect.Haze}
    effect.Density=0; effect.Haze=0
   end end
  end
  for _,entry in entries do
   local source=entry.object
   if not session.scene and (contactSheet or not source:IsDescendantOf(workspace)) then
    if not session.scratch then
     local scratch=Instance.new('Folder')
     session.scratch=scratch
     scratch.Name='RoMCPViewScratch_'..game:GetService('HttpService'):GenerateGUID(false)
     scratch.Parent=workspace
    end
    local clone=assert(source:Clone(),'Inspection target is not Archivable')
    entry.object=clone
    session.clones=session.clones or {}
    table.insert(session.clones,clone)
    -- Keep the clone detached until every executable descendant is removed.
    for _,instance in clone:GetDescendants() do
     if instance:IsA('LuaSourceContainer') then instance:Destroy() end
    end
    if clone:IsA('BasePart') then clone.Anchored=true end
    for _,instance in clone:GetDescendants() do
     if instance:IsA('BasePart') then instance.Anchored=true end
    end
    clone:PivotTo(CFrame.new(10000,10000,10000))
    clone.Parent=session.scratch
   end
   if not session.scene then
    local center,size=bounds(entry.object)
    entry.center=center.Position; entry.box=center; entry.size=size
   end
  end
  session.active=entries[1].object
  local function hide(instance)
   if not session then return end
   local active=session.active
   if instance:IsA('BasePart') then
    local included=instance==active or instance:IsDescendantOf(active)
    if session.geometry[instance]==nil then session.geometry[instance]=instance.LocalTransparencyModifier end
    instance.LocalTransparencyModifier=included and session.geometry[instance] or 1
   elseif instance:IsA('Humanoid') then
    if session.labels[instance]==nil then session.labels[instance]=instance.DisplayDistanceType end
    instance.DisplayDistanceType=instance:IsDescendantOf(active) and session.labels[instance] or Enum.HumanoidDisplayDistanceType.None
   elseif session.ui[instance]==nil and instance:IsA('LayerCollector') and not instance:IsA('PluginGui') and not instance:IsDescendantOf(game:GetService('CoreGui')) then
    session.ui[instance]=instance.Enabled
    instance.Enabled=false
   end
  end
  session.hide=hide
  for _,instance in game:GetDescendants() do hide(instance) end
  -- Edit's native camera controller can retain a stale render pose even when
  -- CurrentCamera.CFrame reads back correctly. Own the camera in both modes.
  camera.CameraType=Enum.CameraType.Scriptable
  session.added=game.DescendantAdded:Connect(hide)
  game:GetService('Selection'):Set({})
  return {views=session.count}
 end
 assert(session,'No inspection session is active')
 armTimeout()
 assert(index>=1 and index<=session.count,'Invalid inspection index')
 if session.target then
  game:GetService('RunService').Heartbeat:Wait(); game:GetService('RunService').Heartbeat:Wait()
  return {label=session.target.Name}
 end
 if session.scene then
  if cameraFrame then
   session.camera.CFrame=CFrame.new(table.unpack(cameraFrame))
   session.camera.Focus=session.camera.CFrame*CFrame.new(0,0,-20)
  end
  game:GetService('RunService').Heartbeat:Wait(); game:GetService('RunService').Heartbeat:Wait()
  return {label='Game'}
 end
 local entry=session.entries[session.contactSheet and index or 1]
 if session.active~=entry.object then
  session.active=entry.object
  for _,instance in workspace:GetDescendants() do session.hide(instance) end
 end
 local view=views[session.contactSheet and 7 or index]
 local direction=session.direction or view[2].Unit
 local frame=fit(session.camera,entry,direction)
 session.camera.CFrame=frame; session.camera.Focus=CFrame.new(entry.center)
 game:GetService('RunService').Heartbeat:Wait(); game:GetService('RunService').Heartbeat:Wait()
 assert((session.camera.CFrame.Position-frame.Position).Magnitude<.01 and session.camera.CFrame.LookVector:Dot(frame.LookVector)>.999,'Studio camera overridden')
 return {label=session.contactSheet and entry.label or session.direction and angle or view[1]}
end
return function(...)
 local previous=session
 local ok,result=pcall(run,...)
 if not ok then
  -- Restore a failed replacement session without disturbing a rejected begin.
  if session and (session~=previous or select(2,...)~='begin') then
   local restored,err=pcall(restore)
   if not restored then result=tostring(result)..'; '..tostring(err) end
  end
  error(result,0)
 end
 return result
end
