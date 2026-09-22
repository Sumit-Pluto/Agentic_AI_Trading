export function ChartContainer({ innerRef, hint }: { innerRef: React.RefObject<HTMLDivElement | null>; hint?: string | null }) {
  return (
    <div className="flex-1 min-h-[220px] relative">
      <div ref={innerRef} className="absolute inset-0" />
      {hint ? (
        <div className="absolute inset-0 flex items-center justify-center text-faint text-sm pointer-events-none">{hint}</div>
      ) : null}
    </div>
  )
}
