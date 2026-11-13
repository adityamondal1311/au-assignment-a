import pytest

from solution.config import (REPO_ROOT, defaults, load_dispositions, load_schemas, pii_fields,
                             reference_field)


@pytest.fixture(scope="module")
def schemas():
    return load_schemas()


@pytest.fixture(scope="module")
def dispositions():
    return load_dispositions()


def test_repo_root_is_independent_of_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert (REPO_ROOT / "assignment-a-data" / "schemas").is_dir()
    assert set(load_schemas()) == {"motor_renewal", "policy_feedback"}


def test_both_workflows_load(schemas):
    assert set(schemas) == {"motor_renewal", "policy_feedback"}


def test_reference_field_is_external_ref_source(schemas):
    # Spec: external_ref = "the value of the schema field marked "reference": true"
    assert reference_field(schemas["motor_renewal"]) == "policy_number"
    assert reference_field(schemas["policy_feedback"]) == "order_id"


def test_reference_field_must_be_unique():
    two = {"workflow_id": "x", "fields": [{"name": "a", "reference": True}, {"name": "b", "reference": True}]}
    none = {"workflow_id": "x", "fields": [{"name": "a"}]}
    for schema in (two, none):
        with pytest.raises(ValueError):
            reference_field(schema)


def test_pii_fields_are_name_and_phone(schemas):
    # Spec: fields marked "pii": true must not be stored in plaintext
    for wf in ("motor_renewal", "policy_feedback"):
        assert pii_fields(schemas[wf]) == ["customer_name", "phone"]


def test_renewed_is_terminal_and_success(dispositions):
    assert dispositions[("motor_renewal", "renewed")] == (True, True)


def test_feedback_callback_requested_is_non_terminal(dispositions):
    is_terminal, _ = dispositions[("policy_feedback", "callback_requested")]
    assert is_terminal is False


def test_same_code_in_two_workflows_is_two_keys(dispositions):
    # Spec: unknown disposition = code not in dispositions.csv *for that workflow*
    assert ("motor_renewal", "callback_requested") in dispositions
    assert ("policy_feedback", "callback_requested") in dispositions
    assert ("motor_renewal", "feedback_captured") not in dispositions


def test_codes_are_case_sensitive(dispositions):
    # Decision D21: disposition codes match exactly
    assert ("motor_renewal", "Renewed") not in dispositions


def test_booleans_parsed_strictly(tmp_path):
    bad = tmp_path / "d.csv"
    bad.write_text("workflow_id,code,label,is_terminal,is_success\nw,c,L,True,false\n", encoding="utf-8")
    with pytest.raises(ValueError, match="'true' or 'false'"):
        load_dispositions(bad)


def test_defaults_preferred_language_en(schemas):
    # Spec: "Defaults (for example preferred_language -> en)"
    assert defaults(schemas["motor_renewal"]) == {"preferred_language": "en"}
    assert defaults(schemas["policy_feedback"]) == {"preferred_language": "en"}
