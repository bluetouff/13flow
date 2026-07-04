from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def test_core_v1_boundary_documents_maintainable_scope():
    text = _read("docs/CORE_V1_BOUNDARY.md")

    assert "13FLOW Core V1 boundary" in text
    assert "controlled pilot" in text
    assert "Which paying workflow does it unblock?" in text
    assert "/api/pro/v1/workspace/*" in text
    assert "/api/pro/v1/admin/ops" in text
    assert "/api/pro/v1/admin/pilot-request-assist" in text
    assert "public self-serve checkout" in text
    assert "public submission endpoints that persist prospect PII" in text
    assert "validated alpha" in text
    assert "production x402 settlement" in text
    assert "Prefer extending existing contracts over adding new surfaces" in text
    assert "smoke-pro-key-lifecycle.sh" in text
    assert "routine 13F publication must not require manual filing validation" in text


def test_commercial_docs_link_to_core_v1_gate():
    for path in (
        "README.md",
        "docs/GTM_PRODUCT_STATUS.md",
        "docs/COMMERCIAL_MODEL.md",
        "docs/PRO_API_ONBOARDING.md",
    ):
        text = _read(path)
        assert "docs/CORE_V1_BOUNDARY.md" in text, path


def test_readme_marks_browser_auth_and_checkout_as_removed_from_core_v1():
    readme = _read("README.md")

    assert "No browser accounts or checkout" in readme
    assert "no public signup" in readme
    assert "no Stripe billing flow" in readme
    assert "operator-reviewed Pro API access" in readme
    assert "create-user" not in readme
    assert "POST /api/billing" not in readme
    assert "Stripe Checkout" not in readme
