import pytest

from musicbot import loader


@pytest.fixture(scope="module")
def youtube_extractor():
    return loader._new_downloader().get_info_extractor("Youtube")


def test_youtube_extractor_reads_tv_fallback_after_default_clients(
    youtube_extractor,
):
    assert youtube_extractor._configuration_arg("player_client") == [
        "default",
        "tv",
    ]


def test_bgutil_provider_reads_its_base_url(youtube_extractor):
    got = youtube_extractor._configuration_arg(
        "base_url", ie_key="youtubepot-bgutilhttp"
    )
    assert got == ["http://bgutil-provider:4416"]
