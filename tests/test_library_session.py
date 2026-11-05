"""The browse session's draw protocol, without Discord.

Synchronous throughout, like the cursor's tests.
"""

import ast
from pathlib import Path

from musicbot import library, library_browse, library_session
from musicbot.library_metadata import ArtInfo, Enrichment, ExternalStats
from musicbot.library_session import BrowseSession, stat_fields
from library_fakes import library_song

INDEX = {
    "Radiohead": {
        "OK Computer": [library_song("Airbag", year=1997, genre="Rock")],
        "Kid A": [library_song("Zoo")],
    },
    "The Beatles": {"Abbey Road": [library_song("Come Together")]},
    "Aphex Twin": {"Drukqs": [library_song("Avril")]},
}


def session(index=INDEX, page_size=2):
    return BrowseSession(index, page_size)


def at_artist():
    s = session()
    s.descend("Radiohead")
    s.enter_level()
    return s


def at_album():
    s = at_artist()
    s.descend("OK Computer")
    s.enter_level()
    return s


EMBEDDED = Enrichment(
    stats=None, art=ArtInfo(url=None, data=b"png", extension="png")
)
URL = Enrichment(
    stats=None,
    art=ArtInfo(url="http://x/y.jpg", data=None, extension=None),
)


class TestUndo:
    def test_level_change_undone_restores_position_and_enrichment(self):
        s = at_artist()
        token = s.enter_level()
        assert s.offer_enrichment(token, URL)
        snap = s.snapshot()
        s.descend("Kid A")
        s.enter_level()
        s.restore(snap)
        assert s.position() == ("Radiohead", None, 0)
        assert s.frame().thumbnail == "http://x/y.jpg"

    def test_page_turn_undone_keeps_the_drawn_enrichment(self):
        s = session()
        token = s.enter_level()
        s.offer_enrichment(token, URL)
        pos = s.position()
        s.page_by(1)
        s.restore_position(pos)
        assert s.position().page == 0
        assert s.frame().thumbnail == "http://x/y.jpg"


class TestStaleness:
    def test_stale_enrichment_is_dropped(self):
        s = at_artist()
        token = s.enter_level()
        s.descend("Kid A")
        s.enter_level()
        assert s.offer_enrichment(token, URL) is False
        assert s.frame().thumbnail is None

    def test_undone_level_change_still_drops_earlier_token(self):
        s = at_artist()
        token = s.enter_level()
        snap = s.snapshot()
        s.descend("Kid A")
        s.restore(snap)
        assert s.offer_enrichment(token, URL) is False

    def test_level_change_clears_the_drawn_enrichment(self):
        s = at_artist()
        s.offer_enrichment(
            s.enter_level(),
            Enrichment(
                stats=ExternalStats(
                    listeners=5000,
                    playcount=None,
                    tags=(),
                    popularity=None,
                    followers=None,
                    release_date=None,
                ),
                art=EMBEDDED.art,
            ),
        )
        assert s.frame().attachment is not None
        s.back()
        s.enter_level()
        f = s.frame()
        assert f.thumbnail is None and f.attachment is None
        s.descend("Radiohead")
        s.descend("Kid A")
        s.enter_level()
        f = s.frame()
        assert f.thumbnail is None and f.attachment is None
        assert "Listeners" not in dict(f.fields)

    def test_page_turn_keeps_the_level_revision(self):
        s = session()
        token = s.enter_level()
        s.page_by(1)
        assert s.offer_enrichment(token, URL) is True


class TestAttachments:
    def test_change_until_landed_then_not(self):
        s = at_artist()
        s.offer_enrichment(s.enter_level(), EMBEDDED)
        f = s.frame()
        assert f.attachments_change is True
        assert f.attachment.key == "Radiohead/None.png"
        assert f.thumbnail == "cover.png"
        s.landed(f)
        assert s.frame().attachments_change is False

    def test_failed_edit_leaves_the_key_unchanged(self):
        s = at_artist()
        s.offer_enrichment(s.enter_level(), EMBEDDED)
        s.frame()  # edit failed: landed() not called
        assert s.frame().attachments_change is True

    def test_page_turn_repairs_a_failed_enrichment_edit(self):
        # page_size=1 makes Radiohead's two albums two pages
        s = session(page_size=1)
        s.descend("Radiohead")
        s.landed(s.frame())
        s.offer_enrichment(s.enter_level(), EMBEDDED)
        # enrichment edit failed, then the user turns a real page
        s.page_by(1)
        assert s.position().page == 1
        f = s.frame()
        assert f.attachments_change is True
        assert f.attachment.key == "Radiohead/None.png"

    def test_leaving_an_attached_level_changes_the_attachment(self):
        s = at_artist()
        s.offer_enrichment(s.enter_level(), EMBEDDED)
        s.landed(s.frame())
        s.back()
        s.enter_level()
        f = s.frame()
        assert f.attachments_change is True and f.attachment is None
        s.landed(f)
        assert s.frame().attachments_change is False

    def test_url_art_needs_no_attachment(self):
        s = at_artist()
        s.offer_enrichment(s.enter_level(), URL)
        f = s.frame()
        assert f.attachment is None and f.attachments_change is False


class TestFrameText:
    def test_root(self):
        f = session().frame()
        assert f.title == "\U0001f464 Artists"
        assert f.footer is None
        assert f.description is None
        assert f.fields == (
            ("Artists", "3"),
            ("Albums", "4"),
            ("Songs", "4"),
        )
        assert not f.show_back and not f.show_queue
        assert f.entries == ["Aphex Twin", "Radiohead"]
        assert f.show_next and not f.show_prev

    def test_artist(self):
        f = at_artist().frame()
        assert f.title == "\U0001f464 Radiohead"
        assert f.footer == "Artists › \U0001f464 Radiohead"
        assert f.queue_label == "Queue this Artist"
        assert f.kind == "album"
        assert dict(f.fields)["Albums"] == "2"
        assert f.description == "*Rock*"

    def test_album(self):
        f = at_album().frame()
        assert f.title == "\U0001f4bf OK Computer"
        assert f.footer == ("\U0001f464 Radiohead › \U0001f4bf OK Computer")
        assert f.queue_label == "Queue this Album"
        assert f.kind == "song"
        assert f.labels == ["Airbag"]
        assert dict(f.fields)["Year"] == "1997"
        assert f.description == "*Rock*"

    def test_enrichment_tags_override_genres(self):
        s = at_album()
        s.offer_enrichment(
            s.enter_level(),
            Enrichment(
                stats=ExternalStats(
                    listeners=1500,
                    playcount=None,
                    tags=("a", "b"),
                    popularity=80,
                    followers=None,
                    release_date=None,
                ),
                art=None,
            ),
        )
        f = s.frame()
        assert f.description == "*a · b*"
        assert dict(f.fields)["Listeners"] == "1.5K"
        assert dict(f.fields)["Popularity"] == "80/100"

    def test_long_names_are_trimmed(self):
        name = "x" * 300
        s = session({name: {name: [library_song("s")]}})
        s.descend(name)
        f = s.frame()
        assert f.title == "\U0001f464 " + "x" * 252 + "…"
        s.descend(name)
        assert s.frame().title == "\U0001f4bf " + "x" * 252 + "…"

    def test_empty_level(self):
        f = session({}).frame()
        assert f.empty is True
        assert f.fields == () and f.description is None
        assert f.entries == []


class TestStatFieldBuilder:
    def _screen(self, **kw):
        base = dict(
            entries=["e"],
            labels=["e"],
            kind="artist",
            scope=(None, None),
            counts=None,
            stats=None,
        )
        base.update(kw)
        return library_browse.Screen(**base)

    def test_counts_alone_select_the_root_branch(self):
        # scope says "artist level"; only `counts` is set
        screen = self._screen(scope=("A", None), counts=(1, 2, 3))
        assert stat_fields(screen, None) == [
            ("Artists", "1"),
            ("Albums", "2"),
            ("Songs", "3"),
        ]

    def test_stats_alone_select_the_lower_branch(self):
        stats = library.LevelStats(2, 5, None, None, None, (), None, ())
        screen = self._screen(kind="album", scope=(None, None), stats=stats)
        assert stat_fields(screen, None) == [
            ("Albums", "2"),
            ("Tracks", "5"),
        ]


class TestTheSeam:
    def test_imports_are_pinned(self):
        source = Path(library_session.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        guarded = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.If) and (
                getattr(node.test, "id", None) == "TYPE_CHECKING"
            ):
                for inner in ast.walk(node):
                    guarded.add(id(inner))
        names = set()
        for node in ast.walk(tree):
            if id(node) in guarded:
                continue
            if isinstance(node, ast.Import):
                names.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add(node.module)
                names.update(f"{node.module}.{a.name}" for a in node.names)
        assert {n for n in names if n.startswith("musicbot")} == {
            "musicbot",
            "musicbot.library",
            "musicbot.library_browse",
        }
        assert {n.split(".")[0] for n in names} == {"typing", "musicbot"}
        # and the one allowed exception really is TYPE_CHECKING-only,
        # and is all that block holds
        blocks = [
            n
            for n in tree.body
            if isinstance(n, ast.If)
            and getattr(n.test, "id", None) == "TYPE_CHECKING"
        ]
        assert [ast.unparse(s) for b in blocks for s in b.body] == [
            "from musicbot import library_metadata"
        ]


class TestTrimAndFormat:
    def test_trim_drops_whole_char_when_budget_ends_in_surrogate_pair(self):
        text = "x" + "\U0001f600" * 10
        assert library_session.trim(text, 4) == "x\U0001f600…"

    def test_trim_cuts_bmp_text_at_the_limit(self):
        assert library_session.trim("abcdef", 4) == "abcd…"

    def test_trim_returns_text_within_budget_unchanged(self):
        assert library_session.trim("abcd", 4) == "abcd"
        assert library_session.trim("\U0001f600\U0001f600", 4) == (
            "\U0001f600\U0001f600"
        )

    def test_trim_counts_a_lone_surrogate_as_one_unit(self):
        lone = "\udc80"  # an undecodable filename byte
        assert library_session.trim("ab" + lone, 3) == "ab" + lone
        assert library_session.trim("ab" + lone + "cd", 3) == (
            "ab" + lone + "…"
        )

    def test_non_bmp_title_fits_the_embed_title_limit(self):
        name = "x" + "\U0001f600" * 255
        s = session({name: {"Album": [library_song("S")]}})
        s.descend(name)
        title = s.frame().title
        assert len(title.encode("utf-16-le")) // 2 <= 256
        assert title.endswith("…")

    def test_footer_breadcrumb_is_not_trimmed(self):
        name = "x" * 255
        s = session({name: {"Album": [library_song("S")]}})
        s.descend(name)
        assert name in s.frame().footer

    def test_count_rounds_before_choosing_unit(self):
        def listeners(value):
            s = at_album()
            s.offer_enrichment(
                s.enter_level(),
                Enrichment(
                    stats=ExternalStats(
                        listeners=value,
                        playcount=None,
                        tags=(),
                        popularity=None,
                        followers=None,
                        release_date=None,
                    ),
                    art=None,
                ),
            )
            return dict(s.frame().fields)["Listeners"]

        assert [
            listeners(v)
            for v in (999, 1_500, 999_949, 999_950, 1_204_338_291, 999_999_999)
        ] == ["999", "1.5K", "999.9K", "1M", "1.2B", "1B"]
