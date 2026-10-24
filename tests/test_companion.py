from app.db.models import Message, User
from app.services.companion import (
    Companion,
    build_system_prompt,
    history_to_messages,
    split_photo_tag,
)


def make_user(**overrides) -> User:
    fields = dict(
        id=1,
        first_name="Alex",
        display_name="Sasha",
        level="A2",
        mode="chat",
        persona="uk_casual",
        voice_always=False,
        context_start_id=0,
    )
    fields.update(overrides)
    return User(**fields)


def test_system_prompt_uses_persona_level_and_mode() -> None:
    prompt = build_system_prompt(make_user(mode="correct"))
    assert prompt.startswith("You are Emma, a friendly native English speaker from Manchester")
    assert "chatting in a messenger with Sasha" in prompt
    assert "level ~A2" in prompt
    assert "short sentences" in prompt  # гайдлайн A2
    assert "✏️ wrong fragment → correct fragment" in prompt
    assert "Naturally" not in prompt  # фаза 2: блок review_items пустой


def test_system_prompt_unknown_level_falls_back_to_b1() -> None:
    prompt = build_system_prompt(make_user(level="Z9"))
    assert "level ~B1" in prompt


def test_system_prompt_with_review_items() -> None:
    prompt = build_system_prompt(make_user(), review_items=["blockbuster", "I went"])
    assert "blockbuster, I went" in prompt


def test_split_photo_tag() -> None:
    desc, text = split_photo_tag("[photo: a cat on a sofa]\nAww, cute! Is it yours?")
    assert desc == "a cat on a sofa"
    assert text == "Aww, cute! Is it yours?"


def test_split_photo_tag_absent() -> None:
    assert split_photo_tag("  Hello there!  ") == (None, "Hello there!")


def test_history_to_messages_renders_photos_and_skips_empty() -> None:
    history = [
        Message(role="user", kind="photo", text="my dog", photo_caption="a dog on grass"),
        Message(role="assistant", kind="text", text="So cute!"),
        Message(role="user", kind="voice", text=""),
        Message(role="user", kind="photo", text=None, photo_caption=None),
    ]
    assert history_to_messages(history) == [
        {"role": "user", "content": "[photo: a dog on grass]\nmy dog"},
        {"role": "assistant", "content": "So cute!"},
        {"role": "user", "content": "[photo: a photo]"},
    ]


def test_build_messages_with_summary_and_image(settings) -> None:
    companion = Companion(llm=None, settings=settings)  # type: ignore[arg-type]
    messages = companion.build_messages(
        make_user(),
        [Message(role="assistant", kind="text", text="Hi!")],
        "Sasha likes football.",
        text=None,
        image=b"\xff\xd8jpeg",
    )
    assert [m["role"] for m in messages] == ["system", "system", "assistant", "user"]
    assert messages[1]["content"].endswith("Sasha likes football.")
    content = messages[-1]["content"]
    assert content[0] == {"type": "text", "text": "(sent a photo without a caption)"}
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
