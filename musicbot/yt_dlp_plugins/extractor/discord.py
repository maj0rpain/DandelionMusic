import asyncio
import re
import atexit

from discord.http import HTTPClient
from yt_dlp.extractor.common import InfoExtractor

from config import config


# The extractor's own discord.py client, created lazily on the loader's
# worker loop and logged in once; None until a login has succeeded.
_client = None


def _get_client(loop):
    global _client
    if _client is not None:
        return _client
    from musicbot import linkutils

    client = HTTPClient(loop)
    try:
        loop.run_until_complete(
            asyncio.wait_for(
                client.static_login(config.BOT_TOKEN),
                timeout=linkutils.HTTP_TIMEOUT,
            )
        )
    except Exception as e:
        try:
            loop.run_until_complete(client.close())
        except Exception:
            pass
        raise linkutils.download_error(e) from e
    _client = client
    atexit.register(lambda: loop.run_until_complete(client.close()))
    return client


class DiscordAttachmentsIE(InfoExtractor):
    _VALID_URL = (
        r"^https?://(?:canary\.)?discord\.com"
        r"/channels/(?P<guild_id>\d+)/(?P<channel_id>\d+)/(?P<message_id>\d+)"
    )

    def _real_extract(self, url):
        from musicbot import linkutils
        from musicbot.loader import _loop

        client = _get_client(_loop)

        match = re.match(self._VALID_URL, url)
        try:
            resp = _loop.run_until_complete(
                asyncio.wait_for(
                    client.get_message(
                        int(match.group("channel_id")),
                        int(match.group("message_id")),
                    ),
                    timeout=linkutils.HTTP_TIMEOUT,
                )
            )
        except Exception as e:
            raise linkutils.download_error(e) from e
        uploader = resp["author"]["username"]
        entries = [
            {
                "id": a["id"],
                "url": a["url"],
                "title": a["filename"],
                "uploader": uploader,
            }
            for a in resp["attachments"]
        ]
        return {"_type": "playlist", "entries": entries}
