"""
PKU AI Teaching Assistant CLI

Commands:
  ta grade   --course <id> --column <id> --rubric <file> [--whitelist a,b,c] [--out scores.xlsx] [--verbose] [--resume] [--lang en|zh]
  ta list-assignments [--course <id>]
  ta review  [--scores scores.xlsx] [--submissions submissions/] [--needs-review] [--all]
  ta submit  --course <id> --column <id> --scores <reviewed.xlsx> [--dry-run]
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Annotated, Optional
from time import time
import re

import typer
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, BarColumn, TaskProgressColumn, TimeElapsedColumn, TextColumn

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
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show intermediate scores for each student")] = False,
    resume: Annotated[bool, typer.Option("--resume", "-r", help="Resume from previous partial run (if any)")] = False,
    regrade_unapproved: Annotated[bool, typer.Option("--regrade-unapproved", help="Keep approved students, only regrade those not approved")] = False,
    prompt: Annotated[Path, typer.Option(help="System prompt file for the LLM (default: prompts/system_en.md)")] = Path("prompts/system_en.md"),
) -> None:
    """Crawl submissions, score with LLM, export review spreadsheet.

    Press Ctrl-C to interrupt; partial results will be saved to the output file
    and can be resumed with --resume.

    Use --regrade-unapproved to keep already-approved students and only regrade
    those that haven't been approved yet.
    """
    from threading import Lock

    from auth.iaaa import get_session
    from config import settings
    from crawler.blackboard import BlackboardCrawler
    from crawler.pku_homework import PKUHomeworkCrawler
    from review.spreadsheet import export
    from scorer.llm import score_submission
    from models import ScoringResult

    # Checkpoint save/load using Excel format
    checkpoint_path = out
    all_results: list[ScoringResult] = []
    processed_ids: set[str] = set()
    save_lock = Lock()

    def save_checkpoint() -> None:
        """Save current progress to output Excel file."""
        with save_lock:
            if all_results:
                export(all_results, checkpoint_path)

    def load_checkpoint() -> tuple[list[ScoringResult], set[str]]:
        """Load previous progress from output Excel file (if exists and --resume or --regrade-unapproved is set)."""
        if (resume or regrade_unapproved) and checkpoint_path.exists():
            try:
                from review.spreadsheet import load_reviewed
                records = load_reviewed(checkpoint_path)

                if regrade_unapproved:
                    # Keep only approved students, others will be regraded
                    approved_results = [r.result for r in records if r.approved]
                    all_results_loaded = [r.result for r in records]
                    console.print(f"[bold cyan]Regrade mode:[/bold cyan] Loaded {len(all_results_loaded)} total, keeping {len(approved_results)} already-approved")
                    return approved_results, {r.student_id for r in approved_results}
                else:
                    # Normal resume: keep all previously processed
                    results = [r.result for r in records]
                    console.print(f"[bold cyan]Resuming from checkpoint:[/bold cyan] {len(results)} previously processed result(s)")
                    return results, {r.student_id for r in results}
            except Exception as e:
                console.print(f"[yellow]Warning: Could not load checkpoint: {e}[/yellow]")
        return [], set()

    # Load checkpoint if resuming or regrading unapproved
    unapproved_student_ids: set[str] = set()
    if resume or regrade_unapproved:
        all_results, processed_ids = load_checkpoint()
        if regrade_unapproved and checkpoint_path.exists():
            # For --regrade-unapproved, find students who are NOT approved
            # These are the ones we need to regrade
            try:
                from review.spreadsheet import load_reviewed
                all_records = load_reviewed(checkpoint_path)
                unapproved_student_ids = {r.result.student_id for r in all_records if not r.approved}
                console.print(f"[bold cyan]Regrade mode:[/bold cyan] Found {len(unapproved_student_ids)} unapproved student(s) to regrade")
            except Exception as e:
                console.print(f"[yellow]Warning: Could not determine unapproved students: {e}[/yellow]")
    else:
        all_results = []
        processed_ids = set()

    # Resolve config — CLI args override .env
    course_id = course or settings.course_id
    if not course_id:
        console.print("[red]Error:[/red] --course is required (or set COURSE_ID in .env)")
        raise typer.Exit(1)

    # Determine whitelist:
    # - If --regrade-unapproved: only regrade unapproved students
    # - Else: use CLI whitelist or settings whitelist
    if regrade_unapproved and unapproved_student_ids:
        whitelist_ids: set[str] = unapproved_student_ids
    else:
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

    pku_crawler = PKUHomeworkCrawler(client, course_id, whitelist_ids)
    bb_crawler = BlackboardCrawler(client, course_id, whitelist_ids)

    if column:
        grade_book_pk, column_id = _normalize_column_identifiers(column)
        assignment_title = column
        source = "pku"
        try:
            assignments = pku_crawler.fetch_assignments()
            for assignment in assignments:
                if assignment.get("gradeBookPK") == grade_book_pk or assignment.get("id") == column_id:
                    assignment_title = assignment.get("name") or assignment_title
                    break
            else:
                source = "bb"
        except Exception:
            # If the PKU homework plugin listing fails, try standard Blackboard.
            source = "bb"

        if source == "bb":
            try:
                assignments = bb_crawler.fetch_assignments()
                for assignment in assignments:
                    if assignment.get("id") == column_id:
                        assignment_title = assignment.get("name") or assignment.get("title") or assignment_title
                        break
            except Exception:
                pass

        columns = [{"gradeBookPK": grade_book_pk, "name": assignment_title, "id": column_id, "source": source}]
    else:
        console.print("[bold]Step 1b:[/bold] Fetching assignment list…")
        columns = pku_crawler.fetch_assignments()
        console.print(f"  Found {len(columns)} assignment(s).")

    interrupted = False
    start_time = time()

    try:
        for col in columns:
            grade_book_pk = col.get("gradeBookPK") or col["id"].strip("_").split("_")[0]
            col_title = col.get("name") or col["id"]
            source = col.get("source", "pku")
            console.print(f"\n[bold]Step 2/3:[/bold] Fetching submissions for [cyan]{col_title}[/cyan]…")

            if source == "bb":
                submissions = bb_crawler.fetch_submissions(col["id"], col_title)
            else:
                submissions = pku_crawler.fetch_submissions(grade_book_pk, col_title)

                if column and not submissions:
                    try:
                        bb_submissions = bb_crawler.fetch_submissions(col["id"], col_title)
                    except Exception:
                        bb_submissions = []
                    if bb_submissions:
                        submissions = bb_submissions
                        source = "bb"
                        console.print("  [dim]No PKU plugin submissions found; using Blackboard assignment attempts instead.[/dim]")

            if not submissions:
                console.print("  No submissions found.")
                continue

            # Filter out already graded submissions (from PKU website)
            already_graded = [s for s in submissions if s.already_graded]
            if already_graded:
                console.print(f"  [dim]Skipping {len(already_graded)} already-graded submission(s):[/dim]")
                for s in already_graded:
                    console.print(f"    [dim]{s.student_id} {s.student_name}[/dim]")
                submissions = [s for s in submissions if not s.already_graded]
                if not submissions:
                    console.print("  No ungraded submissions left to process.")
                    continue

            # Filter out already processed submissions if resuming
            if processed_ids:
                submissions = [s for s in submissions if s.student_id not in processed_ids]
                if not submissions:
                    console.print("  All submissions already processed.")
                    continue
                console.print(f"  {len(submissions)} submission(s) remaining to process")

            if save_dir:
                _save_submissions(submissions, save_dir, col_title)
                console.print(f"  Saved files → [cyan]{save_dir / col_title}[/cyan]")

            total_submissions = len(submissions)
            console.print(f"  Scoring {total_submissions} submission(s) with LLM (threads={settings.ta_threads}, prompt={prompt.name})…")
            console.print(f"  [dim]Press Ctrl-C to interrupt — progress will be saved[/dim]")

            # Use transient=False for verbose mode so results stay on screen
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                TimeElapsedColumn(),
                TextColumn("[dim]ETA: {task.fields[eta]}"),
                console=console, transient=not verbose,
            ) as progress:
                task = progress.add_task("  Scoring", total=total_submissions, eta="calculating...")
                completed_count = 0

                with ThreadPoolExecutor(max_workers=settings.ta_threads) as executor:
                    futures = {executor.submit(score_submission, sub, rubric_text, prompt): sub for sub in submissions}
                    for future in as_completed(futures):
                        sub = futures[future]
                        try:
                            result = future.result()
                            all_results.append(result)
                            processed_ids.add(result.student_id)
                            completed_count += 1

                            # Calculate ETA
                            if completed_count >= 2:
                                elapsed = time() - start_time
                                avg_time_per = elapsed / completed_count
                                remaining = (total_submissions - completed_count) * avg_time_per
                                if remaining < 60:
                                    eta_str = f"{remaining:.0f}s"
                                elif remaining < 3600:
                                    eta_str = f"{remaining/60:.1f}m"
                                else:
                                    eta_str = f"{remaining/3600:.1f}h"
                                progress.update(task, eta=eta_str)
                            else:
                                progress.update(task, eta="...")

                            # Save checkpoint after each result for safety
                            save_checkpoint()

                            if verbose:
                                # Show verbose output for each student
                                needs_review = result.needs_review
                                color = "yellow" if needs_review else "green"
                                status = "NEEDS_REVIEW" if needs_review else "OK"
                                console.print(
                                    f"  [{color}]{result.student_id:12s}[/] {result.student_name:10s} "
                                    f"→ {result.total_score:3.0f}/{result.total_max:3.0f} ({result.pct:3.0f}%) "
                                    f"[{color}]{status}[/]"
                                )
                        except Exception as e:
                            console.print(
                                f"  [red]Error scoring {sub.student_id}:[/red] {_summarize_error(e)}"
                            )
                        finally:
                            progress.advance(task)

    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user[/yellow]")
        interrupted = True
        if all_results:
            console.print(f"[yellow]Saving {len(all_results)} partial result(s)...[/yellow]")
            save_checkpoint()
            console.print(f"[cyan]Checkpoint saved to {checkpoint_path}[/cyan]")
            console.print(f"[cyan]Resume later with: --resume[/cyan]")
        raise typer.Exit(1)

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
def list_assignments(
    course: Annotated[str, typer.Option(help="Blackboard course ID, e.g. _12345_1")] = "",
) -> None:
    """List all assignments in the course with their gradeBookPK values."""
    from auth.iaaa import get_session
    from config import settings
    from crawler.pku_homework import PKUHomeworkCrawler

    course_id = course or settings.course_id
    if not course_id:
        console.print("[red]Error:[/red] --course is required (or set COURSE_ID in .env)")
        raise typer.Exit(1)

    console.print("[bold]Authenticating with PKU IAAA…[/bold]")
    client = get_session()

    crawler = PKUHomeworkCrawler(client, course_id, whitelist=set())
    console.print("[bold]Fetching assignment list…[/bold]")
    assignments = crawler.fetch_assignments()

    if not assignments:
        console.print("[yellow]No assignments found.[/yellow]")
        raise typer.Exit(0)

    console.print(f"Found {len(assignments)} assignment(s):")
    for idx, assignment in enumerate(assignments, start=1):
        grade_book_pk = assignment.get("gradeBookPK", "")
        title = assignment.get("name") or assignment.get("id", "")
        col_id = assignment.get("id") or (f"_{grade_book_pk}_1" if grade_book_pk else "")
        console.print(f"{idx}. {title}")
        console.print(f"   gradeBookPK: {grade_book_pk}")
        console.print(f"   column id:   {col_id}")


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
    if not course_id:
        console.print("[red]Error:[/red] --course is required (or set COURSE_ID in .env).")
        raise typer.Exit(1)

    if not scores.exists():
        console.print(f"[red]Error:[/red] Scores file not found: {scores}")
        raise typer.Exit(1)

    records = load_reviewed(scores)
    assignment_ids = sorted({r.result.assignment_id for r in records if r.result.assignment_id})
    selected_column = column.strip()
    if not selected_column:
        if len(assignment_ids) == 1:
            selected_column = assignment_ids[0]
            console.print(f"[dim]Using assignment ID from spreadsheet:[/dim] {selected_column}")
        else:
            console.print("[red]Error:[/red] --column is required when the spreadsheet contains multiple assignments.")
            raise typer.Exit(1)

    _, selected_col_id = _normalize_column_identifiers(selected_column)
    filtered_records = [r for r in records if _column_matches_assignment(selected_col_id, r.result.assignment_id)]

    if not filtered_records and len(assignment_ids) == 1:
        spreadsheet_column = assignment_ids[0]
        _, spreadsheet_col_id = _normalize_column_identifiers(spreadsheet_column)
        console.print(
            f"[yellow]Warning:[/yellow] CLI --column {selected_column} does not match spreadsheet assignment "
            f"{spreadsheet_column}; using the spreadsheet assignment."
        )
        selected_col_id = spreadsheet_col_id
        filtered_records = records
    elif not filtered_records:
        console.print("[red]Error:[/red] No records in the spreadsheet match the requested --column.")
        raise typer.Exit(1)

    if len(filtered_records) != len(records):
        console.print(f"[yellow]Using {len(filtered_records)}/{len(records)} record(s) matching column {selected_col_id}.[/yellow]")

    approved_count = sum(1 for r in filtered_records if r.approved)
    console.print(f"Loaded {len(filtered_records)} record(s), {approved_count} approved.")

    if approved_count == 0:
        console.print("[yellow]Nothing to submit — no records marked approved.[/yellow]")
        raise typer.Exit(0)

    approved_student_ids = [r.result.student_id for r in filtered_records if r.approved]
    duplicate_student_ids = sorted({sid for sid in approved_student_ids if approved_student_ids.count(sid) > 1})
    if duplicate_student_ids:
        console.print(
            "[red]Error:[/red] Duplicate approved student IDs found in the spreadsheet: "
            + ", ".join(duplicate_student_ids)
        )
        console.print("[yellow]Regenerate the scores file or remove duplicate rows before submitting.[/yellow]")
        raise typer.Exit(1)

    console.print("[bold]Authenticating with PKU IAAA…[/bold]")
    client = get_session()

    submit_scores(client, course_id, selected_col_id, filtered_records, dry_run=dry_run)


@app.command()
def review(
    scores: Annotated[Path, typer.Option(help="Excel spreadsheet to review")] = Path("scores.xlsx"),
    submissions: Annotated[Path, typer.Option(help="Directory with submission files")] = Path("submissions"),
    rubric: Annotated[Path, typer.Option(help="Path to rubric file to open during review")] = Path("rubric.md"),
    needs_review_only: Annotated[bool, typer.Option("--needs-review", "-n", help="Only review students marked needs_review=YES")] = False,
    all_students: Annotated[bool, typer.Option("--all", "-a", help="Review all students (including already approved)")] = False,
    auto_approve: Annotated[bool, typer.Option("--auto-approve", help="Auto-approve 100-point submissions that don't need review")] = False,
    auto_approve_safe: Annotated[bool, typer.Option("--auto-approve-safe", help="Auto-approve all submissions with needs_review=NO")] = False,
) -> None:
    """Interactive TUI for reviewing submissions one by one.

    Shows score breakdown, opens submission file, and lets you approve or override scores.
    Press 'e' to edit individual criterion scores, 'r' to open the rubric, 'b' to go back.

    Use --auto-approve to automatically approve students with 100/100 and needs_review=NO.
    Use --auto-approve-safe to automatically approve all students with needs_review=NO.
    """
    from review.tui import run_review_tui

    try:
        run_review_tui(
            console=console,
            scores=scores,
            submissions=submissions,
            rubric=rubric,
            needs_review_only=needs_review_only,
            all_students=all_students,
            auto_approve=auto_approve,
            auto_approve_safe=auto_approve_safe,
        )
    except FileNotFoundError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)
    except typer.Exit:
        raise
    except SystemExit:
        raise
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


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


def _normalize_column_identifiers(column: str) -> tuple[str, str]:
    """Accept either numeric gradeBookPK or Blackboard `_423829_1` column ID."""
    value = column.strip()
    if re.fullmatch(r"\d+", value):
        return value, f"_{value}_1"

    match = re.fullmatch(r"_(\d+)_\d+", value)
    if match:
        grade_book_pk = match.group(1)
        return grade_book_pk, value

    return value, value if value.startswith("_") else f"_{value}_1"


def _column_matches_assignment(column_id: str, assignment_id: str) -> bool:
    _, normalized_assignment = _normalize_column_identifiers(assignment_id)
    return normalized_assignment == column_id


def _summarize_error(exc: Exception, limit: int = 240) -> str:
    text = " ".join(str(exc).split())
    if "<!DOCTYPE html" in text:
        status_match = re.search(r"\b(\d{3})\b", text)
        status = status_match.group(1) if status_match else "unknown"
        if "bad gateway" in text.lower():
            text = f"upstream API returned HTTP {status} Bad Gateway"
        else:
            text = f"upstream API returned HTML error page (HTTP {status})"
    return text[:limit] + ("..." if len(text) > limit else "")


if __name__ == "__main__":
    app()
