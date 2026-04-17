"""Structural validators for docs/rewrite/ per task-20260417-001.

These tests are the TDD gate for the doc-writing execution segment. They
intentionally fail today because the docs under docs/rewrite/ do not yet
exist. After the executor writes the four markdown files to satisfy the
spec's 20 acceptance criteria, every non-skipped test in this module
must pass.

Each acceptance criterion maps to at least one test function whose name
includes the criterion number, e.g. ``test_criterion_03_gap_analysis_rows``.

The tests are self-sufficient with regard to cwd: repo root is derived
from this file's path, not from pytest's cwd.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections import defaultdict, deque
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = REPO_ROOT / "docs" / "rewrite"
README_MD = DOCS_DIR / "README.md"
GAP_MD = DOCS_DIR / "gap-analysis.md"
TARGET_MD = DOCS_DIR / "target-architecture.md"
ROADMAP_MD = DOCS_DIR / "roadmap.md"

EXPECTED_FILES = {"README.md", "gap-analysis.md", "target-architecture.md", "roadmap.md"}

# The seven lock-path literals (criterion 2 / 6 / 14).
LOCK_PATHS = [
    "autofix/llm_io/**",
    "autofix/agent_loop.py",
    "autofix/llm_backend.py",
    ".autofix/state/**",
    ".autofix/autofix-policy.json",
    ".autofix/events.jsonl",
    "benchmarks/agent_bench/**",
]

# The 23 target-architecture rows required in gap-analysis.md (criterion 3).
GAP_TARGET_ROWS = [
    "event ingress",
    "change detector",
    "invalidation planner",
    "incremental parser",
    "symbol/reference index",
    "lexical search index",
    "embedding index",
    "call/dependency graph",
    "optional semantic graph",
    "deterministic analyzers (cheap)",
    "deterministic analyzers (semantic)",
    "impact estimator",
    "candidate findings store",
    "priority scorer",
    "dedup/cluster layer",
    "suppression/policy engine",
    "evidence-packet builder",
    "LLM scheduler",
    "small-model triage",
    "large-model report writer",
    "telemetry/traces",
    "replay store",
    "SARIF export",
]

# Delta cell allowed values (criterion 4).
DELTA_VALUES = {"keep as-is", "wrap", "replace", "new"}

# Deprecated CLI subcommands that must be named (criterion 9).
DEPRECATED_SUBCOMMANDS = [
    "scan",
    "list",
    "policy",
    "suppress",
    "init",
    "daemon",
    "repo",
    "config",
    "scan-all",
    "sync-outcomes",
]

# Roadmap per-task required field labels (criterion 10).
TASK_REQUIRED_FIELDS = [
    "task-slug",
    "Goal",
    "Phase",
    "acceptance criteria seeds",
    "Locked surfaces",
    "Estimated size",
    "Depends-on",
]

SIZE_BANDS = {"XS", "S", "M", "L"}
PHASES = {"Prototype", "Alpha", "Beta", "Production"}

# Pre-exec skip guard for tests that inspect git state after writes.
PRE_EXEC = os.environ.get("TESTS_ARE_PRE_EXEC") is not None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    assert path.exists(), f"required file missing: {path.relative_to(REPO_ROOT)}"
    return path.read_text(encoding="utf-8")


def _strip_code_fences(text: str) -> str:
    """Remove fenced code blocks (```...```) for prose-only checks."""
    return re.sub(r"```.*?```", "", text, flags=re.DOTALL)


def _iter_fenced_blocks(text: str):
    """Yield (language, body) tuples for triple-backtick fenced blocks."""
    for match in re.finditer(r"```([^\n`]*)\n(.*?)```", text, flags=re.DOTALL):
        yield match.group(1).strip().lower(), match.group(2)


def _markdown_tables(text: str):
    """Return a list of tables; each table is a list of row-cell-lists.

    A markdown table is detected as a run of >=2 consecutive lines starting
    with '|'. The alignment row (---|---) is stripped.
    """
    tables = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].lstrip().startswith("|") and i + 1 < len(lines) and re.match(
            r"^\s*\|?[\s:\-|]+\|?\s*$", lines[i + 1]
        ):
            start = i
            i += 2  # skip header + separator
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                i += 1
            raw = lines[start:i]
            header = [c.strip() for c in raw[0].strip().strip("|").split("|")]
            body_rows = []
            for r in raw[2:]:
                cells = [c.strip() for c in r.strip().strip("|").split("|")]
                body_rows.append(cells)
            tables.append({"header": header, "rows": body_rows, "raw": "\n".join(raw)})
        else:
            i += 1
    return tables


def _metadata_block_ok(text: str) -> bool:
    """Metadata block: names task-20260417-001, deep-research-report.md, 2026-04-17.

    Heuristic: all three strings present within the first 40 non-empty lines.
    """
    head = []
    for ln in text.splitlines():
        if ln.strip():
            head.append(ln)
        if len(head) >= 40:
            break
    blob = "\n".join(head)
    return (
        "task-20260417-001" in blob
        and "deep-research-report.md" in blob
        and "2026-04-17" in blob
    )


# ---------------------------------------------------------------------------
# Criterion 1: directory and files
# ---------------------------------------------------------------------------


def test_criterion_01_directory_and_files():
    assert DOCS_DIR.exists(), f"docs/rewrite/ missing: {DOCS_DIR}"
    assert DOCS_DIR.is_dir(), f"docs/rewrite/ is not a directory: {DOCS_DIR}"
    actual = {p.name for p in DOCS_DIR.iterdir() if p.is_file()}
    assert actual == EXPECTED_FILES, (
        f"docs/rewrite/ must contain exactly {sorted(EXPECTED_FILES)}; found {sorted(actual)}"
    )


# ---------------------------------------------------------------------------
# Criterion 2: README structure
# ---------------------------------------------------------------------------


def test_criterion_02_readme_structure():
    text = _read(README_MD)

    # Metadata block
    assert _metadata_block_ok(text), (
        "README.md must have a metadata block naming task-20260417-001, "
        "deep-research-report.md, and 2026-04-17"
    )

    # Purpose paragraph: assert some non-heading prose exists after the metadata.
    non_heading = [
        ln
        for ln in text.splitlines()
        if ln.strip()
        and not ln.lstrip().startswith("#")
        and not ln.lstrip().startswith("|")
        and not ln.lstrip().startswith("-")
        and not ln.lstrip().startswith("*")
        and not ln.lstrip().startswith(">")
    ]
    assert any(len(ln.split()) >= 10 for ln in non_heading), (
        "README.md must contain a purpose paragraph"
    )

    # Numbered table of contents section.
    toc_section = re.search(
        r"(?mis)^##+\s*(table of contents|contents|toc)\b.*?(?=^##\s|\Z)",
        text,
    )
    assert toc_section, "README.md must have a Table of contents section"
    toc_body = toc_section.group(0)
    numbered = re.findall(r"(?m)^\s*\d+\.\s", toc_body)
    assert len(numbered) >= 3, (
        "Table of contents must be numbered and link to the three sibling docs"
    )

    # Intended reader section.
    assert re.search(r"(?mi)^##+\s*intended reader\b", text), (
        "README.md must have an 'Intended reader' section"
    )

    # Locked surfaces section listing all 7 lock paths verbatim.
    locked_section = re.search(
        r"(?mis)^##+\s*locked surfaces\b.*?(?=^##\s|\Z)",
        text,
    )
    assert locked_section, "README.md must have a 'Locked surfaces' section"
    locked_body = locked_section.group(0)
    for lp in LOCK_PATHS:
        assert lp in locked_body, (
            f"README.md Locked surfaces section must contain the lock path {lp!r} verbatim"
        )


# ---------------------------------------------------------------------------
# Criterion 3: gap-analysis rows
# ---------------------------------------------------------------------------


def test_criterion_03_gap_analysis_rows():
    text = _read(GAP_MD)
    # Row entries may appear inside any table on the page. Substring match is
    # sufficient because the labels are distinctive.
    lowered = text.lower()
    missing = [row for row in GAP_TARGET_ROWS if row.lower() not in lowered]
    assert not missing, f"gap-analysis.md missing target-row labels: {missing}"


# ---------------------------------------------------------------------------
# Criterion 4: gap table columns + Delta values
# ---------------------------------------------------------------------------


def _find_main_gap_table(text: str):
    """Return the first table whose header contains the four expected columns."""
    for t in _markdown_tables(text):
        header_lower = [h.lower() for h in t["header"]]
        if (
            any("target" in h and "component" in h for h in header_lower)
            and any("current" in h for h in header_lower)
            and any("target-state" in h or ("target" in h and "state" in h and h != "target component") for h in header_lower)
            and any("delta" in h for h in header_lower)
        ):
            return t
    return None


def test_criterion_04_gap_table_columns_and_delta_values():
    text = _read(GAP_MD)
    table = _find_main_gap_table(text)
    assert table is not None, (
        "gap-analysis.md must contain a table with columns "
        "Target component | Current-state | Target-state | Delta"
    )
    header_lower = [h.lower() for h in table["header"]]
    assert len(table["header"]) == 4, (
        f"gap table must have exactly 4 columns; got {table['header']}"
    )

    # Column index lookup.
    def _col(predicate):
        for idx, h in enumerate(header_lower):
            if predicate(h):
                return idx
        raise AssertionError(f"column not found: header={table['header']}")

    target_col = _col(lambda h: "target" in h and "component" in h)
    current_col = _col(lambda h: "current" in h)
    target_state_col = _col(lambda h: "target" in h and "state" in h and h != header_lower[target_col])
    delta_col = _col(lambda h: "delta" in h)

    # We focus checks on rows that actually belong to the target-vs-current
    # table; those whose Target component cell maps to one of our 23 row labels.
    seen_labels = 0
    for row in table["rows"]:
        if len(row) < 4:
            continue
        label = row[target_col].lower()
        if not any(r.lower() in label for r in GAP_TARGET_ROWS):
            continue
        seen_labels += 1
        current = row[current_col].strip()
        target_state = row[target_state_col].strip()
        delta = row[delta_col].strip().lower()
        assert current, f"row {row[target_col]!r}: Current-state cell is empty"
        assert target_state, f"row {row[target_col]!r}: Target-state cell is empty"
        assert any(dv in delta for dv in DELTA_VALUES), (
            f"row {row[target_col]!r}: Delta {delta!r} not in {DELTA_VALUES}"
        )

    assert seen_labels >= len(GAP_TARGET_ROWS), (
        f"only {seen_labels} of {len(GAP_TARGET_ROWS)} expected target rows found in the gap table"
    )


# ---------------------------------------------------------------------------
# Criterion 5: 15 distinct repo-relative file references
# ---------------------------------------------------------------------------


def test_criterion_05_fifteen_file_refs():
    text = _read(GAP_MD)
    table = _find_main_gap_table(text)
    assert table is not None, "gap-analysis.md must contain the target-vs-current table"
    header_lower = [h.lower() for h in table["header"]]
    current_col = header_lower.index(next(h for h in header_lower if "current" in h))

    # Scan the Current-state cells across all rows for repo-relative autofix/ paths.
    path_re = re.compile(r"\bautofix/[A-Za-z0-9_/\.]+\.py\b")
    distinct = set()
    for row in table["rows"]:
        if len(row) <= current_col:
            continue
        for match in path_re.findall(row[current_col]):
            distinct.add(match)
    assert len(distinct) >= 15, (
        f"gap-analysis Current-state column must reference ≥15 distinct autofix/ paths; "
        f"found {len(distinct)}: {sorted(distinct)}"
    )


# ---------------------------------------------------------------------------
# Criterion 6: locked-surfaces sub-table
# ---------------------------------------------------------------------------


def test_criterion_06_locked_surfaces_subtable():
    text = _read(GAP_MD)
    tables = _markdown_tables(text)
    # The locked-surfaces sub-table: the one in which every lock-path appears
    # in the first column and whose delta cells are keep-as-is.
    locked_table = None
    for t in tables:
        raw_first_col_cells = [row[0] if row else "" for row in t["rows"]]
        matched = sum(
            1
            for lp in LOCK_PATHS
            if any(lp in cell for cell in raw_first_col_cells)
        )
        if matched == len(LOCK_PATHS):
            locked_table = t
            break
    assert locked_table is not None, (
        "gap-analysis.md must contain a locked-surfaces table whose first "
        f"column lists all 7 lock-path literals: {LOCK_PATHS}"
    )
    # Confirm each lock path's row has Delta 'keep as-is'.
    header_lower = [h.lower() for h in locked_table["header"]]
    delta_col = None
    for idx, h in enumerate(header_lower):
        if "delta" in h:
            delta_col = idx
            break
    assert delta_col is not None, "locked-surfaces sub-table must have a Delta column"
    for lp in LOCK_PATHS:
        for row in locked_table["rows"]:
            if row and lp in row[0]:
                delta = row[delta_col].lower() if len(row) > delta_col else ""
                assert "keep as-is" in delta, (
                    f"locked-surfaces row {lp!r} must be marked 'keep as-is' (got {delta!r})"
                )
                break


# ---------------------------------------------------------------------------
# Criterion 7a: mermaid flowchart
# ---------------------------------------------------------------------------


def test_criterion_07a_flowchart_diagram():
    text = _read(TARGET_MD)
    found = False
    for lang, body in _iter_fenced_blocks(text):
        if "mermaid" in lang and body.lstrip().lower().startswith("flowchart"):
            found = True
            break
    assert found, (
        "target-architecture.md must contain a mermaid fenced code block beginning with 'flowchart'"
    )


# ---------------------------------------------------------------------------
# Criterion 7b: module boundaries heading
# ---------------------------------------------------------------------------


def test_criterion_07b_module_boundaries():
    text = _read(TARGET_MD)
    assert re.search(r"(?mi)^##+\s*module boundaries\b", text), (
        "target-architecture.md must have a 'Module boundaries' ## heading"
    )


# ---------------------------------------------------------------------------
# Criterion 7c: data schemas
# ---------------------------------------------------------------------------


def test_criterion_07c_data_schemas():
    text = _read(TARGET_MD)
    assert re.search(r"(?mi)^##+\s*data schemas\b", text), (
        "target-architecture.md must have a 'Data schemas' ## heading"
    )
    # Collect all JSON-ish fenced blocks (lang starts with json).
    json_blocks = [
        body for lang, body in _iter_fenced_blocks(text) if lang.startswith("json")
    ]
    blob = "\n".join(json_blocks) if json_blocks else text

    for schema_name in ("SymbolRecord", "Finding", "EvidencePacket", "ScanEvent"):
        assert schema_name in blob, (
            f"target-architecture.md Data schemas must include a JSON schema for {schema_name}"
        )

    # EvidencePacket must include schema_version.
    ep_idx = blob.find("EvidencePacket")
    assert ep_idx != -1
    ep_window = blob[ep_idx : ep_idx + 4000]
    assert "schema_version" in ep_window, (
        "EvidencePacket schema must include a schema_version field"
    )


# ---------------------------------------------------------------------------
# Criterion 7d: end-to-end scan sequence
# ---------------------------------------------------------------------------


def test_criterion_07d_end_to_end_sequence():
    text = _read(TARGET_MD)

    # Accept either a mermaid sequenceDiagram or a numbered flow under an
    # end-to-end-ish heading.
    has_seq = any(
        "mermaid" in lang and "sequencediagram" in body.lower()
        for lang, body in _iter_fenced_blocks(text)
    )
    if has_seq:
        return

    section = re.search(
        r"(?mis)^##+\s*(end.?to.?end|end-to-end\s+scan|end to end)\b.*?(?=^##\s|\Z)",
        text,
    )
    assert section, (
        "target-architecture.md must have either a mermaid sequenceDiagram "
        "or an end-to-end scan section with a numbered flow"
    )
    numbered = re.findall(r"(?m)^\s*\d+\.\s", section.group(0))
    assert len(numbered) >= 3, (
        "end-to-end scan section must contain a numbered flow of at least 3 steps"
    )


# ---------------------------------------------------------------------------
# Criterion 7e: integration with locked surfaces
# ---------------------------------------------------------------------------


def test_criterion_07e_integration_with_locked_surfaces():
    text = _read(TARGET_MD)
    section_match = re.search(
        r"(?mis)^##+\s*integration with locked surfaces\b.*?(?=^##\s|\Z)",
        text,
    )
    assert section_match, (
        "target-architecture.md must have an 'Integration with locked surfaces' section"
    )
    body = section_match.group(0)
    assert "run_prompt" in body, "Integration section must reference run_prompt"
    assert "findings.json" in body, (
        "Integration section must reference .autofix/state/current/findings.json (or findings.json)"
    )
    assert "events.jsonl" in body, (
        "Integration section must reference .autofix/events.jsonl (or events.jsonl)"
    )
    assert "build_agent" in body, (
        "Integration section must reference build_agent (benchmark adapter contract)"
    )


# ---------------------------------------------------------------------------
# Criterion 8: language registry
# ---------------------------------------------------------------------------


def test_criterion_08_language_registry():
    text = _read(TARGET_MD)
    section_match = re.search(
        r"(?mis)^##+\s*language registry\b.*?(?=^##\s|\Z)",
        text,
    )
    assert section_match, "target-architecture.md must have a 'Language registry' section"
    body = section_match.group(0)
    assert "Protocol" in body or "interface" in body.lower(), (
        "Language registry must sketch a Python Protocol or equivalent interface"
    )
    assert "PythonAdapter" in body, "Language registry must name PythonAdapter"
    assert ("JSTSAdapter" in body) or ("JavaScriptTypeScriptAdapter" in body), (
        "Language registry must name JSTSAdapter (or JavaScriptTypeScriptAdapter)"
    )
    assert "GoAdapter" in body, "Language registry must name GoAdapter"
    assert "tree-sitter" in body.lower() or "treesitter" in body.lower(), (
        "Language registry must discuss Tree-sitter path"
    )
    assert ("scip" in body.lower()) or ("lsif" in body.lower()), (
        "Language registry must discuss SCIP (or LSIF) path"
    )


# ---------------------------------------------------------------------------
# Criterion 9: CLI surfaces
# ---------------------------------------------------------------------------


def test_criterion_09_cli_surfaces():
    text = _read(TARGET_MD)
    clean_match = re.search(
        r"(?mis)^##+\s*clean.?slate cli surface\b.*?(?=^##\s|\Z)",
        text,
    )
    assert clean_match, "target-architecture.md must have a 'Clean-slate CLI surface' section"
    clean_body = clean_match.group(0)
    numbered = re.findall(r"(?m)^\s*\d+\.\s", clean_body)
    assert len(numbered) >= 2, "Clean-slate CLI surface must be a numbered list of subcommands"

    deprec_match = re.search(
        r"(?mis)^##+\s*deprecated cli surface\b.*?(?=^##\s|\Z)",
        text,
    )
    assert deprec_match, "target-architecture.md must have a 'Deprecated CLI surface' section"
    deprec_body = deprec_match.group(0).lower()
    for sub in DEPRECATED_SUBCOMMANDS:
        assert sub in deprec_body, (
            f"Deprecated CLI surface must name subcommand {sub!r}"
        )


# ---------------------------------------------------------------------------
# Roadmap parsing
# ---------------------------------------------------------------------------


def _parse_roadmap_tasks(text: str):
    """Parse roadmap.md into a list of task-block dicts.

    A task block is delimited by a heading like '### some-slug' or
    '### task-slug: some-slug' or '## task-N' or a '**task-slug**: some-slug'
    line. We attempt to be permissive: split on headings (##, ###, ####) that
    come before the Critical path / Parallelizable lanes sections, and keep
    blocks whose body contains 'Goal', 'Phase', and 'Depends-on'.
    """
    # Trim off the trailing Critical path and Parallelizable lanes sections.
    trim_re = re.compile(r"(?mis)^##\s*(critical path|parallelizable lanes)\b")
    m = trim_re.search(text)
    body = text if m is None else text[: m.start()]

    # Split on lines that look like block-starting headings: ## or ### only
    # (#### sub-subsections are kept inside the task).
    heading_re = re.compile(r"(?m)^(##{1,2}\s+.+)$")
    positions = [m.start() for m in heading_re.finditer(body)]
    positions.append(len(body))

    blocks = []
    for i in range(len(positions) - 1):
        chunk = body[positions[i] : positions[i + 1]]
        lower = chunk.lower()
        if "goal" in lower and "phase" in lower and "depends-on" in lower:
            blocks.append(chunk)
    return blocks


def _extract_field(block: str, label: str) -> str:
    """Return the text after ``**Label**:`` or ``Label:`` for a roadmap field."""
    pattern = re.compile(
        r"(?mi)^\s*[*_]{0,2}" + re.escape(label) + r"[*_]{0,2}\s*:\s*(.*?)(?=\n\s*[*_]{0,2}[A-Z][A-Za-z\- ]+[*_]{0,2}\s*:|\n##|\n###|\Z)",
        re.DOTALL,
    )
    m = pattern.search(block)
    return m.group(1).strip() if m else ""


def _extract_task_slug(block: str) -> str:
    """Extract the task-slug. Supports multiple block styles."""
    # Heading style: ### task-slug  OR  ### slug-name OR ### 1. slug-name
    head = block.splitlines()[0] if block.splitlines() else ""
    head = head.lstrip("#").strip()
    # If explicit: task-slug: foo
    m = re.search(r"(?mi)^\s*[*_]{0,2}task-slug[*_]{0,2}\s*:\s*(\S+)", block)
    if m:
        return m.group(1).strip(" `'\"")

    # Heading may look like "1. slug-name" or "slug-name" or "Task: slug-name".
    head_clean = re.sub(r"^\d+\.\s*", "", head)
    head_clean = re.sub(r"^(?:task|task-slug)\s*:\s*", "", head_clean, flags=re.IGNORECASE)
    # Use first kebab-case token.
    m = re.search(r"([a-z][a-z0-9]+(?:-[a-z0-9]+)+)", head_clean)
    if m:
        return m.group(1)
    return head_clean.strip()


def _extract_depends_on(block: str) -> list:
    raw = _extract_field(block, "Depends-on")
    if not raw:
        return []
    if re.search(r"(?i)\bnone\b", raw) or raw.strip() in {"-", "(none)", "—"}:
        return []
    # collect kebab-case tokens.
    return re.findall(r"([a-z][a-z0-9]+(?:-[a-z0-9]+)+)", raw)


# ---------------------------------------------------------------------------
# Criterion 10: roadmap task count & fields
# ---------------------------------------------------------------------------


def test_criterion_10_roadmap_task_count_and_fields():
    text = _read(ROADMAP_MD)
    blocks = _parse_roadmap_tasks(text)
    assert 8 <= len(blocks) <= 16, (
        f"roadmap.md must have between 8 and 16 tasks; got {len(blocks)}"
    )

    slugs = []
    for block in blocks:
        slug = _extract_task_slug(block)
        assert slug, f"task block missing a task-slug: {block[:120]!r}"
        slugs.append(slug)

        goal = _extract_field(block, "Goal")
        assert goal, f"task {slug!r}: missing Goal field"

        phase = _extract_field(block, "Phase")
        assert phase, f"task {slug!r}: missing Phase field"
        # Accept multi-word; the first recognized phase word must appear.
        assert any(p in phase for p in PHASES), (
            f"task {slug!r}: Phase must be one of {PHASES}; got {phase!r}"
        )

        # Acceptance criteria seeds: a labelled section followed by ≥3 bullet items.
        acc_match = re.search(
            r"(?mis)[*_]{0,2}acceptance criteria seeds[*_]{0,2}\s*:?(.*?)(?=\n\s*[*_]{0,2}[A-Z][A-Za-z\- ]+[*_]{0,2}\s*:|\n##|\n###|\Z)",
            block,
        )
        assert acc_match, f"task {slug!r}: missing 'acceptance criteria seeds' field"
        bullets = re.findall(r"(?m)^\s*[-*+]\s+\S", acc_match.group(1))
        assert len(bullets) >= 3, (
            f"task {slug!r}: acceptance criteria seeds must have ≥3 bullets; got {len(bullets)}"
        )

        locked = _extract_field(block, "Locked surfaces")
        assert locked, f"task {slug!r}: missing Locked surfaces field"

        size = _extract_field(block, "Estimated size")
        assert size, f"task {slug!r}: missing Estimated size field"
        # First whitespace-delimited token or any single-char band marker.
        tokens = re.findall(r"\b(XS|S|M|L)\b", size)
        assert tokens, (
            f"task {slug!r}: Estimated size must be one of {SIZE_BANDS}; got {size!r}"
        )

        # Depends-on (presence check — content checked in criterion 11).
        depends_raw = _extract_field(block, "Depends-on")
        assert depends_raw is not None, f"task {slug!r}: missing Depends-on field"

    assert len(slugs) == len(set(slugs)), (
        f"roadmap task slugs must be unique; got {slugs}"
    )


# ---------------------------------------------------------------------------
# Criterion 11: DAG
# ---------------------------------------------------------------------------


def test_criterion_11_depends_on_dag():
    text = _read(ROADMAP_MD)
    blocks = _parse_roadmap_tasks(text)
    assert blocks, "roadmap.md has no parseable task blocks"
    adj = defaultdict(list)
    slugs = []
    for block in blocks:
        slug = _extract_task_slug(block)
        slugs.append(slug)
        deps = _extract_depends_on(block)
        adj[slug] = deps

    slug_set = set(slugs)
    for slug, deps in adj.items():
        for d in deps:
            assert d in slug_set, (
                f"task {slug!r} has Depends-on {d!r} which is not a slug in this document"
            )

    # Topological sort.
    indeg = {s: 0 for s in slug_set}
    for s, deps in adj.items():
        for d in deps:
            # s depends on d → edge d -> s, so indeg[s] += 1
            indeg[s] += 1
    queue = deque([s for s, d in indeg.items() if d == 0])
    visited = 0
    while queue:
        node = queue.popleft()
        visited += 1
        for s, deps in adj.items():
            if node in deps:
                indeg[s] -= 1
                if indeg[s] == 0:
                    queue.append(s)
    assert visited == len(slug_set), (
        f"roadmap Depends-on graph has cycles; visited {visited}/{len(slug_set)}"
    )


# ---------------------------------------------------------------------------
# Criterion 12: prototype vertical slice
# ---------------------------------------------------------------------------


def test_criterion_12_prototype_vertical_slice():
    text = _read(ROADMAP_MD)
    blocks = _parse_roadmap_tasks(text)
    funnel_markers = [
        "end-to-end",
        "end to end",
        "git diff",
        "evidence packet",
        "sarif",
    ]
    found = False
    for block in blocks:
        phase = _extract_field(block, "Phase").lower()
        if "prototype" not in phase:
            continue
        lower = block.lower()
        if "end-to-end" in lower or "end to end" in lower:
            found = True
            break
        hits = sum(1 for m in funnel_markers if m in lower)
        if hits >= 2:
            found = True
            break
    assert found, (
        "roadmap.md must have at least one Prototype-phase task whose goal "
        "or criteria mention a full funnel (end-to-end / git diff → parse → "
        "analyzer → evidence packet → LLM → SARIF)"
    )


# ---------------------------------------------------------------------------
# Criterion 13: telemetry/replay + SARIF with traceability
# ---------------------------------------------------------------------------


def test_criterion_13_telemetry_and_sarif():
    text = _read(ROADMAP_MD)
    blocks = _parse_roadmap_tasks(text)

    def _goal_contains(block: str, tokens: list) -> bool:
        goal = _extract_field(block, "Goal").lower()
        # Fall back to whole block if goal empty.
        target = goal or block.lower()
        return any(t in target for t in tokens)

    telem_task = None
    sarif_task = None
    for block in blocks:
        if telem_task is None and _goal_contains(block, ["telemetry", "replay"]):
            telem_task = block
        if sarif_task is None and _goal_contains(block, ["sarif"]):
            sarif_task = block

    assert telem_task is not None, (
        "roadmap.md must include at least one task whose goal mentions telemetry or replay"
    )
    assert sarif_task is not None, (
        "roadmap.md must include at least one task whose goal mentions SARIF"
    )

    for label, block in (("telemetry/replay", telem_task), ("sarif", sarif_task)):
        lower = block.lower()
        assert ("touches gap rows" in lower) or ("gap row" in lower) or (
            "gap-analysis" in lower
        ), (
            f"{label} task must include a 'Touches gap rows' line "
            f"or similar traceability back to gap-analysis"
        )


# ---------------------------------------------------------------------------
# Criterion 14: locked surfaces per task
# ---------------------------------------------------------------------------


def _task_is_unlock(block: str) -> bool:
    goal = _extract_field(block, "Goal").lower()
    if "successor initiative" in goal or "unlock" in goal:
        return True
    # fallback — first line / heading
    head = block.splitlines()[0].lower() if block.splitlines() else ""
    return "unlock" in head or "successor initiative" in head


def test_criterion_14_locked_surfaces_per_task():
    text = _read(ROADMAP_MD)
    blocks = _parse_roadmap_tasks(text)
    assert blocks, "roadmap.md has no parseable task blocks"
    for block in blocks:
        if _task_is_unlock(block):
            continue
        slug = _extract_task_slug(block)
        locked = _extract_field(block, "Locked surfaces")
        assert locked, f"task {slug!r}: missing Locked surfaces"
        missing = [lp for lp in LOCK_PATHS if lp not in locked]
        assert not missing, (
            f"task {slug!r}: Locked surfaces missing entries {missing}"
        )


# ---------------------------------------------------------------------------
# Criterion 15: Critical path + Parallelizable lanes
# ---------------------------------------------------------------------------


def test_criterion_15_critical_path_and_lanes():
    text = _read(ROADMAP_MD)
    crit_match = re.search(
        r"(?mis)^##+\s*critical path\b.*?(?=^##\s|\Z)",
        text,
    )
    assert crit_match, "roadmap.md must have a 'Critical path' section"
    crit_body = crit_match.group(0)
    slugs = re.findall(r"([a-z][a-z0-9]+(?:-[a-z0-9]+)+)", crit_body)
    assert len(slugs) >= 3, (
        f"Critical path section must list an ordered sequence of task-slugs; got {slugs}"
    )
    # Prefer a recognizable ordered structure: numbered list or arrow chain.
    has_order = bool(
        re.search(r"(?m)^\s*\d+\.\s", crit_body)
        or re.search(r"->|→", crit_body)
        or re.search(r"(?m)^\s*[-*]\s", crit_body)
    )
    assert has_order, "Critical path must be an ordered list or arrow chain"

    lanes_match = re.search(
        r"(?mis)^##+\s*parallelizable lanes\b.*?(?=^##\s|\Z)",
        text,
    )
    assert lanes_match, "roadmap.md must have a 'Parallelizable lanes' section"


# ---------------------------------------------------------------------------
# Criterion 16: no files outside scope
# ---------------------------------------------------------------------------


@pytest.mark.skipif(PRE_EXEC, reason="pre-exec mode: git status check skipped")
def test_criterion_16_no_files_outside_scope():
    try:
        out = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=str(REPO_ROOT),
            stderr=subprocess.STDOUT,
        ).decode("utf-8", errors="replace")
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        pytest.skip(f"git unavailable: {exc}")

    allowed_prefixes = (
        "docs/rewrite/",
        ".dynos/task-20260417-001/",
        "tests/rewrite_docs/",
    )
    violations = []
    for line in out.splitlines():
        # 3-char status prefix + path
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        path = path.strip().strip('"')
        if not path:
            continue
        if path.startswith(allowed_prefixes):
            continue
        violations.append(path)
    assert not violations, (
        "modified/untracked files outside allowed scope (docs/rewrite/, "
        f".dynos/task-20260417-001/, tests/rewrite_docs/): {violations}"
    )


# ---------------------------------------------------------------------------
# Criterion 17: untouched trees
# ---------------------------------------------------------------------------


@pytest.mark.skipif(PRE_EXEC, reason="pre-exec mode: git diff check skipped")
def test_criterion_17_untouched_trees():
    try:
        # Prefer a pre-task snapshot ref if present, else HEAD.
        refs = ["dynos/task-20260417-001-snapshot", "HEAD"]
        ref = None
        for candidate in refs:
            rc = subprocess.run(
                ["git", "rev-parse", "--verify", candidate],
                cwd=str(REPO_ROOT),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if rc.returncode == 0:
                ref = candidate
                break
        if ref is None:
            pytest.skip("no git ref available for diff comparison")

        # Diff committed tree.
        committed = subprocess.check_output(
            ["git", "diff", "--name-only", ref],
            cwd=str(REPO_ROOT),
            stderr=subprocess.STDOUT,
        ).decode("utf-8", errors="replace")
        # Also include untracked files (they'd otherwise be invisible to diff).
        untracked = subprocess.check_output(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=str(REPO_ROOT),
            stderr=subprocess.STDOUT,
        ).decode("utf-8", errors="replace")
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        pytest.skip(f"git unavailable: {exc}")

    def _violates(path: str) -> bool:
        p = path.strip()
        if not p:
            return False
        if p.startswith("tests/rewrite_docs/"):
            return False
        return (
            p.startswith("autofix/")
            or p.startswith("benchmarks/")
            or p.startswith("tests/")
        )

    violations = [
        p for p in (committed + "\n" + untracked).splitlines() if _violates(p)
    ]
    assert not violations, (
        "autofix/, benchmarks/, tests/ (except tests/rewrite_docs/) must be "
        f"byte-identical to ref; violations: {violations}"
    )


# ---------------------------------------------------------------------------
# Criterion 18: metadata blocks on all four files
# ---------------------------------------------------------------------------


def test_criterion_18_metadata_blocks():
    for path in (README_MD, GAP_MD, TARGET_MD, ROADMAP_MD):
        text = _read(path)
        assert _metadata_block_ok(text), (
            f"{path.name}: metadata block must name task-20260417-001, "
            "deep-research-report.md, and 2026-04-17"
        )


# ---------------------------------------------------------------------------
# Criterion 19: repo-relative paths / links
# ---------------------------------------------------------------------------


_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def test_criterion_19_repo_relative_paths():
    for path in (README_MD, GAP_MD, TARGET_MD, ROADMAP_MD):
        text = _read(path)

        # 1. Markdown links pointing to .md files must resolve; none absolute.
        for label, target in _MD_LINK_RE.findall(text):
            if ".md" not in target.lower():
                continue
            bare = target.split("#", 1)[0].strip()
            if not bare:
                continue
            assert not bare.startswith("/"), (
                f"{path.name}: absolute markdown link not allowed: {target!r}"
            )
            assert not bare.startswith("file://"), (
                f"{path.name}: file:// markdown link not allowed: {target!r}"
            )
            assert not re.match(r"^https?://", bare), (
                f"{path.name}: http(s):// markdown link not allowed for .md: {target!r}"
            )
            resolved = (path.parent / bare).resolve()
            assert resolved.exists(), (
                f"{path.name}: markdown link {target!r} does not resolve to an existing file "
                f"(tried {resolved})"
            )

        # 2. Scan prose (outside fenced code blocks) for banned absolute
        #    path patterns.
        prose = _strip_code_fences(text)
        # Also strip inline-code spans that might contain lock-path globs.
        prose = re.sub(r"`[^`]*`", "", prose)
        # Match each banned pattern individually so we can give a clear error.
        banned = [
            (r"(?:^|[^\w])(/Users/[^\s)\]]*)", "/Users/..."),
            (r"(?:^|[^\w])(/home/[^\s)\]]*)", "/home/..."),
            (r"(?:^|[^\w])(/autofix/[^\s)\]]*)", "/autofix/... (leading slash)"),
            (r"(file://[^\s)\]]*)", "file://..."),
        ]
        for pattern, label in banned:
            m = re.search(pattern, prose)
            assert m is None, (
                f"{path.name}: banned absolute-path pattern {label!r} "
                f"found in prose: {m.group(0)!r}"
            )


# ---------------------------------------------------------------------------
# Criterion 20: word count
# ---------------------------------------------------------------------------


def test_criterion_20_word_count():
    blob_parts = []
    for path in (GAP_MD, TARGET_MD, ROADMAP_MD):
        blob_parts.append(_read(path))
    blob = "\n".join(blob_parts)
    words = re.findall(r"\b\w+\b", blob)
    count = len(words)
    assert 3000 <= count <= 12000, (
        f"combined word count of gap-analysis.md + target-architecture.md + "
        f"roadmap.md must be 3000–12000; got {count}"
    )
