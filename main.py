"""
PKU AI Teaching Assistant CLI

Commands:
  ta grade   --course <id> --column <id> --rubric <file> [--whitelist a,b,c] [--out scores.xlsx]
  ta review  [--scores scores.xlsx] [--submissions submissions/] [--needs-review] [--all]
  ta submit  --course <id> --column <id> --scores <reviewed.xlsx> [--dry-run]
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, BarColumn, TaskProgressColumn, TimeElapsedColumn

app = typer.Typer(help="PKU AI Teaching Assistant")
console = Console()


@app.command()
def grade(
    course: Annotated[str, typer.Option(help="Blackboard course ID, e.g. _12345_1")] = "",
    column: Annotated[str, typer.Option(help="Gradebook column (assignment) ID")] = "",
    rubric: Annotated[Path, typer.Option(help="Path to rubric file (any format the LLM supports)")] = Path("rubric.md"),
    whitelist: Annotated[str, typer.Option(help="Comma-separated student IDs to include; empty = all")] = "",
    out: Annotated[Path, typer.Option(help="Output Excel path")] = Path("scores.xlsx"),
    save_dir: Annotated[Optional[Path], typer.Option(help="Save submission files here for human review; default: submissions/")] = Path("submissions"),
) -> None:
    """Crawl submissions, score with LLM, export review spreadsheet."""
    from auth.iaaa import get_session
    from config import settings
    from crawler.pku_homework import PKUHomeworkCrawler
    from review.spreadsheet import export
    from scorer.llm import score_submission

    # Resolve config — CLI args override .env
    course_id = course or settings.course_id
    if not course_id:
        console.print("[red]Error:[/red] --course is required (or set COURSE_ID in .env)")
        raise typer.Exit(1)

    whitelist_ids: set[str] = (
        {s.strip() for s in whitelist.split(",") if s.strip()}
        if whitelist
        else settings.whitelist_ids
    )

    if not rubric.exists():
        console.print(f"[red]Error:[/red] Rubric file not found: {rubric}")
        raise typer.Exit(1)

    rubric_text = rubric.read_text(encoding="utf-8")

    console.print("[bold]Step 1/3:[/bold] Authenticating with PKU IAAA…")
    client = get_session()

    crawler = PKUHomeworkCrawler(client, course_id, whitelist_ids)

    if column:
        # column here is expected to be gradeBookPK (numeric), e.g. "423829"
        columns = [{"gradeBookPK": column, "name": column, "id": f"_{column}_1"}]
    else:
        console.print("[bold]Step 1b:[/bold] Fetching assignment list…")
        columns = crawler.fetch_assignments()
        console.print(f"  Found {len(columns)} assignment(s).")

    all_results = []
    for col in columns:
        grade_book_pk = col.get("gradeBookPK") or col["id"].strip("_").split("_")[0]
        col_title = col.get("name") or col["id"]
        console.print(f"\n[bold]Step 2/3:[/bold] Fetching submissions for [cyan]{col_title}[/cyan]…")

        submissions = crawler.fetch_submissions(grade_book_pk, col_title)
        if not submissions:
            console.print("  No submissions found.")
            continue

        if save_dir:
            _save_submissions(submissions, save_dir, col_title)
            console.print(f"  Saved files → [cyan]{save_dir / col_title}[/cyan]")

        console.print(f"  Scoring {len(submissions)} submission(s) with LLM (threads={settings.ta_threads})…")
        with Progress(
            SpinnerColumn(), BarColumn(), TaskProgressColumn(), TimeElapsedColumn(),
            console=console, transient=True,
        ) as progress:
            task = progress.add_task("  Scoring", total=len(submissions))
            with ThreadPoolExecutor(max_workers=settings.ta_threads) as executor:
                futures = {executor.submit(score_submission, sub, rubric_text): sub for sub in submissions}
                for future in as_completed(futures):
                    sub = futures[future]
                    try:
                        all_results.append(future.result())
                    except Exception as e:
                        console.print(f"  [red]Error scoring {sub.student_id}:[/red] {e}")
                    finally:
                        progress.advance(task)

    if not all_results:
        console.print("[yellow]No results to export.[/yellow]")
        raise typer.Exit(0)

    console.print(f"\n[bold]Step 3/3:[/bold] Exporting {len(all_results)} result(s) → [cyan]{out}[/cyan]")
    export(all_results, out)

    needs_review = sum(1 for r in all_results if r.needs_review)
    console.print(
        f"\n[green]Done.[/green] {needs_review}/{len(all_results)} submission(s) flagged for review "
        f"(highlighted in yellow in the spreadsheet)."
    )
    console.print(f"Review the spreadsheet, set [bold]approved[/bold] = YES, then run [bold]ta submit[/bold].")


@app.command()
def submit(
    course: Annotated[str, typer.Option(help="Blackboard course ID")] = "",
    column: Annotated[str, typer.Option(help="Gradebook column (assignment) ID")] = "",
    scores: Annotated[Path, typer.Option(help="Reviewed Excel spreadsheet")] = Path("scores.xlsx"),
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print what would be submitted without posting")] = False,
) -> None:
    """Submit approved scores from the reviewed spreadsheet back to course.pku.edu.cn."""
    from auth.iaaa import get_session
    from config import settings
    from review.spreadsheet import load_reviewed
    from submitter.blackboard import submit_scores

    course_id = course or settings.course_id
    if not course_id or not column:
        console.print("[red]Error:[/red] Both --course and --column are required.")
        raise typer.Exit(1)
    # BB REST API needs _423829_1 format; accept bare numeric gradeBookPK too
    col_id = column if column.startswith("_") else f"_{column}_1"

    if not scores.exists():
        console.print(f"[red]Error:[/red] Scores file not found: {scores}")
        raise typer.Exit(1)

    records = load_reviewed(scores)
    approved_count = sum(1 for r in records if r.approved)
    console.print(f"Loaded {len(records)} record(s), {approved_count} approved.")

    if approved_count == 0:
        console.print("[yellow]Nothing to submit — no records marked approved.[/yellow]")
        raise typer.Exit(0)

    console.print("[bold]Authenticating with PKU IAAA…[/bold]")
    client = get_session()

    submit_scores(client, course_id, col_id, records, dry_run=dry_run)


@app.command()
def review(
    scores: Annotated[Path, typer.Option(help="Excel spreadsheet to review")] = Path("scores.xlsx"),
    submissions: Annotated[Path, typer.Option(help="Directory with submission files")] = Path("submissions"),
    needs_review_only: Annotated[bool, typer.Option("--needs-review", "-n", help="Only review students marked needs_review=YES")] = False,
    all_students: Annotated[bool, typer.Option("--all", "-a", help="Review all students (including already approved)")] = False,
) -> None:
    """Interactive TUI for reviewing submissions one by one.

    Shows score breakdown, opens submission file, and lets you approve or override scores.
    """
    import json
    import os
    import subprocess
    import sys

    import openpyxl
    from rich.panel import Panel
    from rich.prompt import Confirm, Prompt
    from rich.table import Table
    from rich.text import Text

    def find_submission_file(submissions_dir: Path, student_id: str, student_name: str) -> Path | None:
        """Find submission file for a student by matching student_id in filename."""
        if not submissions_dir.exists():
            return None
        for assignment_dir in submissions_dir.iterdir():
            if not assignment_dir.is_dir() or assignment_dir.name.startswith("."):
                continue
            for f in assignment_dir.iterdir():
                if f.is_file() and not f.name.startswith(".") and student_id in f.name:
                    return f
        return None

    def open_file(filepath: Path) -> None:
        """Open file with default system viewer. Cross-platform support."""
        filepath_str = str(filepath)
        try:
            if sys.platform == "darwin":  # macOS
                subprocess.run(["open", filepath_str], check=False)
            elif sys.platform == "win32":  # Windows
                os.startfile(filepath_str)  # type: ignore
            else:  # Linux / Unix variants
                for opener in ["xdg-open", "gio", "gnome-open", "kde-open"]:
                    try:
                        subprocess.run([opener, filepath_str], check=False,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        break
                    except (subprocess.SubprocessError, FileNotFoundError):
                        continue
        except Exception as e:
            console.print(f"[yellow]Warning: Could not open file ({e})[/yellow]")
            console.print(f"[dim]Please open manually: {filepath_str}[/dim]")

    def display_student(row_data: dict, row_idx: int, total: int) -> None:
        """Display student info and score breakdown using Rich."""
        console.print()
        console.print(Panel.fit(
            f"[bold cyan]Student {row_idx}/{total}[/bold cyan]",
            title="Review Progress",
            border_style="blue"
        ))
        info_table = Table(show_header=False, box=None)
        info_table.add_row("[bold]Student ID:[/]", str(row_data["student_id"]))
        info_table.add_row("[bold]Name:[/]", str(row_data["student_name"]))
        info_table.add_row("[bold]Score:[/]", f"{row_data['total_score']} / {row_data['total_max']} ({row_data['pct']}%)")
        info_table.add_row("[bold]Confidence:[/]", f"{row_data['confidence']}")
        info_table.add_row("[bold]Needs review:[/]", "[yellow]YES[/yellow]" if row_data["needs_review"] == "YES" else "NO")
        info_table.add_row("[bold]Current approved:[/]", "[green]YES[/green]" if str(row_data.get("approved", "")).upper() == "YES" else "[red]NO[/red]")
        console.print(Panel(info_table, title="Student Info", border_style="cyan"))
        try:
            breakdown = json.loads(row_data.get("breakdown_json", "[]"))
            if breakdown:
                bd_table = Table(title="Score Breakdown")
                bd_table.add_column("Criterion", style="cyan")
                bd_table.add_column("Awarded", justify="right", style="green")
                bd_table.add_column("Max", justify="right")
                bd_table.add_column("Reasoning", style="dim")
                for item in breakdown:
                    awarded = float(item.get("points_awarded", 0))
                    max_p = float(item.get("points_max", 0))
                    style = "red" if awarded < max_p else "green"
                    bd_table.add_row(
                        str(item.get("criterion", "")),
                        Text(f"{awarded}", style=style),
                        f"{max_p}",
                        str(item.get("reasoning", ""))
                    )
                console.print(Panel(bd_table, border_style="green"))
        except json.JSONDecodeError:
            console.print("[yellow]Warning: Could not parse breakdown_json[/yellow]")
        try:
            uncertain = json.loads(row_data.get("uncertain_parts_json", "[]"))
            if uncertain:
                uc_table = Table(title="Uncertain Parts")
                uc_table.add_column("Description", style="yellow")
                uc_table.add_column("Suggested Score", justify="right")
                for item in uncertain:
                    uc_table.add_row(
                        str(item.get("description", "")),
                        f"{item.get('suggested_score', 0)} / {item.get('suggested_max', 0)}"
                    )
                console.print(Panel(uc_table, border_style="yellow"))
        except json.JSONDecodeError:
            pass
        if row_data.get("llm_reasoning"):
            console.print(Panel(
                Text(str(row_data["llm_reasoning"]), style="dim"),
                title="LLM Reasoning",
                border_style="dim"
            ))

    if not scores.exists():
        console.print(f"[red]Error:[/red] Scores file not found: {scores}")
        raise typer.Exit(1)

    console.print(f"[bold]Loading spreadsheet:[/bold] {scores}")
    wb = openpyxl.load_workbook(scores)
    ws = wb.active

    headers = [cell.value for cell in ws[1]]
    idx = {name: i for i, name in enumerate(headers)}

    rows: list[tuple[int, dict]] = []
    for row_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if not row[idx["student_id"]]:
            continue
        row_data = {name: row[i] if i < len(row) else None for name, i in idx.items()}
        if needs_review_only and row_data.get("needs_review") != "YES":
            continue
        if not all_students and str(row_data.get("approved", "")).upper() == "YES":
            continue
        rows.append((row_idx, row_data))

    if not rows:
        console.print("[yellow]No students to review with the current filters.[/yellow]")
        raise typer.Exit(0)

    console.print(f"[green]Found {len(rows)} student(s) to review.[/green]")
    modified = False

    for i, (row_idx, row_data) in enumerate(rows, start=1):
        display_student(row_data, i, len(rows))
        student_id = str(row_data["student_id"])
        student_name = str(row_data["student_name"])
        sub_file = find_submission_file(submissions, student_id, student_name)

        if sub_file:
            console.print(f"[bold blue]Opening submission:[/bold blue] {sub_file}")
            open_file(sub_file)
        else:
            console.print("[yellow]Warning: No submission file found[/yellow]")
        console.print()

        action = Prompt.ask(
            "[bold cyan]Action[/bold cyan]",
            choices=["a", "approve", "s", "skip", "o", "override", "q", "quit"],
            default="skip"
        )
        if action in ("q", "quit"):
            console.print("[yellow]Quitting...[/yellow]")
            break
        if action in ("a", "approve"):
            ws.cell(row=row_idx, column=idx["approved"] + 1, value="YES")
            modified = True
            console.print("[green]Marked as approved.[/green]")
        elif action in ("o", "override"):
            new_score = Prompt.ask("[bold cyan]Enter override score[/bold cyan]")
            if new_score:
                try:
                    score_val = float(new_score)
                    ws.cell(row=row_idx, column=idx["reviewer_override_score"] + 1, value=score_val)
                    ws.cell(row=row_idx, column=idx["approved"] + 1, value="YES")
                    modified = True
                    console.print(f"[green]Set override score: {score_val} and marked as approved.[/green]")
                except ValueError:
                    console.print("[red]Invalid score, skipping.[/red]")
        else:
            console.print("[dim]Skipped.[/dim]")

    if modified:
        console.print()
        if Confirm.ask("[bold cyan]Save changes to spreadsheet?[/bold cyan]", default=True):
            wb.save(scores)
            console.print(f"[green]Saved to {scores}[/green]")
        else:
            console.print("[yellow]Changes not saved.[/yellow]")
    else:
        console.print("[dim]No changes made.[/dim]")


def _save_submissions(submissions: list, save_dir: Path, assignment_title: str) -> None:
    """Save each student's attachment file to save_dir/assignment_title/ for human review."""
    import re
    safe_title = re.sub(r'[^\w\u4e00-\u9fff\-]', '_', assignment_title)
    dest = save_dir / safe_title
    dest.mkdir(parents=True, exist_ok=True)
    for sub in submissions:
        for att in sub.attachments:
            ext = Path(att.filename).suffix or ""
            # Filename: studentId_studentName.ext  (e.g. 2300012345_张三.pdf)
            safe_name = re.sub(r'[^\w\u4e00-\u9fff]', '_', sub.student_name)
            filename = f"{sub.student_id}_{safe_name}{ext}"
            (dest / filename).write_bytes(att.data)


if __name__ == "__main__":
    app()
