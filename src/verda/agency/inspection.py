"""Read-only descriptions: registered is not the same as a live connection."""
from pathlib import Path


def tool_description(registry, tool):
    handler = registry.handlers.get(tool.key)
    if handler:
        status, label = 'ready', 'Hazır · yerel kod'
        implementation = {'kind': 'python', 'entrypoint': handler.__module__ + '.' + handler.__name__}
    elif tool.transport in {'script', 'mcp'}:
        status, label = 'configured', 'Tanımlı · bağlantı doğrulanmadı'
        # Never expose argv, environment, URLs, or credentials through the inspector.
        script = next((Path(a).name for a in tool.command[1:] if a.endswith(('.py', '.js', '.mjs', '.sh'))), None)
        implementation = {'kind': tool.transport, 'script_name': script, 'remote_name': tool.remote_name}
    else:
        status, label = 'unconnected', 'Henüz bağlı değil'
        implementation = {'kind': 'unconnected'}
    if tool.effect == 'write':
        status, label = 'policy_missing', 'Gönderim politikası hazır değil'
    return {**tool.model_dump(exclude={'command', 'server_url'}), 'status': status, 'status_label': label,
            'implementation': implementation, 'available': registry.available(tool.key)}


def connection_descriptions(config):
    connections = []
    for key in sorted({t.resource for t in config.tools if t.resource}):
        browser = key == 'sahibinden'
        connections.append({'key': key, 'label': 'Browser · Sahibinden oturumu' if browser else key,
                            'kind': 'browser' if browser else 'resource',
                            'session_status': 'unconnected' if browser else 'not_observed',
                            'note': 'Mac Chrome oturumu Verda’ya henüz atanmadı. Jev de bağlı değil.' if browser
                                    else 'Araçların paylaştığı kaynak kuyruğu; bağlantı durumu araç kartında.'})
    return connections
