# ND South Dining Hall meal planner

Builds a daily **breakfast / lunch / dinner plan** (brunch + dinner on weekends) from the
**official South Dining Hall menu** for that day. The default goal is about **2,240 calories,
165 g protein, lean (40–62 g fat), sodium kept near 2,300 mg**. You get:

- **A webpage** (bookmark it on your phone): https://declan227.github.io/ND-FOOD/
  It covers today, tomorrow, and every later day Notre Dame has published, and it refreshes about every hour.
- **A morning email** (7 AM Eastern) with today's plan plus a preview of tomorrow.

Each plan shows the dining hall, meal period, items, portion, calories, protein, carbs, fat and sodium. It also
suggests easy swaps and links to the exact official menu page each number came from.

## Where the data comes from (and its limits)

| What | URL |
|---|---|
| ND Dining's South Dining Hall page | https://dining.nd.edu/dining-locations/south-dining-hall/ |
| Official menu (what ND links to) | https://nd.nutrislice.com/menu/south-dining-hall/ |
| Machine-readable feed this tool reads | `https://nd.api.nutrislice.com/menu/api/weeks/school/south-dining-hall/menu-type/{breakfast,lunch,dinner,brunch}/YYYY/MM/DD/` |

What was verified against live data on **Sep 23, 2026**:

- **How far ahead:** menus were published through **Sat Oct 3**, about 10 days ahead. The following week was still empty.
- **Weekends:** there is **no breakfast or lunch on Saturday and Sunday**, only **Brunch** and **Dinner**.
- **Nutrition:** about 99% of items list calories, protein, fat and carbs per serving, along with the
  serving size (for example "4 oz", "1 fillet"). About 1% list no nutrition, and the planner never uses those.
- **Ingredients:** 100% of items have an ingredient list.
- **Allergens:** each item is tagged for dairy, egg, soy, wheat, sesame, fish, shellfish,
  peanuts, tree nuts and pork, and marked vegetarian, vegan or "high-performance".
- **Known data problems:**
  - Some numbers are implausible. For example, "Long Grain Rice 4 oz = 403 cal" looks like a dry weight.
    The planner flags items like this and skips them. They are listed under "skipped for data problems" on each meal.
  - Some tags are wrong. For example, walnuts are tagged "peanuts".

**How numbers are estimated:** the only estimate is portion scaling. If the menu lists
"Grilled Chicken Breast, 2 oz = 80 cal / 12 g protein" and the plan says 4 oz, it shows 160 cal
/ 24 g. Real plates vary because servers don't weigh food. As a rough guide:
- 3–4 oz of meat ≈ your palm
- 1 cup ≈ your fist
- 1 oz ≈ two thumbs

Allergen filtering uses the tags **and** scans ingredient text. It errs on the side of caution, but it is **not a guarantee**.

## One-time setup (about 10 minutes)

1. **Put the code on `main`.** Merge the pull request (or branch) into `main`. GitHub only runs
   scheduled jobs from the main branch.
2. **Turn on the webpage.** In the repo, go to **Settings → Pages**. Under "Build and deployment", set
   **Source** to **GitHub Actions**.
3. **Set up email** (skip this for now if you only want the webpage; nothing breaks without it):
   1. Use a Gmail account that has 2-Step Verification on. A personal Gmail is safest, because
      @nd.edu Google accounts may not allow app passwords.
   2. Go to https://myaccount.google.com/apppasswords, create an app password named "ND food",
      and copy the 16-letter code.
   3. In the repo, go to **Settings → Secrets and variables → Actions → New repository secret** and add three secrets:
      - `SMTP_USER`: that Gmail address (the email is sent *from* it)
      - `SMTP_PASSWORD`: the 16-letter app password
      - `EMAIL_TO`: where you want the plan delivered (it can be your @nd.edu address)
4. **Test it.** Go to the **Actions** tab → **Update meal plan** → **Run workflow**. Tick "Send the
   email now" and click Run. Within about 2 minutes the page should be live and the email should arrive.

After that, everything runs by itself:
- **Every hour:** it downloads the latest menus, re-plans, and republishes the page. The page shows
  "Menus last checked …", so you can always see how fresh it is.
- **7 AM Eastern:** the email is sent once per day, on the first hourly run after 7.
- **When you edit `config.toml`:** the page rebuilds right away.

## Changing your targets, allergies and preferences

Edit **`config.toml`** on GitHub: open the file, click the pencil ✏️, then **Commit changes**. The
page updates within a couple of minutes.

- `calories_per_day`, `protein_g_per_day`, `fat_max_g_per_day`, `fat_min_g_per_day`
- `sodium_max_mg_per_day`: a softer limit than fat. The planner picks lower-sodium options but won't force a bad meal to hit it (0 = ignore sodium)
- `max_fat_percent_per_item`: drops individual items that are mostly fat (fried or cheesy food)
- `[meal_split]`: how much of the day goes to breakfast, lunch and dinner
- `avoid`: allergens, e.g. `["peanuts", "tree-nuts", "shellfish"]`
- `diet`: `"vegetarian"` or `"vegan"`
- `avoid_words` / `prefer_words`: words in item names to skip or favor
- `ignore_items`: exact item names whose numbers you don't trust
- `send_hour_eastern`: when the email goes out

Note: this repository is **public**, so `config.toml` is visible to anyone. If you'd rather keep
allergies private, make the repo private. GitHub Pages on private repos needs a paid GitHub plan;
without one, you'd keep the email and lose the webpage.

## Keeping the automatic updates working

- **If something breaks, you'll know:**
  - **Menu couldn't be downloaded:** the page shows a red banner naming the exact meal and error, and the
    email subject starts with ⚠️. It retries every hour on its own.
  - **Email failed** (for example, the app password was revoked): the GitHub run fails and GitHub emails
    you a "Run failed" notice. Fix the secret and re-run.
- **GitHub's 60-day rule:** GitHub pauses scheduled jobs in repos with no activity for 60 days.
  The workflow re-enables itself every run to prevent this. If it's ever paused anyway, open the
  **Actions** tab and click **Enable workflow**.
- **Breaks and holidays:** when South isn't serving, the page says "No menu published". That's expected.
- **If Notre Dame changes menu systems:** every meal will show "Could not retrieve menu". The dining site
  (https://dining.nd.edu/) will show the new menu link; the fetch code is in `mealplan.py` (`API = …`).

## Running it on your own computer (optional)

Requires Python 3.11+. No packages to install.

```bash
python3 mealplan.py                   # writes site/index.html; open it in a browser
python3 mealplan.py --date 2026-09-26 # plan as if it were another day
python3 -m unittest discover tests    # tests against a saved real menu snapshot
```
