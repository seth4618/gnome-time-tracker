'use strict';

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import { Extension } from 'resource:///org/gnome/shell/extensions/extension.js';

// ====== CONFIGURABLE CONSTANTS ======

const INTERVAL_SECONDS = 1;  // poll interval in seconds
const IDLE_PERIOD = 7*(INTERVAL_SECONDS/8)

const LOG_PATH = GLib.build_filenamev([
    GLib.get_home_dir(),
    '.local', 'share', 'window-logger.log',
]);

// ====== Helper Functions ======

function _getCmdlineFromPid(pid) {
    if (!pid)
        return null;

    const path = `/proc/${pid}/cmdline`;
    try {
        const [ok, contents] = GLib.file_get_contents(path);
        if (ok && contents) {
	    // contents is a Uint8Array, convert to string and strip nulls
	    const raw = contents.toString();
	    const cleaned = raw.replace(/\u0000+/g, ' ').trim();
	    return cleaned.length ? cleaned : null;
        }
    } catch (e) {
        // process probably exited
    }

    return null;
}



// ====== Main Class ======

export default class WindowLoggerExtension extends Extension {
    constructor(metadata) {
        super(metadata);
        this._timeoutId = 0;
        this._lastSnapshotJson = null;

	// track whether we are idling or not
	this._idleState = false;

	// track whether locked previously
	this._lockedState = false;

        // Map: title -> hash string ("<first-ts>-<4-char-hash>")
        this._titleHashes = new Map();

        // Set: titles we've already *logged* with their full title
        this._seenTitles = new Set();

        // Idle monitor will be set in enable()
        this._idleMonitor = null;
    }

    // --- Helpers ---

    // Unix timestamp (seconds since epoch)
    _nowUnix() {
        return Math.floor(Date.now() / 1000);
    }

    // Simple 4-character hash of the title, encoded base36
    _simpleHash4(title) {
        let h = 0;
        for (let i = 0; i < title.length; i++) {
            h = (h * 31 + title.charCodeAt(i)) >>> 0;  // unsigned 32-bit
        }
        const base36 = h.toString(36);
        return base36.slice(-4).padStart(4, '0');
    }

    // Return stable hash for this title within the current session.
    // On first sight of this title, we compose "<ts>-<4char>" and store it.
    _getOrCreateTitleHash(title, ts) {
        if (this._titleHashes.has(title)) {
            return this._titleHashes.get(title);
        }

        const h4 = this._simpleHash4(title);
        const fullHash = `${ts}-${h4}`;
        this._titleHashes.set(title, fullHash);
        return fullHash;
    }

    _getWindowsSnapshot(ts) {
        const actors = global.get_window_actors();
        const windows = [];

        for (const actor of actors) {
            const metaWin = actor.meta_window;
            if (!metaWin)
                continue;

            const title = metaWin.get_title() || '';
            const pid = metaWin.get_pid ? metaWin.get_pid() : null;
            const focused = metaWin.has_focus ? metaWin.has_focus() : false;
	    const cmdline = _getCmdlineFromPid(pid);

            const hash = this._getOrCreateTitleHash(title, ts);

            const winRecord = {
                pid,
		cmd: _getCmdlineFromPid(pid),
                focused,
            };

            if (!this._seenTitles.has(title)) {
                // First time we log this title in this session:
                winRecord.title = title;
                winRecord.hash = hash;
                this._seenTitles.add(title);
            } else {
                // Subsequent times: only include hash
                winRecord.hash = hash;
            }

            windows.push(winRecord);
        }

        return windows;
    }

    _ensureLogDir() {
        const file = Gio.File.new_for_path(LOG_PATH);
        const dir = file.get_parent();
        if (!dir)
            return;

        try {
            dir.make_directory_with_parents(null);
        } catch (e) {
            // Ignore if it already exists or cannot be created
        }
    }

    _appendLogLine(line) {
        try {
            this._ensureLogDir();

            const file = Gio.File.new_for_path(LOG_PATH);
            const outputStream = file.append_to(
                Gio.FileCreateFlags.NONE,
                null
            );

            const bytes = new GLib.Bytes(line + '\n');
            outputStream.write_bytes(bytes, null);
            outputStream.close(null);
        } catch (e) {
            logError(e, 'WindowLogger: failed to append log line');
        }
    }

    // see whether we are idle for more than 7/8 of a period.  If so, call this an idle period.
    // if change in idle state, return true so we force a log entry.
    _checkIdleTime() {
        // Idle time in seconds (keyboard/mouse inactivity)
	// This will either be at most INTERVAL_SECONDS
        let idleSec = 0;
        try {
            if (this._idleMonitor) {
		const idleMs = this._idleMonitor.get_idletime();
		idleSec = idleMs / 1000.0;
            }
        } catch (e) {
            logError(e, 'WindowLogger: failed to get idle time');
            idleSec = 0;
	    return false;
        }

	const idling = (idleSec > IDLE_PERIOD) ? true : false;
	if (this._idleState) {
	    // we have been idling
	    if (idling) {
		// no change in idle state, no log necessary for idle time
		return false;
	    } else {
		// now, not idling
		this._idleState = false;
		return true;
	    }
	} else {
	    // we were active
	    if (idling) {
		// now we are idling
		this._idleState = true;
		return true;
	    } else {
		// we are still active
		return false;
	    }
	}
    }

    _checkLocked() {
        // Locked state (screen locked)
        const locked = Main.screenShield ? Main.screenShield.locked : false;
	const change = (locked != this._lockedState) ? true : false;
	this._lockedState = locked;
	return change;
    }

    _tick() {
        try {
            const ts = this._nowUnix();
	    let needLogging = false
	    
	    needLogging |= this._checkIdleTime();
	    needLogging |= this._checkLocked();

            const snapshot = this._getWindowsSnapshot(ts);
            const snapshotJson = JSON.stringify(snapshot);
            if (snapshotJson !== this._lastSnapshotJson) {
		needLogging = true;
		this._lastSnapshotJson = snapshotJson;
	    }

	    if (needLogging) {
                const record = {
                    ts,
                    idle: this._idleState,
                    locked: this._lockedState,
                    windows: snapshot,
                };

                const line = JSON.stringify(record);
                this._appendLogLine(line);
                log('WindowLogger wrote snapshot');
            }
        } catch (e) {
            logError(e, 'WindowLogger: error in tick()');
        }

        return GLib.SOURCE_CONTINUE;
    }

    // --- Lifecycle ---

    enable() {
        log('WindowLogger ENABLED');

        this._lastSnapshotJson = null;
        this._titleHashes = new Map();
        this._seenTitles = new Set();

        // Initialize idle monitor here (modern GNOME Shell pattern)
        try {
            this._idleMonitor = global.backend.get_core_idle_monitor();
        } catch (e) {
            this._idleMonitor = null;
            logError(e, 'WindowLogger: failed to get core idle monitor');
        }

        // Log a restart marker
        const restartRecord = {
            ts: this._nowUnix(),
            restart: true,
        };
        this._appendLogLine(JSON.stringify(restartRecord));

        // Make sure we don't create multiple timers
        if (this._timeoutId !== 0) {
            GLib.source_remove(this._timeoutId);
        }

        this._timeoutId = GLib.timeout_add_seconds(
            GLib.PRIORITY_DEFAULT,
            INTERVAL_SECONDS,
            this._tick.bind(this),
        );
    }

    disable() {
	log('WindowLogger DISABLED');

	// Log a stop marker so the analyzer can see the gap reason
	const stopRecord = {
            ts: this._nowUnix(),
            stopped: true,
	};
	this._appendLogLine(JSON.stringify(stopRecord));

	if (this._timeoutId !== 0) {
            GLib.source_remove(this._timeoutId);
            this._timeoutId = 0;
	}

	this._lastSnapshotJson = null;
	this._titleHashes = new Map();
	this._seenTitles = new Set();
	this._idleMonitor = null;
    }
}
