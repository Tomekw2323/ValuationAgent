import { NextResponse } from "next/server";

const DEFAULT_API = "http://127.0.0.1:8000";

export async function POST(request: Request) {
  const base = (process.env.VALUATION_API_URL ?? DEFAULT_API).replace(/\/$/, "");
  const body = await request.text();

  let upstream: Response;
  try {
    upstream = await fetch(`${base}/api/valuate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
      cache: "no-store",
    });
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    return NextResponse.json(
      {
        error: `Cannot reach valuation API at ${base}. Start: python web_frontend.py (${msg})`,
      },
      { status: 502 },
    );
  }

  const text = await upstream.text();
  const ct = upstream.headers.get("Content-Type") ?? "application/json";
  return new NextResponse(text, {
    status: upstream.status,
    headers: { "Content-Type": ct },
  });
}
