import {
  CandlestickSeries,
  createChart,
  createSeriesMarkers,
  HistogramSeries,
  LineSeries,
  type IChartApi,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type IPriceLine,
  type SeriesMarker,
  type Time,
} from 'lightweight-charts'
import { useEffect, useRef } from 'react'

export interface PriceLineSpec {
  price: number | null | undefined
  color: string
  title: string
  style?: 0 | 1 | 2 | 3 | 4
  width?: number
}

export interface SupertrendPoint {
  time: number
  value: number | null
  bull: boolean
}

export interface ChartHandle {
  loadCandles: (bars: { time: number; open: number; high: number; low: number; close: number; volume: number }[]) => void
  setSupertrend: (pts: SupertrendPoint[]) => void
  setPriceLines: (lines: PriceLineSpec[]) => void
  setMarkers: (markers: SeriesMarker<Time>[]) => void
  fitContent: () => void
  clear: () => void
}

const ST_LINE_OPTS = { lineWidth: 2 as const, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false }

export function useChart(containerRef: React.RefObject<HTMLDivElement | null>) {
  const chartRef = useRef<IChartApi | null>(null)
  const candleRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const volRef = useRef<ISeriesApi<'Histogram'> | null>(null)
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null)
  const stSegmentsRef = useRef<ISeriesApi<'Line'>[]>([])
  const priceLinesRef = useRef<IPriceLine[]>([])

  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const chart = createChart(el, {
      layout: { background: { color: '#ffffff' }, textColor: '#656d76', fontFamily: 'ui-monospace, Menlo, monospace', fontSize: 11 },
      grid: { vertLines: { color: '#eaeef2' }, horzLines: { color: '#eaeef2' } },
      rightPriceScale: { borderColor: '#d0d7de' },
      timeScale: { borderColor: '#d0d7de', timeVisible: true, secondsVisible: false },
      crosshair: { mode: 0 },
      autoSize: true,
    })
    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: '#2fbf71', downColor: '#e5534b', borderVisible: false, wickUpColor: '#2fbf71', wickDownColor: '#e5534b',
    })
    const volSeries = chart.addSeries(HistogramSeries, {
      priceScaleId: 'vol', priceFormat: { type: 'volume' }, color: 'rgba(124,135,151,.35)',
    })
    chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } })
    const markers = createSeriesMarkers(candleSeries, [])

    chartRef.current = chart
    candleRef.current = candleSeries
    volRef.current = volSeries
    markersRef.current = markers

    return () => {
      chart.remove()
      chartRef.current = null
      candleRef.current = null
      volRef.current = null
      markersRef.current = null
      stSegmentsRef.current = []
      priceLinesRef.current = []
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const clearSupertrend = () => {
    const chart = chartRef.current
    if (!chart) return
    for (const s of stSegmentsRef.current) {
      try {
        chart.removeSeries(s)
      } catch {
        /* already gone */
      }
    }
    stSegmentsRef.current = []
  }

  const clearPriceLines = () => {
    const candle = candleRef.current
    if (!candle) return
    for (const pl of priceLinesRef.current) {
      try {
        candle.removePriceLine(pl)
      } catch {
        /* already gone */
      }
    }
    priceLinesRef.current = []
  }

  const handle: ChartHandle = {
    loadCandles(bars) {
      const candle = candleRef.current
      const vol = volRef.current
      if (!candle || !vol) return
      candle.setData(bars as any)
      vol.setData(
        bars.map((b) => ({
          time: b.time as any,
          value: b.volume,
          color: b.close >= b.open ? 'rgba(47,191,113,.30)' : 'rgba(229,83,75,.30)',
        })),
      )
      try {
        candle.priceScale().applyOptions({ autoScale: true })
      } catch {
        /* noop */
      }
    },
    setSupertrend(pts) {
      const chart = chartRef.current
      if (!chart) return
      clearSupertrend()
      const valid = (pts || []).filter((p) => p && p.value !== null && p.value !== undefined)
      let run: { time: Time; value: number }[] = []
      let runBull: boolean | null = null
      const flush = () => {
        if (!run.length) return
        try {
          const s = chart.addSeries(LineSeries, { ...ST_LINE_OPTS, color: runBull ? 'rgba(47,191,113,.85)' : 'rgba(229,83,75,.85)' })
          s.setData(run as any)
          stSegmentsRef.current.push(s)
        } catch {
          /* noop */
        }
      }
      for (const p of valid) {
        const b = !!p.bull
        if (runBull !== null && b !== runBull) {
          run.push({ time: p.time as any, value: p.value as number })
          flush()
          run = []
        }
        runBull = b
        run.push({ time: p.time as any, value: p.value as number })
      }
      flush()
    },
    setPriceLines(lines) {
      const candle = candleRef.current
      if (!candle) return
      clearPriceLines()
      for (const l of lines) {
        if (l.price === null || l.price === undefined) continue
        try {
          priceLinesRef.current.push(
            candle.createPriceLine({
              price: +l.price,
              color: l.color,
              title: l.title,
              lineStyle: l.style === undefined ? 2 : l.style,
              lineWidth: (l.width || 1) as any,
              axisLabelVisible: true,
            }),
          )
        } catch {
          /* noop */
        }
      }
    },
    setMarkers(markers) {
      markersRef.current?.setMarkers([...markers].sort((a, b) => (a.time as number) - (b.time as number)))
    },
    fitContent() {
      chartRef.current?.timeScale().fitContent()
    },
    clear() {
      candleRef.current?.setData([])
      volRef.current?.setData([])
      clearSupertrend()
      clearPriceLines()
      markersRef.current?.setMarkers([])
    },
  }

  return handle
}
