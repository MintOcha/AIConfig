"""Visible Studio Play input through Windows, not plugin virtual input."""
import ctypes
import time
from ctypes import wintypes as w


def _sampled_click(environment,studio,user,button,name):
    """Release only our press, after a real client render samples its held state."""
    user.GetAsyncKeyState.argtypes=[ctypes.c_int];user.GetAsyncKeyState.restype=w.SHORT
    swapped=bool(user.GetSystemMetrics(23))  # SM_SWAPBUTTON: GetAsyncKeyState is physical.
    physical_left=(name=='left')!=swapped
    if user.GetAsyncKeyState(1 if physical_left else 2) & 0x8000:
        raise RuntimeError(f'Cannot click native {name} mouse button while it is already held; no input was sent or released')
    input_type={'left':'MouseButton1','right':'MouseButton2'}[name]
    button(name,True)
    try:
        reply=environment.request(studio,'execute',datamodel='Play',timeout=2,wait_timeout=3,code=
            'local input=game:GetService("UserInputService");local sampled=false;'
            'local connection=game:GetService("RunService").RenderStepped:Connect(function() '
            f'if input:IsMouseButtonPressed(Enum.UserInputType.{input_type}) then sampled=true end end);'
            'local deadline=os.clock()+1.8;repeat task.wait(.02) until sampled or os.clock()>=deadline;'
            'connection:Disconnect();return sampled')
        if not reply.get('success'):
            raise RuntimeError(reply.get('message') or reply.get('error') or 'Native mouse click client sampling failed')
        if reply.get('results')!=[True]:
            raise RuntimeError('No fresh client render sampled the injected mouse button within 2s; '
                               'owned button was released without retrying the click')
    finally:
        button(name,False)


def _input_bounds(environment, studio):
    """Read the full screen rectangle in UIS coordinates, including safe margins."""
    reply=environment.request(studio,'execute',datamodel='Play',timeout=2,wait_timeout=3,code=
        'local g=game:GetService("GuiService");local r=g:GetInsetArea(Enum.ScreenInsets.None);'
        'local inset=g:GetGuiInset();return r.Min.X+inset.X,r.Min.Y+inset.Y,'
        'r.Max.X+inset.X,r.Max.Y+inset.Y')
    if not reply.get('success'):
        raise RuntimeError(reply.get('message') or reply.get('error') or 'Cannot read native input screen bounds')
    bounds=reply.get('results',[])
    if len(bounds)!=4 or bounds[2]<=bounds[0] or bounds[3]<=bounds[1]:
        raise RuntimeError('Invalid native input screen bounds; refusing to guess cursor coordinates')
    return bounds


def _screen_point(bounds, logical_bounds, x, y):
    """Normalize the measured UIS screen rectangle into native capture pixels."""
    left,top,right,bottom=logical_bounds
    return (bounds[0]+int((x-left)*(bounds[2]-bounds[0])/(right-left)),
            bounds[1]+int((y-top)*(bounds[3]-bounds[1])/(bottom-top)))


def send(environment, studio, operation, actions):
    from .windows_capture import focus_viewport, viewport_dimensions
    studio=environment.resolve_studio(studio)
    receiver=environment.studios[studio]
    user=ctypes.WinDLL('user32',use_last_error=True)
    user.SetCursorPos.argtypes=[ctypes.c_int,ctypes.c_int]
    user.SetCursorPos.restype=w.BOOL
    class Mouse(ctypes.Structure):
        _fields_ = [('dx', w.LONG), ('dy', w.LONG), ('mouseData', w.DWORD), ('dwFlags', w.DWORD), ('time', w.DWORD), ('dwExtraInfo', ctypes.c_size_t)]
    class Keyboard(ctypes.Structure):
        _fields_ = [('wVk', w.WORD), ('wScan', w.WORD), ('dwFlags', w.DWORD), ('time', w.DWORD), ('dwExtraInfo', ctypes.c_size_t)]
    class Union(ctypes.Union):
        _fields_ = [('mi', Mouse), ('ki', Keyboard)]
    class Input(ctypes.Structure):
        _fields_ = [('type', w.DWORD), ('u', Union)]
    def inject(value):
        if user.SendInput(1, ctypes.byref(value), ctypes.sizeof(Input)) != 1:
            raise ctypes.WinError(ctypes.get_last_error())
    def button(name, down):
        flag = {'left': (2,4), 'right': (8,16)}[name][not down]
        inject(Input(0, Union(mi=Mouse(0,0,0,flag,0,0))))
    # Resolve the actual native surface again after waits or window changes.
    keys = {'Space':32, 'Return':13, 'Escape':27, 'Tab':9, 'Backspace':8, 'LeftShift':160, 'RightShift':161, 'LeftControl':162, 'RightControl':163, 'Up':38, 'Down':40, 'Left':37, 'Right':39}
    names = {'Zero':'0','One':'1','Two':'2','Three':'3','Four':'4','Five':'5','Six':'6','Seven':'7','Eight':'8','Nine':'9'}
    sent = 0
    for step in actions:
        action = step['action']
        if action == 'wait':
            time.sleep(step['waitTimeMs']/1000); continue
        viewport,simulator,_=viewport_dimensions(environment,studio,play_only=True)
        _,bounds=focus_viewport(receiver,viewport,simulator)
        if sent==0:time.sleep(.1)
        if operation == 'mouse':
            x,y = step.get('x'),step.get('y')
            if step.get('instancePath'):
                import json
                path = json.dumps(step['instancePath'])
                response = environment.request(studio,'execute',datamodel='Play',timeout=5,code=f'local p={path}; local o=game:GetService("Players").LocalPlayer; for n in p:gsub("^LocalPlayer[./]",""):gmatch("[^./]+") do o=assert(o:FindFirstChild(n),"Missing mouse target: "..p) end; assert(o:IsA("GuiObject"),"Mouse target must be GuiObject: "..p); local inset=game:GetService("GuiService"):GetGuiInset(); return o.AbsolutePosition.X+o.AbsoluteSize.X/2+inset.X,o.AbsolutePosition.Y+o.AbsoluteSize.Y/2+inset.Y')
                if not response.get('success'):
                    raise RuntimeError(response.get('message') or response.get('error') or 'Mouse target execution failed')
                x,y=response['results']
            if x is not None and y is not None:
                point=_screen_point(bounds,_input_bounds(environment,studio),x,y)
                if not user.SetCursorPos(*point):
                    raise ctypes.WinError(ctypes.get_last_error())
            if action.startswith('mouseButton'):
                name=step['mouseButton']
                if action=='mouseButtonClick':_sampled_click(environment,studio,user,button,name)
                else:
                    if action!='mouseButtonUp': button(name,True)
                    if action!='mouseButtonDown': button(name,False)
            elif action in ('scrollUp','scrollDown'):
                inject(Input(0,Union(mi=Mouse(0,0,120 if action=='scrollUp' else 0xffffff88,0x800,0,0))))
            elif action!='moveTo': raise ValueError('Unsupported mouse action: '+action)
        else:
            if action=='textInput':
                for character in step['textInputs']:
                    for flags in (4,6): inject(Input(1,Union(ki=Keyboard(0,ord(character),flags,0,0))))
            else:
                key=step['keyCode']; key=names.get(key,key)
                vk=ord(key) if len(key)==1 else keys.get(key)
                if vk is None: raise ValueError('Unsupported Windows key: '+key)
                if action!='keyUp': inject(Input(1,Union(ki=Keyboard(vk,0,0,0,0))))
                if action!='keyDown': inject(Input(1,Union(ki=Keyboard(vk,0,2,0,0))))
        sent += 1
        time.sleep(.03)
    return {'sent':sent, 'transport':'WindowsSendInput'}
