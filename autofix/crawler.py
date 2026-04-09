"""Crawler-style repo inventory and frontier planning for LLM review."""

from __future__ import annotations

import ast
import hashlib
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from autofix.defaults import (
    CRAWL_TTL_DAYS_PRODUCTION,
    CRAWL_TTL_DAYS_SUPPORT,
    CRAWL_TTL_DAYS_TEST,
    DETECTOR_TTL_DAYS,
    FILE_SCORE_CHURN_MAX,
    FILE_SCORE_CHURN_WEIGHT,
    FILE_SCORE_COMPLEXITY_DIVISOR,
    FILE_SCORE_COMPLEXITY_MAX,
    FILE_SCORE_COMPLEXITY_WEIGHT,
    FILE_SCORE_DETECTOR_CONFIDENCE_DIVISOR,
    FILE_SCORE_DETECTOR_FINDING_BONUS,
    FILE_SCORE_DETECTOR_RISK_BONUS,
    FILE_SCORE_GINI_CONCENTRATION_THRESHOLD,
    FILE_SCORE_GINI_REPEAT_PENALTY_CAP,
    FILE_SCORE_GINI_REPEAT_PENALTY_WEIGHT,
    FILE_SCORE_LARGE_FILE_PENALTY_CAP,
    FILE_SCORE_LARGE_FILE_PENALTY_DIVISOR,
    FILE_SCORE_LARGE_FILE_PENALTY_THRESHOLD,
    FILE_SCORE_NEIGHBOR_INVALIDATION_BONUS,
    FILE_SCORE_PENALTY_CLEAN_SCAN,
    FILE_SCORE_PENALTY_SCANNED_3DAYS,
    FILE_SCORE_PENALTY_SCANNED_7DAYS,
    FILE_SCORE_PENALTY_SCANNED_TODAY,
    FILE_SCORE_STALE_DETECTOR_BONUS,
    FILE_SCORE_STALE_ELIGIBILITY_BONUS,
    FILE_SCORE_STALE_REVIEW_BONUS,
    GIT_LOG_CHURN_TIMEOUT,
    GIT_LSFILES_TIMEOUT,
    MISSING_FILE_RETENTION_DAYS,
    NEIGHBOR_INVALIDATION_WINDOW_DAYS,
    RECENT_SELECTION_HISTORY_LIMIT,
    RECENT_SELECTION_LOOKBACK_DAYS,
    DIVERSITY_RESERVED_SLOTS,
)
from autofix.platform import is_generated_file, now_iso

SOURCE_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".rb", ".java",
    ".kt", ".c", ".cpp", ".h", ".hpp", ".cs", ".swift", ".dart",
    ".lua", ".php", ".sh", ".bash", ".zsh",
}

SEVERITY_BONUS = {
    "low": 1.0,
    "medium": 4.0,
    "high": 8.0,
    "critical": 12.0,
}

SECRET_PATTERNS = [
    (re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"][^'\"]{8,}['\"]"), "possible hardcoded secret"),
    (re.compile(r"-----BEGIN (RSA|DSA|EC|OPENSSH|PGP) PRIVATE KEY-----"), "embedded private key"),
]

RISK_PATTERNS = [
    (re.compile(r"\beval\s*\("), "dynamic eval call"),
    (re.compile(r"\bexec\s*\("), "dynamic exec call"),
    (re.compile(r"subprocess\.(Popen|run)\("), "subprocess execution"),
    (re.compile(r"os\.system\s*\("), "shell execution"),
    (re.compile(r"innerHTML\s*="), "unsafe DOM assignment"),
    (re.compile(r"Runtime\.exec\s*\("), "runtime process execution"),
]

DATA_FLOW_PATTERNS = [
    (re.compile(r"\b(jsonDecode|json\.loads|yaml\.load|pickle\.loads|marshal\.loads)\b"), "deserialization boundary"),
    (re.compile(r"\b(open|readAsString|read_text|write_text|writeAsString)\b"), "filesystem boundary"),
    (re.compile(r"\b(http\.|requests\.|fetch\(|aiohttp|urllib)\b"), "network boundary"),
]


def _file_language(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".jsx": "javascript",
        ".go": "go",
        ".rs": "rust",
        ".rb": "ruby",
        ".java": "java",
        ".kt": "kotlin",
        ".c": "c",
        ".cpp": "cpp",
        ".h": "c",
        ".hpp": "cpp",
        ".cs": "csharp",
        ".swift": "swift",
        ".dart": "dart",
        ".lua": "lua",
        ".php": "php",
        ".sh": "shell",
        ".bash": "shell",
        ".zsh": "shell",
    }.get(suffix, suffix.lstrip(".") or "unknown")


def _is_test_file(rel_path: str) -> bool:
    path = Path(rel_path)
    parts = {part.lower() for part in path.parts}
    stem = path.stem.lower()
    return (
        "test" in parts
        or "tests" in parts
        or "__tests__" in parts
        or "spec" in parts
        or stem.startswith("test_")
        or stem.endswith("_test")
        or stem.endswith("_spec")
        or ".test" in path.name.lower()
        or ".spec" in path.name.lower()
    )


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _review_ttl_days(file_info: dict) -> int:
    if file_info.get("is_test_file"):
        return CRAWL_TTL_DAYS_TEST
    if str(file_info.get("path", "")).startswith(("src/", "lib/", "app/", "pkg/")):
        return CRAWL_TTL_DAYS_PRODUCTION
    return CRAWL_TTL_DAYS_SUPPORT


def _is_stale(timestamp: str | None, *, now: datetime, ttl_days: int) -> tuple[bool, float]:
    dt = _parse_iso(timestamp)
    if dt is None:
        return True, float(ttl_days)
    age_days = (now - dt).total_seconds() / 86400
    return age_days >= ttl_days, age_days


def _compute_neighbor_activity(previous_files: dict[str, dict], *, now: datetime) -> dict[str, bool]:
    active_dirs: set[str] = set()
    for rel, info in previous_files.items():
        if not isinstance(info, dict):
            continue
        last_crawled = _parse_iso(str(info.get("last_crawled_at", "") or info.get("last_llm_reviewed_at", "") or ""))
        age_days = (now - last_crawled).total_seconds() / 86400 if last_crawled else float("inf")
        if info.get("changed_since_last_crawl") or age_days <= NEIGHBOR_INVALIDATION_WINDOW_DAYS:
            active_dirs.add(str(Path(rel).parent))
    return {directory: True for directory in active_dirs}


def _gini(values: list[int]) -> float:
    cleaned = [max(int(v), 0) for v in values]
    if not cleaned:
        return 0.0
    if sum(cleaned) == 0:
        return 0.0
    cleaned.sort()
    total = sum(cleaned)
    n = len(cleaned)
    weighted = sum((index + 1) * value for index, value in enumerate(cleaned))
    return round((2 * weighted) / (n * total) - (n + 1) / n, 6)


def _recent_selection_count(file_info: dict, *, now: datetime) -> int:
    raw_history = file_info.get("selection_history", [])
    if not isinstance(raw_history, list):
        return 0
    count = 0
    for value in raw_history:
        dt = _parse_iso(str(value))
        if dt is None:
            continue
        age_days = (now - dt).total_seconds() / 86400
        if age_days <= RECENT_SELECTION_LOOKBACK_DAYS:
            count += 1
    return count


def _detector_signal(rule: str, severity: str, confidence: float, detail: str, *, line: int | None = None) -> dict:
    signal = {
        "rule": rule,
        "severity": severity,
        "confidence": round(confidence, 3),
        "detail": detail,
    }
    if line is not None:
        signal["line"] = int(line)
    return signal


def analyze_file_for_llm(root: Path, rel: str) -> dict:
    path = root / rel
    signals: list[dict] = []
    try:
        content = _load_text(path)
    except OSError:
        return {
            "summary": {"signal_count": 0, "max_severity": "low", "confidence": 0.0, "risk_score": 0.0},
            "signals": [],
        }

    lines = content.splitlines()
    ext = path.suffix.lower()

    if ext == ".py":
        try:
            tree = ast.parse(content, filename=rel)
            func_count = sum(1 for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)))
            class_count = sum(1 for node in ast.walk(tree) if isinstance(node, ast.ClassDef))
            if func_count >= 12:
                signals.append(_detector_signal("high_function_count", "medium", 0.72, f"{func_count} functions in one file"))
            if class_count >= 6:
                signals.append(_detector_signal("high_class_count", "medium", 0.66, f"{class_count} classes in one file"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Try) and not node.handlers:
                    signals.append(_detector_signal("empty_try_handlers", "high", 0.82, "try block without handlers", line=getattr(node, "lineno", None)))
                elif isinstance(node, ast.ExceptHandler) and node.type is None:
                    signals.append(_detector_signal("bare_except", "high", 0.9, "bare except hides unexpected failures", line=getattr(node, "lineno", None)))
                elif isinstance(node, ast.Call):
                    func = getattr(node, "func", None)
                    if isinstance(func, ast.Name) and func.id in {"eval", "exec"}:
                        signals.append(_detector_signal("dynamic_execution", "high", 0.95, f"{func.id} call", line=getattr(node, "lineno", None)))
        except SyntaxError as exc:
            signals.append(_detector_signal("syntax_error", "critical", 0.99, exc.msg, line=exc.lineno or None))

    if len(lines) >= 250:
        signals.append(_detector_signal("large_file", "medium", 0.58, f"{len(lines)} lines; harder to reason about"))

    for regex, detail in SECRET_PATTERNS:
        match = regex.search(content)
        if match:
            line = content[: match.start()].count("\n") + 1
            signals.append(_detector_signal("secret_pattern", "critical", 0.96, detail, line=line))

    risky_api_limit_reached = False
    for regex, detail in RISK_PATTERNS:
        if risky_api_limit_reached:
            break
        for match in regex.finditer(content):
            line = content[: match.start()].count("\n") + 1
            signals.append(_detector_signal("risky_api", "high", 0.78, detail, line=line))
            if len([s for s in signals if s["rule"] == "risky_api"]) >= 3:
                risky_api_limit_reached = True
                break

    for regex, detail in DATA_FLOW_PATTERNS:
        matches = list(regex.finditer(content))
        if matches:
            signals.append(_detector_signal("data_boundary", "medium", 0.61, detail, line=content[: matches[0].start()].count("\n") + 1))

    if "TODO" in content or "FIXME" in content:
        todo_count = content.count("TODO") + content.count("FIXME")
        if todo_count >= 3:
            signals.append(_detector_signal("deferred_work", "low", 0.5, f"{todo_count} TODO/FIXME markers"))

    max_severity = "low"
    for signal in signals:
        if SEVERITY_BONUS.get(signal["severity"], 0) > SEVERITY_BONUS.get(max_severity, 0):
            max_severity = signal["severity"]
    risk_score = sum(SEVERITY_BONUS.get(signal["severity"], 0) for signal in signals)
    confidence = max((float(signal["confidence"]) for signal in signals), default=0.0)
    return {
        "summary": {
            "signal_count": len(signals),
            "max_severity": max_severity,
            "confidence": round(confidence, 3),
            "risk_score": round(risk_score, 3),
            "analyzed_at": now_iso(),
            "ttl_days": DETECTOR_TTL_DAYS,
        },
        "signals": signals[:25],
    }


def normalize_crawl_state(state: dict | None) -> dict:
    files = state.get("files", {}) if isinstance(state, dict) else {}
    repo = state.get("repo", {}) if isinstance(state, dict) else {}
    normalized_files: dict[str, dict] = {}
    if isinstance(files, dict):
        for rel, value in files.items():
            if not isinstance(value, dict):
                continue
            item = dict(value)
            legacy_scanned_at = str(item.get("last_scanned_at", "") or "")
            if legacy_scanned_at and not item.get("last_llm_reviewed_at"):
                item["last_llm_reviewed_at"] = legacy_scanned_at
            if legacy_scanned_at and not item.get("last_crawled_at"):
                item["last_crawled_at"] = legacy_scanned_at
            if not isinstance(item.get("reviewed_chunk_keys"), list):
                item["reviewed_chunk_keys"] = []
            normalized_files[str(rel)] = item
    return {
        "version": 1,
        "repo": repo if isinstance(repo, dict) else {},
        "files": normalized_files,
    }


def discover_repo_files(root: Path) -> list[str]:
    candidates: list[str] = []
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            capture_output=True,
            text=True,
            timeout=GIT_LSFILES_TIMEOUT,
            cwd=str(root),
        )
    except (subprocess.TimeoutExpired, OSError):
        result = None
    if result and result.returncode == 0:
        seen: set[str] = set()
        for line in result.stdout.splitlines():
            rel = line.strip()
            if not rel or rel in seen:
                continue
            seen.add(rel)
            candidates.append(rel)
    else:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            try:
                rel = str(path.relative_to(root))
            except ValueError:
                continue
            candidates.append(rel)

    filtered: list[str] = []
    for rel in candidates:
        path = root / rel
        if not path.is_file():
            continue
        if path.suffix.lower() not in SOURCE_EXTENSIONS:
            continue
        if any(part.startswith(".") for part in Path(rel).parts):
            continue
        if is_generated_file(rel):
            continue
        filtered.append(rel)
    return sorted(set(filtered))


def collect_recent_churn(root: Path) -> dict[str, int]:
    churn: dict[str, int] = {}
    try:
        result = subprocess.run(
            ["git", "log", "--since=30 days ago", "--name-only", "--pretty=format:"],
            capture_output=True,
            text=True,
            timeout=GIT_LOG_CHURN_TIMEOUT,
            cwd=str(root),
        )
    except (subprocess.TimeoutExpired, OSError):
        return churn
    if result.returncode != 0:
        return churn
    for line in result.stdout.splitlines():
        rel = line.strip()
        if rel:
            churn[rel] = churn.get(rel, 0) + 1
    return churn


def _findings_by_file(findings: list[dict]) -> dict[str, dict]:
    summary: dict[str, dict] = {}
    for finding in findings:
        rel = str(finding.get("evidence", {}).get("file", "") or "")
        if not rel:
            continue
        bucket = summary.setdefault(rel, {
            "count": 0,
            "unresolved_count": 0,
            "max_severity": "low",
            "last_finding_at": None,
        })
        bucket["count"] += 1
        if finding.get("status") not in {"fixed", "suppressed-policy"}:
            bucket["unresolved_count"] += 1
        severity = str(finding.get("severity", "low") or "low")
        if SEVERITY_BONUS.get(severity, 0) > SEVERITY_BONUS.get(bucket["max_severity"], 0):
            bucket["max_severity"] = severity
        found_at = str(finding.get("found_at", "") or "")
        if found_at and (bucket["last_finding_at"] is None or found_at > bucket["last_finding_at"]):
            bucket["last_finding_at"] = found_at
    return summary


def _reason(rule: str, impact: float, detail: str) -> dict:
    return {"rule": rule, "impact": round(impact, 3), "detail": detail}


def _compute_priority(
    rel: str,
    file_info: dict,
    finding_summary: dict,
    now: datetime,
    neighbor_activity: dict[str, bool],
    selection_gini: float,
) -> tuple[float, list[dict]]:
    score = 0.0
    reasons: list[dict] = []

    if not file_info.get("last_llm_reviewed_at"):
        score += 30.0
        reasons.append(_reason("never_reviewed", 30.0, "File has never been sent to LLM review"))

    if file_info.get("changed_since_last_crawl"):
        score += 20.0
        reasons.append(_reason("content_changed", 20.0, "Content changed since last crawl"))

    ttl_days = _review_ttl_days(file_info)
    stale_review, review_age_days = _is_stale(file_info.get("last_llm_reviewed_at"), now=now, ttl_days=ttl_days)
    if file_info.get("last_llm_reviewed_at") and stale_review:
        score += FILE_SCORE_STALE_REVIEW_BONUS
        reasons.append(_reason("stale_review_ttl", FILE_SCORE_STALE_REVIEW_BONUS, f"Review is stale after {round(review_age_days, 1)} days; ttl={ttl_days}"))

    detector_summary = file_info.get("detector_summary", {})
    detector_stale, detector_age_days = _is_stale(detector_summary.get("analyzed_at"), now=now, ttl_days=DETECTOR_TTL_DAYS)
    if detector_summary and detector_summary.get("analyzed_at") and detector_stale:
        score += FILE_SCORE_STALE_DETECTOR_BONUS
        reasons.append(_reason("stale_detector_state", FILE_SCORE_STALE_DETECTOR_BONUS, f"Detector state is stale after {round(detector_age_days, 1)} days"))

    next_eligible = _parse_iso(str(file_info.get("next_eligible_at", "") or ""))
    if next_eligible and now >= next_eligible:
        score += FILE_SCORE_STALE_ELIGIBILITY_BONUS
        reasons.append(_reason("eligibility_window_open", FILE_SCORE_STALE_ELIGIBILITY_BONUS, "File reached its forced re-review window"))

    parent_dir = str(Path(rel).parent)
    if neighbor_activity.get(parent_dir) and not file_info.get("changed_since_last_crawl"):
        score += FILE_SCORE_NEIGHBOR_INVALIDATION_BONUS
        reasons.append(_reason("neighbor_changed", FILE_SCORE_NEIGHBOR_INVALIDATION_BONUS, "Nearby file activity invalidates cached confidence"))

    unresolved = int(finding_summary.get("unresolved_count", 0) or 0)
    if unresolved:
        impact = unresolved * 8.0
        score += impact
        reasons.append(_reason("unresolved_findings", impact, f"{unresolved} unresolved historical findings"))

    total_findings = int(finding_summary.get("count", 0) or 0)
    if total_findings:
        impact = total_findings * 4.0
        score += impact
        reasons.append(_reason("historical_findings", impact, f"{total_findings} historical findings"))

    max_severity = str(finding_summary.get("max_severity", "low") or "low")
    severity_impact = SEVERITY_BONUS.get(max_severity, 0.0)
    if severity_impact:
        score += severity_impact
        reasons.append(_reason("severity_history", severity_impact, f"Historical max severity is {max_severity}"))

    try:
        churn = min(int(file_info.get("recent_churn", 0) or 0), FILE_SCORE_CHURN_MAX)
    except (ValueError, TypeError):
        churn = 0
    if churn:
        impact = churn * FILE_SCORE_CHURN_WEIGHT
        score += impact
        reasons.append(_reason("recent_churn", impact, f"File changed {churn} times in the last 30 days"))

    try:
        line_count = int(file_info.get("line_count", 0) or 0)
    except (ValueError, TypeError):
        line_count = 0
    complexity = min(line_count / FILE_SCORE_COMPLEXITY_DIVISOR, FILE_SCORE_COMPLEXITY_MAX)
    if complexity:
        impact = complexity * FILE_SCORE_COMPLEXITY_WEIGHT
        score += impact
        reasons.append(_reason("file_size", impact, f"{line_count} lines of code"))
    if line_count > FILE_SCORE_LARGE_FILE_PENALTY_THRESHOLD:
        oversize = line_count - FILE_SCORE_LARGE_FILE_PENALTY_THRESHOLD
        penalty = min(oversize / FILE_SCORE_LARGE_FILE_PENALTY_DIVISOR, FILE_SCORE_LARGE_FILE_PENALTY_CAP)
        score -= penalty
        reasons.append(
            _reason(
                "large_file_penalty",
                -penalty,
                f"{line_count} lines exceeds large-file threshold {FILE_SCORE_LARGE_FILE_PENALTY_THRESHOLD}",
            )
        )

    try:
        recent_selection_count = int(file_info.get("recent_selection_count", 0) or 0)
    except (ValueError, TypeError):
        recent_selection_count = 0
    if selection_gini >= FILE_SCORE_GINI_CONCENTRATION_THRESHOLD and recent_selection_count > 1:
        penalty = min(
            (recent_selection_count - 1) * FILE_SCORE_GINI_REPEAT_PENALTY_WEIGHT * selection_gini,
            FILE_SCORE_GINI_REPEAT_PENALTY_CAP,
        )
        score -= penalty
        reasons.append(
            _reason(
                "gini_repeat_penalty",
                -penalty,
                f"Recent scan attention is concentrated (gini={round(selection_gini, 3)}); file selected {recent_selection_count} times in {RECENT_SELECTION_LOOKBACK_DAYS} days",
            )
        )

    if str(rel).startswith(("src/", "lib/", "app/", "pkg/")):
        score += 2.0
        reasons.append(_reason("production_code", 2.0, "File is in a production-code path"))

    last_reviewed = _parse_iso(str(file_info.get("last_llm_reviewed_at", "") or ""))
    if last_reviewed:
        age_days = (now - last_reviewed).total_seconds() / 86400
        if age_days < 1:
            score -= FILE_SCORE_PENALTY_SCANNED_TODAY
            reasons.append(_reason("recent_review", -FILE_SCORE_PENALTY_SCANNED_TODAY, "Reviewed within the last day"))
        elif age_days < 3:
            score -= FILE_SCORE_PENALTY_SCANNED_3DAYS
            reasons.append(_reason("recent_review", -FILE_SCORE_PENALTY_SCANNED_3DAYS, "Reviewed within the last 3 days"))
        elif age_days < 7:
            score -= FILE_SCORE_PENALTY_SCANNED_7DAYS
            reasons.append(_reason("recent_review", -FILE_SCORE_PENALTY_SCANNED_7DAYS, "Reviewed within the last 7 days"))
        else:
            revisit_boost = min(age_days / 7.0, 6.0)
            score += revisit_boost
            reasons.append(_reason("stale_review", revisit_boost, f"Last reviewed {round(age_days, 1)} days ago"))

    if file_info.get("last_result") == "clean" and not file_info.get("changed_since_last_crawl"):
        score -= FILE_SCORE_PENALTY_CLEAN_SCAN
        reasons.append(_reason("previously_clean", -FILE_SCORE_PENALTY_CLEAN_SCAN, "Last LLM review was clean and file is unchanged"))

    if file_info.get("is_test_file"):
        score += 1.0
        reasons.append(_reason("test_file", 1.0, "Tests can expose harness and runtime bugs too"))

    detector_signal_count = int(detector_summary.get("signal_count", 0) or 0)
    if detector_signal_count:
        impact = detector_signal_count * FILE_SCORE_DETECTOR_FINDING_BONUS
        score += impact
        reasons.append(_reason("detector_signals", impact, f"{detector_signal_count} pre-LLM detector signals"))
    detector_risk = float(detector_summary.get("risk_score", 0.0) or 0.0)
    if detector_risk:
        impact = min(detector_risk, FILE_SCORE_DETECTOR_RISK_BONUS)
        score += impact
        reasons.append(_reason("detector_risk", impact, f"Detector risk score {round(detector_risk, 2)}"))
    detector_conf = float(detector_summary.get("confidence", 0.0) or 0.0)
    if detector_conf:
        impact = detector_conf * FILE_SCORE_DETECTOR_CONFIDENCE_DIVISOR
        score += impact
        reasons.append(_reason("detector_confidence", impact, f"Detector confidence {round(detector_conf, 2)}"))

    return round(score, 3), reasons


def build_crawl_plan(root: Path, state: dict, findings: list[dict], *, max_files: int) -> tuple[dict, dict]:
    crawl_state = normalize_crawl_state(state)
    previous_files = crawl_state.get("files", {})
    churn = collect_recent_churn(root)
    findings_by_file = _findings_by_file(findings)
    now_dt = datetime.now(timezone.utc)
    now = now_iso()
    neighbor_activity = _compute_neighbor_activity(previous_files, now=now_dt)
    recent_selection_counts = [
        _recent_selection_count(info, now=now_dt)
        for info in previous_files.values()
        if isinstance(info, dict)
    ]
    selection_gini = _gini(recent_selection_counts)

    discovered = discover_repo_files(root)
    next_files: dict[str, dict] = {}
    ranked: list[dict] = []
    for rel in discovered:
        path = root / rel
        previous = previous_files.get(rel, {}) if isinstance(previous_files.get(rel), dict) else {}
        try:
            stat = path.stat()
            content = _load_text(path)
            line_count = len(content.splitlines())
            content_hash = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:16]
        except OSError:
            continue
        file_info = dict(previous)
        file_info.update({
            "path": rel,
            "language": _file_language(path),
            "extension": path.suffix.lower(),
            "is_test_file": _is_test_file(rel),
            "size_bytes": int(stat.st_size),
            "line_count": int(line_count),
            "content_hash": content_hash,
            "recent_churn": int(churn.get(rel, 0)),
            "directory_depth": len(Path(rel).parts),
            "first_seen_at": previous.get("first_seen_at") or now,
            "last_seen_at": now,
            "last_inventory_at": now,
            "finding_count_total": int(findings_by_file.get(rel, {}).get("count", previous.get("finding_count_total", 0)) or 0),
            "unresolved_finding_count": int(findings_by_file.get(rel, {}).get("unresolved_count", previous.get("unresolved_finding_count", 0)) or 0),
            "historical_max_severity": str(findings_by_file.get(rel, {}).get("max_severity", previous.get("historical_max_severity", "low")) or "low"),
            "last_known_finding_at": findings_by_file.get(rel, {}).get("last_finding_at") or previous.get("last_known_finding_at"),
        })
        history = previous.get("selection_history", [])
        if isinstance(history, list):
            normalized_history = [str(item) for item in history if isinstance(item, str)]
        else:
            normalized_history = []
        file_info["selection_history"] = normalized_history[-RECENT_SELECTION_HISTORY_LIMIT:]
        file_info["recent_selection_count"] = _recent_selection_count(file_info, now=now_dt)
        detector_analysis = analyze_file_for_llm(root, rel)
        file_info["detector_summary"] = detector_analysis["summary"]
        file_info["detector_signals"] = detector_analysis["signals"]
        last_crawl_hash = str(previous.get("last_crawl_hash", "") or "")
        last_review_marker = (
            previous.get("last_crawled_at")
            or previous.get("last_llm_reviewed_at")
            or previous.get("last_scanned_at")
        )
        if last_crawl_hash:
            changed_since_last_crawl = last_crawl_hash != content_hash
        elif last_review_marker:
            changed_since_last_crawl = True
        else:
            changed_since_last_crawl = True
        file_info["changed_since_last_crawl"] = changed_since_last_crawl
        review_ttl_days = _review_ttl_days(file_info)
        stale_review, review_age_days = _is_stale(file_info.get("last_llm_reviewed_at"), now=now_dt, ttl_days=review_ttl_days)
        file_info["review_ttl_days"] = review_ttl_days
        file_info["review_age_days"] = round(review_age_days, 3) if review_age_days != float("inf") else None
        file_info["stale_review"] = stale_review
        detector_stale, detector_age_days = _is_stale(file_info["detector_summary"].get("analyzed_at"), now=now_dt, ttl_days=DETECTOR_TTL_DAYS)
        file_info["detector_ttl_days"] = DETECTOR_TTL_DAYS
        file_info["detector_age_days"] = round(detector_age_days, 3) if detector_age_days != float("inf") else None
        file_info["stale_detector_state"] = detector_stale
        score, reasons = _compute_priority(rel, file_info, findings_by_file.get(rel, {}), now_dt, neighbor_activity, selection_gini)
        file_info["last_priority_score"] = score
        file_info["last_priority_reasons"] = reasons
        next_files[rel] = file_info
        ranked.append({
            "path": rel,
            "score": score,
            "reasons": reasons,
            "language": file_info["language"],
            "line_count": file_info["line_count"],
            "recent_churn": file_info["recent_churn"],
            "finding_count_total": file_info["finding_count_total"],
            "unresolved_finding_count": file_info["unresolved_finding_count"],
            "changed_since_last_crawl": file_info["changed_since_last_crawl"],
            "last_llm_reviewed_at": file_info.get("last_llm_reviewed_at"),
            "review_ttl_days": file_info["review_ttl_days"],
            "stale_review": file_info["stale_review"],
            "stale_detector_state": file_info["stale_detector_state"],
            "next_eligible_at": file_info.get("next_eligible_at"),
            "detector_summary": file_info["detector_summary"],
            "detector_signals": file_info["detector_signals"][:10],
            "recent_selection_count": file_info["recent_selection_count"],
            "selection_count": int(file_info.get("selection_count", 0) or 0),
            "last_selected_at": file_info.get("last_selected_at") or "",
        })

    for rel, previous in previous_files.items():
        if rel in next_files:
            continue
        if not isinstance(previous, dict):
            continue
        missing = dict(previous)
        missing["missing"] = True
        missing["last_missing_at"] = now
        missing_at = _parse_iso(now)
        first_missing = _parse_iso(str(previous.get("last_missing_at", "") or ""))
        age_days = ((missing_at - first_missing).total_seconds() / 86400) if (missing_at and first_missing) else 0
        if age_days < MISSING_FILE_RETENTION_DAYS:
            next_files[rel] = missing

    ranked.sort(key=lambda item: (item["score"], item["finding_count_total"], item["recent_churn"]), reverse=True)

    # Two-phase selection: reserve slots for rarely/never-selected files to
    # enforce diversity when the score distribution is heavily skewed.
    reserved = min(DIVERSITY_RESERVED_SLOTS, max_files // 2)
    priority_slots = max_files - reserved
    priority_selected = ranked[:priority_slots]
    priority_paths = {item["path"] for item in priority_selected}

    # Build the diversity pool from files NOT already in the priority set.
    # Sort by: unseen first (selection_count=0), then oldest last_selected_at,
    # then score as final tiebreaker.  This is fully state-driven: each scan
    # selects a file, which updates its selection_count and last_selected_at,
    # pushing it to the back of the queue and letting the next-oldest file
    # rotate in.  Guarantees eventual full coverage.
    remaining = [item for item in ranked if item["path"] not in priority_paths]
    remaining.sort(key=lambda item: (
        0 if item["selection_count"] == 0 else 1,
        item["last_selected_at"] or "",
        -item["score"],
    ))
    diversity_selected = remaining[:reserved]
    diversity_paths = {item["path"] for item in diversity_selected}

    selected = priority_selected + diversity_selected
    selected_paths = {item["path"] for item in selected}
    for item in selected:
        info = next_files[item["path"]]
        info["last_selected_at"] = now
        info["selection_count"] = int(info.get("selection_count", 0) or 0) + 1
        history = info.get("selection_history", [])
        if not isinstance(history, list):
            history = []
        history.append(now)
        info["selection_history"] = history[-RECENT_SELECTION_HISTORY_LIMIT:]
        info["recent_selection_count"] = _recent_selection_count(info, now=now_dt)

    crawl_state["repo"] = {
        "last_inventory_at": now,
        "last_planned_at": now,
        "file_count": len(discovered),
        "eligible_file_count": len(ranked),
        "selected_file_count": len(selected),
        "selection_gini": selection_gini,
    }
    crawl_state["files"] = next_files
    plan = {
        "generated_at": now,
        "file_count": len(discovered),
        "selected_files": selected,
        "frontier": ranked,
        "review_files": [item["path"] for item in selected if item["score"] >= 0 or item["path"] in diversity_paths],
    }
    return crawl_state, plan


def finalize_crawl_state(
    state: dict,
    selected_paths: list[str],
    findings: list[dict],
    reviewed_chunks_by_file: dict[str, list[str]] | None = None,
) -> dict:
    crawl_state = normalize_crawl_state(state)
    files = crawl_state.get("files", {})
    now = now_iso()
    findings_by_file = _findings_by_file(findings)
    reviewed_chunks_by_file = reviewed_chunks_by_file or {}
    for rel in selected_paths:
        file_info = files.get(rel)
        if not isinstance(file_info, dict):
            file_info = {"path": rel}
            files[rel] = file_info
        file_info["last_crawled_at"] = now
        file_info["last_llm_reviewed_at"] = now
        file_info["crawl_count"] = int(file_info.get("crawl_count", 0) or 0) + 1
        file_info["llm_review_count"] = int(file_info.get("llm_review_count", 0) or 0) + 1
        file_info["last_crawl_hash"] = file_info.get("content_hash")
        finding_summary = findings_by_file.get(rel, {})
        file_info["last_finding_count"] = int(finding_summary.get("count", 0) or 0)
        file_info["last_result"] = "findings" if file_info["last_finding_count"] else "clean"
        file_info["changed_since_last_crawl"] = False
        if file_info["last_finding_count"]:
            file_info["last_finding_at"] = now
            file_info["historical_max_severity"] = str(finding_summary.get("max_severity", file_info.get("historical_max_severity", "low")) or "low")
        cooldown = timedelta(days=1 if file_info["last_finding_count"] else _review_ttl_days(file_info))
        file_info["next_eligible_at"] = (datetime.now(timezone.utc) + cooldown).isoformat().replace("+00:00", "Z")
        file_info.setdefault("detector_summary", {})
        file_info["detector_summary"]["analyzed_at"] = now
        file_info["stale_review"] = False
        file_info["stale_detector_state"] = False
        prior_chunk_keys = file_info.get("reviewed_chunk_keys", [])
        if not isinstance(prior_chunk_keys, list):
            prior_chunk_keys = []
        new_chunk_keys = reviewed_chunks_by_file.get(rel, [])
        merged_chunk_keys = list(dict.fromkeys([*prior_chunk_keys, *new_chunk_keys]))
        file_info["reviewed_chunk_keys"] = merged_chunk_keys[-100:]
    crawl_state["files"] = files
    crawl_state.setdefault("repo", {})["last_completed_crawl_at"] = now
    crawl_state["repo"]["last_reviewed_file_count"] = len(selected_paths)
    return crawl_state
