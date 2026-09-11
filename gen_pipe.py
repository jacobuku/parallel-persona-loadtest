r"""Generate a fan-out/fan-in load-test pipeline from the persona inputs.

Reads `personas.json`, `faq.md`, and `prompts/frontdesk_v1.md`, and writes a
`.pipe` whose topology is:

    webhook_1 --text--> question_1 --questions--> agent_p1 --answers--\
                                  \-------------> agent_p2 --answers---> response_answers_1
                                  \-------------> agent_pN --answers--/

Fan-out is topological: every branch agent lists the SAME `from` (`question_1`)
in its `input`. Fan-in is topological too: the single response node lists one
`input` entry per branch, all on the `answers` lane. Per the docs' execution
model, independent branches then run concurrently across engine threads --
there is no wave/parallel field in the .pipe schema to set.

Each branch's system prompt is: front-desk persona + FAQ + that persona's
first_message as the situation to handle.

Every provider name used here is one verified in NOTES.md / the workshop pipes:
webhook, question, agent_deepagent, llm_anthropic, response_answers.
None are guessed.

Usage:
    python gen_pipe.py --n 2                      # parallel pipe, first 2 personas
    python gen_pipe.py --n 2 --serial             # N single-branch pipes
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
PERSONAS_PATH = ROOT / "personas.json"
FAQ_PATH = ROOT / "faq.md"
FRONTDESK_PATH = ROOT / "prompts" / "frontdesk_v1.md"
PIPELINES_DIR = ROOT / "pipelines"

# Anthropic profile name as it appears in the workshop pipes.
LLM_PROFILE = "claude-sonnet-4-6"

# Stable project id for every generated pipe.
PROJECT_ID = "3e8b17a0-6c24-4f5b-9d81-0a7e5c42d9f1"

# Canvas spacing, purely cosmetic.
X_WEBHOOK, X_QUESTION, X_AGENT, X_LLM, X_RESPONSE = 50, 320, 620, 620, 960
Y_TOP, Y_STEP = 120, 260


def load_inputs() -> tuple[list[dict[str, Any]], str, str]:
    """Read the three source documents, failing loudly if any is missing."""
    missing = [p for p in (PERSONAS_PATH, FAQ_PATH, FRONTDESK_PATH) if not p.exists()]
    if missing:
        raise SystemExit("[fatal] missing input(s): " + ", ".join(str(p) for p in missing))

    personas = json.loads(PERSONAS_PATH.read_text(encoding="utf-8"))
    if not isinstance(personas, list) or not personas:
        raise SystemExit("[fatal] personas.json must be a non-empty JSON array")
    for i, p in enumerate(personas):
        for field in ("id", "first_message"):
            if not isinstance(p, dict) or not p.get(field):
                raise SystemExit(f"[fatal] personas.json[{i}] is missing '{field}'")

    return personas, FAQ_PATH.read_text(encoding="utf-8"), FRONTDESK_PATH.read_text(encoding="utf-8")


def build_system_prompt(frontdesk: str, faq: str, persona: dict[str, Any]) -> str:
    """Front-desk persona + FAQ + this persona's opening message as the situation."""
    return (
        f"{frontdesk.strip()}\n\n"
        f"---\n\n"
        f"{faq.strip()}\n\n"
        f"---\n\n"
        f"## This conversation\n\n"
        f"The guest ({persona.get('name') or persona['id']}) has just opened the chat with:\n\n"
        f"> {persona['first_message'].strip()}\n\n"
        f"Reply to that message as the front desk agent. You have no tools; answer "
        f"directly from the FAQ above. Reply with plain text only -- no JSON, no preamble."
    )


def agent_node(node_id: str, name: str, system_prompt: str, y: int) -> dict[str, Any]:
    """A tool-less agent_deepagent branch. Input lane `questions` from question_1."""
    return {
        "id": node_id,
        "provider": "agent_deepagent",
        "name": name,
        "config": {
            "profile": "default",
            "default": {
                "advanced_mode": True,
                "agent_description": f"Front desk agent handling the '{name}' guest scenario.",
                "system_prompt": system_prompt,
            },
            "name": name,
        },
        "ui": {"position": {"x": X_AGENT, "y": y}, "nodeType": "default", "formDataValid": True},
        # FAN-OUT: every branch reads the same upstream node.
        "input": [{"lane": "questions", "from": "question_1"}],
    }


def llm_node(node_id: str, agent_id: str, y: int) -> dict[str, Any]:
    """Anthropic LLM control-attached to its branch agent (never a data-lane node)."""
    return {
        "id": node_id,
        "provider": "llm_anthropic",
        "name": f"Anthropic {agent_id}",
        "config": {
            "profile": LLM_PROFILE,
            LLM_PROFILE: {"modelSource": "manual", "apikey": "${ROCKETRIDE_ANTHROPIC_KEY}"},
            "name": f"Anthropic {agent_id}",
        },
        "ui": {"position": {"x": X_LLM, "y": y + 110}, "nodeType": "default", "formDataValid": True},
        "control": [{"classType": "llm", "from": agent_id}],
    }


def build_pipeline(personas: list[dict[str, Any]], faq: str, frontdesk: str) -> dict[str, Any]:
    """Assemble the full component list for the given personas."""
    components: list[dict[str, Any]] = [
        {
            "id": "webhook_1",
            "provider": "webhook",
            "name": "Web Hook",
            "config": {"hideForm": True, "mode": "Source", "parameters": {}, "type": "webhook",
                       "name": "Web Hook"},
            "ui": {"position": {"x": X_WEBHOOK, "y": Y_TOP}, "nodeType": "default",
                   "formDataValid": True},
        },
        {
            "id": "question_1",
            "provider": "question",
            "name": "Question",
            "config": {"type": "question", "name": "Question"},
            "ui": {"position": {"x": X_QUESTION, "y": Y_TOP}, "nodeType": "default",
                   "formDataValid": True},
            "input": [{"lane": "text", "from": "webhook_1"}],
        },
    ]

    fan_in: list[dict[str, str]] = []
    for i, persona in enumerate(personas):
        y = Y_TOP + i * Y_STEP
        agent_id = f"agent_{persona['id']}"
        name = persona.get("name") or persona["id"]
        components.append(agent_node(agent_id, name, build_system_prompt(frontdesk, faq, persona), y))
        components.append(llm_node(f"llm_anthropic_{persona['id']}", agent_id, y))
        # FAN-IN: one input entry per branch, all on the `answers` lane.
        fan_in.append({"lane": "answers", "from": agent_id})

    components.append({
        "id": "response_answers_1",
        "provider": "response_answers",
        "name": "Return Answers",
        "config": {"laneName": "answers", "name": "Return Answers"},
        "ui": {"position": {"x": X_RESPONSE, "y": Y_TOP}, "nodeType": "default",
               "formDataValid": True},
        "input": fan_in,
    })

    return {
        "components": components,
        "project_id": PROJECT_ID,
        "version": 1,
        "isLocked": False,
        "snapToGrid": True,
        "snapGridSize": [10, 10],
        "docRevision": 1,
    }


def write_pipe(pipeline: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pipeline, indent=2) + "\n", encoding="utf-8")
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=2, help="number of persona branches (default 2)")
    ap.add_argument("--serial", action="store_true",
                    help="emit N single-branch pipes instead of one N-branch pipe")
    ap.add_argument("--out", type=Path, default=PIPELINES_DIR / "loadtest.pipe")
    ap.add_argument("--outdir", type=Path, default=PIPELINES_DIR / "serial")
    args = ap.parse_args()

    personas, faq, frontdesk = load_inputs()
    if args.n > len(personas):
        raise SystemExit(f"[fatal] --n {args.n} exceeds {len(personas)} personas in personas.json")
    selected = personas[: args.n]

    if args.serial:
        written = []
        for persona in selected:
            path = args.outdir / f"loadtest-{persona['id']}.pipe"
            written.append(write_pipe(build_pipeline([persona], faq, frontdesk), path))
        print(f"wrote {len(written)} single-branch pipes to {args.outdir}/")
        for p in written:
            print(f"  {p.relative_to(ROOT)}")
    else:
        path = write_pipe(build_pipeline(selected, faq, frontdesk), args.out)
        ids = ", ".join(p["id"] for p in selected)
        print(f"wrote {path.relative_to(ROOT)}: {args.n} parallel branches ({ids})")
        print(f"  components: {len(build_pipeline(selected, faq, frontdesk)['components'])}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
