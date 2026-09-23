#!/usr/bin/env python3
"""Meal planner for Notre Dame South Dining Hall, built from the official Nutrislice menus.

Every number shown comes from Nutrislice's listed per-serving nutrition, multiplied by
the number of servings suggested. Nothing is invented: items without nutrition data are
never used in a plan, and fetch failures are reported instead of being papered over.

Usage:
    python mealplan.py                     # build site/ for today + upcoming days
    python mealplan.py --date 2026-09-24   # pretend "today" is another date
    python mealplan.py --email-if-due      # also send the morning email when it's time
    python mealplan.py --send-email        # send the email right now
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import html
import itertools
import json
import os
import re
import smtplib
import sys
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
EASTERN = ZoneInfo("America/New_York")
API = "https://nd.api.nutrislice.com/menu/api/weeks/school/{hall}/menu-type/{meal}/{y}/{m:02d}/{d:02d}/?format=json"
WEB = "https://nd.nutrislice.com/menu/{hall}/{meal}/{date}"
MEAL_TYPES = ["breakfast", "brunch", "lunch", "dinner"]
MEAL_LABEL = {"breakfast": "Breakfast", "brunch": "Brunch", "lunch": "Lunch", "dinner": "Dinner"}

# Words used to catch allergens in ingredient lists, in addition to Nutrislice's own tags.
# Deliberately broad: a false alarm (e.g. "oat milk" under dairy) is safer than a miss.
ALLERGEN_WORDS = {
    "dairy": r"milk|cheese|butter|cream|whey|casein|yogurt|lactose",
    "egg": r"eggs?|egg whites?|albumen|mayonnaise",
    "soy": r"soy|soya|soybean|tofu|edamame",
    "wheat": r"wheat|flour|semolina|farina|seitan",
    "sesame": r"sesame|tahini",
    "shellfish": r"shellfish|shrimp|crab|lobster|clams?|scallops?|mussels?|oysters?|crawfish",
    "fish": r"fish|salmon|tuna|cod|tilapia|pollock|anchov\w*|swordfish|halibut|sole|trout",
    "peanuts": r"peanuts?",
    "tree-nuts": r"almonds?|walnuts?|pecans?|cashews?|pistachios?|hazelnuts?|macadamia|tree nuts?",
    "pork": r"pork|bacon|ham|prosciutto|pepperoni|chorizo|pancetta|salami",
}
VEG_WORDS = re.compile(
    r"broccoli|green beans?|spinach|carrots?|cauliflower|asparagus|zucchini|squash|vegetables?|"
    r"veggies?|greens|kale|brussels|peppers|cabbage|salad|tomato|cucumber|romaine|lettuce|mixed greens",
    re.I,
)
# Things that show up on the line but aren't a food you'd build a meal around.
NOT_A_DISH = re.compile(
    r"dressing|vinaigrette|sauce|syrup|gravy|garlic|water|jelly|jam|honey|ketchup|mustard|relish|"
    r"sugar|seasoning|salsa|croutons?|sprinkles|marinade|glaze|lemonade|juice|tea\b|coffee",
    re.I,
)
FRUIT_WORDS = re.compile(r"fruit|apple|banana|orange|grape|berr|melon|pineapple|pear|peach", re.I)


# ───────────────────────────── data model ─────────────────────────────

@dataclass
class Item:
    name: str
    station: str
    meal: str
    date: str
    cal: float | None
    protein: float | None
    fat: float | None
    carbs: float | None
    sodium: float  # mg; 0 when not listed
    amount: float
    unit: str
    tags: set[str]
    ingredients: str
    flags: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.cal is not None and self.protein is not None and self.cal > 0 and not self.flags


@dataclass
class MenuResult:
    meal: str
    status: str  # "ok" | "not_published" | "error"
    items: list[Item]
    message: str = ""
    url: str = ""


# ───────────────────────────── fetching ─────────────────────────────

class Fetcher:
    """Fetches Nutrislice week menus once per (meal, week) and caches them for the run."""

    def __init__(self, hall: str, retries: int = 3):
        self.hall = hall
        self.retries = retries
        self.cache: dict[str, dict | Exception] = {}

    def week(self, meal: str, day: dt.date) -> dict:
        sunday = day - dt.timedelta(days=(day.weekday() + 1) % 7)
        url = API.format(hall=self.hall, meal=meal, y=sunday.year, m=sunday.month, d=sunday.day)
        if url not in self.cache:
            self.cache[url] = self._get(url)
        result = self.cache[url]
        if isinstance(result, Exception):
            raise result
        return result

    def _get(self, url: str) -> dict | Exception:
        last: Exception = RuntimeError("no attempt made")
        for attempt in range(self.retries):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "nd-food-meal-planner/1.0"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.load(r)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
                last = e
                time.sleep(2 * (attempt + 1))
        return last


def _num(v) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def parse_day(raw_day: dict, meal: str) -> list[Item]:
    items: list[Item] = []
    station = ""
    for row in raw_day.get("menu_items", []):
        if row.get("is_station_header") or row.get("is_section_title"):
            station = (row.get("text") or "").strip()
            continue
        food = row.get("food")
        if not food:
            continue
        nut = food.get("rounded_nutrition_info") or {}
        size = food.get("serving_size_info") or {}
        item = Item(
            name=(food.get("name") or "").strip(),
            station=station,
            meal=meal,
            date=raw_day.get("date", ""),
            cal=_num(nut.get("calories")),
            protein=_num(nut.get("g_protein")),
            fat=_num(nut.get("g_fat")) or 0.0,
            carbs=_num(nut.get("g_carbs")) or 0.0,
            sodium=_num(nut.get("mg_sodium")) or 0.0,
            amount=_num(size.get("serving_size_amount")) or 1.0,
            unit=(size.get("serving_size_unit") or "serving").strip(),
            tags={i.get("slug", "") for i in (food.get("icons") or {}).get("food_icons", [])},
            ingredients=food.get("ingredients") or "",
        )
        if item.cal is None or item.protein is None:
            item.flags.append("no nutrition listed")
        elif item.cal > 60:
            # Sanity check: listed calories should roughly match 4/4/9 kcal per g of P/C/F.
            est = 4 * item.protein + 4 * item.carbs + 9 * item.fat
            if abs(est - item.cal) / item.cal > 0.35:
                item.flags.append(f"listed calories ({item.cal:.0f}) don't match its macros (~{est:.0f})")
            elif (norm_unit(item.unit) == "oz" and item.cal / item.amount > 90
                  and 9 * item.fat / item.cal < 0.2 and 4 * item.carbs / item.cal > 0.6):
                item.flags.append(f"{item.cal / item.amount:.0f} cal per oz is implausibly high for a low-fat food "
                                  "(may be listed by dry weight)")
        items.append(item)
    return items


def get_menu(fetcher: Fetcher, meal: str, day: dt.date) -> MenuResult:
    url = WEB.format(hall=fetcher.hall, meal=meal, date=day.isoformat())
    try:
        week = fetcher.week(meal, day)
    except Exception as e:  # network / HTTP / bad JSON
        return MenuResult(meal, "error", [], f"Could not retrieve menu: {e}", url)
    for raw_day in week.get("days", []):
        if raw_day.get("date") == day.isoformat():
            items = parse_day(raw_day, meal)
            if items:
                return MenuResult(meal, "ok", items, "", url)
    return MenuResult(meal, "not_published", [], "No menu published", url)


# ───────────────────────────── planning ─────────────────────────────

def norm_unit(unit: str) -> str:
    u = unit.lower().strip()
    if u in ("z", "oz", "ounce", "ounces"):
        return "oz"
    if "fl" in u:
        return "floz"
    if u.startswith("cup"):
        return "cup"
    return "count"


def portion_options(item: Item, role: str) -> list[float]:
    """Sensible numbers of listed servings to take, based on the serving unit."""
    unit, amt = norm_unit(item.unit), item.amount or 1.0
    totals = {
        # Big 8 oz protein portions only for items served as a real entree portion (listed >= 4 oz).
        "oz": {"protein": [3, 4, 6, 8] if amt >= 4 else [3, 4, 6], "carb": [3, 4, 6, 8], "veg": [3, 4]},
        "floz": {"protein": [8, 12, 16], "carb": [8, 12], "veg": [8]},
        "cup": {"protein": [0.5, 1, 1.5], "carb": [0.5, 1, 1.5, 2], "veg": [1]},
    }
    if role == "carb" and unit == "oz" and item.cal and item.cal / amt > 70:
        return [1.0]  # dense foods (dried fruit, chips, breads by weight): one listed serving
    if unit == "count":
        # Small pieces (eggs, tenders, links) up to 3; burgers, fillets, sandwiches up to 2.
        small = (item.cal or 0) < 120
        counts = {"protein": [1, 2, 3] if small else [1, 2], "carb": [1, 2], "veg": [1]}[role]
        return [float(c) for c in counts]
    opts = sorted({round(t / amt, 2) for t in totals[unit][role] if 0.5 <= t / amt <= 8})
    return opts or [1.0]


def describe_portion(item: Item, servings: float) -> str:
    unit, amt = norm_unit(item.unit), item.amount or 1.0
    total = round(servings * amt, 1)
    if unit == "oz":
        text = f"{total:g} oz"
    elif unit == "floz":
        text = f"{total:g} fl oz"
    elif unit == "cup":
        text = f"{total:g} cup" + ("s" if total != 1 else "")
    elif item.unit.lower() in ("serving", "servings", "each", "ea", "customer", "portion"):
        return f"{servings:g} listed serving" + ("s" if servings != 1 else "")
    else:
        text = f"{total:g} {item.unit}"
    if servings != 1:
        text += f" ({servings:g} × listed {amt:g} {item.unit})"
    return text


class Planner:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        t = cfg["targets"]
        self.cal_day = float(t["calories_per_day"])
        self.pro_day = float(t["protein_g_per_day"])
        self.fat_day = float(t["fat_max_g_per_day"])
        self.fat_min_day = float(t.get("fat_min_g_per_day", 0))
        self.sodium_day = float(t.get("sodium_max_mg_per_day", 0))  # 0 = don't limit
        f = cfg["food"]
        self.avoid = [a.lower() for a in f.get("avoid", [])]
        self.diet = (f.get("diet") or "").lower()
        self.avoid_words = [w.lower() for w in f.get("avoid_words", [])]
        self.prefer_words = [w.lower() for w in f.get("prefer_words", [])]
        self.ignore = {n.lower() for n in f.get("ignore_items", [])}
        self.max_fat_share = float(f.get("max_fat_percent_per_item", 40)) / 100
        self.excl_stations = {s.lower() for s in cfg["menu"].get("exclude_stations", [])}

    # --- filtering ---
    def allowed(self, it: Item) -> bool:
        name = it.name.lower()
        if not it.usable or name in self.ignore or it.station.lower() in self.excl_stations:
            return False
        if any(w in name for w in self.avoid_words):
            return False
        if self.diet and self.diet not in it.tags and not (self.diet == "vegetarian" and "vegan" in it.tags):
            return False
        text = f"{it.name} {it.ingredients}".lower()
        for a in self.avoid:
            if a in it.tags or (a in ALLERGEN_WORDS and re.search(rf"\b(?:{ALLERGEN_WORDS[a]})\b", text)):
                return False
        return True

    def preferred(self, it: Item) -> bool:
        return any(w in it.name.lower() for w in self.prefer_words)

    def roles(self, it: Item) -> list[str]:
        cal = it.cal or 0
        if cal <= 0:
            return []
        p_sh, f_sh, c_sh = 4 * it.protein / cal, 9 * it.fat / cal, 4 * it.carbs / cal
        roles = []
        if f_sh > self.max_fat_share or NOT_A_DISH.search(it.name):
            return roles
        if p_sh >= 0.30 and it.protein * max(portion_options(it, "protein")) >= 10:
            roles.append("protein")
        if c_sh >= 0.50 and f_sh <= 0.30 and p_sh < 0.30 and cal >= 40:
            roles.append("carb")
        if VEG_WORDS.search(it.name) and not FRUIT_WORDS.search(it.name) and cal <= 150:
            roles.append("veg")
        return roles

    # --- meal optimisation ---
    def plan_meal(self, items: list[Item], cal_t: float, pro_t: float, fat_t: float, fat_min: float,
                  used: set[str], recent: set[str], sodium_t: float = 0.0) -> dict | None:
        """Pick 1-2 protein items + 0-2 carb sides + 0-1 vegetable, with portions, closest to the targets.

        `used` = items already planned earlier today; `recent` = items in this meal yesterday.
        Both get a small penalty so plans vary when the menu allows it.
        """
        pool: dict[str, list[Item]] = {"protein": [], "carb": [], "veg": []}
        seen: set[str] = set()
        for it in items:
            if it.name in seen or not self.allowed(it):
                continue
            seen.add(it.name)
            for r in self.roles(it):
                pool[r].append(it)
        if not pool["protein"]:
            return None

        def bonus(it: Item) -> float:
            return (0.15 * self.preferred(it) + 0.05 * ("high-performance" in it.tags)
                    + 0.10 * (it.protein >= 15) - 0.05 * (it.name in recent))

        prot = sorted(pool["protein"], key=lambda i: -(4 * i.protein / i.cal + bonus(i)))[:10]
        carb = sorted(pool["carb"], key=lambda i: -(4 * i.carbs / i.cal - 9 * i.fat / i.cal + bonus(i)))[:9]
        veg = sorted(pool["veg"], key=lambda i: -(4 * i.protein / i.cal + bonus(i)))[:4]

        def item_pen(it: Item, role: str) -> float:
            pen = 0.012 - 0.02 * self.preferred(it) - 0.01 * ("high-performance" in it.tags)
            pen += 0.04 * (it.name in recent)
            # Repeating something already eaten earlier today: strong nudge for mains, lighter for sides.
            pen += (0.15 if role == "protein" else 0.06) * (it.name in used)
            if role == "protein":
                pen -= 0.03 * (it.protein >= 15)
            return pen

        def options(pool_items: list[Item], role: str, max_items: int) -> list[tuple]:
            """All (combo, cal, protein, fat, sodium, penalty) for up to max_items distinct items of a role."""
            singles = [((it, s, role),) for it in pool_items for s in portion_options(it, role)]
            combos = [()] if role != "protein" else []
            combos += singles
            if max_items >= 2:
                combos += [a + b for a, b in itertools.combinations(singles, 2) if a[0][0] is not b[0][0]]
            out = []
            for c in combos:
                out.append((c, sum(i.cal * s for i, s, _ in c), sum(i.protein * s for i, s, _ in c),
                            sum(i.fat * s for i, s, _ in c), sum(i.sodium * s for i, s, _ in c),
                            sum(item_pen(i, r) for i, _, r in c)))
            return out

        prot_opts = options(prot, "protein", 2)
        carb_opts = sorted(options(carb, "carb", 2), key=lambda x: x[1])
        carb_cals = [x[1] for x in carb_opts]
        veg_opts = options(veg, "veg", 1)
        window = 0.25 * cal_t

        best, best_score = None, float("inf")
        for pc, p_cal, p_pro, p_fat, p_na, p_pen in prot_opts:
            if p_cal > cal_t * 1.2:
                continue
            rem = cal_t - p_cal
            lo, hi = bisect.bisect_left(carb_cals, rem - window - 60), bisect.bisect_right(carb_cals, rem + window)
            for cc, c_cal, c_pro, c_fat, c_na, c_pen in carb_opts[lo:hi]:
                for vc, v_cal, v_pro, v_fat, v_na, v_pen in veg_opts:
                    if vc and any(vc[0][0] is x[0] for x in pc + cc):
                        continue
                    cal, pro, fat = p_cal + c_cal + v_cal, p_pro + c_pro + v_pro, p_fat + c_fat + v_fat
                    dc = (cal - cal_t) / cal_t
                    score = 4 * dc * dc + p_pen + c_pen + v_pen - 0.02 * bool(vc)
                    if pro < pro_t:
                        score += 6 * ((pro_t - pro) / pro_t) ** 2
                    else:
                        score += max(0.0, (pro - 1.35 * pro_t) / pro_t) ** 2
                    if fat > fat_t:
                        score += 3 * ((fat - fat_t) / fat_t) ** 2
                    elif fat < fat_min:
                        score += 3 * ((fat_min - fat) / fat_min) ** 2
                    # Sodium is a softer limit than fat: gently steer away, never force a bad meal.
                    na = p_na + c_na + v_na
                    if sodium_t and na > sodium_t:
                        score += 0.8 * ((na - sodium_t) / sodium_t) ** 2
                    if score < best_score:
                        best, best_score = pc + cc + vc, score
        if best is None:
            return None
        chosen = {i.name for i, _, _ in best}
        lines = []
        for it, s, role in best:
            lines.append({
                "item": it, "servings": s, "role": role,
                "portion": describe_portion(it, s),
                "cal": it.cal * s, "protein": it.protein * s, "fat": it.fat * s, "carbs": it.carbs * s,
                "sodium": it.sodium * s,
                "swaps": self.swaps(it, s, role, pool[role], chosen),
            })
        return {
            "lines": lines,
            "cal": sum(l["cal"] for l in lines), "protein": sum(l["protein"] for l in lines),
            "fat": sum(l["fat"] for l in lines), "carbs": sum(l["carbs"] for l in lines),
            "sodium": sum(l["sodium"] for l in lines),
            "cal_target": cal_t, "protein_target": pro_t,
        }

    def swaps(self, it: Item, servings: float, role: str, pool: list[Item], chosen: set[str]) -> list[dict]:
        base_cal, base_pro = it.cal * servings, it.protein * servings
        out = []
        for alt in pool:
            if alt.name in chosen:
                continue
            s = min(portion_options(alt, role), key=lambda x: abs(alt.cal * x - base_cal))
            dc, dp = alt.cal * s - base_cal, alt.protein * s - base_pro
            key = abs(dc) + (4 * abs(dp) if role == "protein" else abs(dp))
            out.append((key, {"item": alt, "portion": describe_portion(alt, s),
                              "cal": alt.cal * s, "protein": alt.protein * s, "dcal": dc, "dpro": dp}))
        return [o for _, o in sorted(out, key=lambda x: x[0])[:2]]

    def plan_day(self, day: dt.date, menus: dict[str, MenuResult], recent: dict[str, set[str]] | None = None) -> dict:
        split = self.cfg["meal_split"]
        has = lambda m: menus[m].status == "ok"  # noqa: E731
        if has("brunch") and not (has("breakfast") or has("lunch")):
            slots = [("brunch", split["breakfast"] + split["lunch"]), ("dinner", split["dinner"])]
        else:
            slots = [("breakfast", split["breakfast"]), ("lunch", split["lunch"]), ("dinner", split["dinner"])]
            if has("brunch"):
                slots.insert(1, ("brunch", 0.0))  # rare: shown for reference only
        fat_share = self.fat_day / self.cal_day
        meals, used, carry_cal, carry_pro = [], set(), 0.0, 0.0
        for meal, share in slots:
            res = menus[meal]
            entry = {"meal": meal, "status": res.status, "message": res.message, "url": res.url,
                     "share": share, "plan": None,
                     "missing_nutrition": [i.name for i in res.items if "no nutrition listed" in i.flags],
                     "suspect": [(i.name, f) for i in res.items for f in i.flags if f != "no nutrition listed"]}
            if res.status == "ok" and share > 0:
                cal_t = max(200.0, share * self.cal_day + max(-150.0, min(150.0, carry_cal)))
                pro_t = max(15.0, share * self.pro_day + max(-15.0, min(15.0, carry_pro)))
                fat_min = cal_t * self.fat_min_day / self.cal_day
                plan = self.plan_meal(res.items, cal_t, pro_t, cal_t * fat_share, fat_min, used,
                                      (recent or {}).get(meal, set()), share * self.sodium_day)
                if plan:
                    entry["plan"] = plan
                    carry_cal = cal_t - plan["cal"]
                    carry_pro = pro_t - plan["protein"]
                    used |= {l["item"].name for l in plan["lines"] if l["role"] != "veg"}
                else:
                    entry["message"] = "Menu retrieved, but no items fit your filters with nutrition data."
            meals.append(entry)
        planned = [m["plan"] for m in meals if m["plan"]]
        return {
            "date": day, "meals": meals,
            "cal": sum(p["cal"] for p in planned), "protein": sum(p["protein"] for p in planned),
            "fat": sum(p["fat"] for p in planned), "carbs": sum(p["carbs"] for p in planned),
            "sodium": sum(p["sodium"] for p in planned),
            "all_planned": all(m["plan"] for m in meals if m["share"] > 0),
            "any_menu": any(m["status"] == "ok" for m in meals),
        }


# ───────────────────────────── rendering ─────────────────────────────

def esc(s) -> str:
    return html.escape(str(s))


def fmt_day(d: dt.date) -> str:
    return d.strftime("%A, %b %-d")


def day_label(d: dt.date, today: dt.date) -> str:
    return {0: "Today", 1: "Tomorrow"}.get((d - today).days, d.strftime("%a %-m/%-d"))


def status_lines(days: list[dict]) -> list[tuple[str, str]]:
    """(level, message) pairs describing anything that couldn't be retrieved."""
    out = []
    for d in days:
        for m in d["meals"]:
            label = f"{fmt_day(d['date'])} {MEAL_LABEL[m['meal']]}"
            if m["status"] == "error":
                out.append(("error", f"{label}: {m['message']}"))
            elif m["status"] == "ok" and m["share"] > 0 and not m["plan"]:
                out.append(("warn", f"{label}: {m['message']}"))
    return out


def sources_note(cfg: dict) -> str:
    return (
        "<b>Where the numbers come from:</b> every calorie, protein, carb, fat and sodium value is the per-serving "
        "figure Notre Dame publishes on Nutrislice for that exact day and meal (click the meal's "
        "“source” link). When a plan says 2× or 6 oz, the listed values are simply multiplied by that "
        "many servings; that multiplication is the only estimate. Servers don't weigh portions, so "
        "real plates vary. As a guide: 3–4 oz of meat or fish ≈ a deck of cards / your palm, "
        "1 cup ≈ a fist, 1 oz ≈ two thumbs. Items with no nutrition listed, or whose calories "
        "don't match their own protein/carb/fat numbers, are never used. Allergen filtering uses "
        "Nutrislice's tags plus a scan of ingredient lists, but it is not a guarantee. Check with "
        "dining staff for anything serious."
    )


CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1c2230;--muted:#5d6678;--line:#e3e6ec;--accent:#0c2340;--gold:#ae9142;
--ok:#1f7a4a;--warn:#9a6a00;--err:#b3261e;--chip:#eef1f6}
@media (prefers-color-scheme:dark){:root{--bg:#10141b;--card:#171c25;--ink:#e8ecf3;--muted:#9aa4b6;--line:#283041;
--accent:#9fb8e0;--gold:#d4b75c;--ok:#5cc58c;--warn:#e0b04a;--err:#ff7b72;--chip:#222a37}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:860px;margin:0 auto;padding:16px}
h1{font-size:1.35rem;margin:.2rem 0}h2{font-size:1.1rem;margin:0}h3{font-size:1rem;margin:0}
.muted{color:var(--muted)}.small{font-size:.85rem}
.banner{border-radius:10px;padding:10px 12px;margin:10px 0;border:1px solid var(--line);background:var(--card)}
.banner.ok{border-left:4px solid var(--ok)}.banner.warn{border-left:4px solid var(--warn)}.banner.error{border-left:4px solid var(--err)}
nav{display:flex;gap:6px;flex-wrap:wrap;margin:12px 0}
nav a{padding:6px 12px;border-radius:999px;background:var(--chip);color:var(--ink);text-decoration:none;font-size:.9rem}
nav a.active{background:var(--accent);color:var(--bg)}
.day{display:none}.day.active{display:block}.nojs .day{display:block;margin-bottom:28px}
.totals{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:8px;margin:10px 0}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 10px}
.tile b{font-size:1.15rem;display:block}.bar{height:6px;background:var(--chip);border-radius:3px;margin-top:4px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--gold)}.bar i.over{background:var(--err)}
.macros{display:none;font-size:.82rem;margin-top:2px}
@media (max-width:620px){td.n,th.n{display:none}.macros{display:block}}
.meal{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px;margin:12px 0}
.meal header{display:flex;justify-content:space-between;align-items:baseline;gap:8px;flex-wrap:wrap}
table{width:100%;border-collapse:collapse;margin-top:8px;font-size:.92rem}
td,th{padding:6px 4px;border-top:1px solid var(--line);vertical-align:top;text-align:left}
th{font-weight:600;color:var(--muted);font-size:.8rem;border-top:none}
td.n,th.n{text-align:right;white-space:nowrap}
.swap{color:var(--muted);font-size:.82rem;margin-top:2px}
.tag{display:inline-block;font-size:.72rem;padding:0 6px;border-radius:6px;background:var(--chip);color:var(--muted);margin-left:4px}
.sum td{font-weight:600}
a{color:var(--accent)}details{margin-top:6px}summary{cursor:pointer;color:var(--muted);font-size:.85rem}
"""


def render_meal(m: dict, cfg: dict) -> str:
    hall = cfg["menu"]["hall_name"]
    head = (f"<header><h3>{MEAL_LABEL[m['meal']]} · {esc(hall)}</h3>"
            f"<a class='small' href='{esc(m['url'])}' target='_blank' rel='noopener'>source: Nutrislice ↗</a></header>")
    if m["status"] == "error":
        return f"<section class='meal'>{head}<div class='banner error'>⚠️ {esc(m['message'])}. Check the source link directly.</div></section>"
    if m["status"] == "not_published":
        return f"<section class='meal'>{head}<p class='muted'>No {MEAL_LABEL[m['meal']].lower()} menu published for this day (yet).</p></section>"
    if not m["plan"]:
        return f"<section class='meal'>{head}<div class='banner warn'>{esc(m['message'] or 'No plan for this meal.')}</div></section>"
    p = m["plan"]
    rows = []
    for l in p["lines"]:
        it = l["item"]
        swaps = "".join(
            f"<div class='swap'>↔ swap: {esc(s['item'].name)}, {esc(s['portion'])} "
            f"({s['cal']:.0f} cal, {s['protein']:.0f} g P; {s['dcal']:+.0f} cal, {s['dpro']:+.0f} g P)</div>"
            for s in l["swaps"])
        rows.append(
            f"<tr><td><b>{esc(it.name)}</b><span class='tag'>{esc(it.station)}</span>"
            f"<div class='small muted'>{esc(l['portion'])}</div>{macro_line(l)}{swaps}</td>{macro_cells(l)}</tr>")
    rows.append(f"<tr class='sum'><td>Meal total <span class='small muted'>(target ≈ {p['cal_target']:.0f} cal, "
                f"{p['protein_target']:.0f} g P)</span>{macro_line(p)}</td>{macro_cells(p)}</tr>")
    extra = ""
    if m["missing_nutrition"] or m["suspect"]:
        li = "".join(f"<li>{esc(n)}: no nutrition listed</li>" for n in m["missing_nutrition"])
        li += "".join(f"<li>{esc(n)}: {esc(f)}</li>" for n, f in m["suspect"])
        extra = (f"<details><summary>{len(m['missing_nutrition']) + len(m['suspect'])} item(s) on this menu "
                 f"were skipped for data problems</summary><ul class='small'>{li}</ul></details>")
    heads = "".join(f"<th class='n'>{h}</th>" for h in ("Cal", "Protein", "Carbs", "Fat", "Sodium"))
    return (f"<section class='meal'>{head}<table><tr><th>Item & portion</th>{heads}</tr>"
            f"{''.join(rows)}</table>{extra}</section>")


def macro_cells(x: dict) -> str:
    """Numeric table columns (wide screens)."""
    return (f"<td class='n'>{x['cal']:.0f}</td><td class='n'>{x['protein']:.0f} g</td>"
            f"<td class='n'>{x['carbs']:.0f} g</td><td class='n'>{x['fat']:.0f} g</td>"
            f"<td class='n'>{x['sodium']:,.0f} mg</td>")


def macro_line(x: dict) -> str:
    """Same numbers as one line under the item (phones, where 5 columns don't fit)."""
    return (f"<div class='macros'>{x['cal']:.0f} cal · <b>{x['protein']:.0f} g protein</b> · "
            f"{x['carbs']:.0f} g carbs · {x['fat']:.0f} g fat · {x['sodium']:,.0f} mg sodium</div>")


def tile(label: str, val: float, target: float, unit: str, cap: bool = False, note: str = "") -> str:
    pct = 0 if target <= 0 else min(100, 100 * val / target)
    over = cap and target > 0 and val > target
    sub = note or f"{'limit' if cap else 'target'} {target:,.0f}{unit}"
    bar = "" if target <= 0 else f"<div class='bar'><i class='{'over' if over else ''}' style='width:{pct:.0f}%'></i></div>"
    return (f"<div class='tile'><span class='small muted'>{label}</span><b>{val:,.0f}{unit}</b>"
            f"<span class='small muted'>{sub}</span>{bar}</div>")


def render_day(d: dict, cfg: dict, today: dt.date, active: bool) -> str:
    t = cfg["targets"]
    body = []
    if not d["any_menu"]:
        body.append("<div class='banner warn'>No South Dining Hall menus are available for this day "
                    "(not published yet, closed, or couldn't be retrieved; see below).</div>")
    else:
        body.append("<div class='totals'>" + tile("Calories", d["cal"], t["calories_per_day"], "")
                    + tile("Protein", d["protein"], t["protein_g_per_day"], " g")
                    + tile("Carbs", d["carbs"], 0, " g", note="fills the rest")
                    + tile("Fat", d["fat"], t["fat_max_g_per_day"], " g", cap=True)
                    + tile("Sodium", d["sodium"], t.get("sodium_max_mg_per_day", 0), " mg", cap=True,
                           note="" if t.get("sodium_max_mg_per_day") else "no limit set") + "</div>")
        gap = t["protein_g_per_day"] - d["protein"]
        if d["all_planned"] and gap > 10:
            body.append(f"<div class='banner warn'>Planned meals come up {gap:.0f} g short on protein. "
                        f"Adding a glass of skim milk (8 g) or a Greek yogurt at any meal closes the gap.</div>")
        na_max = t.get("sodium_max_mg_per_day", 0)
        if na_max and d["sodium"] > na_max:
            body.append(f"<div class='banner warn'>Sodium comes to {d['sodium']:,.0f} mg, over your "
                        f"{na_max:,.0f} mg limit; that's common with dining-hall food. Skipping deli meats, "
                        f"sauces and soups, and drinking plenty of water, helps.</div>")
    body += [render_meal(m, cfg) for m in d["meals"]]
    iso = d["date"].isoformat()
    return (f"<section class='day{' active' if active else ''}' id='d{iso}'>"
            f"<h2>{day_label(d['date'], today)} · {fmt_day(d['date'])}</h2>{''.join(body)}</section>")


def goal_text(t: dict) -> str:
    text = f"{t['calories_per_day']:,} cal · {t['protein_g_per_day']} g protein · ≤ {t['fat_max_g_per_day']} g fat"
    if t.get("sodium_max_mg_per_day"):
        text += f" · ≤ {t['sodium_max_mg_per_day']:,} mg sodium"
    return text


def render_page(days: list[dict], cfg: dict, today: dt.date, checked: dt.datetime) -> str:
    issues = status_lines(days)
    if not any(d["any_menu"] for d in days):
        banner = ("error", "Couldn't retrieve any South Dining Hall menus. The menu site may be down "
                           "or unreachable. The planner will retry automatically.")
    elif any(l == "error" for l, _ in issues):
        banner = ("error", "Some menus could not be retrieved; affected meals are marked below.")
    else:
        banner = ("ok", "All published menus retrieved successfully.")
    issue_html = "".join(f"<li>{esc(msg)}</li>" for _, msg in issues)
    issue_html = f"<ul class='small'>{issue_html}</ul>" if issue_html else ""
    nav = "".join(f"<a href='#d{d['date'].isoformat()}' data-d='d{d['date'].isoformat()}'>{day_label(d['date'], today)}</a>"
                  for d in days)
    t = cfg["targets"]
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ND South Meal Plan</title>
<style>{CSS}</style></head><body class="nojs"><main>
<h1>South Dining Hall meal plan</h1>
<div class="muted small">Goal: {goal_text(t)} per day.
Menus last checked <b>{checked.strftime('%a %b %-d, %-I:%M %p')} ET</b> (refreshes about hourly).</div>
<div class="banner {banner[0]}">{esc(banner[1])}{issue_html}</div>
<nav>{nav}</nav>
{''.join(render_day(d, cfg, today, i == 0) for i, d in enumerate(days))}
<p class="small muted">{sources_note(cfg)}</p>
<p class="small muted">Change targets, allergies or preferences in <code>config.toml</code> in the GitHub repo.</p>
</main>
<script>
document.body.classList.remove('nojs');
function show(id){{document.querySelectorAll('.day').forEach(s=>s.classList.toggle('active',s.id===id));
document.querySelectorAll('nav a').forEach(a=>a.classList.toggle('active',a.dataset.d===id));}}
document.querySelectorAll('nav a').forEach(a=>a.addEventListener('click',e=>{{e.preventDefault();show(a.dataset.d);history.replaceState(null,'','#'+a.dataset.d);}}));
show((location.hash||'').slice(1)||document.querySelector('nav a')?.dataset.d);
</script></body></html>"""


def render_email(days: list[dict], cfg: dict, today: dt.date, checked: dt.datetime) -> tuple[str, str]:
    """Plain-HTML email (inline styles only) for today + tomorrow."""
    t = cfg["targets"]
    td = "style='padding:4px 6px;border-top:1px solid #ddd;vertical-align:top'"
    tdn = "style='padding:4px 6px;border-top:1px solid #ddd;text-align:right;white-space:nowrap'"
    parts = [f"<div style='font-family:Arial,sans-serif;font-size:14px;color:#1c2230;max-width:640px'>",
             f"<p style='color:#5d6678'>Menus checked {checked.strftime('%-I:%M %p ET, %a %b %-d')}. "
             f"Goal {goal_text(t)}.</p>"]
    issues = status_lines(days)
    if issues:
        parts.append("<p style='background:#fdecea;padding:8px;border-radius:6px'><b>⚠️ Problems:</b><br>"
                     + "<br>".join(esc(m) for _, m in issues) + "</p>")
    for d in days:
        parts.append(f"<h2 style='font-size:17px;margin:18px 0 4px'>{day_label(d['date'], today)}: {fmt_day(d['date'])}</h2>")
        if not d["any_menu"]:
            parts.append("<p>No menus available for this day.</p>")
            continue
        parts.append(f"<p style='margin:0 0 6px'><b>Day total: {d['cal']:.0f} cal · {d['protein']:.0f} g protein · "
                     f"{d['carbs']:.0f} g carbs · {d['fat']:.0f} g fat · {d['sodium']:,.0f} mg sodium</b></p>")
        for m in d["meals"]:
            head = f"<a href='{esc(m['url'])}'>{MEAL_LABEL[m['meal']]}</a> · {esc(cfg['menu']['hall_name'])}"
            if not m["plan"]:
                if m["share"] > 0:
                    msg = m["message"] or "No menu published"
                    parts.append(f"<p><b>{head}</b>: {esc(msg)}</p>")
                continue
            p = m["plan"]
            rows = "".join(
                f"<tr><td {td}><b>{esc(l['item'].name)}</b>, {esc(l['portion'])}"
                + "".join(f"<br><span style='color:#5d6678;font-size:12px'>↔ {esc(s['item'].name)}, {esc(s['portion'])} "
                          f"({s['dcal']:+.0f} cal, {s['dpro']:+.0f} g P)</span>" for s in l["swaps"][:1])
                + f"<br><span style='font-size:12px'>{l['carbs']:.0f} g carbs · {l['fat']:.0f} g fat · "
                f"{l['sodium']:,.0f} mg sodium</span>"
                + f"</td><td {tdn}>{l['cal']:.0f} cal</td><td {tdn}>{l['protein']:.0f} g P</td></tr>"
                for l in p["lines"])
            rows += (f"<tr><td {td}><b>Total</b><br><span style='font-size:12px'>{p['carbs']:.0f} g carbs · "
                     f"{p['fat']:.0f} g fat · {p['sodium']:,.0f} mg sodium</span></td><td {tdn}><b>{p['cal']:.0f} cal</b></td>"
                     f"<td {tdn}><b>{p['protein']:.0f} g P</b></td></tr>")
            parts.append(f"<p style='margin:10px 0 2px'><b>{head}</b></p><table style='border-collapse:collapse;width:100%'>{rows}</table>")
    url = cfg["email"].get("page_url", "")
    if url:
        parts.append(f"<p><a href='{esc(url)}'>Open the full plan with more swaps and upcoming days →</a></p>")
    parts.append(f"<p style='color:#5d6678;font-size:12px'>{sources_note(cfg)}</p></div>")
    first = days[0]
    subject = (f"South DH plan {first['date'].strftime('%a %-m/%-d')}: {first['cal']:.0f} cal, {first['protein']:.0f} g protein"
               if first["any_menu"] else f"South DH plan {first['date'].strftime('%a %-m/%-d')}: menu unavailable")
    if issues:
        subject = "⚠️ " + subject
    return subject, "".join(parts)


def send_email(subject: str, body_html: str) -> None:
    user, pw, to = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD"), os.environ.get("EMAIL_TO")
    if not (user and pw and to):
        raise RuntimeError("Email secrets SMTP_USER / SMTP_PASSWORD / EMAIL_TO are not all set")
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.set_content("Your meal plan is in the HTML part of this email.")
    msg.add_alternative(body_html, subtype="html")
    host, port = os.environ.get("SMTP_HOST", "smtp.gmail.com"), int(os.environ.get("SMTP_PORT", "465"))
    with smtplib.SMTP_SSL(host, port, timeout=30) as s:
        s.login(user, pw)
        s.send_message(msg)


# ───────────────────────────── main ─────────────────────────────

def serialize(days: list[dict], checked: dt.datetime) -> dict:
    def line(l):
        return {"item": l["item"].name, "station": l["item"].station, "portion": l["portion"],
                "cal": round(l["cal"]), "protein_g": round(l["protein"]), "carbs_g": round(l["carbs"]),
                "fat_g": round(l["fat"]), "sodium_mg": round(l["sodium"]),
                "swaps": [{"item": s["item"].name, "portion": s["portion"], "cal": round(s["cal"]),
                           "protein_g": round(s["protein"])} for s in l["swaps"]]}
    return {
        "checked_at": checked.isoformat(),
        "days": [{
            "date": d["date"].isoformat(), "cal": round(d["cal"]), "protein_g": round(d["protein"]),
            "carbs_g": round(d["carbs"]), "fat_g": round(d["fat"]), "sodium_mg": round(d["sodium"]),
            "meals": [{"meal": m["meal"], "status": m["status"], "message": m["message"], "source": m["url"],
                       "items": [line(l) for l in m["plan"]["lines"]] if m["plan"] else []} for m in d["meals"]],
        } for d in days],
    }


def build(cfg: dict, today: dt.date, fetcher: Fetcher | None = None) -> list[dict]:
    fetcher = fetcher or Fetcher(cfg["menu"]["hall"])
    planner = Planner(cfg)
    days = []
    for n in range(int(cfg["menu"].get("days_ahead", 6)) + 1):
        day = today + dt.timedelta(days=n)
        menus = {meal: get_menu(fetcher, meal, day) for meal in MEAL_TYPES}
        recent = {m["meal"]: {l["item"].name for l in m["plan"]["lines"]}
                  for m in (days[-1]["meals"] if days else []) if m["plan"]}
        days.append(planner.plan_day(day, menus, recent))
    # Keep today and tomorrow always; drop later days that have nothing published yet.
    return [d for i, d in enumerate(days) if i < 2 or d["any_menu"]]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "config.toml"))
    ap.add_argument("--out", default=str(ROOT / "site"))
    ap.add_argument("--date", help="plan as if today were YYYY-MM-DD")
    ap.add_argument("--email-if-due", action="store_true", help="send the email if it's past send hour and not sent today")
    ap.add_argument("--send-email", action="store_true", help="send the email now")
    ap.add_argument("--state-dir", default=str(ROOT / ".state"))
    args = ap.parse_args()

    cfg = tomllib.loads(Path(args.config).read_text())
    now = dt.datetime.now(EASTERN)
    today = dt.date.fromisoformat(args.date) if args.date else now.date()

    days = build(cfg, today)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(render_page(days, cfg, today, now))
    status = serialize(days, now)
    status["problems"] = [m for _, m in status_lines(days)]
    status["ok"] = any(d["any_menu"] for d in days[:1]) and not any(l == "error" for l, _ in status_lines(days[:2]))

    exit_code = 0
    state = Path(args.state_dir)
    marker = state / f"email-sent-{today.isoformat()}"
    due = (args.email_if_due and cfg["email"].get("enabled", True) and not marker.exists()
           and now.hour >= int(cfg["email"].get("send_hour_eastern", 7)))
    secrets_set = all(os.environ.get(k) for k in ("SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO"))
    if due and not secrets_set and not args.send_email:
        # Not an error: the page still works; the README explains how to switch email on.
        status["email"] = "not configured (SMTP_USER / SMTP_PASSWORD / EMAIL_TO secrets missing)"
        print("Email due but not configured; skipping.")
    elif args.send_email or due:
        subject, body = render_email(days[:2], cfg, today, now)
        try:
            send_email(subject, body)
            state.mkdir(parents=True, exist_ok=True)
            marker.write_text(now.isoformat())
            status["email"] = f"sent: {subject}"
            print(f"Email sent: {subject}")
        except Exception as e:
            status["email"] = f"FAILED: {e}"
            print(f"ERROR sending email: {e}", file=sys.stderr)
            exit_code = 2
    (out / "plan.json").write_text(json.dumps(status, indent=1))

    for d in days:
        meals = ", ".join(f"{m['meal']}={m['status']}" for m in d["meals"])
        print(f"{d['date']}: {d['cal']:.0f} cal, {d['protein']:.0f} g P, {d['carbs']:.0f} g C, {d['fat']:.0f} g fat, "
              f"{d['sodium']:,.0f} mg Na  [{meals}]")
    for _, msg in status_lines(days):
        print("PROBLEM:", msg)
    print(f"Wrote {out / 'index.html'}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
