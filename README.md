# Oak Tree Gather

Oak Tree Gather is a Discord event-planning bot built for recurring
group events such as **MTG nights, snowboarding trips, gaming nights,
dinners, and other meetups**.

It combines customizable RSVPs, location selection and voting, recurring
events, reminders, saved locations, directions, capacity management,
carpool planning, templates, calendar integration, and persistent event
state in one Discord-native workflow.

## Features

### Event creation wizard

Create events with a guided Discord setup flow instead of a long slash
command.

Configure:

-   Event name, description, date, and time
-   Optional event image
-   Optional Discord role ping
-   Optional discussion thread
-   Optional attendance capacity and waitlist
-   One-time or recurring events
-   RSVP and location-voting cutoffs
-   Custom attendance and additional-info options

### Custom attendance

Attendance is a single-choice response group by default:

-   ✅ Going
-   ❓ Tentative
-   ❌ Can't Go

Organizers can customize the available attendance choices.

Participants can also use **↩️ Clear Response** to remove their RSVP and
additional-info responses.

### Additional information

Additional Info supports independent toggle options such as:

-   🚗 Driving
-   🙋 Need a Ride
-   ➕ Bringing +1
-   🍕 Eating

Guest counts, driver seats, and related planning data can be
incorporated into the event summary.

### Location modes

Each event can use one of three location modes:

-   **No Location** --- removes the location section entirely; useful
    for Discord or online events.
-   **Set Location** --- the organizer chooses from the configured
    locations.
-   **Vote** --- attendees vote on where the event should happen.

Location voting supports:

-   Single-choice voting
-   Multiple-choice voting
-   Public results
-   Anonymous results
-   Voting cutoffs
-   Automatic finalization when there is a single winner
-   Organizer finalization for ties

### Saved locations and directions

Servers can maintain reusable saved locations with a friendly name and
optional address.

During planning, attendees only see the friendly name:

``` text
Jon's House
Bob Ross's House
Local Game Store
```

The address remains hidden until the location is finalized. Once
finalized, the event can display the selected name, address, and a
**Directions** button that opens Google Maps.

Example using a fictional address:

``` text
📍 Jon's House
1842 Example Lantern Way
City, STATE 12345

[ 🗺️ Directions ]
```

Saved locations can be managed with:

``` text
/event locations
```

### Recurring events

Events can repeat:

-   Weekly
-   Every two weeks
-   Monthly

Each occurrence receives fresh:

-   RSVPs
-   Additional Info responses
-   Guest/carpool data
-   Waitlist state
-   Location votes
-   Finalized location

The recurring series keeps the reusable configuration while each
occurrence remains independent.

For recurring events, cutoffs can be configured as rules such as:

``` text
Location voting closes: 1 day before at 6:00 PM
RSVP closes:            1 day before at 9:00 PM
```

Oak Tree Gather calculates the correct cutoff for each future occurrence
automatically.

### RSVP and voting cutoffs

One-time events can use exact cutoff dates/times.

Recurring events can use relative cutoff rules.

After a cutoff:

-   Location voting can be locked
-   RSVP controls can be disabled
-   Reminders can be sent before the cutoff
-   A sole location winner can be finalized automatically
-   Ties remain available for organizer resolution

### Capacity and waitlist

Events can optionally have a maximum capacity.

When the event is full:

-   New Going responses are placed on the waitlist
-   Guests count toward capacity
-   When space becomes available, eligible waitlisted attendees can be
    promoted

### Event summaries

Event cards summarize useful planning information such as:

-   Expected attendance
-   Going / Tentative / Can't Go counts
-   Guests
-   Eating count
-   Drivers
-   Available seats
-   Attendees needing a ride
-   Waitlist status

Only configured options are shown.

### Discussion threads

Events can automatically create a Discord discussion thread.

The thread is useful for:

-   Food planning
-   Deck/game discussion
-   Carpool coordination
-   Arrival updates
-   Snowboarding conditions and logistics

Discussion threads automatically archive after the event ends.

### Role pings

An organizer can select a Discord role such as `@MTG` or `@Snowboarding`(which I made this for)
when creating an event.

The event can ping that role when posted.

### Templates

Frequently used event configurations can be saved as templates.

Examples:

``` text
MTG Night
Saturday Snowboarding
Dinner
```

Templates can be:

-   Renamed
-   Updated from an existing event
-   Deleted
-   Used as the starting point for a new event

Manage templates with:

``` text
/event templates
```

### Duplicate events

Existing events can be duplicated while starting with fresh responses
and votes.

``` text
/event duplicate
```

### Event management

Organizers can manage upcoming events from:

``` text
/event manage
```

Management tools include event editing, duplication, template creation,
capacity management, location finalization, calendar access,
discussion-thread access, and cancellation.

### Calendar integration

Events support:

-   **Add to Google** calendar links
-   Downloadable `.ics` files for Apple Calendar, Outlook, and other
    compatible calendar apps

Use:

``` text
/event calendar
```

when an `.ics` file is needed.

### Persistent state

Oak Tree Gather stores event state in SQLite.

Persistent data includes event series, occurrences, RSVPs, location
votes, saved locations, templates, waitlists, and related configuration.

Discord buttons and interactive views are restored when the bot
restarts.

------------------------------------------------------------------------

## Main Commands

  ---------------------------------------------------------------------
  Command                            Purpose
  ---------------------------------- ----------------------------------
  `/event create`                    Create a new event or start from a
                                     saved template

  `/event manage`                    Manage upcoming events

  `/event locations`                 Manage saved locations and
                                     directions addresses

  `/event templates`                 Rename, update, or delete saved
                                     templates

  `/event template_save`             Save an existing event as a
                                     reusable template

  `/event duplicate`                 Duplicate an event with fresh
                                     responses

  `/event calendar`                  Download an `.ics` calendar file

  `/event refresh`                   Refresh an event message from its
                                     stored state
  ---------------------------------------------------------------------

------------------------------------------------------------------------

## Typical Usage

### Example: MTG Night

1.  Run `/event create`.
2.  Enter the event name, description, date, and time.
3.  Open **Location**.
4.  Choose **Set Location**.
5.  Select saved locations such as:
    -   Jon's House
    -   Local Game Store
    -   Your Local Mountain
6.  Configure Attendance.
7.  Enable Additional Info such as:
    -   🍕 Eating
    -   ➕ Bringing +1
8.  Optionally select the `@MTG` role.
9.  Configure RSVP cutoff and recurrence if needed.
10. Create the event.
11. Finalize the location when decided.
12. Once finalized, the saved address and Google Maps Directions button
    become visible.

### Example: Saturday Snowboarding

1.  Run `/event create`.
2.  Choose **Vote** for Location.
3.  Add destinations such as:
    -   Winter Park
    -   Niseko
    -   Whistler
4.  Choose single-choice or multiple-choice voting.
5.  Choose public or anonymous results.
6.  Enable Additional Info such as:
    -   🚗 Driving
    -   🙋 Need a Ride
7.  Set the event to repeat weekly if desired.
8.  Configure voting and RSVP cutoffs.
9.  Create the event.
10. Each weekly occurrence starts with fresh votes and RSVPs.

### Example: Discord Gaming Night

1.  Run `/event create`.
2.  Choose **No Location**.
3.  Configure Attendance.
4.  Create the event.

The finished event contains no Location section.

------------------------------------------------------------------------

## Installation

There are two parts to installing Oak Tree Gather:

1.  Create and install a Discord application/bot.
2.  Run Oak Tree Gather either locally or on an always-on host such as
    Railway.

### Prerequisites

-   Python 3.11+ recommended
-   A Discord account
-   Permission to add applications/bots to the target Discord server
-   Git, if deploying from GitHub
-   A GitHub account for the Railway workflow below

### 1. Create the Discord application

1.  Open the Discord Developer Portal.
2.  Select **New Application**.
3.  Give the application a name such as `Oak Tree Gather`.
4.  Open the application's **Bot** section and create/configure the bot
    user.
5.  Generate or reset the bot token.
6.  Copy the token somewhere secure. It will become `DISCORD_TOKEN`.
7.  Copy the Discord server/guild ID where you want to test or run the
    bot. It will become `DISCORD_GUILD_ID`.

Never place the real bot token directly in `bot.py`, `README.md`, or
another committed file.

### 2. Install the bot in Discord

Configure the application so it can be installed to a server/guild.

Oak Tree Gather uses slash commands and interactive Discord components.
The installation should include the application-command and bot
capabilities required by Discord.

Recommended channel/server permissions include:

-   View Channels
-   Send Messages
-   Embed Links
-   Attach Files
-   Read Message History
-   Create Public Threads
-   Send Messages in Threads
-   Manage Threads

If Oak Tree Gather will ping roles, either make the target role
mentionable or grant the bot the appropriate permission to mention
roles.

After configuring installation, use Discord's generated install/invite
link and select the server where Oak Tree Gather should run.

### 3. Clone the repository

``` bash
git clone https://github.com/ipickjon/Oak-Tree-Gather.git
cd Oak-Tree-Gather
```

### 4. Create a virtual environment

Windows PowerShell:

``` powershell
python -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

macOS/Linux:

``` bash
python3 -m venv .venv
source .venv/bin/activate
```

### 5. Install dependencies

``` bash
pip install -r requirements.txt
```

The project currently uses:

``` text
discord.py>=2.7.1,<3
python-dotenv>=1.0
tzdata>=2025.1
```

### 6. Configure environment variables

Create a `.env` file in the project root:

``` env
DISCORD_TOKEN=your_discord_bot_token
DISCORD_GUILD_ID=your_discord_server_id
GATHER_TIMEZONE=America/Denver
GATHER_DATABASE=gather.db
```

Required:

-   `DISCORD_TOKEN` --- Discord bot token from the Developer Portal.
-   `DISCORD_GUILD_ID` --- server/guild ID used by the bot.

Optional:

-   `GATHER_TIMEZONE` --- IANA timezone used for event scheduling.
    Defaults to `America/Denver`.
-   `GATHER_DATABASE` --- SQLite database path. Defaults to `gather.db`.

The repository's `.gitignore` should keep `.env`, SQLite databases,
virtual environments, and local backups out of Git.

### 7. Run locally

``` bash
python bot.py
```

A successful startup should log that Oak Tree Gather connected to
Discord and synchronized its commands.

Open Discord and try:

``` text
/event create
```

For local development, the terminal must remain open and the computer
must remain online. For normal use, deploy the bot to an always-on host.

------------------------------------------------------------------------

## Deploying to Railway

Railway is a convenient deployment option because Oak Tree Gather is a
long-running Python process and its SQLite database can be placed on a
persistent Railway volume.

### Deployment architecture

``` text
GitHub repository
       │
       ▼
Railway service
       │
       ├── python bot.py
       │
       └── /data/
            └── gather.db
```

The source code lives in GitHub. Secrets live in Railway variables.
Runtime event data lives on the persistent volume.

### 1. Push the project to GitHub

Do not commit:

``` text
.env
gather.db
*.db
.venv/
__pycache__/
```

A minimal production repository contains:

``` text
Oak-Tree-Gather/
├── bot.py
├── requirements.txt
├── README.md
└── .gitignore
```

Push the repository to GitHub before creating the Railway service.

### 2. Create the Railway project

1.  Sign in to Railway.
2.  Choose **New Project**.
3.  Choose the option to deploy from a GitHub repository.
4.  Connect GitHub if prompted.
5.  Select `Oak-Tree-Gather`.
6.  Create the service.

The first deployment may fail until the required environment variables
are added. That is expected.

### 3. Add a persistent volume

Oak Tree Gather uses SQLite, so the database must not live only on the
deployment's temporary filesystem.

In Railway:

1.  Add a **Volume** to the Oak Tree Gather service.
2.  Set the volume mount path to:

``` text
/data
```

3.  Set Oak Tree Gather's production database path to:

``` text
/data/gather.db
```

This allows event data to survive application redeployments and
restarts.

### 4. Configure Railway variables

Open the Oak Tree Gather service's **Variables** tab and add:

``` env
DISCORD_TOKEN=your_real_discord_bot_token
DISCORD_GUILD_ID=your_discord_server_id
GATHER_TIMEZONE=America/Denver
GATHER_DATABASE=/data/gather.db
```

Do not add quotation marks around the values unless the value itself
requires them.

Do not upload your local `.env` file to the public GitHub repository.

### 5. Configure the start command

Railway can often detect a Python application's start command
automatically. To make the deployment explicit, set the service's Start
Command to:

``` bash
python bot.py
```

Oak Tree Gather is a Discord bot, not a web application, so it does not
require a public HTTP domain or web server.

### 6. Deploy

Deploy or redeploy the Railway service.

Open the deployment logs and confirm that Oak Tree Gather:

-   starts successfully,
-   connects to Discord,
-   initializes/migrates its database,
-   restores persistent event controls, and
-   synchronizes its slash commands.

Then open Discord and test:

``` text
/event create
```

### 7. Verify persistence

Before relying on the hosted bot:

1.  Create a test event.
2.  Add an RSVP or location vote.
3.  Restart/redeploy the Railway service.
4.  Return to the existing Discord event.
5.  Confirm its buttons still work and its previous state is still
    present.

If the state survives, `/data/gather.db` is being persisted correctly.

### 8. Stop the local bot

Once the Railway deployment is healthy, stop any local copy of:

``` bash
python bot.py
```

Running the same Discord bot locally and in Railway at the same time can
create duplicate background scheduling/reminder behavior.

Your PC can now be turned off; Railway runs the bot independently.

### Updating the hosted bot

For normal updates:

``` bash
git add .
git commit -m "Describe the change"
git push
```

When Railway is connected to the GitHub repository with automatic
deployments enabled, pushes to the configured branch can trigger a new
deployment.

The persistent `/data/gather.db` volume remains separate from the source
deployment.

### Production checklist

Before inviting a group to rely on the bot, verify:

-   [ ] `.env` is not tracked by Git.
-   [ ] No Discord token is hard-coded in the repository.
-   [ ] `*.db` is ignored by Git.
-   [ ] Railway has `DISCORD_TOKEN`.
-   [ ] Railway has `DISCORD_GUILD_ID`.
-   [ ] Railway has `GATHER_TIMEZONE`.
-   [ ] Railway has `GATHER_DATABASE=/data/gather.db`.
-   [ ] A Railway volume is mounted at `/data`.
-   [ ] Start Command is `python bot.py`.
-   [ ] The local bot is stopped.
-   [ ] `/event create` works from Discord.
-   [ ] Existing event controls still work after a Railway restart.
-   [ ] Thread creation works if enabled.
-   [ ] Role pings work if enabled.
-   [ ] Saved-location addresses are not present in the public
    repository.

### Backups

The SQLite database contains the live event state and may contain saved
addresses. Treat it as private application data.

Periodically back up the Railway volume/database, especially before
major schema or feature changes.

Never commit a production database backup to the public GitHub
repository.

------------------------------------------------------------------------

## Discord Permissions

The bot should have access to the channel where events are created and
posted.

Typical permissions include:

-   View Channels
-   Send Messages
-   Embed Links
-   Attach Files
-   Read Message History
-   Create Public Threads
-   Send Messages in Threads
-   Manage Threads

Role pings may also require the target role to be mentionable or the bot
to have permission to mention roles.

------------------------------------------------------------------------

## Project Structure

The current release intentionally keeps the application simple:

``` text
Oak-Tree-Gather/
├── bot.py
├── requirements.txt
├── README.md
└── .gitignore
```

SQLite databases, virtual environments, secrets, and local backups are
excluded from version control.

------------------------------------------------------------------------

## Privacy

Oak Tree Gather can store Discord event data and optional saved
addresses.

For saved locations:

-   Friendly location names can be shown during planning.
-   Saved addresses remain hidden while a location is undecided.
-   The address is shown to attendees only after that location is
    finalized.
-   No-location events expose no location information.

Because SQLite may contain event responses and saved addresses, database
files should remain private and should never be committed to a public
repository.

------------------------------------------------------------------------

## Development Status

Oak Tree Gather is actively developed and currently focused on
small-group event coordination inside Discord.

The project began as a customizable alternative for coordinating
recurring MTG nights and snowboarding trips, then expanded into a
general-purpose event workflow supporting online and in-person events.
