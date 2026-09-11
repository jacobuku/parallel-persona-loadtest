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
    python gen_pipe.py --n 2 --serial             # N single-branch pipes (v1)
    python gen_pipe.py --n 8 --serial --prompt-version v2
"""

from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
PERSONAS_PATH = ROOT / "personas.json"
FAQ_PATH = ROOT / "faq.md"
PIPELINES_DIR = ROOT / "pipelines"

# The front-desk prompt is versioned: prompts/frontdesk_<version>.md. Generated
# pipes are kept apart by version too, so a v1 and a v2 run never read each
# other's .pipe files.
DEFAULT_PROMPT_VERSION = "v1"


def frontdesk_path(version: str) -> Path:
    return ROOT / "prompts" / f"frontdesk_{version}.md"


def serial_dir(version: str) -> Path:
    return PIPELINES_DIR / "serial" / version

# Anthropic profile name as it appears in the workshop pipes.
LLM_PROFILE = "claude-sonnet-4-6"

# Namespace for per-pipe project ids. Each generated pipe gets its OWN
# project_id derived from the branches it contains: the engine keys a running
# pipeline by project_id, so N pipes sharing one id cannot run concurrently --
# `use()` rejects the second with "Pipeline is already running."
PROJECT_NS = uuid.UUID("3e8b17a0-6c24-4f5b-9d81-0a7e5c42d9f1")


def project_id_for(personas: list[dict[str, Any]], version: str) -> str:
    """Deterministic, unique per (branch set, prompt version).

    The version is part of the key so the v1 and v2 pipe for the same persona
    get different ids -- the engine keys a running pipeline by project_id, and
    two pipes sharing one id cannot run at the same time.
    """
    return str(uuid.uuid5(PROJECT_NS, version + ":" + ",".join(p["id"] for p in personas)))

# Canvas spacing, purely cosmetic.
X_WEBHOOK, X_QUESTION, X_AGENT, X_LLM, X_RESPONSE = 50, 320, 620, 620, 960
Y_TOP, Y_STEP = 120, 260


def load_inputs(version: str = DEFAULT_PROMPT_VERSION) -> tuple[list[dict[str, Any]], str, str]:
    """Read the three source documents, failing loudly if any is missing."""
    frontdesk = frontdesk_path(version)
    missing = [p for p in (PERSONAS_PATH, FAQ_PATH, frontdesk) if not p.exists()]
    if missing:
        raise SystemExit("[fatal] missing input(s): " + ", ".join(str(p) for p in missing))

    personas = json.loads(PERSONAS_PATH.read_text(encoding="utf-8"))
    if not isinstance(personas, list) or not personas:
        raise SystemExit("[fatal] personas.json must be a non-empty JSON array")
    for i, p in enumerate(personas):
        for field in ("id", "first_message"):
            if not isinstance(p, dict) or not p.get(field):
                raise SystemExit(f"[fatal] personas.json[{i}] is missing '{field}'")

    return personas, FAQ_PATH.read_text(encoding="utf-8"), frontdesk.read_text(encoding="utf-8")


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
        f"Reply to that message as the front desk agent.\n\n"
        f"The incoming message on this turn carries the rows retrieved from the venue "
        f"database for this enquiry. Treat those rows as the current truth and answer "
        f"from them together with the FAQ above. A block that says it has no rows means "
        f"nothing is on file for that question -- say so and offer to have it confirmed; "
        f"do not fill the gap with a number or a policy of your own.\n\n"
        f"You have no tools of your own. Reply with plain text only -- no JSON, no preamble."
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


def build_pipeline(personas: list[dict[str, Any]], faq: str, frontdesk: str,
                   version: str = DEFAULT_PROMPT_VERSION) -> dict[str, Any]:
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
        name = persona.get("name") or persona.get("type") or persona["id"]
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
        "project_id": project_id_for(personas, version),
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
    ap.add_argument("--prompt-version", default=DEFAULT_PROMPT_VERSION,
                    help="reads prompts/frontdesk_<version>.md, writes pipelines/serial/<version>/")
    ap.add_argument("--serial", action="store_true",
                    help="emit N single-branch pipes instead of one N-branch pipe")
    ap.add_argument("--out", type=Path, default=PIPELINES_DIR / "loadtest.pipe")
    ap.add_argument("--outdir", type=Path, default=None)
    args = ap.parse_args()

    version = args.prompt_version
    outdir = args.outdir or serial_dir(version)
    personas, faq, frontdesk = load_inputs(version)
    if args.n > len(personas):
        raise SystemExit(f"[fatal] --n {args.n} exceeds {len(personas)} personas in personas.json")
    selected = personas[: args.n]

    if args.serial:
        written = []
        for persona in selected:
            path = outdir / f"loadtest-{persona['id']}.pipe"
            written.append(write_pipe(build_pipeline([persona], faq, frontdesk, version), path))
        print(f"wrote {len(written)} single-branch pipes ({version}) to {outdir}/")
        for p in written:
            print(f"  {p.relative_to(ROOT)}")
    else:
        pipeline = build_pipeline(selected, faq, frontdesk, version)
        path = write_pipe(pipeline, args.out)
        ids = ", ".join(p["id"] for p in selected)
        print(f"wrote {path.relative_to(ROOT)}: {args.n} parallel branches ({ids}), {version}")
        print(f"  components: {len(pipeline['components'])}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
