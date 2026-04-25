from rich.console import Group

from review.tui_components import _navigate_editor_renderable, apply_batch_scores, calculate_breakdown_totals


def make_breakdown():
    return [
        {"criterion": "P1", "points_awarded": 10.0, "points_max": 10.0, "reasoning": ""},
        {"criterion": "P2", "points_awarded": 10.0, "points_max": 10.0, "reasoning": ""},
        {"criterion": "P3", "points_awarded": 10.0, "points_max": 10.0, "reasoning": ""},
    ]


def test_apply_batch_scores_with_explicit_indices():
    updated, errors = apply_batch_scores(make_breakdown(), "1=8,3=0")

    assert errors == []
    assert [item["points_awarded"] for item in updated] == [8.0, 10.0, 0.0]


def test_apply_batch_scores_with_ordered_values_and_blanks():
    updated, errors = apply_batch_scores(make_breakdown(), "8,,6")

    assert errors == []
    assert [item["points_awarded"] for item in updated] == [8.0, 10.0, 6.0]


def test_apply_batch_scores_rejects_out_of_range():
    updated, errors = apply_batch_scores(make_breakdown(), "2=11")

    assert updated[1]["points_awarded"] == 10.0
    assert errors == ["Score for criterion 2 must be between 0 and 10.0"]


def test_calculate_breakdown_totals():
    updated, _ = apply_batch_scores(make_breakdown(), "1=8,2=6,3=4")

    assert calculate_breakdown_totals(updated) == (18.0, 30.0)


def test_navigate_editor_renderable_builds_group():
    renderable = _navigate_editor_renderable(make_breakdown(), selected=1, message="[green]ok[/green]")

    assert isinstance(renderable, Group)
