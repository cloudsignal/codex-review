"""Model, effort, and verdict logic for codex-review actions.

Pure functions and data: no codex execution and no process state. run_review.py imports this
module and this module never imports run_review.py. The runner also executes as a script, so
importing it as a module would load a second copy and split the child-process registry its
stop-signal cleanup relies on.
"""
import json
import re

TIERS = ("light", "standard", "deep")

# Used only when `codex debug models` cannot be read; a live catalog validates per model.
STATIC_EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")

REVIEW_MODEL = "gpt-6-sol"
REVIEW_EFFORT = "xhigh"
ROUTER_MODEL = "gpt-6-luna"
ROUTER_EFFORT = "low"

# profile -> tier -> (model, effort). Reviews and the judge never use gpt-6-luna: a weak
# reviewer or judge produces confident noise, which costs more than it saves.
LADDERS = {
    "review": {"light": ("gpt-6-sol", "medium"), "standard": ("gpt-6-sol", "high"),
               "deep": ("gpt-6-sol", "xhigh")},
    "research": {"light": ("gpt-6-luna", "medium"), "standard": ("gpt-6-sol", "medium"),
                 "deep": ("gpt-6-astra", "high")},
    "eval-advise": {"light": ("gpt-6-luna", "high"), "standard": ("gpt-6-sol", "high"),
                    "deep": ("gpt-6-astra", "high")},
    "eval-generate": {"light": ("gpt-6-luna", "medium"), "standard": ("gpt-6-sol", "medium"),
                      "deep": ("gpt-6-sol", "high")},
    "eval-judge": {"light": ("gpt-6-sol", "high"), "standard": ("gpt-6-sol", "high"),
                   "deep": ("gpt-6-sol", "xhigh")},
}
# The lowest tier a profile reaches through a step-down or a catalog fallback.
TIER_FLOOR = {"eval-judge": "standard"}

HEADROOM_WARN_PCT = 80.0  # the line run_review.USAGE_WARN_PCT draws for `limits`


class SelectionError(Exception):
    """A user-facing reason a selection or a model's output was refused."""


class Catalog:
    """The models codex accepts, from `codex debug models`. `models` is None when the catalog
    could not be read: then any model passes and efforts are checked against STATIC_EFFORTS."""

    def __init__(self, models):
        self.models = models

    @property
    def live(self):
        return self.models is not None

    def supports(self, model, effort):
        if not self.live:
            return effort in STATIC_EFFORTS
        # A model that lists no efforts supports none: treating it as "any effort" would
        # start a call the CLI rejects.
        return effort in self.models.get(model, ())


def parse_catalog(text):
    """{slug: (effort, ...)} from `codex debug models` JSON, or None when unusable."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    models = data.get("models") if isinstance(data, dict) else data
    if not isinstance(models, list):
        return None
    out = {}
    for entry in models:
        if not isinstance(entry, dict):
            continue
        slug = entry.get("slug")
        if not isinstance(slug, str) or not slug:
            continue
        efforts = []
        levels = entry.get("supported_reasoning_levels")
        if isinstance(levels, list):
            for level in levels:
                effort = level.get("effort") if isinstance(level, dict) else None
                if isinstance(effort, str):
                    efforts.append(effort)
        out[slug] = tuple(efforts)
    return out or None


def validate(model, effort, catalog):
    """Raise SelectionError unless codex accepts this model at this effort."""
    if not catalog.live:
        if effort not in STATIC_EFFORTS:
            raise SelectionError("invalid effort %r; choose one of: %s"
                                 % (effort, ", ".join(STATIC_EFFORTS)))
        return
    if model not in catalog.models:
        raise SelectionError("model %r is not in the codex catalog; available: %s"
                             % (model, ", ".join(sorted(catalog.models))))
    if not catalog.supports(model, effort):
        raise SelectionError("invalid effort %r for %s; it supports: %s"
                             % (effort, model, ", ".join(catalog.models[model]) or "none listed"))


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def effective_headroom(rate_limits, now):
    """The fullest rate-limit window that has not reset yet and is at or above the warn line,
    as {"window", "used_percent", "resets_at"}, or None.

    A window whose resets_at is in the past no longer applies, so a snapshot taken before a
    reset never keeps stepping selection down. A window without a usable resets_at counts,
    since it cannot be shown to have reset."""
    if not isinstance(rate_limits, dict):
        return None
    worst = None
    for key in ("primary", "secondary"):
        window = rate_limits.get(key)
        if not isinstance(window, dict) or not _number(window.get("used_percent")):
            continue
        pct = float(window["used_percent"])
        resets = window.get("resets_at")
        if _number(resets) and resets <= now:
            continue
        if pct >= HEADROOM_WARN_PCT and (worst is None or pct > worst["used_percent"]):
            worst = {"window": key, "used_percent": pct, "resets_at": resets}
    return worst


def describe_headroom(headroom, now):
    resets = headroom.get("resets_at")
    when = "at an unknown time"
    if _number(resets):
        hours, minutes = divmod(max(int((resets - now) // 60), 0), 60)
        when = "in %dh %02dm" % (hours, minutes)
    return "%s window %.0f%% used, resets %s" % (
        headroom["window"], headroom["used_percent"], when)


def _floor_index(profile):
    return TIERS.index(TIER_FLOOR.get(profile, "light"))


def step_down(profile, tier):
    """One tier lower, never below the profile's floor."""
    return TIERS[max(TIERS.index(tier) - 1, _floor_index(profile))]


def lowest_tier(profile):
    """The lowest tier a profile runs at: where a router answer can land."""
    return TIER_FLOOR.get(profile, "light")


def pick(profile, tier, catalog, ceiling=None):
    """(model, effort, tier) of the supported rung nearest to `tier`, walking down to the
    profile's floor. Only when nothing there is available does it walk up, and never past
    `ceiling` (default: `tier` itself, so no upward walk): a headroom step-down that lands on
    a missing rung may return to the tier the run asked for, never above it, so the budget
    rule can only save. Callers check before any paid router call that a rung exists at or
    below every tier the router could return (lowest_tier), so a routed run never fails
    here."""
    ladder = LADDERS[profile]
    floor = _floor_index(profile)
    start = max(TIERS.index(tier), floor)
    top = max(TIERS.index(ceiling), start) if ceiling else start
    for index in [*range(start, floor - 1, -1), *range(start + 1, top + 1)]:
        model, effort = ladder[TIERS[index]]
        if catalog.supports(model, effort):
            return model, effort, TIERS[index]
    raise SelectionError("no %s model at or below tier %s is in the codex catalog"
                         % (profile, TIERS[top]))


def auto_pick(profile, tier, headroom, catalog, now):
    """Automatic selection: (model, effort, tier used, notes). Each note explains one
    adjustment (a headroom step-down, a catalog fallback) for the run's printed source."""
    notes = []
    requested = TIERS[max(TIERS.index(tier), _floor_index(profile))]
    ceiling = requested
    if headroom is not None:
        lowered = step_down(profile, requested)
        if lowered != requested:
            notes.append("stepped down from %s (%s)"
                         % (requested, describe_headroom(headroom, now)))
            requested = lowered
    model, effort, used = pick(profile, requested, catalog, ceiling=ceiling)
    if used != requested:
        wanted_model, wanted_effort = LADDERS[profile][requested]
        notes.append("%s / %s is not in the codex catalog; used the %s rung"
                     % (wanted_model, wanted_effort, used))
    return model, effort, used, notes


def fill_explicit(profile, model, effort):
    """--model or --effort turns automatic selection off for the run; the missing half comes
    from the profile's standard rung."""
    standard_model, standard_effort = LADDERS[profile]["standard"]
    return model or standard_model, effort or standard_effort


def sum_usage(*usages):
    """Field-wise sum of token-usage dicts; None when none of them carried usage."""
    present = [usage for usage in usages if usage]
    if not present:
        return None
    total = {}
    for usage in present:
        for key, value in usage.items():
            total[key] = total.get(key, 0) + value
    return total


REASON_MAX = 200
TEXT_MAX = 2000
NAME_MAX = 120
WINNERS = ("A", "B", "tie")
CONFIDENCES = ("low", "medium", "high")
# Control characters except tab and newline: model text lands in terminals and files.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean_text(value, limit):
    if not isinstance(value, str):
        return ""
    return _CONTROL.sub(" ", value).strip()[:limit]


def _load_object(text, what):
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        raise SelectionError(f"{what} is not JSON") from None
    if not isinstance(data, dict):
        raise SelectionError(f"{what} is not a JSON object")
    return data


def _exact_keys(obj, keys, what):
    if not isinstance(obj, dict) or set(obj) != set(keys):
        raise SelectionError(f"{what} does not have exactly the keys {', '.join(keys)}")
    return obj


def _string(value, what):
    if not isinstance(value, str):
        raise SelectionError(f"{what} is not a string")
    return value


def parse_router_output(text):
    """(tier, reason) from the router's final message. The output schema is codex's promise,
    not ours, so every key and type is checked again here; anything else raises."""
    data = _exact_keys(_load_object(text, "router output"), ("tier", "reason"), "router output")
    tier = data["tier"]
    if not isinstance(tier, str) or tier not in TIERS:
        raise SelectionError(f"router tier {tier!r} is not one of {'/'.join(TIERS)}")
    reason = _string(data["reason"], "router reason")
    return tier, " ".join(clean_text(reason, REASON_MAX).split())


def parse_verdict(text):
    """The judge's verdict with exactly the schema's keys and types at every level, cleaned;
    SelectionError on any other shape."""
    data = _exact_keys(_load_object(text, "judge verdict"),
                       ("criteria", "overall", "missed"), "judge verdict")
    criteria = data["criteria"]
    if not isinstance(criteria, list) or not criteria:
        raise SelectionError("judge verdict has no criteria")
    rows = []
    for item in criteria:
        _exact_keys(item, ("name", "winner", "why"), "a verdict criterion")
        if item["winner"] not in WINNERS:
            raise SelectionError("judge verdict has a criterion without a valid winner")
        rows.append({
            "name": clean_text(_string(item["name"], "criterion name"), NAME_MAX)
            or "(unnamed)",
            "winner": item["winner"],
            "why": clean_text(_string(item["why"], "criterion reason"), TEXT_MAX)})
    overall = _exact_keys(data["overall"], ("winner", "confidence", "why"), "overall verdict")
    if overall["winner"] not in WINNERS or overall["confidence"] not in CONFIDENCES:
        raise SelectionError("judge verdict has no valid overall winner and confidence")
    missed = _exact_keys(data["missed"], ("A", "B"), "missed lists")
    lists = {}
    for label in ("A", "B"):
        if not isinstance(missed[label], list):
            raise SelectionError(f"judge verdict's missed list for {label} is not a list")
        cleaned = (clean_text(_string(v, "a missed item"), TEXT_MAX) for v in missed[label])
        lists[label] = [item for item in cleaned if item]
    return {
        "criteria": rows,
        "overall": {"winner": overall["winner"], "confidence": overall["confidence"],
                    "why": clean_text(_string(overall["why"], "overall reason"), TEXT_MAX)},
        "missed": lists,
    }


def assign_labels(rng):
    """{"A": side, "B": side} with the sides "existing" and "codex" in random order."""
    if rng.random() < 0.5:
        return {"A": "existing", "B": "codex"}
    return {"A": "codex", "B": "existing"}


def label_names(labels, codex_name):
    """{"A": name, "B": name}: "existing" for the existing result, codex_name for codex's."""
    return {label: ("existing" if side == "existing" else codex_name)
            for label, side in labels.items()}


def unblind(verdict, labels, codex_name):
    """The verdict with A and B replaced by real names in every structured field."""
    names = label_names(labels, codex_name)

    def name(winner):
        return "tie" if winner == "tie" else names[winner]

    return {
        "criteria": [dict(row, winner=name(row["winner"])) for row in verdict["criteria"]],
        "overall": dict(verdict["overall"], winner=name(verdict["overall"]["winner"])),
        "missed": {names["A"]: verdict["missed"]["A"], names["B"]: verdict["missed"]["B"]},
    }


def _cell(text):
    return text.replace("|", "\\|").replace("\n", " ")


def render_verdict(unblinded, labels, codex_name):
    """Markdown for an un-blinded verdict. The free-text reasons still say A and B, so the
    label mapping comes first."""
    names = label_names(labels, codex_name)
    lines = [f"Labels: A = {names['A']}, B = {names['B']}. Reasons below still say A and B.",
             "", "| Criterion | Winner | Why |", "|---|---|---|"]
    for row in unblinded["criteria"]:
        lines.append(f"| {_cell(row['name'])} | {_cell(row['winner'])} | {_cell(row['why'])} |")
    overall = unblinded["overall"]
    lines += ["", f"Overall: {overall['winner']} (confidence {overall['confidence']}). "
                  f"{overall['why']}".rstrip()]
    for name, items in unblinded["missed"].items():
        lines += ["", f"Missed by {name}:"]
        lines += [f"- {_cell(item)}" for item in items] or ["- nothing noted"]
    return "\n".join(lines)
