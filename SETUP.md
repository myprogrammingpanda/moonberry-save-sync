# Setting up your own coordinator (new friend group)

This is for **one person per friend group**, done **once**. Everyone else
just needs the values this produces (see the bottom of this doc) — they
follow the regular [README](README.md) setup, not this one.

You'll deploy two small pieces of free infrastructure:

1. A **coordinator** (a Cloudflare Worker) — arbitrates who's currently
   hosting each game, so two people can't both host the same game at once.
2. **Storage** (an S3-compatible bucket) — where save files actually live.

Nothing here is specific to Valheim or Zomboid — the same coordinator and
bucket work for every game the app supports, for your whole group.

## 1. The coordinator (Cloudflare Worker)

You need a free [Cloudflare](https://dash.cloudflare.com/sign-up) account
and [Node.js](https://nodejs.org/) (for Cloudflare's `wrangler` tool). The
coordinator keeps its state in a Durable Object, which has to be set up
with `wrangler` (the dashboard's code editor can't create one) — it's all
on Cloudflare's free plan.

From a terminal in this repo's folder:

```
npm install -g wrangler
wrangler login
wrangler kv namespace create sync-status
```

Open [`wrangler.toml`](wrangler.toml) and change two things: `name` (e.g.
`yourgroup-sync-coordinator`) and the KV namespace `id` (the one the
command above printed). Leave the Durable Object and migration sections as
they are. Then:

```
wrangler secret put SHARED_SECRET
# paste a long random value at the prompt, then:
wrangler deploy
```

- **`SHARED_SECRET`**: generate one with e.g. `openssl rand -hex 32`, or
  any password generator. This is the value everyone's `config.json`
  needs as `worker_secret` — anyone who has it can claim the host slot, so
  treat it like a password, not something posted publicly.
- `wrangler deploy` prints your Worker's URL, something like
  `https://yourgroup-sync-coordinator.<your-subdomain>.workers.dev`.
  That's `worker_url`.

The KV namespace holds a readable copy of the coordinator's state (who's
hosting, each world's latest save) — handy for a look in the Cloudflare
dashboard under **Workers & Pages** → **KV**.

## 2. Storage (Backblaze B2 or Cloudflare R2)

Either works identically — both speak the same S3-compatible API this app
uses. Backblaze B2's free tier (10GB storage, 1GB/day download) is plenty
for save files. Steps for B2:

1. Create a free account at [backblaze.com](https://www.backblaze.com/).
2. **Buckets** → **Create a Bucket** — any name, **Private**.
3. Note the bucket's **Endpoint** (shown on the bucket's page, looks like
   `s3.us-west-004.backblazeb2.com`) — `storage_endpoint_url` is
   `https://` + that endpoint.
4. **Application Keys** → **Add a New Application Key** — restrict it to
   the bucket you just created, read+write access. This gives you a
   **Key ID** (`storage_access_key_id`) and a **Application Key**
   (`storage_secret_access_key`) — the secret is only shown once, save it
   somewhere.
5. Your bucket's name is `storage_bucket_name`.

(Cloudflare R2 works the same way — create a bucket, create an API token
scoped to it, use the endpoint R2 shows you. Same free-tier ballpark.)

## 3. Discord notifications (optional, skip if you don't want them)

A separate small Worker/bot — see
[moonberry-discord-bot](https://github.com/myprogrammingpanda/moonberry-discord-bot)
if you want a Discord channel to get a message whenever someone starts or
stops hosting. Not required for the app to work; leave `moonberry_url`/
`moonberry_secret` blank in Settings if you're skipping this.

## 4. Give everyone these values

Once you have them, every player enters these into the app's own
first-run Settings window (**File → Settings** afterward if they need to
change anything) — nobody needs to touch `config.json` by hand or see this
document:

| Setting              | Where it came from                         |
|----------------------|---------------------------------------------|
| Coordinator URL       | Your Worker's URL (step 1.6)                |
| Coordinator secret    | The `SHARED_SECRET` value you set (step 1.5)|
| Storage endpoint URL  | Step 2.3                                    |
| Storage access key ID | Step 2.4                                    |
| Storage secret key    | Step 2.4                                    |
| Storage bucket name   | Step 2.5                                    |

Each player also fills in their own player name and their own game
folders (world name, save paths, etc) — those are per-person, not shared.
