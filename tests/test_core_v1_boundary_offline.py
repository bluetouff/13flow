from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path):
    return (ROOT / path).read_text(encoding="utf-8")


REMOVED_DOCS = [
    "docs/" + "COMMERCIAL" + "_MODEL.md",
    "docs/" + "FIRST" + "_COMMERCIAL" + "_OUTREACH.md",
    "docs/" + "GTM" + "_PRODUCT" + "_STATUS.md",
    "docs/" + "PRO" + "_API" + "_ONBOARDING.md",
    "docs/" + "PRO" + "_MCP" + "_REDISTRIBUTION" + "_TERMS.md",
]


def test_core_v1_boundary_documents_maintainable_scope():
    text = _read("docs/CORE_V1_BOUNDARY.md")

    assert "13FLOW Core V1 boundary" in text
    assert "operator issued" in text
    assert "Which research or operator workflow does it unblock?" in text
    assert "/api/pro/v1/workspace/*" in text
    assert "/api/pro/v1/admin/ops" in text
    assert "/api/pro/v1/admin/release-readiness" in text
    assert "/api/pro/v1/admin/pilot-request-assist" in text
    assert "public self-serve checkout" in text
    assert "public submission endpoints that persist prospect PII" in text
    assert "validated alpha" in text
    assert "production x402 settlement" in text
    assert "Prefer extending existing contracts over adding new surfaces" in text
    assert "smoke-pro-key-lifecycle.sh" in text
    assert "routine 13F publication must not require manual filing validation" in text


def test_operator_docs_link_to_core_v1_gate():
    text = _read("README.md")
    assert "docs/CORE_V1_BOUNDARY.md" in text


def test_commercial_marketing_and_client_docs_are_not_tracked_in_repo():
    for path in REMOVED_DOCS:
        assert not (ROOT / path).exists(), path

    for path in ("README.md", "docs/CORE_V1_BOUNDARY.md"):
        text = _read(path)
        for removed_doc in REMOVED_DOCS:
            assert removed_doc not in text, path
        assert "cold email" not in text.lower(), path


def test_readme_marks_browser_auth_and_checkout_as_removed_from_core_v1():
    readme = _read("README.md")

    assert "No browser accounts or checkout" in readme
    assert "/api/pro/v1/admin/release-readiness" in readme
    assert "no public signup" in readme
    assert "no Stripe billing flow" in readme
    assert "Pro API access is operator" in readme
    assert "issued: create a scoped key" in readme
    assert "create-user" not in readme
    assert "POST /api/billing" not in readme
    assert "Stripe Checkout" not in readme
