"""Scoring checks. Determinism is the one the pipeline depends on.

    python -m api.test_scoring
"""

from __future__ import annotations

from api.scoring import score_lead


def test_determinism() -> None:
    """Same inputs, same output, 10x -- P5 exit criterion."""
    kwargs = dict(
        visitor_name="Ayesha Khan",
        visitor_email="ayesha@acme.pk",
        company_name="Acme Retail",
        website_url="https://acme.pk",
        industry="retail",
        service_recommended="/seo/local-seo",
        messages_count=7,
        pages_visited=["/", "/seo", "/seo/local-seo"],
        transcript_text="We need this done this month.",
    )
    results = [score_lead(**kwargs) for _ in range(10)]
    assert len({(r.status, r.points) for r in results}) == 1, results
    assert results[0].status == "hot", results[0]


def test_anonymous_is_cold() -> None:
    s = score_lead(messages_count=2, transcript_text="what is local seo")
    assert s.status == "cold", s
    assert s.points == 0, s


def test_email_only_is_warm() -> None:
    s = score_lead(visitor_name="Sam", visitor_email="s@x.com", messages_count=3)
    assert s.status == "warm", s


def test_booking_overrides_to_hot() -> None:
    """Committed action beats a low point total."""
    s = score_lead(visitor_email="s@x.com", meeting_booked=True, messages_count=2)
    assert s.status == "hot", s
    assert s.points >= 8, s


def test_proposal_overrides_to_hot() -> None:
    s = score_lead(visitor_email="s@x.com", proposal_requested=True)
    assert s.status == "hot", s


def test_student_is_cold_despite_engagement() -> None:
    """Self-disqualification beats otherwise-hot signals."""
    s = score_lead(
        visitor_name="Ali",
        visitor_email="ali@uni.edu",
        company_name="Uni",
        website_url="https://uni.edu",
        industry="education",
        messages_count=12,
        pages_visited=["/", "/seo", "/development", "/marketing"],
        transcript_text="I'm a student doing research for a school project.",
    )
    assert s.status == "cold", s


def test_booking_beats_self_disqualification() -> None:
    """Someone who booked a slot is qualified whatever they said earlier."""
    s = score_lead(
        visitor_email="a@b.com",
        meeting_booked=True,
        transcript_text="just curious really",
    )
    assert s.status == "hot", s


def test_engagement_alone_cannot_reach_hot() -> None:
    """No contact detail means nobody can act on it, however long the chat."""
    s = score_lead(
        messages_count=40,
        pages_visited=["/", "/seo", "/development", "/marketing", "/designing"],
        transcript_text="tell me more. and more. and more.",
    )
    assert s.status != "hot", s


def test_urgency_lifts_warm_to_hot() -> None:
    base = dict(
        visitor_name="Bilal",
        visitor_email="b@co.pk",
        company_name="Co",
        messages_count=5,
    )
    calm = score_lead(**base, transcript_text="sometime later this year maybe")
    urgent = score_lead(**base, transcript_text="We need this done ASAP.")
    assert urgent.points > calm.points, (calm, urgent)


def test_phone_only_is_reachable() -> None:
    """A phone number alone is as workable as an email alone."""
    email_only = score_lead(visitor_email="a@b.com")
    phone_only = score_lead(visitor_phone="+92 300 1234567")
    assert phone_only.points == email_only.points, (phone_only, email_only)


def test_reasons_explain_the_score() -> None:
    s = score_lead(visitor_email="a@b.com", transcript_text="urgent please")
    assert any("email" in r for r in s.reasons), s.reasons
    assert any("urgency" in r for r in s.reasons), s.reasons


def demo() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  OK  {name}")
    print("scoring: all checks passed")


if __name__ == "__main__":
    demo()
