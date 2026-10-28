# Shared Twitch list

One **master list** of Xbox players and their Twitch channels, kept by the list owner. Every copy of the app downloads
it, and players in that user's history get their channel automatically (marked ◆ in the Twitch column). Users can
**send their own links** from inside the app; they wait as suggestions until the owner approves them in the app.
No browser and no account are needed for normal users.

## How it is protected

| Who | Key | Can |
| --- | --- | --- |
| Everyone (ships in the program) | Supabase **publishable** key | read the master list, add suggestions. Nothing else |
| Only the owner's PC | Supabase **secret** key | review, approve, reject, block, publish, remove |

The publishable key is made to be public: the database rules in [`shared_list_setup.sql`](shared_list_setup.sql)
decide what it may do (row-level security, column rights and a trigger). It can't read other people's suggestions,
change the master list or delete anything.

Limits on suggestions (in the database, so they also hold for someone not using the app):

* 500 suggestions per copy of the app per day, and at most 20,000 waiting in total
* strict formats (XUID digits, Twitch login characters, lengths)
* duplicates and suggestions the master list already has are dropped silently
* blocked copies are refused

A "copy of the app" is identified by a random ID created on first send. It contains nothing about the person. Someone
determined can make a new ID, so blocking slows a troll down but can't stop one for good. Nothing reaches the
master list without your approval anyway.

## Owner setup (once, about 10 minutes)

1. Make a free account at supabase.com and create a new project (any name; pick a region near you).
2. In the project: **SQL Editor** → New query → paste all of [`shared_list_setup.sql`](shared_list_setup.sql) → **Run**.
3. **Project Settings → API Keys**: note the **Project URL**, the **publishable** key (`sb_publishable_...`)
   and a **secret** key (`sb_secret_...`).
4. In the app: **Shared list...** → press **Ctrl+Shift+O** (the Owner setup button is only shown once a PC has the
   secret key) → paste the secret key, and the URL and publishable key unless the program already has them built in
   (then leave those empty) → **Save and test**. The owner tools now show in the Shared list window.
5. **Publish my links** puts your own links in the master list.

Keep the secret key to yourself. The app stores it only in `%APPDATA%\BetterRecentlyPlayedWith\shared_admin.json`.
If it ever leaks, create a new secret key in Supabase and delete the old one.

## Shipping the list address with the program

Normal users must not need setup, so the URL and the publishable key go into the program file at build time,
**not** into the repository:

* Local build: create `assets/shared_service.json` with
  `{"url": "https://YOUR-PROJECT.supabase.co", "key": "sb_publishable_..."}`. `build.ps1` bundles it when it
  exists, and `.gitignore` keeps it out of git.
* GitHub release build: add a repository secret `SHARED_SERVICE_JSON` with that same JSON
  (Settings → Secrets and variables → Actions). The workflow writes the file before building. Without the secret the
  release simply has no shared list.

Never put the **secret** key in either place.

The app never shows the built-in address and key: Owner setup is hidden from normal users and its fields stay empty
unless that PC saved its own. They are still inside the program file and in its network traffic, so a determined person
can find them; that is fine, because the database only lets that key read the list and add suggestions.

## Day to day

* **Shared list...** shows how many channels the master list has and how many of your links it doesn't have yet.
* **Send my links** (everyone) sends your hand-made links that the master list doesn't have and that you haven't sent
  before.
* **Review suggestions...** (owner) lists waiting suggestions with notes from data you already have: the master
  list has another channel, your own link, the player is in your history (under another name), several people sent the
  same link, or different channels were suggested for one player. Senders with more rejected than approved
  suggestions are shown in red. **Check Twitch** asks Twitch (with your Twitch setup) whether each channel still
  exists under that name. Then **Approve**, **Reject** or **Block sender...**.
* Links are kept by **Xbox XUID** and **Twitch channel ID**, so a renamed gamertag or Twitch channel stays linked.
  With Twitch set up, the app looks up the current Twitch names once a day. On the owner's PC, **Publish my links**
  also counts master entries whose gamertag or Twitch name changed and publishes the current names.
* **Remove from master list** (owner, right-click a player) removes a wrong entry for everyone on their next download.
* Users' apps download the list at start and every 6 hours (can be turned off). A user who removes a ◆ link keeps it
  removed, and their own links always win over the master list.

## Free tier notes

Supabase's free tier pauses a project after about a week without any requests. Every copy of the app downloads the
list at start, so an active user base keeps it awake; if it does get paused, un-pause it in the Supabase dashboard.
Storage is far beyond what this needs (a link is ~100 bytes).
