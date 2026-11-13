"""Derived facts — spec README "Event files" steps 3-4 and "Derived facts". Synthetic data only.
phone_bidx values here are opaque placeholders ("P1"), not real blind indexes."""
import itertools
import random
from datetime import date, datetime

import pytest

from solution.facts import (COMPLETED, IN_PROGRESS_DIALED, IN_PROGRESS_TELEPHONY_FAILED,
                            YET_TO_REACH_OUT, compute_facts)

MR, PF = "motor_renewal", "policy_feedback"
CAT = {
    (MR, "renewed"): (True, True),
    (MR, "callback_requested"): (False, False),
    (MR, "payment_link_sent"): (False, False),
    (MR, "dnd"): (True, False),
    (PF, "feedback_captured"): (True, True),
    (PF, "refused"): (True, False),
}
T0 = "2026-10-05T10:00:00+00:00"
_seq = itertools.count(1)


def lead(lead_id, phone="P1", wf=MR, batch="batch_001"):
    return {"lead_id": lead_id, "workflow_id": wf, "phone_bidx": phone, "batch_id": batch,
            "external_ref": f"REF{lead_id}"}


def ev(event_id, event_type, call_id="c1", at=T0, phone="P1", wf=MR, code=None, payload=None):
    p = dict(payload or {})
    if code is not None:
        p["disposition_code"] = code
    return {"seq": next(_seq), "event_id": event_id, "event_type": event_type, "workflow_id": wf,
            "call_id": call_id, "occurred_at": datetime.fromisoformat(at), "phone_bidx": phone,
            "payload": p}


def ov(event_id, call_id="c1", at=T0, code="renewed", wf=MR):
    """disposition.override: spec says it has no phone, only call_id."""
    return ev(event_id, "disposition.override", call_id=call_id, at=at, phone=None, wf=wf, code=code)


def init(event_id, call_id="c1", at=T0, phone="P1", wf=MR):
    return ev(event_id, "call.initiated", call_id=call_id, at=at, phone=phone, wf=wf)


def dset(event_id, code, call_id="c1", at=T0, phone="P1", wf=MR):
    return ev(event_id, "disposition.set", call_id=call_id, at=at, phone=phone, wf=wf, code=code)


def facts(events, leads=None):
    return compute_facts(leads if leads is not None else [lead(1)], events, CAT)


# ---------------------------------------------------------------- linking / orphans (step 3)

def test_step3_event_links_by_workflow_and_phone_else_orphan():
    f = facts([init("e1"), init("e2", phone="P9"), init("e3", wf=PF), init("e4", phone=None),
               init("e5", wf=None)])
    assert f.orphan_events == 4                       # wrong phone, wrong workflow, no phone, no workflow
    assert f.per_lead[1].total_calls == 1


def test_step3_override_links_via_call_id_to_lead_of_initiated_A6():
    f = facts([init("e1"), ov("e2", code="renewed")])
    assert f.orphan_events == 0
    assert f.per_lead[1].current_disposition == "renewed"


def test_step3_override_whose_call_is_orphan_is_orphan():
    # initiated has an unknown phone -> orphan; the override can only link through it -> orphan too
    f = facts([init("e1", phone="P9"), ov("e2")])
    assert f.orphan_events == 2
    assert f.per_lead[1].bucket == YET_TO_REACH_OUT


def test_step3_override_without_linked_initiated_is_orphan_D14():
    # call c1 has a linked call.connected but no call.initiated -> override is an orphan
    f = facts([ev("e1", "call.connected"), ov("e2"), ov("e3", call_id=None)])
    assert f.orphan_events == 2


def test_step3_override_does_not_link_across_workflows():
    f = facts([init("e1"), ov("e2", wf=PF, code="refused")])
    assert f.orphan_events == 1


def test_D44_ambiguous_call_override_links_to_lead_of_earliest_initiated():
    leads = [lead(1, "P1"), lead(2, "P2")]
    events = [init("e1", phone="P2", at="2026-10-05T10:00:00+00:00"),
              init("e2", phone="P1", at="2026-10-05T09:00:00+00:00"),
              ov("e3", code="renewed")]
    f = facts(events, leads)
    assert f.orphan_events == 0
    assert f.per_lead[1].current_disposition == "renewed"
    assert f.per_lead[2].current_disposition is None


def test_D39_unknown_event_type_counts_for_orphans_but_is_ignored_for_facts():
    f = facts([ev("e1", "", phone="P9"), ev("e2", "call.weird"), ev("e3", "")])
    assert f.orphan_events == 1                       # only the unlinked one
    assert f.per_lead[1].total_calls == 0


def test_D45_linked_event_without_call_id_is_not_orphan_and_not_a_call():
    f = facts([init("e1", call_id=None), dset("e2", "renewed", call_id=None)])
    assert f.orphan_events == 0
    assert f.unknown_disposition_events == 0
    assert f.per_lead[1].total_calls == 0
    assert f.per_lead[1].bucket == YET_TO_REACH_OUT


def test_orphan_resolves_once_lead_exists_final_state():
    # spec heads-up: an event that was an orphan after drop_1 must count once its lead arrives
    events = [init("e1", phone="P2")]
    assert facts(events, [lead(1)]).orphan_events == 1
    f = facts(events, [lead(1), lead(2, "P2")])
    assert f.orphan_events == 0
    assert f.per_lead[2].total_calls == 1


# ---------------------------------------------------------------- unknown disposition (step 4)

def test_step4_unknown_code_on_linked_event_counted_and_ignored():
    f = facts([init("e1"), dset("e2", "renewed", at="2026-10-05T10:01:00+00:00"),
               dset("e3", "bogus", at="2026-10-05T10:02:00+00:00")])
    assert f.unknown_disposition_events == 1
    assert f.orphan_events == 0
    assert f.per_lead[1].current_disposition == "renewed"   # the later bogus set is ignored


def test_step4_unknown_override_ignored_falls_back_to_set():
    f = facts([init("e1"), dset("e2", "callback_requested"), ov("e3", code="bogus")])
    assert f.unknown_disposition_events == 1
    assert f.per_lead[1].current_disposition == "callback_requested"


def test_D22_unknown_code_on_orphan_event_counts_as_orphan_only():
    f = facts([dset("e1", "bogus", phone="P9")])
    assert (f.orphan_events, f.unknown_disposition_events) == (1, 0)


def test_step4_code_valid_only_in_other_workflow_is_unknown():
    f = facts([init("e1"), dset("e2", "feedback_captured")])
    assert f.unknown_disposition_events == 1
    assert f.per_lead[1].current_disposition is None


def test_D21_case_different_code_is_unknown():
    f = facts([init("e1"), dset("e2", "Renewed")])
    assert f.unknown_disposition_events == 1
    assert f.per_lead[1].bucket == IN_PROGRESS_DIALED


def test_D46_missing_or_non_string_code_is_unknown():
    f = facts([init("e1"), ev("e2", "disposition.set"),
               ev("e3", "disposition.set", payload={"disposition_code": 5})])
    assert f.unknown_disposition_events == 2


def test_step4_counted_even_when_call_has_no_initiated():
    f = facts([dset("e1", "bogus")])
    assert (f.orphan_events, f.unknown_disposition_events) == (0, 1)


# ---------------------------------------------------------------- calls

def test_call_without_initiated_not_counted():
    f = facts([ev("e1", "call.connected"), dset("e2", "renewed")])
    lf = f.per_lead[1]
    assert (lf.total_calls, lf.bucket, lf.ever_success) == (0, YET_TO_REACH_OUT, False)
    assert f.per_day == {}


def test_call_date_is_ist_date_of_initiated_20Z_is_next_day():
    f = facts([init("e1", "c1", at="2026-10-05T20:00:00Z"),
               init("e2", "c2", at="2026-10-05T18:29:59Z"),     # 23:59:59 IST, still the 5th
               init("e3", "c3", at="2026-10-06 00:10:00+05:30")])
    assert sorted(f.per_day) == [(MR, date(2026, 10, 5)), (MR, date(2026, 10, 6))]
    assert f.per_day[(MR, date(2026, 10, 6))].total_calls == 2


def test_connected_and_telephony_failed_flags():
    f = facts([init("e1", "c1"), ev("e2", "call.connected", "c1"),
               init("e3", "c2"), ev("e4", "call.telephony_failed", "c2")])
    assert (f.per_lead[1].total_calls, f.per_lead[1].connected_calls) == (2, 1)
    assert f.per_day[(MR, date(2026, 10, 5))].connected_calls == 1


def test_D43_several_initiated_count_once_and_earliest_defines_time():
    f = facts([init("e1", at="2026-10-05T20:00:00Z"), init("e2", at="2026-10-05T10:00:00Z")])
    assert f.per_lead[1].total_calls == 1
    assert f.per_lead[1].last_call_at == datetime.fromisoformat("2026-10-05T10:00:00+00:00")
    assert list(f.per_day) == [(MR, date(2026, 10, 5))]


def test_same_call_id_for_two_leads_is_two_calls():
    # calls are (lead, call_id) groups
    f = facts([init("e1", phone="P1"), init("e2", phone="P2")], [lead(1), lead(2, "P2")])
    assert f.per_day[(MR, date(2026, 10, 5))].total_calls == 2


# ---------------------------------------------------------------- effective disposition

def test_latest_override_beats_a_later_set():
    f = facts([init("e1"), ov("e2", code="callback_requested", at="2026-10-05T11:00:00+00:00"),
               dset("e3", "renewed", at="2026-10-05T12:00:00+00:00")])
    lf = f.per_lead[1]
    assert lf.current_disposition == "callback_requested"
    assert (lf.bucket, lf.ever_success) == (IN_PROGRESS_DIALED, False)


def test_latest_of_two_overrides_wins_by_occurred_at_not_input_order():
    f = facts([init("e1"), ov("e9", code="renewed", at="2026-10-05T12:00:00+00:00"),
               ov("e2", code="dnd", at="2026-10-05T11:00:00+00:00")])
    assert f.per_lead[1].current_disposition == "renewed"


def test_latest_set_wins_when_no_override():
    f = facts([init("e1"), dset("e9", "renewed", at="2026-10-05T10:01:00+00:00"),
               dset("e2", "callback_requested", at="2026-10-05T10:05:00+00:00")])
    assert f.per_lead[1].current_disposition == "callback_requested"


def test_override_only_call():
    f = facts([init("e1"), ov("e2", code="renewed")])
    lf = f.per_lead[1]
    assert (lf.current_disposition, lf.bucket, lf.ever_success) == ("renewed", COMPLETED, True)


def test_A5_tie_on_occurred_at_higher_event_id_wins():
    f = facts([init("e1"), ov("evt_00000010", code="renewed"), ov("evt_00000009", code="dnd")])
    assert f.per_lead[1].current_disposition == "renewed"
    f = facts([init("e1"), dset("evt_00000002", "dnd"), dset("evt_00000003", "callback_requested")])
    assert f.per_lead[1].current_disposition == "callback_requested"


def test_timestamps_compared_as_instants_across_offsets():
    # 10:30+05:30 == 05:00Z, which is earlier than 06:00Z
    f = facts([init("e1"), dset("e2", "renewed", at="2026-10-05T06:00:00Z"),
               dset("e3", "dnd", at="2026-10-05T10:30:00+05:30")])
    assert f.per_lead[1].current_disposition == "renewed"


# ---------------------------------------------------------------- buckets

def test_bucket_yet_to_reach_out():
    assert facts([]).per_lead[1].bucket == YET_TO_REACH_OUT


def test_bucket_completed_via_terminal_success():
    assert facts([init("e1"), dset("e2", "renewed")]).per_lead[1].bucket == COMPLETED


def test_bucket_completed_via_terminal_non_success_dnd():
    lf = facts([init("e1"), dset("e2", "dnd")]).per_lead[1]
    assert (lf.bucket, lf.ever_success) == (COMPLETED, False)


def test_bucket_completed_if_any_call_terminal_even_if_later_call_is_not():
    lf = facts([init("e1", "c1", at="2026-10-05T09:00:00Z"), dset("e2", "dnd", "c1"),
                init("e3", "c2", at="2026-10-05T11:00:00Z"),
                dset("e4", "callback_requested", "c2", at="2026-10-05T11:00:00Z")]).per_lead[1]
    assert (lf.bucket, lf.current_disposition) == (COMPLETED, "callback_requested")


def test_bucket_in_progress_telephony_failed_when_every_call_failed():
    lf = facts([init("e1", "c1"), ev("e2", "call.telephony_failed", "c1"),
                init("e3", "c2"), ev("e4", "call.telephony_failed", "c2")]).per_lead[1]
    assert lf.bucket == IN_PROGRESS_TELEPHONY_FAILED


def test_bucket_one_failed_one_not_answered_is_in_progress_dialed():
    lf = facts([init("e1", "c1"), ev("e2", "call.telephony_failed", "c1"),
                init("e3", "c2"), ev("e4", "call.ended", "c2")]).per_lead[1]
    assert lf.bucket == IN_PROGRESS_DIALED


def test_bucket_completed_beats_telephony_failed():
    lf = facts([init("e1"), ev("e2", "call.telephony_failed"), dset("e3", "dnd")]).per_lead[1]
    assert lf.bucket == COMPLETED


def test_bucket_non_terminal_disposition_is_in_progress_dialed():
    assert facts([init("e1"), dset("e2", "payment_link_sent")]).per_lead[1].bucket == IN_PROGRESS_DIALED


# ---------------------------------------------------------------- per-lead fields

def test_current_disposition_skips_latest_call_without_disposition():
    lf = facts([init("e1", "c1", at="2026-10-05T09:00:00Z"), dset("e2", "callback_requested", "c1"),
                init("e3", "c2", at="2026-10-05T11:00:00Z")]).per_lead[1]
    assert lf.current_disposition == "callback_requested"
    assert lf.last_call_at == datetime.fromisoformat("2026-10-05T11:00:00+00:00")


def test_current_disposition_by_initiated_time_not_disposition_time():
    # c1 initiated later but its disposition is older -> c1 still is the most recent call
    lf = facts([init("e1", "c1", at="2026-10-05T12:00:00Z"),
                dset("e2", "dnd", "c1", at="2026-10-05T12:01:00Z"),
                init("e3", "c2", at="2026-10-05T09:00:00Z"),
                dset("e4", "callback_requested", "c2", at="2026-10-05T13:00:00Z")]).per_lead[1]
    assert lf.current_disposition == "dnd"


def test_D13_initiated_tie_higher_call_id_is_most_recent():
    lf = facts([init("e1", "call_00000002"), dset("e2", "dnd", "call_00000002"),
                init("e3", "call_00000003"), dset("e4", "callback_requested", "call_00000003")]).per_lead[1]
    assert lf.current_disposition == "callback_requested"


def test_ever_success_from_any_call():
    lf = facts([init("e1", "c1", at="2026-10-05T09:00:00Z"), dset("e2", "renewed", "c1"),
                init("e3", "c2", at="2026-10-05T11:00:00Z"),
                dset("e4", "callback_requested", "c2")]).per_lead[1]
    assert (lf.ever_success, lf.current_disposition) == (True, "callback_requested")


def test_last_call_at_is_none_without_calls_and_every_lead_present():
    f = facts([], [lead(1), lead(2, "P2")])
    assert set(f.per_lead) == {1, 2}
    assert f.per_lead[1].last_call_at is None and f.per_lead[1].current_disposition is None


# ---------------------------------------------------------------- per day

def test_success_leads_distinct_leads_per_day():
    leads = [lead(1, "P1"), lead(2, "P2"), lead(3, "P3")]
    d5, d6 = "2026-10-05T05:00:00Z", "2026-10-06T05:00:00Z"
    events = [
        # lead 1: two successful calls on the 5th -> counted once
        init("a1", "c1", at=d5, phone="P1"), dset("a2", "renewed", "c1", at=d5, phone="P1"),
        init("a3", "c2", at=d5, phone="P1"), dset("a4", "renewed", "c2", at=d5, phone="P1"),
        # lead 2: success on the 5th
        init("b1", "c3", at=d5, phone="P2"), ov("b2", "c3", at=d5, code="renewed"),
        # lead 3: non-success call on the 5th, success call on the 6th -> only counts on the 6th
        init("x1", "c4", at=d5, phone="P3"), dset("x2", "callback_requested", "c4", at=d5, phone="P3"),
        init("x3", "c5", at=d6, phone="P3"), dset("x4", "renewed", "c5", at=d6, phone="P3"),
    ]
    f = facts(events, leads)
    assert f.per_day[(MR, date(2026, 10, 5))].success_leads == 2
    assert f.per_day[(MR, date(2026, 10, 5))].total_calls == 4
    assert f.per_day[(MR, date(2026, 10, 6))].success_leads == 1


def test_per_day_split_by_workflow_and_terminal_non_success_is_not_success():
    leads = [lead(1, "P1"), lead(2, "P1", wf=PF)]
    f = facts([init("e1"), dset("e2", "dnd"), init("e3", wf=PF), dset("e4", "feedback_captured", wf=PF)],
              leads)
    assert f.per_day[(MR, date(2026, 10, 5))].success_leads == 0
    assert f.per_day[(PF, date(2026, 10, 5))].success_leads == 1


# ---------------------------------------------------------------- order independence / guards

def _rich_fixture():
    leads = [lead(1, "P1"), lead(2, "P2"), lead(3, "P3", wf=PF), lead(4, "P4")]
    events = [
        init("e01", "c1", at="2026-10-05T20:00:00Z"), ev("e02", "call.connected", "c1"),
        dset("e03", "callback_requested", "c1"), ov("e04", "c1", code="renewed"),
        ov("e05", "c1", code="dnd"),                                   # same time as e04 -> e05 wins
        init("e06", "c2", at="2026-10-05T20:00:00Z"), dset("e07", "payment_link_sent", "c2"),
        init("e08", "c3", phone="P2"), ev("e09", "call.telephony_failed", "c3", phone="P2"),
        init("e10", "c3", phone="P2", at="2026-10-04T10:00:00Z"),      # earlier initiated, same call
        init("e11", "c9", wf=PF, phone="P3"), dset("e12", "Feedback_captured", "c9", wf=PF, phone="P3"),
        dset("e13", "feedback_captured", "c9", wf=PF, phone="P3"),
        ov("e14", "c404"), dset("e15", "bogus", phone="P9"), ev("e16", "", phone="P4"),
    ]
    return leads, events


def test_shuffling_input_event_order_gives_identical_results():
    leads, events = _rich_fixture()
    baseline = compute_facts(leads, events, CAT)
    assert (baseline.orphan_events, baseline.unknown_disposition_events) == (2, 1)
    # c1 and c2 initiated at the same instant -> c2 (higher call_id) is most recent (D13);
    # c1's effective disposition is dnd (override tie, higher event_id) -> terminal -> completed
    lf = baseline.per_lead[1]
    assert (lf.current_disposition, lf.bucket, lf.ever_success) == ("payment_link_sent", COMPLETED, False)
    assert baseline.per_lead[2].total_calls == 1 and baseline.per_lead[3].ever_success is True
    assert baseline.per_lead[4].bucket == YET_TO_REACH_OUT
    for seed in range(25):
        rng = random.Random(seed)
        ev_copy, lead_copy = events[:], leads[:]
        rng.shuffle(ev_copy)
        rng.shuffle(lead_copy)
        assert compute_facts(lead_copy, ev_copy, CAT) == baseline


def test_naive_occurred_at_is_rejected():
    e = init("e1")
    e["occurred_at"] = datetime(2026, 10, 5, 10, 0)
    with pytest.raises(ValueError):
        facts([e])


def test_duplicate_lead_identity_is_rejected():
    with pytest.raises(ValueError):
        facts([], [lead(1), lead(2)])
