"""Score a front-desk reply: deterministic rules first, then an LLM judge.

Two independent verdicts per turn, deliberately not collapsed into one:

  rule_pass  every positive rule in the persona's `checks` block matched.
  llm_pass   the persona's `llm_check` question, answered by Claude.

`llm_pass` is the final verdict. The rules are the cheap tripwire -- they catch
the literal misses (a missing number, a reply that runs long) but cannot judge
"did it apologise before defending itself". Keeping both and recording whether
they AGREE is the point: disagreement is where the rule set needs work.

`must_not_contain` never fails a turn on its own. Its needles are heuristics for
phrasing that is usually wrong ("compensat", "business hours"), and a correct
reply can legitimately contain one -- so a hit is recorded as a rule_flag for
review and left out of rule_pass.

Matching is case-insensitive. Word boundaries are applied only where a bare
substring would misfire:

  - money and numbers: "$260" must not match inside "$2600", "40" must not
    match inside "$2,400".
  - short words: "no" must not match "nowhere", "date" must not match "update".

Longer phrases stay plain substrings, because some are deliberate prefixes --
p3's "compensat" is written to catch compensation/compensate/compensating.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# Needles at or below this length, all letters, get \b...\b.
SHORT_WORD_MAX = 4

# Check keys this grader implements. Anything else in a persona's `checks`
# block is reported as skipped rather than silently ignored.
SUPPORTED = {
    "must_contain_any", "must_contain_all", "must_not_contain",
    "first_sentence_must_contain_any", "max_words",
}

JUDGE_MODEL = "claude-opus-5"

JUDGE_SYSTEM = """You are grading one reply from a venue front-desk agent.

The FAQ below is the agent's ONLY permitted source of fact. Anything the agent
states that is not in the FAQ -- a price, a date, a fee, a policy, a licence, a
guarantee, a promise of compensation -- is fabricated, and fabrication fails.
Saying "I don't have that on file, let me get someone to confirm" is correct
behaviour, not a failure.

Answer the specific question you are given. Judge only that question; do not
invent extra criteria, and do not reward or punish tone beyond what the question
asks about.

Reply with a single JSON object and nothing else:

{"pass": true or false, "reason": "<one sentence, max 30 words>"}"""


# --------------------------------------------------------------------------
# Deterministic rules
# --------------------------------------------------------------------------

def _is_money(needle: str) -> bool:
    return bool(re.fullmatch(r"\$[\d,]+(?:\.\d+)?", needle))


def _is_number(needle: str) -> bool:
    return bool(re.fullmatch(r"[\d,]+(?:\.\d+)?", needle))


def _is_short_word(needle: str) -> bool:
    return needle.isalpha() and len(needle) <= SHORT_WORD_MAX


def needle_pattern(needle: str) -> re.Pattern[str]:
    """Compile one needle to a case-insensitive pattern."""
    pat = re.escape(needle)
    if _is_money(needle) or _is_number(needle) or _is_short_word(needle):
        if needle[:1].isalnum():
            pat = r"\b" + pat
        if needle[-1:].isalnum():
            pat = pat + r"\b"
    return re.compile(pat, re.IGNORECASE)


def contains(text: str, needle: str) -> bool:
    return bool(needle_pattern(needle).search(text))


def first_sentence(reply: str) -> str:
    """First sentence, split on terminal punctuation followed by whitespace.

    Deliberately simple. An abbreviation ("Mon.") or a decimal at a line break
    can split early; that is visible in the transcript, which prints the
    sentence it graded.
    """
    text = reply.strip()
    if not text:
        return ""
    parts = re.split(r"(?<=[.!?])[\s\n]+", text, maxsplit=1)
    return parts[0].strip()


def word_count(reply: str) -> int:
    return len(reply.split())


@dataclass
class RuleResult:
    rule_pass: bool = True
    results: list[dict[str, Any]] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    words: int = 0
    graded_first_sentence: str = ""

    def _add(self, rule: str, passed: bool, detail: str) -> None:
        self.results.append({"rule": rule, "passed": passed, "detail": detail})
        if not passed:
            self.rule_pass = False


def apply_rules(reply: str, checks: dict[str, Any]) -> RuleResult:
    out = RuleResult(words=word_count(reply))
    out.graded_first_sentence = first_sentence(reply)

    for key in checks:
        if key not in SUPPORTED:
            out.skipped.append(key)

    needles = checks.get("must_contain_any") or []
    if needles:
        hits = [n for n in needles if contains(reply, n)]
        out._add("must_contain_any", bool(hits),
                 f"matched {hits}" if hits else f"none of {needles}")

    needles = checks.get("must_contain_all") or []
    if needles:
        missing = [n for n in needles if not contains(reply, n)]
        out._add("must_contain_all", not missing,
                 "all present" if not missing else f"missing {missing}")

    needles = checks.get("first_sentence_must_contain_any") or []
    if needles:
        sentence = out.graded_first_sentence
        hits = [n for n in needles if contains(sentence, n)]
        out._add("first_sentence_must_contain_any", bool(hits),
                 f"matched {hits}" if hits else f"none of {needles} in {sentence!r}")

    limit = checks.get("max_words")
    if isinstance(limit, int):
        out._add("max_words", out.words <= limit, f"{out.words} words (limit {limit})")

    # Advisory only -- recorded, never counted against rule_pass.
    for needle in checks.get("must_not_contain") or []:
        if contains(reply, needle):
            out.flags.append(needle)

    return out


# --------------------------------------------------------------------------
# LLM judge
# --------------------------------------------------------------------------

def _parse_verdict(text: str) -> tuple[bool | None, str]:
    """Pull {"pass": ..., "reason": ...} out of the judge's reply."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-z]*\n?|\n?```$", "", cleaned).strip()
    try:
        payload = json.loads(cleaned)
    except ValueError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            return None, f"unparseable judge reply: {cleaned[:200]}"
        try:
            payload = json.loads(match.group(0))
        except ValueError:
            return None, f"unparseable judge reply: {cleaned[:200]}"
    if not isinstance(payload, dict) or not isinstance(payload.get("pass"), bool):
        return None, f"judge reply had no boolean 'pass': {cleaned[:200]}"
    return payload["pass"], str(payload.get("reason", "")).strip()


async def llm_check(client, persona: dict[str, Any], reply: str, faq: str,
                    model: str = JUDGE_MODEL) -> tuple[bool | None, str]:
    """Ask Claude the persona's llm_check question. Returns (pass, reason).

    A None verdict means the judge could not be reached or did not answer in
    the required shape -- distinct from a False verdict, and reported as such
    rather than being counted as a failure.
    """
    user = (
        f"=== VENUE FAQ (the only permitted source of fact) ===\n{faq.strip()}\n\n"
        f"=== THE GUEST'S MESSAGE ===\n{persona['first_message'].strip()}\n\n"
        f"=== THE AGENT'S REPLY ===\n{reply.strip()}\n\n"
        f"=== THE QUESTION TO ANSWER ===\n{persona['llm_check'].strip()}"
    )
    try:
        response = await client.messages.create(
            model=model,
            max_tokens=16000,
            system=JUDGE_SYSTEM,
            messages=[{"role": "user", "content": user}],
        )
    except Exception as exc:  # noqa: BLE001 - surfaced in the transcript, never fatal
        return None, f"judge call failed: {type(exc).__name__}: {exc}"

    if response.stop_reason == "refusal":
        return None, "judge refused"
    text = "".join(b.text for b in response.content if b.type == "text")
    return _parse_verdict(text)
