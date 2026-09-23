# Oak Tree Gather — Phase 3.2 (Saved Locations + No Location)

This build is based on Phase 3.1 and keeps the existing event, template, recurrence, cutoff, RSVP, waitlist, carpool, calendar, thread, and advanced voting features.

## New in 3.2

### Saved Locations / private addresses
Use `/event locations` to maintain a server location book.

Each saved place has:
- a short public name, e.g. `Ryan's House`
- an optional directions address

Addresses are intentionally hidden while an event is being planned or voted on. Event setup, location buttons, voting results, and templates show only the location name.

When a location is finalized, the public event changes to show:

```
📍 Location
Ryan's House
1234 Example St, Denver, CO 80202

[ 🗺️ Directions ]
```

The Directions button opens Google Maps with the saved address as the destination.

Existing events snapshot their selected addresses, so deleting or editing a Saved Location does not break old events.

### No Location
Location Setup now offers:
- 🌐 No Location
- 🗳️ Vote
- 📍 Set Location

Use **No Location** for Discord game nights, online hangouts, remote study sessions, etc. The public event card will omit the Location section and all location controls entirely.

### Set Location behavior
For organizer-selected events, choosing a location counts as finalizing it. The address and Directions button appear only after the organizer selects the location.

### Vote behavior
During voting, only names/counts are shown. Addresses remain hidden. After the vote is finalized manually or automatically at cutoff, the winning location's address and Directions button appear.

## Upgrade
Back up first:

```powershell
Copy-Item bot.py bot_phase3_1_backup.py
Copy-Item gather.db gather_pre_locations_backup.db
```

Replace `bot.py` with this build and keep your existing `gather.db`.

Then:

```powershell
pip install -r requirements.txt
python bot.py
```

Database migrations run automatically.

## Suggested test
1. Run `/event locations` and save `Ryan's House` with a test address.
2. Create an event using **Set Location** and choose Ryan's House.
3. Confirm the setup UI shows only `Ryan's House`, not the address.
4. Create the event. Before selection, confirm no address is visible.
5. Select Ryan's House. Confirm the address + Directions button appear.
6. Create a second event with **No Location** and confirm the public event has no Location section.
7. Create a Vote event using saved locations and confirm addresses stay hidden until finalization.
