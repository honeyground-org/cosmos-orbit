"""Project provider observations into durable events, without inventing progress."""


def recorder(store, task_id, provider, alias=None):
    def record(kind, payload):
        with store.db:
            store._event(task_id, kind, dict(payload, provider=provider, alias=alias or provider))
    return record


def provider_event(event):
    """Only expose tool lifecycle and public messages; never reasoning or tool output."""
    kind = event.get('type')
    item = event.get('item') or {}
    if kind in ('item.started', 'item.completed') and isinstance(item, dict):
        tool = item.get('type')
        if tool in ('command_execution', 'file_change', 'mcp_tool_call', 'web_search'):
            finished = kind == 'item.completed'
            return [('provider_tool_finished' if finished else 'provider_tool_started', {
                'tool_id': item.get('id'), 'tool': tool,
                'direction': 'in' if finished else 'out',
                'layer': 'evidence' if tool == 'file_change' else 'execution',
                'note': tool + (' returned' if finished else ' started'),
                'exit_code': item.get('exit_code'), 'status': item.get('status')})]
        if tool == 'agent_message' and kind == 'item.completed':
            return [('provider_message', {'layer': 'execution', 'direction': 'in',
                                         'note': str(item.get('text', ''))[:600]})]
    message = event.get('message') or {}
    blocks = message.get('content', []) if isinstance(message, dict) else []
    results = []
    if kind in ('assistant', 'user') and isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get('type') == 'tool_use':
                results.append(('provider_tool_started', {'tool_id': block.get('id'),
                    'tool': block.get('name'), 'direction': 'out', 'layer': 'execution',
                    'note': str(block.get('name', 'Tool')) + ' started'}))
            elif block.get('type') == 'tool_result':
                results.append(('provider_tool_finished', {'tool_id': block.get('tool_use_id'),
                    'direction': 'in', 'layer': 'execution', 'note': 'Tool result received',
                    'failed': bool(block.get('is_error'))}))
    return results
