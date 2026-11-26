"""The shell extension lets go of every key when glanced goes or stops answering.

Runs the extension's own modules (no GNOME Shell imports) under plain gjs, against a
private dbus-daemon: never the session bus.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _gjs(tmp_path: Path, body: str, env: dict[str, str] | None = None) -> str:
    gjs = shutil.which("gjs")
    if gjs is None:
        pytest.skip("gjs is not installed")
    script = tmp_path / "check.js"
    script.write_text(body.replace("@REPO@", str(REPO)))
    done = subprocess.run([gjs, "-m", str(script)], capture_output=True, text=True,
                          timeout=30, check=False, env={**os.environ, **(env or {})})
    return done.stdout.strip() + done.stderr.strip()


_GUARD = """
import {KeyGuard} from 'file://@REPO@/extension/keyguard.js';

function world() {
    const w = {grabs: new Map([['escape', 'Escape'], ['strip_use', '<Alt>Return']]),
               timers: [], log: []};
    w.guard = new KeyGuard({
        held: () => [...w.grabs],
        drop: n => { w.grabs.delete(n); w.log.push('drop ' + n); },
        take: (n, a) => { w.grabs.set(n, a); w.log.push('take ' + n); },
        after: (ms, fn) => { w.timers.push(fn); return w.timers.length - 1; },
        cancel: id => { w.timers[id] = null; },
    });
    w.fire = () => { for (const t of w.timers) t?.(); w.timers = []; };
    return w;
}
const out = [];

// answered in time: nothing changes
let w = world(); let done;
w.guard.pressed(d => { done = d; }); done(true); w.fire();
out.push(['in time', w.grabs.size === 2 && w.log.length === 0]);

// no answer within the deadline: every transient grab goes, and stays gone on a failure
w = world();
w.guard.pressed(d => { done = d; }); w.fire();
out.push(['deadline drops', w.grabs.size === 0]);
done(false);
out.push(['failure keeps them dropped', w.grabs.size === 0]);

// a late but good answer: glanced was only slow, the grabs come back
w = world();
w.guard.pressed(d => { done = d; }); w.fire(); done(true);
out.push(['late answer restores', w.grabs.size === 2]);

// glanced vanished meanwhile: a late answer must not grab for it again
w = world();
w.guard.pressed(d => { done = d; }); w.fire(); w.guard.gone(); done(true);
out.push(['gone forgets', w.grabs.size === 0]);

// glanced released a key itself meanwhile: not taken back
w = world();
w.guard.pressed(d => { done = d; }); w.fire(); w.guard.asked('escape'); done(true);
out.push(['asked wins', !w.grabs.has('escape') && w.grabs.has('strip_use')]);

// no glanced to ask at all: released at once
w = world();
w.guard.pressed(d => d(false));
out.push(['no daemon', w.grabs.size === 0]);

const bad = out.filter(([, ok]) => !ok).map(([n]) => n);
print(bad.length === 0 ? 'OK' : 'FAILED ' + JSON.stringify(bad));
"""


def test_key_guard_drops_transient_grabs_when_glanced_does_not_answer(tmp_path):
    assert _gjs(tmp_path, _GUARD) == "OK"


_PEERS = """
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import {Peers, ROLES} from 'file://@REPO@/extension/peers.js';

const address = GLib.getenv('GLANCE_TEST_BUS');
const connect = () => Gio.DBusConnection.new_for_address_sync(address,
    Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT |
    Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, null, null);
const shell = connect();
const loop = new GLib.MainLoop(null, false);
const gone = [];
const peers = new Peers(shell, role => gone.push(role));
const spin = (ms, until) => {
    const end = GLib.get_monotonic_time() + ms * 1000;
    while (!until() && GLib.get_monotonic_time() < end)
        GLib.MainContext.default().iteration(false);
};
spin(300, () => false);
const before = gone.length;   // first reports: nobody owns the names
const daemon = connect();
const own = Gio.bus_own_name_on_connection(daemon, ROLES.daemon, Gio.BusNameOwnerFlags.NONE,
    null, null);
spin(2000, () => peers._owner.daemon);
const owned = Boolean(peers._owner.daemon);
daemon.close_sync(null);      // glanced quits or crashes
spin(2000, () => gone.length > before);
const ok = owned && gone.slice(before).includes('daemon') && peers._owner.daemon === null;
peers.destroy();
Gio.bus_unown_name(own);
print(ok ? 'OK' : 'FAILED ' + JSON.stringify({owned, gone, before}));

"""


def test_peers_reports_glanced_leaving_the_bus(tmp_path):
    daemon = shutil.which("dbus-daemon")
    if daemon is None:
        pytest.skip("dbus-daemon is not installed")
    bus = subprocess.Popen([daemon, "--session", "--nofork", "--print-address=1",
                            f"--address=unix:path={tmp_path}/bus"],
                           stdout=subprocess.PIPE, text=True)
    try:
        address = bus.stdout.readline().strip()
        assert _gjs(tmp_path, _PEERS, {"GLANCE_TEST_BUS": address}) == "OK"
    finally:
        bus.kill()
        bus.wait()


def test_extension_releases_everything_when_glanced_vanishes():
    """The wiring the gjs tests cannot load (it imports GNOME Shell)."""
    src = (REPO / "extension" / "extension.js").read_text()
    gone = src[src.index("    _daemonGone() {"):]
    gone = gone[:gone.index("\n    }\n")]
    assert "this._ungrab(name)" in gone and "this._grabs.keys()" in gone
    assert "this._overlay?.reset()" in gone and "this._guard?.gone()" in gone
    assert "role === 'daemon'" in src and "this._daemonGone()" in src
    assert "this._guard.pressed(done => this._daemon('Key', {name}, done))" in src
    overlay = (REPO / "extension" / "overlay.js").read_text()
    reset = overlay[overlay.index("    reset() {"):]
    assert "this._closeAsk(false)" in reset[:200] and "this.render({})" in reset[:200]
