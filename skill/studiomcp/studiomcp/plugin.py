import json
import os
from pathlib import Path
import shutil
import tempfile
import xml.etree.ElementTree as ET


def reload_setting():
    local_app_data = os.environ.get('LOCALAPPDATA')
    if not local_app_data:
        return None
    settings = Path(local_app_data) / 'Roblox/GlobalSettings_13.xml'
    try:
        value = ET.parse(settings).find(".//bool[@name='ReloadLocalPluginsOnChange']")
        return None if value is None else value.text == 'true'
    except (OSError, ET.ParseError):
        return None


def _cdata(text: str) -> str:
    return '<![CDATA[' + text.replace(']]>', ']]]]><![CDATA[>') + ']]>'


def _script_suffix(filename: str) -> tuple[str, str | None] | None:
    for suffix, cls in (
        ('.server.luau', 'Script'),
        ('.server.lua', 'Script'),
        ('.client.luau', 'LocalScript'),
        ('.client.lua', 'LocalScript'),
        ('.module.luau', 'ModuleScript'),
        ('.module.lua', 'ModuleScript'),
        ('.luau', 'ModuleScript'),
        ('.lua', 'ModuleScript'),
    ):
        if filename.endswith(suffix):
            return filename[:-len(suffix)], cls
    return None


def _build_rbxmx(source: Path, root_name: str) -> str:
    ref_counter = [0]

    def next_ref() -> int:
        val = ref_counter[0]
        ref_counter[0] += 1
        return val

    def emit_item(cls: str, name: str, code: str | None, children_xml: list[str], indent: str) -> str:
        ref = next_ref()
        lines = [
            f'{indent}<Item class="{cls}" referent="{ref}">',
            f'{indent}  <Properties>',
            f'{indent}    <string name="Name">{name}</string>',
        ]
        if cls == 'Script':
            lines.append(f'{indent}    <token name="RunContext">0</token>')
        if code is not None:
            lines.append(f'{indent}    <string name="Source">{_cdata(code)}</string>')
        lines.append(f'{indent}  </Properties>')
        lines.extend(children_xml)
        lines.append(f'{indent}</Item>')
        return '\n'.join(lines)

    def build_dir(directory: Path, item_name: str, indent: str) -> str:
        init_cls = 'Folder'
        init_code = None
        for init_file, cls in (
            ('init.server.luau', 'Script'),
            ('init.server.lua', 'Script'),
            ('init.client.luau', 'LocalScript'),
            ('init.client.lua', 'LocalScript'),
            ('init.module.luau', 'ModuleScript'),
            ('init.module.lua', 'ModuleScript'),
            ('init.luau', 'ModuleScript'),
            ('init.lua', 'ModuleScript'),
        ):
            candidate = directory / init_file
            if candidate.is_file():
                init_cls = cls
                init_code = candidate.read_text(encoding='utf-8')
                break

        children_xml = []
        for entry in sorted(directory.iterdir(), key=lambda p: p.name):
            if entry.name.startswith('.') or entry.name == 'plugin.json':
                continue
            if entry.is_dir():
                children_xml.append(build_dir(entry, entry.name, indent + '  '))
            elif entry.is_file():
                parsed = _script_suffix(entry.name)
                if parsed and parsed[0] != 'init':
                    child_name, child_cls = parsed
                    children_xml.append(
                        emit_item(child_cls, child_name, entry.read_text(encoding='utf-8'), [], indent + '  ')
                    )
        return emit_item(init_cls, item_name, init_code, children_xml, indent)

    if source.is_file():
        parsed = _script_suffix(source.name) or (root_name, 'Script')
        body = emit_item(parsed[1], root_name, source.read_text(encoding='utf-8'), [], '  ')
    else:
        body = build_dir(source, root_name, '  ')
    return f'<roblox version="4">\n{body}\n</roblox>\n'


def save_plugin(directory: str = None) -> dict:
    if directory:
        base = Path(directory).resolve(strict=True)
    else:
        base = Path(os.path.abspath(__file__)).parents[1] / 'plugin'
        base.mkdir(parents=True, exist_ok=True)
    config = base / 'plugin.json'
    if config.exists():
        metadata = json.loads(config.read_text(encoding='utf-8'))
    elif (base / 'init.server.luau').exists():
        metadata = {'name': 'StudioMCP', 'source': '.', 'rootName': 'StudioMCP'}
    else:
        config.write_text(json.dumps({'name': base.name, 'source': '.', 'rootName': base.name}, indent=2), encoding='utf-8')
        return {'metadataCreated': str(config), 'installed': False, 'message': 'Edit plugin.json name, source and rootName, then call savePlugin again.'}
    name = metadata['name']
    if not name or any(c in name for c in '/\\:*?"<>|') or name in ('.', '..'):
        raise ValueError('Invalid plugin name')
    source = (base / metadata['source']).resolve(strict=True)
    if not source.is_relative_to(base):
        raise ValueError('Plugin source must be inside its directory')
    local_app_data = os.environ.get('LOCALAPPDATA') or str(Path.home() / 'AppData/Local')
    plugins = Path(local_app_data) / 'Roblox/Plugins'
    plugins.mkdir(parents=True, exist_ok=True)
    output = plugins / (name + '.rbxmx')
    xml_text = _build_rbxmx(source, metadata.get('rootName', name))
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=plugins, suffix='.tmp', delete=False) as staging:
        staging.write(xml_text)
        staged = Path(staging.name)
    try:
        try:
            os.replace(staged, output)
        except PermissionError:
            try:
                shutil.copyfile(staged, output)
            except OSError as error:
                raise RuntimeError(f'Cannot update installed plugin {output}: {error}. Reload or disable this local plugin in Studio, then savePlugin again; Studio need not be closed.') from error
    finally:
        staged.unlink(missing_ok=True)
    enabled = reload_setting()
    message = 'Saved successfully.'
    if enabled is False:
        message += ' Automatic plugin reload is disabled in Studio’s saved settings; you may need to reload manually.'
    elif enabled is None:
        message += ' Could not detect the automatic reload setting; you may need to reload manually.'
    return {'installed': True, 'output': str(output), 'name': name,
            'autoReloadEnabled': enabled, 'settingSource': 'Studio saved settings (unsaved session changes may differ)',
            'message': message}
