"""Derived facts (spec README -> "Event files" steps 3-4 and "Derived facts").

Pure functions: no SQL, no file I/O. Everything is recomputed from scratch on every report run
from the full set of leads + ledger events (D2, D22), so incremental == all-at-once.
Input order never matters: every choice uses an explicit sort key (D13).

Inputs
  leads:     [{lead_id, workflow_id, phone_bidx, batch_id, external_ref}]
  events:    [{seq, event_id, event_type, workflow_id, call_id, occurred_at (aware datetime),
               phone_bidx, payload (dict)}]
  catalogue: {(workflow_id, code): (is_terminal, is_success)}   (config.load_dispositions)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

IST = timezone(timedelta(hours=5, minutes=30), "IST")

INITIATED = "call.initiated"
TELEPHONY_FAILED = "call.telephony_failed"
CONNECTED = "call.connected"
ENDED = "call.ended"
DISPOSITION_SET = "disposition.set"
DISPOSITION_OVERRIDE = "disposition.override"
DISPOSITION_TYPES = frozenset({DISPOSITION_SET, DISPOSITION_OVERRIDE})
KNOWN_TYPES = frozenset({INITIATED, TELEPHONY_FAILED, CONNECTED, ENDED}) | DISPOSITION_TYPES

YET_TO_REACH_OUT = "yet_to_reach_out"
COMPLETED = "completed"
IN_PROGRESS_TELEPHONY_FAILED = "in_progress_telephony_failed"
IN_PROGRESS_DIALED = "in_progress_dialed"

Catalogue = Mapping[tuple[str, str], tuple[bool, bool]]


@dataclass(frozen=True)
class Call:
    lead_id: Any
    call_id: str
    initiated_at: datetime                 # earliest linked call.initiated (D43)
    connected: bool
    telephony_failed: bool
    effective_disposition: str | None      # always a catalogue code for this workflow, or None


@dataclass(frozen=True)
class LeadFacts:
    total_calls: int
    connected_calls: int
    last_call_at: datetime | None          # aware; reports format it in IST
    current_disposition: str | None
    ever_success: bool
    bucket: str


@dataclass(frozen=True)
class DayFacts:
    total_calls: int
    connected_calls: int
    success_leads: int


@dataclass(frozen=True)
class Facts:
    orphan_events: int
    unknown_disposition_events: int
    per_lead: dict[Any, LeadFacts]                  # every lead, including yet_to_reach_out
    per_day: dict[tuple[str, date], DayFacts]       # only (workflow_id, IST date) with >= 1 call


def ist_date(ts: datetime) -> date:
    """Spec: "All dates in reports are IST dates"; a call's date is the IST date of call.initiated."""
    return ts.astimezone(IST).date()


def _when(e: Mapping[str, Any]) -> tuple[datetime, str]:
    """Order key for events: occurred_at, then event_id (D13: higher event_id wins a tie for "latest")."""
    return (e["occurred_at"], e["event_id"])


def _check_event(e: Mapping[str, Any]) -> None:
    ts = e["occurred_at"]
    if not isinstance(ts, datetime) or ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError(f"event {e.get('event_id')!r}: occurred_at must be an aware datetime")


def _lead_index(leads: Iterable[Mapping[str, Any]]) -> tuple[dict[tuple[str, str], Any], dict[Any, str]]:
    by_phone: dict[tuple[str, str], Any] = {}
    workflow_of: dict[Any, str] = {}
    for lead in leads:
        key = (lead["workflow_id"], lead["phone_bidx"])
        if key in by_phone or lead["lead_id"] in workflow_of:
            raise ValueError(f"duplicate lead identity for lead_id {lead['lead_id']!r}")
        by_phone[key] = lead["lead_id"]
        workflow_of[lead["lead_id"]] = lead["workflow_id"]
    return by_phone, workflow_of


def link_events(leads: Iterable[Mapping[str, Any]], events: list[Mapping[str, Any]]
                ) -> tuple[list[tuple[Mapping[str, Any], Any]], int]:
    """Spec step 3 "Orphan". Returns ([(event, lead_id)] for linked events, orphan count).

    - Non-override events link via (workflow_id, phone_bidx); a missing value never links (D16).
      This includes unrecognised event types, which still count for linking/orphans (D39).
    - disposition.override links via (workflow_id, call_id) to the lead of that call's linked
      call.initiated (A6/D14); if several such initiated events link to different leads, the
      earliest by (occurred_at, event_id) decides (D44). Otherwise the override is an orphan.
    """
    by_phone, _ = _lead_index(leads)
    linked: list[tuple[Mapping[str, Any], Any]] = []
    orphans = 0
    overrides: list[Mapping[str, Any]] = []
    call_owner: dict[tuple[str, str], tuple[tuple[datetime, str], Any]] = {}

    for e in events:
        _check_event(e)
        if e["event_type"] == DISPOSITION_OVERRIDE:
            overrides.append(e)
            continue
        wf, bidx = e["workflow_id"], e["phone_bidx"]
        lead_id = by_phone.get((wf, bidx)) if wf is not None and bidx is not None else None
        if lead_id is None:
            orphans += 1
            continue
        linked.append((e, lead_id))
        if e["event_type"] == INITIATED and e["call_id"] is not None:
            key, when = (wf, e["call_id"]), _when(e)
            if key not in call_owner or when < call_owner[key][0]:
                call_owner[key] = (when, lead_id)

    for e in overrides:
        wf, call_id = e["workflow_id"], e["call_id"]
        owner = call_owner.get((wf, call_id)) if wf is not None and call_id is not None else None
        if owner is None:
            orphans += 1
        else:
            linked.append((e, owner[1]))
    return linked, orphans


def _code(e: Mapping[str, Any]) -> str | None:
    payload = e.get("payload")
    code = payload.get("disposition_code") if isinstance(payload, Mapping) else None
    return code if isinstance(code, str) else None


def build_calls(linked: list[tuple[Mapping[str, Any], Any]], workflow_of: Mapping[Any, str],
                catalogue: Catalogue) -> tuple[list[Call], int]:
    """Spec step 4 "Unknown disposition" (applied to linked events only, D22) + calls.

    Returns (calls, unknown_disposition_events).
    - A linked disposition.* whose code is not in the catalogue for its workflow (exact match, D21;
      missing/non-string code included, D46) is counted and ignored.
    - Unrecognised event types are ignored here (D39). Linked events with no call_id belong to no
      call (D45).
    - A call = (lead, call_id) group with at least one linked call.initiated; the earliest
      (occurred_at, event_id) initiated defines its time and IST date (D43).
    - Effective disposition: latest override by (occurred_at, event_id), else latest set, else None.
    """
    unknown = 0
    groups: dict[tuple[Any, str], list[Mapping[str, Any]]] = {}
    for e, lead_id in linked:
        etype = e["event_type"]
        if etype not in KNOWN_TYPES:
            continue
        if etype in DISPOSITION_TYPES and (workflow_of[lead_id], _code(e)) not in catalogue:
            unknown += 1
            continue
        if e["call_id"] is None:
            continue
        groups.setdefault((lead_id, e["call_id"]), []).append(e)

    calls: list[Call] = []
    for (lead_id, call_id), evs in groups.items():
        initiated = [e for e in evs if e["event_type"] == INITIATED]
        if not initiated:                    # spec: counted only if call.initiated is present
            continue
        overrides = [e for e in evs if e["event_type"] == DISPOSITION_OVERRIDE]
        sets = [e for e in evs if e["event_type"] == DISPOSITION_SET]
        chosen = max(overrides or sets, key=_when, default=None)
        types = {e["event_type"] for e in evs}
        calls.append(Call(lead_id=lead_id, call_id=call_id,
                          initiated_at=min(initiated, key=_when)["occurred_at"],
                          connected=CONNECTED in types,
                          telephony_failed=TELEPHONY_FAILED in types,
                          effective_disposition=_code(chosen) if chosen is not None else None))
    return calls, unknown


def _call_order(c: Call) -> tuple[datetime, str]:
    """Most recent call by initiated time; ties -> higher call_id (D13)."""
    return (c.initiated_at, c.call_id)


def lead_facts(calls: list[Call], workflow_id: str, catalogue: Catalogue) -> LeadFacts:
    """Per-lead facts and bucket (spec "Derived facts" bucket order, first match wins)."""
    flags = [catalogue[(workflow_id, c.effective_disposition)] for c in calls
             if c.effective_disposition is not None]
    if not calls:
        bucket = YET_TO_REACH_OUT
    elif any(is_terminal for is_terminal, _ in flags):
        bucket = COMPLETED
    elif all(c.telephony_failed for c in calls):
        bucket = IN_PROGRESS_TELEPHONY_FAILED
    else:
        bucket = IN_PROGRESS_DIALED
    with_dispo = [c for c in calls if c.effective_disposition is not None]
    return LeadFacts(
        total_calls=len(calls),
        connected_calls=sum(c.connected for c in calls),
        last_call_at=max(calls, key=_call_order).initiated_at if calls else None,
        current_disposition=(max(with_dispo, key=_call_order).effective_disposition
                             if with_dispo else None),
        ever_success=any(is_success for _, is_success in flags),
        bucket=bucket)


def day_facts(calls: list[Call], workflow_of: Mapping[Any, str], catalogue: Catalogue
              ) -> dict[tuple[str, date], DayFacts]:
    """daily_overview: per (workflow_id, IST date of initiated) with >= 1 call.
    success_leads = distinct leads with >= 1 call on that date whose effective disposition is_success."""
    totals: dict[tuple[str, date], list[int]] = {}
    success: dict[tuple[str, date], set[Any]] = {}
    for c in calls:
        wf = workflow_of[c.lead_id]
        key = (wf, ist_date(c.initiated_at))
        t = totals.setdefault(key, [0, 0])
        t[0] += 1
        t[1] += c.connected
        s = success.setdefault(key, set())
        if c.effective_disposition is not None and catalogue[(wf, c.effective_disposition)][1]:
            s.add(c.lead_id)
    return {k: DayFacts(total_calls=t[0], connected_calls=t[1], success_leads=len(success[k]))
            for k, t in totals.items()}


def compute_facts(leads: list[Mapping[str, Any]], events: list[Mapping[str, Any]],
                  catalogue: Catalogue) -> Facts:
    _, workflow_of = _lead_index(leads)
    linked, orphans = link_events(leads, events)
    calls, unknown = build_calls(linked, workflow_of, catalogue)
    by_lead: dict[Any, list[Call]] = {lead_id: [] for lead_id in workflow_of}
    for c in calls:
        by_lead[c.lead_id].append(c)
    return Facts(
        orphan_events=orphans,
        unknown_disposition_events=unknown,
        per_lead={lead_id: lead_facts(cs, workflow_of[lead_id], catalogue)
                  for lead_id, cs in by_lead.items()},
        per_day=day_facts(calls, workflow_of, catalogue))
