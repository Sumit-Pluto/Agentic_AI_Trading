import { useEffect, useState } from 'react'
import { fmt } from '../lib/format'
import type { TreeNode } from '../lib/types'
import { Chip, Switch } from './ui'

function seedExpanded(node: TreeNode, depth: number, set: Set<string>) {
  if (depth < 2 && node.children?.length) set.add(node.key)
  node.children?.forEach((c) => seedExpanded(c, depth + 1, set))
}

function collectAll(node: TreeNode, set: Set<string>) {
  set.add(node.key)
  node.children?.forEach((c) => collectAll(c, set))
}

function Node({
  node,
  depth,
  expanded,
  toggleExpanded,
  readOnly,
  onToggleEnabled,
}: {
  node: TreeNode
  depth: number
  expanded: Set<string>
  toggleExpanded: (key: string) => void
  readOnly: boolean
  onToggleEnabled?: (node: TreeNode, enabled: boolean) => void
}) {
  const hasKids = !!node.children?.length
  const isOpen = expanded.has(node.key)
  const chipTone = node.score === null || node.score === undefined ? 'na' : node.passed ? 'ok' : 'bad'

  return (
    <>
      <div
        className={`flex items-center gap-2.5 py-1.5 pr-2 rounded-md border border-transparent hover:bg-bg3 ${
          hasKids && !readOnly ? 'cursor-pointer' : ''
        } ${!node.enabled ? 'opacity-100' : ''}`}
        style={{ paddingLeft: 8 + depth * 20 }}
        onClick={() => hasKids && !readOnly && toggleExpanded(node.key)}
      >
        <span
          className={`w-3.5 shrink-0 text-center text-[10px] select-none inline-block transition-transform text-dim ${
            !hasKids ? 'invisible' : ''
          } ${isOpen ? 'rotate-90' : ''}`}
        >
          ▶
        </span>
        <span className={`font-semibold text-[12.5px] whitespace-nowrap ${!node.enabled ? 'opacity-40' : ''}`} title={node.key}>
          {node.name}
        </span>
        <Chip tone={chipTone} title={`threshold ${fmt(node.threshold, 0)}`} className={!node.enabled ? 'opacity-45' : ''}>
          {node.score === null || node.score === undefined ? 'n/a' : fmt(node.score, 1)}
        </Chip>
        <span className={`font-mono text-[11px] text-dim whitespace-nowrap ${!node.enabled ? 'opacity-40' : ''}`}>
          w {fmt(node.weight, 2)}
        </span>
        <span
          className={`text-[11.5px] text-dim overflow-hidden text-ellipsis whitespace-nowrap flex-1 min-w-[40px] ${
            !node.enabled ? 'opacity-40' : ''
          }`}
          title={node.detail || ''}
        >
          {node.detail || ''}
        </span>
        {!readOnly && (
          <Switch checked={!!node.enabled} onChange={(v) => onToggleEnabled?.(node, v)} />
        )}
      </div>
      {hasKids && isOpen && node.children!.map((c) => (
        <Node key={c.key} node={c} depth={depth + 1} expanded={expanded} toggleExpanded={toggleExpanded} readOnly={readOnly} onToggleEnabled={onToggleEnabled} />
      ))}
    </>
  )
}

export function AgentTreeView({
  tree,
  readOnly = false,
  onToggleEnabled,
}: {
  tree: TreeNode | null
  readOnly?: boolean
  onToggleEnabled?: (node: TreeNode, enabled: boolean) => void
}) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set())

  useEffect(() => {
    if (!tree) return
    const set = new Set<string>()
    if (readOnly) collectAll(tree, set)
    else seedExpanded(tree, 0, set)
    setExpanded(set)
  }, [tree, readOnly])

  if (!tree) return null

  const toggleExpanded = (key: string) => {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  return (
    <div>
      <Node node={tree} depth={0} expanded={expanded} toggleExpanded={toggleExpanded} readOnly={readOnly} onToggleEnabled={onToggleEnabled} />
    </div>
  )
}
