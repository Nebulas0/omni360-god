from compatibility import compatibility_rule


def test_known_content_disc_maps_to_game_title() -> None:
    rule = compatibility_rule(
        "Batman - Arkham City - Game of the Year Edition (USA, Europe) (Disc 2)"
    )
    assert rule is not None
    assert rule.action == "content"
    assert rule.target_title_id == "57520802"


def test_known_multiplayer_disc_is_not_converted_to_god() -> None:
    rule = compatibility_rule(
        "Assassin's Creed IV - Black Flag (World) (Disc 2)"
    )
    assert rule is not None
    assert rule.action == "skip"


def test_unlisted_playable_disc_uses_generic_processing() -> None:
    assert compatibility_rule("Example Game (World) (Disc 2)") is None
