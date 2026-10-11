local HttpService = game:GetService("HttpService")
local RunService = game:GetService("RunService")
local Bridge = {}
local active

local function resolve(path)
    local object = game
    if path == "game" then return object end
    path = path:gsub("^game[./]", "")
    for name in path:gmatch("[^/%.]+") do
        object = object:FindFirstChild(name)
        assert(object, 'Missing instance: ' .. path)
    end
    return object
end

local function output()
    local entries = {}
    for _, entry in game:GetService("LogService"):GetLogHistory() do
        local previous = entries[#entries]
        local kind = tostring(entry.messageType)
        if previous and previous.message == entry.message and previous.type == kind then
            previous.count += 1
            previous.lastTimestamp = entry.timestamp
        else
            table.insert(entries, {message = entry.message, type = kind, count = 1, timestamp = entry.timestamp, lastTimestamp = entry.timestamp})
        end
    end
    return {entries = entries}
end

local function coverage(root)
    local report = {instances = 0, classes = {}, properties = {}}
    local objects = {root}
    for _, object in root:GetDescendants() do
        table.insert(objects, object)
    end
    for _, object in objects do
        report.instances += 1
        report.classes[object.ClassName] = (report.classes[object.ClassName] or 0) + 1
    end
    return report
end

function Bridge.start(baseUrl, installed)
    assert(not active, "StudioMCP receiver already running")
    baseUrl = baseUrl or "http://127.0.0.1:23456"
    local running = true
    local studio = HttpService:GenerateGUID(false)
    local identityOk, userId = pcall(function() return game:GetService('StudioService'):GetUserId() end)
    if not identityOk or type(userId) ~= 'number' or userId <= 0 then userId = nil end
    local run = game:GetService('RunService')
    local registration = {
        studio = studio,
        name = game.Name,
        placeId = game.PlaceId,
        userId = userId,
        installed = installed == true,
        protocol = 2,
        datamodel = run:IsRunning() and 'Play' or 'Edit',
        context = run:IsRunning() and (run:IsClient() and 'client' or 'server') or 'edit'
    }

    local function refreshIdentity()
        registration.name = game.Name
        registration.placeId = game.PlaceId
    end
    local nameChanged = game:GetPropertyChangedSignal('Name'):Connect(refreshIdentity)
    local placeChanged = game:GetPropertyChangedSignal('PlaceId'):Connect(refreshIdentity)
    local worker, heartbeat
    local stopRelay
    if run:IsRunning() and run:IsServer() then
        stopRelay = require(script.Parent.ClientRelay).start(baseUrl, registration)
    end

    local controller = {}
    function controller.stop()
        running = false
        active = nil
        nameChanged:Disconnect()
        placeChanged:Disconnect()
        if worker then task.cancel(worker); worker = nil end
        if heartbeat then task.cancel(heartbeat); heartbeat = nil end
        if stopRelay then stopRelay() end
        pcall(function()
            HttpService:PostAsync(baseUrl .. '/unregister', HttpService:JSONEncode({studio = studio}), Enum.HttpContentType.ApplicationJson)
        end)
        local restored, err = pcall(function() require(script.Parent.View)(nil, 'end') end)
        if not restored then warn('StudioMCP preview unload restoration failed: ' .. tostring(err)) end
    end
    active = controller

    heartbeat = task.spawn(function()
        while running do
            pcall(function()
                HttpService:PostAsync(baseUrl .. '/register', HttpService:JSONEncode(registration), Enum.HttpContentType.ApplicationJson)
            end)
            task.wait(10)
        end
    end)

    local disconnected = false
    worker = task.spawn(function()
        while running do
            local ok, err = pcall(function()
                if disconnected then
                    print("StudioMCP reconnected")
                    disconnected = false
                end
                local command = HttpService:JSONDecode(HttpService:GetAsync(baseUrl .. "/commands?studio=" .. studio, true))
                if not running or command.action == "idle" then return end

                local success, result = pcall(function()
                    if command.action == 'view' and command.stage == 'end' then
                        return require(script.Parent.View)(nil, 'end')
                    end
                    if command.action == 'camera' then
                        assert(command.mode == 'reset' or command.mode == 'status', 'Camera mode must be reset or status')
                        local camera = assert(workspace.CurrentCamera, 'No current camera')
                        local isRunning = RunService:IsRunning()
                        if command.mode == 'reset' then
                            assert(not isRunning, 'Camera reset is only available in Edit mode')
                            camera.CameraType = Enum.CameraType.Custom
                        end
                        return {cameraType = camera.CameraType.Name, mode = isRunning and 'Play' or 'Edit'}
                    end
                    if command.action == 'output' then
                        return output()
                    end
                    if command.action == 'coverage' then
                        return coverage(resolve(command.root))
                    end
                    if command.action == 'exportAsset' then
                        local object = resolve(command.path)
                        assert(object:IsA('Model') or object:IsA('BasePart') or object:IsA('KeyframeSequence'), 'Export requires a model, part or animation')
                        local copy = object:Clone()
                        local encodeOk, bytes = pcall(function()
                            for name in pairs(copy:GetAttributes()) do
                                if string.sub(name, 1, 9) == 'StudioMCP' then copy:SetAttribute(name, nil) end
                            end
                            return game:GetService('SerializationService'):SerializeInstancesAsync({copy})
                        end)
                        copy:Destroy()
                        assert(encodeOk, bytes)
                        return {data = buffer.tostring(game:GetService('EncodingService'):Base64Encode(bytes)), name = object.Name, assetType = object:IsA('KeyframeSequence') and 'Animation' or 'Model'}
                    end
                    if command.action == 'libraryAdd' then
                        return require(script.Parent.Library)(command)
                    end
                    if command.action == 'ls' or command.action == 'cat' or command.action == 'find' then
                        return require(script.Parent.Hierarchy)[command.action](command)
                    end
                    if command.action == 'view' then
                        return require(script.Parent.View)(resolve(command.path), command.stage, command.index, command.rig and resolve(command.rig), command.angle, command.samples, command.camera_cframe, command.contact_sheet)
                    end

                    local tools = require(script.Parent.Tools)
                    if tools[command.action] then
                        return tools[command.action](command)
                    end
                    error("Unknown receiver command: " .. tostring(command.action))
                end)

                local data = HttpService:JSONEncode(success and result or {error = tostring(result)})
                for offset = 1, #data, 500000 do
                    HttpService:RequestAsync({
                        Url = baseUrl .. "/result",
                        Method = "POST",
                        Headers = {
                            ['Content-Type'] = 'text/plain',
                            ['X-Request-Id'] = command.request,
                            ['X-Final'] = offset + 500000 > #data and 'true' or 'false'
                        },
                        Body = data:sub(offset, offset + 499999)
                    })
                end
            end)

            if not ok and running then
                if not disconnected then
                    warn("StudioMCP disconnected: " .. tostring(err))
                    disconnected = true
                end
                task.wait(2)
            end
            if not ok then task.wait(0.4) end
        end
    end)
    return controller
end

return Bridge
