"""Exercise the installed management UI and its service boundary during builds."""
import asyncio
import importlib.util
import os
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from textual.widgets import Button, DataTable, Input, TabbedContent, TextArea
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
            calls = []
            image = 'docker.io/kyuz0/amd-strix-halo-toolboxes@sha256:' + 'a' * 64
            first = 'Q4/model-00001-of-00002.gguf'
            files = {first:'b'*64, 'Q4/model-00002-of-00002.gguf':'c'*64}
            config = dict(image=image, model_dir=str(Path(temporary) / '.local/share/ai-box/models/test'),
                model_file=first, model_files=files, render_device='/dev/dri/renderD128',
                port=8080, context=8192, memory_gib='auto', reserve_gib=4)
            unit = 'Image=' + image + '\nPublishPort=127.0.0.1:8080:8080\nMemoryMax=124G\n--api-key-file /run/secrets/key'
            def transport(account, operation, args):
                calls.append((account, operation, args))
                if operation == 'inspect':
                    return dict(config=None, service={'ActiveState':'inactive'}, linger=True,
                        preflight={'render_devices':[dict(path='/dev/dri/renderD128', accessible=True, vendor='0x1002')]})
                if operation == 'catalog':
                    return dict(repo=args['repo'], revision='d'*40,
                        variants=[dict(first=first, files=files, bytes=100)])
                if operation == 'prepare':
                    assert args['settings']['reserve_gib'] == 4
                    assert args['settings']['memory_gib'] == 'auto'
                    return dict(config=config, unit=unit)
                if operation == 'install':
                    assert args['config'] == config
                return {'message':'done', 'service':{'ActiveState':'active'}, 'linger':True}
            with patch("shutil.which", return_value=None), \
                 patch("socket.create_connection", side_effect=AssertionError("Unexpected network access")), \
                 patch('service_panel.request_service', side_effect=transport):
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
                    async def press(action, confirm=False):
                        app.query_one('#service-' + action, Button).press()
                        await pilot.pause()
                        if confirm:
                            app.screen.query_one('#btn_yes', Button).press()
                            await pilot.pause()
                        await app.workers.wait_for_complete()
                        await pilot.pause()
                    await press('inspect')
                    await press('catalog')
                    await press('download', confirm=True)
                    await press('prepare')
                    assert not app.query_one('#service-install', Button).disabled
                    assert image in app.query_one('#service-output', TextArea).text
                    await press('install', confirm=True)
                    await press('smoke')
                    app.query_one('#service-context', Input).value = '16384'
                    await pilot.pause()
                    assert app.query_one('#service-install', Button).disabled, 'Stale preview could be installed'
                    assert [call[1] for call in calls] == ['inspect', 'catalog', 'download', 'prepare', 'install', 'smoke']
            # Verify the real client transport never builds an arbitrary shell command.
            from service_panel import request_service
            from types import SimpleNamespace
            with patch('service_panel.subprocess.run', return_value=SimpleNamespace(
                    returncode=0, stdout=json.dumps({'ok':True, 'result':{}}))) as run:
                request_service('ai', 'stop', {})
                command = run.call_args.args[0]
                assert 'ai@localhost' in command and 'ForwardAgent=no' in command
                assert 'shell' not in run.call_args.kwargs
                assert json.loads(run.call_args.kwargs['input']) == {'operation':'stop','args':{}}
                run.reset_mock()
                try:
                    request_service('ai;id', 'stop', {})
                except ValueError:
                    pass
                else:
                    raise AssertionError('Unsafe account was accepted')
                run.assert_not_called()
                # Image enrollment supplies a dedicated config without replacing
                # the operator's ordinary ~/.ssh/config.
                home = Path(temporary)
                (home / '.ssh').mkdir(exist_ok=True)
                (home / '.ssh/ai-box-config').write_text('Host localhost\n')
                with patch('service_panel.Path.home', return_value=home):
                    request_service('another-serving-user', 'inspect', {})
                    assert '-F' in run.call_args.args[0]
                    assert str(home / '.ssh/ai-box-config') in run.call_args.args[0]
                run.reset_mock()
                with patch('service_panel.pwd.getpwuid', return_value=SimpleNamespace(pw_name='serving-user')):
                    try:
                        request_service('serving-user', 'inspect', {})
                    except ValueError as error:
                        assert 'separate operator' in str(error)
                    else:
                        raise AssertionError('Cockpit admitted self-management under the serving account')
                run.assert_not_called()
    print("Cockpit UI: connected service setup/download/preview/install/API flow, stale-preview rejection and safe transport passed.")


asyncio.run(check())
