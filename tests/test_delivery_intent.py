from onebrief.delivery_intent import (
    construction_directives,
    requires_new_product_construction,
)


def test_detects_explicit_new_client_construction_without_matching_maintenance() -> None:
    explicit = "Build a new Unity client presentation layer while reusing server assets."
    korean = "서버와 이미지 자산은 재사용하되 Unity 클라이언트는 새롭게 제작한다."

    assert requires_new_product_construction(explicit)
    assert requires_new_product_construction(korean)
    assert construction_directives(explicit) == [explicit]
    assert not requires_new_product_construction(
        "Modernize the existing Unity client and add a new behavior test to the existing UI."
    )
    assert requires_new_product_construction(
        "A newly constructed Login surface reaches a new Lobby shell."
    )
