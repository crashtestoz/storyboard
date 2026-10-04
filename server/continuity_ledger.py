"""Continuity ledger: what each shot establishes, carried shot to shot.

A local model is unreliable at holding 36 shots' worth of positions, props
and wardrobe in its head at once, but good at reading one shot and saying
what it states. So the work is split:

1. Extract (model, one call per shot, in parallel): what this shot states —
   location, whether it continues the previous shot, whether the camera
   crosses the line, and each character, prop and set piece's frame side,
   pose, facing, what is held in which hand and what is worn, at the start
   and end of the shot. Unstated values are null, never guessed.
2. Carry (code): walk the shots in order; anything a shot does not restate
   carries over from where the previous shot left it, and the last known
   look of every character, prop and location is remembered, so a table
   seen in shot 12 is still known when it reappears in shot 16.
3. Check: code rules catch the clear-cut breaks (a frame-side flip with no
   camera crossing the line, a pose jump, a prop changing hands, something
   worn appearing or vanishing across a cut); then one model call per cut,
   in parallel, judges the rest and writes a fix for each break.

Extractions are cached per shot in continuity-ledger.json beside the board,
keyed on the text they were read from, so only edited shots are read again.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

LEDGER_FILE = "continuity-ledger.json"
#: Bump when the extraction prompt or schema changes, so cached reads redo.
EXTRACT_VERSION = 2

SIDES = ("left", "right")
RANK = {"left": 0, "centre": 1, "right": 2}
#: What the frame-side rules apply to. A set piece (a table, a pillar, a wind
#: machine) stays put, so it flipping sides is as telling as a person doing
#: it; a portable prop's frame side follows whoever carries it and is noise.
PLACED = {"character", "set"}
GROUNDED = {"seated", "lying", "kneeling"}
UPRIGHT = {"standing", "moving"}

EXTRACT_SYSTEM = """\
You are a script supervisor. You read ONE shot prompt from a film storyboard
and record, as JSON, what it states about continuity. Record only what the
text states or directly implies. Use null for anything it does not state.
Never guess, never fill in from what is usual.

Return ONLY this JSON object:
{
 "location": "short lowercase name of the place, e.g. \\"parking deck\\", \\"lab\\", \\"interrogation room\\", or null",
 "timeOfDay": "night | day | dawn | dusk | null",
 "lighting": "a few words on the light as described, or null",
 "continues": true if this shot follows straight on from the PREVIOUS SHOT in the same place and moment; false if it cuts to another place, a time jump, a flashback or a vision; null if unclear or there is no previous shot,
 "cameraCrossesLine": true only if this shot views the action from the opposite side compared with the previous shot (a reverse angle, or the camera orbits or moves round to the other side), else false,
 "entities": [
  {
   "name": "character: their cast name; prop or set piece: short lowercase generic name with no article or possessive, e.g. \\"headset\\", \\"steel table\\", \\"workbench\\"",
   "kind": "character | prop | set",
   "look": "how it looks as described in this shot, a few words, or null",
   "start": STATE,
   "end": STATE
  }
 ]
}
STATE is the entity at that moment of the shot:
{
 "side": "left | centre | right | null — its frame side as the camera sees it",
 "depth": "foreground | midground | background | null",
 "pose": "standing | seated | lying | kneeling | moving | null (characters)",
 "facing": "left | right | camera | away | null (where a character faces or looks)",
 "holding": {"item name": "left | right | both | unknown"},
 "wearing": {"item name": true if worn, false if stated taken off}
}
Rules:
- "frame left", "at frame right", "on the left of frame" give side; "centre of
  frame" is centre. Directions like "turns left" are not a frame side.
- end is the state after the shot's action. If nothing changes, repeat start.
- Use the same name for the same thing everywhere; items in holding and
  wearing use the same names as entities (a worn headset is "headset").
- Include props and set pieces the shot names, and every character in it.
- A spectral echo, apparition, duplicate, replay or vision of a character is
  a separate entity from the real one: name it "<cast name> (echo)", kind
  character. Never merge it with the real character.
- An item being worn or held goes only in that character's wearing or
  holding, not as its own entity. List it as its own entity only while it is
  set down or stands apart (a headset lying on a table).
- Use the PREVIOUS SHOT only to judge continues and cameraCrossesLine.
- Do not wrap the JSON in markdown fences or add text outside it.
"""

CUT_SYSTEM = """\
You are a continuity supervisor checking ONE cut in a film storyboard: the
cut from SHOT A to SHOT B. Each shot renders separately from its own prompt,
so anything B's prompt gets wrong about where things stand when A ends will
appear on screen as a jump.

You are given the SCENE DESCRIPTION and CAST, which are sent with every
shot; both prompts; the STATE AT THE CUT (where each character,
prop and set piece stands when A ends, carried from earlier shots, with the
shot each value comes from); EARLIER LOOKS of things or places that reappear
in B after a gap; and RULE ISSUES that automatic checks already found.

Check, using only what the words say:
- Screen direction and the 180-degree line: characters keep their frame side
  across a cut in the same scene unless the camera crosses the line or
  someone visibly moves; two characters facing each other keep their sides.
- Eye lines: someone looking frame right is answered by a look frame left.
- Pose and action handoff: B opens in the state A ended in unless time has
  visibly passed.
- Props and which hand holds them; wardrobe, injuries, wet or dry.
- Environment: time of day, light direction and colour, weather, set dressing.
- Something or somewhere that reappears must match its earlier look unless a
  change is shown or motivated.
These are NOT breaks — never report them:
- A cut to another place or time, or time visibly passing within a scene
  (after an explosion, a fight, a vision). Only flag a pose, prop or position
  change when B clearly picks up the very moment A ended.
- Anything SCENE DESCRIPTION or a CAST description already gives. Those go
  with every shot, so a prompt that leaves out a costume or a detail they
  describe still has it; a Clothing / Appearance line lists only what
  differs. Never ask a prompt to restate them.
- Story: a new character, prop or event appearing; an echo, apparition or
  vision behaving differently from the real person (it may foreshadow them);
  a character picking up or putting on something on screen or between shots
  when the story calls for it.
- Something a prompt simply does not mention.
- The camera moving to a new angle that keeps each character on the same
  side of the line: an over-the-shoulder from behind someone at frame left
  shows the person they face at frame right, which is correct.
Report only a contradiction you can quote: B's words say one thing where
A's words, or the state, say another, and nothing in between explains it.

The RULE ISSUES come from an automatic reading of the prompts, which can be
wrong. Judge each against the actual words: real, or a false alarm.

Return ONLY this JSON object:
{"rules":[{
  "rule": the RULE ISSUE number,
  "real": true or false,
  "reason": "one sentence, quoting the words that decide it",
  "shot": "A" or "B" — the shot to change, when real,
  "find": "exact text copied character for character from that shot's prompt, when real",
  "replace": "the corrected text, when real"
 }],
 "issues":[{
  "shot": "A" or "B" — the shot to change,
  "quote": "the exact clashing words",
  "problem": "one sentence: what contradicts what",
  "find": "exact text copied character for character from that shot's prompt",
  "replace": "the corrected text"
 }]}
"issues" holds only breaks that are not RULE ISSUES; leave it empty when
there are none, which is the usual case. Prefer changing B over A. Keep
fixes small: change only the words that cause the break.
"""


# --------------------------------------------------------------------------- #
# Model calls
# --------------------------------------------------------------------------- #


def _json_reply(raw: str) -> dict[str, Any] | None:
    """The first JSON object in a model reply, past any inline reasoning."""
    text = (raw or "").strip()
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1].strip()
    start = text.find("{")
    if start == -1:
        return None
    try:
        doc, _ = json.JSONDecoder().raw_decode(text, start)
    except json.JSONDecodeError:
        return None
    return doc if isinstance(doc, dict) else None


def run_parallel(fn: Callable[[Any, Any], Any], items: list[Any],
                 services: list[Any], per_service: int = 2) -> list[Any]:
    """fn(service, item) for every item, *per_service* at a time on each
    service, results in item order. A failed item's result is the exception,
    so one bad reply never sinks the whole pass."""
    if not items:
        return []
    services = [_quick(s) for s in services]

    def call(pair):
        index, item = pair
        try:
            return fn(services[index % len(services)], item)
        except Exception as exc:  # noqa: BLE001 — reported per item
            return exc

    workers = max(1, min(len(items), len(services) * per_service))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(call, enumerate(items)))


def _quick(service):
    """The service with thinking off. Filling in a form from one shot needs
    no long reasoning: on a local 27B model, medium thinking took ~200s a
    shot against ~10s without, for the same reading. A copy, so the shared
    service the rest of the app uses keeps its own setting."""
    if getattr(service, "thinking", "none") == "none":
        return service
    service = copy.copy(service)
    service.thinking = "none"
    return service


def _complete_json(service, system: str, user: str) -> dict[str, Any]:
    doc = _json_reply(service.complete(system, user, timeout=300.0, max_tokens=4096))
    if doc is None:  # one retry: a local model's malformed reply is rarely repeated
        doc = _json_reply(service.complete(system, user, timeout=300.0, max_tokens=4096))
    if doc is None:
        raise RuntimeError("the model did not return JSON")
    return doc


# --------------------------------------------------------------------------- #
# 1. Extract
# --------------------------------------------------------------------------- #


def chain_sources(board: dict[str, Any]) -> list[int | None]:
    """Per shot, the 1-based number of the earlier shot its Start frame is
    chained from, or None. A chain may skip shots (shot 3 picking up from
    shot 1 with a cutaway between), and then that source, not the shot
    before, is what the shot continues from."""
    shots = board.get("shots") or []
    index = {s.get("id"): i for i, s in enumerate(shots)}
    out: list[int | None] = []
    for i, shot in enumerate(shots):
        ref = shot.get("startRef")
        src = index.get(ref.get("from")) if isinstance(ref, dict) and ref.get("kind") == "chain" else None
        out.append(src + 1 if src is not None and src < i else None)
    return out


def _previous_number(sources: list[int | None] | None, number: int) -> int:
    """The shot that shot *number* (1-based) continues from."""
    src = sources[number - 1] if sources and number <= len(sources) else None
    return src or number - 1


def _extract_input(board: dict[str, Any], index: int) -> str:
    shots = board.get("shots") or []
    shot = shots[index]
    cast = [c.get("name") for c in board.get("characters") or [] if c.get("name")]
    prev = _previous_number(chain_sources(board), index + 1)
    previous = shots[prev - 1].get("prompt") if prev > 0 else None
    return (
        "SCENE DESCRIPTION (applies to every shot):\n"
        + (board.get("sceneDescription") or "(none)")
        + "\n\nCAST NAMES: " + (", ".join(cast) or "(none)")
        + "\n\nPREVIOUS SHOT:\n" + (previous or "(none — this is the first shot)")
        + f"\n\nSHOT {index + 1}:\n" + (shot.get("prompt") or "")
    )


def _hash(text: str) -> str:
    return hashlib.sha1(f"{EXTRACT_VERSION}\n{text}".encode()).hexdigest()


def load_ledger(project_dir: Path) -> dict[str, Any]:
    try:
        doc = json.loads((Path(project_dir) / LEDGER_FILE).read_text())
    except (OSError, ValueError):
        return {"shots": {}}
    return doc if isinstance(doc, dict) and isinstance(doc.get("shots"), dict) else {"shots": {}}


def save_ledger(project_dir: Path, ledger: dict[str, Any]) -> None:
    path = Path(project_dir) / LEDGER_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(ledger, ensure_ascii=False, indent=1))
    tmp.replace(path)


def extract(board: dict[str, Any], services: list[Any], cache: dict[str, Any],
            per_service: int = 2) -> tuple[list[dict[str, Any] | None], int]:
    """Each shot's facts, in order (None where the read failed), and how many
    shots were read fresh. *cache* (a ledger's "shots") is updated in place;
    entries for shots no longer on the board are dropped."""
    shots = board.get("shots") or []
    inputs = [_extract_input(board, i) for i in range(len(shots))]
    facts: list[dict[str, Any] | None] = [None] * len(shots)
    todo = []
    for i, shot in enumerate(shots):
        hit = cache.get(shot.get("id"))
        if isinstance(hit, dict) and hit.get("hash") == _hash(inputs[i]):
            facts[i] = hit.get("facts")
        else:
            todo.append(i)
    results = run_parallel(
        lambda service, i: _clean_facts(_complete_json(service, EXTRACT_SYSTEM, inputs[i])),
        todo, services, per_service)
    for i, result in zip(todo, results):
        if isinstance(result, dict):
            facts[i] = result
            cache[shots[i].get("id")] = {"hash": _hash(inputs[i]), "facts": result}
    live = {s.get("id") for s in shots}
    for sid in [k for k in cache if k not in live]:
        del cache[sid]
    return facts, len(todo)


def _word(value: Any, allowed: set[str] | tuple[str, ...]) -> str | None:
    value = value.strip().lower() if isinstance(value, str) else None
    if value == "center":
        value = "centre"
    return value if value in allowed else None


def _text(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def key(name: Any) -> str:
    """A name as a lookup key: lowercase, no article or possessive."""
    text = re.sub(r"\s+", " ", str(name or "").strip().lower()).strip(" .,:;'\"")
    return re.sub(r"^(?:the|a|an|his|her|their|its|my)\s+", "", text)


def _clean_state(raw: Any) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    holding = raw.get("holding") if isinstance(raw.get("holding"), dict) else {}
    wearing = raw.get("wearing") if isinstance(raw.get("wearing"), dict) else {}
    return {
        "side": _word(raw.get("side"), {"left", "centre", "right"}),
        "depth": _word(raw.get("depth"), {"foreground", "midground", "background"}),
        "pose": _word(raw.get("pose"), GROUNDED | UPRIGHT),
        "facing": _word(raw.get("facing"), {"left", "right", "camera", "away"}),
        "holding": {key(k): (_word(v, {"left", "right", "both"}) or "unknown")
                    for k, v in holding.items() if key(k)},
        "wearing": {key(k): v for k, v in wearing.items()
                    if key(k) and isinstance(v, bool)},
    }


def _clean_facts(raw: dict[str, Any]) -> dict[str, Any]:
    """Allow-list a model's extraction, so the carry step can trust types."""
    entities = []
    for e in raw.get("entities") or []:
        if not isinstance(e, dict) or not key(e.get("name")):
            continue
        entities.append({
            "name": key(e["name"]),
            "label": str(e["name"]).strip(),
            "kind": _word(e.get("kind"), {"character", "prop", "set"}) or "prop",
            "look": _text(e.get("look")),
            "start": _clean_state(e.get("start")),
            "end": _clean_state(e.get("end") or e.get("start")),
        })
    continues = raw.get("continues")
    return {
        "location": key(raw.get("location")) or None,
        "timeOfDay": _word(raw.get("timeOfDay"), {"night", "day", "dawn", "dusk"}),
        "lighting": _text(raw.get("lighting")),
        "continues": continues if isinstance(continues, bool) else None,
        "cameraCrossesLine": raw.get("cameraCrossesLine") is True,
        "entities": entities,
    }


# --------------------------------------------------------------------------- #
# 2. Carry, and the rule checks
# --------------------------------------------------------------------------- #


def _same_scene(facts: dict[str, Any], previous: dict[str, Any] | None) -> bool:
    if previous is None:
        return False
    if facts.get("continues") is not None:
        return facts["continues"]
    return bool(facts.get("location")) and facts.get("location") == previous.get("location")


def _apply(state: dict[str, Any], moment: dict[str, Any], shot: int) -> None:
    """Stated values in *moment* overwrite *state*, each tagged with its shot."""
    for attr in ("side", "depth", "pose", "facing"):
        if moment.get(attr) is not None:
            state[attr] = (moment[attr], shot)
    for attr in ("holding", "wearing"):
        for item, value in (moment.get(attr) or {}).items():
            state.setdefault(attr, {})[item] = (value, shot)


def _rule_issues(name: str, carried: dict[str, Any], start: dict[str, Any],
                 crosses: bool, shot: int, prev: int | None = None) -> list[dict[str, Any]]:
    issues = []
    cut = [shot - 1 if prev is None else prev, shot]

    def issue(problem: str) -> None:
        issues.append({"cut": list(cut), "source": "rule", "problem": problem})

    side = carried.get("side")
    if side and side[0] in SIDES and start["side"] in SIDES \
            and side[0] != start["side"] and not crosses:
        issue(f"{name} is frame {side[0]} at the cut (shot {side[1]}) but opens "
              f"shot {shot} frame {start['side']}, with no camera crossing the line.")
    pose = carried.get("pose")
    if pose and start["pose"] and (
            (pose[0] in GROUNDED and start["pose"] in UPRIGHT)
            or (pose[0] in UPRIGHT and start["pose"] in GROUNDED)):
        issue(f"{name} is {pose[0]} at the cut (shot {pose[1]}) but opens shot "
              f"{shot} {start['pose']}.")
    for item, hand in start["holding"].items():
        held = (carried.get("holding") or {}).get(item)
        if held and held[0] in SIDES and hand in SIDES and held[0] != hand:
            issue(f"{name} holds the {item} in the {held[0]} hand at the cut "
                  f"(shot {held[1]}) but the {hand} hand in shot {shot}.")
    for item, worn in start["wearing"].items():
        was = (carried.get("wearing") or {}).get(item)
        if was and was[0] != worn:
            issue(f"{name} {'has the ' + item + ' off' if not was[0] else 'wears the ' + item} "
                  f"at the cut (shot {was[1]}) but {'wears it' if worn else 'has it off'} "
                  f"at the start of shot {shot}.")
    return issues


def _order_issues(entities: list[dict[str, Any]], state: dict[str, Any],
                  found: list[dict[str, Any]], shot: int,
                  prev: int | None = None) -> list[dict[str, Any]]:
    """The 180-degree rule proper: who is left of whom. A new framing can
    slide everyone along the frame (centre to left is not a break), but two
    characters or set pieces swapping their left-to-right order means the
    camera crossed the line. Pairs already flagged by a side flip are left
    out, so one break is reported once."""
    flagged = {i["problem"].split(" is frame ", 1)[0] for i in found}
    placed = [e for e in entities if e["kind"] in PLACED and e["start"]["side"]
              and (state.get(e["name"]) or {}).get("side") and e["label"] not in flagged]
    issues = []
    for i, a in enumerate(placed):
        for b in placed[i + 1:]:
            was_a, was_b = state[a["name"]]["side"], state[b["name"]]["side"]
            before = RANK[was_a[0]] - RANK[was_b[0]]
            after = RANK[a["start"]["side"]] - RANK[b["start"]["side"]]
            if before * after < 0:
                left, right = (a, b) if before < 0 else (b, a)
                issues.append({"cut": [shot - 1 if prev is None else prev, shot],
                               "source": "rule", "problem": (
                    f"{left['label']} is left of {right['label']} at the cut (shot "
                    f"{max(was_a[1], was_b[1])}) but right of it in shot {shot}, with no "
                    "camera crossing the line.")})
    return issues


def carry(facts: list[dict[str, Any] | None],
          sources: list[int | None] | None = None) -> dict[str, Any]:
    """Walk the shots in order. Returns, per shot (1-based, index 0 = shot
    1): whether it continues the previous shot's scene, the carried state of
    every entity at its start, earlier looks of whatever reappears in it
    after a gap, and the rule issues found at the cut into it.

    *sources* (from chain_sources) lets a shot continue from an earlier shot
    than the one before it: it then starts from the state that source ended
    in, and its cut is checked against that source."""
    shots = []
    issues: list[dict[str, Any]] = []
    state: dict[str, dict[str, Any]] = {}
    looks: dict[str, tuple[str, int]] = {}
    seen: dict[str, int] = {}
    places: dict[str, dict[str, Any]] = {}
    previous = None
    ends: dict[int, tuple[dict[str, Any], dict[str, Any] | None]] = {}
    for number, f in enumerate(facts, 1):
        prev = _previous_number(sources, number)
        if prev != number - 1:
            # Resume the chained source's thread; the shots between are a
            # cutaway whose positions and props do not carry into this one.
            state, previous = copy.deepcopy(ends.get(prev, ({}, None)))
        if f is None:
            shots.append({"number": number, "unread": True})
            state, previous = {}, None
            ends[number] = ({}, None)
            continue
        same = _same_scene(f, previous)
        if not same:
            # Positions, poses and what is in hand belong to a scene; a new
            # place or time starts them over. Looks are remembered for good.
            state = {}
        at_start = {name: json.loads(json.dumps(s)) for name, s in state.items()}
        if same:
            found = []
            for e in f["entities"]:
                if e["kind"] in PLACED and e["name"] in state:
                    found.extend(_rule_issues(e["label"], state[e["name"]], e["start"],
                                              f["cameraCrossesLine"], number, prev))
            issues.extend(found)
            if not f["cameraCrossesLine"]:
                issues.extend(_order_issues(f["entities"], state, found, number, prev))
        earlier = {}
        for e in f["entities"]:
            last = seen.get(e["name"])
            if last is not None and last != prev and e["name"] in looks:
                earlier[e["name"]] = {"look": looks[e["name"]][0], "shot": looks[e["name"]][1]}
        place = places.get(f.get("location") or "")
        if place and place["shot"] != prev:
            earlier["location: " + f["location"]] = place
        shots.append({"number": number, "previous": prev, "sameScene": same,
                      "stateAtStart": at_start, "earlier": earlier})
        for e in f["entities"]:
            s = state.setdefault(e["name"], {})
            _apply(s, e["start"], number)
            _apply(s, e["end"], number)
            if e["look"]:
                looks[e["name"]] = (e["look"], number)
            seen[e["name"]] = number
        if f.get("location"):
            places[f["location"]] = {
                "look": "; ".join(v for v in (f.get("timeOfDay"), f.get("lighting")) if v),
                "shot": number}
        previous = f
        ends[number] = (copy.deepcopy(state), f)
    return {"shots": shots, "issues": issues}


def state_before(facts: list[dict[str, Any] | None], number: int,
                 sources: list[int | None] | None = None) -> dict[str, Any]:
    """The carried state where shot *number* (1-based) starts — what a new or
    rewritten shot at that point has to agree with."""
    return carry(facts[:number], sources)["shots"][number - 1] if number <= len(facts) else {}


# --------------------------------------------------------------------------- #
# 3. Cut checks
# --------------------------------------------------------------------------- #


def _plain(state: dict[str, Any]) -> dict[str, Any]:
    """Carried state as readable text: value (shot N)."""
    out = {}
    for name, attrs in state.items():
        parts = {}
        for attr, value in attrs.items():
            if attr in ("holding", "wearing"):
                parts[attr] = {item: f"{v} (shot {s})" for item, (v, s) in value.items()}
            else:
                parts[attr] = f"{value[0]} (shot {value[1]})"
        out[name] = parts
    return out


def _board_context(board: dict[str, Any]) -> str:
    cast = "\n".join(f"- {c.get('name')}: {c.get('description') or ''}".rstrip(": ")
                     for c in board.get("characters") or [] if c.get("name"))
    return ("SCENE DESCRIPTION:\n" + (board.get("sceneDescription") or "(none)")
            + "\n\nCAST:\n" + (cast or "(none)"))


def _cut_input(context: str, shots: list[dict[str, Any]], info: dict[str, Any],
               rules: list[dict[str, Any]]) -> str:
    b = info["number"]
    a = info.get("previous") or b - 1
    return (
        context
        + f"\n\nSHOT A (shot {a}):\n" + (shots[a - 1].get("prompt") or "")
        + f"\n\nSHOT B (shot {b}):\n" + (shots[b - 1].get("prompt") or "")
        + "\n\nSAME SCENE: " + ("yes, B follows straight on from A" if info["sameScene"]
                               else "no, B cuts to another place or time")
        + "\n\nSTATE AT THE CUT:\n" + json.dumps(_plain(info["stateAtStart"]), ensure_ascii=False)
        + "\n\nEARLIER LOOKS:\n" + (json.dumps(info["earlier"], ensure_ascii=False)
                                   if info["earlier"] else "(none)")
        + "\n\nRULE ISSUES:\n" + ("\n".join(f"{i}. {r['problem']}" for i, r in enumerate(rules, 1))
                                  or "(none)")
    )


def check_cuts(board: dict[str, Any], carried: dict[str, Any], services: list[Any],
               per_service: int = 2) -> list[dict[str, Any]]:
    """One model review per cut, in parallel. Returns every issue with a
    checked fix where the model gave one; rule issues the model dismissed
    are kept, marked dismissed, so a false alarm is visible as one."""
    shots = board.get("shots") or []
    rules_at: dict[int, list[dict[str, Any]]] = {}
    for r in carried["issues"]:
        rules_at.setdefault(r["cut"][1], []).append(r)
    cuts = [info for info in carried["shots"][1:]
            if not info.get("unread")
            and not carried["shots"][(info.get("previous") or info["number"] - 1) - 1].get("unread")]
    context = _board_context(board)
    results = run_parallel(
        lambda service, info: _complete_json(
            service, CUT_SYSTEM,
            _cut_input(context, shots, info, rules_at.get(info["number"], []))),
        cuts, services, per_service)
    out: list[dict[str, Any]] = []
    for info, result in zip(cuts, results):
        b = info["number"]
        a = info.get("previous") or b - 1
        rules = [dict(r) for r in rules_at.get(b, [])]
        if not isinstance(result, dict):
            out.extend(rules)
            out.append({"cut": [a, b], "source": "error",
                        "problem": f"Could not check this cut: {result}"})
            continue
        for raw in result.get("rules") or []:
            rule = raw.get("rule") if isinstance(raw, dict) else None
            if not (isinstance(rule, int) and 1 <= rule <= len(rules)):
                continue
            target = rules[rule - 1]
            reason = _text(raw.get("reason"))
            # Only an explicit false counts as a false alarm: a rule the
            # model answered vaguely stays reported.
            if raw.get("real") is False:
                target["dismissed"] = reason or "judged a false alarm"
                continue
            if reason:
                target["reason"] = reason
            fix = _fix(shots, a, b, raw)
            if fix:
                target["fix"] = fix
        for raw in result.get("issues") or []:
            if not isinstance(raw, dict):
                continue
            fix = _fix(shots, a, b, raw)
            if _text(raw.get("problem")):
                out.append({"cut": [a, b], "source": "review",
                            "problem": _text(raw["problem"]),
                            **({"quote": _text(raw.get("quote"))} if _text(raw.get("quote")) else {}),
                            **({"fix": fix} if fix else {})})
        out.extend(rules)
    return out


def _fix(shots: list[dict[str, Any]], a: int, b: int,
         raw: dict[str, Any]) -> dict[str, Any] | None:
    """A model's fix as a replace_text proposal, only if its find text really
    is in that shot's prompt — otherwise it would replace nothing."""
    number = a if str(raw.get("shot")).strip().upper() == "A" else b
    shot = shots[number - 1]
    find, replace = raw.get("find"), raw.get("replace")
    if not (isinstance(find, str) and find and isinstance(replace, str) and find != replace
            and find in (shot.get("prompt") or "")):
        return None
    return {"tool": "replace_text", "find": find, "replace": replace,
            "scope": ["shots"], "shotIds": [shot.get("id")], "shotNumber": number}


# --------------------------------------------------------------------------- #
# The whole pass
# --------------------------------------------------------------------------- #


def check_board(board: dict[str, Any], project_dir: Path, services: list[Any],
                per_service: int = 2) -> dict[str, Any]:
    """Extract (cached), carry, and check every cut. Saves the ledger."""
    ledger = load_ledger(project_dir)
    facts, fresh = extract(board, services, ledger["shots"], per_service)
    save_ledger(project_dir, ledger)
    carried = carry(facts, chain_sources(board))
    issues = check_cuts(board, carried, services, per_service)
    issues.sort(key=lambda i: (i["cut"][1], i.get("source") != "rule"))
    return {
        "shots": len(facts), "readFresh": fresh,
        "unread": [s["number"] for s in carried["shots"] if s.get("unread")],
        "issues": issues,
    }


def _main() -> None:
    import argparse
    import time

    from .llm import load_services

    parser = argparse.ArgumentParser(description="Check a storyboard's continuity.")
    parser.add_argument("project", type=Path, help="board folder holding storyboard.json")
    parser.add_argument("--service", action="append", required=True,
                        help="llm-services.json id; repeat to spread across servers")
    parser.add_argument("--per-service", type=int, default=2)
    args = parser.parse_args()
    available = load_services(Path(__file__).resolve().parents[1])
    services = [available[s] for s in args.service]
    board = json.loads((args.project / "storyboard.json").read_text())
    began = time.monotonic()
    report = check_board(board, args.project, services, args.per_service)
    report["seconds"] = round(time.monotonic() - began)
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    _main()
