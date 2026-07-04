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


def test_readme_marks_legacy_full_build_surfaces_as_outside_current_pilot():
    readme = _read("README.md")

    assert "Accounts & auth (legacy/full build, not current public pilot)" in readme
    assert "Billing (Stripe) - legacy/full build, not current public pilot" in readme
    assert "does not expose browser account management" in readme
    assert "does not use public Stripe checkout" in readme
