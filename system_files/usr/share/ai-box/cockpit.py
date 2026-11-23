"""Image-owned management UI integration; trusted operator tooling, not a sandbox."""
import argparse
import sys
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Footer, Header, Label, Static, TabbedContent, TabPane

from ai_toolbox_cockpit.app import AiToolboxCockpitApp, generate_banner
from ai_toolbox_cockpit.settings import load_settings, save_settings
from ai_toolbox_cockpit.updates import installed_version
from ai_toolbox_cockpit.views import ModelsView, ToolboxesView
from ai_toolbox_cockpit.widgets import SearchableSelect

# This directory is image-owned; do not import operator-site Python packages.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from service_panel import ManagedServersView


class ManagedCockpitApp(AiToolboxCockpitApp):
    SUB_TITLE = "Persistent inference, models and trusted toolboxes"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(generate_banner(self.version), id="title-banner")
        with Horizontal(id="platform-row"):
            yield Label("Platform", id="platform-select-label")
            yield SearchableSelect("Select hardware platform", id="platform-select")
        with TabbedContent(initial="tab-servers"):
            with TabPane("Inference service", id="tab-servers"):
                yield ManagedServersView(id="servers-view")
            with TabPane("Operator toolboxes", id="tab-toolboxes"):
                yield ToolboxesView(self.toolbox_catalog, self.active_platform_id, id="toolboxes-view")
            with TabPane("Operator models", id="tab-models"):
                yield ModelsView(self.model_catalog, id="models-view")
        yield Footer()

    def check_application_update(self) -> None:
        # Cockpit and its dependencies are updated through reviewed image builds.
        # Do not offer pipx upgrades or perform upstream application update checks.
        pass


def initialize_settings() -> None:
    settings = load_settings()
    settings.setdefault("active_platform", "strix-halo")
    backends = settings.setdefault("backends", {})
    if not isinstance(backends, dict):
        raise ValueError("Cockpit backends settings must be an object")
    llama = backends.setdefault("llama_cpp", {})
    if not isinstance(llama, dict):
        raise ValueError("Cockpit llama_cpp settings must be an object")
    llama.setdefault("models_dir", str(Path.home() / ".local/share/ai-toolbox-cockpit/models"))
    if not save_settings(settings):
        raise OSError("Could not save private Cockpit operator settings")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manage models, runtimes and persistent rootless inference through Cockpit."
    )
    parser.add_argument("--version", action="version", version=installed_version())
    parser.parse_args()
    initialize_settings()
    ManagedCockpitApp().run()


if __name__ == "__main__":
    main()
