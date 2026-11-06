"""The browse session's draw protocol, without Discord.

Synchronous throughout, like the cursor's tests.
"""

from musicbot import library, library_browse, library_session
from musicbot.library_metadata import ArtInfo, Enrichment
from musicbot.library_session import BrowseSession, stat_fields
from helpers import imported_names, imported_roots, type_checking_imports
from library_fakes import external_stats, library_song

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
                stats=external_stats(listeners=5000),
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
        assert f.queue is None
        assert f.entries == ["Aphex Twin", "Radiohead"]
        assert f.show_next and not f.show_prev

    def test_artist(self):
        f = at_artist().frame()
        assert f.title == "\U0001f464 Radiohead"
        assert f.footer == "Artists › \U0001f464 Radiohead"
        assert f.queue == (
            "Queue this Artist",
            "browse: entire discography of Radiohead",
        )
        assert f.kind == "album"
        assert dict(f.fields)["Albums"] == "2"
        assert f.description == "*Rock*"

    def test_album(self):
        f = at_album().frame()
        assert f.title == "\U0001f4bf OK Computer"
        assert f.footer == ("\U0001f464 Radiohead › \U0001f4bf OK Computer")
        assert f.queue == (
            "Queue this Album",
            "browse: entire album Radiohead - OK Computer",
        )
        assert f.kind == "song"
        assert f.labels == ["Airbag"]
        assert dict(f.fields)["Year"] == "1997"
        assert f.description == "*Rock*"

    def test_enrichment_tags_override_genres(self):
        s = at_album()
        s.offer_enrichment(
            s.enter_level(),
            Enrichment(
                stats=external_stats(
                    listeners=1500, tags=("a", "b"), popularity=80
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

    def test_root_kind_selects_the_counts_fields(self):
        screen = self._screen(counts=(1, 2, 3))
        assert stat_fields(screen, None) == [
            ("Artists", "1"),
            ("Albums", "2"),
            ("Songs", "3"),
        ]

    def test_album_kind_selects_the_artist_level_fields(self):
        stats = library.LevelStats(2, 5, None, None, None, (), None, ())
        screen = self._screen(kind="album", scope=("A", None), stats=stats)
        assert stat_fields(screen, None) == [
            ("Albums", "2"),
            ("Tracks", "5"),
        ]

    def test_song_kind_selects_the_album_level_fields(self):
        stats = library.LevelStats(1, 5, None, None, None, (), None, ())
        screen = self._screen(kind="song", scope=("A", "B"), stats=stats)
        assert stat_fields(screen, None) == [("Tracks", "5")]


class TestTheSeam:
    def test_imports_are_pinned(self):
        names = imported_names(library_session, skip_type_checking=True)
        assert {n for n in names if n.startswith("musicbot")} == {
            "musicbot",
            "musicbot.library",
            "musicbot.library_browse",
            "musicbot.library_display",
        }
        assert imported_roots(library_session, skip_type_checking=True) == {
            "typing",
            "musicbot",
        }
        # and the one allowed exception really is TYPE_CHECKING-only,
        # and is all that block holds
        assert type_checking_imports(library_session) == [
            "from musicbot import library_metadata"
        ]


class TestTrimAndFormat:
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
                    stats=external_stats(listeners=value),
                    art=None,
                ),
            )
            return dict(s.frame().fields)["Listeners"]

        assert [
            listeners(v)
            for v in (999, 1_500, 999_949, 999_950, 1_204_338_291, 999_999_999)
        ] == ["999", "1.5K", "999.9K", "1M", "1.2B", "1B"]


class TestNarrowReads:
    def test_scope_at_each_level(self):
        assert session().scope() == (None, None)
        assert at_artist().scope() == ("Radiohead", None)
        assert at_album().scope() == ("Radiohead", "OK Computer")

    def test_tracks_delegates_to_the_cursor(self):
        files = [f for _, _, f in at_album().tracks()]

        assert files == ["Airbag.flac"]
