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
   the auto-generated "Source code" links above it), then right-click it
   → **Extract All...** into a folder you'll keep (e.g.
   `Documents\MoonberrySaveSync`). Don't run it from inside the zip.
3. Double-click **`run.bat`** in that folder. The **first time**, a
   console window opens and installs the packages the app needs — this
   takes a minute or two and needs an internet connection. When it's
   done, you're offered **Desktop** and **Start Menu** shortcuts (tick
   whichever you want). After that, the app opens straight away every
   time. (Skipped the shortcuts? Double-click **`create-shortcuts.bat`**
   any time to make them.)

   If anything goes wrong — Python missing or too old, no internet —
   `run.bat` pops up a message saying what to do.
4. The **first time** the app opens, a setup window will appear asking for
   a few values (coordinator URL, shared secret, storage credentials,
   etc.) — get these from whoever's running the group's coordinator/bot
   (not something you make up yourself). Fill them in and hit Save.
   That's it — you won't need to do this again.

   **Which edition of the game do you have?** Valheim has an **Edition**
   dropdown in its settings: **Steam** (the default) or **Xbox app /
   Game Pass (PC)**. Pick yours (or click **Detect**) and the launch/save
   settings fill in to match. Both editions use the same save format, so
   Steam and Game Pass players can share one world.

   Game Pass (PC) support is **new and not yet tested** on a real Game
   Pass install — it's based on research, so if something's off (the
   game doesn't launch, or the join code never shows up), tell whoever
   runs the group. Game Pass players: in Valheim, open **Manage Saves**
   and use **Move to Local** on the world first — worlds left in Xbox
   cloud storage can't be synced.

   **RuneScape: Dragonwilds** has the same **Edition** dropdown: **Steam**
   or **Xbox app / Game Pass (PC)**. Steam and Game Pass players can share
   one world; only the world is synced, never your character. Game Pass
   players: launch Dragonwilds and load or create any world once on your
   PC before syncing. The Game Pass side has been tested on a real
   install; the Steam side hasn't yet.

   **V Rising** (Steam, **Private Game** hosted from the game — not a
   dedicated server): set **World name** to the world's name exactly as
   it shows in **Continue / Load Game**. Worlds in your Steam Cloud saves
   are found automatically; a world downloaded for the first time goes
   into your local saves and shows up in Load Game as a local world.
   Everyone keeps their own character when someone else hosts. Friends
   join through a Steam invite or the server list — there's no join code
   — and the game asks the host for the world's password each time.

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
- **Playing with friends on Xbox/PlayStation/Switch (Valheim)**: tick
  **Crossplay** when you start the world in-game. Console players can
  then join with the join code shown in the status line — they just
  can't host, since consoles can't run this app.
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
