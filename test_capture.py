"""Smoke test for CaptureConsole."""
from src.voidx.ui.capture import CaptureConsole
from src.voidx.ui.tree import OutputTree

tree = OutputTree()
parent = tree.new_node(parent=tree.root, node_type='subagent', header='sub')
cc = CaptureConsole(tree, parent)

# 1. console.width
assert cc.console.width == 80
print('[PASS] console.width == 80')

# 2. step_header
cc.step_header(1, 3, 'test-agent')
assert parent.children[0].node_type == 'turn'
assert 'Step 1/3' in parent.children[0].header
print('[PASS] step_header')

# 3. print (no-op)
cc.print('hello world')  # should not raise
print('[PASS] print (no-op)')

# 4. markdown (no-op)
cc.markdown('# Title')  # should not raise
print('[PASS] markdown (no-op)')

# 5. thinking (no-op)
cc.thinking('reasoning...')  # should not raise
print('[PASS] thinking (no-op)')

# 6. tool_call
cc.tool_call('read', {'file_path': 'test.py'})
assert cc._current_tool.node_type == 'tool_call'
assert cc._current_tool.status == 'running'
print('[PASS] tool_call')

# 7. tool_done
cc.tool_done('read', 1.5, ok=True)
assert cc._current_tool.status == 'done'
assert cc._current_tool.elapsed == 1.5
print('[PASS] tool_done')

# 8. tool_result
cc.tool_result('line1\nline2\nline3')
assert cc._current_tool.children[0].node_type == 'tool_result'
assert cc._current_tool.children[0].header == 'line1'
assert len(cc._current_tool.children[0].body_lines) == 3
print('[PASS] tool_result')

# 9. diff
cc.diff('+added\n-removed', 'changes')
assert cc._current_tool.children[1].node_type == 'diff'
assert cc._current_tool.children[1].header == 'changes'
assert len(cc._current_tool.children[1].body_lines) == 2
print('[PASS] diff')

# 10. error
cc.error('something broke')
assert parent.children[-1].node_type == 'error'
assert 'something broke' in parent.children[-1].header
print('[PASS] error')

# 11. warn
cc.warn('heads up')
assert parent.children[-1].node_type == 'warn'
print('[PASS] warn')

# 12. sep (no-op)
cc.sep()
print('[PASS] sep (no-op)')

# 13. tool_done error status
cc.tool_done('bad-tool', 1.0, ok=False)
assert cc._current_tool.status == 'error'
assert '✗' in cc._current_tool.header
print('[PASS] tool_done error status')

# 14. tool_result falls back to parent when no current tool
cc2 = CaptureConsole(tree, parent)
cc2.tool_result('orphan result')
assert parent.children[-1].node_type == 'tool_result'
print('[PASS] tool_result fallback to parent')

print()
print('=== ALL 14 TESTS PASSED ===')
