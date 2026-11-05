YARD BIRDS - your Haikubox, as a real website
=============================================

TO USE IT
  Double-click  "Open Yard Birds.bat"

  - A black window opens (that's the program - leave it open).
  - Your browser opens to http://localhost:8765
  - The FIRST time, it downloads your whole Haikubox history and
    a photo for every bird. That takes a few minutes; the page fills
    in by itself as it goes.
  - After that it only grabs what's new (a few seconds), and it
    re-checks your Haikubox every hour while the window is open.
  - Close the black window when you're done.

  Tip: bookmark http://localhost:8765 - it works any time the
  black window is open.

WHAT'S ON THE SITE
  Today        - who's in the yard today (with photos), new arrivals,
                 regulars that have gone quiet, busiest hours
  Field Guide  - every bird you've ever had, with photos. Search,
                 sort, filter by Year-round / Summer / Winter / Migrant
  Calendar     - week-by-week strip for each bird; compare two years
  Trends       - species per month, daily activity, top birds,
                 year-vs-year (who's new, who's missing)
  Click any bird for: more photos, description, a link to
  All About Birds for ID tips and songs, its history, busiest
  months, time of day, a download of its data, and a "hide"
  button for misidentifications.
  "CSV" (top right) downloads everything as a spreadsheet.

ON YOUR PHONE
  With Yard Birds running, click "Phone" (top right) on the computer.
  Scan the QR code with your phone's camera (phone must be on your
  home Wi-Fi). Then:
    iPhone:  Safari > Share > Add to Home Screen
    Android: Chrome > menu > Add to Home screen / Install app
  First time only: if Windows Firewall pops up on the computer,
  choose Allow for Private networks.

  Away from home: install the free Tailscale app on the computer and
  the phone, sign in to both with the same account, restart Yard
  Birds, and the Phone button shows a second address that works
  anywhere (private to you).

  The computer must be awake with the Yard Birds window open.
  To stop sharing on Wi-Fi, set "phone_access": false in config.json.

IS IT REALLY THAT BIRD?  (the Review tab)
  Every bird gets a rating: Verified by you / Confident / Probable /
  Check it / Unlikely. It's based on:
   - how many times and on how many different days it was heard
     (one or two detections = Check it)
   - the Haikubox's own High/Medium/Low confidence on each detection
   - whether people actually report that bird near you (iNaturalist)
   - your own answers
  The Review tab lists the doubtful ones. Play the recording, compare
  it with "Hear the real song", then press "Yes, it's here" or
  "No, it's wrong" ("wrong" hides it everywhere; you can undo).
  You can also tick/cross individual recordings in any bird's page.

  Recordings: the Haikubox only shares the last ~24 hours of clips,
  so Yard Birds saves them while it's running. Older clips can still
  be played in the Haikubox app/website.

  "Reported near you" needs your rough location. Open
  YardBirds/config.json in Notepad and fill in, for example:
     "latitude": 43.62,
     "longitude": -116.20,
  (your town is close enough; find it by right-clicking your house on
  Google Maps - the first number is latitude). Radius is
  nearby_radius_km (default 25).

YOUR DATA
  YardBirds/data/yard_birds.db   - the database (SQLite)
  Copy that one file to back everything up.

SETTINGS  (YardBirds/config.json - open with Notepad)
  serial            your Haikubox serial number
  years_of_history  how far back to download (default 3)
  port              8765
  auto_sync_minutes 60

OLDER HISTORY FROM CSV (optional)
  If you have Haikubox CSV exports, drop them in YardBirds/imports
  and press "Sync now". You never need to do this for new data.

IF SOMETHING LOOKS WRONG
  The black window shows what happened. Raw samples of what the
  Haikubox API sent are saved in YardBirds/data/api_samples.json -
  send that file along if you need help.

Needs: Python 3.8 or newer (no extra packages).
