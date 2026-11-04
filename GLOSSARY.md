# Glossary

**Enrichment**: what is known about an artist or album beyond the library index itself: online listener counts, popularity and community tags, and cover art from the web or the files' embedded artwork. A browse screen draws without it and fills it in when it arrives.

**Extraction lane** - one of the loader's dedicated extraction workers: the *preload lane* serves only preloads, so playback never waits behind a user-requested load in the *load lane*.

**Joining command**: a command that may bring the bot into the invoker's voice channel, or move it there: before it runs, or later, when a pick in a view it opened acts in its name. Every other command answers without touching voice.

**Truncated playlist**: a Spotify playlist or album that loaded fewer tracks than Spotify reports it has, because the bot has no Spotify API credentials or the API failed partway through. The user is told how many of how many were queued, and why.
