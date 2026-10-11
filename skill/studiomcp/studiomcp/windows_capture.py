"""Capture the visible native Studio viewport, explicitly not the proxy renderer."""
import ctypes
import ntpath
import socket
from ctypes import wintypes as w
from contextlib import contextmanager
from io import BytesIO
from PIL import ImageGrab


class NativeWindowNotResponsive(RuntimeError):
    """The matched native Studio window cannot safely be focused or captured."""


class _TcpOwnerRow(ctypes.Structure):
    _fields_=[(name,ctypes.c_uint32) for name in
              ('state','localAddress','localPort','remoteAddress','remotePort','processId')]


class _TcpOwnerTable(ctypes.Structure):
    _fields_=[('count',ctypes.c_uint32),('rows',_TcpOwnerRow*1)]


def _tcp_owner_rows():
    """Read the documented IPv4 TCP_TABLE_OWNER_PID_ALL table."""
    api=ctypes.WinDLL('iphlpapi',use_last_error=True)
    api.GetExtendedTcpTable.argtypes=[ctypes.c_void_p,ctypes.POINTER(w.DWORD),w.BOOL,w.ULONG,ctypes.c_int,w.ULONG]
    api.GetExtendedTcpTable.restype=w.DWORD
    size=w.DWORD()
    status=api.GetExtendedTcpTable(None,ctypes.byref(size),False,socket.AF_INET,5,0)
    if status!=122:raise OSError(status,'Cannot size native TCP connection owners')
    buffer=ctypes.create_string_buffer(size.value)
    capacity=size.value
    status=api.GetExtendedTcpTable(buffer,ctypes.byref(size),False,socket.AF_INET,5,0)
    if status:raise OSError(status,'Cannot read native TCP connection owners')
    if size.value<_TcpOwnerTable.rows.offset:raise RuntimeError('Invalid native TCP owner table')
    count=ctypes.c_uint32.from_buffer(buffer).value
    if _TcpOwnerTable.rows.offset+count*ctypes.sizeof(_TcpOwnerRow)>capacity:
        raise RuntimeError('Truncated native TCP owner table')
    return (_TcpOwnerRow*count).from_buffer_copy(buffer,_TcpOwnerTable.rows.offset)


def _connection_process_id(peer,local,rows):
    """Match the client half of this accepted socket, not another Studio socket."""
    if peer[0]!='127.0.0.1' or local[0]!='127.0.0.1':
        raise RuntimeError('Native receiver provenance requires an IPv4 loopback socket')
    peer_address=int.from_bytes(socket.inet_aton(peer[0]),'little')
    local_address=int.from_bytes(socket.inet_aton(local[0]),'little')
    matches=[row for row in rows if row.state==5 and row.localAddress==peer_address
             and socket.ntohs(row.localPort & 0xffff)==peer[1]
             and row.remoteAddress==local_address and socket.ntohs(row.remotePort & 0xffff)==local[1]
             and row.processId>0]
    if len(matches)!=1:
        raise RuntimeError('Native receiver socket has no unique established client process owner')
    return matches[0].processId


def _studio_process_identity(process_id):
    """Verify the executable and lifetime; a reused PID must never inherit a receiver."""
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];kernel.OpenProcess.restype=w.HANDLE
    kernel.CloseHandle.argtypes=[w.HANDLE];kernel.CloseHandle.restype=w.BOOL
    kernel.QueryFullProcessImageNameW.argtypes=[w.HANDLE,w.DWORD,w.LPWSTR,ctypes.POINTER(w.DWORD)]
    kernel.QueryFullProcessImageNameW.restype=w.BOOL
    kernel.GetProcessTimes.argtypes=[w.HANDLE,*([ctypes.POINTER(w.FILETIME)]*4)]
    kernel.GetProcessTimes.restype=w.BOOL
    handle=kernel.OpenProcess(0x1000,False,process_id)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:raise OSError(ctypes.get_last_error(),'Cannot inspect native receiver process')
    try:
        path=ctypes.create_unicode_buffer(32768);size=w.DWORD(len(path))
        if not kernel.QueryFullProcessImageNameW(handle,0,path,ctypes.byref(size)):
            raise OSError(ctypes.get_last_error(),'Cannot read native receiver executable')
        if ntpath.basename(path.value).casefold()!='robloxstudiobeta.exe':
            raise RuntimeError('Native receiver socket owner is not RobloxStudioBeta.exe')
        created=w.FILETIME();exited=w.FILETIME();system=w.FILETIME();user=w.FILETIME()
        if not kernel.GetProcessTimes(handle,ctypes.byref(created),ctypes.byref(exited),ctypes.byref(system),ctypes.byref(user)):
            raise OSError(ctypes.get_last_error(),'Cannot read native receiver process creation time')
        return {'processId':process_id,'creationTime':str((created.dwHighDateTime<<32)|created.dwLowDateTime),
                'executable':path.value}
    finally:
        kernel.CloseHandle(handle)


def receiver_process_identity(connection):
    """Server-only provenance captured while the registration socket is still live."""
    peer=connection.getpeername();local=connection.getsockname()
    process_id=_connection_process_id(peer,local,_tcp_owner_rows())
    return _studio_process_identity(process_id)


def _select_studio_window(record,candidates):
    identity=record.get('nativeProcess')
    if not isinstance(identity,dict) or not identity.get('processId'):return None
    matches=[item for item in candidates if item['visible'] and item['className']!='Ghost'
             and item['processId']==identity['processId']]
    return matches[0] if len(matches)==1 else None


def focus(record):
    identity=record.get('nativeProcess')
    if not isinstance(identity,dict) or not identity.get('processId'):
        raise RuntimeError('Native Studio window requires server-observed receiver process provenance; '
                           f'receiverName={record.get("name")!r}, placeId={record.get("placeId")!r}, '
                           f'provenanceError={record.get("nativeIdentityError")!r}; '
                           'wait for a genuine Studio registration heartbeat; no title or launch-place fallback is allowed')
    current_identity=_studio_process_identity(identity['processId'])
    if current_identity!=identity:
        raise RuntimeError('Native receiver process identity changed; refusing a stale or reused PID')
    user=ctypes.WinDLL('user32',use_last_error=True)
    user.SetProcessDPIAware()
    user.GetForegroundWindow.restype=w.HWND
    user.SetForegroundWindow.argtypes=[w.HWND]
    user.GetWindowRect.argtypes=[w.HWND,ctypes.POINTER(w.RECT)]
    user.IsChild.argtypes=[w.HWND,w.HWND]
    user.GetDpiForWindow.argtypes=[w.HWND]
    user.GetClassNameW.argtypes=[w.HWND,w.LPWSTR,ctypes.c_int]
    user.GetWindowThreadProcessId.argtypes=[w.HWND,ctypes.POINTER(w.DWORD)]
    user.IsHungAppWindow.argtypes=[w.HWND]
    user.IsWindowEnabled.argtypes=[w.HWND]
    user.GetLastActivePopup.argtypes=[w.HWND];user.GetLastActivePopup.restype=w.HWND
    callback=ctypes.WINFUNCTYPE(w.BOOL,w.HWND,w.LPARAM)
    candidates=[]
    name=record.get('name','').strip()
    def collect(hwnd,_):
        text=ctypes.create_unicode_buffer(512);user.GetWindowTextW(hwnd,text,512)
        visible=bool(user.IsWindowVisible(hwnd))
        if 'Roblox Studio' in text.value:
            matches=name in text.value
            class_name=ctypes.create_unicode_buffer(256);user.GetClassNameW(hwnd,class_name,256)
            process_id=w.DWORD();user.GetWindowThreadProcessId(hwnd,ctypes.byref(process_id))
            candidates.append({'hwnd':hwnd,'title':text.value,'visible':visible,'matchesReceiverName':matches,
                               'className':class_name.value,'processId':process_id.value,
                               'matchesReceiverProcess':process_id.value==identity['processId'],
                               'hung':bool(user.IsHungAppWindow(hwnd))})
        return True
    user.EnumWindows(callback(collect),0)
    selected=_select_studio_window(record,candidates)
    visible=[item for item in candidates if item['visible'] and item['className']!='Ghost']
    matching=[item for item in visible if item['matchesReceiverName']]
    process_matching=[item for item in visible if item['matchesReceiverProcess']]
    if selected is None:raise RuntimeError('Capture requires one visible Studio document window in the receiver process; '
                                         f'receiverName={name!r}, placeId={record.get("placeId")!r}, nativeProcess={identity!r}, '
                                         f'visibleStudioCount={len(visible)}, matchingVisibleCount={len(matching)}, '
                                         f'matchingProcessCount={len(process_matching)}, candidates={candidates}')
    if selected['hung']:raise NativeWindowNotResponsive('Studio native window is Not Responding; refusing visible capture')
    hwnd=selected['hwnd']
    if not user.IsWindowEnabled(hwnd):
        popup=user.GetLastActivePopup(hwnd)
        title=ctypes.create_unicode_buffer(512)
        if popup:user.GetWindowTextW(popup,title,512)
        raise RuntimeError('Studio document window is disabled for native input; '
                           f'window={hwnd}, lastActivePopup={popup}, popupTitle={title.value!r}; '
                           'resolve the blocking Studio dialog before retrying; no activation or input was sent')
    foreground=user.GetForegroundWindow()
    current=ctypes.windll.kernel32.GetCurrentThreadId()
    threads=set(user.GetWindowThreadProcessId(handle,None) for handle in (foreground,hwnd) if handle)
    attached=[]
    try:
        for thread in threads:
            if thread!=current and user.AttachThreadInput(current,thread,True):attached.append(thread)
        user.ShowWindow(hwnd,9);user.SetForegroundWindow(hwnd)
        if user.GetForegroundWindow()!=hwnd:
            # Windows locks foreground changes after another process receives
            # input. A native Alt tap releases that lock without clicking GUI.
            user.keybd_event(0x12,0,0,0)
            user.keybd_event(0x12,0,2,0)
            user.SetForegroundWindow(hwnd)
        if user.GetForegroundWindow()!=hwnd:raise RuntimeError('Windows refused to foreground Studio; visible capture would be occluded')
    finally:
        for thread in attached:user.AttachThreadInput(current,thread,False)
    return hwnd


def viewport_dimensions(environment, studio, play_only=False):
    """Read native screen metadata; camera size omits simulated safe areas."""
    reply=environment.request(studio,'execute',datamodel='Edit',context='server',timeout=2,wait_timeout=3,code='local s=game:GetService("StudioDeviceSimulatorService");local id=s:GetDeviceAsync();if id=="default" then return false end;local r=s:GetResolutionAsync();if s:GetOrientationAsync()==Enum.ScreenOrientation.Portrait then return {r.Y,r.X} end;return {r.X,r.Y}')
    if not reply.get('success') or not reply.get('results'):
        raise RuntimeError('Cannot read native simulator dimensions; refusing to guess a Studio panel: '+str(reply.get('message') or reply))
    simulator=reply['results'][0] or None
    if isinstance(simulator,dict):simulator=[simulator['1'],simulator['2']]
    code='local c=assert(workspace.CurrentCamera,"Native viewport camera unavailable");return c.ViewportSize.X,c.ViewportSize.Y'
    try:
        reply=environment.request(studio,'execute',datamodel='Play',timeout=2,wait_timeout=3,code=code)
        play=True
    except ValueError as error:
        if play_only or 'No live Play client receiver' not in str(error):raise
        reply=environment.request(studio,'execute',datamodel='Edit',context='server',timeout=2,wait_timeout=3,code=code)
        play=False
    if not reply.get('success') or len(reply.get('results',[]))!=2:
        raise RuntimeError('Cannot read current native camera dimensions; refusing to guess a Studio panel: '+str(reply.get('message') or reply))
    return reply['results'],simulator,play


def _client_bounds(user, handle):
    rect=w.RECT()
    if not user.GetClientRect(handle,ctypes.byref(rect)):
        raise RuntimeError('Cannot read Studio native client bounds')
    first=w.POINT(rect.left,rect.top);last=w.POINT(rect.right,rect.bottom)
    if not user.ClientToScreen(handle,ctypes.byref(first)) or not user.ClientToScreen(handle,ctypes.byref(last)):
        raise RuntimeError('Cannot map Studio native client bounds to screen pixels')
    return first.x,first.y,last.x,last.y


def _surface(record, viewport=None, simulator=None):
    if not viewport or len(viewport)!=2 or any(value<=0 for value in viewport):
        raise RuntimeError('Capture requires current native camera dimensions; refusing to guess a Studio panel')
    hwnd=focus(record)
    user=ctypes.WinDLL('user32',use_last_error=True)
    user.GetClientRect.argtypes=[w.HWND,ctypes.POINTER(w.RECT)]
    user.ClientToScreen.argtypes=[w.HWND,ctypes.POINTER(w.POINT)]
    user.IsChild.argtypes=[w.HWND,w.HWND]
    user.GetParent.argtypes=[w.HWND];user.GetParent.restype=w.HWND
    user.GetDpiForWindow.argtypes=[w.HWND]
    user.WindowFromPoint.argtypes=[w.POINT];user.WindowFromPoint.restype=w.HWND
    callback=ctypes.WINFUNCTYPE(w.BOOL,w.HWND,w.LPARAM)
    children=[];failures=[]
    def child(handle,_):
        try:
            if user.IsWindowVisible(handle):
                text=ctypes.create_unicode_buffer(512);user.GetWindowTextW(handle,text,512)
                bounds=_client_bounds(user,handle)
                if bounds[2]>bounds[0] and bounds[3]>bounds[1]:
                    children.append((handle,text.value,bounds))
        except RuntimeError as error:
            failures.append(error)
            return False
        return True
    user.EnumChildWindows(hwnd,callback(child),0)
    if failures:raise failures[0]
    # Hidden simulator children do not make the ordinary renderer a docking
    # parent. Small fitted phones are valid surfaces, not disposable panels.
    surfaces=[row for row in children if not any(other[0]!=row[0] and user.IsChild(row[0],other[0]) for other in children)]
    if not surfaces:raise RuntimeError('No visible Studio viewport native surface exists')
    if simulator:
        width,height=simulator
        if width<=0 or height<=0:raise RuntimeError('Invalid native simulator screen dimensions')
        # Camera.ViewportSize excludes simulated safe areas. The native screen
        # follows the full oriented resolution, scaled uniformly by Studio;
        # monitor DPI alone cannot describe FitToWindow/physical-size scaling.
        matches=[row for row in surfaces if abs((row[2][2]-row[2][0])-(row[2][3]-row[2][1])*width/height)<=2 and abs((row[2][3]-row[2][1])-(row[2][2]-row[2][0])*height/width)<=2]
        # Studio's fitted phone has coextensive sibling frame/mask HWNDs,
        # not independent renderers. Their exact-bounds parent owns the
        # screen center. Keep separate surfaces ambiguous unless native
        # hit-testing proves this shared render owner; never pick an overlay.
        by_handle={row[0]:row for row in children}
        groups={}
        for row in matches:
            parent=user.GetParent(row[0])
            groups.setdefault((parent,row[2]),[]).append(row)
        matches=[]
        for (parent,bounds),rows in groups.items():
            owner=by_handle.get(parent)
            center=w.POINT(bounds[0]+(bounds[2]-bounds[0])//2,bounds[1]+(bounds[3]-bounds[1])//2)
            if len(rows)>1 and owner and owner[2]==bounds and user.WindowFromPoint(center)==parent:
                matches.append(owner)
            else:
                matches.extend(rows)
    else:
        scale=user.GetDpiForWindow(hwnd)/96
        matches=[row for row in surfaces if abs((row[2][2]-row[2][0])-viewport[0]*scale)<=2 and abs((row[2][3]-row[2][1])-viewport[1]*scale)<=2]
    if len(matches)!=1:
        sizes=[(row[2][2]-row[2][0],row[2][3]-row[2][1]) for row in surfaces]
        raise RuntimeError(f'Visible native viewport dimensions do not uniquely match the selected client (camera={viewport}, simulator={simulator}, surfaces={sizes}); refusing to capture another Studio panel')
    surface=matches[0];bounds=surface[2]
    parent=user.GetParent(surface[0])
    while parent:
        clip=_client_bounds(user,parent)
        if bounds[0]<clip[0] or bounds[1]<clip[1] or bounds[2]>clip[2] or bounds[3]>clip[3]:
            raise RuntimeError('Studio native viewport is clipped by its docking bounds; use native simulator FitToWindow or resize Studio before capturing')
        if parent==hwnd:break
        parent=user.GetParent(parent)
    desktop=(user.GetSystemMetrics(76),user.GetSystemMetrics(77),user.GetSystemMetrics(78),user.GetSystemMetrics(79))
    if bounds[0]<desktop[0] or bounds[1]<desktop[1] or bounds[2]>desktop[0]+desktop[2] or bounds[3]>desktop[1]+desktop[3]:
        raise RuntimeError('Studio native viewport extends off screen; refusing an incomplete visible capture')
    return user,surface


def _focus_surface(user, handle):
    user.GetWindowThreadProcessId.argtypes=[w.HWND,ctypes.POINTER(w.DWORD)]
    user.SetFocus.argtypes=[w.HWND]
    user.GetFocus.restype=w.HWND
    user.AttachThreadInput.argtypes=[w.DWORD,w.DWORD,w.BOOL]
    thread=user.GetWindowThreadProcessId(handle,None)
    current=ctypes.windll.kernel32.GetCurrentThreadId()
    attached=False
    if thread!=current:
        if not user.AttachThreadInput(current,thread,True):
            raise RuntimeError('Windows refused to attach Studio viewport input thread')
        attached=True
    try:
        user.SetFocus(handle)
        if user.GetFocus()!=handle:
            raise RuntimeError('Windows refused to focus Studio native viewport')
    finally:
        if attached:user.AttachThreadInput(current,thread,False)


def focus_viewport(record, viewport=None, simulator=None):
    """Resolve and focus the same provenance-bound surface used for capture."""
    user,surface=_surface(record,viewport,simulator)
    _focus_surface(user,surface[0])
    return user,surface[2]


@contextmanager
def awake_viewport(record, viewport=None, simulator=None):
    user,bounds=focus_viewport(record,viewport,simulator)
    # Foreground alone can leave Studio's renderer asleep. Keep the cursor
    # over its native surface through the fresh-frame probe and capture,
    # then restore it even if either fails; send no clicks or held buttons.
    cursor=w.POINT()
    user.GetCursorPos(ctypes.byref(cursor))
    import time
    try:
        user.SetCursorPos(bounds[0]+(bounds[2]-bounds[0])//2,bounds[1]+(bounds[3]-bounds[1])//2)
        for _ in range(8):
            user.mouse_event(1,1,0,0,0)
            time.sleep(.05)
        yield bounds
    finally:
        user.SetCursorPos(cursor.x,cursor.y)


def capture(record, viewport=None, simulator=None):
    with awake_viewport(record,viewport,simulator) as bounds:
        image=ImageGrab.grab(bbox=bounds,all_screens=True)
    out=BytesIO();image.convert('RGB').save(out,'PNG')
    return out.getvalue()


def capture_bounded(record, timeout=4, viewport=None, simulator=None):
    import json
    import subprocess
    import sys
    try:
        completed=subprocess.run([sys.executable,'-m','studiomcp.windows_capture',json.dumps({'record':record,'viewport':viewport,'simulator':simulator})],capture_output=True,timeout=timeout)
    except subprocess.TimeoutExpired:
        raise TimeoutError(f'Windows viewport capture exceeded {timeout}s; native window capture worker was terminated (Studio receiver/status were not involved)') from None
    if completed.returncode:
        raise RuntimeError(completed.stderr.decode('utf-8',errors='replace').strip())
    return completed.stdout


if __name__=='__main__':
    import json
    import sys
    try:
        args=json.loads(sys.argv[1])
        sys.stdout.buffer.write(capture(args['record'],args['viewport'],args['simulator']))
    except Exception as error:
        print(str(error),file=sys.stderr)
        sys.exit(1)
