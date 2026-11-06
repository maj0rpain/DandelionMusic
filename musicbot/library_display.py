"""Library display helpers the browser and the search results share.

Discord-free; imports nothing from the project.
"""

# One icon per kind of thing the library holds, shared by the browser
# and the search results so the two can't drift apart. In a Select
# these go in SelectOption's own `emoji` field, which renders them as
# an icon column and leaves the whole 100-character label budget for
# the name; embed titles and footers have no such field, so there they
# are prefixed as literal text.
KIND_EMOJI = {
    "artist": "\U0001f464",
    "album": "\U0001f4bf",
    "song": "\U0001f3b5",
}

# An embed title holds 256 UTF-16 code units: the icon is two of them,
# the space one and the "…" one, leaving 252 for the name. trim()
# measures in UTF-16 units too, so a non-BMP name counts double.
NAME_TITLE_LIMIT = 252


def trim(text: str, limit: int) -> str:
    """Cuts to `limit` UTF-16 code units plus an ellipsis. A character
    that would straddle the limit is dropped whole. A lone surrogate
    (an undecodable filename byte) counts as one unit."""
    units = 0
    for end, char in enumerate(text):
        units += 2 if ord(char) > 0xFFFF else 1
        if units > limit:
            return text[:end] + "…"
    return text
