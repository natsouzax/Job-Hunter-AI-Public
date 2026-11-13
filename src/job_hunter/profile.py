"""Candidate supplied profile data and explicit confirmations."""
from .contracts import CandidateProfile, VerifiedFact, utc_now


def load_profile(store):
    profiles = store.list_records("CandidateProfile")
    if len(profiles) != 1:
        raise ValueError("expected one profile; create it with your own details first")
    return profiles[0]


def create_profile(store, data):
    """Create a local profile from candidate supplied, verified facts."""
    if store.list_records("CandidateProfile"):
        raise ValueError("profile already exists")
    if not isinstance(data, dict) or set(data) != {"name", "location", "facts", "availability"}:
        raise ValueError("name, location, facts and availability are required")
    facts = data["facts"]
    if not isinstance(facts, list) or not facts:
        raise ValueError("provide at least one verified fact")
    if not isinstance(data["availability"], list):
        raise ValueError("availability must be a list of windows")
    for fact in facts:
        if not isinstance(fact, dict) or set(fact) != {"category", "statement", "tags"}:
            raise ValueError("each fact needs category, statement and tags")
    profile = CandidateProfile(name=data["name"], location=data["location"], fact_ids=[],
        preferences={}, availability={"status": "unconfirmed", "confirmed_at": None,
                                      "windows": data["availability"]},
        unconfirmed=["disponibilidade atual"])
    # Validate all facts before writing any record.
    checked = [VerifiedFact(profile_id=profile.id, category=f["category"],
        statement=f["statement"], tags=f["tags"], source="user:profile-onboarding",
        verified_at=utc_now()) for f in facts]
    saved = store.save("CandidateProfile", profile.to_dict())
    ids = [store.save("VerifiedFact", f.to_dict())["id"] for f in checked]
    return store.update_profile(saved["id"], expected=saved, changes={"fact_ids": ids})


def confirm_availability(store, profile_id, windows=None):
    profile = store.get("CandidateProfile", profile_id)
    if profile is None:
        raise ValueError("candidate profile missing")
    availability = {"status": "confirmed", "confirmed_at": utc_now(),
                    "windows": profile["availability"]["windows"] if windows is None else windows}
    unconfirmed = [x for x in profile["unconfirmed"] if x != "disponibilidade atual"]
    return store.update_profile(profile_id, expected=profile,
                                changes={"availability": availability, "unconfirmed": unconfirmed})


def set_contact(store, profile_id, *, email=None, phone=None):
    profile = store.get("CandidateProfile", profile_id)
    if profile is None:
        raise ValueError("candidate profile missing")
    contact = {k: v.strip() for k, v in (("email", email), ("phone", phone)) if v and v.strip()}
    if not contact:
        raise ValueError("give at least one of email/phone")
    contact = {**profile["preferences"].get("contact", {}), **contact}
    return store.update_profile(profile_id, expected=profile,
                                changes={"preferences": {**profile["preferences"], "contact": contact}})
