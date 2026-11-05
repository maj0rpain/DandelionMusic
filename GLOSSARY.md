# Glossary

**Admission**: the decision whether a request - a command, a player button click, a pick, or a reaction - may act now, and, for one that may bring the bot into voice, the join that follows it. A refused request does nothing; a join happens only after every check has admitted it.

**Enrichment**: what is known about an artist or album beyond the library index itself: online listener counts, popularity and community tags, and cover art from the web or the files' embedded artwork. A browse screen draws without it and fills it in when it arrives.

**Extraction lane** - one of the loader's dedicated extraction workers: the *preload lane* serves only preloads, so playback never waits behind a user-requested load in the *load lane*.

**Joining command**: a command that may bring the bot into the invoker's voice channel, or move it there: when it runs, or later, when a pick in a view it opened acts in its name. Every other command never brings the bot into voice or moves it there.

**Truncated playlist**: a Spotify playlist or album that loaded fewer tracks than Spotify reports it has, because the bot has no Spotify API credentials or the API failed partway through. The user is told how many of how many were queued, and why.
