# PKU AI Teaching Assistant

Automatically grades student homework submissions from [course.pku.edu.cn](https://course.pku.edu.cn) (Blackboard Learn) using an LLM, exports results to Excel for human review, and submits approved scores back to the platform.

**Workflow:** crawl submissions → LLM scores against rubric → review in Excel → submit scores

---

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager
- A [OpenRouter](https://openrouter.ai) API key (or any OpenAI-compatible endpoint)
- PKU IAAA credentials (student/staff ID + password)

---

## Setup

**1. Clone and install**

```bash
git clone <repo-url>
cd PKU-AI-TA
uv sync
```

**2. Configure credentials**

```bash
cp .env.example .env
# Edit .env with your credentials
```

Key variables in `.env`:

| Variable | Description |
|---|---|
| `OPENAI_API_KEY` | Your OpenRouter API key |
| `TA_MODEL` | Model to use, e.g. `qwen/qwen3.5-397b-a17b` |
| `PKU_USERNAME` | Your PKU student/staff ID |
| `PKU_PASSWORD` | Your PKU password |
| `COURSE_ID` | Blackboard course ID, e.g. `_98024_1` (from the course URL) |

**3. Prepare your rubric**

Create `rubric.md` describing the scoring criteria. Example:

```markdown
# Homework 1 Rubric (100 points)

## Problem 1.2 (12 pts)
- Part 1 (6 pts): correct answer = full marks
- Part 2 (6 pts): correct answer = full marks; only worst-case = 3 pts

## Problem 1.6 (12 pts)
- Correct algorithm (4 pts)
- Correct multiplication count (4 pts)
- Correct addition count (4 pts)
```

**4. Prepare your student list** (optional — for targeting specific students)

```
# student_list  (one ID per line)
2300012345
2300012346
2300012347
```

---

## Usage

### Step 1 — Grade

Crawl submissions, score with LLM, and export a review spreadsheet:

```bash
# Grade all students in the course
uv run python main.py grade --course _98024_1 --column 423829 --rubric rubric.md

# Grade only students in your whitelist file
uv run python main.py grade \
  --course _98024_1 \
  --column 423829 \
  --rubric rubric.md \
  --whitelist $(cat student_list | tr '\n' ',' | sed 's/,$//') \
  --out scores.xlsx
```

| Flag | Description |
|---|---|
| `--course` | Blackboard course ID (or set `COURSE_ID` in `.env`) |
| `--column` | Assignment `gradeBookPK` — the numeric ID in the `getStudentWork.do` URL |
| `--rubric` | Path to your rubric Markdown file |
| `--whitelist` | Comma-separated student IDs to grade; omit to grade everyone |
| `--out` | Output Excel file (default: `scores.xlsx`) |

This produces `scores.xlsx`. Rows highlighted **yellow** have low LLM confidence and need manual review.

> **Finding `--course` (course ID):**
> Navigate to any page of your course on course.pku.edu.cn. The URL contains `course_id=_98024_1` — copy that value including the underscores, e.g. `_98024_1`.
>
> **Finding `--column` (assignment ID):**
> Go to the homework list, click **查看** next to any assignment. The URL of the student list page looks like:
> ```
> …/getStudentWork.do?course_id=_98024_1&gradeBookPK=423829&title=第一次作业
> ```
> Copy the bare number after `gradeBookPK=`, e.g. `423829`.

### Step 2 — Review

Open `scores.xlsx`. For each student you want to finalise:

1. Check the LLM's `breakdown_json` and `llm_reasoning` columns.
2. Optionally override the score in `reviewer_override_score`.
3. Add notes in `reviewer_notes`.
4. Set `approved` to **YES**.

Rows without `approved = YES` are never submitted.

### Step 3 — Submit

Push approved scores back to Blackboard:

```bash
uv run python main.py submit \
  --course _98024_1 \
  --column 423829 \
  --scores scores.xlsx
```

Use `--dry-run` to preview what would be submitted without posting anything:

```bash
uv run python main.py submit --course _98024_1 --column 423829 --scores scores.xlsx --dry-run
```

---

## How submissions are handled

| File type | How it's processed |
|---|---|
| Text-embedded PDF | Text extracted with `pypdf`, sent to LLM as text |
| Scanned PDF | Original JPEG/PNG images extracted from PDF via `pymupdf`, sent as images |
| Submitted JPEG/PNG | Sent directly as images |
| Word (.docx) | Text extracted with `python-docx` |

The LLM is instructed to give students the benefit of the doubt and state explicit reasons for every point deducted.

---

## Development

```bash
# Install with dev dependencies
uv sync --extra dev

# Run tests
uv run pytest tests/ -v
```
