"""A minimal EWMH window manager for the private Xvfb server the tests run on.

    mini_wm.py [DEAF_FLAG]    (uses $DISPLAY; prints READY when it owns the root window)

It does only what Glance looks at: `_NET_CLIENT_LIST`, `_NET_ACTIVE_WINDOW` (property and
client message) and the input focus. A newly mapped window is activated, as a real window
manager would for a window the user just opened. When the active window goes away nothing is
activated in its place: whoever wants the previous window back has to ask for it, which is
what the "Rewrite as…" entry is tested for. While the file DEAF_FLAG exists, requests to
activate a window are ignored, as a window manager that refuses them would.
Never point it at a display somebody is using.
"""
import os
import signal
import sys

from Xlib import X, Xatom, display, error


def main() -> int:
    deaf_flag = sys.argv[1] if len(sys.argv) > 1 else None
    d = display.Display()
    root = d.screen().root
    atom = d.intern_atom
    net_active, net_clients = atom("_NET_ACTIVE_WINDOW"), atom("_NET_CLIENT_LIST")
    net_stacking, net_check = atom("_NET_CLIENT_LIST_STACKING"), atom("_NET_SUPPORTING_WM_CHECK")
    wm_state = atom("WM_STATE")

    root.change_attributes(event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
    d.sync()  # a BadAccess here means another window manager is running
    check = root.create_window(-10, -10, 1, 1, 0, X.CopyFromParent)
    for win in (root, check):
        win.change_property(net_check, Xatom.WINDOW, 32, [check.id])
    check.change_property(atom("_NET_WM_NAME"), atom("UTF8_STRING"), 8, b"glance-test-wm")
    root.change_property(atom("_NET_SUPPORTED"), Xatom.ATOM, 32,
                         [net_active, net_clients, net_stacking, net_check])
    clients: list[int] = []
    active = 0

    def publish() -> None:
        for name in (net_clients, net_stacking):
            root.change_property(name, Xatom.WINDOW, 32, clients)
        root.change_property(net_active, Xatom.WINDOW, 32, [active])
        d.flush()

    def activate(xid: int) -> None:
        nonlocal active
        if xid not in clients:
            return
        clients.remove(xid)
        clients.append(xid)  # top of the stack
        win = d.create_resource_object("window", xid)
        try:
            win.configure(stack_mode=X.Above)
            win.set_input_focus(X.RevertToPointerRoot, X.CurrentTime)
            d.sync()
        except error.XError:
            return
        active = xid
        publish()

    def forget(xid: int) -> None:
        nonlocal active
        if xid in clients:
            clients.remove(xid)
            if active == xid:
                active = 0
                root.set_input_focus(X.RevertToPointerRoot, X.CurrentTime)
            publish()

    publish()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    sys.stdout.write("READY\n")
    sys.stdout.flush()
    while True:
        e = d.next_event()
        try:
            if e.type == X.MapRequest:
                e.window.map()
                e.window.change_property(wm_state, wm_state, 32, [1, 0])  # NormalState
                if e.window.id not in clients:
                    clients.append(e.window.id)
                activate(e.window.id)
            elif e.type == X.ConfigureRequest:
                keys = {"x": X.CWX, "y": X.CWY, "width": X.CWWidth, "height": X.CWHeight,
                        "border_width": X.CWBorderWidth}
                e.window.configure(**{k: getattr(e, k) for k, bit in keys.items()
                                      if e.value_mask & bit})
            elif e.type in (X.UnmapNotify, X.DestroyNotify):
                forget(e.window.id)
            elif (e.type == X.ClientMessage and e.client_type == net_active
                  and not (deaf_flag and os.path.exists(deaf_flag))):
                activate(e.window.id)
            d.flush()
        except error.XError:
            pass  # the window went away mid-request


if __name__ == "__main__":
    sys.exit(main())
