![](https://repository-images.githubusercontent.com/494393877/c7c7a9a7-321a-48a0-b19d-b1e7dcf2a1c0)


# DandelionMusic
A Discord music bot written in Python with support for Youtube, SoundCloud, Spotify, Bandcamp, Twitter, and custom files.

### Keep in mind:
* Every configuration option is described by the comments in `.env.sample`.
* Known problems are listed in Issues. If you are interested in this project feel free to submit an Issue.


<h3>What's Coming?</h1>

  - See TODO in Projects tab

## Prerequisites:

#### API Keys
* Discord - https://discord.com/developers
* Spotify (optional) - https://developer.spotify.com/dashboard/
  - Client ID
  - Client Secret
  - Note: Limited to 50 playlist items without API
* Last.fm (optional) - https://www.last.fm/api/account/create
  - API Key
  - Only used for artist/album listener counts and tags in `d!library browse`

Obtained keys must be entered into the ```.env``` file as environment variables

#### Requirements

Skip Python and uv if you've decided to use the pre-built exe. You still need ffmpeg installed and on PATH.

* Installation of Python 3.13+
* Installation of [uv](https://docs.astral.sh/uv/getting-started/installation/)

Install dependencies:
```
uv sync
```

##### Windows
Download `ffmpeg` from the [official download page](https://ffmpeg.org/download.html) and put it into a folder in PATH. The bot does not download ffmpeg itself; if it is not found on PATH, startup fails with "ffmpeg was not found".
##### Other platforms
Install `ffmpeg` and `libopus` packages.

### Installing - Self hosting

1. Download release if available, alternatively download repository zip
2. Complete Prerequisites
3. Start ```run.py``` in project root (or the exe)
4. See configuration options in the .env file (each one is described by the comments in `.env.sample`)

Button play plugin:
* Set emoji with the setting command to enable this feature
* Emote must be available for bot
* Needs Manage Message permissions

<a id="custom-cookies"></a>
Custom Cookies:
* Extract cookies.txt from your browser using your preferred method
* Save it as a new file inside `config/cookies/`, for example `config/cookies/my-cookies.txt`. Do not overwrite the tracked placeholder `config/cookies/cookies.txt`; git ignores every other file in that directory, so your cookies can't be committed by accident
* Set `COOKIE_PATH` in your `.env` file to that file, for example `COOKIE_PATH=config/cookies/my-cookies.txt` (`.env` overrides the `config.py` defaults and keeps a per-host path out of a tracked file)

### Docker

Compose builds the image locally; there is no pre-built image to pull.

1. Copy the sample environment file: `cp .env.sample .env`
2. Set `BOT_TOKEN` in `.env`
3. Set `MUSIC_LIBRARY_HOST_PATH` in `.env` to the folder with your music on the host. It defaults to `./music`; to use the library, also set `ENABLE_LOCAL_LIBRARY=True` (see below)
4. Build and start the bot: `docker compose up -d --build`

For cookies, follow the custom cookies steps above; compose mounts `config/cookies/` read-only.

The container runs as UID 1000 (user `app`). When upgrading an existing deployment, run `sudo chown -R 1000:1000 data/` once; without it the bot cannot write its database. Also either remove the `backup` volume or chown it the same way. If your host user is not UID 1000, you can instead set `user:` on the bot (`doybot`) service in the compose file and chown `data/` and the `backup` volume to that UID, accepting that the fallback `log.txt` and the yt-dlp/deno caches may then not be writable.

The compose file mounts a music library folder read-only at `/music`. Its host source is `MUSIC_LIBRARY_HOST_PATH` (read by Docker Compose, not by the bot), which defaults to the repo's `./music` directory, so a fresh clone starts on any OS. To use your library, set `MUSIC_LIBRARY_HOST_PATH` in `.env` and also `ENABLE_LOCAL_LIBRARY=True`.

**Upgrade note:** earlier versions defaulted this mount to `F:/Music`. If you relied on that default, set `MUSIC_LIBRARY_HOST_PATH=F:/Music` in `.env`, or the library will appear empty.

Environment variables are described by the comments in `.env.sample`.

## Commands:

### Music

After the bot has joined your server, use ```d!help``` to display help and command information.


```
d!play / d!p / d!yt [link/video title/key words/playlist-link/soundcloud link/spotify link/bandcamp link/twitter link]
```

* Plays the audio of supported website
    - A link to the video (https://ww...)
    - The title of a video ex. (Gennifer Flowers - Fever Dolls)
    - A link to a YouTube playlist
* If a song is playing, it will be added to queue

```
d!playnext / d!pn [link/video title/key words]
```

* Like `d!play`, but puts the song at the front of the queue so it plays next.

```
d!search / d!sc [key words]
```

* Searches YouTube and lists the results to pick from.

#### Playlist Commands

```
d!skip / d!s / d!next
```

* Skips the current song and plays next in queue.

```
d!queue / d!q
```

* Show the list of songs in queue

```
d!shuffle / d!sh
```

* Shuffle the queue

```
d!l / d!loop [all/single/off]
```

* Loops the entire queue by default. `d!loop single` loops current track.

```
d!mv / d!move
```

* Move song position in queue

```
d!clear / d!cl
```

* Clears the queue.

```
d!restore
```

* Restores the last playlist after an unexpected disconnect.

#### Saved Playlists

```
d!playlist / d!pl
```

* Saves, loads and edits named playlists. Subcommands:
    - `d!playlist save (name)` / `d!pl s (name)`: saves the current queue
    - `d!playlist load (name)` / `d!pl l (name)`: loads and plays a playlist
    - `d!playlist remove (name)` / `d!pl r (name)`: deletes a playlist
    - `d!playlist list` / `d!pl li`: lists the saved playlists
    - `d!playlist show (name)` / `d!pl sw (name)`: shows the songs of a playlist
    - `d!playlist add_song (name) (song)` / `d!pl as (name) (song)`: adds a song to a playlist
    - `d!playlist remove_song (name) (position)` / `d!pl rs (name) (position)`: removes a song from a playlist
    - `d!playlist move_song (name) (from) (to)` / `d!pl ms (name) (from) (to)`: moves a song within a playlist

#### Audio Commands

```
d!pause / d!resume
```

* Toggles pause: pauses the current song, or resumes it when paused. `d!resume` is an alias of `d!pause`, so it also pauses a playing song.

```
d!prev / d!back
```

* Goes back one song and plays the last song again.

```
d!songinfo / d!np
```

* Shows more details about the current song.

```
d!volume / d!vol
```

* Adjust the volume 0-100%
* Pass no arguments for current volume

```
d!remove / d!rm
```

* Removes a song from the queue (defaults to last song)

```
d!stop / d!st
```
* Stops the current song and clears the playqueue.

#### Local Library Commands

Requires `ENABLE_LOCAL_LIBRARY=True` and `MUSIC_LIBRARY_PATH` set to a folder laid out as `Artist/Album/song.ext` (see `.env.sample`).

`d!lib` is a shorthand for `d!library`, so `d!lib search`, `d!lib browse` and `d!lib refresh` all work. It applies to the prefix commands only - the slash commands stay under `/library`.

Artists, albums and songs are marked with 👤, 💿 and 🎵 respectively, both in the search results and throughout the browser.

`d!library browse`'s artist and album screens show cover art alongside statistics about the selection. Track/album counts, total runtime, years, formats and stream quality come from your own files and are always shown; listener counts and tags are added when Last.fm/Spotify are configured (both optional - the browser works without either); album screens also show Spotify popularity. Cover art comes from artwork embedded in the audio files themselves, falling back to Spotify and Last.fm.

```
d!library search (query) / d!lib search (query)
```

* Searches artists, albums and songs for the five closest matches to the query and lists them to pick from.
* Matching is fuzzy, so partial names and typos still find the right entry.
* Picking a result queues it straight away: a song queues that track, an album queues all of its tracks, an artist queues their whole discography.
* Searches run one at a time - scoring a large library costs real CPU on the host - so if someone else is searching, yours waits its turn rather than being refused.

```
d!library browse / d!lib browse
```

* Opens an interactive browser to queue a song, album, or artist from the local music library.

```
d!library refresh / d!lib refresh
```

* Rescans `MUSIC_LIBRARY_PATH` and rebuilds the browsable library index.
* Bot owner only - adding music files to the library means access to the host itself, so a DJ has no reason to rescan it.


### General

```
d!settings / d!setting / d!set
```
* No Arguments: Lists server settings
* Arguments: (setting) [value]
* Omit the value to reset a setting to its default (Not Set for channels, the DJ role and the button emote)
* Example: d!setting start_voice_channel ChannelName
* One subcommand per setting:
    - `d!setting command_channel [value]`
    - `d!setting start_voice_channel [value]`
    - `d!setting dj_role [value]`
    - `d!setting user_must_be_in_vc [value]`
    - `d!setting button_emote [value]`
    - `d!setting default_volume [value]`
    - `d!setting vc_timeout [value]`
    - `d!setting announce_songs [value]`
* Administrators only

```
d!connect / d!c / d!cc
```

* Connects the bot to the user's voice channel

```
d!disconnect / d!dc
```

* Disconnects the bot from the current voice channel

```
d!history
```
* Shows you the titles of the X last played songs. Configurable in the .env file (MAX_HISTORY_LENGTH)


### Utility

```
d!reset / d!rs / d!restart
```

* Resets the player. May recover the bot after unexpected problems.

```
d!ping
```

* Test bot connectivity

```
d!addbot
```

* Displays information on how to add the bot to another server of yours.

```
d!shutdown
```

* Owner only. Stops the bot completely. May be useful after you move the bot to background.
* Note that unlike other commands, this one doesn't have a slash variant. This allows it to be hidden.

```
d!update
```

* Owner only. Runs `git pull` in the bot's folder and reports its output.

```
d!execute / d!exec (code)
```

* Owner only. **Warning:** runs arbitrary Python in the bot process, with full access to the host. Only bot owners can use it; keep `EXTRA_OWNERS` (see `.env.sample`) short.

```
d!guild_whitelist / d!gw
```

* Owner only. Manages the guilds the bot may stay in; use it instead of the `GUILD_WHITELIST` environment variable.
    - `d!guild_whitelist show` / `d!gw show`: lists the whitelisted guilds
    - `d!guild_whitelist add (guild id)` / `d!gw add (guild id)`: adds a guild
    - `d!guild_whitelist remove (guild id)` / `d!gw remove (guild id)`: removes a guild (the bot leaves it)

```
d!help [command]
```

* Displays help and command information.

## Development

### Docker: the `bgutil-provider` sidecar

`docker compose up --build` also starts a `bgutil-provider` sidecar (`brainicism/bgutil-ytdlp-pot-provider`). yt-dlp's YouTube extractor calls it over HTTP (`http://bgutil-provider:4416`) to generate PO tokens, which reliable YouTube playback requires.

### Building a Windows exe

```
uv sync --locked --group build
uv run --locked python -m config.build
```

This produces `dist/DandelionMusic.exe` via PyInstaller (see `config/build.py` for the bundled data and hidden imports). CI runs it on tag pushes (`.github/workflows/release.yml`).

## License

This program is free software: you can redistribute it and/or modify
it under the terms of the [GNU General Public License](LICENSE) as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.


## Acknowledgements

https://github.com/adriansteffan/DiscordJockey
