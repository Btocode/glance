// Who may call the extension: glanced and its glance-capture helper, and nobody else.
//
// Wayland withholds key injection, the focused window and the clipboard from ordinary
// clients on purpose; the extension must not hand them back to any process on the session
// bus. A caller is accepted only when it is the current owner of one of Glance's own
// well-known names AND that connection belongs to a process of this user whose command
// line is glanced (or glance-capture). The check runs when a name gets an owner and again
// for a caller not seen yet; the result is cached per unique bus name and dropped when the
// name changes hands.
//
// Residual risk: an unsandboxed process of the same user can still start something that
// looks like glanced (it can also ptrace the user's processes or read their files, so the
// session bus is not the weakest point). Flatpak and snap apps cannot reach these names
// unless their sandbox is given --talk-name for them.
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

export const ROLES = {
    daemon: 'io.github.btocode.Glance',
    capture: 'io.github.btocode.GlanceCapture',
};

// The command lines Glance's own processes run with: the console scripts of its venv, or
// `python -m <module>` (how glanced starts glance-capture, and how tests start glanced).
const PROGRAMS = {
    daemon: {scripts: ['glanced'], modules: ['glance.daemon.main']},
    capture: {scripts: ['glance-capture'], modules: ['glance.capture.main']},
};

export function commandLineIs(role, args) {
    const want = PROGRAMS[role];
    if (!want || !Array.isArray(args) || args.length === 0)
        return false;
    for (let i = 0; i < args.length; i++) {
        const base = args[i].split('/').pop();
        if (want.scripts.includes(base))
            return true;
        if (args[i] === '-m' && want.modules.includes(args[i + 1]))
            return true;
    }
    return false;
}

function readCmdline(pid) {
    try {
        const [ok, bytes] = GLib.file_get_contents(`/proc/${pid}/cmdline`);
        if (!ok)
            return [];
        return new TextDecoder().decode(bytes).split('\0').filter(a => a.length > 0);
    } catch (_e) {
        return [];
    }
}

function busCall(conn, method, args, signature) {
    return new Promise(resolve => {
        conn.call('org.freedesktop.DBus', '/org/freedesktop/DBus', 'org.freedesktop.DBus',
            method, new GLib.Variant(signature, args), null, Gio.DBusCallFlags.NONE, 2000,
            null, (c, res) => {
                try {
                    resolve(c.call_finish(res).recursiveUnpack()[0]);
                } catch (_e) {
                    resolve(null);
                }
            });
    });
}

export class Peers {
    constructor(conn) {
        this._conn = conn;
        this._uid = new Gio.Credentials().get_unix_user();
        this._owner = {};      // role -> unique name that owns its well-known name now
        this._verified = {};   // role -> unique name checked and accepted
        this._pending = new Map();  // unique name -> Promise<role|null>
        this._watches = Object.entries(ROLES).map(([role, name]) =>
            Gio.bus_watch_name_on_connection(conn, name, Gio.BusNameWatcherFlags.NONE,
                (_c, _n, owner) => {
                    this._owner[role] = owner;
                    this._verified[role] = null;
                    this.verify(role, owner);  // ahead of its first call
                },
                () => {
                    this._owner[role] = null;
                    this._verified[role] = null;
                }));
    }

    destroy() {
        for (const id of this._watches)
            Gio.bus_unwatch_name(id);
        this._watches = [];
    }

    // The unique names accepted right now (for unicast signals).
    accepted() {
        return Object.values(this._verified).filter(Boolean);
    }

    // Resolves to the role of `sender`, or null when it may not call.
    async authorize(sender) {
        for (const [role, name] of Object.entries(this._verified)) {
            if (name && name === sender)
                return role;
        }
        for (const [role, name] of Object.entries(ROLES)) {
            // the watch may not have reported a new owner yet: ask the bus
            const owner = await busCall(this._conn, 'GetNameOwner', [name], '(s)');
            if (owner === sender && await this.verify(role, owner) &&
                this._verified[role] === owner)
                return role;
        }
        return null;
    }

    // The verified unique name of `role`, or null (checks the current owner if needed).
    async peer(role) {
        if (this._verified[role])
            return this._verified[role];
        const owner = this._owner[role] ??
            await busCall(this._conn, 'GetNameOwner', [ROLES[role]], '(s)');
        return owner && await this.verify(role, owner) && this._verified[role] === owner
            ? owner : null;
    }

    verify(role, owner) {
        const key = `${role}\n${owner}`;
        if (!this._pending.has(key)) {
            this._pending.set(key, this._check(role, owner).then(ok => {
                this._pending.delete(key);
                const now = this._owner[role];
                if (ok && (now === owner || now === undefined))
                    this._verified[role] = owner;
                return ok;
            }));
        }
        return this._pending.get(key);
    }

    async _check(role, owner) {
        const creds = await busCall(this._conn, 'GetConnectionCredentials', [owner], '(s)');
        if (!creds || creds.UnixUserID !== this._uid || !creds.ProcessID)
            return false;
        return commandLineIs(role, readCmdline(creds.ProcessID));
    }
}
