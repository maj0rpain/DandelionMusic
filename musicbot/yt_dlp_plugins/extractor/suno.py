import re

from yt_dlp.extractor.common import InfoExtractor


class SunoAIIE(InfoExtractor):
    _VALID_URL = r"^https?://(app\.suno\.ai|suno\.com)/song/(?P<code>\w+)"

    def _real_extract(self, url):
        from musicbot.loader import _loop
        from musicbot import linkutils

        match = re.match(self._VALID_URL, url)
        try:
            soup = _loop.run_until_complete(linkutils.get_soup(url))
            return {
                "id": match.group("code"),
                "url": soup.find(property="og:audio")["content"],
                "title": re.sub(r" \| Suno$", "", soup.find("title").string),
                "thumbnail": soup.find(property="og:image")["content"],
            }
        except Exception as e:
            raise linkutils.download_error(e) from e
