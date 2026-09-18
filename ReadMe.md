# NLB Jaya Incremental Scraper

Checks Firestore for the latest NLB Jaya draw already stored, scrapes only
newer draws from nlb.lk, and uploads them. If Firestore is already current,
it does nothing. Runs on GitHub Actions every 5 minutes.

## 1. Create the repo

1. Create a **new private** GitHub repository.
2. Push these files to it:
   - `nlb_jaya_incremental.py`
   - `requirements.txt`
   - `.github/workflows/nlb-jaya-scraper.yml`
3. **Do not commit `serviceAccount.json`** — it goes in as a secret instead (step 2).

## 2. Add your Firebase service account as a secret

1. Base64-encode your `serviceAccount.json` file:

   - macOS/Linux:
     ```bash
     base64 -i serviceAccount.json | tr -d '\n' | pbcopy   # copies to clipboard (macOS)
     # or, without pbcopy:
     base64 -i serviceAccount.json | tr -d '\n' > encoded.txt
     ```
   - This avoids any issues with quotes/newlines in the raw JSON.

2. In your GitHub repo: **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `FIREBASE_SERVICE_ACCOUNT_BASE64`
   - Value: paste the base64 string from step 1.
3. Save.

The workflow decodes this secret in memory at runtime — the credentials file
never touches disk or the repo.

## 3. First run / backfill

The incremental script expects Firestore to already have at least one NLB
Jaya draw stored (same as your other scrapers) — it reads the latest
`draw_number` and works forward from there. If the `NLB_Jaya` sub-collection
is empty, it will print a warning and do nothing rather than guess a start
point. Run your original full-range scraper once manually first (with a
local `serviceAccount.json`) to seed at least one draw, then let the
incremental workflow take over.

## 4. Schedule

The workflow runs on `*/5 * * * *` (every 5 minutes). GitHub Actions cron
runs on a best-effort basis and can be delayed a few minutes when GitHub is
under load — this is normal and not a bug in the script.

You can also trigger it manually anytime from the **Actions** tab via
"Run workflow" (enabled by `workflow_dispatch`).

## 5. Config (optional)

These are set as `env` vars in the workflow file — change them there if needed:

| Var | Default | Meaning |
|---|---|---|
| `MAIN_COLLECTION` | `NLB_Results_All` | Top-level Firestore collection |
| `SUB_COLLECTION` | `NLB_Jaya` | Sub-collection for this lottery |
| `MAX_NEW_DRAWS` | `10` | Safety cap on new draws per run |
| `USE_SELENIUM` | `false` | Set `true` if plain `requests` stops working and you add a Chrome setup step to the workflow |

## Notes

- Each run checks the next draw after the latest stored one. If it's not
  published yet, the script stops immediately (cheap, fast run). If several
  draws are missing (e.g. after downtime), it catches up up to
  `MAX_NEW_DRAWS` in one run.
- Uses plain `requests` + `BeautifulSoup` by default (no browser needed),
  matching your other incremental scrapers. Selenium is left as an option
  but isn't installed by default — add `selenium` to `requirements.txt` and
  a Chrome install step to the workflow if you ever need it.
