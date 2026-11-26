"""CarbonHotkeys: the HotkeyPort over Carbon's RegisterEventHotKey (through ctypes).

No Accessibility or Input Monitoring permission is needed, the key press is consumed (the
focused app never sees it), and a chord another application registered fails with
`eventHotKeyExistsErr`, which is reported as a conflict. The events arrive through the
application event target while NSApplication runs, i.e. on the main thread.
"""
from __future__ import annotations

import ctypes
import ctypes.util
from collections.abc import Callable
from dataclasses import dataclass

from glance.log import log_event
from glance.platform.macos.hotkeys_mac import carbon_chord, mac_chord

_carbon = ctypes.cdll.LoadLibrary(ctypes.util.find_library("Carbon"))


def _fourcc(s: str) -> int:
    return int.from_bytes(s.encode("ascii"), "big")


class EventHotKeyID(ctypes.Structure):
    _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]


class EventTypeSpec(ctypes.Structure):
    _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]


_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
_carbon.GetApplicationEventTarget.restype = ctypes.c_void_p
_carbon.RegisterEventHotKey.argtypes = [ctypes.c_uint32, ctypes.c_uint32, EventHotKeyID,
                                        ctypes.c_void_p, ctypes.c_uint32,
                                        ctypes.POINTER(ctypes.c_void_p)]
_carbon.RegisterEventHotKey.restype = ctypes.c_int32
_carbon.UnregisterEventHotKey.argtypes = [ctypes.c_void_p]
_carbon.UnregisterEventHotKey.restype = ctypes.c_int32
_carbon.InstallEventHandler.argtypes = [ctypes.c_void_p, _HANDLER, ctypes.c_uint32,
                                        ctypes.POINTER(EventTypeSpec), ctypes.c_void_p,
                                        ctypes.POINTER(ctypes.c_void_p)]
_carbon.InstallEventHandler.restype = ctypes.c_int32
_carbon.GetEventParameter.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                      ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
                                      ctypes.c_void_p]
_carbon.GetEventParameter.restype = ctypes.c_int32

SIGNATURE = _fourcc("GLNC")
EXISTS_ERR = -9878  # eventHotKeyExistsErr
_K_EVENT_CLASS_KEYBOARD = _fourcc("keyb")
_K_EVENT_HOTKEY_PRESSED = 5
_K_PARAM_DIRECT = _fourcc("----")
_TYPE_HOTKEY_ID = _fourcc("hkid")


@dataclass
class _Grab:
    chord: str
    ref: ctypes.c_void_p
    key: tuple[int, int]
    cb: Callable[[], None]


class CarbonHotkeys:
    """HotkeyPort. `post` hands a callback to the main loop."""

    def __init__(self, post: Callable[[Callable[[], None]], None]) -> None:
        self._post = post
        self._grabs: dict[str, _Grab] = {}
        self._by_id: dict[int, str] = {}
        self._next = 1
        self._handler = _HANDLER(self._on_event)  # kept alive: Carbon holds the C pointer
        self._handler_ref = ctypes.c_void_p()
        spec = EventTypeSpec(_K_EVENT_CLASS_KEYBOARD, _K_EVENT_HOTKEY_PRESSED)
        err = _carbon.InstallEventHandler(_carbon.GetApplicationEventTarget(), self._handler, 1,
                                          ctypes.byref(spec), None,
                                          ctypes.byref(self._handler_ref))
        if err != 0:
            log_event("hotkeys_handler_failed", err=int(err))

    def grab(self, name: str, chord: str, cb: Callable[[], None]) -> bool:
        code, mods = carbon_chord(mac_chord(chord))
        self.ungrab(name)
        if any(g.key == (code, mods) for g in self._grabs.values()):
            log_event("hotkey_conflict", name=name, holder="glance")
            return False
        ident = self._next
        self._next += 1
        ref = ctypes.c_void_p()
        err = _carbon.RegisterEventHotKey(code, mods, EventHotKeyID(SIGNATURE, ident),
                                          _carbon.GetApplicationEventTarget(), 0,
                                          ctypes.byref(ref))
        if err != 0:
            log_event("hotkey_conflict", name=name,
                      holder="other" if err == EXISTS_ERR else "error")
            return False
        self._grabs[name] = _Grab(chord, ref, (code, mods), cb)
        self._by_id[ident] = name
        return True

    def ungrab(self, name: str) -> None:
        g = self._grabs.pop(name, None)
        if g is None:
            return
        _carbon.UnregisterEventHotKey(g.ref)
        for ident, n in list(self._by_id.items()):
            if n == name:
                del self._by_id[ident]

    def grabbed(self) -> dict[str, str]:
        return {name: g.chord for name, g in self._grabs.items()}

    def close(self) -> None:
        for name in list(self._grabs):
            self.ungrab(name)

    def _on_event(self, _call_ref: int, event: int, _user: int) -> int:
        hk = EventHotKeyID()
        err = _carbon.GetEventParameter(event, _K_PARAM_DIRECT, _TYPE_HOTKEY_ID, None,
                                        ctypes.sizeof(hk), None, ctypes.byref(hk))
        if err == 0 and hk.signature == SIGNATURE:
            name = self._by_id.get(hk.id)
            g = self._grabs.get(name) if name is not None else None
            if g is not None:
                self._post(g.cb)
        return 0
