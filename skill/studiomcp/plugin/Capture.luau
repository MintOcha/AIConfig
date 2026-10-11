local Capture={}
function Capture.png()
 local image
 local stage='StudioCaptureService'
 local ok,result=pcall(function()
  local service=game:GetService('StudioCaptureService')
  stage='CanCaptureScreenshot'
  if not service:CanCaptureScreenshot() then
   stage='RequestScreenshotPermissionAsync'
   assert(service:RequestScreenshotPermissionAsync(),'Studio screenshot permission denied or globally disabled')
   stage='CanCaptureScreenshot after permission'
   assert(service:CanCaptureScreenshot(),'Active DataModel cannot capture a screenshot')
  end
  stage='viewport'
  local size=assert(workspace.CurrentCamera,'Studio viewport camera unavailable').ViewportSize
  assert(size.X>0 and size.Y>0,'Studio viewport unavailable')
  stage='CaptureScreenshot PNG'
  image=service:CaptureScreenshot({Position=Vector2.zero,CaptureSize=size,OutputSize=size,Format=Enum.StudioCaptureScreenshotFormat.PNG,UICaptureMode=Enum.UICaptureMode.All})
  assert(image,'Studio capture did not return an image')
  stage='screenshot buffer'
  local deadline=os.clock()+10
  while image.BufferStatus~=Enum.StudioCaptureBufferStatus.Ready do
   if image.BufferStatus==Enum.StudioCaptureBufferStatus.Error then
    local errors=table.concat(image:GetErrors(),'; ')
    error(errors~='' and errors or 'Native screenshot buffer failed')
   end
   assert(os.clock()<deadline,'Native screenshot buffer timed out after 10 seconds')
   task.wait()
  end
  stage='GetBuffer'
  local bytes=image:GetBuffer()
  stage='Base64Encode'
  return buffer.tostring(game:GetService('EncodingService'):Base64Encode(bytes))
 end)
 if image then
  local cleaned,cleanupError=pcall(function() image:Destroy() end)
  if not cleaned then
   if ok then error('Native screenshot cleanup failed: '..tostring(cleanupError),0) end
   result=tostring(result)..'; cleanup failed: '..tostring(cleanupError)
  end
 end
 if not ok then error('Native screenshot '..stage..' failed: '..tostring(result),0) end
 return result
end
return Capture
