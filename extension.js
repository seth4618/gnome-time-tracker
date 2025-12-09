'use strict';

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import { Extension } from 'resource:///org/gnome/shell/extensions/extension.js';

// ====== CONFIGURABLE CONSTANTS ======

const INTERVAL_SECONDS = 1;  // poll interval in seconds
const LOG_PATH = GLib.build_filenamev([
    GLib.get_home_dir(),
    '.local', 'share', 'window-logger.log',
]);

export default class WindowLoggerExtension extends Extension {
    constructor(metadata) {
        super(metadata);
        this._timeoutId = 0;
        this._lastSnapshotJson = null;

        // Map: title -> hash string ("<first-ts>-<4-char-hash>")
        this._titleHashes = new Map();

        // Set: titles we've already *logged* with their full title
        this._seenTitles = new Set();
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

            const hash = this._getOrCreateTitleHash(title, ts);

            const winRecord = {
                pid,
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

    _tick() {
        try {
            const ts = this._nowUnix();
            const snapshot = this._getWindowsSnapshot(ts);
            const snapshotJson = JSON.stringify(snapshot);

            // Only log when something changed compared to last snapshot
            if (snapshotJson !== this._lastSnapshotJson) {
                this._lastSnapshotJson = snapshotJson;

                const record = {
                    ts,
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

        if (this._timeoutId !== 0) {
            GLib.source_remove(this._timeoutId);
            this._timeoutId = 0;
        }

        this._lastSnapshotJson = null;
        this._titleHashes = new Map();
        this._seenTitles = new Set();
    }
}
