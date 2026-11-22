"""Exercise the installed management UI and its service boundary during builds."""
import asyncio
import importlib.util
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from textual.widgets import Button, DataTable, TabbedContent
from ai_toolbox_cockpit.backends.base import BackendServerPanel
from ai_toolbox_cockpit.settings import load_settings, save_settings

spec = importlib.util.spec_from_file_location("managed_cockpit", "/usr/share/ai-box/cockpit.py")
cockpit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cockpit)


async def check():
    with tempfile.TemporaryDirectory() as temporary:
        with patch.dict(os.environ, {"HOME": temporary, "XDG_CONFIG_HOME": temporary}):
            cockpit.initialize_settings()
            settings = load_settings()
            assert settings["active_platform"] == "strix-halo"
            assert settings["backends"]["llama_cpp"]["models_dir"].startswith(temporary)
            assert (Path(temporary) / "ai-toolbox-cockpit/config.json").stat().st_mode & 0o777 == 0o600
            settings["operator_custom_field"] = True
            settings["backends"]["llama_cpp"]["models_dir"] = temporary
            save_settings(settings)
            cockpit.initialize_settings()
            assert load_settings()["operator_custom_field"] is True
            assert load_settings()["backends"]["llama_cpp"]["models_dir"] == temporary
            (Path(temporary) / "example.gguf").touch()
            # No container engine or network is needed to exercise UI navigation.
            with patch("shutil.which", return_value=None), patch("socket.create_connection", side_effect=AssertionError("Unexpected network access")):
                app = cockpit.ManagedCockpitApp()
                async with app.run_test(size=(140, 45)) as pilot:
                    await pilot.pause()
                    assert app.query_one("#toolbox-catalog-table", DataTable).row_count > 0
                    assert app.query_one("#toolbox-create-update", Button)
                    assert not list(app.query(BackendServerPanel)), "Launch-capable server panels were mounted"
                    assert not list(app.query("#application-update-run")), "Self-upgrade was exposed"
                    tabs = app.query_one(TabbedContent)
                    tabs.active = "tab-models"
                    await pilot.pause()
                    assert app.query_one("#llama-local-models", DataTable).row_count == 1
                    tabs.active = "tab-servers"
                    await pilot.pause()
                    app.refresh_server_model_inventory("llama_cpp")
                    app.query_one("#servers-view").set_platform("strix-halo")
                    assert not list(app.query_one("#servers-view").query(Button))
    print("Cockpit UI: model inventory, toolbox management, private settings and service boundary passed.")


asyncio.run(check())
