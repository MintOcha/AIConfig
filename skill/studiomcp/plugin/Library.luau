-- Public assets are loaded unparented, inspected, and isolated without running scripts.
return function(command)
 assert(not game:GetService("RunService"):IsRunning(), "Library additions require Studio Edit mode")
 local assetId=tonumber(command.assetId)
 assert(assetId and assetId>0 and assetId%1==0, "assetId must be a positive integer")
 local info=game:GetService("MarketplaceService"):GetProductInfo(assetId,Enum.InfoType.Asset)
 local assetType
 for _,item in Enum.AssetType:GetEnumItems() do
  if item.Value==info.AssetTypeId then assetType=item.Name; break end
 end
 assert(assetType, "Unknown native AssetType")
 assert(assetType~="Plugin", "Plugin discovery only: installation and execution are not supported")
 -- GetObjects is the documented plugin authoring loader; runtime loading uses experience permissions.
 local objects=game:GetObjects("rbxassetid://"..tostring(assetId))
 assert(#objects>0, "Asset contains no insertable instances")
 local loaded=Instance.new("Model")
 local scripts,geometry,textures={},{},{}
 local ok,result=pcall(function()
  for _,object in objects do
   assert(object.Parent==nil, "Asset loader returned a parented instance")
   local nodes={object}
   for _,node in object:GetDescendants() do table.insert(nodes,node) end
   for _,node in nodes do
    if node:IsA("BaseScript") then node.Disabled=true end
    if node:IsA("LuaSourceContainer") then table.insert(scripts,{path=node:GetFullName(),className=node.ClassName,disabled=node:IsA("BaseScript") and node.Disabled or nil}) end
    if node:IsA("BasePart") then table.insert(geometry,{path=node:GetFullName(),className=node.ClassName,size=tostring(node.Size),meshId=node:IsA("MeshPart") and node.MeshId or nil}) end
    if node:IsA("Decal") then table.insert(textures,{name=node.Name,texture=node.Texture}) end
   end
  end
  for _,object in objects do
   object.Parent=loaded
  end
  local storage=game:GetService("ServerStorage")
  local folder=storage:FindFirstChild("StudioMCPAssetDiscovery")
  if not folder then folder=Instance.new("Folder"); folder.Name="StudioMCPAssetDiscovery"; folder.Parent=storage end
  assert(folder:IsA("Folder"),"StudioMCPAssetDiscovery is not a Folder")
  loaded.Name=command.name or info.Name
  loaded:SetAttribute("StudioMCPAssetId",tostring(command.assetId))
  loaded.Parent=folder
  return {assetId=tostring(command.assetId),assetType=assetType,name=loaded.Name,path="game."..loaded:GetFullName(),scripts=scripts,geometry=geometry,textures=textures,isolated=true}
 end)
 if not ok then loaded:Destroy(); for _,object in objects do object:Destroy() end; error(result) end
 return result
end
