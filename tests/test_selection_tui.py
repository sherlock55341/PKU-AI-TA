from review.selection_tui import complete_path, normalize_assignment, normalize_course, prompt_manual_course_id


def test_normalize_course_extracts_display_fields():
    course = normalize_course(
        {
            "id": "_98024_1",
            "name": "Algorithm Design",
            "courseId": "CS101",
            "availability": {"available": "Yes"},
        }
    )

    assert course.id == "_98024_1"
    assert course.name == "Algorithm Design"
    assert course.course_code == "CS101"
    assert course.available == "Yes"


def test_normalize_assignment_derives_grade_book_pk_from_column_id():
    assignment = normalize_assignment(
        {"id": "_423829_1", "name": "Homework 1"},
        "bb",
    )

    assert assignment.id == "_423829_1"
    assert assignment.grade_book_pk == "423829"
    assert assignment.name == "Homework 1"
    assert assignment.source == "bb"


def test_normalize_assignment_builds_column_id_from_grade_book_pk():
    assignment = normalize_assignment(
        {"gradeBookPK": "423829", "name": "Homework 1"},
        "pku",
    )

    assert assignment.id == "_423829_1"
    assert assignment.grade_book_pk == "423829"


def test_complete_path_marks_directories_with_slash(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "rubric.md").write_text("rubric", encoding="utf-8")
    (tmp_path / "prompts").mkdir()

    assert complete_path("rub") == ["rubric.md"]
    assert complete_path("pro") == ["prompts/"]


def test_prompt_manual_course_id(monkeypatch):
    monkeypatch.setattr("review.selection_tui.Prompt.ask", lambda *args, **kwargs: "_98024_1")

    course = prompt_manual_course_id()

    assert course.id == "_98024_1"
    assert course.name == "_98024_1"
