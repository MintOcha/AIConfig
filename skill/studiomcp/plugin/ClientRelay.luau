local Relay={}
local Http=game:GetService('HttpService')
local clientSource=[[
local folder=script.Parent
local token=folder:GetAttribute('OwnerToken')
if not token or token~=script:GetAttribute('OwnerToken') or not game:GetService('RunService'):IsStudio() then return end
local remote=folder.Remote
local logs={}
local logConnection=game:GetService('LogService').MessageOut:Connect(function(message,kind) table.insert(logs,{message=message,type=kind.Name,timestamp=os.clock()}) end)
local commandConnection=remote.OnClientEvent:Connect(function(command)
 if command.token~=token then return end
 local ok,result=pcall(function()
  local tools=require(folder.Tools)
  if tools[command.action] then return tools[command.action](command) end
  if command.action=='ls' or command.action=='cat' or command.action=='find' then return require(folder.Hierarchy)[command.action](command) end
  if command.action=='output' then return {entries=logs} end
  error('Unsupported client action: '..command.action)
 end)
 remote:FireServer(command.request,ok and result or {error=tostring(result)},token)
end)
script.Destroying:Connect(function() commandConnection:Disconnect(); logConnection:Disconnect() end)
local run=game:GetService('RunService')
repeat run.RenderStepped:Wait() until workspace.CurrentCamera and workspace.CurrentCamera.ViewportSize.X>100 and workspace.CurrentCamera.ViewportSize.Y>100
local viewport=workspace.CurrentCamera.ViewportSize
remote:FireServer('ready',{viewport={viewport.X,viewport.Y},renderGameTime=workspace.DistributedGameTime},token)
]]
function Relay.start(baseUrl,registration)
 local run=game:GetService('RunService')
 assert(run:IsStudio() and run:IsRunning() and run:IsServer(),'Client relay requires Studio Play server')
 local token=Http:GenerateGUID(false)
 local folder=Instance.new('Folder'); folder.Name='StudioMCPClientRelay'; folder:SetAttribute('OwnerToken',token)
 for _,name in {'Tools','Hierarchy'} do local copy=script.Parent[name]:Clone(); copy.Parent=folder end
 local remote=Instance.new('RemoteEvent'); remote.Name='Remote'; remote.Parent=folder
 folder.Parent=game.ReplicatedStorage
 local clientRegistration=table.clone(registration); clientRegistration.studio=Http:GenerateGUID(false); clientRegistration.context='client'
 local running=true; local ready=false; local pending={}; local worker,heartbeat
 local function register()
  -- The bridge tracks native DataModel identity changes; the relay retains its
  -- own GUID, client context and render-readiness metadata.
  clientRegistration.name=registration.name; clientRegistration.placeId=registration.placeId
  pcall(function() Http:PostAsync(baseUrl..'/register',Http:JSONEncode(clientRegistration),Enum.HttpContentType.ApplicationJson) end)
 end
 local received=remote.OnServerEvent:Connect(function(player,request,result,owner)
  if owner~=token then return end
  if request=='ready' then
   ready=true; clientRegistration.viewport=result.viewport; clientRegistration.renderReady=true; clientRegistration.renderGameTime=result.renderGameTime
   task.spawn(register)
   return
  end
  local record=pending[request]; if record then record.result=result; record.ready=true end
 end)
 -- Explicit client context survives respawn and temporary PlayerGui reparenting.
 local client=Instance.new('Script'); client.Name='Client'; client.Enabled=false; client.Source=clientSource
 client.RunContext=Enum.RunContext.Client; client:SetAttribute('OwnerToken',token); client.Parent=folder; client.Enabled=true
 -- Command execution can outlast the service's receiver TTL; registration
 -- must keep running independently, just like the plugin bridge heartbeat.
 heartbeat=task.spawn(function()
  while running do
   if ready then register() end
   task.wait(10)
  end
 end)
 worker=task.spawn(function()
  while running do
   local ok,err=pcall(function()
    if not ready then task.wait(.1); return end
    local command=Http:JSONDecode(Http:GetAsync(baseUrl..'/commands?studio='..clientRegistration.studio,true))
    if command.action=='idle' or not running then return end
    command.token=token
    if command.action=='execute' then
     local module=Instance.new('ModuleScript'); module.Name='Execution_'..command.request; module.Source='return function(print,warn)\n'..command.code..'\nend'; module.Parent=folder; command.execution=module
    end
    local record={}; pending[command.request]=record
    remote:FireAllClients(command)
    local deadline=os.clock()+(command.timeout or 60)+2
    repeat task.wait() until record.ready or not running or os.clock()>deadline
    pending[command.request]=nil
    if command.execution then command.execution:Destroy() end
    local data=Http:JSONEncode(record.result or {error='Client relay command timed out'})
    Http:RequestAsync({Url=baseUrl..'/result',Method='POST',Headers={['Content-Type']='text/plain',['X-Request-Id']=command.request,['X-Final']='true'},Body=data})
   end)
   if not ok and running then warn('RoMCP client relay: '..tostring(err)); task.wait(1) end
  end
 end)
 return function()
  if not running then return end
  running=false; received:Disconnect(); if worker then task.cancel(worker) end
  if heartbeat then task.cancel(heartbeat) end
  pcall(function() Http:PostAsync(baseUrl..'/unregister',Http:JSONEncode({studio=clientRegistration.studio}),Enum.HttpContentType.ApplicationJson) end)
  folder:Destroy()
 end
end
return Relay
