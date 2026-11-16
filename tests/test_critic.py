"""Tests for the critic: the blind order and how Python maps A and B back.

No real agent runs: critic.ask is replaced with a fake that always answers the same letter.
"""

import json

import pytest

from agentic_os import critic


class FixedOrder:
    """A fake random generator: random() always returns the same number."""

    def __init__(self, value):
        self.value = value

    def random(self):
        return self.value


RESULT_FIRST = FixedOrder(0.1)  # below 0.5: the result is answer A
REFERENCE_FIRST = FixedOrder(0.9)  # the reference is answer A


@pytest.fixture
def fake_critic(tmp_path, monkeypatch):
    """Replace the agent. Return a dict: set "better" to the letter it answers;
    after a call, "prompt" has the prompt it got."""
    monkeypatch.setattr(critic, "LOG_FILE", tmp_path / "critic_log.jsonl")
    monkeypatch.setattr(critic, "DATA_DIR", tmp_path)
    monkeypatch.setattr(critic, "CRITIC_DIR", tmp_path / "critic")
    state = {"better": "A"}

    def ask(prompt, **options):
        state["prompt"] = prompt
        output = {"better": state["better"], "why": "clearer", "fix": "be shorter"}
        return {"structured_output": output, "total_cost_usd": 0.02}

    monkeypatch.setattr(critic, "ask", ask)
    return state


@pytest.mark.parametrize("order, better, winner", [
    (RESULT_FIRST, "A", "result"),
    (RESULT_FIRST, "B", "reference"),
    (REFERENCE_FIRST, "A", "reference"),
    (REFERENCE_FIRST, "B", "result"),
])
def test_the_letter_is_mapped_back(fake_critic, order, better, winner):
    fake_critic["better"] = better

    verdict = critic.compare("Explain X.", "new text", "good text", rng=order)

    assert verdict["winner"] == winner


def test_the_prompt_is_blind(fake_critic):
    critic.compare("Explain X.", "new text", "good text", rng=REFERENCE_FIRST)

    prompt = fake_critic["prompt"]
    assert prompt.index("good text") < prompt.index("new text")  # the reference is A
    assert "reference" not in prompt.lower() and "result" not in prompt.lower()


def test_every_verdict_is_logged(fake_critic, tmp_path):
    critic.compare("Explain X.", "new text", "good text", rng=RESULT_FIRST)

    [line] = (tmp_path / "critic_log.jsonl").read_text().splitlines()
    saved = json.loads(line)
    assert saved["winner"] == "result" and saved["fix"] == "be shorter" and saved["cost"] == 0.02


def test_no_clear_choice_is_an_error(fake_critic):
    fake_critic["better"] = "both"

    with pytest.raises(ValueError, match="no clear choice"):
        critic.compare("Explain X.", "new text", "good text")
