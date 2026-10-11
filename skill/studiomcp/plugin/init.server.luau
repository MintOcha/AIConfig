if not plugin then
    return
end

-- Play clients cannot use HttpService; the Play server's ClientRelay serves client commands.
local run = game:GetService("RunService")
if run:IsRunning() and run:IsClient() and not run:IsServer() then
    return
end

local bridge = require(script.Bridge).start(nil, true)
local toolbar = plugin:CreateToolbar("StudioMCP")
local button = toolbar:CreateButton("StudioMCP", "Show StudioMCP connection information", "", "StudioMCP")
button.Click:Connect(function()
    print("StudioMCP receiver connects to http://127.0.0.1:23456. Use StudioMCP CLI or MCP tools for execution, playtest, view, and output.")
end)
plugin.Unloading:Connect(function()
    bridge.stop()
end)
