# Track acitivty under gnome with wayland

Track focus on windows. Eventually, set up focus times using the 45-on/15-off method.

## Log format

The extension writes newline-delimited JSON to `~/.local/share/window-logger.log`. Each
record always contains a Unix timestamp under `ts` and can include the following shapes:

- Restart marker: `{ "ts": 1700000000, "restart": true }`
- Stop marker: `{ "ts": 1700003600, "stopped": true }`
- State snapshot (idle/locked/window focus):
  - Base keys: `ts`, optional `idle: true` if currently idle, optional `locked: true`
    if the session is locked.
  - Window payload: `windows` is an array of window objects. Two snapshot modes are
    used:
    - Full snapshot (`full: true` or omitted): contains membership and focus for all
      known windows.
    - Focus-only snapshot (`focusOnly: true`): only the focus change is reported; window
      membership is carried over from the last full snapshot.
  - Window objects can include:
    - `hash` (string): window identifier (e.g., `<first-seen-ts>-<4-char-hash>`).
    - `focused` (boolean): whether the window is focused in this snapshot.
    - `title` (string): first time a window is seen during a session.
    - `cmd` (string): command line for the window's process, when known.

## Managing the extension

List extensions to make sure it’s seen:

```bash
gnome-extensions list | grep window-logger
```

Enable it:

```bash
gnome-extensions enable window-logger@example.com
```

Check that it is ok:

```bash
gnome-extensions info window-logger@example.com
```

Reloading under Wayland currently requires logging out and back in.
