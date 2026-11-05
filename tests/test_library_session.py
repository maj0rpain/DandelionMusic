"""The browse session's draw protocol, without Discord.

Synchronous throughout, like the cursor's tests.
"""

import ast
from pathlib import Path

from musicbot import library, library_browse, library_session
from musicbot.library import LibrarySong
from musicbot.library_session import BrowseSession, stat_fields
from collections import namedtuple

Art = namedtuple("Art", "url data extension")
Ext = namedtuple(
    "Ext", "listeners playcount tags popularity followers " "release_date"
)
Enr = namedtuple("Enr", "stats art")


def song(title, **kw):
    return LibrarySong(
        filename=kw.get("filename", title + ".flac"),
        title=title,
        duration=kw.get("duration", 60),
        year=kw.get("year"),
        genre=kw.get("genre"),
        fmt="FLAC",
        bitrate=None,
        sample_rate=None,
        bit_depth=None,
    )


INDEX = {
    "Radiohead": {
        "OK Computer": [song("Airbag", year=1997, genre="Rock")],
        "Kid A": [song("Zoo")],
    },
    "The Beatles": {"Abbey Road": [song("Come Together")]},
    "Aphex Twin": {"Drukqs": [song("Avril")]},
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


EMBEDDED = Enr(None, Art(None, b"png", "png"))
URL = Enr(None, Art("http://x/y.jpg", None, None))


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
            Enr(Ext(5000, None, (), None, None, None), EMBEDDED.art),
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
        s = at_artist()
        s.landed(s.frame())
        s.offer_enrichment(s.enter_level(), EMBEDDED)
        # enrichment edit failed, then the user turns a page
        s.page_by(1)
        assert s.frame().attachments_change is True

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

    def test_enrichment_tags_override_genres(self):
        s = at_album()
        s.offer_enrichment(
            s.enter_level(),
            Enr(Ext(1500, None, ("a", "b"), 80, None, None), None),
        )
        f = s.frame()
        assert f.description == "*a · b*"
        assert dict(f.fields)["Listeners"] == "1.5K"
        assert dict(f.fields)["Popularity"] == "80/100"

    def test_long_names_are_trimmed(self):
        name = "x" * 300
        s = session({name: {name: [song("s")]}})
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
        # and the one allowed exception really is TYPE_CHECKING-only
        assert "from musicbot import library_metadata" in source
