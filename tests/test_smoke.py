import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# bot.py validates the token at import time, but importing under tests must never
# connect to Discord. bot.run() is protected by the __main__ guard.
os.environ.setdefault("DISCORD_TOKEN", "smoke-test-token")
os.environ.pop("DISCORD_GUILD_ID", None)

import bot as app  # noqa: E402


class InteractionClassSmokeTests(unittest.TestCase):
    def make_draft(self, **overrides):
        values = dict(
            creator_id=1,
            creator_name="Tester",
            guild_id=100,
            channel_id=200,
            title="Smoke Test",
            description="Test event",
            date_text="10/01/2026",
            time_text="6:30 PM",
            repeat_rule="none",
        )
        values.update(overrides)
        return app.EventDraft(**values)

    def test_major_interaction_classes_exist(self):
        names = [
            "EventSetupView",
            "LocationModeView",
            "SetLocationModal",
            "VoteLocationModal",
            "AttendanceSetupModal",
            "AdditionalInfoSetupModal",
            "ImageUploadModal",
            "RoleSetupModal",
            "ScheduleSetupModal",
            "CapacitySetupModal",
            "RepeatSetupModal",
            "BasicEventModal",
            "EditEventModal",
            "FinalizeLocationModal",
            "ManageEventView",
            "TemplateManageView",
            "SavedLocationBookView",
        ]
        for name in names:
            with self.subTest(name=name):
                self.assertTrue(hasattr(app, name), f"Missing interaction class: {name}")

    def test_event_setup_view_instantiates(self):
        view = app.EventSetupView(self.make_draft())
        labels = {getattr(child, "label", None) for child in view.children}
        self.assertIn("Location", labels)
        self.assertIn("Attendance", labels)
        self.assertIn("Ping", labels)
        self.assertIn("Create Event", labels)

    def test_location_views_and_modals_instantiate(self):
        setup = app.EventSetupView(self.make_draft())
        chooser = app.LocationModeView(setup)
        labels = {getattr(child, "label", None) for child in chooser.children}
        self.assertTrue({"No Location", "Set Location", "Vote"}.issubset(labels))

        with patch.object(app, "get_saved_locations", return_value=[]):
            self.assertIsInstance(app.SetLocationModal(setup), app.discord.ui.Modal)
            self.assertIsInstance(app.VoteLocationModal(setup), app.discord.ui.Modal)

    def test_setup_modals_instantiate(self):
        setup = app.EventSetupView(self.make_draft())
        modal_types = [
            app.AttendanceSetupModal,
            app.AdditionalInfoSetupModal,
            app.ImageUploadModal,
            app.RoleSetupModal,
            app.ScheduleSetupModal,
            app.CapacitySetupModal,
            app.RepeatSetupModal,
        ]
        for modal_type in modal_types:
            with self.subTest(modal=modal_type.__name__):
                self.assertIsInstance(modal_type(setup), app.discord.ui.Modal)

    def test_ping_modal_offers_everyone_here_and_role(self):
        setup = app.EventSetupView(self.make_draft())
        modal = app.RoleSetupModal(setup)
        values = {option.value for option in modal.ping_type_select.options}
        self.assertEqual(values, {"none", "role", "here", "everyone"})

    def test_setup_embed_renders_ping_target(self):
        everyone = app.build_setup_embed(self.make_draft(ping_type="everyone"))
        here = app.build_setup_embed(self.make_draft(ping_type="here"))
        self.assertTrue(any(field.name == "📣 Ping" and field.value == "@everyone" for field in everyone.fields))
        self.assertTrue(any(field.name == "📣 Ping" and field.value == "@here" for field in here.fields))

    def test_ping_helpers(self):
        self.assertEqual(app.role_ping_content({"ping_type": "everyone", "ping_role_id": None}, None, None), "@everyone")
        self.assertEqual(app.role_ping_content({"ping_type": "here", "ping_role_id": None}, None, None), "@here")
        self.assertIsNone(app.role_ping_content({"ping_type": "none", "ping_role_id": None}, None, None))
        self.assertTrue(app.allowed_mentions_for_ping({"ping_type": "everyone", "ping_role_id": None}).everyone)
        self.assertTrue(app.allowed_mentions_for_ping({"ping_type": "here", "ping_role_id": None}).everyone)


class DatabaseSmokeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_database = app.DATABASE
        app.DATABASE = str(Path(self.tmp.name) / "smoke.db")
        app.initialize_database()

def tearDown(self):
    # Explicitly close any SQLite connection retained by a test.
    db = getattr(self, "db", None)

    if db is not None:
        try:
            db.close()
        except Exception:
            pass

        self.db = None

    # Reset bot database state before Windows tries to remove smoke.db.
    if hasattr(self.bot, "DB_PATH"):
        self.bot.DB_PATH = None

    # Windows can briefly retain the SQLite file handle.
    import gc
    import time

    gc.collect()
    time.sleep(0.1)

    self.tmp.cleanup()

    def test_ping_type_migration_exists(self):
        with app.get_db() as db:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(event_series)").fetchall()}
        self.assertIn("ping_type", columns)

    def test_series_copy_preserves_everyone_ping(self):
        with app.get_db() as db:
            cur = db.execute(
                """
                INSERT INTO event_series (
                    guild_id, channel_id, creator_id, creator_name,
                    title, description, date_text, time_text,
                    repeat_rule, location_mode, ping_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (100, 200, 1, "Tester", "Source", "", "10/01/2026", "6:30 PM", "none", "none", "everyone"),
            )
            source_id = cur.lastrowid

        copied_id = app.copy_series_configuration(
            source_id,
            guild_id=100,
            channel_id=201,
            creator_id=2,
            creator_name="Copy Tester",
            title="Copied",
            description="",
            date_text="10/08/2026",
            time_text="6:30 PM",
        )
        copied = app.get_series(copied_id)
        self.assertEqual(copied["ping_type"], "everyone")


if __name__ == "__main__":
    unittest.main()
