"""Unit tests for selection.py: pure logic, no codex, no subprocess."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import selection  # noqa: E402
from selection import Catalog, SelectionError  # noqa: E402

LEVELS = ("low", "medium", "high", "xhigh", "max", "ultra")


def catalog_json(astra=True):
    models = [
        {"slug": "gpt-6-sol", "supported_reasoning_levels": [{"effort": e} for e in LEVELS]},
        {"slug": "gpt-6-luna",
         "supported_reasoning_levels": [{"effort": e} for e in LEVELS[:-1]]},
    ]
    if astra:
        models.insert(0, {"slug": "gpt-6-astra",
                          "supported_reasoning_levels": [{"effort": e} for e in LEVELS]})
    return json.dumps({"models": models})


LIVE = Catalog(selection.parse_catalog(catalog_json()))
NO_ASTRA = Catalog(selection.parse_catalog(catalog_json(astra=False)))
UNAVAILABLE = Catalog(None)
NOW = 1_800_000_000.0
NEAR = {"window": "primary", "used_percent": 86.0, "resets_at": NOW + 3600}


def limits(pct, resets_at, key="primary"):
    return {key: {"used_percent": pct, "window_minutes": 300, "resets_at": resets_at}}


class CatalogTest(unittest.TestCase):
    def test_parse_reads_slugs_and_per_model_efforts(self):
        models = selection.parse_catalog(catalog_json())
        self.assertEqual(models["gpt-6-luna"], ("low", "medium", "high", "xhigh", "max"))
        self.assertIn("ultra", models["gpt-6-sol"])

    def test_parse_rejects_unusable_output(self):
        for text in ("not json", json.dumps({"models": []}), json.dumps({"nope": 1}), ""):
            with self.subTest(text=text):
                self.assertIsNone(selection.parse_catalog(text))

    def test_effort_is_validated_per_model(self):
        # ultra is valid for sol and not for luna: one global effort list would accept both.
        selection.validate("gpt-6-sol", "ultra", LIVE)
        with self.assertRaises(SelectionError) as ctx:
            selection.validate("gpt-6-luna", "ultra", LIVE)
        self.assertIn("invalid effort 'ultra' for gpt-6-luna", str(ctx.exception))

    def test_unknown_model_lists_what_is_available(self):
        with self.assertRaises(SelectionError) as ctx:
            selection.validate("gpt-7-nope", "high", LIVE)
        self.assertIn("not in the codex catalog", str(ctx.exception))
        self.assertIn("gpt-6-sol", str(ctx.exception))

    def test_model_without_listed_efforts_supports_none(self):
        # Treating "no levels listed" as "any effort" would start a call the CLI rejects.
        catalog = Catalog(selection.parse_catalog(json.dumps({"models": [
            {"slug": "gpt-6-sol"}, {"slug": "odd", "supported_reasoning_levels": "bad"}]})))
        self.assertFalse(catalog.supports("gpt-6-sol", "xhigh"))
        with self.assertRaises(SelectionError):
            selection.validate("odd", "high", catalog)

    def test_unavailable_catalog_accepts_any_model_and_checks_static_efforts(self):
        selection.validate("any-model", "ultra", UNAVAILABLE)
        with self.assertRaises(SelectionError) as ctx:
            selection.validate("any-model", "bogus", UNAVAILABLE)
        self.assertIn("invalid effort", str(ctx.exception))


class HeadroomTest(unittest.TestCase):
    def test_live_window_over_the_line_counts(self):
        h = selection.effective_headroom(limits(86.0, NOW + 3600), NOW)
        self.assertEqual((h["window"], h["used_percent"]), ("primary", 86.0))

    def test_line_is_inclusive(self):
        self.assertIsNotNone(selection.effective_headroom(limits(80.0, NOW + 60), NOW))
        self.assertIsNone(selection.effective_headroom(limits(79.9, NOW + 60), NOW))

    def test_window_that_already_reset_is_ignored(self):
        # 95% an hour before a reset that has since happened: the window is fresh now.
        self.assertIsNone(selection.effective_headroom(limits(95.0, NOW - 3600), NOW))

    def test_window_without_a_reset_time_still_counts(self):
        self.assertIsNotNone(selection.effective_headroom(limits(90.0, None), NOW))

    def test_window_resetting_exactly_now_is_ignored(self):
        self.assertIsNone(selection.effective_headroom(limits(95.0, NOW), NOW))

    def test_fullest_live_window_wins_in_either_order(self):
        # Each order defeats one shortcut: "take the last window" and "take the first".
        later = {"primary": {"used_percent": 85.0, "resets_at": NOW + 60},
                 "secondary": {"used_percent": 97.0, "resets_at": NOW + 86400}}
        earlier = {"primary": {"used_percent": 97.0, "resets_at": NOW + 60},
                   "secondary": {"used_percent": 85.0, "resets_at": NOW + 86400}}
        self.assertEqual(selection.effective_headroom(later, NOW)["window"], "secondary")
        self.assertEqual(selection.effective_headroom(earlier, NOW)["window"], "primary")

    def test_no_snapshot_is_none(self):
        self.assertIsNone(selection.effective_headroom(None, NOW))

    def test_description_names_window_and_reset(self):
        text = selection.describe_headroom(NEAR, NOW)
        self.assertEqual(text, "primary window 86% used, resets in 1h 00m")


class AutoPickTest(unittest.TestCase):
    def test_ladder_by_tier(self):
        self.assertEqual(selection.auto_pick("research", "light", None, LIVE, NOW)[:3],
                         ("gpt-6-luna", "medium", "light"))
        self.assertEqual(selection.auto_pick("research", "deep", None, LIVE, NOW)[:3],
                         ("gpt-6-astra", "high", "deep"))

    def test_headroom_steps_down_exactly_one_tier(self):
        model, effort, used, notes = selection.auto_pick("research", "deep", NEAR, LIVE, NOW)
        # One tier (standard, sol), not the bottom (light, luna).
        self.assertEqual((model, effort, used), ("gpt-6-sol", "medium", "standard"))
        self.assertIn("stepped down from deep", notes[0])

    def test_headroom_steps_standard_down_to_light(self):
        self.assertEqual(selection.auto_pick("research", "standard", NEAR, LIVE, NOW)[:3],
                         ("gpt-6-luna", "medium", "light"))

    def test_judge_never_drops_below_standard(self):
        self.assertEqual(selection.auto_pick("eval-judge", "standard", NEAR, LIVE, NOW)[:3],
                         ("gpt-6-sol", "high", "standard"))
        self.assertEqual(selection.auto_pick("eval-judge", "light", None, LIVE, NOW)[2],
                         "standard")

    def test_catalog_fallback_walks_down_one_rung(self):
        model, effort, used, notes = selection.auto_pick("research", "deep", None, NO_ASTRA, NOW)
        self.assertEqual((model, effort, used), ("gpt-6-sol", "medium", "standard"))
        self.assertTrue(any("gpt-6-astra" in note for note in notes), notes)

    def test_catalog_fallback_walks_past_several_missing_rungs(self):
        # Only luna: deep (astra) and standard (sol) are both missing. "Use sol whenever astra
        # is absent" fails here; a real walk lands on the light rung.
        luna_only = Catalog({"gpt-6-luna": ("low", "medium", "high", "xhigh", "max")})
        model, effort, used, notes = selection.auto_pick("research", "deep", None, luna_only, NOW)
        self.assertEqual((model, effort, used), ("gpt-6-luna", "medium", "light"))
        self.assertIn("gpt-6-astra / high is not in the codex catalog", notes[0])

    def test_catalog_fallback_never_climbs_past_the_requested_tier(self):
        # luna lacks medium, so research's light rung is missing and there is nothing below it.
        partial = Catalog({"gpt-6-sol": ("medium",), "gpt-6-luna": ("low",)})
        with self.assertRaises(SelectionError):
            selection.auto_pick("research", "light", None, partial, NOW)
        # A step-down that lands on the missing rung returns to the tier asked for, never above:
        # the budget rule may save, and must never spend more than the run would have.
        model, effort, used, notes = selection.auto_pick("research", "standard", NEAR, partial,
                                                         NOW)
        self.assertEqual((model, effort, used), ("gpt-6-sol", "medium", "standard"))
        self.assertIn("gpt-6-luna / medium is not in the codex catalog; used the standard rung",
                      notes[1])
        # With only astra, an upward walk would land on the costliest rung; it must refuse.
        astra_only = Catalog({"gpt-6-astra": ("high",)})
        with self.assertRaises(SelectionError):
            selection.auto_pick("research", "standard", NEAR, astra_only, NOW)
        # Down still works: deep with sol/medium available picks standard.
        self.assertEqual(selection.pick("research", "deep", partial)[2], "standard")

    def test_no_rung_available_raises(self):
        with self.assertRaises(SelectionError):
            selection.auto_pick("research", "deep", None, Catalog({"other": ("high",)}), NOW)

    def test_fill_explicit_takes_the_missing_half_from_standard(self):
        self.assertEqual(selection.fill_explicit("research", "gpt-6-astra", None),
                         ("gpt-6-astra", "medium"))
        self.assertEqual(selection.fill_explicit("eval-judge", None, "xhigh"),
                         ("gpt-6-sol", "xhigh"))

    def test_sum_usage(self):
        self.assertIsNone(selection.sum_usage(None, None))
        self.assertEqual(
            selection.sum_usage({"input_tokens": 100, "output_tokens": 10}, None,
                                {"input_tokens": 5}),
            {"input_tokens": 105, "output_tokens": 10})


VERDICT = {
    "criteria": [{"name": "Correctness", "winner": "A", "why": "A is right"},
                 {"name": "Concision", "winner": "B", "why": "B is shorter"}],
    "overall": {"winner": "A", "confidence": "medium", "why": "A answers it"},
    "missed": {"A": ["an edge case"], "B": ["the main point"]},
}
CODEX = "codex (gpt-6-sol/medium)"


class FixedRng:
    def __init__(self, value):
        self.value = value

    def random(self):
        return self.value


class RouterOutputTest(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(
            selection.parse_router_output('{"tier": "deep", "reason": "many constraints"}'),
            ("deep", "many constraints"))

    def test_reason_is_cleaned_and_capped(self):
        text = json.dumps({"tier": "light", "reason": "a\x1b[31mb\nc" + "x" * 500})
        tier, reason = selection.parse_router_output(text)
        self.assertEqual(tier, "light")
        self.assertNotIn("\x1b", reason)
        self.assertNotIn("\n", reason)
        self.assertLessEqual(len(reason), selection.REASON_MAX)

    def test_rejects_every_other_shape(self):
        # Missing, extra, and wrong-type fields: a parser that trusts codex's schema accepts
        # the last three, since each still carries a valid tier.
        for bad in ("not json", "[]", '{"tier": "huge", "reason": "x"}',
                    '{"reason": "no tier"}', '{"tier": ["deep"], "reason": "x"}',
                    '{"tier": "deep"}', '{"tier": "deep", "reason": 7}',
                    '{"tier": "deep", "reason": "x", "extra": 1}'):
            with self.subTest(bad=bad), self.assertRaises(SelectionError):
                selection.parse_router_output(bad)


class VerdictTest(unittest.TestCase):
    def test_assign_labels_follows_the_rng_both_ways(self):
        # A fixed assignment passes either half alone and cannot pass both.
        self.assertEqual(selection.assign_labels(FixedRng(0.1)),
                         {"A": "existing", "B": "codex"})
        self.assertEqual(selection.assign_labels(FixedRng(0.9)),
                         {"A": "codex", "B": "existing"})

    def test_unblind_maps_winners_for_both_orders(self):
        verdict = selection.parse_verdict(json.dumps(VERDICT))
        existing_first = selection.unblind(verdict, {"A": "existing", "B": "codex"}, CODEX)
        codex_first = selection.unblind(verdict, {"A": "codex", "B": "existing"}, CODEX)
        self.assertEqual(existing_first["overall"]["winner"], "existing")
        self.assertEqual(codex_first["overall"]["winner"], CODEX)
        self.assertEqual([row["winner"] for row in codex_first["criteria"]],
                         [CODEX, "existing"])
        self.assertEqual(codex_first["missed"]["existing"], ["the main point"])
        self.assertEqual(codex_first["missed"][CODEX], ["an edge case"])

    def test_tie_stays_a_tie(self):
        tied = dict(VERDICT, overall={"winner": "tie", "confidence": "low", "why": "even"})
        out = selection.unblind(selection.parse_verdict(json.dumps(tied)),
                                {"A": "codex", "B": "existing"}, CODEX)
        self.assertEqual(out["overall"]["winner"], "tie")

    def test_render_states_the_mapping_first_and_escapes_cells(self):
        piped = dict(VERDICT, criteria=[{"name": "Tone", "winner": "A", "why": "a | b\nc"}])
        labels = {"A": "codex", "B": "existing"}
        text = selection.render_verdict(
            selection.unblind(selection.parse_verdict(json.dumps(piped)), labels, CODEX),
            labels, CODEX)
        self.assertTrue(text.startswith(f"Labels: A = {CODEX}, B = existing."))
        self.assertIn(f"| Tone | {CODEX} | a \\| b c |", text)
        self.assertIn(f"Overall: {CODEX} (confidence medium). A answers it", text)
        self.assertIn("Missed by existing:\n- the main point", text)

    def test_parse_verdict_rejects_bad_shapes(self):
        bad = [
            dict(VERDICT, criteria=[]),
            dict(VERDICT, criteria=[{"name": "x", "winner": "C", "why": ""}]),
            {k: v for k, v in VERDICT.items() if k != "overall"},
            dict(VERDICT, overall={"winner": "A", "confidence": "sure", "why": ""}),
            dict(VERDICT, missed={"A": []}),
            # Missing, extra, and wrong-type fields that a defaulting parser would paper over.
            dict(VERDICT, criteria=[{"winner": "A", "why": "no name"}]),
            dict(VERDICT, criteria=[{"name": "x", "winner": "A", "why": 3}]),
            dict(VERDICT, overall={"winner": "A", "confidence": "low"}),
            dict(VERDICT, missed={"A": [1], "B": []}),
            dict(VERDICT, extra="field"),
        ]
        for case in bad:
            with self.subTest(case=case), self.assertRaises(SelectionError):
                selection.parse_verdict(json.dumps(case))
        with self.assertRaises(SelectionError):
            selection.parse_verdict("the judge rambled")


class ReviewDefaultTest(unittest.TestCase):
    ALL = ("low", "medium", "high", "xhigh", "max", "ultra")

    def test_reviews_run_on_the_latest_sol_when_the_cli_lists_it(self):
        catalog = Catalog({"gpt-6.1-sol": self.ALL, "gpt-6-sol": self.ALL})
        self.assertEqual(selection.review_default(catalog, "xhigh"), ("gpt-6.1-sol", None))

    def test_an_older_cli_falls_back_to_gpt_6_sol_and_says_why(self):
        model, note = selection.review_default(Catalog({"gpt-6-sol": self.ALL}), "xhigh")
        self.assertEqual(model, "gpt-6-sol")
        self.assertIn("gpt-6.1-sol", note)
        self.assertIn("update codex", note)

    def test_listed_without_the_effort_also_falls_back(self):
        catalog = Catalog({"gpt-6.1-sol": ("low", "medium"), "gpt-6-sol": self.ALL})
        self.assertEqual(selection.review_default(catalog, "xhigh")[0], "gpt-6-sol")
        self.assertEqual(selection.review_default(catalog, "medium"), ("gpt-6.1-sol", None))

    def test_an_unreadable_catalog_keeps_the_latest_sol(self):
        # Nothing says it is missing; codex itself names the problem if it is.
        self.assertEqual(selection.review_default(Catalog(None), "xhigh"),
                         ("gpt-6.1-sol", None))

    def test_no_fallback_when_gpt_6_sol_is_missing_too(self):
        # Validation then refuses naming the model the run actually asked for.
        catalog = Catalog({"gpt-6-luna": self.ALL})
        self.assertEqual(selection.review_default(catalog, "xhigh"), ("gpt-6.1-sol", None))


if __name__ == "__main__":
    unittest.main()
