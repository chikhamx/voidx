from voidx.agent.domain.profile import RuntimeProfile


def test_runtime_profile_with_prompt_policy_serializes_to_json() -> None:
    """Profiles carrying a PromptPolicy instance must dump cleanly in every mode."""
    import warnings

    from voidx.agent.domain.prompt_policy import ChatPromptPolicy

    profile = RuntimeProfile(
        profile_id="chat", revision=1, name="Chat", prompt_policy=ChatPromptPolicy()
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("error")
        dumped = profile.model_dump(mode="json")
        dumped_json = profile.model_dump_json()

    assert dumped["prompt_policy"] == "ChatPromptPolicy"
    assert '"prompt_policy":"ChatPromptPolicy"' in dumped_json
    assert caught == []


def test_runtime_profile_without_prompt_policy_serializes_none() -> None:
    profile = RuntimeProfile(profile_id="loop", revision=1, name="Loop")

    assert profile.model_dump(mode="json")["prompt_policy"] is None
    assert profile.model_dump()["prompt_policy"] is None


def test_thread_store_profile_json_roundtrip_with_policy() -> None:
    """_json_profile must serialize a policy-bearing profile and load it back."""
    import warnings

    from voidx.agent.domain.prompt_policy import CodingPromptPolicy
    from voidx.agent.adapters.persistence.thread_repository import _json_profile

    profile = RuntimeProfile(
        profile_id="coding", revision=1, name="Coding", prompt_policy=CodingPromptPolicy()
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("error")
        raw = _json_profile(profile)
        loaded = RuntimeProfile.model_validate_json(raw)

    assert loaded.profile_id == "coding"
    assert caught == []
