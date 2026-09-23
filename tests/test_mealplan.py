"""Tests run against a real snapshot of South Dining Hall's Nutrislice data (week of Sep 20, 2026).

Run with:  python -m unittest discover tests
"""
import copy
import datetime as dt
import json
import sys
import tomllib
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import mealplan  # noqa: E402

FIXTURE = json.loads((ROOT / "tests/fixtures/south_2026-09-20_week.json").read_text())
CFG = tomllib.loads((ROOT / "config.toml").read_text())
WED, SAT = dt.date(2026, 9, 23), dt.date(2026, 9, 26)


class FakeFetcher(mealplan.Fetcher):
    """Serves the saved snapshot; meals listed in `fail` raise like a network error."""

    def __init__(self, fail=()):
        super().__init__("south-dining-hall")
        self.fail = set(fail)

    def week(self, meal, day):
        if meal in self.fail:
            raise OSError("HTTP Error 503: Service Unavailable")
        return FIXTURE[meal]


def plan(day, cfg=CFG, fail=()):
    f = FakeFetcher(fail)
    menus = {m: mealplan.get_menu(f, m, day) for m in mealplan.MEAL_TYPES}
    return mealplan.Planner(cfg).plan_day(day, menus)


def all_lines(d):
    return [l for m in d["meals"] if m["plan"] for l in m["plan"]["lines"]]


class ParsingTests(unittest.TestCase):
    def test_real_menu_parses_with_nutrition(self):
        items = mealplan.get_menu(FakeFetcher(), "dinner", WED).items
        self.assertGreater(len(items), 200)
        salmon = next(i for i in items if i.name == "Citrus Herb Salmon")
        self.assertEqual((salmon.cal, salmon.protein, salmon.amount, salmon.unit), (225.0, 20.0, 1.0, "fillet"))
        self.assertIn("fish", salmon.tags)
        self.assertEqual(salmon.station, "Comfort Kitchen")

    def test_bad_numbers_are_flagged_not_used(self):
        items = mealplan.get_menu(FakeFetcher(), "dinner", WED).items
        rice = next(i for i in items if i.name == "Long Grain Rice")  # listed 403 cal per 4 oz
        self.assertFalse(rice.usable)
        self.assertTrue(any("dry weight" in f for f in rice.flags))

    def test_unpublished_day(self):
        res = mealplan.get_menu(FakeFetcher(), "breakfast", SAT)  # weekends: brunch only
        self.assertEqual(res.status, "not_published")


class PlanningTests(unittest.TestCase):
    def test_weekday_hits_targets(self):
        d = plan(WED)
        self.assertEqual([m["meal"] for m in d["meals"]], ["breakfast", "lunch", "dinner"])
        self.assertTrue(d["all_planned"])
        self.assertAlmostEqual(d["cal"], 2240, delta=150)
        self.assertGreaterEqual(d["protein"], 165 * 0.95)
        self.assertLessEqual(d["fat"], 62)

    def test_weekend_uses_brunch(self):
        d = plan(SAT)
        self.assertEqual([m["meal"] for m in d["meals"]], ["brunch", "dinner"])
        self.assertTrue(d["all_planned"])
        self.assertAlmostEqual(d["cal"], 2240, delta=150)

    def test_every_number_comes_from_the_menu(self):
        d = plan(WED)
        for l in all_lines(d):
            it = l["item"]
            self.assertAlmostEqual(l["cal"], it.cal * l["servings"])
            self.assertAlmostEqual(l["protein"], it.protein * l["servings"])
            self.assertTrue(it.usable)

    def test_no_item_twice_in_a_meal_and_no_condiments(self):
        for day in (WED, SAT):
            for m in plan(day)["meals"]:
                names = [l["item"].name for l in m["plan"]["lines"]]
                self.assertEqual(len(names), len(set(names)))
                for n in names:
                    self.assertIsNone(mealplan.NOT_A_DISH.search(n), n)

    def test_allergy_filter_uses_tags_and_ingredients(self):
        cfg = copy.deepcopy(CFG)
        cfg["food"]["avoid"] = ["dairy", "egg", "wheat"]
        d = plan(WED, cfg)
        self.assertTrue(d["all_planned"])
        for l in all_lines(d):
            self.assertFalse({"dairy", "egg", "wheat"} & l["item"].tags, l["item"].name)
            text = l["item"].ingredients.lower()
            for w in ("milk", "cheese", "wheat", "flour"):
                self.assertNotRegex(text, rf"\b{w}\b", l["item"].name)

    def test_vegetarian(self):
        cfg = copy.deepcopy(CFG)
        cfg["food"]["diet"] = "vegetarian"
        for l in all_lines(plan(WED, cfg)):
            self.assertTrue({"vegetarian", "vegan"} & l["item"].tags, l["item"].name)

    def test_targets_are_configurable(self):
        cfg = copy.deepcopy(CFG)
        cfg["targets"].update(calories_per_day=1800, protein_g_per_day=140)
        d = plan(WED, cfg)
        self.assertAlmostEqual(d["cal"], 1800, delta=150)

    def test_swaps_offered(self):
        lines = all_lines(plan(WED))
        self.assertTrue(all(l["swaps"] for l in lines if l["role"] == "protein"))


class FailureReportingTests(unittest.TestCase):
    def test_fetch_error_is_reported(self):
        d = plan(WED, fail={"lunch"})
        lunch = next(m for m in d["meals"] if m["meal"] == "lunch")
        self.assertEqual(lunch["status"], "error")
        issues = mealplan.status_lines([d])
        self.assertTrue(any("Lunch" in m and "503" in m for _, m in issues))
        html = mealplan.render_page([d], CFG, WED, dt.datetime(2026, 9, 23, 7, 0, tzinfo=mealplan.EASTERN))
        self.assertIn("could not be retrieved", html)
        self.assertIn("503", html)

    def test_everything_down(self):
        d = plan(WED, fail=set(mealplan.MEAL_TYPES))
        html = mealplan.render_page([d], CFG, WED, dt.datetime(2026, 9, 23, 7, 0, tzinfo=mealplan.EASTERN))
        self.assertIn("retrieve any South Dining Hall menus", html)
        subject, _ = mealplan.render_email([d], CFG, WED, dt.datetime(2026, 9, 23, 7, 0, tzinfo=mealplan.EASTERN))
        self.assertTrue(subject.startswith("⚠️"))

    def test_page_shows_last_checked(self):
        d = plan(WED)
        html = mealplan.render_page([d], CFG, WED, dt.datetime(2026, 9, 23, 7, 5, tzinfo=mealplan.EASTERN))
        self.assertIn("Menus last checked <b>Wed Sep 23, 7:05 AM ET</b>", html)


class EmailTests(unittest.TestCase):
    def test_email_is_built_and_sent_over_smtp_ssl(self):
        sent = {}

        class FakeSMTP:
            def __init__(self, host, port, timeout):
                sent["host"], sent["port"] = host, port

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def login(self, u, p):
                sent["login"] = (u, p)

            def send_message(self, msg):
                sent["msg"] = msg

        env = {"SMTP_USER": "me@gmail.com", "SMTP_PASSWORD": "app-pass", "EMAIL_TO": "me@nd.edu"}
        days = [plan(WED), plan(dt.date(2026, 9, 26))]
        subject, body = mealplan.render_email(days, CFG, WED, dt.datetime(2026, 9, 23, 7, 0, tzinfo=mealplan.EASTERN))
        with unittest.mock.patch.dict("os.environ", env), unittest.mock.patch("smtplib.SMTP_SSL", FakeSMTP):
            mealplan.send_email(subject, body)
        self.assertEqual((sent["host"], sent["port"]), ("smtp.gmail.com", 465))
        self.assertEqual(sent["msg"]["To"], "me@nd.edu")
        self.assertIn("South DH plan Wed 9/23", sent["msg"]["Subject"])
        html = sent["msg"].get_body(("html",)).get_content()
        self.assertIn("Today: Wednesday, Sep 23", html)
        self.assertIn("Brunch", html)


if __name__ == "__main__":
    unittest.main()
