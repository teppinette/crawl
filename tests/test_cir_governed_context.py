import hashlib
import json
import sys
import types
from pathlib import Path

import pytest


API_DIR = Path(__file__).resolve().parents[1] / "api"
sys.path.insert(0, str(API_DIR))

# The repository's deploy image owns FastAPI/Pydantic/PostgreSQL dependencies;
# these contract tests exercise pure validation/persistence orchestration in a
# lightweight source checkout, so provide narrow import doubles when they are
# not installed on the workstation running pytest.
try:
    import fastapi  # noqa: F401
except ImportError:
    fastapi_stub = types.ModuleType("fastapi")

    class _Router:
        def __init__(self, *args, **kwargs):
            pass

        def post(self, *args, **kwargs):
            return lambda fn: fn

    class _HTTPException(Exception):
        def __init__(self, status_code, detail):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    fastapi_stub.APIRouter = _Router
    fastapi_stub.HTTPException = _HTTPException
    sys.modules["fastapi"] = fastapi_stub

try:
    import pydantic  # noqa: F401
except ImportError:
    pydantic_stub = types.ModuleType("pydantic")
    pydantic_stub.BaseModel = object
    pydantic_stub.Field = lambda default=None, **_kwargs: default
    sys.modules["pydantic"] = pydantic_stub

evidence_db_stub = types.ModuleType("evidence_db")
evidence_db_stub.add_evidence = lambda *_args, **_kwargs: None
evidence_db_stub.get_run = lambda *_args, **_kwargs: None
evidence_db_stub.list_evidence = lambda *_args, **_kwargs: []
sys.modules["evidence_db"] = evidence_db_stub

import cir_orchestrator as cir  # noqa: E402
import counterparty_llm as cllm  # noqa: E402


def _context(name="Chisage Resource (Singapore) PTE. LTD.", entity_id="cp-1"):
    value = {
        "contract": {
            "name": "copap.onboarding.cir-governed-context",
            "version": 1,
        },
        "generated_at": "2026-08-10T12:00:00+00:00",
        "subject": {
            "entity_id": entity_id,
            "legal_name": name,
            "country": "SG",
            "compliance_entity_id": "9001",
        },
        "parent": {
            "linked": False,
            "declared_name": "Chisage Holding Group Co., Ltd.",
        },
        "subsidiaries": [],
        "reported_subsidiaries": [{"name": "Zhongzhe Metals Co."}],
        "relationships": [],
        "group_memberships": [],
        "directors": [{"name": "Example Director", "role": "Director"}],
        "beneficial_owners": [],
        "registrations": [],
        "stored_intelligence": {
            "scale_tier": "LARGE",
            "classification": "INTERNAL_DERIVED",
        },
        "quality": {"state": "COMPLETE", "limitations": [], "counts": {}},
        "interpretation_policy": {
            "external_not_found_means": "not_externally_corroborated",
            "external_not_found_does_not_mean": "internal_record_absent",
        },
    }
    canonical = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    value["context_fingerprint"] = hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()
    return value


def test_valid_context_is_bound_to_subject_and_ids():
    context = _context()

    result = cir._validate_governed_context(
        context,
        entity_name="  CHISAGE RESOURCE (SINGAPORE) PTE. LTD. ",
        onboarding_entity_id="cp-1",
        compliance_entity_id="9001",
    )

    assert result is context


@pytest.mark.parametrize("change", ["name", "entity_id", "payload"])
def test_context_rejects_wrong_identity_or_tampering(change):
    context = _context()
    name, entity_id = context["subject"]["legal_name"], "cp-1"
    if change == "name":
        name = "Different Entity Ltd"
    elif change == "entity_id":
        entity_id = "cp-2"
    else:
        context["parent"]["declared_name"] = "Injected Parent Ltd"

    with pytest.raises(ValueError):
        cir._validate_governed_context(
            context,
            entity_name=name,
            onboarding_entity_id=entity_id,
            compliance_entity_id="9001",
        )


def test_onboarding_context_is_persisted_as_internal_governed_evidence(monkeypatch):
    captured = {}

    def add_evidence(run_id, **kwargs):
        captured.update({"run_id": run_id, **kwargs})
        return "evidence-1"

    monkeypatch.setattr(cir.evidence_db, "add_evidence", add_evidence)

    evidence_id = cir._persist_onboarding_governed("run-1", _context())

    assert evidence_id == "evidence-1"
    assert captured["source_id"] == "onboarding_governed"
    assert captured["extracted"]["source_tier"] == "INTERNAL_GOVERNED"
    assert captured["extracted"]["context"]["parent"]["declared_name"].startswith(
        "Chisage Holding Group"
    )
    assert captured["raw_content"]


def test_governed_directors_and_ubos_feed_principal_screening():
    extracted = {
        "context": {
            "directors": [{"name": "Example Director", "role": "Director"}],
            "beneficial_owners": [{
                "name": "Example Beneficial Owner",
                "ubo_type": "Individual",
            }],
        },
    }

    assert cir._named_people_from_evidence(extracted) == [
        ("Example Director", "Director"),
    ]
    assert cir._named_people_from_evidence(
        extracted, include_ubos=True,
    ) == [
        ("Example Director", "Director"),
        ("Example Beneficial Owner", "Individual"),
    ]


def test_mdm_falls_back_to_compliance_entity_id_without_cpid(monkeypatch):
    calls = []
    evidence = {}

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"data": {"9001": {"role": "Supplier", "in_cietrade": False}}}

    def post(url, **kwargs):
        calls.append((url, kwargs["json"]))
        return Response()

    monkeypatch.setenv("MDM_M2M_TOKEN", "configured-test-token")
    monkeypatch.setattr(
        cir.evidence_db, "get_run",
        lambda _run_id: {"meta": {"cpid": "", "compliance_entity_id": "9001"}},
    )
    monkeypatch.setattr("requests.post", post)
    monkeypatch.setattr(
        cir.evidence_db, "add_evidence",
        lambda _run_id, **kwargs: evidence.update(kwargs),
    )

    cir._mdm_governed_persist("run-1", "SG", "Example Trading Ltd")

    assert calls[0][0].endswith("/api/m2m/counterparty-role-by-entity")
    assert calls[0][1] == {"entity_ids": ["9001"]}
    assert evidence["source_id"] == "mdm_governed"
    assert evidence["extracted"]["counterparty_role"]["role"] == "Supplier"
    assert evidence["extracted"]["compliance_entity_id"] == "9001"


def test_governed_registration_prevents_external_not_found_from_erasing_identity():
    context = _context()
    context["registrations"] = [{
        "registration_number": "SG-REG-1",
        "registered_name": context["subject"]["legal_name"],
        "evidence_origin": "document",
    }]
    unsigned = dict(context)
    unsigned.pop("context_fingerprint")
    context["context_fingerprint"] = hashlib.sha256(json.dumps(
        unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()
    evidence = [
        {
            "id": "a1b2c3d4-0000-0000-0000-000000000000",
            "source_id": "onboarding_governed",
            "source_tier": "INTERNAL_GOVERNED",
            "extracted": {"context": context},
        },
        {
            "id": "d4c3b2a1-0000-0000-0000-000000000000",
            "source_id": "sg_registry",
            "source_tier": "PRIMARY_GOVERNMENT",
            "extracted": {"found": False},
        },
    ]

    grade = cllm._grounding_rating(
        "- Onboarding registration recorded [Ea1b2c3d4]",
        evidence,
    )["rating"]

    assert grade["primary_collected"] is False
    assert grade["governed_identity_collected"] is True
    assert grade["identity_collected"] is True
    assert grade["identity_basis"] == "GOVERNED_ONBOARDING_REGISTRATION"
    assert not grade["verdict"].startswith("HOLD")
