"""
Rich-based selectors for choosing courses, assignments, and workflow actions.
"""
from __future__ import annotations

import glob
import os
from dataclasses import dataclass
from pathlib import Path
import shutil

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from crawler.blackboard import BlackboardCrawler, fetch_courses
from crawler.pku_homework import PKUHomeworkCrawler


@dataclass(frozen=True)
class CourseOption:
    id: str
    name: str
    course_code: str
    available: str


@dataclass(frozen=True)
class AssignmentOption:
    id: str
    name: str
    grade_book_pk: str
    source: str


@dataclass(frozen=True)
class GradingWizardConfig:
    backend: str
    pku_username: str
    pku_password: str
    openai_base_url: str = ""
    openai_api_key: str = ""
    model: str = ""
    enable_thinking: bool = False
    ta_threads: int = 1
    rubric: Path = Path("rubric.md")
    prompt: Path = Path("prompts/system_en.md")
    whitelist: str = ""
    out: Path = Path("scores.xlsx")
    save_dir: Path | None = Path("submissions")
    verbose: bool = False


@dataclass(frozen=True)
class LoginWizardConfig:
    backend: str
    pku_username: str
    pku_password: str


@dataclass(frozen=True)
class PKUCredentials:
    pku_username: str
    pku_password: str


def prompt_pku_credentials(*, console: Console, defaults: object) -> PKUCredentials:
    """Collect PKU credentials without asking for a scoring backend."""
    pku_username = _prompt_required(
        "PKU username",
        default=str(getattr(defaults, "pku_username", "") or ""),
    )
    pku_password = _prompt_secret(
        "PKU password",
        existing=str(getattr(defaults, "pku_password", "") or ""),
    )
    return PKUCredentials(pku_username=pku_username, pku_password=pku_password)


def prompt_login_wizard_config(*, console: Console, defaults: object) -> LoginWizardConfig:
    """Collect only the settings needed before course and assignment selection."""
    console.print(Panel.fit("[bold cyan]Batch Grading Wizard[/bold cyan]", border_style="cyan"))
    backend = _prompt_backend(console)
    if backend == "codex-cli" and shutil.which("codex") is None:
        raise RuntimeError("Codex CLI was not found on PATH. Install or log in to Codex CLI before using this backend.")

    credentials = prompt_pku_credentials(console=console, defaults=defaults)
    return LoginWizardConfig(
        backend=backend,
        pku_username=credentials.pku_username,
        pku_password=credentials.pku_password,
    )


def prompt_grading_wizard_config(
    *,
    console: Console,
    defaults: object,
    backend: str,
    rubric_default: Path = Path("rubric.md"),
    prompt_default: Path = Path("prompts/system_en.md"),
    out_default: Path = Path("scores.xlsx"),
    save_dir_default: Path | None = Path("submissions"),
) -> GradingWizardConfig:
    """Collect settings needed after course and assignment selection."""
    console.print(Panel.fit("[bold cyan]Grading Settings[/bold cyan]", border_style="cyan"))
    openai_base_url = ""
    openai_api_key = ""
    model = ""
    enable_thinking = False
    if backend == "api-key":
        openai_base_url = Prompt.ask(
            "[bold cyan]API base URL[/bold cyan]",
            default=str(getattr(defaults, "openai_base_url", "") or "https://openrouter.ai/api/v1"),
        ).strip()
        openai_api_key = _prompt_secret(
            "API key",
            existing=str(getattr(defaults, "openai_api_key", "") or ""),
        )
        model = Prompt.ask(
            "[bold cyan]Model[/bold cyan]",
            default=str(getattr(defaults, "ta_model", "") or "qwen/qwen3.5-397b-a17b"),
        ).strip()
        enable_thinking = Confirm.ask("[bold cyan]Enable thinking tokens?[/bold cyan]", default=bool(getattr(defaults, "enable_thinking", False)))

    default_threads = "1" if backend == "codex-cli" else str(getattr(defaults, "ta_threads", 4) or 4)
    ta_threads = _prompt_int("Scoring threads", default=default_threads, minimum=1)
    rubric = _prompt_existing_path("Rubric file", rubric_default)
    prompt = _prompt_existing_path("System prompt file", prompt_default)
    whitelist = _prompt_whitelist()
    out = prompt_path("Output Excel path", default=out_default, must_exist=False)
    save_dir = prompt_path(
        "Submission save directory",
        default=str(save_dir_default or ""),
        must_exist=False,
        allow_empty=True,
    )
    verbose = Confirm.ask("[bold cyan]Print each scoring result as it finishes?[/bold cyan]", default=False)

    return GradingWizardConfig(
        backend=backend,
        pku_username=str(getattr(defaults, "pku_username", "") or ""),
        pku_password=str(getattr(defaults, "pku_password", "") or ""),
        openai_base_url=openai_base_url,
        openai_api_key=openai_api_key,
        model=model,
        enable_thinking=enable_thinking,
        ta_threads=ta_threads,
        rubric=rubric,
        prompt=prompt,
        whitelist=whitelist,
        out=out,
        save_dir=save_dir,
        verbose=verbose,
    )


def normalize_course(raw: dict) -> CourseOption:
    """Convert Blackboard course payloads into fields the selector can display."""
    availability = raw.get("availability", {})
    available = availability.get("available", "") if isinstance(availability, dict) else ""
    return CourseOption(
        id=str(raw.get("id") or ""),
        name=str(raw.get("name") or raw.get("courseId") or raw.get("id") or ""),
        course_code=str(raw.get("courseId") or ""),
        available=str(available or ""),
    )


def normalize_assignment(raw: dict, source: str) -> AssignmentOption:
    """Convert PKU plugin or Blackboard assignment metadata into selector fields."""
    assignment_id = str(raw.get("id") or "")
    grade_book_pk = str(raw.get("gradeBookPK") or _grade_book_pk_from_column_id(assignment_id))
    if not assignment_id and grade_book_pk:
        assignment_id = f"_{grade_book_pk}_1"
    return AssignmentOption(
        id=assignment_id,
        name=str(raw.get("name") or raw.get("title") or assignment_id or grade_book_pk),
        grade_book_pk=grade_book_pk,
        source=source,
    )


def select_course_and_assignment(
    *,
    console: Console,
    client: httpx.Client,
    default_course_id: str = "",
) -> tuple[CourseOption, AssignmentOption]:
    """Prompt for a course and assignment, using default_course_id as fallback."""
    console.print(Panel.fit("[bold cyan]PKU AI TA Interactive Selector[/bold cyan]", border_style="cyan"))
    course = select_course(console=console, client=client, default_course_id=default_course_id)
    assignments = fetch_assignment_options(client, course.id, console)
    if not assignments:
        raise RuntimeError(f"No assignments found for course {course.id}.")
    assignment = select_assignment(console, assignments)
    return course, assignment


def select_course(*, console: Console, client: httpx.Client, default_course_id: str = "") -> CourseOption:
    """Fetch and prompt for a Blackboard course."""
    courses: list[CourseOption] = []
    try:
        console.print("[bold]Fetching course list from Blackboard pages...[/bold]")
        courses = [c for c in (normalize_course(raw) for raw in fetch_courses(client)) if c.id]
    except Exception as exc:
        console.print(f"[yellow]Could not fetch course list: {_compact_error(exc)}[/yellow]")

    if courses:
        return _select_course_from_list(console, courses)

    if default_course_id and Confirm.ask(f"[bold cyan]Use configured COURSE_ID {default_course_id}?[/bold cyan]", default=True):
        return CourseOption(id=default_course_id, name=default_course_id, course_code="", available="")

    return prompt_manual_course_id()


def prompt_manual_course_id() -> CourseOption:
    """Prompt for a course_id copied from a Blackboard course URL."""
    manual_id = _prompt_required("Course ID", default="")
    return CourseOption(id=manual_id, name=manual_id, course_code="", available="")


def fetch_assignment_options(
    client: httpx.Client,
    course_id: str,
    console: Console | None = None,
) -> list[AssignmentOption]:
    """Fetch assignments from the PKU homework plugin and Blackboard fallback."""
    assignments: list[AssignmentOption] = []
    seen_ids: set[str] = set()

    def add(raw_items: list[dict], source: str) -> None:
        for raw in raw_items:
            option = normalize_assignment(raw, source)
            key = option.id or option.grade_book_pk
            if not key or key in seen_ids:
                continue
            seen_ids.add(key)
            assignments.append(option)

    try:
        if console:
            console.print("[bold]Fetching PKU homework assignments...[/bold]")
        add(PKUHomeworkCrawler(client, course_id, whitelist=set()).fetch_assignments(), "pku")
    except Exception as exc:
        if console:
            console.print(f"[yellow]PKU homework assignment list failed: {_compact_error(exc)}[/yellow]")

    try:
        if console:
            console.print("[bold]Fetching Blackboard gradebook columns...[/bold]")
        add(BlackboardCrawler(client, course_id, whitelist=set()).fetch_assignments(), "bb")
    except Exception as exc:
        if console:
            console.print(f"[yellow]Blackboard assignment list failed: {_compact_error(exc)}[/yellow]")

    return assignments


def select_assignment(console: Console, assignments: list[AssignmentOption]) -> AssignmentOption:
    """Prompt for one assignment from a list."""
    table = Table(title="Assignments")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Title", style="cyan")
    table.add_column("gradeBookPK")
    table.add_column("Column ID")
    table.add_column("Source")
    for idx, assignment in enumerate(assignments, start=1):
        table.add_row(
            str(idx),
            assignment.name,
            assignment.grade_book_pk,
            assignment.id,
            assignment.source,
        )
    console.print(table)
    selected = _prompt_index(console, len(assignments), "Assignment")
    return assignments[selected]


def prompt_action(console: Console) -> str:
    """Prompt for the next workflow action after course and assignment selection."""
    actions = [
        ("grade", "Grade selected assignment"),
        ("submit", "Submit approved scores for selected assignment"),
        ("review", "Review scores spreadsheet"),
        ("print", "Print selected IDs only"),
        ("quit", "Quit"),
    ]
    table = Table(title="Actions")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Action", style="cyan")
    table.add_column("Description")
    for idx, (name, description) in enumerate(actions, start=1):
        table.add_row(str(idx), name, description)
    console.print(table)
    selected = _prompt_index(console, len(actions), "Action")
    return actions[selected][0]


def _prompt_backend(console: Console) -> str:
    options = [
        ("codex-cli", "Use local Codex CLI login; no manual API key"),
        ("api-key", "Use OpenAI/OpenRouter/OpenAI-compatible API key"),
    ]
    table = Table(title="Scoring Backends")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Backend", style="cyan")
    table.add_column("Description")
    for idx, (name, description) in enumerate(options, start=1):
        table.add_row(str(idx), name, description)
    console.print(table)
    return options[_prompt_index(console, len(options), "Backend")][0]


def _prompt_required(label: str, default: str = "") -> str:
    while True:
        value = Prompt.ask(f"[bold cyan]{label}[/bold cyan]", default=default).strip()
        if value:
            return value


def _prompt_secret(label: str, existing: str = "") -> str:
    suffix = " [dim](leave blank to use configured value)[/dim]" if existing else ""
    while True:
        value = Prompt.ask(f"[bold cyan]{label}[/bold cyan]{suffix}", password=True, default="").strip()
        if value:
            return value
        if existing:
            return existing


def _prompt_int(label: str, *, default: str, minimum: int = 1) -> int:
    while True:
        value = Prompt.ask(f"[bold cyan]{label}[/bold cyan]", default=default).strip()
        try:
            parsed = int(value)
        except ValueError:
            continue
        if parsed >= minimum:
            return parsed


def _prompt_existing_path(label: str, default: Path) -> Path:
    while True:
        path = prompt_path(label, default=default, must_exist=True)
        if path.exists():
            return path


def _prompt_whitelist() -> str:
    mode = Prompt.ask(
        "[bold cyan]Student whitelist[/bold cyan] [dim](none/comma/file)[/dim]",
        choices=["none", "comma", "file"],
        default="none",
        show_choices=False,
    )
    if mode == "none":
        return ""
    if mode == "comma":
        return Prompt.ask("[bold cyan]Student IDs[/bold cyan] [dim](comma-separated)[/dim]", default="").strip()

    path = _prompt_existing_path("Whitelist file", Path("student_list"))
    ids = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return ",".join(ids)


def prompt_path(
    label: str,
    *,
    default: Path | str = "",
    must_exist: bool = False,
    allow_empty: bool = False,
) -> Path | None:
    """Prompt for a path using readline tab completion when available."""
    default_text = str(default)
    while True:
        raw = _input_with_path_completion(label, default_text)
        value = raw.strip() or default_text
        if not value and allow_empty:
            return None
        path = Path(os.path.expanduser(value))
        if not must_exist or path.exists():
            return path
        print(f"Path not found: {path}")


def complete_path(text: str) -> list[str]:
    """Return shell-like path completion candidates for readline."""
    expanded = os.path.expanduser(text)
    matches = glob.glob(expanded + "*")
    completions: list[str] = []
    for match in matches:
        suffix = "/" if os.path.isdir(match) else ""
        display = match + suffix
        if text.startswith("~"):
            home = os.path.expanduser("~")
            display = "~" + display[len(home):]
        completions.append(display)
    return sorted(completions)


def _input_with_path_completion(label: str, default: str = "") -> str:
    prompt = f"{label}"
    if default:
        prompt += f" [{default}]"
    prompt += ": "

    try:
        import readline
    except ImportError:
        return input(prompt)

    old_completer = readline.get_completer()
    old_delims = readline.get_completer_delims()

    def completer(text: str, state: int) -> str | None:
        options = complete_path(text)
        if state < len(options):
            return options[state]
        return None

    try:
        readline.set_completer_delims(" \t\n")
        readline.set_completer(completer)
        readline.parse_and_bind("tab: complete")
        return input(prompt)
    finally:
        readline.set_completer(old_completer)
        readline.set_completer_delims(old_delims)


def _select_course_from_list(console: Console, courses: list[CourseOption]) -> CourseOption:
    table = Table(title="Courses")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Name", style="cyan")
    table.add_column("Course ID")
    table.add_column("Code")
    table.add_column("Available")
    for idx, course in enumerate(courses, start=1):
        table.add_row(str(idx), course.name, course.id, course.course_code, course.available)
    console.print(table)
    selected = _prompt_index(console, len(courses), "Course")
    return courses[selected]


def _prompt_index(console: Console, count: int, label: str) -> int:
    choices = [str(i) for i in range(1, count + 1)] + ["q"]
    value = Prompt.ask(
        f"[bold cyan]{label} #[/bold cyan] [dim](q to quit)[/dim]",
        choices=choices,
        show_choices=False,
    )
    if value == "q":
        raise KeyboardInterrupt
    return int(value) - 1


def _grade_book_pk_from_column_id(column_id: str) -> str:
    parts = column_id.strip("_").split("_")
    return parts[0] if parts and parts[0].isdigit() else ""


def _compact_error(exc: Exception, limit: int = 160) -> str:
    text = " ".join(str(exc).split())
    return text[:limit] + ("..." if len(text) > limit else "")
