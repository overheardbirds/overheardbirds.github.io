# Yard Birds

A website for my Haikubox: who's in the yard today (with photos), a field guide,
week-by-week calendar, trends, and a review page for doubtful detections.

**How it stays up to date:** a GitHub Action runs every hour, pulls new detections
from the Haikubox API, saves the database to the `data` branch, and republishes
the site on GitHub Pages. Nothing needs to run on my computer.

Settings (Settings > Secrets and variables > Actions):

| Secret | What |
|---|---|
| `HAIKUBOX_SERIAL` | Haikubox serial number (required) |
| `YB_LATITUDE`, `YB_LONGITUDE` | Rough location, for the "reported near you" check (optional) |

Update by hand: Actions > Update Yard Birds > Run workflow.

The same code also runs locally on Windows: double-click `Open Yard Birds.bat`.
