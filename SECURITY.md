# Security

## What Desk Buddy does and does not do

- **No network.** It never opens a network connection: no telemetry, no update checks, no downloads.
- **Reads only `/proc`** (CPU, memory and the logged-in user's own processes), the same data
  System Monitor shows. It cannot see other users' processes.
- **No admin rights at run time.** `sudo` is used only by `install`, only to install the listed
  distribution packages, and only after showing the exact command and asking.
- **Writes only inside your home folder:** `~/.local/share/desk-buddy/`, `~/.config/desk-buddy/`,
  `~/.config/autostart/desk-buddy.desktop`, `~/.local/share/applications/desk-buddy.desktop`, and a
  lock file in your private runtime folder (`$XDG_RUNTIME_DIR`, or `~/.cache/desk-buddy/` if that is unset).
- **No shell commands built from text.** Every command it runs (package manager, `gnome-extensions`,
  System Monitor) is a fixed argument list; nothing you type or it reads is passed to a shell.
- **No third-party Python packages.** It uses the Python standard library plus PyGObject/GTK from
  your distribution. Nothing is fetched from PyPI.

## Checking your copy

Read `desk_buddy.py` before installing; it is a single file. Only install a copy from this repository
or from someone you trust.

## Reporting a problem

Please open a GitHub issue. For anything sensitive, don't post the details publicly. Open an issue
asking for a private contact, or use GitHub's "Report a vulnerability" button on the Security tab.
