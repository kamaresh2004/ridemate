"""Offline all-India gazetteer: coverage, aliases, ranking.

The dataset ships with the repo (backend/data/places_india.bin.gz), so these
tests are deterministic and never touch the network.
"""

import pytest

from backend import places as gazetteer


@pytest.fixture(scope="module", autouse=True)
def _index():
    gazetteer.reload()


def labels(query, limit=8):
    return [item["label"] for item in gazetteer.search_places(query, limit)]


def test_dataset_covers_every_state_and_union_territory():
    assert gazetteer.dataset_size() > 400000
    states = gazetteer.states()
    assert {"Tamil Nadu", "Kerala", "Karnataka", "Andhra Pradesh",
            "Telangana", "Puducherry", "Bihar", "Rajasthan", "Uttar Pradesh",
            "Punjab", "West Bengal", "Assam", "Ladakh",
            "Andaman and Nicobar"} <= states
    assert len(states) >= 36


def test_dataset_covers_districts_and_taluks_everywhere():
    # Districts (ADM2) and taluks/tehsils (ADM3) are indexed for the whole
    # country, so typing one resolves without a network call.
    assert len(gazetteer.districts()) >= 700
    for query, expected in [
        ("Patna", "Bihar"),
        ("Jaipur", "Rajasthan"),
        ("Lucknow", "Uttar Pradesh"),
        ("Bhopal", "Madhya Pradesh"),
        ("Raipur", "Chhattisgarh"),
        ("Ranchi", "Jharkhand"),
        ("Guwahati", "Assam"),
        ("Bhubaneswar", "Odisha"),
        ("Dehradun", "Uttarakhand"),
        ("Shimla", "Himachal Pradesh"),
        ("Leh", "Ladakh"),
    ]:
        found = gazetteer.search_places(query, limit=3)
        assert found, f"{query} produced no suggestions"
        # The label prefers the district ("Lucknow, Lucknow District"); the
        # full address is what proves the right state was picked.
        assert expected.casefold() in found[0]["address"].casefold(), (query, found[0])


@pytest.mark.parametrize("query, expected", [
    ("Thanjavur", "Thanjavur"),
    ("Kumbakonam", "Kumbakonam"),
    ("Karaikudi", "Karaik"),      # spellings differ upstream; squashed match
    ("Ooty", "Ooty"),
    ("Kozhikode", "Kozhikode"),
    ("Kollam", "Kollam"),
    ("Vijayawada", "Vijayawada"),
    ("Warangal", "Warangal"),
    ("Puducherry", "Puducherry"),
    ("Shivamogga", "Shivamogga"),
    ("Anantapur", "Anantapur"),
    ("Kanniyakumari", "Kanniyakumari"),
])
def test_main_town_in_every_region_is_suggested(query, expected):
    found = labels(query)
    assert found, f"{query} produced no suggestions"
    assert expected.casefold() in found[0].casefold()


def test_local_spelling_variants_are_matched():
    # "Trichy" and "Tanjore" are the names people actually type.
    assert any("Tiruchirappalli" in label for label in labels("Trichy"))
    assert any("Thanjavur" in label for label in labels("Tanjore"))
    assert any("Kanniyakumari" in label for label in labels("Kanyakumari"))


def test_doubled_letters_do_not_hide_the_real_town():
    # Kaggle-style spelling of Karaikkudi; the big Sivaganga town must win over
    # a tiny same-named hamlet elsewhere.
    top = labels("Karaikudi")[0]
    assert "Sivaganga" in top


def test_suggestion_label_carries_the_district():
    top = labels("Kumbakonam")[0]
    assert top == "Kumbakonam, Thanjavur"


def test_a_name_typed_in_full_is_confident_but_a_poi_query_is_not():
    assert gazetteer.has_confident_match("Thanjavur", gazetteer.search_places("Thanjavur"))
    assert not gazetteer.has_confident_match(
        "Thanjavur new bus stand", gazetteer.search_places("Thanjavur new bus stand"))


def test_unknown_names_return_nothing_instead_of_noise():
    assert gazetteer.search_places("zzzqqqxxx") == []
