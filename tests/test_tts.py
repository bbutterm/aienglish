import shutil
import subprocess

import pytest

from app.bot.handlers.common import format_reply_html
from app.services.tts import ensure_ogg_opus, prepare_tts_text


def test_prepare_strips_correction_line_and_markdown() -> None:
    text = "✏️ I go → I went\nOh *nice*! What did you   watch?"
    assert prepare_tts_text(text, 400) == "Oh nice! What did you watch?"


def test_prepare_cuts_at_sentence_boundary() -> None:
    text = "First sentence here. Second sentence is a bit longer. Third one."
    assert prepare_tts_text(text, 40) == "First sentence here."


def test_prepare_cuts_at_word_boundary_when_no_sentence() -> None:
    text = "word " * 30
    result = prepare_tts_text(text, 23)
    assert result == "word word word word…"


def test_format_reply_html_escapes_and_italicises_corrections() -> None:
    html = format_reply_html("✏️ a → b\n<b>Nice</b> & cool")
    assert html == "<i>✏️ a → b</i>\n&lt;b&gt;Nice&lt;/b&gt; &amp; cool"


async def test_ensure_ogg_passthrough() -> None:
    data = b"OggS" + b"\x01" * 10
    assert await ensure_ogg_opus(data) is data


@pytest.fixture
def mp3_sample() -> bytes:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed")
    command = "ffmpeg -hide_banner -loglevel error -f lavfi -i sine=duration=1 -f mp3 pipe:1"
    return subprocess.run(command.split(), capture_output=True, check=True).stdout


async def test_ensure_ogg_converts_mp3_with_ffmpeg(mp3_sample: bytes) -> None:
    ogg = await ensure_ogg_opus(mp3_sample)
    assert ogg[:4] == b"OggS"
    assert b"OpusHead" in ogg[:200]
