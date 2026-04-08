#!/usr/bin/env python3
"""Detection helpers for autofix scans."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from autofix.defaults import (
    FILE_SCORE_CHURN_MAX,
    FILE_SCORE_CHURN_WEIGHT,
    FILE_SCORE_COMPLEXITY_DIVISOR,
    FILE_SCORE_COMPLEXITY_MAX,
    FILE_SCORE_COMPLEXITY_WEIGHT,
    FILE_SCORE_NO_TEST_BOOST,
    FILE_SCORE_PENALTY_CLEAN_SCAN,
    FILE_SCORE_PENALTY_SCANNED_3DAYS,
    FILE_SCORE_PENALTY_SCANNED_7DAYS,
    FILE_SCORE_PENALTY_SCANNED_TODAY,
    GIT_LOG_CHURN_TIMEOUT,
    GIT_LSFILES_TIMEOUT,
    HIGH_CONFIDENCE_THRESHOLD,
    LLM_INVOCATION_TIMEOUT,
    LLM_REVIEW_FILE_TRUNCATION,
    LLM_REVIEW_MAX_FILES,
    MIN_FINDING_CONFIDENCE,
    NPM_AUDIT_TIMEOUT,
    PIP_AUDIT_TIMEOUT,
)
from autofix.platform import (
    collect_retrospectives,
    compute_scan_targets,
    is_generated_file,
    local_patterns_path,
    now_iso,
    persistent_project_dir,
)
from autofix.state import load_findings, load_scan_coverage, make_finding, save_scan_coverage

HAIKU_REVIEW_PROMPT = """You are a thorough code auditor. Analyze the following source files for issues.

Follow the evidence — read the actual code, trace actual values, follow actual execution paths.

## ONLY report issues that would cause:
- Runtime crashes or unhandled exceptions
- Data corruption or data loss
- Security vulnerabilities (injection, auth bypass, secrets exposure)
- Logic errors that produce wrong results
- Resource leaks (file handles, connections, memory)

## Do NOT report:
- Style preferences (formatting, whitespace, import order)
- Naming conventions (variable names, function names, casing)
- Missing documentation, docstrings, or comments
- Framework-specific intentional patterns (e.g. Django conventions, React patterns)
- Issues in generated files (*.g.dart, *.generated.*, *.pb.go, etc.)
- Dead code or unused imports (handled by another scanner)
- Anything that is clearly intentional and correct

Look for:
1. **Logic bugs** — incorrect conditions, off-by-one errors, wrong variable used, missing edge cases
2. **Security issues** — injection risks, unvalidated input at system boundaries, secrets in code, unsafe deserialization
3. **Error handling gaps** — swallowed exceptions, missing try/except around I/O, bare except clauses that hide bugs
4. **Race conditions** — shared mutable state without locks, TOCTOU patterns, async gaps
5. **Data integrity** — writes without validation, missing null checks at boundaries, type confusion
6. **Anti-patterns** — God functions (>100 lines doing too much), circular dependencies, hidden side effects

For each issue found, return ONLY a JSON array. Each item must have:
- "description": one sentence describing the issue
- "file": the filename
- "line": approximate line number
- "severity": "low", "medium", "high", or "critical"
- "category_detail": which of the 6 categories above
- "confidence": a float between 0.0 and 1.0 representing your confidence that this is a real bug (not a style preference or false positive)

If no issues are found, return an empty array: []

Return ONLY the JSON array, no other text.

FILES TO REVIEW:
"""


def detect_recurring_audit(root: Path) -> list[dict]:
    findings: list[dict] = []
    retros = collect_retrospectives(root)
    if not retros:
        return findings
    recent = retros[-10:]
    task_count = len(recent)
    if task_count == 0:
        return findings

    category_tasks: dict[str, list[str]] = {}
    for retro in recent:
        fbc = retro.get("findings_by_category", {})
        if not isinstance(fbc, dict):
            continue
        task_id = str(retro.get("task_id", "unknown"))
        for cat, count in fbc.items():
            if isinstance(cat, str) and isinstance(count, (int, float)) and count > 0:
                category_tasks.setdefault(cat, []).append(task_id)

    if task_count < 3:
        return findings
    threshold = task_count * 0.5
    for cat, task_ids in category_tasks.items():
        if len(task_ids) > threshold:
            rate = round(len(task_ids) / task_count, 2)
            finding_id = f"recurring-audit-{cat}-{datetime.now(timezone.utc).strftime('%Y%m%d')}"
            findings.append(make_finding(
                finding_id=finding_id,
                severity="medium",
                category="recurring-audit",
                description=f"Audit category '{cat}' appeared in {len(task_ids)} of last {task_count} tasks ({rate:.0%} rate)",
                evidence={"category": cat, "occurrence_rate": rate, "task_ids": task_ids},
            ))
    return findings


def detect_dependency_vulns(root: Path, *, log) -> list[dict]:
    findings: list[dict] = []
    if shutil.which("pip-audit"):
        try:
            result = subprocess.run(
                ["pip-audit", "--format=json"],
                capture_output=True, text=True, timeout=PIP_AUDIT_TIMEOUT, cwd=str(root),
            )
            if result.stdout.strip():
                try:
                    data = json.loads(result.stdout)
                except json.JSONDecodeError:
                    data = {}
                vulns = data.get("dependencies", []) if isinstance(data, dict) else data if isinstance(data, list) else []
                for vuln in vulns:
                    if not isinstance(vuln, dict):
                        continue
                    for item in vuln.get("vulns", []):
                        if not isinstance(item, dict):
                            continue
                        pkg_name = str(vuln.get("name", "unknown"))
                        pkg_version = str(vuln.get("version", "unknown"))
                        vuln_id = str(item.get("id", "unknown"))
                        vuln_desc = str(item.get("description", "No description"))
                        desc_lower = vuln_desc.lower()
                        if any(w in desc_lower for w in ("critical", "remote code", "rce", "arbitrary code")):
                            severity = "critical"
                        elif any(w in desc_lower for w in ("high", "injection", "overflow", "bypass")):
                            severity = "high"
                        elif any(w in desc_lower for w in ("medium", "moderate", "denial")):
                            severity = "medium"
                        else:
                            severity = "low"
                        findings.append(make_finding(
                            finding_id=f"dep-vuln-{pkg_name}-{vuln_id}",
                            severity=severity,
                            category="dependency-vuln",
                            description=f"Vulnerability {vuln_id} in {pkg_name}=={pkg_version}: {vuln_desc[:200]}",
                            evidence={"package": pkg_name, "version": pkg_version, "vuln_id": vuln_id, "source": "pip-audit"},
                        ))
        except (subprocess.TimeoutExpired, OSError) as exc:
            log(f"pip-audit failed: {exc}")

    lockfile_exists = (root / "package-lock.json").exists() or (root / "yarn.lock").exists()
    if lockfile_exists and shutil.which("npm"):
        try:
            result = subprocess.run(
                ["npm", "audit", "--json"],
                capture_output=True, text=True, timeout=NPM_AUDIT_TIMEOUT, cwd=str(root),
            )
            if result.stdout.strip():
                try:
                    data = json.loads(result.stdout)
                except json.JSONDecodeError:
                    data = {}
                if isinstance(data, dict):
                    advisories = data.get("advisories", {})
                    if isinstance(advisories, dict):
                        for adv_id, adv in advisories.items():
                            if not isinstance(adv, dict):
                                continue
                            raw_sev = str(adv.get("severity", "moderate"))
                            sev_map = {"critical": "critical", "high": "high", "moderate": "medium", "low": "low"}
                            severity = sev_map.get(raw_sev, "medium")
                            module_name = str(adv.get("module_name", "unknown"))
                            title = str(adv.get("title", "No title"))
                            findings.append(make_finding(
                                finding_id=f"dep-vuln-npm-{module_name}-{adv_id}",
                                severity=severity,
                                category="dependency-vuln",
                                description=f"npm vulnerability in {module_name}: {title[:200]}",
                                evidence={"package": module_name, "advisory_id": str(adv_id), "source": "npm-audit"},
                            ))
                    vulns_v2 = data.get("vulnerabilities", {})
                    if isinstance(vulns_v2, dict) and not advisories:
                        for pkg_name, vuln_data in vulns_v2.items():
                            if not isinstance(vuln_data, dict):
                                continue
                            raw_sev = str(vuln_data.get("severity", "moderate"))
                            sev_map = {"critical": "critical", "high": "high", "moderate": "medium", "low": "low"}
                            severity = sev_map.get(raw_sev, "medium")
                            findings.append(make_finding(
                                finding_id=f"dep-vuln-npm-{pkg_name}-v2",
                                severity=severity,
                                category="dependency-vuln",
                                description=f"npm vulnerability in {pkg_name} (severity: {raw_sev})",
                                evidence={"package": str(pkg_name), "severity_raw": raw_sev, "source": "npm-audit-v2"},
                            ))
        except (subprocess.TimeoutExpired, OSError) as exc:
            log(f"npm audit failed: {exc}")
    return findings


def detect_syntax_errors(root: Path) -> list[dict]:
    findings: list[dict] = []
    hooks_dir = root / "hooks"
    if not hooks_dir.is_dir():
        return findings
    for py_file in sorted(hooks_dir.glob("*.py")):
        try:
            source = py_file.read_text()
            ast.parse(source, filename=py_file.name)
        except SyntaxError as exc:
            findings.append(make_finding(
                finding_id=f"syntax-error-{py_file.name}-{exc.lineno or 0}",
                severity="medium",
                category="syntax-error",
                description=f"Syntax error in {py_file.name} line {exc.lineno}: {exc.msg}",
                evidence={"file": f"hooks/{py_file.name}", "line": exc.lineno, "message": exc.msg, "text": (exc.text or '').strip()},
            ))
        except (OSError, UnicodeDecodeError):
            continue
    return findings


def _description_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def detect_dead_code(root: Path) -> list[dict]:
    findings: list[dict] = []
    hooks_dir = root / "hooks"
    if not hooks_dir.is_dir():
        return findings
    py_files = sorted(hooks_dir.glob("*.py"))
    all_defined_funcs: dict[str, list[str]] = {}
    all_source_texts: dict[str, str] = {}

    for py_file in py_files:
        try:
            source = py_file.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        all_source_texts[py_file.name] = source
        try:
            tree = ast.parse(source, filename=py_file.name)
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.FunctionDef):
                all_defined_funcs.setdefault(node.name, []).append(py_file.name)

    for py_file in py_files:
        try:
            source = py_file.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        try:
            tree = ast.parse(source, filename=py_file.name)
        except (SyntaxError, UnicodeDecodeError):
            continue
        imported_names: dict[str, int] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_names[alias.asname if alias.asname else alias.name] = node.lineno
            elif isinstance(node, ast.ImportFrom):
                if node.module == "__future__":
                    continue
                for alias in node.names:
                    imported_names[alias.asname if alias.asname else alias.name] = node.lineno
        used_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                used_names.add(node.id)
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                used_names.add(node.value.id)

        has_all = False
        all_names: set[str] = set()
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "__all__":
                        has_all = True
                        if isinstance(node.value, (ast.List, ast.Tuple)):
                            for elt in node.value.elts:
                                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                    all_names.add(elt.value)

        unused_imports: list[str] = []
        for name in imported_names:
            if name.startswith("_") and name != "__all__":
                continue
            if name in used_names:
                continue
            if has_all and name in all_names:
                continue
            if py_file.name == "dynoslib.py":
                continue
            module_name = py_file.stem
            is_reexport = False
            for other_file in py_files:
                if other_file == py_file:
                    continue
                other_src = all_source_texts.get(other_file.name, "")
                if not other_src:
                    continue
                if re.search(rf"from\s+{re.escape(module_name)}\s+import\s+.*\b{re.escape(name)}\b", other_src):
                    is_reexport = True
                    break
                if re.search(rf"^import\s+{re.escape(module_name)}\b", other_src, re.MULTILINE):
                    is_reexport = True
                    break
            if is_reexport:
                continue
            unused_imports.append(name)

        if unused_imports:
            findings.append(make_finding(
                finding_id=f"dead-code-unused-import-{py_file.name}-{_description_hash(','.join(sorted(unused_imports)))}",
                severity="low",
                category="dead-code",
                description=f"Unused imports in {py_file.name}: {', '.join(sorted(unused_imports)[:5])}",
                evidence={"file": str(py_file.relative_to(root)), "unused_imports": sorted(unused_imports)[:10]},
            ))

    all_combined_source = "\n".join(all_source_texts.values())
    for func_name, defining_files in all_defined_funcs.items():
        if func_name.startswith("_") or func_name.startswith("cmd_") or func_name.startswith("test_"):
            continue
        if func_name in ("build_parser", "cli_main", "main", "setup", "teardown"):
            continue
        occurrence_count = all_combined_source.count(func_name)
        if occurrence_count <= len(defining_files):
            findings.append(make_finding(
                finding_id=f"dead-code-unreferenced-{func_name}-{_description_hash(func_name)}",
                severity="low",
                category="dead-code",
                description=f"Potentially unreferenced function '{func_name}' in {', '.join(defining_files)}",
                evidence={"function": func_name, "defined_in": defining_files, "occurrence_count": occurrence_count},
            ))
    return findings


def detect_architectural_drift(root: Path, *, log) -> list[dict]:
    findings: list[dict] = []
    patterns_path = local_patterns_path(root)
    if not patterns_path.exists():
        log(f"No patterns file at {patterns_path}, skipping drift detection")
        return findings
    try:
        content = patterns_path.read_text()
    except OSError as exc:
        log(f"Could not read patterns file: {exc}")
        return findings

    prevention_rules: list[dict] = []
    in_prevention = False
    for line in content.splitlines():
        if "## Prevention Rules" in line:
            in_prevention = True
            continue
        if in_prevention and line.startswith("##"):
            break
        if in_prevention and line.startswith("|") and "---" not in line and "Executor" not in line:
            parts = [p.strip() for p in line.split("|") if p.strip()]
            if len(parts) >= 3:
                prevention_rules.append({"executor": parts[0], "rule": parts[1], "source": parts[2]})

    gold_standards: list[str] = []
    in_gold = False
    for line in content.splitlines():
        if "## Gold Standard" in line:
            in_gold = True
            continue
        if in_gold and line.startswith("##"):
            break
        if in_gold and line.startswith("|") and "---" not in line and "Task ID" not in line:
            parts = [p.strip() for p in line.split("|") if p.strip()]
            if parts and parts[0] != "none":
                gold_standards.append(parts[0])

    retros = collect_retrospectives(root)
    for retro in retros[-5:] if retros else []:
        fbc = retro.get("findings_by_category", {})
        repair_count = int(retro.get("repair_cycle_count", 0) or 0)
        task_id = str(retro.get("task_id", "unknown"))
        if repair_count > 2 and isinstance(fbc, dict) and sum(int(v) for v in fbc.values() if isinstance(v, (int, float))) > 3:
            findings.append(make_finding(
                finding_id=f"arch-drift-high-repair-{task_id}",
                severity="medium",
                category="architectural-drift",
                description=f"Task {task_id} required {repair_count} repair cycles with multiple finding categories, suggesting architectural drift from gold standards",
                evidence={
                    "task_id": task_id,
                    "repair_cycles": repair_count,
                    "findings_by_category": fbc,
                    "prevention_rules_count": len(prevention_rules),
                    "gold_standard_count": len(gold_standards),
                },
            ))
    return findings


def compute_file_scores(root: Path, coverage: dict) -> list[tuple[Path, float]]:
    source_extensions = {
        ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".rb", ".java",
        ".kt", ".c", ".cpp", ".h", ".hpp", ".cs", ".swift", ".dart",
        ".lua", ".php", ".sh", ".bash", ".zsh",
    }
    unique_files: list[Path] = []
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            capture_output=True, text=True, timeout=GIT_LSFILES_TIMEOUT, cwd=root,
        )
        if result.returncode == 0:
            seen: set[str] = set()
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                path = root / line
                if path.suffix not in source_extensions:
                    continue
                if any(part.startswith(".") for part in Path(line).parts):
                    continue
                if is_generated_file(line):
                    continue
                if line not in seen and path.is_file():
                    seen.add(line)
                    unique_files.append(path)
    except (subprocess.TimeoutExpired, OSError):
        pass
    if not unique_files:
        return []

    now = datetime.now(timezone.utc)
    file_coverage = coverage.get("files", {})
    churn: dict[str, int] = {}
    try:
        result = subprocess.run(
            ["git", "log", "--since=30 days ago", "--name-only", "--pretty=format:"],
            capture_output=True, text=True, timeout=GIT_LOG_CHURN_TIMEOUT, cwd=root,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                line = line.strip()
                if line:
                    churn[line] = churn.get(line, 0) + 1
    except (subprocess.TimeoutExpired, OSError):
        pass

    prev_findings: dict[str, int] = {}
    for finding in load_findings(root):
        evidence_file = finding.get("evidence", {}).get("file", "")
        if evidence_file:
            prev_findings[evidence_file] = prev_findings.get(evidence_file, 0) + 1

    test_files: set[str] = set()
    for test_dir in ("tests", "test", "__tests__", "spec"):
        for test_file in (root / test_dir).rglob("*") if (root / test_dir).is_dir() else []:
            if test_file.is_file():
                stem = test_file.stem
                for prefix in ("test_", "test-"):
                    if stem.startswith(prefix):
                        stem = stem[len(prefix):]
                for suffix in ("_test", "-test", ".test", "_spec", "-spec", ".spec"):
                    if stem.endswith(suffix):
                        stem = stem[:-len(suffix)]
                test_files.add(stem)

    scored: list[tuple[Path, float]] = []
    for path in unique_files:
        rel = str(path.relative_to(root))
        score = 0.0
        score += min(churn.get(rel, 0), FILE_SCORE_CHURN_MAX) * FILE_SCORE_CHURN_WEIGHT
        try:
            line_count = len(path.read_text().splitlines())
        except OSError:
            line_count = 0
        score += min(line_count / FILE_SCORE_COMPLEXITY_DIVISOR, FILE_SCORE_COMPLEXITY_MAX) * FILE_SCORE_COMPLEXITY_WEIGHT
        if f"test_{path.stem}" not in test_files:
            score += FILE_SCORE_NO_TEST_BOOST
        if rel in prev_findings:
            score += prev_findings[rel] * 3
        file_info = file_coverage.get(rel, {})
        last_scanned = file_info.get("last_scanned_at", "")
        if last_scanned:
            try:
                scanned_dt = datetime.fromisoformat(last_scanned.replace("Z", "+00:00"))
                days_since = (now - scanned_dt).total_seconds() / 86400
                if days_since < 1:
                    score -= FILE_SCORE_PENALTY_SCANNED_TODAY
                elif days_since < 3:
                    score -= FILE_SCORE_PENALTY_SCANNED_3DAYS
                elif days_since < 7:
                    score -= FILE_SCORE_PENALTY_SCANNED_7DAYS
            except (ValueError, TypeError):
                pass
        if file_info.get("last_result") == "clean":
            score -= FILE_SCORE_PENALTY_CLEAN_SCAN
        scored.append((path, score))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored


def detect_llm_review(root: Path, *, log) -> list[dict]:
    findings: list[dict] = []
    if not shutil.which("claude"):
        log("Skipping LLM review: claude CLI not available")
        return findings

    coverage = load_scan_coverage(root)
    findings_list = load_findings(root)
    scored_files = compute_scan_targets(root, max_files=LLM_REVIEW_MAX_FILES, coverage=coverage, findings=findings_list)
    if not scored_files:
        scored_files = [
            (str(path.relative_to(root)), score)
            for path, score in compute_file_scores(root, coverage)[:LLM_REVIEW_MAX_FILES]
        ]
    if not scored_files:
        return findings

    review_files: list[Path] = []
    for rel_path, score in scored_files:
        if score < 0:
            continue
        review_files.append(root / rel_path)
        if len(review_files) >= LLM_REVIEW_MAX_FILES:
            break
    if not review_files:
        log("All files recently scanned, skipping LLM review this cycle")
        return findings

    log(f"File scores (top 10): {[(str(f), round(s, 1)) for f, s in scored_files[:10]]}")
    prompt = HAIKU_REVIEW_PROMPT

    try:
        patterns_path = persistent_project_dir(root) / "dynos_patterns.md"
        if patterns_path.exists():
            prevention_text = ""
            in_prevention = False
            for line in patterns_path.read_text().splitlines():
                if "## Prevention Rules" in line:
                    in_prevention = True
                    continue
                if in_prevention and line.startswith("##"):
                    break
                if in_prevention:
                    prevention_text += line + "\n"
            prevention_text = prevention_text.strip()
            if prevention_text:
                prompt += f"\n## Project-specific patterns to watch for:\n{prevention_text}\n"
    except OSError:
        pass

    if findings_list:
        known_descs = [f.get("description", "") for f in findings_list if f.get("description")]
        if known_descs:
            prompt += "\n## Already known issues (do NOT re-report these):\n" + "\n".join(f"- {d}" for d in known_descs[:50]) + "\n"

    for path in review_files:
        try:
            content = path.read_text()
            rel = str(path.relative_to(root))
            lines = content.splitlines()
            if len(lines) > LLM_REVIEW_FILE_TRUNCATION:
                content = "\n".join(lines[:LLM_REVIEW_FILE_TRUNCATION]) + f"\n... (truncated, {len(lines)} total lines)"
            prompt += f"\n--- {rel} ---\n{content}\n"
        except OSError:
            continue

    log(f"Running Haiku LLM review on {len(review_files)} files: {[str(f.relative_to(root)) for f in review_files]}")
    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--model", "haiku"],
            capture_output=True, text=True, timeout=LLM_INVOCATION_TIMEOUT, cwd=root,
        )
    except subprocess.TimeoutExpired:
        log(f"Haiku review timed out after {LLM_INVOCATION_TIMEOUT}s")
        return findings
    except OSError as exc:
        log(f"Haiku review failed: {exc}")
        return findings

    if result.returncode != 0:
        log(f"Haiku review exited {result.returncode}")
        return findings

    output = result.stdout.strip()
    if output.startswith("```"):
        output = "\n".join(line for line in output.splitlines() if not line.startswith("```")).strip()
    try:
        issues = json.loads(output)
    except json.JSONDecodeError:
        start = output.find("[")
        end = output.rfind("]")
        if start >= 0 and end > start:
            try:
                issues = json.loads(output[start:end + 1])
            except json.JSONDecodeError:
                log("Could not parse Haiku response as JSON")
                return findings
        else:
            log("No JSON array found in Haiku response")
            return findings
    if not isinstance(issues, list):
        return findings

    filtered = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        confidence = float(issue.get("confidence", 0.5))
        if confidence < MIN_FINDING_CONFIDENCE:
            log(f"Filtering low-confidence finding (confidence={confidence}): {issue.get('description', '')[:60]}")
            continue
        issue["_confidence_score"] = confidence
        filtered.append(issue)

    if filtered and all(float(i.get("_confidence_score", i.get("confidence", 0))) >= HIGH_CONFIDENCE_THRESHOLD for i in filtered):
        log(f"WARNING: All findings in this batch have confidence >= {HIGH_CONFIDENCE_THRESHOLD}. Possible confidence degeneration (model may be over-confident).")

    for issue in filtered:
        desc = str(issue.get("description", ""))
        file_name = str(issue.get("file", ""))
        line_num = issue.get("line", 0)
        severity = str(issue.get("severity", "low"))
        cat_detail = str(issue.get("category_detail", ""))
        conf_score = float(issue.get("_confidence_score", issue.get("confidence", 0.5)))
        if not desc or not file_name:
            continue
        if severity not in ("low", "medium", "high", "critical"):
            severity = "medium"
        fid_raw = f"llm-review-{file_name}-{line_num}-{desc[:50]}"
        fid = f"llm-review-{hashlib.sha256(fid_raw.encode()).hexdigest()[:16]}"
        finding = make_finding(
            finding_id=fid,
            severity=severity,
            category="llm-review",
            description=f"[{cat_detail}] {desc}",
            evidence={"file": file_name, "line": line_num, "category_detail": cat_detail, "reviewer": "haiku"},
        )
        finding["confidence_score"] = conf_score
        findings.append(finding)

    log(f"Haiku review found {len(findings)} issues (after confidence filtering)")
    file_coverage = coverage.get("files", {})
    files_with_findings = {finding.get("evidence", {}).get("file", "") for finding in findings}
    for path in review_files:
        rel = str(path.relative_to(root))
        file_coverage[rel] = {"last_scanned_at": now_iso(), "last_result": "findings" if rel in files_with_findings else "clean"}
    coverage["files"] = file_coverage
    coverage["last_scan_at"] = now_iso()
    save_scan_coverage(root, coverage)
    return findings
