"""Cockpit UI for the restricted persistent service, using a serving-account RPC."""
import json
import re
import subprocess

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Input, Label, Select, Static, TextArea
from ai_toolbox_cockpit.views import ServersView
from ai_toolbox_cockpit.widgets import ConfirmModal, SearchableSelect
from ai_toolbox_cockpit.settings import get_setting, set_setting


def request_service(account, operation, args):
    if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', account):
        raise ValueError('Enter a local, unprivileged serving account name')
    command = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
        '-o', 'HostName=localhost', '-o', 'Port=22', '-o', 'StrictHostKeyChecking=yes',
        '-o', 'ClearAllForwardings=yes', '-o', 'ForwardAgent=no', '-o', 'PermitLocalCommand=no',
        f'{account}@localhost', '/usr/lib/ai-toolbox-cockpit/bin/python', '-I', '/usr/share/ai-box/service.py']
    result = subprocess.run(command, input=json.dumps(dict(operation=operation, args=args)),
                            capture_output=True, text=True, timeout=24 * 60 * 60)
    if result.returncode == 255:
        raise ValueError('Cannot connect to the serving account. Complete the one-time local SSH authorization and host-key setup in the management guide.')
    try:
        response = json.loads(result.stdout)
    except ValueError as error:
        raise ValueError('Serving account returned an invalid response. Check the installed image and SSH forced-command configuration.') from error
    if not response.get('ok'):
        raise ValueError(response.get('error', 'Service operation failed'))
    return response['result']


class ManagedServersView(ServersView):
    DEFAULT_CSS = """
    ManagedServersView .service-row { height: 3; margin: 0 0 1 0; }
    ManagedServersView .service-row Label { width: 20; content-align: left middle; }
    ManagedServersView .service-row Input, ManagedServersView .service-row Select,
    ManagedServersView .service-row SearchableSelect { width: 1fr; }
    ManagedServersView .service-actions { height: auto; layout: grid; grid-size: 3; grid-columns: 1fr; grid-gutter: 1; margin-bottom: 1; }
    ManagedServersView .service-actions Button { width: 1fr; height: 3; }
    ManagedServersView #service-message { height: auto; margin-bottom: 1; }
    ManagedServersView #service-output { height: 16; min-height: 8; }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.catalog_result = None
        self.existing_config = None
        self.prepared = None
        self.prepared_request = None
        self.busy = False

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static('Configure persistent inference here. It keeps running after Cockpit closes. '
                'Connect to the dedicated serving account; model downloads go directly into its private storage.', classes='view-note')
            with Horizontal(classes='service-row'):
                yield Label('Serving account')
                yield Input(value=str(get_setting('service_account', 'ai')), id='service-account')
                yield Button('Connect / refresh', id='service-inspect')
            with Horizontal(classes='service-row'):
                yield Label('Runtime')
                yield Select([('Vulkan RADV — managed profile', 'vulkan-radv')], value='vulkan-radv',
                             allow_blank=False, id='service-runtime')
            with Horizontal(classes='service-row'):
                yield Label('Model repository')
                yield SearchableSelect('Choose a model repository', id='service-repo')
                yield Button('Find quantizations', id='service-catalog')
            with Horizontal(classes='service-row'):
                yield Label('Quantization / files')
                yield Select([], prompt='Find quantizations first', id='service-model')
            with Horizontal(classes='service-row'):
                yield Label('AMD render device')
                yield Select([], prompt='Connect to discover devices', id='service-device')
            for name, value, title in [('context', '8192', 'Context tokens'), ('port', '8080', 'Local API port'),
                ('memory', 'auto', 'Service memory GiB'), ('reserve', '4', 'System reserve GiB')]:
                with Horizontal(classes='service-row'):
                    yield Label(title)
                    yield Input(value=value, id=f'service-{name}')
            with Horizontal(classes='service-actions'):
                for title, action in [('Download model', 'download'), ('Prepare & preview', 'prepare'),
                    ('Install & start', 'install'), ('Start', 'start'), ('Stop', 'stop'),
                    ('Restart', 'restart'), ('Check API', 'smoke'), ('View logs', 'logs')]:
                    yield Button(title, id=f'service-{action}', variant='primary' if action == 'install' else 'default')
            yield Static('Connect first. Initial account/GPU/linger and local SSH authorization require administrator provisioning.',
                         id='service-message', markup=False)
            yield TextArea('', read_only=True, id='service-output')

    def on_mount(self):
        repos = {entry['repo']: entry.get('name', entry['repo'])
                 for entry in self.app.model_catalog.backends['llama_cpp'].entries}
        selector = self.query_one('#service-repo', SearchableSelect)
        selector.set_options([(f'{name} — {repo}', repo) for repo, name in sorted(repos.items())])
        selector.value = 'unsloth/Qwen3.8-Flash-Next-GGUF'
        self.update_buttons()

    def set_platform(self, platform_id):
        # The persistent service has a reviewed Strix Halo Vulkan profile only.
        pass

    def refresh_model_inventory(self, backend_id):
        # Upstream operator downloads are separate from serving-account storage.
        pass

    def update_buttons(self):
        for widget in self.query('Input, Select, SearchableSelect'):
            widget.disabled = self.busy
        for button in self.query(Button):
            button.disabled = self.busy
        self.query_one('#service-install', Button).disabled = self.busy or self.prepared is None

    def account(self):
        return self.query_one('#service-account', Input).value.strip()

    def selection_request(self):
        model = None
        selected = self.query_one('#service-model', Select).value
        repo = str(self.query_one('#service-repo', SearchableSelect).value)
        if self.catalog_result and selected is not Select.BLANK and repo == self.catalog_result['repo']:
            model = dict(repo=repo, revision=self.catalog_result['revision'], first=str(selected))
        device = self.query_one('#service-device', Select).value
        if device is Select.BLANK:
            raise ValueError('Connect and select an accessible AMD render device')
        memory = self.query_one('#service-memory', Input).value.strip()
        settings = dict(render_device=str(device), port=int(self.query_one('#service-port', Input).value),
            context=int(self.query_one('#service-context', Input).value),
            memory_gib='auto' if memory == 'auto' else int(memory),
            reserve_gib=int(self.query_one('#service-reserve', Input).value))
        return dict(channel=str(self.query_one('#service-runtime', Select).value),
                    model=model, config=self.existing_config if model is None else None, settings=settings)

    def fingerprint(self):
        return json.dumps(dict(account=self.account(), selection=self.selection_request()), sort_keys=True)

    @on(Input.Changed)
    @on(Select.Changed)
    @on(SearchableSelect.Changed)
    def selection_changed(self):
        if self.prepared is not None:
            try:
                same = self.fingerprint() == self.prepared_request
            except (ValueError, TypeError):
                same = False
            if not same:
                self.prepared = None
                self.query_one('#service-install', Button).disabled = True

    @on(Button.Pressed)
    def pressed(self, event):
        if not event.button.id or not event.button.id.startswith('service-'):
            return
        event.stop()
        if self.busy:
            return
        action = event.button.id.removeprefix('service-')
        try:
            if action == 'catalog':
                args = {'repo': str(self.query_one('#service-repo', SearchableSelect).value)}
            elif action == 'prepare':
                args = self.selection_request()
                self.prepared_request = self.fingerprint()
            elif action == 'install':
                if self.prepared is None or self.fingerprint() != self.prepared_request:
                    raise ValueError('Settings changed; prepare and review the service again')
                args = {'config': self.prepared['config']}
            elif action == 'download':
                selected = self.selection_request()['model']
                if selected is None:
                    raise ValueError('Find and choose a model quantization first')
                args = {'model': selected}
            else:
                args = {}
            account = self.account()
            if action in {'download', 'install', 'stop', 'restart'}:
                if action == 'download':
                    variant = next(v for v in self.catalog_result['variants'] if v['first'] == args['model']['first'])
                    message = f"Download {len(variant['files'])} file(s), {variant['bytes'] / 1e9:.2f} GB, "
                    message += f"from {args['model']['repo']} at revision {args['model']['revision']}? "
                    message += 'Review the model license before proceeding. Interrupted downloads can be resumed.'
                elif action == 'install':
                    message = 'Install the reviewed service and start/restart inference? The current service may be interrupted.'
                else:
                    message = f'{action.capitalize()} inference? Active requests may be interrupted. Stop does not disable startup on later boots.'
                self.app.push_screen(ConfirmModal(message, yes_text='Continue'),
                    lambda confirmed: self.launch(account, action, args) if confirmed else None)
            else:
                self.launch(account, action, args)
        except (ValueError, TypeError, StopIteration) as error:
            self.query_one('#service-message', Static).update(str(error))

    def launch(self, account, operation, args):
        self.busy = True
        self.update_buttons()
        self.query_one('#service-message', Static).update(
            'Working… model downloads and checksum validation can take a long time. '
            'Prepare pulls the runtime; install loads the model in the background.')
        self.perform(account, operation, args)

    @work(thread=True, exclusive=True, group='managed-service')
    def perform(self, account, operation, args):
        try:
            result = request_service(account, operation, args)
            self.app.call_from_thread(self.completed, operation, result, None)
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            self.app.call_from_thread(self.completed, operation, None, str(error))

    def completed(self, operation, result, error):
        self.busy = False
        if error:
            if operation == 'prepare':
                self.prepared = None
            self.query_one('#service-message', Static).update(error)
            self.update_buttons()
            return
        output = result.get('logs', '')
        if operation == 'inspect':
            set_setting('service_account', self.account())
            self.existing_config = result.get('config')
            devices = [d for d in result['preflight'].get('render_devices', [])
                       if d['vendor'] == '0x1002' and d['accessible']]
            selector = self.query_one('#service-device', Select)
            selector.set_options([(d['path'], d['path']) for d in devices])
            if devices:
                selector.value = devices[0]['path']
            if self.existing_config:
                config = self.existing_config
                for field, key in [('context', 'context'), ('port', 'port'), ('memory', 'memory_gib'), ('reserve', 'reserve_gib')]:
                    self.query_one(f'#service-{field}', Input).value = str(config[key])
                if config['render_device'] in {d['path'] for d in devices}:
                    selector.value = config['render_device']
            preflight = result['preflight']
            output = ('Host preflight: ' + ('passed' if preflight.get('ready') else 'needs attention') +
                '\nRootless: ' + str(preflight.get('rootless', False)) +
                '\nSELinux enforcing: ' + str(preflight.get('selinux_enforcing', False)) +
                '\nLinux-visible memory GiB: ' + str(preflight.get('mem_total_gib', 'unknown')) +
                '\nAccessible AMD render devices: ' + ', '.join(d['path'] for d in devices) +
                '\nMissing tools: ' + ', '.join(k for k,v in preflight.get('tools', {}).items() if not v))
            if self.existing_config:
                output += '\n\nInstalled model: ' + self.existing_config['model_file']
                output += '\nInstalled runtime: ' + self.existing_config['image']
            if result.get('config_error'):
                output += '\n\nConfiguration needs attention: ' + result['config_error']
        elif operation == 'catalog':
            self.catalog_result = result
            self.prepared = None
            selector = self.query_one('#service-model', Select)
            selector.set_options([(f"{v['first']} — {v['bytes'] / 1e9:.2f} GB", v['first']) for v in result['variants']])
            preferred = next((v['first'] for v in result['variants'] if '/Qwen3.8-Flash-Next-UD-Q4_K_XL-' in v['first']), result['variants'][0]['first'])
            selector.value = preferred
            output = ('Source: ' + result['repo'] + '\nImmutable revision: ' + result['revision'] +
                      '\nChoose a quantization above. All of its weight shards will be downloaded and verified.')
        elif operation == 'prepare':
            self.prepared = result
            output = ('Runtime: ' + result['config']['image'] + '\n\n' + result['unit'] +
                      '\nModel SHA-256 manifest:\n' + json.dumps(result['config']['model_files'], indent=2))
        if not output:
            output = result.get('message', 'Operation completed.')
            if 'service' in result:
                output += '\nService: ' + result['service'].get('ActiveState', 'unknown')
        self.query_one('#service-output', TextArea).load_text(output)
        message = result.get('message', 'Operation completed. Review the result below.')
        if 'service' in result:
            message += ' State: ' + result['service'].get('ActiveState', 'unknown') + '.'
        if result.get('linger') is False:
            message += ' Administrator action: enable linger for this account to start inference at boot.'
        if operation == 'prepare':
            message = 'Validated. Review the runtime digest, model hashes and service settings below, then Install & start.'
        if operation == 'logs':
            message = 'Logs may contain sensitive information. Keep them out of Git and shared transcripts.'
        self.query_one('#service-message', Static).update(message)
        self.update_buttons()
