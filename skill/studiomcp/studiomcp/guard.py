"""Keep `execute` observational: it must not leave the camera or selection changed.

Before and after a run we read the camera type (and, in Edit, its CFrame and the selection).
Anything that moved is put back and reported in `sideEffects`, unless the caller says the
change was intended (`allow_side_effects`). The Play client camera is script-driven every
frame, so only its CameraType is compared there.
"""
from .profiler import listify


PROBE = r'''
local camera = workspace.CurrentCamera
local state = {}
state.inEdit = not game:GetService("RunService"):IsRunning()
if camera then
	state.cameraType = camera.CameraType.Name
	state.cframe = {camera.CFrame:GetComponents()}
	state.fov = camera.FieldOfView
end
local ok, selection = pcall(function() return game:GetService("Selection"):Get() end)
state.selection = ok and #selection or 0
return state
'''


def differences(before, after, in_edit):
    if not before or not after:
        return []
    found = []
    if before.get('cameraType') != after.get('cameraType'):
        found.append(f"camera type {before.get('cameraType')} -> {after.get('cameraType')}")
    if in_edit:
        before_components = listify(before.get('cframe', []))
        after_components = listify(after.get('cframe', []))
        moved = max((abs(a - b) for a, b in zip(before_components, after_components)), default=0)
        if moved > 1e-3:
            found.append('camera moved')
        if before.get('selection') != after.get('selection'):
            found.append('selection changed')
    return found


def restore_code(before, in_edit):
    parts = ['local camera = workspace.CurrentCamera', 'if camera then',
             f'camera.CameraType = Enum.CameraType.{before["cameraType"]}']
    if in_edit:
        components = ','.join(repr(float(v)) for v in listify(before['cframe']))
        parts.append(f'camera.CFrame = CFrame.new({components})')
        parts.append(f'camera.FieldOfView = {before["fov"]}')
    parts.append('end')
    return '\n'.join(parts)
