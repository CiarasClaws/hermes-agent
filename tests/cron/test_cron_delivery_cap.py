"""One cron job cannot deliver unbounded text to a messaging client.

On 16/08/2026 a Daily Reset tick echoed its gate word as "[NO-BLOCK]" instead of the
silent marker and looped 2,427 times, delivering 264,583 characters to Telegram and
tripping its flood control. Every individual message was well-formed, so no
content-based guard could have caught it; the bound has to be on LENGTH.

This is the local patch that was carried on 0.15.1, re-ported onto current upstream.
It lives inside `_deliver_result` rather than at a call site because upstream now
reaches delivery from several places — the normal final response, the crash-failure
notice, and the durable queue restart-safe workers use — and a call-site cap would
bound exactly one of them.

⚠ Do not confuse this with `cron.scheduler_prompt._MAX_CONTEXT_CHARS` (8000), which
upstream does have. That bounds what a job's previous output contributes to the NEXT
prompt. This bounds what leaves the machine. Different failure modes.
"""

from cron.scheduler_delivery import _MAX_DELIVERY_CHARS, _cap_delivery

JOB = {"id": "daily-reset"}


def test_an_ordinary_response_passes_through_untouched():
    text = "Morning. Three blocks today, first one at 09:00."
    assert _cap_delivery(JOB, text) is text


def test_a_response_exactly_at_the_cap_is_not_truncated():
    """The boundary belongs to the passing side — a response that fits, ships whole."""
    text = "x" * _MAX_DELIVERY_CHARS
    assert _cap_delivery(JOB, text) == text


def test_the_flood_is_bounded():
    """264,583 chars is the real incident size."""
    out = _cap_delivery(JOB, "x" * 264_583)
    assert out.startswith("x" * _MAX_DELIVERY_CHARS)
    assert len(out) < 264_583


def test_it_truncates_rather_than_drops():
    """Silence is indistinguishable from a job that never ran. A capped delivery must
    still carry the job's own words, and say that it was capped."""
    out = _cap_delivery(JOB, "The gate is open. " + "y" * 100_000)
    assert out.startswith("The gate is open. ")
    assert "truncated" in out


def test_the_notice_names_the_real_size_and_where_the_rest_went():
    out = _cap_delivery(JOB, "z" * 264_583)
    assert "264,583" in out                     # what it was
    assert f"{_MAX_DELIVERY_CHARS:,}" in out    # what the cap is
    assert "save_job_output" in out             # where the untruncated copy lives


def test_the_cap_has_real_headroom_over_a_legitimate_response():
    """Measured over 1,509 cron runs in the week before the incident, the largest
    legitimate response was 7,049 chars. If someone ever tightens this, the test says
    what the number was chosen against."""
    assert _MAX_DELIVERY_CHARS >= 7_049 * 2


# ------------------------------------------------- the wiring, not just the helper

def test_deliver_result_actually_applies_the_cap(monkeypatch):
    """A helper that is correct but unreachable is the same as no cap at all. The
    0.15.1 patch sat at a call site; this asserts the re-port is on the path every
    delivery lane takes, by spying on it from inside `_deliver_result` itself."""
    from cron import scheduler_delivery as sd

    seen = {}

    def spy(job, content):
        seen["len"] = len(content)
        return content[:sd._MAX_DELIVERY_CHARS]

    monkeypatch.setattr(sd, "_cap_delivery", spy)
    # No delivery targets: _deliver_result returns early — but the cap is the first
    # thing it does, so it has already run by then. That is the point.
    monkeypatch.setattr(sd, "_resolve_delivery_targets", lambda job, for_failure=False: [])
    monkeypatch.setattr(sd, "_record_delivery_verification", lambda job, targets: None)
    monkeypatch.setattr(sd, "_unresolved_delivery_outcome", lambda job, for_failure: None)

    sd._deliver_result({"id": "daily-reset"}, "w" * 264_583)
    assert seen["len"] == 264_583, "the cap never ran — _deliver_result bypasses it"
