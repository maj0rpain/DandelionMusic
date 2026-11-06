from musicbot import loader


def _youtube_extractor():
    return loader._new_downloader().get_info_extractor("Youtube")


def test_youtube_extractor_reads_tv_fallback_after_default_clients():
    ie = _youtube_extractor()
    assert ie._configuration_arg("player_client") == ["default", "tv"]


def test_bgutil_provider_reads_its_base_url():
    ie = _youtube_extractor()
    got = ie._configuration_arg("base_url", ie_key="youtubepot-bgutilhttp")
    assert got == ["http://bgutil-provider:4416"]
