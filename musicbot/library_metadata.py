import asyncio
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import NamedTuple, Optional, Tuple

from musicbot.audiotags import read_tags

# Dedicated pool (not the loop's shared default executor) for the
# blocking calls this module makes - mutagen reads and spotipy's
# synchronous HTTP client. asyncio.wait_for()'s timeout only abandons
# the await, not the underlying thread, so a string of slow/hung
# external calls could otherwise pile up threads in whatever pool the
# rest of the bot shares.
_executor = ThreadPoolExecutor(max_workers=4)


class ArtInfo(NamedTuple):
    url: Optional[str]  # a real URL - use with embed.set_thumbnail(url=...)
    data: Optional[bytes]  # raw embedded bytes - needs discord.File
    extension: Optional[str]  # "jpeg"/"png"/etc, set only when data is set


async def _resolve_names(
    artist_folder: str, album_folder: Optional[str], sample_file: Path
) -> Tuple[str, Optional[str]]:
    """Resolves the query name(s) to use for external lookups,
    preferring tag data over the (possibly organizational-only,
    non-canonical) folder name: albumartist tag > artist tag > folder
    name for the artist, album tag > folder name for the album. A
    hung/slow read (e.g. MUSIC_LIBRARY_PATH on a NAS mount) times out
    and falls back to the folder names rather than blocking forever."""
    loop = asyncio.get_running_loop()
    artist_name = artist_folder
    album_name = album_folder
    try:
        tags = await asyncio.wait_for(
            loop.run_in_executor(_executor, read_tags, sample_file),
            timeout=3,
        )
    except asyncio.TimeoutError:
        print(
            f"library_metadata: reading tags from {sample_file} timed out",
            file=sys.stderr,
        )
    else:
        artist_name = tags.album_artist or tags.artist or artist_folder
        if album_folder is not None:
            album_name = tags.album or album_folder
    return artist_name, album_name
