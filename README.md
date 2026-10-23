# Desk Buddy

One small app for a Linux laptop that does two jobs:

1. **Shows usage in the top bar:** `Chr 12%  Fx 3%  CPU 20%  RAM 45%`
   - **Chr** / **Fx**: how much of the whole machine Chrome and Firefox are using right now
   - **CPU**: total processor use (100% = every core busy)
   - **RAM**: memory in use
   - Click the text to see more: each browser's CPU and approximate RAM, totals in GB,
     the next reminder times, and the menu.
2. **Reminds you to drink water and walk**, on separate schedules:
   - 💧 Water: every **30 min** by default
   - 🚶 Walk: every **1 hour** by default
   - Both from **11:00 AM to 8:00 PM**, every day. Change it all with the ⚙ button.

The whole app is one file, `desk_buddy.py`. It needs no internet, no account and no pip packages.

> **Status: v0.1 (early).** Tested on Ubuntu 24.04 / GNOME 46: usage readings, reminder cards,
> schedules, floating bar, start/stop, install and uninstall. **Not yet tested:** the top-bar (tray)
> view and the package-install step on a fresh laptop, and Fedora / Arch / openSUSE. Reports welcome.

---

## Will it work on my laptop?

| Laptop | Works? |
|---|---|
| Ubuntu, Linux Mint, Pop!_OS, Debian (any desktop) | ✅ Yes |
| Fedora, Arch / Manjaro, openSUSE | ✅ Should (the installer knows their package names) |
| Windows, macOS | ❌ No (it uses Linux-only parts) |

---

## Install (about 2 minutes)

1. Copy the `desk-buddy-pack` folder to the laptop (USB stick, Google Drive, anything).
2. Open a **Terminal** in that folder. In the file manager, right-click inside the folder and choose
   **Open in Terminal**.
3. Run:

   ```
   python3 desk_buddy.py install
   ```

4. If it lists missing packages, press **Enter** to install them and type the laptop's password.
   That is the normal `sudo` password prompt.
5. Done. The usage text appears in the top bar, and the app starts by itself at every login.

Try a reminder right away:

```
python3 ~/.local/share/desk-buddy/desk_buddy.py test
```

> **Ubuntu / GNOME note:** the top-bar text needs GNOME's "AppIndicator" extension. Ubuntu has it on
> already. On other GNOME systems the installer installs and switches it on. If the text does not show,
> **log out and back in once**.

After installing, you can delete the copied folder. The app lives in `~/.local/share/desk-buddy/`.

---

## Using it

| I want to… | Do this |
|---|---|
| Change water / walk times | Click **⚙** on any reminder card, **or** click the top-bar text → **Reminder settings…**, **or** open **Desk Buddy** from the app menu |
| Close a reminder | Click **Done ✓**; the next one comes at its next time |
| Turn only water (or only walk) off | Settings → that section → switch **On** off → **Save** |
| Pause all reminders for an hour | Top-bar text → **Pause reminders for 1 hour** (click again to resume) |
| See a reminder now | Top-bar text → **Show a reminder now** |
| Quit until next login | Top-bar text → **Quit Desk Buddy** (start it again from the app menu) |

**Settings window, per reminder:** On/off, first reminder, last reminder, how often
(15 min, 30 min, 45 min, 1 h, 1.5 h, 2 h), and which days.

**When water and walk are due at the same time** (e.g. 12:00) you get **one** card for both, not two.
If a card is still open when the next one is due, the new one is skipped, so cards never pile up.

---

## Commands

Run these in a terminal. After install, the file is at `~/.local/share/desk-buddy/desk_buddy.py`;
before install, use the copy in this folder.

| Command | What it does |
|---|---|
| `python3 desk_buddy.py install` | Install packages + app, start at login, start now |
| `python3 desk_buddy.py install --floating` | Same, but show usage in a small floating bar (bottom-right) instead of the top bar |
| `python3 desk_buddy.py status` | Installed? Running? Next water / walk times |
| `python3 desk_buddy.py usage` | Print one usage reading in the terminal |
| `python3 desk_buddy.py settings` | Open the reminder settings |
| `python3 desk_buddy.py test` | Show a water + walk card now (`test water` or `test walk` for one) |
| `python3 desk_buddy.py stop` | Stop the app (it starts again at next login) |
| `python3 desk_buddy.py run` | Start the app by hand |
| `python3 desk_buddy.py uninstall` | Remove the app; keeps your settings |
| `python3 desk_buddy.py uninstall --purge` | Remove the app **and** your settings |

Running `install` again is safe. It is also how you **upgrade**: copy the new `desk_buddy.py`,
run `install`, and it restarts with your settings kept.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| No text in the top bar (GNOME) | Log out and back in once. Still nothing? `python3 desk_buddy.py install --floating` |
| "GTK 3 for Python is missing" | Run `python3 desk_buddy.py install` again and let it install the packages |
| Reminder never came | `python3 desk_buddy.py status`: is it running? Are the times and days right? Is it paused? |
| Want to check it starts at login | `ls ~/.config/autostart/desk-buddy.desktop` should exist |
| Browser shows `–` | That browser is not running (Chrome, Chromium and Firefox, including snap versions, are detected) |
| Two screens | Cards and the floating bar appear on the **primary** screen |

**Packages it installs** (Ubuntu/Debian names): `python3-gi`, `python3-gi-cairo`, `gir1.2-gtk-3.0`,
`gir1.2-ayatanaappindicator3-0.1`, and on GNOME `gnome-shell-extension-appindicator`. All are standard
Ubuntu packages. Nothing is downloaded from anywhere else.

---

## What it touches (and what it doesn't)

**Creates:**
- `~/.local/share/desk-buddy/desk_buddy.py` (the app)
- `~/.config/autostart/desk-buddy.desktop` (start at login)
- `~/.local/share/applications/desk-buddy.desktop` (app-menu entry)
- `~/.config/desk-buddy/settings.json` (your reminder times, once you press Save)

**Reads only:** `/proc`, the same numbers System Monitor uses. It sees only processes of the logged-in
user.

**Never:** connects to the internet, needs admin rights after install, or changes any other app's settings.
The only system-wide changes are the packages above (with your OK) and, on GNOME, switching on the
AppIndicator extension. See [SECURITY.md](SECURITY.md).

---

## Credits and licence

Original code, written for this project. No code was copied from other projects. It uses only the
Python standard library and your distribution's GTK 3 / PyGObject / Ayatana AppIndicator libraries.
Those are installed from your system's package manager, not bundled here, and keep their own (LGPL)
licences. Chrome and Firefox are trademarks of their owners; Desk Buddy only measures them.

Released under the [MIT licence](LICENSE).
