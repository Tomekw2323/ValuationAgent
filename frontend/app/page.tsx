"use client";

import { useCallback, useState } from "react";

type ValuationPayload = {
  ticker: string;
  wacc_pct: number;
  growth_pct: number;
  wide_moat: boolean;
  buyback: boolean;
  fresh_data: boolean;
};

type ValuationResponse = {
  ticker?: string;
  currency?: string;
  market_price?: number | null;
  report?: string;
  dcf?: { price?: number | null };
  multiples?: { median?: number | null };
  sensitivity?: {
    pesymistyczny?: number | null;
    bazowy?: number | null;
    optymistyczny?: number | null;
  };
  ddm?: { wycena?: number | null } | null;
  error?: string;
};

function fmtNumber(value: unknown, currency = ""): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return `${n.toLocaleString("en-US", { maximumFractionDigits: 2 })}${
    currency ? ` ${currency}` : ""
  }`;
}

export default function Home() {
  const [ticker, setTicker] = useState("AAPL");
  const [wacc, setWacc] = useState(10);
  const [growth, setGrowth] = useState(5);
  const [wideMoat, setWideMoat] = useState(false);
  const [buyback, setBuyback] = useState(true);
  const [freshData, setFreshData] = useState(false);

  const [loading, setLoading] = useState(false);
  const [status, setStatus] = useState({ text: "", kind: "" as "" | "ok" | "err" });
  const [result, setResult] = useState<ValuationResponse | null>(null);

  const runValuation = useCallback(async () => {
    const t = ticker.trim().toUpperCase();
    if (!t) {
      setStatus({ text: "Enter a ticker.", kind: "err" });
      return;
    }

    setLoading(true);
    setStatus({ text: "Calling backend...", kind: "" });
    setResult(null);

    const payload: ValuationPayload = {
      ticker: t,
      wacc_pct: wacc,
      growth_pct: growth,
      wide_moat: wideMoat,
      buyback,
      fresh_data: freshData,
    };

    try {
      const resp = await fetch("/api/valuate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });

      const data = (await resp.json()) as ValuationResponse;
      if (!resp.ok) {
        throw new Error(data.error || `HTTP ${resp.status}`);
      }

      setResult(data);
      setStatus({ text: "Done.", kind: "ok" });
    } catch (err) {
      setStatus({
        text: `Error: ${err instanceof Error ? err.message : String(err)}`,
        kind: "err",
      });
    } finally {
      setLoading(false);
    }
  }, [buyback, freshData, growth, ticker, wacc, wideMoat]);

  const market = result?.market_price;
  const dcf = result?.dcf?.price;
  const median = result?.multiples?.median;
  const currency = result?.currency ?? "";
  const sensitivity = result?.sensitivity ?? {};
  const ddm = result?.ddm?.wycena;
  const potential =
    dcf !== undefined && dcf !== null && market !== undefined && market !== null && market > 0
      ? ((dcf - market) / market) * 100
      : null;

  const metricRows =
    result != null
      ? [
          { key: "Ticker", val: result.ticker ?? "—" },
          { key: "Market price", val: fmtNumber(market, currency) },
          { key: "DCF", val: fmtNumber(dcf, currency) },
          { key: "Multiples median", val: fmtNumber(median, currency) },
          {
            key: "DCF vs market",
            val:
              potential === null
                ? "—"
                : `${potential >= 0 ? "+" : ""}${potential.toFixed(1)}%`,
          },
          { key: "DDM", val: fmtNumber(ddm, currency) },
          { key: "DCF bear", val: fmtNumber(sensitivity.pesymistyczny, currency) },
          { key: "DCF base", val: fmtNumber(sensitivity.bazowy, currency) },
          { key: "DCF bull", val: fmtNumber(sensitivity.optymistyczny, currency) },
        ]
      : [];

  return (
    <div className="container">
      <div className="card">
        <h1 className="valuationTitle">Valuation Agent - Frontend HTML</h1>
        <div className="subtitle">Minimal web UI for the local valuation backend.</div>
      </div>

      <div className="card">
        <div className="grid">
          <div>
            <label className="fieldLabel" htmlFor="ticker">
              Ticker
            </label>
            <input
              id="ticker"
              type="text"
              value={ticker}
              onChange={(e) => setTicker(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && runValuation()}
              placeholder="e.g. AAPL, PKN.WA"
            />
          </div>
          <div className="rangeWrap">
            <label className="fieldLabel" htmlFor="wacc">
              WACC:{" "}
              <span aria-live="polite">{Number(wacc).toFixed(1)}%</span>
            </label>
            <input
              id="wacc"
              type="range"
              min={5}
              max={20}
              step={0.5}
              value={wacc}
              onChange={(e) => setWacc(Number(e.target.value))}
            />
          </div>
          <div className="rangeWrap">
            <label className="fieldLabel" htmlFor="growth">
              FCF growth:{" "}
              <span aria-live="polite">{Number(growth).toFixed(1)}%</span>
            </label>
            <input
              id="growth"
              type="range"
              min={0}
              max={25}
              step={0.5}
              value={growth}
              onChange={(e) => setGrowth(Number(e.target.value))}
            />
          </div>
        </div>

        <div className="checks">
          <label>
            <input
              type="checkbox"
              checked={wideMoat}
              onChange={(e) => setWideMoat(e.target.checked)}
            />{" "}
            Wide moat (+0.5pp terminal growth)
          </label>
          <label>
            <input
              type="checkbox"
              checked={buyback}
              onChange={(e) => setBuyback(e.target.checked)}
            />{" "}
            Include buyback adjustment
          </label>
          <label>
            <input
              type="checkbox"
              checked={freshData}
              onChange={(e) => setFreshData(e.target.checked)}
            />{" "}
            Refresh data (skip cache)
          </label>
        </div>

        <button type="button" className="runButton" disabled={loading} onClick={runValuation}>
          {loading ? "Working..." : "Run valuation"}
        </button>

        <div className={`status ${status.kind}`.trim()}>{status.text}</div>
      </div>

      <div className={`card ${result ? "" : "hidden"}`}>
        <h2 className="sectionTitle">Summary</h2>
        <div className="metrics">
          {metricRows.map((row) => (
            <div key={row.key} className="metric">
              <div className="k">{row.key}</div>
              <div className="v">{row.val}</div>
            </div>
          ))}
        </div>
      </div>

      <div className={`card ${result?.report ? "" : "hidden"}`}>
        <h2 className="sectionTitle">Agent narrative (Markdown)</h2>
        <div className="report">{result?.report ?? ""}</div>
      </div>
    </div>
  );
}
