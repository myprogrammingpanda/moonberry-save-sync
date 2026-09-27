# Working on this repo

## Branching

Always work on a dedicated branch — never commit directly to `master`, even for a small one-line fix.

- **Bug fixes**: `bugfix/<short-description>` (e.g. `bugfix/wrong-game-save-key`)
- **New features**: `feature/<short-description>` (e.g. `feature/dragonwilds-support`)

Merge back to `master` when the work is done and confirmed working (fast-forward is fine, sole dev — no PR needed unless asked for one). Always ask before pushing anything or creating a release.

## Coordinator

The coordinator Worker lives in `coordinator/`. Deploy the original group's coordinator with `npx wrangler deploy --config coordinator/wrangler.moonberry.toml` — never with `coordinator/wrangler.toml`, which is the template for new groups (Deploy to Cloudflare button) and has no `HOST_KV`. Keep `name`, the Durable Object binding and `[[migrations]]` identical in both files: the stored data belongs to that Worker name + class.
