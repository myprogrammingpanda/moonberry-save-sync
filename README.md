# Moonberry Save-Sync

A small desktop app that keeps a shared Valheim world in sync between
friends: whoever opens the app can see if someone's already hosting, claim
the host slot themselves, and have their save automatically uploaded and
shared when they're done. Built to be extendable to other games later
without changing how it works today.

## Setup (one-time)

1. **Install Python** (3.11 or newer) from [python.org](https://www.python.org/downloads/).
   On the installer's first screen, check **"Add python.exe to PATH"**.
2. **Download this app**: go to the [Releases page](../../releases/latest)
   and download **`moonberry-save-sync-vX.X.X.zip`** under Assets (not
   the auto-generated "Source code" links above it), then extract it
   somewhere convenient (e.g. `Documents\MoonberrySaveSync`).
3. Open a terminal in that folder (Windows: right-click inside the folder
   in File Explorer → **Open in Terminal**) and run:
   ```
   pip install -r requirements.txt
   ```
4. Run the app by double-clicking **`run.bat`** in that folder (or from a
   terminal: `python main.py`).
   The **first time** you run it, a setup window will appear asking for
   a few values (coordinator URL, shared secret, storage credentials,
   etc.) — get these from whoever's running the group's coordinator/bot
   (not something you make up yourself). Fill them in and hit Save.
   That's it — you won't need to do this again.

   **Setting this up for a new friend group?** See [`SETUP.md`](SETUP.md)
   for deploying your own coordinator and storage — only one person per
   group needs to do this, once.

## Using it

- **Status**: the app shows who's currently hosting (if anyone), live.
- **Host Now**: claims the host slot, downloads the latest save, then
  waits for you to actually start the game yourself (click Play Now, or
  launch it any other way) — it doesn't open the game for you. When you
  close the game, your save is automatically zipped and uploaded for
  everyone else. Refuses with a clear error if the game's already
  running, since syncing a save the game already has open isn't safe.
- **Play Now**: just opens the game — nothing more. Use it to join a
  friend who's already hosting (the join code is shown right in the
  status line), or to actually start playing after clicking Host Now.
  Click **Host Now before Play Now** if you want your save synced before
  you play — once the game's open, syncing can no longer happen for that
  session.
- **Manual Sync** (for when you played outside the app, e.g. solo):
  - **Force Upload Current Save** — uploads whatever's currently in your
    save folder as a new version. Fails cleanly if someone's actively
    hosting, or if the game is currently open, instead of overwriting
    anything.
  - **Force Download Latest** — pulls down the latest cloud save, after
    confirming, with a backup of your current local save kept
    automatically. Also refuses while the game is open.
- **File → Settings**: change any of the setup values later (bucket,
  secrets, world name, etc.) without ever touching a config file by
  hand. Restart the app afterward for changes to take effect.
- **Dark Mode**: the switch in the top-right of the window.

## Troubleshooting

- **"config.json not found" / setup window keeps reappearing**: make
  sure you clicked **Save** in the setup window, not just closed it.
- **Nothing happens when you click Host Now or Play Now**: check the
  Activity Log at the bottom of the window — it logs exactly what's
  happening (or why something failed) at every step.
- Still stuck? Ask whoever set up the group's coordinator/bot.
