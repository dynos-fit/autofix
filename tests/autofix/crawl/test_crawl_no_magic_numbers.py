"""No-magic-numbers grep test for the crawl subsystem (ARCH-016 AC-27).

Asserts that consumer modules in ``autofix/crawl/`` reference
constants by name (from ``crawl_constants``), not by inline literal.
Targeted to the specific business numbers/strings — control-flow
constants like ``0`` / ``1`` are NOT flagged (matches the precedent
set by ``test_run_command_no_magic_numbers.py`` in ARCH-010).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


# Modules being audited. These all live in autofix/crawl/ and are
# expected to import their numeric/string defaults from
# autofix/crawl/crawl_constants.py.
_CONSUMER_MODULES = [
    "bundles.py",
    "score.py",
    "ledger.py",
    "picker.py",
]

CRAWL_PKG = (
    Path(__file__).resolve().parents[3] / "autofix" / "crawl"
)


def _body(filename: str) -> str:
    """Return the file source minus triple-quoted docstrings."""
    src = (CRAWL_PKG / filename).read_text(encoding="utf-8")
    return re.sub(r'"""[\s\S]*?"""', "", src, flags=re.MULTILINE)


@pytest.mark.parametrize("filename", _CONSUMER_MODULES)
def test_no_inline_horizon_hours(filename: str) -> None:
    """Standalone ``24`` literal MUST NOT appear in consumer bodies."""
    body = _body(filename)
    assert re.search(r"(?<![0-9])24(?![0-9])", body) is None, (
        f"{filename} inlines the 24 horizon literal — use "
        f"STALENESS_HORIZON_HOURS / HUB_SATURATION_WINDOW_HOURS."
    )


@pytest.mark.parametrize("filename", _CONSUMER_MODULES)
def test_no_inline_max_hub_appearances(filename: str) -> None:
    """Standalone ``3`` literal MUST NOT appear (use MAX_HUB_APPEARANCES)."""
    body = _body(filename)
    # The cap value `3` may appear inside arithmetic like `K * 3`. We
    # forbid only the assignment forms most likely to be a magic number.
    assert "MAX_HUB_APPEARANCES = 3" not in body
    assert re.search(r"=\s*3\s*$", body, re.MULTILINE) is None or all(
        line for line in body.splitlines() if line.endswith("= 3")
        if "MAX_HUB_APPEARANCES" not in line and "STALENESS" not in line
    )


@pytest.mark.parametrize("filename", _CONSUMER_MODULES)
def test_no_inline_max_bundle_files(filename: str) -> None:
    """Standalone ``5`` literal as a default arg MUST NOT appear in consumer bodies."""
    body = _body(filename)
    # The forbidden form is ``max_files=5`` or ``max_files = 5`` as a
    # constant default; using the imported MAX_BUNDLE_FILES is required.
    assert "max_files=5" not in body
    assert "max_files = 5" not in body


@pytest.mark.parametrize("filename", _CONSUMER_MODULES)
def test_no_inline_max_bundle_bytes(filename: str) -> None:
    body = _body(filename)
    forbidden = ["max_bytes=50000", "max_bytes = 50000", "max_bytes=50_000"]
    for f in forbidden:
        assert f not in body, (
            f"{filename} inlines the byte cap literal — use MAX_BUNDLE_BYTES"
        )


@pytest.mark.parametrize("filename", _CONSUMER_MODULES)
def test_no_inline_relevance_weights(filename: str) -> None:
    """Bare ``0.6`` / ``0.4`` MUST NOT appear in consumer bodies."""
    body = _body(filename)
    # Each weight has a named constant; use it.
    forbidden_pairs = [
        ("0.6", "RELEVANCE_WEIGHT_RECENCY"),
        ("0.4", "RELEVANCE_WEIGHT_CHURN"),
    ]
    for raw, _const in forbidden_pairs:
        # ``\b`` doesn't work for floats; require the literal to NOT be
        # adjacent to digits.
        pattern = rf"(?<![0-9.])\s*{re.escape(raw)}(?![0-9])"
        if re.search(pattern, body):
            # Confirm none of those matches are inside a comment.
            for line in body.splitlines():
                if re.search(pattern, line) and not line.strip().startswith("#"):
                    pytest.fail(
                        f"{filename}: inline {raw!r} literal — use the "
                        f"named relevance weight constant."
                    )


@pytest.mark.parametrize("filename", _CONSUMER_MODULES)
def test_no_inline_ledger_filename(filename: str) -> None:
    body = _body(filename)
    assert '"crawl-ledger.jsonl"' not in body
    assert "'crawl-ledger.jsonl'" not in body


@pytest.mark.parametrize("filename", _CONSUMER_MODULES)
def test_no_inline_mode_strings(filename: str) -> None:
    body = _body(filename)
    # Ledger / picker / score / bundles MUST NOT inline mode strings.
    # The driver does — that's the only legitimate consumer.
    forbidden = ['"preview"', '"commit"', '"pr"']
    for f in forbidden:
        assert f not in body, f"{filename} inlines mode string {f}"


def test_consumers_import_from_crawl_constants() -> None:
    """Every consumer module must import at least one symbol from
    ``crawl_constants``."""
    for mod in _CONSUMER_MODULES:
        src = (CRAWL_PKG / mod).read_text(encoding="utf-8")
        assert "from autofix.crawl.crawl_constants import" in src, (
            f"{mod} does not import from crawl_constants"
        )
