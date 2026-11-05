# Glossary

**Admission**: the decision whether a request - a command, a player button click, a pick, or a reaction - may act now, and, for one that may bring the bot into voice, the join that follows it. A refused request does nothing; a join happens only after every check has admitted it.

**Enrichment**: what is known about an artist or album beyond the library index itself: online listener counts, popularity and community tags, and cover art from the web or the files' embedded artwork. A browse screen draws without it and fills it in when it arrives.

**Extraction lane** - one of the loader's dedicated extraction workers: the *preload lane* serves only preloads, so playback never waits behind a user-requested load in the *load lane*.

**Joining command**: a command that may bring the bot into the invoker's voice channel, or move it there: when it runs, or later, when a pick in a view it opened acts in its name. Every other command never brings the bot into voice or moves it there.

**Truncated playlist**: a Spotify playlist or album that loaded fewer tracks than Spotify reports it has, because the bot has no Spotify API credentials or the API failed partway through. The user is told how many of how many were queued, and why.

**Guild whitelist** - The guilds the bot may stay in. When it is empty the whitelist is *disabled* and every guild is *admitted*; otherwise a guild is admitted only if it is listed. A change that leaves a present guild unadmitted makes the bot leave it.

**Playback** - one guild's start/advance/stop state machine over its voice connection: which track is starting or playing, which comes next, and when the playlist backup is written. Distinct from a guild session (the guild's settings, controller and player panel, `GuildSessions`). Its states are idle, starting (a track is claimed but its preload has not finished) and active (playing or paused).

**Player panel**: A guild's playback-facing side in Discord: the channel the bot posts playback news to (now playing, skipped tracks), and the one message whose buttons follow the playback state. _Avoid_: view, now-playing message.
